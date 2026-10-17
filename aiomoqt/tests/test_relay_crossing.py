"""The relay's upstream SUBSCRIBE and a publisher's PUBLISH of the same
track (d18 §5.1): concurrent subscribers share one upstream SUBSCRIBE, a
crossing PUBLISH takes over from it, and neither side overwrites the
other. Two tracks on one session, over raw QUIC and WebTransport."""
import asyncio
import contextlib
import logging

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import SubgroupHeader
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import SubscribedTrack
from aiomoqt.types import (MOQTMessageType, MOQTRequestError, ParamType,
                           RequestErrorCode)

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 18100
_NS = "cross/live"
_TRACKS = ("a", "b")
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _port(case: int, use_quic: bool) -> int:
    return _BASE_PORT + 2 * case + (not use_quic)


def _clear_relay():
    for table in (relay._announced, relay._announce_requests, relay._tracks,
                  relay._establishing):
        table.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()


@pytest.fixture(autouse=True)
def _clean_relay():
    _clear_relay()
    yield
    _clear_relay()


@contextlib.asynccontextmanager
async def _relay(port, use_quic):
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=[18])
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect(handlers=()):
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=18)
                for msg_type, handler in handlers:
                    client.register_handler(msg_type, handler)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s
            yield connect
    finally:
        handle.close()


def _forward(reply) -> int:
    forward = getattr(reply, "forward", None)
    if forward is None:
        forward = (getattr(reply, "parameters", None) or {}).get(
            ParamType.FORWARD, 1)
    return int(forward)


async def _send_object(session, alias, payload):
    sid = await session.open_uni_stream()
    hdr = SubgroupHeader(track_alias=alias, group_id=0, subgroup_id=0,
                         publisher_priority=128, extensions_present=False,
                         prof=session._profile)
    session.stream_write(sid, hdr.serialize().data)
    await session.stream_write_drain(
        sid, hdr.next_object(payload=payload, extensions=None,
                             object_id=0).data)


async def _subscribe_all(sub, got):
    async def one(name):
        track = SubscribedTrack(
            sub, _NS, name,
            on_object=lambda obj, *a, n=name: got.setdefault(n, []).append(
                bytes(obj.payload)))
        track._quiet = True
        await track.subscribe(timeout=8.0)
    await asyncio.gather(*(one(n) for n in _TRACKS))


