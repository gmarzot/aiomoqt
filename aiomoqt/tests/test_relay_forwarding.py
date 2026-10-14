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
async def _flowing(port, use_quic):
    """A relay with video and audio PUBLISHed on one session, two groups
    of each already published (Largest {1, 4}). Yields a coroutine that
    opens a subscriber session and returns (session, objects), mapping
    each track name to the (group, object) Locations received."""
    _reset_relay_state()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=18)
    handle = await server.serve()
    by_size = {size: name for name, size in _SIZES.items()}
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def _session():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=18)
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
    """PUBLISH, then one-object groups ended by END_OF_GROUP, sent once
    the relay has taken the PUBLISH and before anyone subscribes.
    Returns the PUBLISH."""
    msg = session.publish(namespace=_AHEAD_NS, track_name=name, forward=1)
    for _ in range(100):
        if _ahead_track(name) is not None and _ahead_track(name).upstream:
            break
        await asyncio.sleep(0.01)
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
        got = []
        rx.on_object_received = lambda m, *_: got.append(bytes(m.payload))
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
