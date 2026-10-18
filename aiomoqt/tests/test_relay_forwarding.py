"""Relay forwarding: publisher -> relay -> subscriber, three sessions.

The relay used to be control-plane only, so nothing here could be
tested: it acked SUBSCRIBE and no object ever moved. These exercise the
upstream subscription, the object fan-out, and the §2.4 prefix matching
that decides which publisher a SUBSCRIBE reaches.
"""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import (
    FilterType, MOQTMessageType, MOQTRequestError, RequestErrorCode,
    SubscribeDoneCode,
)
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.tools import moq_interop_relay as relay

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 15310
_FRAMES = [b"o0", b"o1", b"o2"]


class _Pub(PublishedTrack):
    """Emits its frames as one group on a single subgroup, then stops."""

    frames = _FRAMES

    async def generate(self, session, track_alias):
        from aiomoqt.messages import SubgroupHeader
        sid = await session.open_uni_stream()
        hdr = SubgroupHeader(
            track_alias=track_alias, group_id=0, subgroup_id=0,
            publisher_priority=200, extensions_present=True,
            prof=session._profile)
        session.stream_write(sid, hdr.serialize().data)
        for i, payload in enumerate(self.frames):
            buf = hdr.next_object(payload=payload, extensions=None,
                                  object_id=i)
            await session.stream_write_drain(sid, buf.data)
        self._largest = (0, len(self.frames) - 1)


def _reset_relay_state():
    relay._announced.clear()
    relay._tracks.clear()


async def _run(port, pub_ns, sub_ns, draft=18):
    """Publisher announces pub_ns; subscriber subscribes to sub_ns."""
    _reset_relay_state()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=True, draft=draft)
    handle = await server.serve()
    got = []
    try:
        pub_client = MOQTClient("localhost", port, path="/", use_quic=True,
                                verify_tls=False, supported_drafts=draft)
        async with pub_client.connect() as pub_session:
            await pub_session.client_session_init()
            # The track the subscriber names, under the announced prefix.
            track = _Pub(pub_session, sub_ns, "video")
            track.attach()
            await pub_session.publish_namespace(namespace=pub_ns,
                                                wait_response=True)
            await asyncio.sleep(0.1)

            sub_client = MOQTClient("localhost", port, path="/",
                                    use_quic=True, verify_tls=False,
                                    supported_drafts=draft)
            async with sub_client.connect() as sub_session:
                await sub_session.client_session_init()
                sub = SubscribedTrack(
                    sub_session, sub_ns, "video",
                    on_object=lambda m, s, t, g, sg: got.append(
                        (bytes(m.payload), m.publisher_priority)))
                try:
                    await sub.subscribe(timeout=8.0)
                except Exception as e:
                    return None, e
                for _ in range(150):
                    if len(got) >= len(_FRAMES):
                        break
                    await asyncio.sleep(0.02)
        return got, None
    finally:
        handle.close()
        _reset_relay_state()


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [18, 16, 14])
async def test_objects_traverse_the_relay(draft):
    got, err = await _run(_BASE_PORT + 10 + draft, "relay/ns", "relay/ns",
                          draft=draft)
    assert err is None, f"d{draft} subscribe failed: {err}"
    assert [p for p, _ in got] == _FRAMES


@pytest.mark.asyncio
async def test_subscribe_reaches_a_prefix_publisher():
    # §2.4: a SUBSCRIBE under an announced prefix reaches its publisher.
    got, err = await _run(_BASE_PORT + 1, "relay", "relay/sub")
    assert err is None, f"prefix subscribe failed: {err}"
    assert [p for p, _ in got] == _FRAMES


@pytest.mark.asyncio
async def test_unannounced_namespace_still_errors():
    got, err = await _run(_BASE_PORT + 2, "relay/ns", "other/ns")
    assert err is not None, "subscribe to an unannounced namespace was acked"


@pytest.mark.asyncio
async def test_publisher_priority_survives_the_relay():
    """A relay forwards the publisher's priority, not one of its own —
    a subscriber's scheduling depends on it."""
    got, err = await _run(_BASE_PORT + 30, "relay/ns", "relay/ns", draft=18)
    assert err is None, f"subscribe failed: {err}"
    assert [prio for _, prio in got] == [200] * len(_FRAMES), got


def _keys_for(namespace):
    ns = tuple(p.encode() for p in namespace.split("/"))
    return [k for k in relay._tracks if k[0] == ns]


