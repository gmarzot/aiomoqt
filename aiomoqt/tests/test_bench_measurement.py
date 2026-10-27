"""Bench and load tools measure what they report: tail latency, per-slot
delivery, bounded and complete loss accounting."""
import queue
import time
from collections import deque
from types import SimpleNamespace

import pytest

from aiomoqt.tools import adaptive_bench as ab
from aiomoqt.utils import workers
from aiomoqt.utils.stats import TrackStats


def _obj(gid, oid, latency_ms=None):
    now_us = int(time.time() * 1e6)
    ext = ({0x20: now_us - int(latency_ms * 1000)}
           if latency_ms is not None else None)
    return SimpleNamespace(group_id=gid, object_id=oid, subgroup_id=0,
                           extensions=ext), now_us


def _feed(stats, gid, oid, latency_ms=None):
    msg, recv_us = _obj(gid, oid, latency_ms)
    stats.on_object(msg, 1000, recv_us)


def _bw_actuator():
    events = queue.Queue()
    act = ab.BWActuator(ab.BenchState(), ab.LiveStats(1000),
                        ab.Scenario(1000, 1, 1, 1, 100, 1, 1),
                        mp_rate_queue=queue.Queue(), mp_events_queue=events)
    return act, events


# -- bw ramp latency ---------------------------------------------------

async def test_bw_controls_on_the_subscribers_p90():
    stats = TrackStats()
    for oid in range(100):
        _feed(stats, 0, oid, 1 if oid < 80 else 100)
    snap = dict(stats.snapshot(), kind='stats')
    act, events = _bw_actuator()
    events.put(snap)
    sig = await act.observe()
    assert snap['lat_n'] == 100
    assert sig.latency_p90_ms == pytest.approx(100, abs=1)
    assert sig.latency_known


async def test_bw_empty_window_is_not_zero_latency():
    snap = dict(TrackStats().snapshot(), kind='stats', rx_bytes=1000)
    assert snap['lat_n'] == 0
    act, events = _bw_actuator()
    events.put(snap)
    sig = await act.observe()
    assert not sig.latency_known


def test_snapshot_reports_its_interval(monkeypatch):
    from aiomoqt.utils import stats as st
    clock = [100.0]
    monkeypatch.setattr(st.time, 'monotonic', lambda: clock[0])
    s = TrackStats()
    clock[0] = 102.5
    assert s.snapshot()['dt'] == pytest.approx(2.5)
    clock[0] = 103.0
    assert s.snapshot()['dt'] == pytest.approx(0.5)


# -- subscriber ramp delivery -------------------------------------------

def test_slot_delivering_floor():
    rate = 1_250_000            # 10 Mbps in bytes/s
    now = 100.0
    at_rate = deque((now - i, (10 - i) * rate) for i in range(5, -1, -1))
    trickle = deque((now - i, (10 - i) * 1000) for i in range(5, -1, -1))
    assert workers.slot_delivering(at_rate, 50.0, now, 4.5, rate)
    assert not workers.slot_delivering(trickle, 50.0, now, 4.5, rate)
    # Inside its grace after a resubscribe: not judged delivering yet.
    assert not workers.slot_delivering(at_rate, now - 1, now, 4.5, rate)
    # Bytes from before the resubscribe don't count.
    stalled = deque([(94.0, 0), (95.0, 6 * rate), (96.0, 6 * rate),
                     (100.0, 6 * rate)])
    assert not workers.slot_delivering(stalled, 95.5, now, 10.0, rate)


def _subs_actuator(monkeypatch):
    monkeypatch.setattr(workers, 'MP_CTX', SimpleNamespace(
        Queue=lambda **kw: queue.Queue()))
    a = ab.SubsActuator(
        relay_url='moqt://127.0.0.1:4433', namespace='ns', trackname='t',
        draft=[22], insecure=True, force_quic=False, min_subs=1,
        max_subs=100, start_subs=10, step_subs=10,
        object_size=1000, per_sub_mbps=10)
    a._workers = {0: (SimpleNamespace(is_alive=lambda: True), None)}
    a._spawned_t[0] = time.monotonic() - 60
    return a


async def _observe(a, n, **batch):
    for _ in range(n):
        a._events_q.put(dict(dict(kind='batch_stats', worker_id=0,
                                  total_objs=1, loss=0, lat_n=10,
                                  lat_p90_ms=1, lat_mean_ms=1, dt=1.0),
                             **batch))
        sig = await a.observe()
    return sig


async def test_trickling_slots_are_not_capacity(monkeypatch):
    # The review's case: ten subscribed slots, 1000 B/s between them.
    a = _subs_actuator(monkeypatch)
    sig = await _observe(a, 4, active=10, rx_bytes=1000)
    assert sig.shortfall and sig.health == 'degraded'


async def test_one_slot_trickling_is_a_shortfall(monkeypatch):
    a = _subs_actuator(monkeypatch)
    sig = await _observe(a, 2, active=10, delivering=9,
                         rx_bytes=9 * 1_250_000)
    assert sig.delivering_subs == 9 and sig.active_subs == 10
    assert sig.shortfall and sig.health == 'degraded'


async def test_all_slots_at_rate_are_ok(monkeypatch):
    a = _subs_actuator(monkeypatch)
    sig = await _observe(a, 4, active=10, delivering=10,
                         rx_bytes=10 * 1_250_000)
    assert not sig.shortfall and sig.health == 'ok'
    assert sig.rx_mbps == pytest.approx(100)


async def test_subs_rate_divides_by_the_batch_interval(monkeypatch):
    a = _subs_actuator(monkeypatch)
    sig = await _observe(a, 1, active=10, delivering=10,
                         rx_bytes=20 * 1_250_000, dt=2.0)
    assert sig.rx_mbps == pytest.approx(100)
