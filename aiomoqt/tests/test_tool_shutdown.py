"""SIGINT and SIGTERM on long-running tools: a clean exit, no traceback or
unretrieved task exception."""
import os
import signal
import subprocess
import time

import pytest

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._procs import (ENV, REPO, free_udp_port, spawn, stop,
                                  tool_cmd, wait_for)

CLEAN_RC = (0, 130)


def _interrupt(proc, log, sig=signal.SIGINT, timeout=15):
    proc.send_signal(sig)
    try:
        rc = proc.wait(timeout=timeout)
    finally:
        stop(proc)
    out = log.read_text(errors="replace")
    assert "Traceback" not in out, out
    assert "never retrieved" not in out, out
    return rc, out


@requires_certs
@pytest.mark.parametrize("marker", ["latency threshold:", "warming"],
                         ids=["start", "mid-run"])
def test_adaptive_bench_sigint(tmp_path, marker):
    log = tmp_path / "adaptive.log"
    proc = spawn("adaptive_bench", "--interval", 1, "-t", 30, log=log)
    wait_for(log, marker, proc)
    rc, out = _interrupt(proc, log)
    assert rc in CLEAN_RC, out


@pytest.mark.parametrize("entry,nargs", [
    ("sub_worker_entry", 1), ("sub_batch_worker_entry", 1),
    ("pub_worker_entry", 2), ("loopback_server_entry", 2)])
def test_workers_leave_sigint_to_the_parent(monkeypatch, entry, nargs):
    # The parent stops workers by event; a worker ignores Ctrl-C.
    from aiomoqt.utils import workers

    seen = []

    def run(coro):
        coro.close()
        seen.append(signal.getsignal(signal.SIGINT))
    monkeypatch.setattr(workers, "_setup_quiet_logging", lambda *a: None)
    monkeypatch.setattr(workers.asyncio, "run", run)
    old = signal.getsignal(signal.SIGINT)
    try:
        getattr(workers, entry)({"sub_id": 0, "worker_id": 0}, None,
                                *([None] * nargs))
    finally:
        signal.signal(signal.SIGINT, old)
    assert seen == [signal.SIG_IGN]


@requires_certs
def test_adaptive_bench_mp_sigint_process_group(tmp_path):
    # A terminal Ctrl-C reaches every worker process, not only the parent.
    log = tmp_path / "adaptive.log"
    with open(log, "w") as f:
        proc = subprocess.Popen(
            tool_cmd("adaptive_bench", "--mp", "--interval", 1, "-t", 30,
                     "--loopback-port", free_udp_port()),
            stdout=f, stderr=subprocess.STDOUT, cwd=REPO, env=ENV,
            start_new_session=True)
    try:
        wait_for(log, "warming", proc)
        time.sleep(1)
        os.killpg(proc.pid, signal.SIGINT)
        rc = proc.wait(timeout=20)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stop(proc)
    out = log.read_text(errors="replace")
    assert "Traceback" not in out, out
    assert "never retrieved" not in out, out
    assert rc in CLEAN_RC, out


def _closed_within(peers, seconds):
    """Every peer process exits within `seconds`: it saw the close."""
    deadline = time.monotonic() + seconds
    for p in peers:
        try:
            p.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return False
    return True


@requires_certs
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM],
                         ids=["INT", "TERM"])
@pytest.mark.parametrize("listen,url", [
    ("-Q", "moqt://localhost:{}"), ("-W", "https://localhost:{}/")],
    ids=["quic", "wt"])
def test_pub_server_closes_its_sessions(tmp_path, listen, url, sig):
    port = free_udp_port()
    slog, blog = tmp_path / "server.log", tmp_path / "sub.log"
    server = spawn("pub_server", "-p", port, "-r", 100, "-s", 256, listen,
                   log=slog)
    sub = None
    try:
        wait_for(slog, "publisher server ready", server)
        sub = spawn("sub_bench", url.format(port), "-k", "-t", 60, log=blog)
        wait_for(blog, "receiving", sub)
        rc, _ = _interrupt(server, slog, sig)
        assert rc in CLEAN_RC
        assert _closed_within([sub], 5), blog.read_text()
    finally:
        if sub is not None:
            stop(sub)


@requires_certs
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM],
                         ids=["INT", "TERM"])
@pytest.mark.parametrize("transport", ["quic", "wt"])
def test_relay_closes_its_sessions(tmp_path, transport, sig):
    port = free_udp_port()
    rlog = tmp_path / "relay.log"
    url = (f"moqt://127.0.0.1:{port}" if transport == "quic"
           else f"https://127.0.0.1:{port}/")
    relay = spawn("moq_interop_relay", "--bind", "127.0.0.1", "--port", port,
                  "--dual", "--cert", CERT, "--key", KEY, log=rlog)
    peers = []
    try:
        wait_for(rlog, "Listening on", relay)
        plog, blog = tmp_path / "pub.log", tmp_path / "sub.log"
        peers.append(spawn("pub_bench", url, "-k", "-t", 60, "-r", 100,
                           "-s", 256, "-T", "t", log=plog))
        wait_for(plog, "waiting for subscriber", peers[0])
        peers.append(spawn("sub_bench", url, "-k", "-t", 60, "-T", "t",
                           log=blog))
        wait_for(blog, "receiving", peers[1])
        rc, _ = _interrupt(relay, rlog, sig)
        assert rc in CLEAN_RC
        assert _closed_within(peers, 5), (plog.read_text()
                                           + blog.read_text())
    finally:
        for p in peers:
            stop(p)


