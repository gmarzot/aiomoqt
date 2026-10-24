"""pub_server and sub_bench as processes: each listener mode, both
transports, d18 and d22."""
import time

import pytest

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._procs import (free_udp_port, objects_received, run, spawn,
                                  stop, wait_for)


@pytest.fixture
def pub_server(tmp_path):
    procs = []

    def start(*listen):
        port = free_udp_port()
        log = tmp_path / f"pub_server-{port}.log"
        proc = spawn("pub_server", "-p", port, "-s", 512, "-r", 200,
                     *listen, log=log)
        procs.append(proc)
        wait_for(log, "publisher server ready", proc)
        return port

    yield start
    for p in procs:
        stop(p)


def _url(scheme, port):
    return (f"moqt://localhost:{port}" if scheme == "quic"
            else f"https://localhost:{port}/")


@requires_certs
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("listen,dial", [
    (("-Q",), "quic"), (("-W",), "wt"),
    (("-Q", "-W"), "quic"), (("-Q", "-W"), "wt"),
], ids=["Q", "W", "QW-quic", "QW-wt"])
def test_sub_bench_receives_from_pub_server(pub_server, listen, dial, draft):
    port = pub_server(*listen)
    rc, out = run("sub_bench", _url(dial, port), "-k", "-t", 2, "-i", 1,
                  "--draft", draft)
    assert objects_received(out) > 0, out
    assert rc == 0, out


@requires_certs
@pytest.mark.parametrize("dial", ["quic", "wt"])
def test_sub_bench_fails_on_a_dead_port_within_its_duration(dial):
    t0 = time.monotonic()
    rc, out = run("sub_bench", _url(dial, free_udp_port()), "-k", "-t", 2,
                  timeout=30)
    assert rc != 0, out
    assert "Connection failed" in out, out
    assert "Traceback" not in out, out
    assert time.monotonic() - t0 < 12, out


@requires_certs
@pytest.mark.parametrize("dial", ["quic", "wt"])
def test_pub_bench_fails_cleanly_on_a_dead_port(dial):
    rc, out = run("pub_bench", _url(dial, free_udp_port()), "-k", "-t", 2,
                  timeout=60)
    assert rc != 0, out
    assert "Connection failed" in out, out
    assert "Traceback" not in out, out


@requires_certs
def test_sub_bench_with_no_objects_fails(tmp_path):
    port = free_udp_port()
    log = tmp_path / "relay.log"
    relay = spawn("moq_interop_relay", "--bind", "127.0.0.1", "--port", port,
                  "--quic", "--cert", CERT, "--key", KEY, log=log)
    try:
        wait_for(log, "Listening on", relay)
        rc, out = run("sub_bench", f"moqt://127.0.0.1:{port}", "-k",
                      "-T", "nobody", "-t", 2, timeout=30)
    finally:
        stop(relay)
    assert "No data received" in out, out
    assert rc != 0, out


@requires_certs
@pytest.mark.parametrize("transport", ["-Q", "-W"])
def test_loopback_bench_verifies_its_own_cert(transport):
    rc, out = run("loopback_bench", transport, "-p", free_udp_port(),
                  "--draft", 18, "-t", 2, "-s", 1200, "--cert", CERT,
                  "--key", KEY)
    assert rc == 0, out
    assert objects_received(out) > 0


def test_loopback_bench_without_objects_exits_1(monkeypatch):
    import asyncio
    import sys
    from aiomoqt.tools import loopback_bench

    class _Server:
        def close(self):
            pass

    async def _serve(args):
        return _Server()

    async def _nothing(args, stats):
        pass
    monkeypatch.setattr(loopback_bench, "run_server", _serve)
    monkeypatch.setattr(loopback_bench, "run_subscriber", _nothing)
    monkeypatch.setattr(sys, "argv", ["loopback_bench", "-t", "1"])
    assert asyncio.run(loopback_bench.main()) == 1
