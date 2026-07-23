"""Loopback d18: control-plane fragmentation over real raw QUIC.

A real client deliberately splits its SETUP across multiple QUIC packets
(separate StreamDataReceived events server-side) — the wire shape that
crashed the relay in the official interop run (moq-dev-js,
BufferReadError at pull_uint16). The cut=1 case splits INSIDE the 2-byte
vi64 stream/message type itself, which additionally exercises control-uni
*classification*: the server must not misroute a control stream whose
first event doesn't yet hold a decodable SETUP type.

Complements tests/test_control_fragmentation.py (unit, synthetic buffers)
with the full stack: QUIC handshake, real events, real server dispatch.
"""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, SetupParamType
from aiomoqt.messages.d18.session_setup import Setup

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 14520


async def _start_server(port, on_subscribe=None):
    server = MOQTServer(
        host="localhost", port=port,
        certificate=CERT, private_key=KEY,
        path="/",
        use_quic=True,
        supported_drafts=18,
    )
    if on_subscribe is not None:
        server.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
    return await server.serve()


async def _fragmented_setup(session, cut):
    """Open the client's control write-uni and send SETUP split at
    byte `cut`, flushing + pausing between fragments so they land as
    separate StreamDataReceived events on the server."""
    sid = await session.open_uni_stream()
    session._d18_control_write_sid = sid
    wire = bytes(Setup(options={
        SetupParamType.IMPLEMENTATION: b"frag-test"}).serialize(
            prof=session._profile).data)
    assert len(wire) > cut
    for frag in (wire[:cut], wire[cut:]):
        session.stream_write(sid, frag)
        session.transmit()
        await asyncio.sleep(0.08)


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", [1, 3])
async def test_fragmented_setup_establishes(cut):
    """Server reassembles a SETUP split across packets — including
    mid-type (cut=1) — and completes the d18 handshake."""
    port = _BASE_PORT + cut
    server = await _start_server(port)
    try:
        client = MOQTClient(
            "localhost", port, path="/",
            use_quic=True, verify_tls=False, supported_drafts=18,
        )
        async with client.connect() as session:
            await _fragmented_setup(session, cut)
            async with asyncio.timeout(5):
                assert await session._moqt_session_setup is True
            assert session.negotiated_draft == 18
    finally:
        server.close()


@pytest.mark.asyncio
async def test_fragmented_setup_then_subscribe_roundtrip():
    """After a mid-type-fragmented SETUP, the session is fully
    functional: a SUBSCRIBE round-trips on its request bidi stream."""
    port = _BASE_PORT + 9
    got_subscribe = asyncio.Event()

    async def _on_subscribe(session, msg):
        session.subscribe_ok(request_msg=msg, content_exists=0)
        got_subscribe.set()

    server = await _start_server(port, on_subscribe=_on_subscribe)
    try:
        client = MOQTClient(
            "localhost", port, path="/",
            use_quic=True, verify_tls=False, supported_drafts=18,
        )
        async with client.connect() as session:
            await _fragmented_setup(session, cut=1)
            async with asyncio.timeout(5):
                assert await session._moqt_session_setup is True
            async with asyncio.timeout(5):
                ok = await session.subscribe(
                    "frag/ns", "track", wait_response=True)
            assert ok is not None
            assert got_subscribe.is_set()
    finally:
        server.close()
