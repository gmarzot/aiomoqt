"""The relay's --watchdog: a self-probe, run in a child process, that
relays one object over loopback; an exit after WATCHDOG_MISSES failed
probes in a row; and a faulthandler timer that kills a stalled event
loop."""
import asyncio
import logging
import os
import subprocess
import sys
import textwrap
import time

import pytest

from aiomoqt.tools import moq_interop_relay as relay

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 16620
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


@pytest.fixture(autouse=True)
def _clean_tables():
    relay._tracks.clear()
    relay._announced.clear()
    relay._announce_requests.clear()
    yield
    relay._tracks.clear()
    relay._announced.clear()
    relay._announce_requests.clear()


@requires_certs
@_TRANSPORTS
async def test_a_probe_passes_through_a_live_relay(use_quic):
    port = _BASE_PORT + (not use_quic)
    handle = await relay._build_server("localhost", port, CERT, KEY,
                                       use_quic=use_quic, draft=18).serve()
    try:
        assert await relay._self_probe("127.0.0.1", port, use_quic, 18, 5.0)
        for _ in range(100):
            if not relay._tracks and not relay._announced:
                break
            await asyncio.sleep(0.02)
        assert not relay._tracks and not relay._announced
    finally:
        handle.close()


@requires_certs
@_TRANSPORTS
async def test_a_probe_fails_when_the_relay_drops_objects(use_quic,
                                                          monkeypatch):
    async def _drop(self, *args, **kwargs):
        return None
    monkeypatch.setattr(relay._RelayedTrack, "_forward_one", _drop)
    port = _BASE_PORT + 2 + (not use_quic)
    handle = await relay._build_server("localhost", port, CERT, KEY,
                                       use_quic=use_quic, draft=18).serve()
    try:
        assert not await relay._self_probe("127.0.0.1", port, use_quic, 18,
                                           1.0)
    finally:
        handle.close()


@requires_certs
@_TRANSPORTS
async def test_a_child_process_probe_passes_and_misses(use_quic):
    port = _BASE_PORT + 4 + (not use_quic)
    handle = await relay._build_server("localhost", port, CERT, KEY,
                                       use_quic=use_quic, draft=18).serve()
    try:
        assert await relay._probe_in_child("127.0.0.1", port, use_quic, 18,
                                           5.0)
    finally:
        handle.close()
    assert not await relay._probe_in_child("127.0.0.1", port, use_quic, 18,
                                           1.0)


@requires_certs
@pytest.mark.parametrize("mode", ["quic", "wt", "dual"])
async def test_a_failed_probe_logs_transport_state_and_wakes_readers(
        mode, caplog):
    port = _BASE_PORT + 6 + ["quic", "wt", "dual"].index(mode)
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=mode != "wt", draft=18)
    handle = await (server.serve_dual() if mode == "dual"
                    else server.serve())
    try:
        with caplog.at_level("WARNING"):
            await relay._diagnose_probe_miss([handle])
        lines = [r.getMessage() for r in caplog.records
                 if "relay: watchdog:" in r.getMessage()]
        assert len(lines) == 2, lines
        assert "rx_notify_pending_now" in lines[0]
        assert ("woke readers [True]" if sys.platform.startswith("linux")
                else "woke readers [False]") in lines[1]
        (counters,) = relay.transport_counters([handle])
        assert set(counters) == set(relay.TRANSPORT_COUNTERS)
        assert counters["loop_callbacks"] > 0
        # A spurious wake costs one empty drain; the relay still relays.
        assert await relay._self_probe("127.0.0.1", port, mode != "wt", 18,
                                       5.0)
    finally:
        handle.close()


async def test_the_watchdog_exits_after_consecutive_misses_only(
        monkeypatch):
    results = iter([False, False, True, False, False, False])
    probes = []

    class _Exit(Exception):
        pass

    async def _probe(*args):
        probes.append(args)
        return next(results)

    async def _no_heartbeat(_stall_s):
        return None

    def _exit(code):
        raise _Exit(code)

    monkeypatch.setattr(relay, "_probe_in_child", _probe)
    monkeypatch.setattr(relay, "_heartbeat", _no_heartbeat)
    monkeypatch.setattr(relay, "WATCHDOG_KICK_S", 0)
    monkeypatch.setattr(relay.os, "_exit", _exit)
    monkeypatch.setattr(relay.logging, "shutdown", lambda: None)
    with pytest.raises(_Exit) as exc:
        await relay._watchdog(0.01, "127.0.0.1", 1, True, [14, 16, 18])
    assert exc.value.args == (relay.WATCHDOG_EXIT,)
    assert len(probes) == 6
    assert {args[3] for args in probes} == {18}       # newest draft only


