"""flow_control_window sets max_data and max_stream_data in the QUIC
configuration of the server (serve(), serve_dual()) and the client, raw QUIC
and WebTransport."""
import pytest

from aiomoqt import server as server_mod
from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 29740
WINDOW = 64 << 20


@pytest.mark.parametrize("window,expected", [(WINDOW, WINDOW), (None, 1 << 24)])
@pytest.mark.parametrize("mode", ["quic", "wt", "dual"])
async def test_server_configuration_takes_the_window(monkeypatch, mode, window,
                                                     expected):
    seen = []

    def capture(*args, configuration=None, **kwargs):
        seen.append(configuration)

    for name in ("aiopquic_serve", "serve_webtransport", "serve_dispatch"):
        monkeypatch.setattr(server_mod, name, capture)
    monkeypatch.setattr(server_mod, "_check_udp_port_free", lambda *a: None)
    server = MOQTServer(host="localhost", port=1, certificate=CERT,
                        private_key=KEY, use_quic=mode == "quic",
                        flow_control_window=window)
    server.serve_dual() if mode == "dual" else server.serve()
    assert [(c.max_data, c.max_stream_data) for c in seen] == [
        (expected, expected)]


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_client_configuration_takes_the_window(use_quic):
    port = _BASE_PORT + (not use_quic)
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        flow_control_window=WINDOW)
    handle = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, flow_control_window=WINDOW)
        async with client.connect() as session:
            await session.client_session_init()
            cfg = client.effective_configuration
            assert (cfg.max_data, cfg.max_stream_data) == (WINDOW, WINDOW)
    finally:
        handle.close()