async def _publish_cycle(port, namespace="relay/ns"):
    """Flow B: PUBLISH held for a subscriber, delivered, then the
    publisher ends the track with its session still open. Returns the
    publisher's session and track."""
    pub_client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=18)
    pub_cm = pub_client.connect()
    pub_session = await pub_cm.__aenter__()
    await pub_session.client_session_init()
    track = _Pub(pub_session, namespace, "video")
    await track.publish(announce_namespace=False, publish_track=True)
    await asyncio.sleep(0.1)

    got = []
    sub_client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=18)
    async with sub_client.connect() as sub_session:
        await sub_session.client_session_init()
        sub = SubscribedTrack(
            sub_session, namespace, "video",
            on_object=lambda m, s, t, g, sg: got.append(bytes(m.payload)))
        await sub.subscribe(timeout=8.0)
        for _ in range(150):
            if len(got) >= len(_FRAMES):
                break
            await asyncio.sleep(0.02)
    assert got == _FRAMES, f"publish cycle delivered {got}"
    return pub_cm, pub_session, track


def _end_track(pub_session, track):
    """PUBLISH_DONE on the PUBLISH's own request, session left open."""
    sub = track._subs[0]
    pub_session.subscribe_done(
        request_id=sub.request_id, status_code=0x2,
        stream_count=sub.stream_count, reason="track ended")


def _track_for(namespace):
    keys = _keys_for(namespace)
    return relay._tracks[keys[0]] if keys else None


@pytest.mark.asyncio
async def test_publish_first_is_answered_then_raised():
    """§9.5 publish-first: a publisher that blocks on PUBLISH_OK before
    it subscribes must be answered.

    Holding the reply until a subscriber arrives deadlocks — it cannot
    subscribe until we answer, and we would not answer until it
    subscribed. Answer with Forward State 0 and raise it instead.
    """
    port = _BASE_PORT + 52
    _reset_relay_state()
    relay._track_subs.clear()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=True, draft=18)
    handle = await server.serve()
    try:
        pub_client = MOQTClient("localhost", port, path="/", use_quic=True,
                                verify_tls=False, supported_drafts=18)
        async with pub_client.connect() as pub_session:
            await pub_session.client_session_init()
            track = _Pub(pub_session, "relay/pf", "video")
            await track.publish(announce_namespace=False,
                                publish_track=True)
            for _ in range(100):
                t = _track_for("relay/pf")
                if t is not None and t.parked is not None:
                    break
                await asyncio.sleep(0.02)
            t = _track_for("relay/pf")
            assert t is not None and t.parked is not None, (
                "PUBLISH was held with no subscriber to wait for")

            got = []
            sub_client = MOQTClient("localhost", port, path="/",
                                    use_quic=True, verify_tls=False,
                                    supported_drafts=18)
            async with sub_client.connect() as sub_session:
                await sub_session.client_session_init()
                await sub_session.subscribe_tracks(namespace="relay/pf")
                msg = await sub_session.await_publish(timeout=8.0)
                sub_session._track_aliases[msg.track_alias] = msg.request_id
                sub_session.register_object_handler(
                    msg.track_alias,
                    lambda m, s, tr, g, sg: got.append(bytes(m.payload)))
                sub_session.publish_ok(msg, forward=1)
                for _ in range(200):
                    if len(got) >= len(_FRAMES):
                        break
                    await asyncio.sleep(0.02)
            assert got == _FRAMES, f"publish-first delivered {got}"
            assert _track_for("relay/pf").parked is None, (
                "forward state was never raised")
    finally:
        handle.close()
        _reset_relay_state()
        relay._track_subs.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [14, 16, 18])
async def test_no_subscriber_to_offer_means_the_reply_is_answered(draft):
    """With nobody the track can be offered to, no reply is coming from
    anywhere, so the PUBLISH is answered with Forward State 0 rather
    than held. Holding it deadlocks a publisher that waits for
    PUBLISH_OK before it subscribes, at every draft that has a forward
    state to answer with."""
    port = _BASE_PORT + 54 + draft
    _reset_relay_state()
    relay._track_subs.clear()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=True, draft=draft)
    handle = await server.serve()
    try:
        pub_client = MOQTClient("localhost", port, path="/", use_quic=True,
                                verify_tls=False, supported_drafts=draft)
        async with pub_client.connect() as pub_session:
            await pub_session.client_session_init()
            track = _Pub(pub_session, "relay/pre18", "video")
            await track.publish(announce_namespace=False,
                                publish_track=True)
            await asyncio.sleep(0.3)
            t = _track_for("relay/pre18")
            assert t is not None, "no track registered"
            assert t.parked is not None, (
                f"d{draft} held the PUBLISH with nobody to offer it to; "
                f"a publisher waiting on PUBLISH_OK before it subscribes "
                f"deadlocks")
    finally:
        handle.close()
        _reset_relay_state()
        relay._track_subs.clear()