def test_a_stalled_event_loop_is_dumped_and_exited():
    script = textwrap.dedent("""
        import asyncio, time
        from aiomoqt.tools import moq_interop_relay as relay

        async def main():
            asyncio.get_running_loop().create_task(relay._heartbeat(0.5))
            await asyncio.sleep(0.1)
            time.sleep(10)          # the event loop is stuck

        asyncio.run(main())
    """)
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(relay.__file__))))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (root, env.get("PYTHONPATH")) if p)
    start = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", script], env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 1, proc.stderr
    assert time.monotonic() - start < 8
    assert "Timeout" in proc.stderr and "most recent call first" in proc.stderr


@requires_certs
@_TRANSPORTS
async def test_probes_pass_and_the_relay_logs_their_sessions_quietly(
        use_quic):
    from aiomoqt.tests.test_log_contract import _captured
    port = _BASE_PORT + 10 + (not use_quic)
    handle = await relay._build_server("localhost", port, CERT, KEY,
                                       use_quic=use_quic, draft=18).serve()
    try:
        with _captured(logging.INFO) as records:
            assert await relay._self_probe("127.0.0.1", port, use_quic, 18,
                                           5.0, full=False)
            assert await relay._self_probe("127.0.0.1", port, use_quic, 18,
                                           5.0)
    finally:
        handle.close()
    info = records.at(logging.INFO)
    # The probe's own client side logs as usual; the relay's side does not:
    # no relay summaries, and not its parse of the probe's SETUP.
    assert not [m for m in info if m.startswith("relay:")]
    assert not [m for m in info if "parsed: Setup(" in m
                and relay.WATCHDOG_IMPL in m]


class _Stop(Exception):
    pass


async def _no_heartbeat(_stall_s):
    return None


async def test_the_watchdog_relays_an_object_every_nth_probe(monkeypatch):
    fulls = []

    async def _probe(*args):
        fulls.append(args[5])
        if len(fulls) == 2 * relay.WATCHDOG_FULL_EVERY + 1:
            raise _Stop
        return True

    monkeypatch.setattr(relay, "_probe_in_child", _probe)
    monkeypatch.setattr(relay, "_heartbeat", _no_heartbeat)
    with pytest.raises(_Stop):
        await relay._watchdog(0.01, "127.0.0.1", 1, True, 18)
    every = [True] + [False] * (relay.WATCHDOG_FULL_EVERY - 1)
    assert fulls == every * 2 + [True]


async def test_the_watchdog_summarises_its_probes(monkeypatch):
    from aiomoqt.tests.test_log_contract import _captured
    results = iter([True, False])

    async def _probe(*args):
        try:
            return next(results)
        except StopIteration:
            raise _Stop

    async def _no_diagnosis(_handles):
        return None

    monkeypatch.setattr(relay, "_probe_in_child", _probe)
    monkeypatch.setattr(relay, "_heartbeat", _no_heartbeat)
    monkeypatch.setattr(relay, "_diagnose_probe_miss", _no_diagnosis)
    monkeypatch.setattr(relay, "WATCHDOG_REPORT_S", 0.0)
    with _captured(logging.INFO) as records, pytest.raises(_Stop):
        await relay._watchdog(0.01, "127.0.0.1", 1, True, 18)
    assert [m for m in records.at(logging.INFO) if "probes ok" in m] == [
        "relay: watchdog: 1/1 probes ok in the last 0 h",
        "relay: watchdog: 0/1 probes ok in the last 0 h"]


def test_sigusr1_toggles_debug_logging():
    from aiomoqt.tests.test_log_contract import _captured
    from aiomoqt.utils.logger import get_log_level
    with _captured(logging.INFO) as records:
        relay._toggle_debug()
        assert get_log_level() == logging.DEBUG
        relay._toggle_debug()
        assert get_log_level() == logging.INFO
    assert [m for m in records.at(logging.INFO) if "debug logging" in m] == [
        "relay: debug logging on", "relay: debug logging off"]


async def test_the_control_socket_reports_and_toggles_debug():
    import json
    import tempfile
    from aiomoqt.tests.test_log_contract import _captured
    # macOS caps AF_UNIX paths at 104 bytes; pytest's tmp_path is longer.
    tmp = tempfile.TemporaryDirectory(dir="/tmp")
    path = os.path.join(tmp.name, "control.sock")

    async def call(method, target="/debug"):
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(f"{method} {target} HTTP/1.1\r\nHost: relay\r\n\r\n"
                     .encode())
        data = await reader.read()
        writer.close()
        head, _, body = data.partition(b"\r\n\r\n")
        return head.split(b" ")[1].decode(), json.loads(body)

    before = set(relay._background)
    with _captured(logging.INFO):
        await relay._serve_control(path)
        try:
            assert await call("GET") == ("200", {"debug": False})
            assert await call("POST") == ("200", {"debug": True})
            assert await call("GET") == ("200", {"debug": True})
            assert await call("POST") == ("200", {"debug": False})
            assert (await call("GET", "/status"))[0] == "404"
            assert os.stat(path).st_mode & 0o777 == 0o666
        finally:
            for server in set(relay._background) - before:
                server.close()
                relay._background.discard(server)
            tmp.cleanup()
