"""moq_interop_relay run as a process with its operator flags."""
import json
import pathlib
import socket
import tempfile
import time

import pytest

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._procs import (free_udp_port, objects_received, run, spawn,
                                  stop, wait_for)


@requires_certs
def test_unusable_control_path_is_a_clean_error(tmp_path):
    rc, out = run("moq_interop_relay", "--bind", "127.0.0.1", "--port",
                  free_udp_port(), "--quic", "--cert", CERT, "--key", KEY,
                  "--control", tmp_path / "missing" / "ctl.sock", timeout=30)
    assert rc != 0, out
    assert "error: --control" in out, out
    assert "Traceback" not in out, out


def _control(path, method):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(5)
        s.connect(str(path))
        s.sendall(f"{method} /debug HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        data = b""
        while chunk := s.recv(4096):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200"), data
    return json.loads(body)


def _status(path, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            time.sleep(0.2)
    raise AssertionError(f"no {path}")


@pytest.fixture
def short_dir():
    # macOS caps AF_UNIX paths near 104 bytes; pytest's tmp_path is longer.
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        yield pathlib.Path(d)


@requires_certs
def test_relay_operator_flags(tmp_path, short_dir):
    port = free_udp_port()
    ctl, status_dir, log_dir = (short_dir / "ctl.sock", tmp_path / "status",
                                tmp_path / "logs")
    log = tmp_path / "relay.out"
    relay = spawn("moq_interop_relay", "--bind", "127.0.0.1", "--port", port,
                  "--dual", "--cert", CERT, "--key", KEY,
                  "--status-dir", status_dir, "--control", ctl,
                  "--log-dir", log_dir, log=log)
    pub = None
    try:
        wait_for(log, "Listening on", relay)
        assert _control(ctl, "GET") == {"debug": False}
        assert _control(ctl, "POST") == {"debug": True}
        assert _control(ctl, "GET") == {"debug": True}
        assert _control(ctl, "POST") == {"debug": False}

        # Publish over raw QUIC, subscribe over WebTransport: one port.
        plog = tmp_path / "pub.log"
        pub = spawn("pub_bench", f"moqt://127.0.0.1:{port}", "-k", "-t", 60,
                    "-T", "t", "-r", 100, "-s", 256, log=plog)
        wait_for(plog, "waiting for subscriber", pub)
        rc, out = run("sub_bench", f"https://127.0.0.1:{port}/", "-k",
                      "-T", "t", "-t", 2, "-i", 1, timeout=30)
        assert objects_received(out) > 0, out
        assert rc == 0, out

        relay_info = _status(status_dir / "status.json")["relay"]
        assert relay_info["port"] == port
        assert sorted(relay_info["transports"]) == ["quic", "wt"]
        assert (log_dir / "relay.log").stat().st_size > 0
    finally:
        if pub is not None:
            stop(pub)
        stop(relay)