@pytest.mark.asyncio
async def test_a_waiting_prefix_subscriber_still_holds_the_reply():
    """With a prefix subscriber already registered the offer's
    PUBLISH_OK is the reply the publisher gets, so the PUBLISH stays
    held. Answering it early instead broke this path once."""
    port = _BASE_PORT + 53
    _reset_relay_state()
    relay._track_subs.clear()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=True, draft=16)
    handle = await server.serve()
    try:
        got = []
        sub_client = MOQTClient("localhost", port, path="/", use_quic=True,
                                verify_tls=False, supported_drafts=16)
        async with sub_client.connect() as sub_session:
            await sub_session.client_session_init()
            sub = SubscribedTrack(
                sub_session, "relay/sf", None,
                on_object=lambda m, s, t, g, sg: got.append(bytes(m.payload)))
            sub_task = asyncio.create_task(sub.subscribe(timeout=8.0))
            for _ in range(100):
                if relay._track_subs:
                    break
                await asyncio.sleep(0.02)
            assert relay._track_subs, "prefix subscriber never registered"

            pub_client = MOQTClient("localhost", port, path="/",
                                    use_quic=True, verify_tls=False,
                                    supported_drafts=16)
            async with pub_client.connect() as pub_session:
                await pub_session.client_session_init()
                track = _Pub(pub_session, "relay/sf", "video")
                await track.publish(announce_namespace=False,
                                    publish_track=True)
                await asyncio.wait_for(sub_task, timeout=10)
                for _ in range(250):
                    if len(got) >= len(_FRAMES):
                        break
                    await asyncio.sleep(0.02)
                t = _track_for("relay/sf")
                assert t is not None, "no track registered"
                assert t.parked is None, (
                    "the reply was answered early instead of held")
            assert got == _FRAMES, f"subscribe-first delivered {got}"
    finally:
        handle.close()
        _reset_relay_state()
        relay._track_subs.clear()


@pytest.mark.asyncio
async def test_publish_done_retires_the_track_with_the_session_open():
    """A publisher's PUBLISH_DONE ends the track even though its
    session stays open.

    Session close is not a usable trigger on its own: a peer's
    connection can linger for its whole idle timeout after it is done,
    and a track still registered in that window is handed to the next
    subscriber, which receives nothing and waits out its own timeout.
    """
    port = _BASE_PORT + 50
    _reset_relay_state()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=True, draft=18)
    handle = await server.serve()
    try:
        pub_cm, pub_session, track = await _publish_cycle(port)
        assert _keys_for("relay/ns"), "track was not registered"
        try:
            _end_track(pub_session, track)
            for _ in range(100):
                if not _keys_for("relay/ns"):
                    break
                await asyncio.sleep(0.02)
            assert not _keys_for("relay/ns"), (
                "PUBLISH_DONE left the track registered; a later "
                "SUBSCRIBE would be acked from a track that has ended")
        finally:
            await pub_cm.__aexit__(None, None, None)
    finally:
        handle.close()
        _reset_relay_state()


@pytest.mark.asyncio
async def test_a_new_publisher_retires_the_previous_track():
    """§"Relays": one publisher per Full Track Name. A PUBLISH for a
    key that still holds a finished track must retire it rather than
    reuse it — a reused track drops every object the new publisher
    sends, and its subscriber times out with nothing."""
    port = _BASE_PORT + 51
    _reset_relay_state()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=True, draft=18)
    handle = await server.serve()
    try:
        pub_cm, pub_session, track = await _publish_cycle(port)
        try:
            _end_track(pub_session, track)
            await asyncio.sleep(0.2)
            # Second cycle on the same key, first publisher still
            # connected: it must deliver on its own fresh track.
            pub_cm2, pub_session2, _ = await _publish_cycle(port)
            await pub_cm2.__aexit__(None, None, None)
        finally:
            await pub_cm.__aexit__(None, None, None)
    finally:
        handle.close()
        _reset_relay_state()


