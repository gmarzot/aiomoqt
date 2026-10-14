"""The agent runtime over a real session, two tracks on one session over
raw QUIC and WebTransport: namespace announcement, reader completion,
concurrent fetches and per-peer send counts."""
import asyncio
import contextlib
import time

from aiomoqt.agent import (
    AgentSession, FetchSpec, PublishSpec, StartAt, SubscribeSpec, TrackRef,
)
from aiomoqt.client import MOQTClient
from aiomoqt.messages import SubgroupHeader
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, SubscribeDoneCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests.test_fetch_serving import (
    _GROUP, _NS as _FETCH_NS, _SIZES, _loopback as _fetch_loopback,
)
from aiomoqt.tests.test_subscription_filter import _TRANSPORTS

_BASE_PORT = 16440


def _port(case: int, use_quic: bool) -> int:
    return _BASE_PORT + 2 * case + (not use_quic)


async def _until(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


def _name(parts) -> str:
    return "/".join(p.decode() if isinstance(p, bytes) else p for p in parts)


def _payloads(into: list):
    """Object handler keeping the payloads of normal objects."""
    def on_object(msg, *_):
        if not getattr(msg, "status", 0):
            into.append(bytes(msg.payload))
    return on_object


def _endpoints(port, use_quic):
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=18)
    return server, client


@contextlib.asynccontextmanager
async def _publishing(port, use_quic):
    """An AgentSession on the client publishes. The server records the
    namespaces announced and the tracks offered to it, and is the side
    that subscribes; yields (agent, server session future, seen)."""
    peer = asyncio.get_running_loop().create_future()
    seen: dict = {"namespaces": [], "publishes": []}

    def _found(session):
        if not peer.done():
            peer.set_result(session)

    async def _on_publish_namespace(session, msg):
        seen["namespaces"].append(_name(msg.namespace))
        session.publish_namepace_ok(msg)
        _found(session)

    async def _on_publish(session, msg):
        seen["publishes"].append(
            f"{_name(msg.track_namespace)}/{msg.track_name.decode()}")
        _found(session)

    server, client = _endpoints(port, use_quic)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            _on_publish_namespace)
    server.register_handler(MOQTMessageType.PUBLISH, _on_publish)
    quic_server = await server.serve()
    try:
        async with client.connect() as pub:
            await pub.client_session_init()
            async with AgentSession(pub) as agent:
                yield agent, peer, seen
    finally:
        quic_server.close()


@requires_certs
@_TRANSPORTS
async def test_announce_once_per_namespace(use_quic):
    async with _publishing(_port(0, use_quic), use_quic) as (
            agent, peer, seen):
        a = agent.writer(PublishSpec(track=TrackRef("agent", "a")))
        b = agent.writer(PublishSpec(track=TrackRef("agent", "b")))
        c = agent.writer(PublishSpec(track=TrackRef("other", "c"),
                                     announce=False))
        await asyncio.gather(a.start(), b.start(), c.start())
        rx = await asyncio.wait_for(peer, 5.0)
        assert await _until(lambda: seen["publishes"])
        got: dict = {"a": [], "b": []}
        for name, into in got.items():
            await rx.subscribe("agent", name, forward=1,
                               on_object=_payloads(into), wait_response=True)
        for i in range(3):
            await a.write(f"a{i}")
            await b.write(f"b{i}")
        assert await _until(lambda: all(len(v) == 3 for v in got.values()))
        assert got == {"a": [b"a0", b"a1", b"a2"], "b": [b"b0", b"b1", b"b2"]}
        assert seen == {"namespaces": ["agent"], "publishes": ["other/c"]}
        assert rx._close_err is None


# -- a reader ends when PUBLISH_DONE's streams have ended ---------------

async def _send_group(session, alias, group_id, payloads) -> None:
    """One subgroup stream: the payloads, END_OF_GROUP, FIN."""
    sid = await session.open_uni_stream()
    hdr = SubgroupHeader(track_alias=alias, group_id=group_id,
                         subgroup_id=0, publisher_priority=0,
                         prof=session._profile)
    session.stream_write(sid, hdr.serialize().data)
    for payload in payloads:
        session.stream_write(sid, hdr.next_object(payload=payload).data)
    session.stream_write(sid, hdr.end_group(object_id=hdr.next_object_id).data,
                         end_stream=True)


