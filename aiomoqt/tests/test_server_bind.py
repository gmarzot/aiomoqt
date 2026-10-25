"""MOQTServer binds the host it is given: one address, the IPv4 wildcard,
or by default every interface on both families."""
import errno
import inspect
import socket

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.tests._certs import CERT, KEY, requires_certs


def _binds_host() -> bool:
    from aiopquic._binding._transport import TransportContext
    try:
        return "host" in inspect.signature(TransportContext.start).parameters
    except (TypeError, ValueError):
        return False


pytestmark = [
    requires_certs,
    pytest.mark.skipif(not _binds_host(),
                       reason="aiopquic without a listener host (< 0.7.0)"),
]


def _have_ipv6() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _taken(family: int, address: str, port: int) -> bool:
    """True when `address:port` is already bound by someone else."""
    with socket.socket(family, socket.SOCK_DGRAM) as s:
        if family == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            s.bind((address, port))
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                return True
            raise
    return False


async def _serve(host, port):
    server = MOQTServer(host=host, port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=True,
                        supported_drafts=18)
    return server, await server.serve()


async def _setup_ok(host: str, port: int) -> None:
    client = MOQTClient(host=host, port=port, path="/", use_quic=True,
                        verify_tls=False, supported_drafts=18)
    async with client.connect() as session:
        await session.client_session_init(timeout=5)


@pytest.mark.asyncio
async def test_an_address_binds_only_that_address():
    port = _free_port()
    _, handle = await _serve("127.0.0.1", port)
    try:
        await _setup_ok("127.0.0.1", port)
        assert _taken(socket.AF_INET, "127.0.0.1", port)
        if _have_ipv6():
            assert not _taken(socket.AF_INET6, "::1", port)
    finally:
        handle.close()


@pytest.mark.asyncio
async def test_ipv4_wildcard_leaves_ipv6_free():
    port = _free_port()
    _, handle = await _serve("0.0.0.0", port)
    try:
        await _setup_ok("127.0.0.1", port)
        assert _taken(socket.AF_INET, "0.0.0.0", port)
        if _have_ipv6():
            assert not _taken(socket.AF_INET6, "::", port)
    finally:
        handle.close()


@pytest.mark.asyncio
async def test_no_host_binds_every_interface():
    port = _free_port()
    _, handle = await _serve(None, port)
    try:
        await _setup_ok("127.0.0.1", port)
        assert _taken(socket.AF_INET, "0.0.0.0", port)
        if _have_ipv6():
            assert _taken(socket.AF_INET6, "::", port)
    finally:
        handle.close()


@pytest.mark.asyncio
async def test_a_busy_address_fails_at_serve():
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    try:
        with pytest.raises(OSError):
            await _serve("127.0.0.1", port)
    finally:
        holder.close()
