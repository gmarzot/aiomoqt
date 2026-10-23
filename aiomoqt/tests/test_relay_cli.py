"""moq_interop_relay run as a process with its operator flags."""
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._procs import free_udp_port, run


@requires_certs
def test_unusable_control_path_is_a_clean_error(tmp_path):
    rc, out = run("moq_interop_relay", "--bind", "127.0.0.1", "--port",
                  free_udp_port(), "--quic", "--cert", CERT, "--key", KEY,
                  "--control", tmp_path / "missing" / "ctl.sock", timeout=30)
    assert rc != 0, out
    assert "error: --control" in out, out
    assert "Traceback" not in out, out
