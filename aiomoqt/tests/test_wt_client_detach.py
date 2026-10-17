"""A WebTransport client connection gives back its transport's resources."""
import gc
import os

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 24640
_CYCLES = 10


def _open_fds() -> int:
    for path in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(path):
            return len(os.listdir(path))
    pytest.skip("no per-process fd listing")


async def _connect_once() -> None:
    client = MOQTClient("localhost", _PORT, path="/", use_quic=False,
                        supported_drafts=18, verify_tls=False)
    async with client.connect() as session:
        assert session


async def test_wt_client_connections_do_not_hold_fds():
    server = await MOQTServer(host="localhost", port=_PORT, certificate=CERT,
                              private_key=KEY, path="/", use_quic=False,
                              supported_drafts=18).serve()
    try:
        await _connect_once()  # first-use allocations
        gc.collect()
        before = _open_fds()
        for _ in range(_CYCLES):
            await _connect_once()
        gc.collect()
        assert _open_fds() == before
    finally:
        server.close()
