"""packet_train_mode reaches picoquic on both ends, raw QUIC and
WebTransport, from serve() and serve_dual()."""
import asyncio

import pytest
from aiopquic._binding import _transport

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = [requires_certs, pytest.mark.skipif(
    not hasattr(_transport, "_packet_train_mode"),
    reason="aiopquic without packet_train_mode")]

_BASE_PORT = 29730


def _mode(session, use_quic):
    if use_quic:
        return _transport._packet_train_mode(session._quic._cnx_ptr)
    return _transport._wt_packet_train_mode(session._state)


@pytest.mark.parametrize("dual", [False, True], ids=["serve", "dual"])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_mode_reaches_both_ends(use_quic, dual):
    port = _BASE_PORT + 2 * dual + (not use_quic)
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        packet_train_mode=True)
    handle = await (server.serve_dual() if dual else server.serve())
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, packet_train_mode=True)
        async with client.connect() as session:
            await session.client_session_init()
            for _ in range(100):
                if server._live:
                    break
                await asyncio.sleep(0.01)
            assert _mode(session, use_quic) == 1
            assert _mode(next(iter(server._live)), use_quic) == 1
    finally:
        handle.close()


async def test_off_by_default():
    server = MOQTServer(host="localhost", port=_BASE_PORT + 4, certificate=CERT,
                        private_key=KEY, path="/", use_quic=True)
    handle = await server.serve()
    try:
        client = MOQTClient("localhost", _BASE_PORT + 4, path="/", use_quic=True,
                            verify_tls=False)
        async with client.connect() as session:
            await session.client_session_init()
            assert _mode(session, True) == 0
    finally:
        handle.close()
