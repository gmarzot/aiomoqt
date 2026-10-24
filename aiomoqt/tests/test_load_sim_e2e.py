"""load_sim as a process against the relay: over WebTransport, and with a
packaged scenario file."""
import re

import pytest

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._procs import free_udp_port, run, spawn, stop, wait_for


@pytest.fixture
def relay(tmp_path):
    port = free_udp_port()
    log = tmp_path / "relay.log"
    proc = spawn("moq_interop_relay", "--bind", "127.0.0.1", "--port", port,
                 "--dual", "--cert", CERT, "--key", KEY, log=log)
    try:
        wait_for(log, "Listening on", proc)
        yield port
    finally:
        stop(proc)


def _total_objects(out):
    m = re.search(r"^\s*Total objects: (\d+)$", out, re.MULTILINE)
    assert m, out
    return int(m.group(1))


@requires_certs
def test_load_sim_over_webtransport(relay):
    rc, out = run("load_sim", f"https://127.0.0.1:{relay}/", "--subs", 2,
                  "-t", 3, "-i", 1, "-k", "--draft", 18, "--pub-ns",
                  timeout=60)
    assert _total_objects(out) > 0, out
    assert "RESULT: OK" in out, out
    assert rc == 0, out


@requires_certs
def test_load_sim_runs_a_packaged_scenario(relay):
    rc, out = run("load_sim", f"moqt://127.0.0.1:{relay}", "-f", "default",
                  "--duration-scale", 0.05, "-t", 8, "-i", 2, "-k",
                  "--draft", 18, timeout=90)
    assert "scenarios/default.json" in out, out
    assert _total_objects(out) > 0, out
    assert rc == 0, out
