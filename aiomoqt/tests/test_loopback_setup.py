"""Loopback SETUP self-tests.

Confirms that the aiomoqt client + server agree on the MoQT session
handshake at each supported draft version, and that optional SETUP-time
parameters (AUTH_TOKEN on PUBLISH_NAMESPACE) round-trip.

Runs publisher + subscriber in a single process via qh3 on localhost.
No relay needed.
"""
import asyncio
import os
import ssl

import pytest

from qh3.quic.configuration import QuicConfiguration
from qh3.asyncio.server import serve
from qh3.h3.connection import H3_ALPN

from aiomoqt.types import (
    MOQTMessageType, ParamType,
    MOQT_VERSION_DRAFT14, MOQT_VERSION_DRAFT16,
)
from aiomoqt.client import MOQTClient
from aiomoqt.protocol import MOQTPeer, MOQTSession


_CERT_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'certs')
CERT = os.path.realpath(os.path.join(_CERT_DIR, 'cert.pem'))
KEY = os.path.realpath(os.path.join(_CERT_DIR, 'key.pem'))

pytestmark = pytest.mark.skipif(
    not os.path.exists(CERT) or not os.path.exists(KEY),
    reason="TLS certs not found in certs/",
)


async def _start_server(port: int, on_publish_namespace=None):
    """Minimal loopback server that accepts SETUP and optionally handles
    PUBLISH_NAMESPACE via the provided callback."""
    peer = MOQTPeer()
    peer.endpoint = "moq"

    if on_publish_namespace is not None:
        peer.register_handler(
            MOQTMessageType.PUBLISH_NAMESPACE, on_publish_namespace)

    config = QuicConfiguration(
        is_client=False,
        alpn_protocols=H3_ALPN,
        verify_mode=ssl.CERT_NONE,
        max_data=2**24,
        max_stream_data=2**24,
        max_datagram_frame_size=64 * 1024,
    )
    config.load_cert_chain(CERT, KEY)

    server = await serve(
        "localhost", port,
        configuration=config,
        create_protocol=lambda *a, **kw:
            MOQTSession(*a, **kw, session=peer),
    )
    return server


_BASE_PORT = 14450


@pytest.mark.asyncio
async def test_setup_draft14():
    """Client with draft_version=14 completes session handshake."""
    port = _BASE_PORT + 1
    server = await _start_server(port)
    try:
        client = MOQTClient(
            "localhost", port, endpoint="moq",
            verify_tls=False, draft_version=MOQT_VERSION_DRAFT14,
        )
        async with client.connect() as session:
            await session.client_session_init()
            assert session._moqt_session_setup.done()
            assert session._moqt_session_setup.result() is True
    finally:
        server.close()


@pytest.mark.asyncio
async def test_setup_draft16():
    """Client with draft_version=16 completes session handshake."""
    port = _BASE_PORT + 2
    server = await _start_server(port)
    try:
        client = MOQTClient(
            "localhost", port, endpoint="moq",
            verify_tls=False, draft_version=MOQT_VERSION_DRAFT16,
        )
        async with client.connect() as session:
            await session.client_session_init()
            assert session._moqt_session_setup.done()
            assert session._moqt_session_setup.result() is True
    finally:
        server.close()


@pytest.mark.asyncio
async def test_setup_auth_token_roundtrip():
    """AUTH_TOKEN parameter on PUBLISH_NAMESPACE reaches the server."""
    port = _BASE_PORT + 3
    received_tokens: list[bytes] = []

    async def _handle_pub_ns(session, msg):
        token = msg.parameters.get(ParamType.AUTH_TOKEN)
        if token is not None:
            received_tokens.append(token)
        session.publish_namepace_ok(msg)

    server = await _start_server(port, on_publish_namespace=_handle_pub_ns)
    try:
        client = MOQTClient("localhost", port, endpoint="moq",
                            verify_tls=False)
        async with client.connect() as session:
            await session.client_session_init()
            await session.publish_namespace(
                namespace="setup-test",
                parameters={ParamType.AUTH_TOKEN: b"tok-xyz"},
                wait_response=True,
            )
            await asyncio.sleep(0.1)
        assert received_tokens == [b"tok-xyz"], \
            f"expected one tok-xyz; got {received_tokens}"
    finally:
        server.close()