@pytest.mark.asyncio
async def test_relay_serves_a_second_publish_subscribe_cycle():
    """A relay process outlives the sessions it serves. Publish,
    subscribe, deliver, disconnect — then do it all again against the
    same relay. The second cycle must deliver too: state bound to the
    first cycle's sessions cannot be reused after they close.
    """
    port = _BASE_PORT + 40
    _reset_relay_state()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=True, draft=18)
    handle = await server.serve()
    try:
        for cycle in (1, 2):
            got = []
            pub_client = MOQTClient("localhost", port, path="/",
                                    use_quic=True, verify_tls=False,
                                    supported_drafts=18)
            async with pub_client.connect() as pub_session:
                await pub_session.client_session_init()
                track = _Pub(pub_session, "relay/ns", "video")
                await track.publish(announce_namespace=True,
                                    publish_track=False)
                await asyncio.sleep(0.1)
                sub_client = MOQTClient("localhost", port, path="/",
                                        use_quic=True, verify_tls=False,
                                        supported_drafts=18)
                async with sub_client.connect() as sub_session:
                    await sub_session.client_session_init()
                    sub = SubscribedTrack(
                        sub_session, "relay/ns", "video",
                        on_object=lambda m, s, t, g, sg: got.append(
                            bytes(m.payload)))
                    await sub.subscribe(timeout=8.0)
                    for _ in range(150):
                        if len(got) >= len(_FRAMES):
                            break
                        await asyncio.sleep(0.02)
            assert got == _FRAMES, f"cycle {cycle} delivered {got}"
            # Reconnect promptly: the previous session has not yet
            # been observed as closed, which is the case that broke.
            await asyncio.sleep(0.2)
    finally:
        handle.close()
        _reset_relay_state()


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [16, 18])
async def test_tracks_published_on_one_session_each_reach_a_subscriber(draft):
    """Three PUBLISHes on one session, held by the relay: the reply to
    each must start that track, not the last one published."""
    port = _BASE_PORT + 80 + draft
    names = ("a", "b", "c")
    got = {name: [] for name in names}
    _reset_relay_state()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=True, draft=draft)
    handle = await server.serve()
    try:
        pub_client = MOQTClient("localhost", port, path="/", use_quic=True,
                                verify_tls=False, supported_drafts=draft)
        async with pub_client.connect() as pub_session:
            await pub_session.client_session_init()
            for name in names:
                track = _Pub(pub_session, "relay/multi", name)
                track.frames = [name.encode() + f for f in _FRAMES]
                await track.publish(announce_namespace=False,
                                    publish_track=True)
            await asyncio.sleep(0.1)
            sub_client = MOQTClient("localhost", port, path="/",
                                    use_quic=True, verify_tls=False,
                                    supported_drafts=draft)
            async with sub_client.connect() as sub_session:
                await sub_session.client_session_init()
                # One at a time: each subscription's frames arrive before
                # the next SUBSCRIBE.
                for name in names:
                    sub = SubscribedTrack(
                        sub_session, "relay/multi", name,
                        on_object=lambda m, s, t, g, sg, n=name:
                            got[n].append(bytes(m.payload)))
                    await sub.subscribe(timeout=8.0)
                    for _ in range(150):
                        if len(got[name]) >= len(_FRAMES):
                            break
                        await asyncio.sleep(0.02)
    finally:
        handle.close()
        _reset_relay_state()
    assert got == {name: [name.encode() + f for f in _FRAMES]
                   for name in names}, got


# -- per-subscriber Forward State and filters (§5.1, §5.1.2) --------------

_FLOW_NS = "relay/flow"
_SIZES = {"video": 300, "audio": 100}
_GROUP = 5
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _flow_port(case, use_quic):
    return _BASE_PORT + 120 + 2 * case + (not use_quic)


def _relayed(name):
    return relay._tracks.get(((b"relay", b"flow"), name.encode()))


