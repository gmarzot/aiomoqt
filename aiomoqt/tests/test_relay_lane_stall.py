"""aiomoqt-relay lane stall diagnostics: a lane with work that completes
nothing for LANE_STALL_S logs one WARNING naming what its write awaits, and
one INFO line when it moves again or ends; a slow lane that still moves
logs none."""
import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys

import pytest

from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack

from aiomoqt.tests.test_relay_memory_bounds import _STALLED_SUB
from aiomoqt.tests.test_relay_send_lanes import (_NS, _Fake, _Sub,
                                                 _clear_relay, _obj, _relay,
                                                 _settle, _slow_down)
from aiomoqt.tests._certs import requires_certs

_PORT = 27400


@pytest.fixture
def relay_tables():
    _clear_relay()
    yield
    _clear_relay()


def _lines(caplog, text, level=None):
    return [r.getMessage() for r in caplog.records
            if text in r.getMessage()
            and (level is None or r.levelno == level)]


# -- against fake sessions -------------------------------------------------

@pytest.mark.parametrize("parked", ["header", "object"])
async def test_a_parked_lane_warns_once_then_resumes(parked, monkeypatch,
                                                     caplog):
    monkeypatch.setattr(relay, "LANE_STALL_S", 0.05)
    caplog.set_level(logging.INFO, logger="aiomoqt.relay")
    track = relay._RelayedTrack(((b"ns",), b"t"))
    slow = _Fake(blocked=(parked == "header"))
    d = track.add_downstream(slow, 1, 1)
    try:
        track.on_object(_obj(0, 0), 0, 0, 0, 0)
        await _settle()
        if parked == "object":
            slow.gate.clear()
            track.on_object(_obj(0, 1), 0, 0, 0, 0)
            await _settle()
        assert d.lane.busy and d.lane.parked == parked
        for _ in range(4):
            relay._check_lanes()
            await asyncio.sleep(0.03)
        warned = _lines(caplog, "lane stalled", logging.WARNING)
        assert len(warned) == 1, warned
        assert f"parked on {parked};" in warned[0], warned[0]
        assert "track ns/t" in warned[0] and "budget" in warned[0]
        assert d.lane.stalls == 1 and d.lane.stalled_s(1e12) > 0
        slow.gate.set()
        await _settle()
        relay._check_lanes()
        assert len(_lines(caplog, "lane resumed after", logging.INFO)) == 1
        assert d.lane.stalled_s(1e12) == 0
    finally:
        track.close()


async def test_a_lane_ending_stalled_says_so(monkeypatch, caplog):
    monkeypatch.setattr(relay, "LANE_STALL_S", 0.0)
    caplog.set_level(logging.INFO, logger="aiomoqt.relay")
    track = relay._RelayedTrack(((b"ns",), b"t"))
    d = track.add_downstream(_Fake(blocked=True), 1, 1)
    track.on_object(_obj(0, 0), 0, 0, 0, 0)
    await _settle()
    relay._check_lanes()
    relay._check_lanes()
    assert len(_lines(caplog, "lane stalled", logging.WARNING)) == 1
    track._end(d, 0x2, None, "ended")
    track.close()
    assert len(_lines(caplog, "lane ended stalled after", logging.INFO)) == 1


async def test_an_idle_lane_is_not_stalled(monkeypatch, caplog):
    monkeypatch.setattr(relay, "LANE_STALL_S", 0.0)
    caplog.set_level(logging.INFO, logger="aiomoqt.relay")
    track = relay._RelayedTrack(((b"ns",), b"t"))
    d = track.add_downstream(_Fake(), 1, 1)
    try:
        track.on_object(_obj(0, 0), 0, 0, 0, 0)
        await _settle()
        assert d.lane.idle
        relay._check_lanes()
        relay._check_lanes()
        assert not _lines(caplog, "lane stalled")
    finally:
        track.close()


# -- live ------------------------------------------------------------------

@requires_certs
@pytest.mark.skipif(sys.platform == "win32", reason="SIGSTOP")
async def test_a_stalled_viewer_on_a_large_subgroup_warns_once(
        monkeypatch, caplog, relay_tables):
    monkeypatch.setattr(relay, "LANE_STALL_S", 1.0)
    caplog.set_level(logging.INFO, logger="aiomoqt.relay")
    ns, name = "lanestall/stall", "video"
    port = _PORT
    proc = None
    watch = asyncio.ensure_future(relay._watch_lanes())
    try:
        async with _relay(port, True, 18) as connect:
            pub = await connect()
            # 2 MB groups, each one subgroup: above the 512 KiB budget.
            PublishedTrack(pub, namespace=ns, trackname=name,
                           object_size=50_000, group_size=40,
                           rate=100).attach()
            await pub.publish_namespace(namespace=ns, wait_response=True)
            # Not an asyncio subprocess: on macOS py3.14 the child watcher
            # reaps a SIGSTOP'd child with a blocking waitpid on the loop.
            proc = subprocess.Popen(
                [sys.executable, "-c", _STALLED_SUB, str(port), ns, name],
                stdout=subprocess.PIPE, cwd=os.getcwd())
            line = await asyncio.wait_for(
                asyncio.to_thread(proc.stdout.readline), 10)
            assert line.strip() == b"subscribed", line
            track = relay._tracks[(relay._ns_tuple(ns), name.encode())]
            (d,) = track.downstream
            os.kill(proc.pid, signal.SIGSTOP)
            for _ in range(60):
                await asyncio.sleep(0.1)
                if d.lane is not None and d.lane.stalls:
                    break
            await asyncio.sleep(2.0)
            warned = _lines(caplog, "lane stalled", logging.WARNING)
            print(warned)
            assert len(warned) == 1, warned
            assert "parked on object;" in warned[0], warned[0]
            assert d.lane.stalls == 1
            os.kill(proc.pid, signal.SIGCONT)
            for _ in range(150):
                await asyncio.sleep(0.1)
                if (_lines(caplog, "lane resumed after", logging.INFO)
                        or _lines(caplog, "lane ended stalled", logging.INFO)):
                    break
            ended = (_lines(caplog, "lane resumed after", logging.INFO)
                     + _lines(caplog, "lane ended stalled", logging.INFO))
            print(ended)
            assert len(ended) == 1, ended
            assert len(_lines(caplog, "lane stalled", logging.WARNING)) == 1
    finally:
        watch.cancel()
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(proc.pid, signal.SIGCONT)
            proc.kill()
            await asyncio.to_thread(proc.wait, 10)
            proc.stdout.close()


@requires_certs
async def test_a_slow_reading_viewer_is_not_stalled(monkeypatch, caplog,
                                                    relay_tables):
    monkeypatch.setattr(relay, "LANE_STALL_S", 1.0)
    caplog.set_level(logging.INFO, logger="aiomoqt.relay")
    slow = _Sub()
    watch = asyncio.ensure_future(relay._watch_lanes())
    try:
        async with _relay(_PORT + 1, True, 18) as connect:
            pub = await connect()
            PublishedTrack(pub, namespace=_NS, trackname="video",
                           object_size=40_000, group_size=10,
                           rate=30).attach()
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            viewer = await connect()
            await slow.subscribe(viewer, "video")
            track = relay._tracks[(relay._ns_tuple(_NS), b"video")]
            (d,) = track.downstream
            _slow_down(d.session)
            behind = 0
            for _ in range(40):
                await asyncio.sleep(0.1)
                lane = d.lane
                behind += bool(lane and lane.items)
            assert behind >= 20, behind
            assert slow.count > 0
            assert d.lane.stalls == 0
            assert not _lines(caplog, "lane stalled")
    finally:
        watch.cancel()