@requires_certs
def test_relay_probe_sigint(tmp_path):
    log = tmp_path / "probe.log"
    proc = spawn("relay_probe", "--url", f"moqt://127.0.0.1:{free_udp_port()}",
                 "--timeout", 20, "--draft", 18, "-d", log=log)
    wait_for(log, "session connect", proc)
    rc, out = _interrupt(proc, log)
    assert rc in CLEAN_RC, out


@requires_certs
def test_interop_client_sigint(tmp_path):
    port = free_udp_port()
    rlog, clog = tmp_path / "relay.log", tmp_path / "client.log"
    relay = spawn("moq_interop_relay", "--bind", "127.0.0.1", "--port", port,
                  "--quic", "--cert", CERT, "--key", KEY, log=rlog)
    try:
        wait_for(rlog, "Listening on", relay)
        proc = spawn("moq_interop_client", "-r", f"moqt://127.0.0.1:{port}",
                     "--tls-disable-verify", "-v", log=clog)
        wait_for(clog, "[PASS]", proc)
        rc, out = _interrupt(proc, clog)
        assert rc in CLEAN_RC, out
    finally:
        stop(relay)


@pytest.fixture(scope="module")
def relay_with_publishers(tmp_path_factory):
    """The relay (both transports on one port) with a bench track
    aiomoqt/t and a media broadcast demo/live."""
    d = tmp_path_factory.mktemp("relay")
    port = free_udp_port()
    url = f"moqt://127.0.0.1:{port}"
    procs = [spawn("moq_interop_relay", "--bind", "127.0.0.1", "--port", port,
                   "--dual", "--cert", CERT, "--key", KEY,
                   log=d / "relay.log")]
    try:
        wait_for(d / "relay.log", "Listening on", procs[0])
        procs.append(spawn("pub_bench", url, "-k", "-t", 300, "-T", "t",
                           "-r", 50, "-s", 256, log=d / "pub.log"))
        wait_for(d / "pub.log", "waiting for subscriber", procs[1])
        procs.append(spawn("pub_media", url, "-k", "-t", 300,
                           "-N", "demo/live", log=d / "media.log"))
        wait_for(d / "media.log", "publishing", procs[2])
        yield url
    finally:
        for p in reversed(procs):
            stop(p)


# tool, argv (URL substituted), start marker, mid-run marker
LONG_RUNNING = [
    ("pub_bench", ["{url}", "-k", "-t", 60, "-T", "p2", "-r", 50],
     "Connecting", "waiting for subscriber"),
    ("sub_bench", ["{url}", "-k", "-t", 60, "-T", "t", "-i", 1],
     "Connecting", "receiving"),
    ("loopback_bench", ["-p", "{port}", "-t", 60, "-i", 1, "-r", 100,
                        "-s", 512], "Starting server", "0-1s"),
    ("load_sim", ["{url}", "--subs", 2, "-t", 60, "-i", 1, "-k",
                  "--draft", 18, "--pub-ns", "-N", "ls"], "scenario:",
     "subs=2"),
    ("pub_media", ["{url}", "-k", "-t", 60, "-N", "demo/other"], "relay:",
     "publishing"),
    ("sub_media", ["{url}", "-k", "-t", 60, "-N", "demo/live", "--analyze",
                   "-i", 1], "relay:", "0-1s"),
    ("moqtest_origin", ["--bind", "127.0.0.1", "--port", "{port}", "--quic",
                        "--cert", CERT, "--key", KEY], "moqtest-origin:",
     "moqtest-origin:"),
]


@requires_certs
@pytest.mark.parametrize("when", ["start", "mid-run"])
@pytest.mark.parametrize("tool,argv,start,mid", LONG_RUNNING,
                         ids=[c[0] for c in LONG_RUNNING])
def test_sigint_exits_cleanly(relay_with_publishers, tmp_path, tool, argv,
                              start, mid, when):
    port = free_udp_port()
    args = [str(a).format(url=relay_with_publishers, port=port) for a in argv]
    log = tmp_path / f"{tool}.log"
    proc = spawn(tool, *args, log=log)
    wait_for(log, start if when == "start" else mid, proc)
    rc, out = _interrupt(proc, log)
    # Stopped before any object, a tool may report its zero-object failure.
    zero = when == "start" and "No data received" in out
    ok = CLEAN_RC + ((1,) if zero else ())
    assert rc in ok, out
