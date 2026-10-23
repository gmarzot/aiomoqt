"""adaptive_bench's multi-process rate covers the time actually measured,
not a fixed 5 s window, so the first intervals are not under-reported."""
import queue

from aiomoqt.tools import adaptive_bench as ab


def _actuator():
    sc = ab.Scenario(object_size=1000, subgroups=1, start_mbps=10,
                     step_mbps=10, max_mbps=100, interval_s=1, settle_s=1)
    events = queue.Queue()
    act = ab.BWActuator(ab.BenchState(), ab.LiveStats(1000), sc,
                        mp_rate_queue=queue.Queue(), mp_events_queue=events)
    return act, events


async def test_first_second_rate_is_not_diluted(monkeypatch):
    act, events = _actuator()
    clock = [1000.0]
    monkeypatch.setattr(ab.time, "monotonic", lambda: clock[0])
    await act.apply(10.0)
    clock[0] += 1.0
    events.put({"kind": "stats", "t": clock[0], "rx_bytes": 1_250_000})
    sig = await act.observe()
    assert abs(sig.rx_mbps - 10.0) < 0.01, sig.rx_mbps
    assert not sig.shortfall


async def test_rate_window_caps_at_five_seconds(monkeypatch):
    act, events = _actuator()
    clock = [1000.0]
    monkeypatch.setattr(ab.time, "monotonic", lambda: clock[0])
    await act.apply(10.0)
    clock[0] += 30.0
    for i in range(5):
        events.put({"kind": "stats", "t": clock[0] - i, "rx_bytes": 1_250_000})
    sig = await act.observe()
    assert abs(sig.rx_mbps - 10.0) < 0.01, sig.rx_mbps
