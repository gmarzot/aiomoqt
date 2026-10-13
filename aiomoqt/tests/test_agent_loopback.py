"""The agent runtime over a real session, two tracks on one session over
raw QUIC and WebTransport: reader completion."""
import asyncio
import contextlib
import time

from aiomoqt.agent import AgentSession, SubscribeSpec, TrackRef
from aiomoqt.client import MOQTClient
from aiomoqt.messages import SubgroupHeader
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, SubscribeDoneCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests.test_subscription_filter import _TRANSPORTS

_BASE_PORT = 16440


def _port(case: int, use_quic: bool) -> int:
    return _BASE_PORT + 2 * case + (not use_quic)


def _endpoints(port, use_quic):
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=18)
    return server, client


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
