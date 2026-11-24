"""adaptive_bench --hold: step and hold to a confirmed equilibrium."""
import asyncio
import sys
import time

import pytest

from aiomoqt.tools import adaptive_bench as ab


class Clock:
    """Virtual time, advanced one tick per controller tick."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _on_virtual_time(ctl, clock):
    """Run ctl's ticks on clock, so decisions do not depend on the host's speed."""
    async def tick():
        clock.t += ctl.tick_s
        await asyncio.sleep(0)
        return ctl.state.stop.is_set()
    ctl._wait_tick = tick


class FakeRelay:
    """A subs actuator whose p90 and loss follow the level and the time at it."""
    unit = 'subs'
    min_level = 50.0
    keep_window = 0.0
    step_floor = initial_step = 50.0

    def __init__(self, p90, start=100, max_level=10_000, lost=lambda level, t: 0):
        self.p90, self.lost_fn = p90, lost
        self.clock = Clock()
        self.initial_level, self.max_level = float(start), float(max_level)
        self.level, self.since, self.lost = float(start), self.clock(), 0
        self.applied = []
        self.drops = 0

    async def drop_all(self):
        self.level, self.since = 0.0, self.clock()
        self.drops += 1

    async def apply(self, level):
        if level != self.level:
            self.level, self.since = float(level), self.clock()
        self.applied.append(float(level))

    def settled(self):
        return True

    @property
    def lost_total(self):
        return self.lost, 1000

    async def observe(self):
        t = self.clock() - self.since
        self.lost += self.lost_fn(self.level, t)
        p90 = self.p90(self.level, t)
        return ab.Signal(t=self.clock(), level=self.level, latency_mean_ms=p90,
                         latency_p90_ms=p90, loss_pct=0, shortfall=False,
                         target_mbps=1, tx_mbps=1, rx_mbps=1 if self.level else 0)


def _backlog_until_reset(relay):
    """After the overshoot to 350, 300 stays over the threshold until everyone is dropped."""
    p90, over = relay.p90, []

    def tracked(level, t):
        over.append(level >= 350)
        return 600 if level == 300 and any(over) and not relay.drops else p90(level, t)
    relay.p90 = tracked
    return relay


def _hold(relay, exit_sustained=True, duration_s=30, recover_s=15.0):
    state = ab.BenchState()
    ctl = ab.HoldController(relay, state, hold_s=0.02, confirm_s=0.05, recover_s=recover_s,
                            recover_clean_s=0.005, reset_gap_s=0.005,
                            exit_sustained=exit_sustained,
                            interval_s=0.001, latency_threshold_ms=500,
                            duration_s=duration_s, clock=relay.clock)
    _on_virtual_time(ctl, relay.clock)
    actions = []
    ctl._emit = lambda sig, action, force=False: actions.append(action)
    return ctl, state, actions


async def test_ends_at_the_last_level_that_held():
    relay = FakeRelay(lambda level, t: 20 if level <= 300 else 600)
    ctl, state, actions = _hold(relay)
    await ctl.run()
    assert ctl.sustained == (300, 20)
    assert state.ceiling_reason == 'equilibrium confirmed at 300 subs'
    assert 'fail (p90 over), back to 300' in actions
    assert relay.drops == 0, 'the passing audience stays on'
    assert max(relay.applied) == 350, 'one step past the knee, no further'


async def test_a_rising_p90_fails_the_step_under_the_threshold():
    relay = FakeRelay(lambda level, t: 20 if level < 300 else 100 if t < 0.01 else 400)
    ctl, state, actions = _hold(relay)
    await ctl.run()
    assert 'fail (p90 still rising), back to 250' in actions
    assert ctl.sustained == (250, 20)


async def test_a_p90_swing_under_half_the_threshold_is_not_rising():
    relay = FakeRelay(lambda level, t: 600 if level > 300 else 60 if t < 0.01 else 160)
    ctl, state, actions = _hold(relay)
    await ctl.run()
    assert not any('rising' in action for action in actions)
    assert 'fail (p90 over), back to 300' in actions
    assert ctl.sustained == (300, 160)


async def test_lost_objects_fail_the_step():
    relay = FakeRelay(lambda level, t: 20, lost=lambda level, t: level >= 350)
    ctl, state, actions = _hold(relay)
    await ctl.run()
    assert 'fail (objects lost), back to 300' in actions
    assert ctl.sustained == (300, 20)


async def test_a_failed_confirmation_steps_down_one_group():
    relay = FakeRelay(lambda level, t: 20 if level < 300 or (level == 300 and t < 0.03) else 600)
    ctl, state, actions = _hold(relay)
    await ctl.run()
    assert 'fail (p90 over), down to 250' in actions
    assert ctl.sustained == (250, 20)


async def test_a_level_that_does_not_drain_gets_one_reset():
    relay = _backlog_until_reset(FakeRelay(lambda level, t: 600 if level >= 350 else 20))
    ctl, state, actions = _hold(relay, recover_s=0.03)
    await ctl.run()
    assert relay.drops == 1
    assert 'fail (p90 over), back to 300' in actions
    assert 'no recovery, reset at 300' in actions and 'rejoin to 300' in actions
    assert ctl.sustained == (300, 20)


async def test_no_data_during_a_reset_does_not_abort():
    relay = _backlog_until_reset(FakeRelay(lambda level, t: 600 if level >= 350 else 20))
    ctl, state, actions = _hold(relay, recover_s=0.03)
    ctl.reset_gap_s = 0.05
    await ctl.run()
    assert not state.failed and actions.count('reset') > 3
    assert ctl.sustained == (300, 20)


async def test_a_level_that_does_not_recover_steps_down():
    # After the overshoot to 350, the backlog keeps 300 over the threshold.
    seen = set()
    relay = FakeRelay(lambda level, t: 600 if level >= 350 or (level == 300 and 350 in seen) else 20)
    apply = relay.apply

    async def tracked(level):
        seen.add(level)
        await apply(level)
    relay.apply = tracked
    ctl, state, actions = _hold(relay, recover_s=0.03)
    await ctl.run()
    assert 'fail (p90 over), back to 300' in actions
    assert 'no recovery, reset at 300' in actions and relay.drops == 1
    assert 'fail (no recovery), down to 250' in actions
    assert ctl.sustained == (250, 20)


async def test_step_downs_are_bounded():
    relay = FakeRelay(lambda level, t: 20 if t < 0.03 else 600, start=300, max_level=400)
    ctl, state, actions = _hold(relay)
    await ctl.run()
    assert ctl.sustained is None and ctl.last_pass == 400
    assert state.ceiling_reason.startswith('not demonstrated: p90 over after 3 step(s) down')


async def test_the_first_level_failing_is_not_demonstrated():
    ctl, state, _ = _hold(FakeRelay(lambda level, t: 600))
    await ctl.run()
    assert ctl.sustained is None and ctl.last_pass is None
    assert state.ceiling_reason == 'not demonstrated: p90 over at the first level, 100 subs'


async def test_confirms_at_max():
    ctl, state, _ = _hold(FakeRelay(lambda level, t: 20, max_level=200))
    await ctl.run()
    assert ctl.sustained == (200, 20)
    assert state.ceiling_reason == 'equilibrium confirmed at 200 subs (--max-subs)'


async def test_holds_the_confirmed_level_to_the_end():
    ctl, state, actions = _hold(FakeRelay(lambda level, t: 20, max_level=200),
                                exit_sustained=False, duration_s=0.3)
    await ctl.run()
    assert ctl.sustained == (200, 20) and ctl.held_s > 0.15
    assert state.ceiling_reason == 'held 200 subs to the end'
    assert 'hold (sustained)' in actions


async def test_a_fault_after_confirmation_steps_down_and_confirms_again():
    # Another host's load arrives once 200 has held for a while.
    relay = FakeRelay(lambda level, t: 600 if level == 200 and t > 0.12 else 20, max_level=200)
    ctl, state, actions = _hold(relay, exit_sustained=False, duration_s=0.5)
    await ctl.run()
    assert 'fail (p90 over), down to 150' in actions
    assert ctl.sustained == (150, 20)
    assert state.ceiling_reason == 'held 150 subs to the end'


def test_steps_shrink_as_p90_nears_the_threshold():
    ctl, _, _ = _hold(FakeRelay(lambda level, t: 20))
    assert ctl._next_level(2000, 20) == 2200
    assert ctl._next_level(2000, 100) == 2100
    assert ctl._next_level(2000, 200) == 2050
    assert ctl._next_level(100, 20) == 150, 'never less than one group'


def test_summary_says_what_held(capsys):
    ctl, state, _ = _hold(FakeRelay(lambda level, t: 20))
    ctl.sustained, ctl.held_s = (300, 20.4), 95.2
    ab._print_summary(state, ctl, 5, None)
    out = capsys.readouterr().out
    assert 'Sustained:   300 subs, held 95 s, p90 max 20 ms, 0 objects lost' in out
    assert 'High-water' not in out and 'Equilibrium' not in out
    ctl.sustained, ctl.last_pass = None, 250
    ab._print_summary(state, ctl, 5, None)
    out = capsys.readouterr().out
    assert 'Sustained:   not demonstrated' in out
    assert 'Last step:   250 subs held 0.02 s, unconfirmed' in out


def test_settled_waits_for_every_worker_past_its_grace():
    a = object.__new__(ab.SubsActuator)
    now = time.monotonic()
    a._targeted, a.batch_size, a._startup_grace = 100.0, 50, 8.0
    a._workers, a._spawned_t = {1: None, 2: None}, {1: now - 20, 2: now - 20}
    assert a.settled()
    a._spawned_t[2] = now
    assert not a.settled()
    del a._workers[2], a._spawned_t[2]
    assert not a.settled()


async def test_shedding_stops_starting_workers_then_the_furthest_behind():
    a = object.__new__(ab.SubsActuator)
    now = time.monotonic()
    a.min_level, a.max_level, a.batch_size, a._startup_grace = 10.0, 100.0, 10, 8.0
    a._workers = dict.fromkeys(range(1, 6))
    a._spawned_t = {1: now - 60, 2: now - 50, 3: now - 40, 4: now - 2, 5: now - 1}
    a._latest_batch = {1: {'lat_p90_ms': 40}, 2: {'lat_p90_ms': 300}, 3: {'lat_p90_ms': 90}}
    a._ensure_draining = a._reap_dead = lambda: 0
    killed = []

    def kill(wid):
        killed.append(wid)
        del a._workers[wid]
    a._kill_worker = kill
    await a.apply(20)
    assert killed == [5, 4, 2] and list(a._workers) == [1, 3]


@pytest.mark.parametrize('extra, why', [
    (['--hold', '30'], 'subs-mode only'),
    (['--hold', '30', '--mode', 'subs', '--exit', 'max'], 'use --exit sustained'),
    (['--exit', 'sustained', '--mode', 'subs'], '--exit sustained needs --hold'),
    (['--hold', '30', '--mode', 'subs', '--too-far-behind', '2'], 'drop --too-far-behind'),
    (['--hold', '-1', '--mode', 'subs'], '--hold and --confirm: seconds'),
])
def test_hold_refuses(monkeypatch, capsys, extra, why):
    monkeypatch.setattr(sys, 'argv', ['moq-adaptive-bench', 'moqt://127.0.0.1:4433', *extra])
    with pytest.raises(SystemExit):
        ab.parse_args()
    assert why in capsys.readouterr().err
