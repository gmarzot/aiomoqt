"""Dual-stack loopback: one MOQTServer port serving raw QUIC and
WebTransport MoQT simultaneously (serve_dual over aiopquic
serve_dispatch).

The acceptance case: a raw-QUIC subscriber and a WebTransport
subscriber complete full d18 handshake + subscribe + object delivery
against the SAME UDP port, concurrently.
"""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType
from aiomoqt.messages import SubgroupHeader

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 14620
_N_OBJECTS = 5


async def _on_subscribe(session, msg):
    ok = session.subscribe_ok(request_msg=msg, content_exists=0)
    stream_id = await session.open_uni_stream()
    hdr = SubgroupHeader(track_alias=ok.track_alias, group_id=0,
                         subgroup_id=0, publisher_priority=0)
    session.stream_write(stream_id, hdr.serialize().data)
    for i in range(_N_OBJECTS):
        session.stream_write(
            stream_id, hdr.next_object(payload=f"dual-{i}".encode()).data,
            end_stream=(i == _N_OBJECTS - 1))
    session.transmit()


async def _subscribe_roundtrip(port, use_quic):
    received = []

    def on_obj(msg, size, ts, group_id, subgroup_id):
        received.append((msg.object_id, bytes(msg.payload)))

    client = MOQTClient(
        "localhost", port, path="/", use_quic=use_quic,
        verify_tls=False, supported_drafts=18,
    )
    async with client.connect() as session:
        await session.client_session_init()
        assert session.negotiated_draft == 18
        session.on_object_received = on_obj
        await session.subscribe("dual/ns", "track", wait_response=True)
        for _ in range(100):
            if len(received) >= _N_OBJECTS:
                break
            await asyncio.sleep(0.02)
    assert len(received) == _N_OBJECTS, (
        f"{'quic' if use_quic else 'wt'}: got {len(received)}/{_N_OBJECTS}")
    for i, (object_id, payload) in enumerate(
            sorted(received, key=lambda r: r[0])):
        assert object_id == i and payload == f"dual-{i}".encode()


def _dual_server(port):
    server = MOQTServer(
        host="localhost", port=port, certificate=CERT, private_key=KEY,
        path="/", supported_drafts=18,
    )
    server.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    return server


@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_dual_port_single_transport(use_quic):
    port = _BASE_PORT + (1 if use_quic else 2)
    server = await _dual_server(port).serve_dual()
    try:
        await _subscribe_roundtrip(port, use_quic)
    finally:
        server.close()


@pytest.mark.asyncio
async def test_dual_port_concurrent_quic_and_wt():
    # The acceptance case: both transports subscribing at once, one port.
    port = _BASE_PORT + 3
    server = await _dual_server(port).serve_dual()
    try:
        async with asyncio.timeout(30):
            await asyncio.gather(
                _subscribe_roundtrip(port, True),
                _subscribe_roundtrip(port, False),
            )
    finally:
        server.close()
