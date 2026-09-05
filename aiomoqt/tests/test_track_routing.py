"""Per-track object routing: register_object_handler(track_alias, cb)
routes a track's objects to its own callback; tracks without a
registration fall back to the session-global on_object_received.
Multi-track sessions (catalog + audio + video) depend on this isolation.
"""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType
from aiomoqt.messages import SubgroupHeader
from aiomoqt.messages.data import ObjectDatagram

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 14760
_N_OBJECTS = 3


async def _on_subscribe(session, msg):
    session._next_track_alias = max(session._next_track_alias, 1000)
    ok = session.subscribe_ok(request_msg=msg, content_exists=0)
    await asyncio.sleep(0.05)  # let the OK land before data (§10.4.2)
    name = bytes(msg.track_name)
    stream_id = await session.open_uni_stream()
    hdr = SubgroupHeader(track_alias=ok.track_alias, group_id=0,
                         subgroup_id=0, publisher_priority=0,
                         prof=session._profile)
    session.stream_write(stream_id, hdr.serialize().data)
    for i in range(_N_OBJECTS):
        session.stream_write(
            stream_id, hdr.next_object(payload=name + b"-%d" % i).data,
            end_stream=(i == _N_OBJECTS - 1))


async def _on_subscribe_datagrams(session, msg):
    session._next_track_alias = max(session._next_track_alias, 1000)
    ok = session.subscribe_ok(request_msg=msg, content_exists=0)
    await asyncio.sleep(0.05)
    name = bytes(msg.track_name)
    for i in range(_N_OBJECTS):
        dgram = ObjectDatagram(track_alias=ok.track_alias, group_id=0,
                               object_id=i, payload=name + b"-%d" % i)
        session.send_dgram_message(dgram.serialize(prof=session._profile))


def _server(port, handler=_on_subscribe):
    server = MOQTServer(
        host="localhost", port=port, certificate=CERT, private_key=KEY,
        path="/", use_quic=True, supported_drafts=18,
    )
    server.register_handler(MOQTMessageType.SUBSCRIBE, handler)
    return server


def _sink(bucket):
    return lambda msg, size, ts, gid, sgid: bucket.append(
        bytes(msg.payload))


async def _wait(buckets, total):
    for _ in range(200):
        if sum(len(b) for b in buckets) >= total:
            return
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [_on_subscribe, _on_subscribe_datagrams],
                         ids=["streams", "datagrams"])
async def test_two_tracks_route_independently(handler):
    port = _BASE_PORT + (1 if handler is _on_subscribe else 2)
    server = await _server(port, handler).serve()
    got_a, got_b = [], []
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            ok_a = await session.subscribe("ns", "ta", wait_response=True)
            session.register_object_handler(ok_a.track_alias, _sink(got_a))
            ok_b = await session.subscribe("ns", "tb", wait_response=True)
            session.register_object_handler(ok_b.track_alias, _sink(got_b))
            assert ok_a.track_alias != ok_b.track_alias
            await _wait((got_a, got_b), 2 * _N_OBJECTS)
    finally:
        server.close()
    assert sorted(got_a) == [b"ta-%d" % i for i in range(_N_OBJECTS)]
    assert sorted(got_b) == [b"tb-%d" % i for i in range(_N_OBJECTS)]


@pytest.mark.asyncio
async def test_unrouted_track_falls_back_to_global():
    port = _BASE_PORT + 3
    server = await _server(port).serve()
    routed, fallback = [], []
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            session.on_object_received = _sink(fallback)
            ok_a = await session.subscribe("ns", "ta", wait_response=True)
            session.register_object_handler(ok_a.track_alias, _sink(routed))
            await session.subscribe("ns", "tb", wait_response=True)
            await _wait((routed, fallback), 2 * _N_OBJECTS)
    finally:
        server.close()
    # Registered track never leaks to the global; unregistered track
    # (and only it) lands there.
    assert sorted(routed) == [b"ta-%d" % i for i in range(_N_OBJECTS)]
    assert sorted(fallback) == [b"tb-%d" % i for i in range(_N_OBJECTS)]