@contextlib.asynccontextmanager
async def _flowing(port, use_quic, draft=18):
    """A relay with video and audio PUBLISHed on one session, two groups
    of each already published (Largest {1, 4}). Yields a coroutine that
    opens a subscriber session and returns (session, objects), mapping
    each track name to the (group, object) Locations received."""
    _reset_relay_state()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    by_size = {size: name for name, size in _SIZES.items()}
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def _session():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s

            pub = await _session()
            for name, size in _SIZES.items():
                track = PublishedTrack(pub, namespace=_FLOW_NS,
                                       trackname=name, object_size=size,
                                       group_size=_GROUP, rate=500)
                track.prefill(2)
                await track.publish(announce_namespace=False,
                                    publish_track=True)
            for _ in range(100):
                if all(_relayed(n) is not None and _relayed(n).parked
                       for n in _SIZES):
                    break
                await asyncio.sleep(0.02)

            async def _subscriber():
                s = await _session()
                objects = {name: [] for name in _SIZES}

                def _on_object(msg, _size, _ts, group_id, _subgroup_id):
                    name = by_size.get(len(msg.payload or b""))
                    if name is not None:
                        objects[name].append((group_id, msg.object_id))
                s.on_object_received = _on_object
                return s, objects

            yield _subscriber
    finally:
        handle.close()
        _reset_relay_state()


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [20, 22])
@_TRANSPORTS
async def test_published_tracks_reach_subscribers_at_d20_and_d22(draft,
                                                                use_quic):
    # From d20 PUBLISH_OK carries only EXPIRES; Forward State follows in a
    # REQUEST_UPDATE.
    port = _flow_port(4 + (draft == 22), use_quic)
    async with _flowing(port, use_quic, draft=draft) as subscriber:
        rx, objects = await subscriber()
        for name in _SIZES:
            await rx.subscribe(_FLOW_NS, name, forward=1, wait_response=True)
        for _ in range(150):
            if all(objects[n] for n in _SIZES):
                break
            await asyncio.sleep(0.02)
        assert all(objects[n] for n in _SIZES), objects
        assert rx._close_err is None


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
async def test_a_forward_0_subscriber_gets_nothing_until_it_updates(
        use_quic):
    # §9.5: only a Forward State 1 subscriber raises the publisher.
    async with _flowing(_flow_port(0, use_quic), use_quic) as subscriber:
        rx, objects = await subscriber()
        await rx.subscribe(_FLOW_NS, "audio", forward=1, wait_response=True)
        video = await rx.subscribe(_FLOW_NS, "video", forward=0,
                                   wait_response=True)
        await asyncio.sleep(0.3)
        assert objects["video"] == [] and objects["audio"]
        assert _relayed("video").parked is not None
        await rx.request_update(video.request_id, forward=1,
                                wait_response=True)
        for _ in range(100):
            if objects["video"]:
                break
            await asyncio.sleep(0.02)
        assert objects["video"]
        assert rx._close_err is None


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
async def test_each_absolute_range_ends_at_its_own_end_group(use_quic):
    async with _flowing(_flow_port(1, use_quic), use_quic) as subscriber:
        rx, objects = await subscriber()
        ends = {"video": 3, "audio": 4}
        done = {}
        for name, end in ends.items():
            ok = await rx.subscribe(
                _FLOW_NS, name, forward=1,
                filter_type=FilterType.ABSOLUTE_RANGE, start_group=2,
                start_object=0, end_group=end, wait_response=True)
            done[name] = asyncio.get_running_loop().create_future()
            rx.register_publish_done_handler(ok.request_id,
                                             done[name].set_result)
        for name, end in ends.items():
            status = await asyncio.wait_for(done[name], 5.0)
            assert status.status_code == SubscribeDoneCode.SUBSCRIPTION_ENDED
            groups = {g for g, _ in objects[name]}
            assert groups == set(range(2, end + 1)), objects[name]
        assert rx._close_err is None


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
async def test_largest_object_and_next_group_start_on_a_flowing_track(
        use_quic):
    async with _flowing(_flow_port(2, use_quic), use_quic) as subscriber:
        first, _ = await subscriber()
        for name in _SIZES:
            await first.subscribe(_FLOW_NS, name, forward=1,
                                  wait_response=True)
        await asyncio.sleep(0.2)
        rx, objects = await subscriber()
        video = await rx.subscribe(_FLOW_NS, "video", forward=1,
                                   filter_type=FilterType.LATEST_OBJECT,
                                   wait_response=True)
        audio = await rx.subscribe(_FLOW_NS, "audio", forward=1,
                                   filter_type=FilterType.NEXT_GROUP_START,
                                   wait_response=True)
        await asyncio.sleep(0.2)
        assert objects["video"] and objects["audio"]
        assert min(objects["video"]) > (video.largest_group_id,
                                        video.largest_object_id)
        assert min(objects["audio"]) == (audio.largest_group_id + 1, 0)
        assert rx._close_err is None


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
async def test_a_range_already_behind_is_refused(use_quic):
    async with _flowing(_flow_port(3, use_quic), use_quic) as subscriber:
        rx, _ = await subscriber()
        with pytest.raises(MOQTRequestError) as err:
            await rx.subscribe(
                _FLOW_NS, "video", forward=1,
                filter_type=FilterType.ABSOLUTE_RANGE, start_group=0,
                start_object=0, end_group=0, wait_response=True)
        assert err.value.error_code == RequestErrorCode.INVALID_RANGE
        assert rx._close_err is None


# -- objects published before anyone subscribed ---------------------------

_AHEAD_NS = "relay/ahead"


