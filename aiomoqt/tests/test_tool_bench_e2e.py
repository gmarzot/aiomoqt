"""pub_server and sub_bench as processes: each listener mode, both
transports, d18 and d22."""
import re
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
    rows = re.findall(r"^\s+\d+-(\d+)s\s", out, re.MULTILINE)
    assert rows and rows[-1] == "2", out


@requires_certs
@pytest.mark.parametrize("listen,dial", [("-Q", "quic"), ("-W", "wt")],
                         ids=["quic", "wt"])
def test_sub_bench_receives_a_video_profile_from_pub_server(tmp_path, listen,
                                                            dial):
    port = free_udp_port()
    log = tmp_path / "pub_server.log"
    proc = spawn("pub_server", "-p", port, "--video", "240p", "-r", 60,
                 listen, log=log)
    try:
        wait_for(log, "publisher server ready", proc)
        assert "video 240p" in log.read_text()
        rc, out = run("sub_bench", _url(dial, port), "-k", "-t", 2, "-i", 1,
                      "--verify-payload")
    finally:
        stop(proc)
    assert objects_received(out) > 0, out
    assert _payload_bad(out) == 0, out
    assert rc == 0, out


def _payload_bad(out):
    """The summary's bad-payload count; fails if the line is missing."""
    m = re.search(r"^\s*Payload:\s+[\d,]+ checked.*?, (\d+) bad$", out,
                  re.MULTILINE)
    assert m, out
    return int(m.group(1))


@requires_certs
@pytest.mark.parametrize("transport", ["-Q", "-W"])
def test_loopback_bench_payloads_check_clean(transport):
    rc, out = run("loopback_bench", transport, "-p", free_udp_port(),
                  "--draft", 18, "-t", 2, "-s", 1200, "-P", 2, "-k",
                  "--verify-payload")
    assert objects_received(out) > 0, out
    assert _payload_bad(out) == 0, out
    assert rc == 0, out


@requires_certs
@pytest.mark.parametrize("scheme", ["moqt", "https"])
def test_payloads_check_clean_through_the_relay(tmp_path, scheme):
    port = free_udp_port()
    url = (f"moqt://127.0.0.1:{port}" if scheme == "moqt"
           else f"https://127.0.0.1:{port}/")
    log = tmp_path / "relay.log"
    relay = spawn("moq_interop_relay", "--bind", "127.0.0.1", "--port", port,
                  "--dual", "--cert", CERT, "--key", KEY, log=log)
    pubs = []
    try:
        wait_for(log, "Listening on", relay)
        for name, shape in (("plain", ("-s", 900, "-r", 200, "-P", 2)),
                            ("video", ("--video", "240p", "-r", 60))):
            plog = tmp_path / f"pub-{name}.log"
            pubs.append(spawn("pub_bench", url, "-k", "-T", name, "-t", 30,
                              *shape, log=plog))
            wait_for(plog, "Published", pubs[-1])
        results = [run("sub_bench", url, "-k", "-T", name, "-t", 2,
                       "--verify-payload", timeout=30)
                   for name in ("plain", "video")]
    finally:
        for p in pubs:
            stop(p)
        stop(relay)
    for rc, out in results:
        assert objects_received(out) > 0, out
        assert _payload_bad(out) == 0, out
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


@requires_certs
@pytest.mark.parametrize("props", [(), ("--per-peer-props",)],
                         ids=["generator", "per-peer"])
@pytest.mark.parametrize("transport", ["-Q", "-W"])
def test_loopback_bench_fanout_delivers_through_the_lane(transport, props):
    rc, out = run("loopback_bench", transport, "--fanout", *props, "-p",
                  free_udp_port(), "--draft", 18, "-t", 2, "-s", 1200, "-k",
                  "--verify-payload")
    assert rc == 0, out
    assert "fanout lane" in out, out
    assert objects_received(out) > 0, out
    assert re.search(r"^\s*Lost:\s+0 ", out, re.MULTILINE), out
    assert _payload_bad(out) == 0, out


def test_loopback_bench_fanout_refuses_parallel_subgroups(monkeypatch):
    import sys
    from aiomoqt.tools import loopback_bench
    monkeypatch.setattr(sys, "argv", ["loopback_bench", "--fanout", "-P", "2"])
    with pytest.raises(SystemExit):
        loopback_bench.parse_args()


@requires_certs
def test_loopback_bench_reports_through_its_last_second():
    # 2 s intervals: a row printed late still leaves the closing row
    # its half-interval (1 s) on a loaded CPU.
    rc, out = run("loopback_bench", "-Q", "-p", free_udp_port(), "--draft", 18,
                  "-t", 4, "-i", 2, "-s", 1200, "-k")
    assert rc == 0, out
    rows = re.findall(r"^\s+(\d+)-(\d+)s\s", out, re.MULTILINE)
    assert len(rows) == 2 and rows[-1][1] == "4", out


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