async def _until(cond, timeout=4.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


@_TRANSPORTS
async def test_concurrent_subscribers_share_one_upstream_subscribe(use_quic):
    seen = []

    async def on_subscribe(session, msg):
        seen.append(msg.track_name.decode())
        await asyncio.sleep(0.3)
        session.subscribe_ok(request_msg=msg, content_exists=0)

    async with _relay(_port(0, use_quic), use_quic) as connect:
        pub = await connect([(MOQTMessageType.SUBSCRIBE, on_subscribe)])
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        subs = [await connect() for _ in range(3)]
        await asyncio.gather(*(_subscribe_all(s, {}) for s in subs))
    assert sorted(seen) == sorted(_TRACKS)


def _crossing_publisher(sent, cancelled):
    """Answers the relay's SUBSCRIBE with a PUBLISH of the same track.
    Track a: PUBLISH, then wait for the SUBSCRIBE to be cancelled.
    Track b: refuse it DUPLICATE_SUBSCRIPTION first, then PUBLISH."""
    async def on_subscribe(session, msg):
        name = msg.track_name.decode()
        session.register_request_cancel_handler(
            msg.request_id, lambda rid, n=name: cancelled.add(n))
        if name == "b":
            session.subscribe_error(
                msg.request_id,
                error_code=RequestErrorCode.DUPLICATE_SUBSCRIPTION,
                reason="our PUBLISH holds it")
            await asyncio.sleep(0.2)
        message = session.publish(namespace=_NS, track_name=name)
        fut = session._loop.create_future()
        session._pending_requests[message.request_id] = fut
        reply = await session._await_response(message.request_id)
        sent[name] = _forward(reply)
        await _send_object(session, message.track_alias, name.encode())
    return on_subscribe


@_TRANSPORTS
async def test_a_crossing_publish_takes_over_the_upstream_subscribe(
        use_quic, caplog):
    sent, cancelled, got = {}, set(), {}
    async with _relay(_port(1, use_quic), use_quic) as connect:
        pub = await connect([(MOQTMessageType.SUBSCRIBE,
                              _crossing_publisher(sent, cancelled))])
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        sub = await connect()
        await _subscribe_all(sub, got)
        await _until(lambda: len(got) == len(_TRACKS)
                     and "a" in cancelled)
    assert sent == {"a": 1, "b": 1}
    assert "a" in cancelled
    assert got == {name: [name.encode()] for name in _TRACKS}
    assert not [r for r in caplog.records
                if r.levelno >= logging.ERROR and "relay" in r.getMessage()]


@_TRANSPORTS
async def test_a_publish_beside_an_established_subscription_is_refused(
        use_quic):
    refused = {}

    async def on_subscribe(session, msg):
        session.subscribe_ok(request_msg=msg, content_exists=0)

    async with _relay(_port(2, use_quic), use_quic) as connect:
        pub = await connect([(MOQTMessageType.SUBSCRIBE, on_subscribe)])
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        sub = await connect()
        await _subscribe_all(sub, {})
        for name in _TRACKS:
            with pytest.raises(MOQTRequestError) as err:
                await asyncio.wait_for(pub.publish(
                    namespace=_NS, track_name=name, wait_response=True), 4.0)
            refused[name] = err.value.error_code
    assert refused == {name: int(RequestErrorCode.DUPLICATE_SUBSCRIPTION)
                       for name in _TRACKS}


@_TRANSPORTS
async def test_an_upstream_subscription_never_overwrites_a_publish(use_quic):
    # The announcing publisher answers late; another session PUBLISHes
    # each track meanwhile and keeps it.
    got = {}
    released = asyncio.Event()
    held = []

    async def slow_ok(session, msg):
        held.append(msg.track_name)
        await released.wait()
        session.subscribe_ok(request_msg=msg, content_exists=0)

    async with _relay(_port(3, use_quic), use_quic) as connect:
        origin = await connect([(MOQTMessageType.SUBSCRIBE, slow_ok)])
        await origin.publish_namespace(namespace=_NS, wait_response=True)
        other = await connect()
        sub = await connect()
        subscribing = asyncio.create_task(_subscribe_all(sub, got))
        assert await _until(lambda: len(held) == len(_TRACKS))

        async def publish(name):
            message = other.publish(namespace=_NS, track_name=name)
            fut = other._loop.create_future()
            other._pending_requests[message.request_id] = fut
            await other._await_response(message.request_id)
            return message

        published = await asyncio.gather(*(publish(n) for n in _TRACKS))
        released.set()
        await asyncio.wait_for(subscribing, 8.0)
        for message in published:
            name = message.track_name.decode()
            await _send_object(other, message.track_alias, name.encode())
        await _until(lambda: len(got) == len(_TRACKS))
        for name in _TRACKS:
            track = relay._tracks[(relay._ns_tuple(_NS), name.encode())]
            # The PUBLISH's session, not our SubscribedTrack.
            assert relay._is_session(track.upstream), name
    assert got == {name: [name.encode()] for name in _TRACKS}


@_TRANSPORTS
async def test_no_reply_to_a_subscribe_cancelled_while_held(use_quic, caplog):
    refused = asyncio.Event()

    async def slow_refusal(session, msg):
        await asyncio.sleep(0.4)
        session.subscribe_error(msg.request_id,
                                error_code=RequestErrorCode.DOES_NOT_EXIST,
                                reason="gone")
        refused.set()

    async with _relay(_port(4, use_quic), use_quic) as connect:
        pub = await connect([(MOQTMessageType.SUBSCRIBE, slow_refusal)])
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        sub = await connect()
        for name in _TRACKS:
            message = sub.subscribe(_NS, name)
            await asyncio.sleep(0.1)
            sub.unsubscribe(message.request_id)
        await asyncio.wait_for(refused.wait(), 4.0)
        await asyncio.sleep(0.3)
        assert not sub._close_err and not pub._close_err
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