@contextlib.asynccontextmanager
async def _mock_publisher(port, use_quic):
    """The server publishes two tracks under "mock". "a" answers with
    SUBSCRIBE_OK and PUBLISH_DONE(stream_count=1) together, then sends its
    one stream: five objects and END_OF_GROUP. "b" sends a group every
    20 ms until the session ends. Yields the client session."""
    tasks = []

    async def _stream_b(session, alias):
        try:
            for group in range(10_000):
                await _send_group(session, alias, group, [b"b-%d" % group] * 3)
                await asyncio.sleep(0.02)
        except Exception:
            return

    async def _on_subscribe(session, msg):
        ok = session.subscribe_ok(request_msg=msg, content_exists=0)
        if bytes(msg.track_name) == b"b":
            tasks.append(asyncio.ensure_future(
                _stream_b(session, ok.track_alias)))
            return
        session.subscribe_done(msg.request_id,
                               status_code=SubscribeDoneCode.TRACK_ENDED,
                               stream_count=1, reason="done")
        # Data racing ahead of SUBSCRIBE_OK (§10.4.2) is not this test.
        await asyncio.sleep(0.05)
        await _send_group(session, ok.track_alias, 0,
                          [b"a-%d" % i for i in range(5)])

    server, client = _endpoints(port, use_quic)
    server.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    quic_server = await server.serve()
    try:
        async with client.connect() as session:
            await session.client_session_init()
            yield session
    finally:
        for task in tasks:
            task.cancel()
        quic_server.close()


@requires_certs
@_TRANSPORTS
async def test_publish_done_ends_only_its_reader(use_quic):
    async with _mock_publisher(_port(1, use_quic), use_quic) as session:
        # Completion has to come from the Stream Count, not the grace.
        session.PUBLISH_DONE_GRACE_S = 30.0
        agent = AgentSession(session)
        b = await agent.reader(SubscribeSpec(track=TrackRef("mock", "b")))
        a = await agent.reader(SubscribeSpec(track=TrackRef("mock", "a")))
        t0 = time.monotonic()
        got = await a.read(100, timeout=20)
        assert time.monotonic() - t0 < 5
        assert [o.payload for o in got] == [b"a-%d" % i for i in range(5)]
        assert a.done and a.ended == "complete"
        assert a.publish_done["stream_count"] == 1
        got_b = await b.read(6, timeout=5)
        assert len(got_b) == 6
        assert all(o.payload.startswith(b"b-") for o in got_b)
        assert not b.done and b.ended is None
        await agent.close()
        assert session._close_err is None


# -- fetches -----------------------------------------------------------------

@requires_certs
@_TRANSPORTS
async def test_two_fetches_on_one_session(use_quic):
    async with _fetch_loopback(_port(2, use_quic), use_quic, prefill=3) as (
            _tracks, rx, fetched):
        agent = AgentSession(rx)
        video, audio = await asyncio.gather(
            agent.fetch(FetchSpec(track=TrackRef(_FETCH_NS, "video"),
                                  start_at=StartAt.range(0, 1))),
            agent.fetch(FetchSpec(track=TrackRef(_FETCH_NS, "audio"),
                                  start_at=StartAt(mode="range", group=1,
                                                   object=2, end_group=2))))
        got_v = await video.read(100, timeout=5)
        got_a = await audio.read(100, timeout=5)
        assert video.ended == audio.ended == "complete"
        assert [(o.group, o.object_id) for o in got_v] == [
            (g, o) for g in (0, 1) for o in range(_GROUP)]
        assert [(o.group, o.object_id) for o in got_a] == [
            (g, o) for g in (1, 2) for o in range(_GROUP) if (g, o) >= (1, 2)]
        assert {len(o.payload) for o in got_v} == {_SIZES["video"]}
        assert {len(o.payload) for o in got_a} == {_SIZES["audio"]}
        # Per-request routing: the session-wide handler saw nothing.
        assert fetched == {}
        await agent.close()
        assert rx._close_err is None


# -- WriteStats.sent ---------------------------------------------------------

@requires_certs
@_TRANSPORTS
async def test_a_paused_writer_counts_nothing_sent(use_quic):
    async with _publishing(_port(3, use_quic), use_quic) as (
            agent, peer, _seen):
        a = agent.writer(PublishSpec(track=TrackRef("agent", "a"),
                                     group_size=4))
        b = agent.writer(PublishSpec(track=TrackRef("agent", "b"),
                                     group_size=4))
        await asyncio.gather(a.start(), b.start())
        rx = await asyncio.wait_for(peer, 5.0)
        sub_a = await rx.subscribe("agent", "a", forward=1,
                                   wait_response=True)
        await rx.subscribe("agent", "b", forward=1, wait_response=True)
        stop = asyncio.Event()

        async def pump():
            while not stop.is_set():
                await a.write(b"x")
                await b.write(b"y")
                await asyncio.sleep(0.005)

        task = asyncio.ensure_future(pump())
        try:
            assert await _until(
                lambda: a.stats.sent >= 8 and b.stats.sent >= 8)
            await rx.request_update(sub_a.request_id, forward=0,
                                    wait_response=True)
            await asyncio.sleep(0.1)            # writes in flight land
            sent_a, queued_a = a.stats.sent, a.stats.queued
            sent_b = b.stats.sent
            await asyncio.sleep(0.3)
            assert a.stats.sent == sent_a
            assert a.stats.queued > queued_a
            assert b.stats.sent > sent_b
        finally:
            stop.set()
            await task
        assert rx._close_err is None
