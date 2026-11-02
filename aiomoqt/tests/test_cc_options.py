"""congestion_control_options reaches picoquic's connection on both ends, raw
QUIC and WebTransport, from serve() and serve_dual()."""
import asyncio

import pytest
from aiopquic._binding import _transport

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = [requires_certs, pytest.mark.skipif(
    not hasattr(_transport, "_cc_options"),
    reason="aiopquic without congestion_control_options")]

_BASE_PORT = 29720
OPTS = "Q0.02"


def _options(session, use_quic):
    if use_quic:
        return _transport._cc_options(session._quic._cnx_ptr)
    return _transport._wt_cc_options(session._state)


@pytest.mark.parametrize("dual", [False, True], ids=["serve", "dual"])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_options_reach_both_ends(use_quic, dual):
    port = _BASE_PORT + 2 * dual + (not use_quic)
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        congestion_control_options=OPTS)
    handle = await (server.serve_dual() if dual else server.serve())
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, congestion_control_options=OPTS)
        async with client.connect() as session:
            await session.client_session_init()
            for _ in range(100):
                if server._live:
                    break
                await asyncio.sleep(0.01)
            assert _options(session, use_quic) == OPTS
            assert _options(next(iter(server._live)), use_quic) == OPTS
    finally:
        handle.close()
