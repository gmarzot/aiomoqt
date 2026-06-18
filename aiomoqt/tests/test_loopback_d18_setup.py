"""Loopback draft-18 SETUP self-test (Phase 3).

draft-18 runs control over a PAIR of unidirectional streams (SETUP type
0x2F00, which is both the control uni-stream type and the SETUP message
type). This proves the raw-QUIC uni-pair handshake end to end:

  * the client opens a write-control uni and sends SETUP on it,
  * the server binds its read-control uni by the 0x2F00 demux, brings up
    its own write-control uni, and replies with SETUP,
  * the client completes setup on receipt of the server's SETUP.

A returning client_session_init() therefore proves the full exchange.
d18 is gated behind AIOMOQT_ENABLE_D18. WebTransport d18 control is
deferred (different surfacing path), so this is raw-QUIC only.
"""
import os

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer


_CERT_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'certs')
CERT = os.path.realpath(os.path.join(_CERT_DIR, 'cert.pem'))
KEY = os.path.realpath(os.path.join(_CERT_DIR, 'key.pem'))

pytestmark = pytest.mark.skipif(
    not os.path.exists(CERT) or not os.path.exists(KEY),
    reason="TLS certs not found in certs/",
)

_BASE_PORT = 14480


async def _start_server(port):
    server = MOQTServer(
        host="localhost", port=port,
        certificate=CERT, private_key=KEY,
        path="/",
        use_quic=True,
        draft_version=18,
    )
    return await server.serve()


@pytest.mark.asyncio
async def test_d18_setup_handshake_quic(monkeypatch):
    """Client + server complete the d18 raw-QUIC handshake over the
    control uni-stream pair."""
    monkeypatch.setenv("AIOMOQT_ENABLE_D18", "1")
    port = _BASE_PORT + 1
    server = await _start_server(port)
    try:
        client = MOQTClient(
            "localhost", port, path="/",
            use_quic=True, verify_tls=False, draft_version=18,
        )
        async with client.connect() as session:
            await session.client_session_init()
            assert session._moqt_session_setup.done()
            assert session._moqt_session_setup.result() is True
            assert session._draft == 18
            # Control ran over a uni pair, not a single bidi stream.
            assert session._control_stream_id is None
            assert session._d18_control_write_sid is not None
            assert session._d18_control_read_sid is not None
    finally:
        server.close()