async def _publish_ahead(session, name, groups):
    """PUBLISH, then one-object groups ended by END_OF_GROUP, sent at once
    without waiting for PUBLISH_OK (§5.1). Returns the PUBLISH."""
    msg = session.publish(namespace=_AHEAD_NS, track_name=name, forward=1)
    await _send_groups(session, msg.track_alias, name, range(groups))
    return msg


async def _send_groups(session, alias, name, groups):
    from aiomoqt.messages import SubgroupHeader
    for g in groups:
        sid = await session.open_uni_stream()
        hdr = SubgroupHeader(track_alias=alias, group_id=g, subgroup_id=0,
                             publisher_priority=128, extensions_present=True,
                             prof=session._profile)
        session.stream_write(sid, hdr.serialize().data)
        session.stream_write(sid, hdr.next_object(
            payload=f"{name}-{g}".encode(), extensions=None,
            object_id=0).data)
        session.stream_write(sid, hdr.end_group(object_id=1).data,
                             end_stream=True)


def _ahead_track(name):
    return relay._tracks.get(((b"relay", b"ahead"), name.encode()))


@contextlib.asynccontextmanager
async def _ahead(port, use_quic, groups):
    """A relay holding `groups` groups of video and audio, published on one
    session before any subscriber. Yields (publisher, subscriber, msgs)."""
    _reset_relay_state()
    relay._track_subs.clear()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=18)
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            sessions = []
            for _ in range(2):
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=18)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                sessions.append(s)
            pub, sub = sessions
            msgs = {name: await _publish_ahead(pub, name, groups)
                    for name in ("video", "audio")}
            for _ in range(100):
                if all(_ahead_track(n) is not None
                       and _ahead_track(n).largest == (groups - 1, 1)
                       for n in msgs):
                    break
                await asyncio.sleep(0.02)
            yield pub, sub, msgs
    finally:
        handle.close()
        _reset_relay_state()
        relay._track_subs.clear()


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
async def test_an_absolute_start_before_largest_is_served_from_cache(
        use_quic):
    # §5.1.2: the Start MAY be less than the Largest Object; the relay
    # serves what it holds, then what arrives, each object once.
    async with _ahead(_BASE_PORT + 140 + (not use_quic), use_quic, 4) as (
            pub, rx, msgs):
        got, first = [], {}

        def _on(m, *_):
            got.append(bytes(m.payload))
            if m.payload:
                first[bytes(m.payload)] = bool((m.stream_flags or (0,))[0])
        rx.on_object_received = _on
        for name, start in (("video", 2), ("audio", 1)):
            await rx.subscribe(_AHEAD_NS, name, forward=1,
                               filter_type=FilterType.ABSOLUTE_START,
                               start_group=start, start_object=0,
                               wait_response=True)
        await asyncio.sleep(0.3)
        for name in ("video", "audio"):
            await _send_groups(pub, msgs[name].track_alias, name, [4])
        want = sorted([b"video-2", b"video-3", b"video-4",
                       b"audio-1", b"audio-2", b"audio-3", b"audio-4"])
        for _ in range(100):
            if len([p for p in got if p]) >= len(want):
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        assert sorted(p for p in got if p) == want
        # §2.2: each stream opens with its subgroup's first object, replayed
        # or live, so FIRST_OBJECT passes through.
        assert all(first[p] for p in want), first
        assert rx._close_err is None


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
async def test_a_stream_joined_mid_subgroup_does_not_claim_first_object(
        use_quic):
    from aiomoqt.messages import SubgroupHeader
    async with _ahead(_BASE_PORT + 144 + (not use_quic), use_quic, 1) as (
            pub, rx, msgs):
        sids = {}
        for name in ("video", "audio"):
            sid = sids[name] = await pub.open_uni_stream()
            hdr = SubgroupHeader(track_alias=msgs[name].track_alias,
                                 group_id=5, subgroup_id=0,
                                 publisher_priority=128,
                                 extensions_present=True, prof=pub._profile)
            pub.stream_write(sid, hdr.serialize().data)
            pub.stream_write(sid, hdr.next_object(
                payload=f"{name}-5.0".encode(), extensions=None,
                object_id=0).data)
            sids[name] = (sid, hdr)
        for _ in range(100):
            if all(_ahead_track(n).largest == (5, 0) for n in sids):
                break
            await asyncio.sleep(0.02)
        first = {}
        rx.on_object_received = lambda m, *_: first.setdefault(
            bytes(m.payload), bool((m.stream_flags or (0,))[0]))
        for name in sids:
            await rx.subscribe(_AHEAD_NS, name, forward=1,
                               filter_type=FilterType.LATEST_OBJECT,
                               wait_response=True)
        for name, (sid, hdr) in sids.items():
            pub.stream_write(sid, hdr.next_object(
                payload=f"{name}-5.1".encode(), extensions=None,
                object_id=1).data)
        for _ in range(100):
            if len(first) >= 2:
                break
            await asyncio.sleep(0.02)
        assert first == {b"video-5.1": False, b"audio-5.1": False}
        assert rx._close_err is None


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
async def test_a_subscribe_tracks_offer_carries_largest_and_the_cache(
        use_quic):
    # §10.2.11: a PUBLISH for a track with objects carries LARGEST_OBJECT.
    async with _ahead(_BASE_PORT + 142 + (not use_quic), use_quic, 2) as (
            _pub, rx, _msgs):
        got = {"video": [], "audio": []}
        await rx.subscribe_tracks(namespace=_AHEAD_NS)
        for _ in range(2):
            offer = await rx.await_publish(timeout=5.0)
            name = offer.track_name.decode()
            assert (offer.largest_group_id, offer.largest_object_id) == (1, 1)
            rx._track_aliases[offer.track_alias] = offer.request_id
            rx.register_object_handler(
                offer.track_alias,
                lambda m, *_, n=name: got[n].append(bytes(m.payload)))
            rx.publish_ok(offer, forward=1,
                          filter_type=FilterType.ABSOLUTE_START,
                          start_group=0, start_object=0)
        for _ in range(100):
            if all(len([p for p in v if p]) >= 2 for v in got.values()):
                break
            await asyncio.sleep(0.02)
        assert {n: [p for p in v if p] for n, v in got.items()} == {
            "video": [b"video-0", b"video-1"],
            "audio": [b"audio-0", b"audio-1"]}
        assert rx._close_err is None


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
async def test_d14_parked_publishes_are_raised_by_subscribe_update(use_quic):
    # d14 §9.10: the update that raises a parked PUBLISH is SUBSCRIBE_UPDATE,
    # which carries Start, End Group, Priority and Forward as fields.
    async with _flowing(_flow_port(6, use_quic), use_quic,
                        draft=14) as subscriber:
        rx, objects = await subscriber()
        for name in _SIZES:
            await rx.subscribe(_FLOW_NS, name, forward=1, wait_response=True)
        for _ in range(150):
            if all(objects[n] for n in _SIZES):
                break
            await asyncio.sleep(0.02)
        assert all(objects[n] for n in _SIZES), objects
        assert all(_relayed(n).parked is None for n in _SIZES)
        assert rx._close_err is None


