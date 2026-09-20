"""Relay forwarding: publisher -> relay -> subscriber, three sessions.

The relay used to be control-plane only, so nothing here could be
tested: it acked SUBSCRIBE and no object ever moved. These exercise the
upstream subscription, the object fan-out, and the §2.4 prefix matching
that decides which publisher a SUBSCRIBE reaches.
"""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.tools import moq_interop_relay as relay

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 15310
_FRAMES = [b"o0", b"o1", b"o2"]


class _Pub(PublishedTrack):
    """Emits _FRAMES as one group on a single subgroup, then stops."""

    async def generate(self, session, track_alias):
        from aiomoqt.messages import SubgroupHeader
        sid = await session.open_uni_stream()
        hdr = SubgroupHeader(
            track_alias=track_alias, group_id=0, subgroup_id=0,
            publisher_priority=200, extensions_present=True,
            prof=session._profile)
        session.stream_write(sid, hdr.serialize().data)
        for i, payload in enumerate(_FRAMES):
            buf = hdr.next_object(payload=payload, extensions=None,
                                  object_id=i)
            await session.stream_write_drain(sid, buf.data)
        self._largest = (0, len(_FRAMES) - 1)


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
            track = _Pub(pub_session, pub_ns, "video")
            await track.publish(announce_namespace=True,
                                publish_track=False)
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
    # §2.4: announcing (relay) must serve a SUBSCRIBE for (relay, sub).
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
