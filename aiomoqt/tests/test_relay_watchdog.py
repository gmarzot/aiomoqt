"""The relay's --watchdog: a self-probe, run in a child process, that
relays one object over loopback; an exit after WATCHDOG_MISSES failed
probes in a row; and a faulthandler timer that kills a stalled event
loop."""
import asyncio
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
