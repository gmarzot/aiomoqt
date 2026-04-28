"""Loopback SETUP self-tests.

Confirms that the aiomoqt client + server agree on the MoQT session
handshake at each supported draft version, and that optional SETUP-time
parameters (AUTH_TOKEN on PUBLISH_NAMESPACE) round-trip.

Runs publisher + subscriber in a single process via aiopquic on localhost.
Parameterized over use_quic=True (raw QUIC) and use_quic=False (WT).
"""
import asyncio
import os

import pytest

from aiomoqt.types import (
    MOQTMessageType, ParamType,
    MOQT_VERSION_DRAFT14, MOQT_VERSION_DRAFT16,
)
from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer


_CERT_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'certs')
CERT = os.path.realpath(os.path.join(_CERT_DIR, 'cert.pem'))
KEY = os.path.realpath(os.path.join(_CERT_DIR, 'key.pem'))

pytestmark = pytest.mark.skipif(
    not os.path.exists(CERT) or not os.path.exists(KEY),
    reason="TLS certs not found in certs/",
)


async def _start_server(port: int, draft_version, use_quic,
                         on_publish_namespace=None):
    server = MOQTServer(
        host="localhost", port=port,
        certificate=CERT, private_key=KEY,
        path="moq",
        use_quic=use_quic,
        draft_version=draft_version,
    )
    if on_publish_namespace is not None:
        server.register_handler(
            MOQTMessageType.PUBLISH_NAMESPACE, on_publish_namespace)
    return await server.serve()


_BASE_PORT = 14450


@pytest.fixture(params=[True, False], ids=["use_quic", "wt"])
def use_quic(request):
    return request.param


@pytest.mark.asyncio
async def test_setup_draft14(use_quic):
    """Client + server complete the d14 handshake on both transports."""
    port = _BASE_PORT + (1 if use_quic else 4)
    server = await _start_server(
        port, draft_version=MOQT_VERSION_DRAFT14, use_quic=use_quic)
    try:
        client = MOQTClient(
            "localhost", port, path="moq",
            use_quic=use_quic,
            verify_tls=False, draft_version=MOQT_VERSION_DRAFT14,
        )
        async with client.connect() as session:
            await session.client_session_init()
            assert session._moqt_session_setup.done()
            assert session._moqt_session_setup.result() is True
    finally:
        server.close()


@pytest.mark.asyncio
async def test_setup_draft16(use_quic):
    """Client + server complete the d16 handshake on both transports."""
    port = _BASE_PORT + (2 if use_quic else 5)
    server = await _start_server(
        port, draft_version=MOQT_VERSION_DRAFT16, use_quic=use_quic)
    try:
        client = MOQTClient(
            "localhost", port, path="moq",
            use_quic=use_quic,
            verify_tls=False, draft_version=MOQT_VERSION_DRAFT16,
        )
        async with client.connect() as session:
            await session.client_session_init()
            assert session._moqt_session_setup.done()
            assert session._moqt_session_setup.result() is True
    finally:
        server.close()


@pytest.mark.asyncio
async def test_setup_auth_token_roundtrip(use_quic):
    """AUTH_TOKEN parameter on PUBLISH_NAMESPACE reaches the server."""
    port = _BASE_PORT + (3 if use_quic else 6)
    received_tokens: list[bytes] = []

    async def _handle_pub_ns(session, msg):
        token = msg.parameters.get(ParamType.AUTH_TOKEN)
        if token is not None:
            received_tokens.append(token)
        session.publish_namepace_ok(msg)

    server = await _start_server(
        port, draft_version=MOQT_VERSION_DRAFT16, use_quic=use_quic,
        on_publish_namespace=_handle_pub_ns)
    try:
        client = MOQTClient(
            "localhost", port, path="moq",
            use_quic=use_quic,
            verify_tls=False, draft_version=MOQT_VERSION_DRAFT16,
        )
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
