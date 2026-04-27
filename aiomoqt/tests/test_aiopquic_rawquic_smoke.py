"""Phase D-partial smoke — aiomoqt over aiopquic raw-QUIC end-to-end.

Validates that MOQTSessionQuic (aiopquic-backed) can stand up a session,
exchange CLIENT_SETUP/SERVER_SETUP, and ship one subgroup of objects.
Both server and client use_quic=True; transport is aiopquic.

If this passes, the StreamChain + StreamChunk + SPSC bridge is wired up
correctly and the whole MoQT framing path runs over aiopquic.
"""
import asyncio
import os
import time

import pytest

from aiomoqt.types import (
    MOQTMessageType, MOQT_TIMESTAMP_EXT,
    MOQT_VERSION_DRAFT16,
)
from aiomoqt.messages import Subscribe
from aiomoqt.messages.track import SubgroupHeader
from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer

_CERT_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'certs')
CERT = os.path.realpath(os.path.join(_CERT_DIR, 'cert.pem'))
KEY = os.path.realpath(os.path.join(_CERT_DIR, 'key.pem'))

pytestmark = pytest.mark.skipif(
    not os.path.exists(CERT) or not os.path.exists(KEY),
    reason="TLS certs not found in certs/",
)

_BASE_PORT = 14534


def _make_subscribe_handler(num_objects: int, object_size: int):
    async def _handle_subscribe(session, msg: Subscribe):
        ok = session.subscribe_ok(
            request_msg=msg, content_exists=0,
        )
        track_alias = ok.track_alias
        group_id = 0
        stream_id = session.open_uni_stream()
        header = SubgroupHeader(
            track_alias=track_alias,
            group_id=group_id,
            subgroup_id=0,
            publisher_priority=128,
            extensions_present=True,
        )
        session.stream_write(stream_id, header.serialize().data)
        for obj_id in range(num_objects):
            ext = {MOQT_TIMESTAMP_EXT: int(time.time() * 1000)}
            buf = header.next_object(
                payload=f"obj-{obj_id}".encode().ljust(object_size, b'\x00'),
                extensions=ext,
                object_id=obj_id,
            )
            session.stream_write(stream_id, buf.data)
        session.stream_write(stream_id, b'', end_stream=True)
    return _handle_subscribe


@pytest.mark.asyncio
async def test_aiopquic_rawquic_handshake():
    """Step 1 smoke: handshake + CLIENT_SETUP/SERVER_SETUP exchange only."""
    port = _BASE_PORT + 1
    server = MOQTServer(
        host="localhost", port=port,
        certificate=CERT, private_key=KEY,
        path="moq",
        use_quic=True,
        draft_version=MOQT_VERSION_DRAFT16,
    )
    server_handle = await server.serve()
    try:
        client = MOQTClient(
            host="localhost", port=port,
            path="moq",
            use_quic=True,
            verify_tls=False,
            draft_version=MOQT_VERSION_DRAFT16,
        )
        async with client.connect() as session:
            await session.client_session_init(timeout=5)
            assert session._moqt_session_setup.done(), \
                "MOQT session setup did not complete"
    finally:
        server_handle.close()


@pytest.mark.asyncio
async def test_aiopquic_rawquic_pubsub_one_subgroup():
    """Step 2 correctness: pub one subgroup of N objects, sub receives all."""
    port = _BASE_PORT + 2
    num_objects = 16
    object_size = 64

    server = MOQTServer(
        host="localhost", port=port,
        certificate=CERT, private_key=KEY,
        path="moq",
        use_quic=True,
        draft_version=MOQT_VERSION_DRAFT16,
    )
    server.register_handler(
        MOQTMessageType.SUBSCRIBE,
        _make_subscribe_handler(num_objects, object_size),
    )
    server_handle = await server.serve()

    received = []

    def on_object(msg, size, recv_ms, group_id, subgroup_id):
        received.append((group_id, msg.object_id, bytes(msg.payload)))

    try:
        client = MOQTClient(
            host="localhost", port=port,
            path="moq",
            use_quic=True,
            verify_tls=False,
            draft_version=MOQT_VERSION_DRAFT16,
        )
        async with client.connect() as session:
            await session.client_session_init(timeout=5)
            session.on_object_received = on_object

            await session.subscribe(
                namespace="bench",
                track_name="track",
                wait_response=True,
            )
            await asyncio.sleep(1.0)

        assert len(received) == num_objects, \
            f"Expected {num_objects} objects, got {len(received)}"
        for i, (g, oid, payload) in enumerate(received):
            assert g == 0, f"Object {i}: expected group 0, got {g}"
            assert oid == i, f"Object {i}: expected object_id {i}, got {oid}"
            expected = f"obj-{i}".encode().ljust(object_size, b'\x00')
            assert payload == expected, \
                f"Object {i}: payload mismatch"

    finally:
        server_handle.close()
