"""adaptive_bench's multi-process rate divides by the time measured so far,
capped at the window, and missing latency prints as n/a. The controller holds
at max until -t."""
import queue
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

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



async def test_late_report_is_prorated_to_the_window(monkeypatch):
    act, events = _actuator()
    clock = [100.0]
    monkeypatch.setattr(ab.time, "monotonic", lambda: clock[0])
    await act.apply(10.0)
    clock[0] = 110.0
    events.put({"kind": "stats", "t": 110.0, "dt": 10.0,
                "rx_bytes": 12_500_000, "rx_objs": 12_500})
    sig = await act.observe()
    assert abs(sig.rx_mbps - 10.0) < 0.01, sig.rx_mbps



async def test_steady_reports_read_the_full_rate(monkeypatch):
    act, events = _actuator()
    clock = [1000.0]
    monkeypatch.setattr(ab.time, "monotonic", lambda: clock[0])
    await act.apply(10.0)
    clock[0] = 1010.5
    for t in range(1006, 1011):
        events.put({"kind": "stats", "t": float(t), "dt": 1.0,
                    "rx_bytes": 1_250_000})
    sig = await act.observe()
    assert abs(sig.rx_mbps - 10.0) < 0.01, sig.rx_mbps


async def test_empty_reports_after_a_burst_lower_the_rate(monkeypatch):
    act, events = _actuator()
    clock = [1000.0]
    monkeypatch.setattr(ab.time, "monotonic", lambda: clock[0])
    await act.apply(10.0)
    clock[0] = 1010.0
    for t in range(1003, 1011):
        events.put({"kind": "stats", "t": float(t), "dt": 1.0,
                    "rx_bytes": 1_250_000 if t <= 1007 else 0})
    sig = await act.observe()
    assert abs(sig.rx_mbps - 4.0) < 0.01, sig.rx_mbps

async def test_loss_is_a_share_of_received_plus_lost():
    act, events = _actuator()
    events.put({"kind": "stats", "rx_objs": 10, "iv_lost": 20})
    sig = await act.observe()
    assert abs(sig.loss_pct - 100 * 20 / 30) < 1e-9, sig.loss_pct


def test_silence_after_a_burst_lowers_the_rate(monkeypatch):
    live = ab.LiveStats(1000)
    clock = [100.0]
    monkeypatch.setattr(ab.time, "monotonic", lambda: clock[0])
    for t, oid in ((100.0, 0), (100.1, 1)):
        clock[0] = t
        live.on_object(SimpleNamespace(group_id=0, object_id=oid,
                                       subgroup_id=0, extensions=None),
                       1000, int(time.time() * 1e6))
    burst = live.sample(0.1).rx_bitrate_mbps
    clock[0] = 104.9
    after = live.sample(0.1).rx_bitrate_mbps
    assert abs(burst - 0.16) < 1e-6, burst
    assert abs(after - 2000 * 8 / (4.9 * 1e6)) < 1e-9, after


async def test_no_latency_samples_print_as_na(monkeypatch, capsys):
    act, events = _actuator()
    await act.apply(10.0)
    events.put({"kind": "stats", "t": ab.time.monotonic(),
                "rx_bytes": 1000})
    sig = await act.observe()
    assert not sig.latency_known
    ctl = object.__new__(ab.AIMDController)
    ctl.actuator, ctl.t0, ctl._hdr_done = act, sig.t, True
    ctl._print_row(sig, "ramp")
    row = capsys.readouterr().out
    assert "n/a" in row and "0.0us" not in row, row


def _at_max(duration_s, p90s=(), stop_after=None, max_level=10, exit_at_max=False):
    """A controller at level 10; p90s feed the first ticks, then 0."""
    state = ab.BenchState()
    p90s = list(p90s)
    ticks = [0]

    async def observe():
        ticks[0] += 1
        if stop_after and ticks[0] >= stop_after:
            state.stop.set()
        p90 = p90s.pop(0) if p90s else 0.0
        return ab.Signal(t=time.monotonic(), level=10, latency_mean_ms=p90,
                         latency_p90_ms=p90, loss_pct=0, shortfall=False,
                         target_mbps=10, tx_mbps=10, rx_mbps=10)

    actuator = SimpleNamespace(initial_level=10, unit='Mbps', min_level=1,
                               max_level=max_level, initial_step=1, step_floor=1,
                               apply=AsyncMock(), observe=observe)
    ctl = ab.AIMDController(actuator, state, interval_s=0.001,
                            latency_threshold_ms=100, duration_s=duration_s,
                            exit_at_max=exit_at_max)
    actions = []
    ctl._emit = lambda sig, action, force=False: actions.append(action)
    return ctl, state, actions, actuator


async def test_holds_at_max_until_duration():
    ctl, state, actions, actuator = _at_max(0.05)
    await ctl.run()
    assert 'duration cap reached' in state.ceiling_reason
    assert actions.count('hold (max)') > 1, actions
    assert ctl.held_at_max and ctl.high_water == 10
    actuator.apply.assert_awaited_with(10)


async def test_zero_duration_holds_until_stopped():
    for duration_s in (0, None):
        ctl, state, actions, _ = _at_max(duration_s, stop_after=20)
        await ctl.run()
        assert state.ceiling_reason is None
        assert actions.count('hold (max)') >= 15, actions


async def test_exit_max_still_respects_duration():
    ctl, state, _, _ = _at_max(0.05, max_level=100, exit_at_max=True)
    await ctl.run()
    assert 'duration cap reached' in state.ceiling_reason


async def test_exit_max_ends_at_max():
    ctl, state, actions, _ = _at_max(30, exit_at_max=True)
    await ctl.run()
    assert state.ceiling_reason == 'reached max 10 Mbps'
    assert actions[-1] == 'max' and not ctl.held_at_max
    assert ctl.high_water == 10


async def test_back_off_still_applies_at_max():
    ctl, state, actions, actuator = _at_max(None, p90s=[500, 500, 500], stop_after=10)
    await ctl.run()
    assert 'back-off (p90 over)' in actions
    assert any(call.args[0] < 10 for call in actuator.apply.await_args_list)


def test_summary_says_it_held_at_max(capsys):
    ctl, state, _, _ = _at_max(1)
    ctl.held_at_max = True
    ab._print_summary(state, ctl, 5, None)
    assert 'held at max 10 Mbps' in capsys.readouterr().out
