"""--idle-timeout: the QUIC idle timeout aiomoqt-relay offers reaches
every listener's configuration; unset, aiopquic's default stands."""
import pytest
from aiopquic.quic.configuration import QuicConfiguration

from aiomoqt import server as server_mod
from aiomoqt.server import MOQTServer
from aiomoqt.tools import moq_interop_relay as relay

from aiomoqt.tests._certs import CERT, KEY, requires_certs


def test_the_flag_and_the_file_set_it(tmp_path):
    args = relay.parse_args(["--idle-timeout", "60"])
    assert relay.transport_settings(args)["idle_timeout"] == 60.0
    path = tmp_path / "relay.toml"
    path.write_text("idle-timeout = 45\n")
    assert relay.parse_args(["--config", str(path)]).idle_timeout == 45.0
    assert relay.transport_settings(relay.parse_args([]))["idle_timeout"] \
        is None


@requires_certs
@pytest.mark.parametrize("mode", ["quic", "wt", "dual"])
@pytest.mark.parametrize("idle", [60.0, None])
async def test_every_listener_offers_it(mode, idle, monkeypatch):
    seen = []

    def capture(*args, configuration=None, **kwargs):
        seen.append(configuration)
    for name in ("aiopquic_serve", "serve_webtransport", "serve_dispatch"):
        monkeypatch.setattr(server_mod, name, capture)
    monkeypatch.setattr(server_mod, "_check_udp_port_free",
                        lambda host, port: None)
    server = MOQTServer("localhost", 0, CERT, KEY, use_quic=mode == "quic",
                        idle_timeout=idle)
    (server.serve_dual if mode == "dual" else server.serve)()
    assert len(seen) == 1
    want = QuicConfiguration().idle_timeout if idle is None else idle
    assert seen[0].idle_timeout == want