# (publisher draft, subscriber draft): the upstream SUBSCRIBE takes the
# publisher's draft.
_FILTER_DRAFTS = [(18, 18), (22, 22), (14, 18)]


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
@pytest.mark.parametrize("pub_draft,sub_draft", _FILTER_DRAFTS,
                         ids=["d18", "d22", "d14-pub"])
async def test_upstream_subscribe_keeps_each_subscribers_start(
        use_quic, pub_draft, sub_draft):
    # §10.2.9: a SUBSCRIBE with no filter is unfiltered. The shared upstream
    # SUBSCRIBE must stay unfiltered (omitted from d16, AbsoluteStart {0, 0}
    # at d14, which has no omission), and must not end where one
    # subscriber's range ends.
    i = _FILTER_DRAFTS.index((pub_draft, sub_draft))
    port = _BASE_PORT + 160 + 2 * i + (not use_quic)
    _reset_relay_state()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic,
                                 draft=sorted({pub_draft, sub_draft},
                                              reverse=True))
    handle = await server.serve()
    upstream = {}
    try:
        pub_client = MOQTClient("localhost", port, path="/",
                                use_quic=use_quic, verify_tls=False,
                                supported_drafts=pub_draft)

        async def on_subscribe(session, msg):
            upstream[bytes(msg.track_name)] = msg
            session.subscribe_ok(request_msg=msg)
        pub_client.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
        async with pub_client.connect() as pub:
            await pub.client_session_init()
            await pub.publish_namespace(namespace="relay/filt", parameters={},
                                        wait_response=True)
            sub_client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=sub_draft)
            async with sub_client.connect() as rx:
                await rx.client_session_init()
                await rx.subscribe("relay/filt", "all", filter_type=None,
                                   wait_response=True)
                await rx.subscribe("relay/filt", "range",
                                   filter_type=FilterType.ABSOLUTE_RANGE,
                                   start_group=2, start_object=1,
                                   end_group=5, wait_response=True)
    finally:
        handle.close()
        _reset_relay_state()
    got = {name: (m.filter_type, m.start_group, m.start_object)
           for name, m in upstream.items()}
    unfiltered = ((FilterType.ABSOLUTE_START, 0, 0) if pub_draft == 14
                  else (None, None, None))
    assert got == {b"all": unfiltered,
                   b"range": (FilterType.ABSOLUTE_START, 2, 1)}, got
    # Omitted, not a Location Filter of type None (0x00).
    assert upstream[b"all"].location_filter is None


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
async def test_d14_request_update_repeats_each_subscriptions_location(
        use_quic):
    # d14 §9.10: Start never decreases, so an update that only pauses a
    # subscription repeats the Start its filter resolved to and its range.
    async with _flowing(_flow_port(8, use_quic), use_quic,
                        draft=14) as subscriber:
        rx, _objects = await subscriber()
        latest = await rx.subscribe(_FLOW_NS, "video", forward=1, priority=7,
                                    wait_response=True)
        ranged = await rx.subscribe(_FLOW_NS, "audio", forward=1,
                                    filter_type=FilterType.ABSOLUTE_RANGE,
                                    start_group=1, start_object=2,
                                    end_group=9, wait_response=True)
        video = rx.request_update(latest.request_id, forward=0)
        audio = rx.request_update(ranged.request_id, forward=0)
        with pytest.raises(ValueError):
            rx.request_update(latest.request_id + 100, forward=0)
        await asyncio.sleep(0.2)
        assert rx._close_err is None
    assert latest.largest_group_id is not None
    assert (video.subscription_request_id, video.start_group,
            video.start_object, video.end_group, video.priority,
            video.forward) == (latest.request_id, latest.largest_group_id,
                               latest.largest_object_id + 1, 0, 7, 0)
    assert (audio.subscription_request_id, audio.start_group,
            audio.start_object, audio.end_group, audio.forward) == (
                ranged.request_id, 1, 2, 10, 0)


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
async def test_a_second_publisher_in_a_namespace_keeps_the_firsts_track(
        use_quic):
    # Two live publishers announce the same namespace, each serving its own
    # track. The second announcement must not close the first's track.
    port = _flow_port(9, use_quic)
    ns = "relay/shared"
    got = {"a": 0, "b": 0}
    _reset_relay_state()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=18)
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def _session():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=18)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s

            def _count(name):
                def on_object(*_args):
                    got[name] += 1
                return on_object

            async def _publisher(name):
                pub = await _session()
                PublishedTrack(pub, namespace=ns, trackname=name,
                               object_size=100, group_size=5,
                               rate=200).attach()
                await pub.publish_namespace(namespace=ns, wait_response=True)

            await _publisher("a")
            rx = await _session()
            await SubscribedTrack(rx, ns, "a",
                                  on_object=_count("a")).subscribe(timeout=5)
            first = relay._tracks[(relay._ns_tuple(ns), b"a")]
            await _publisher("b")
            before = got["a"]
            await SubscribedTrack(rx, ns, "b",
                                  on_object=_count("b")).subscribe(timeout=5)
            for _ in range(150):
                if got["b"] and got["a"] > before + 5:
                    break
                await asyncio.sleep(0.02)
            assert relay._tracks.get((relay._ns_tuple(ns), b"a")) is first
            assert got["a"] > before + 5 and got["b"], got
    finally:
        handle.close()
        _reset_relay_state()


def test_a_closed_track_takes_no_objects():
    track = relay._RelayedTrack(("ns", "t"))
    track.close()
    track.on_object(_Obj(group_id=0, object_id=0), 0, 0, 0, 0)
    assert track.task is None and track.queue.empty()


class _Obj:
    def __init__(self, group_id, object_id):
        self.group_id = group_id
        self.object_id = object_id
        self.payload = b"x" * 8
        self.extensions = None
        self.publisher_priority = 128
        self.status = None
        self.stream_flags = None


@pytest.mark.asyncio
async def test_relay_flags_reach_the_server(monkeypatch):
    monkeypatch.setattr("sys.argv", ["moq_interop_relay"])
    transport = relay.transport_settings(relay.parse_args())
    assert transport == {"congestion_control_algorithm": "cubic",
                         "max_connections": 4096,
                         "stream_ring_cap": 1 << 20,
                         "tx_max_inflight_bytes": 512 << 10}
    server = relay._build_server("localhost", 0, CERT, KEY, True, 18,
                                 **transport)
    assert (server.congestion_control_algorithm, server.max_connections,
            server.stream_ring_cap, server.tx_max_inflight_bytes) == (
                "cubic", 4096, 1 << 20, 512 << 10)
