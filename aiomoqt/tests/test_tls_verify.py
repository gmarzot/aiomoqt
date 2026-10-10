"""verify_tls and ca_file reach the transport on both transports."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 24620


async def _serve(port, use_quic):
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)
    return await server.serve()


async def _connects(port, use_quic, **kw) -> bool:
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        supported_drafts=18, **kw)
    try:
        async with asyncio.timeout(6):
            async with client.connect() as session:
                return bool(session)
    except (ConnectionError, OSError, TimeoutError):
        return False


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_default_refuses_the_self_signed_test_server(use_quic):
    port = _PORT + (0 if use_quic else 1)
    server = await _serve(port, use_quic)
    try:
        assert not await _connects(port, use_quic)
    finally:
        server.close()


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_verify_tls_false_connects(use_quic):
    port = _PORT + (2 if use_quic else 3)
    server = await _serve(port, use_quic)
    try:
        assert await _connects(port, use_quic, verify_tls=False)
    finally:
        server.close()


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_ca_file_with_the_server_certificate_connects(use_quic):
    port = _PORT + (4 if use_quic else 5)
    server = await _serve(port, use_quic)
    try:
        assert await _connects(port, use_quic, ca_file=CERT)
    finally:
        server.close()
