"""adaptive_bench --join-steps: the join rate a relay admits under an audience."""
import asyncio
import queue
import random
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from aiomoqt.tools import adaptive_bench as ab
from aiomoqt.utils import workers
from aiomoqt.utils.stats import TrackStats


class FakeJoins:
    """A subs actuator whose joins and viewer p90 follow the join rate."""
    unit = 'subs'
    min_level = 1.0
    step_floor = initial_step = 5.0
    initial_level = 1.0

    def __init__(self, p90=lambda rate: 20, sub_ms=lambda rate: 50,
                 first_ms=lambda rate: 100, failed=lambda rate: 0, joins=lambda rate: 100,
                 skipped=lambda rate: 0, joiner_slots=1000):
        self.p90, self.sub_ms, self.first_ms = p90, sub_ms, first_ms
        self.failed, self.joins, self.skipped = failed, joins, skipped
        self.joiner_slots = joiner_slots
        self.max_level, self.level, self.rate = 20.0, 0.0, 0.0
        self.rates = []

    async def apply(self, level):
        self.level = float(level)

    def set_join_rate(self, rate):
        self.rate = rate
        self.rates.append(rate)

    def settled(self):
        return True

    @property
    def lost_total(self):
        return 0, 1000

    async def observe(self):
        r = self.rate
        n = self.joins(r) if r else 0
        p90 = self.p90(r)
        return ab.Signal(t=time.monotonic(), level=self.level, latency_mean_ms=p90,
                         latency_p90_ms=p90, loss_pct=0, shortfall=False,
                         target_mbps=1, tx_mbps=1, rx_mbps=1, joins=n,
                         join_failed=self.failed(r) if r else 0,
                         join_skipped=self.skipped(r) if r else 0,
                         join_sub_ms=[self.sub_ms(r)] * n, join_first_ms=[self.first_ms(r)] * n)


def _ctl(relay, steps=(5, 10, 20, 50), duration_s=30):
    state = ab.BenchState()
    ctl = ab.JoinRateController(relay, state, join_steps=steps, step_s=0.01, base_s=0.01,
                                join_bound_ms=1000, group_s=0.5, interval_s=0.001,
                                latency_threshold_ms=500, duration_s=duration_s)
    ctl.SETTLE_S = 0.005
    actions = []
    ctl._emit = lambda sig, action, force=False: actions.append(action)
    return ctl, state, actions


async def test_steps_pass_until_join_failures_pass_one_percent():
    relay = FakeJoins(failed=lambda rate: 2 if rate >= 20 else 1)
    ctl, state, actions = _ctl(relay)
    await ctl.run()
    assert [s.rate for s in ctl.steps] == [5, 10, 20]
    assert [s.fault for s in ctl.steps] == [None, None, 'joins failed']
    assert ctl.supported == 10
    assert relay.rates == [5, 10, 20, 0], 'the failing step stops cycling'
    assert state.ceiling_reason == 'join step 20/s failed (joins failed)'
    assert 'pass 5/s' in ' '.join(actions)


async def test_a_slow_subscribe_ok_fails_the_step():
    ctl, state, _ = _ctl(FakeJoins(sub_ms=lambda rate: 1500 if rate >= 20 else 50))
    await ctl.run()
    assert ctl.supported == 10
    assert ctl.steps[-1].fault == 'SUBSCRIBE_OK p99 over 1000 ms'


async def test_a_late_first_object_fails_the_step():
    # The bound is join_bound + one group: 1000 + 500 ms.
    ctl, _, _ = _ctl(FakeJoins(first_ms=lambda rate: 1600 if rate >= 50 else 1400))
    await ctl.run()
    assert ctl.supported == 20
    assert ctl.steps[-1].fault == 'first object p99 over 1500 ms'


async def test_a_viewer_over_the_threshold_fails_the_step():
    ctl, state, _ = _ctl(FakeJoins(p90=lambda rate: 600 if rate >= 10 else 20))
    await ctl.run()
    assert ctl.supported == 5
    assert ctl.steps[-1].fault == 'p90 over'
    assert state.ceiling_reason == 'join step 10/s failed (p90 over)'


async def test_every_step_passing_ends_the_run():
    relay = FakeJoins()
    ctl, state, _ = _ctl(relay)
    await ctl.run()
    assert ctl.supported == 50 and all(s.fault is None for s in ctl.steps)
    assert state.ceiling_reason == 'all 4 join steps passed'
    assert relay.rates[-1] == 0


async def test_a_step_without_joins_fails():
    ctl, _, _ = _ctl(FakeJoins(joins=lambda rate: 0 if rate >= 20 else 100))
    await ctl.run()
    assert ctl.supported == 10 and ctl.steps[-1].fault == 'no joins'
    assert ctl.steps[-1].line() == (
        '  Join step:   20/s failed (no joins), 0 joins, 0 failed, '
        'SUBSCRIBE_OK p50/p99 0/0 ms, first object p50/p99 0/0 ms')


async def test_a_base_audience_that_does_not_hold_is_not_demonstrated(capsys):
    relay = FakeJoins(p90=lambda rate: 600)
    ctl, state, _ = _ctl(relay)
    await ctl.run()
    assert ctl.steps == [] and ctl.supported is None
    assert state.ceiling_reason == 'not demonstrated: base audience did not hold (p90 over)'
    assert relay.level == 20, 'the base is the max level'
    ab._print_summary(state, ctl, 5, None)
    out = capsys.readouterr().out
    assert '  Join rate:   not demonstrated (base audience did not hold: p90 over)\n' in out
    assert 'Join step' not in out and 'Sustained' not in out


async def test_the_first_step_failing_is_not_demonstrated(capsys):
    ctl, state, _ = _ctl(FakeJoins(failed=lambda rate: 50))
    await ctl.run()
    ab._print_summary(state, ctl, 5, None)
    out = capsys.readouterr().out
    assert '  Join rate:   not demonstrated (5/s failed: joins failed)\n' in out


async def test_the_run_ending_mid_step_keeps_what_passed():
    ctl, state, _ = _ctl(FakeJoins(), steps=(5, 10, 20, 50), duration_s=0.03)
    ctl.step_s = 0.008
    await ctl.run()
    assert state.ceiling_reason.startswith('duration cap reached')
    assert ctl.steps[-1].fault == 'run ended'
    assert all(s.fault is None for s in ctl.steps[:-1])


def test_summary_lines(capsys):
    ctl, state, _ = _ctl(FakeJoins())
    ctl.steps = [ab.JoinStep(5, joins=75, failed=0, sub_ms=[10.0] * 50 + [30.0] * 25,
                             first_ms=[40.0] * 74 + [900.0]),
                 ab.JoinStep(12.5, joins=150, failed=3, sub_ms=[20.0] * 150,
                             first_ms=[60.0] * 150, fault='joins failed')]
    ctl.supported = 5
    ab._print_summary(state, ctl, 5, None)
    lines = capsys.readouterr().out.splitlines()
    assert ('  Join step:   5/s passed, 75 joins, 0 failed, SUBSCRIBE_OK p50/p99 10/30 ms, '
            'first object p50/p99 40/900 ms') in lines
    assert ('  Join step:   12.5/s failed (joins failed), 150 joins, 3 failed, '
            'SUBSCRIBE_OK p50/p99 20/20 ms, first object p50/p99 60/60 ms') in lines
    assert '  Join rate:   5 subs/s supported' in lines
    assert not any('High-water' in x or 'Sustained' in x for x in lines)


async def test_a_pool_limited_step_keeps_the_last_rate(capsys):
    relay = FakeJoins(joins=lambda rate: 0 if rate >= 20 else 100,
                      skipped=lambda rate: 5 if rate >= 20 else 0)
    ctl, state, _ = _ctl(relay)
    await ctl.run()
    assert ctl.steps[-1].fault == 'joiner pool exhausted' and ctl.pool_limited
    ab._print_summary(state, ctl, 5, None)
    out = capsys.readouterr().out
    assert '  Join step:   20/s failed (joiner pool exhausted), 0 joins, 0 failed,' in out
    assert '  Join rate:   10 subs/s supported (pool-limited)\n' in out


def test_a_pool_exhausted_with_another_fault_fails_on_the_other():
    ctl, _, _ = _ctl(FakeJoins())
    step = ab.JoinStep(10, joins=50, sub_ms=[1500.0], first_ms=[1.0], skipped=3, seconds=10.0)
    assert ctl._judge(step) == 'SUBSCRIBE_OK p99 over 1000 ms'
    ctl.supported, ctl.steps = 5, [step]
    step.fault = ctl._judge(step)
    assert not ctl.pool_limited


@pytest.mark.parametrize('attempts, skipped, why', [
    (90, 0, None), (89, 0, 'rate not reached'), (89, 2, 'joiner pool exhausted'),
    (0, 2, 'joiner pool exhausted'), (0, 0, 'no joins'),
])
def test_a_step_must_reach_ninety_percent_of_its_rate(attempts, skipped, why):
    ctl, _, _ = _ctl(FakeJoins())
    step = ab.JoinStep(10, joins=attempts, sub_ms=[1.0], first_ms=[1.0],
                       skipped=skipped, seconds=10.0)
    assert ctl._judge(step) == why


@pytest.mark.parametrize('pool, warned', [(6, True), (90, False)])
async def test_a_small_joiner_pool_warns_once(capsys, pool, warned):
    ctl, _, _ = _ctl(FakeJoins(joiner_slots=pool))
    await ctl.run()
    out = capsys.readouterr().out
    line = "  warning: 6 joiner slots reach about 4 joins/s, under the top step's 50/s"
    assert out.count(line) == (1 if warned else 0)
    assert ('warning' in out) is warned


def test_percentile_is_nearest_rank():
    assert ab._pctl([], 99) == 0.0
    assert ab._pctl([3.0, 1.0, 2.0], 50) == 2.0
    assert ab._pctl(list(range(1, 101)), 99) == 99
    assert ab._pctl(list(range(1, 101)), 100) == 100


def test_exactly_one_percent_failed_passes():
    ctl, _, _ = _ctl(FakeJoins())
    assert ctl._judge(ab.JoinStep(5, joins=99, failed=1, sub_ms=[1], first_ms=[1])) is None
    assert ctl._judge(ab.JoinStep(5, joins=98, failed=2, sub_ms=[1],
                                  first_ms=[1])) == 'joins failed'


@pytest.mark.parametrize('extra, why', [
    (['--join-steps', '5,10'], 'subs-mode only'),
    (['--join-steps', '5,10', '--mode', 'subs', '--hold', '30'], 'drop --hold'),
    (['--join-steps', '5,10', '--mode', 'subs', '--exit', 'max'], 'drop --exit'),
    (['--join-steps', '5,10', '--mode', 'subs', '--exit', 'sustained'], 'drop --exit'),
    (['--join-steps', '5,10', '--mode', 'subs', '--too-far-behind', '2'],
     'drop --too-far-behind'),
    (['--join-steps', '10,5', '--mode', 'subs'], 'ascending'),
    (['--join-steps', '0,5', '--mode', 'subs'], 'ascending'),
    (['--join-steps', 'a', '--mode', 'subs'], 'not a list of rates'),
    (['--join-steps', '5', '--mode', 'subs', '--step-s', '0'], 'positive'),
    (['--join-steps', '5', '--mode', 'subs', '--watchers', '1'], 'between 0 and 1'),
    (['--join-steps', '5', '--mode', 'subs', '--watchers', '0'], 'between 0 and 1'),
])
def test_join_steps_refuses(monkeypatch, capsys, extra, why):
    monkeypatch.setattr(sys, 'argv', ['moq-adaptive-bench', 'moqt://127.0.0.1:4433', *extra])
    with pytest.raises(SystemExit):
        ab.parse_args()
    assert why in capsys.readouterr().err


def test_the_banner_says_start_subs_is_ignored(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['moq-adaptive-bench', 'moqt://127.0.0.1:4433',
                                      '--mode', 'subs', '--join-steps', '5,10',
                                      '--max-subs', '40', '--start-subs', '7'])
    ab._print_banner(ab.parse_args())
    assert ('  join rate:          40 subs (--start-subs ignored) held 30 s, then 5,10 '
            'joins/s, 15 s each, bound 1000 ms, 25% watchers') in capsys.readouterr().out


def test_group_duration_follows_the_publisher(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['moq-adaptive-bench', 'moqt://127.0.0.1:4433',
                                      '--mode', 'subs', '--join-steps', '5,10',
                                      '--sub-mbps', '8', '-s', '1000', '-g', '500'])
    args = ab.parse_args()
    assert args.join_steps == [5.0, 10.0]
    assert ab._group_s(args) == pytest.approx(0.5)
    args.video, args.video_fps, args.group_size = '1080p', 30, 60
    assert ab._group_s(args) == pytest.approx(2.0)


# -- actuator ---------------------------------------------------------------

class _Queue(queue.Queue):
    def cancel_join_thread(self):
        pass

    def close(self):
        pass


class _Process:
    def __init__(self, target, args, **kwargs):
        self.cfg, self.alive = args[0], False

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, **kwargs):
        self.alive = False

    terminate = join


def _actuator(monkeypatch, step_subs=1, max_subs=4):
    monkeypatch.setattr(workers, 'MP_CTX', SimpleNamespace(
        Queue=_Queue, Event=threading.Event, Process=_Process,
        Value=lambda typ, v: SimpleNamespace(value=v)))
    return ab.SubsActuator('moqt://127.0.0.1:4433', 'ns', 'track', None, True,
                           force_quic=False, min_subs=1, max_subs=max_subs, start_subs=1,
                           step_subs=step_subs, object_size=1000, per_sub_mbps=8.0,
                           interval_s=1.0, stagger=0.0, cycling=True)


def _report(wid=0, **extra):
    return {'kind': 'batch_stats', 'worker_id': wid, 'batch_size': 2, 'active': 2,
            'delivering': 2, 't': time.monotonic(), 'dt': 1.0, 'rx_bytes': 2_000_000,
            'rx_objs': 30, 'loss': 0, 'lat_n': 0, 'lat_p90_ms': 0.0, 'lat_mean_ms': 0.0,
            'jitter_ms': 0.0, 'total_objs': 30, **extra}


async def test_the_join_rate_is_split_across_live_workers(monkeypatch):
    act = _actuator(monkeypatch)
    await act.apply(1)
    await act.apply(2)
    act.set_join_rate(10)
    assert [v.value for v in act._cycle_rates.values()] == [5.0, 5.0]
    assert all(p.cfg['cycle_rate'] is act._cycle_rates[w]
               for w, (p, _) in act._workers.items())
    await act.apply(3)
    assert [round(v.value, 3) for v in act._cycle_rates.values()] == [3.333] * 3
    act._kill_worker(0)
    assert [v.value for v in act._cycle_rates.values()] == [5.0, 5.0]
    act.set_join_rate(0)
    assert [v.value for v in act._cycle_rates.values()] == [0.0, 0.0]
    await act.shutdown()


async def test_observe_totals_the_joins_of_the_tick(monkeypatch):
    act = _actuator(monkeypatch, step_subs=2, max_subs=2)
    await act.apply(2)
    act._spawned_t[0] -= 100.0
    act._events_q.put(_report(joins=2, join_failed=1, join_sub_ms=[10.0, 20.0],
                              join_first_ms=[30.0, 40.0]))
    act._events_q.put(_report(joins=1, join_sub_ms=[15.0], join_first_ms=[35.0]))
    sig = await act.observe()
    assert (sig.joins, sig.join_failed) == (3, 1)
    assert sig.join_sub_ms == [10.0, 20.0, 15.0]
    assert sig.join_first_ms == [30.0, 40.0, 35.0]
    sig = await act.observe()
    assert (sig.joins, sig.join_failed, sig.join_sub_ms) == (0, 0, [])
    await act.shutdown()


async def test_only_judged_slots_are_expected_to_deliver(monkeypatch):
    act = _actuator(monkeypatch, step_subs=2, max_subs=2)
    await act.apply(2)
    act._spawned_t[0] -= 100.0
    for _ in range(3):
        act._events_q.put(_report(active=1, delivering=1, judged=1, rx_bytes=1_000_000))
        sig = await act.observe()
        assert not sig.shortfall
    for _ in range(2):
        act._events_q.put(_report(active=1, delivering=1, rx_bytes=1_000_000))
        sig = await act.observe()
    assert sig.shortfall, 'the same slot short while judged is'
    await act.shutdown()


async def test_joiner_slots_and_watchers_reach_the_workers(monkeypatch):
    act = _actuator(monkeypatch, step_subs=4, max_subs=8)
    await act.apply(8)
    assert act.joiner_slots == 6
    assert all(p.cfg['watchers'] == 0.25 for p, _ in act._workers.values())
    await act.shutdown()


# -- worker bookkeeping -----------------------------------------------------

def _slot(**kw):
    state = workers.new_slot_state(workers.new_join_tally())
    state.update(kw)
    return state


def test_a_join_is_timed_from_its_dial_to_its_first_object():
    state = _slot()
    workers.join_begin(state, 100.0)
    workers.join_mark(state, 'join_setup_ms', 100.05)
    workers.join_mark(state, 'join_sub_ms', 100.1)
    workers.join_mark(state, 'join_sub_ms', 100.2)
    workers.join_first_object(state, 100.3)
    workers.join_first_object(state, 100.4)
    tally = workers.take_join_tally(state['tally'])
    assert (tally['joins'], tally['join_failed']) == (1, 0)
    assert tally['join_setup_ms'] == [pytest.approx(50)]
    assert tally['join_sub_ms'] == [pytest.approx(100)]
    assert tally['join_first_ms'] == [pytest.approx(300)]
    assert workers.take_join_tally(state['tally']) == workers.new_join_tally()


def test_a_first_object_ahead_of_subscribe_ok_completes_the_join_there():
    state = _slot()
    workers.join_begin(state, 100.0)
    workers.join_first_object(state, 100.08)
    workers.join_first_object(state, 100.09)
    assert state['tally']['joins'] == 0
    workers.join_mark(state, 'join_sub_ms', 100.1)
    assert state['tally']['joins'] == 1 and state['join_t0'] is None
    assert state['tally']['join_first_ms'] == [pytest.approx(80)]


def test_a_join_without_a_first_object_fails_once():
    state = _slot(cycling=True)
    workers.join_begin(state, 100.0)
    workers.join_expire(state, 109.0)
    assert state['join_t0'] == 100.0
    workers.join_expire(state, 110.5)
    workers.join_fail(state)
    assert state['tally']['join_failed'] == 1


def test_join_samples_are_capped():
    state = _slot()
    for i in range(workers.JOIN_SAMPLES_MAX + 10):
        workers.join_begin(state, i)
        workers.join_mark(state, 'join_setup_ms', i + 0.01)
        workers.join_mark(state, 'join_sub_ms', i + 0.02)
        workers.join_first_object(state, i + 0.03)
    tally = state['tally']
    assert tally['joins'] == workers.JOIN_SAMPLES_MAX + 10
    assert len(tally['join_first_ms']) == workers.JOIN_SAMPLES_MAX


def _joiner(**kw):
    return _slot(**{'watcher': False, 'active': True, 'joined_at': 0.0, **kw})


def test_a_cycle_picks_an_eligible_joiner():
    rng = random.Random(1)
    states = [_slot(active=True, joined_at=0.0), _joiner(active=False),
              _joiner(join_t0=5.0), _joiner()]
    picked = workers.pick_cycle_slot(states, 10.0, rng)
    assert picked is states[3] and picked['cycle']
    assert workers.pick_cycle_slot(states, 10.0, rng) is None, 'a watcher is never cycled'


def test_a_joiner_is_eligible_after_its_first_object_and_the_dwell():
    state = _joiner(joined_at=None)
    workers.join_begin(state, 100.0)
    workers.join_mark(state, 'join_sub_ms', 100.1)
    assert workers.pick_cycle_slot([state], 105.0) is None, 'no first object yet'
    workers.join_first_object(state, 100.2)
    assert workers.pick_cycle_slot([state], 100.2 + workers.JOINER_DWELL_S - 0.01) is None
    assert workers.pick_cycle_slot([state], 100.2 + workers.JOINER_DWELL_S) is state


def test_watchers_are_the_first_share_of_a_batch():
    assert workers.watcher_count(5, 0.25) == 2
    assert workers.watcher_count(20, 0.25) == 5
    assert workers.watcher_count(1, 0.25) == 1
    assert workers.watcher_count(5, None) == 5


def _feed(stats, groups):
    for g in groups:
        for o in range(8):
            stats.on_object(SimpleNamespace(group_id=g, object_id=o, subgroup_id=0,
                                            extensions=None), 100, 0)


@pytest.mark.parametrize('rate, judged, lossy, skipped', [(0.0, 4, 3, False),
                                                          (1000.0, 1, 0, True)])
async def test_health_is_judged_on_watchers_while_joiners_cycle(
        monkeypatch, rate, judged, lossy, skipped):
    async def supervisor(config, relay, stop_ev, stats, state, delay, shared):
        state['active'] = True
        _feed(stats, (0, 1, 2, 3, 4, 5) if state['watcher'] else (0, 1, 3, 4, 5))
        await stop_ev.wait()

    monkeypatch.setattr(workers, '_slot_supervisor', supervisor)
    monkeypatch.setattr(workers, 'SharedTransport',
                        lambda **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(workers, 'STATS_INTERVAL_S', 0.05)
    stop, reports = threading.Event(), queue.Queue()
    task = asyncio.create_task(workers._subscriber_batch_task(
        dict(worker_id=0, batch_size=4, relay_url='moqt://127.0.0.1:4433', watchers=0.25,
             cycle_rate=SimpleNamespace(value=rate)), stop, reports))
    report = await asyncio.to_thread(reports.get, timeout=5)
    stop.set()
    await task
    assert (report['judged'], report['lossy'], report['active']) == (judged, lossy, judged)
    assert report['loss'] == 8 * lossy
    assert report['total_objs'] == 4 * 40 + 8
    assert (report['join_skipped'] > 0) is skipped, 'no joiner has joined yet'


def test_cycles_follow_the_rate():
    assert workers.next_cycle(None, 10.0, 0) == (False, None)
    due, t = workers.next_cycle(None, 10.0, 4.0)
    assert (due, t) == (False, 10.25)
    assert workers.next_cycle(t, 10.2, 4.0) == (False, 10.25)
    assert workers.next_cycle(t, 10.3, 4.0) == (True, 10.5)
    assert workers.next_cycle(10.5, 15.0, 4.0) == (True, 15.0), 'catches up one cycle at most'
    assert workers.next_cycle(15.0, 15.0, 0) == (False, None)


def test_a_cycled_slot_starts_fresh_stats_and_keeps_the_totals():
    old = TrackStats(group_loss=True)
    for g in (0, 2):
        old.on_object(SimpleNamespace(group_id=g, object_id=0, subgroup_id=0,
                                      extensions=None), 100, 0)
    state = _slot(lost=3, lost_at=50.0)
    state['hist'].append((1.0, 200))
    new = workers.renew_stats(state, old)
    assert new is state['stats'] and new is not old and new.total_objects == 0
    assert state['carried'] == {'loss': old.total_lost, 'total_bytes': 200, 'total_objs': 2}
    assert state['lost'] == 0 and not state['hist']


class _Session:
    async def client_session_init(self):
        pass

    def close(self):
        pass


class _Client:
    dials = 0

    def __init__(self, *args, **kwargs):
        pass

    def connect(self):
        _Client.dials += 1
        return self

    async def __aenter__(self):
        return _Session()

    async def __aexit__(self, *exc):
        return False


async def test_a_cycled_slot_redials_at_once_as_a_new_viewer(monkeypatch):
    stop = asyncio.Event()
    state = _slot()
    stats = TrackStats(group_loss=True)

    class Track:
        state = None

        def __init__(self, *args, on_object, **kwargs):
            self.on_object = on_object

        async def subscribe(self, **kwargs):
            if _Client.dials == 1:
                # Ahead of SUBSCRIBE_OK's processing, as a racing data stream is.
                self.on_object(SimpleNamespace(group_id=0, object_id=0, subgroup_id=0,
                                               extensions=None), 100, 0)
                state['cycle'] = True
            else:
                stop.set()

    monkeypatch.setattr(workers, 'MOQTClient', _Client)
    monkeypatch.setattr(workers, 'SubscribedTrack', Track)
    monkeypatch.setattr(workers, 'SELF_HEAL_BACKOFF_S', 30.0)
    monkeypatch.setattr(workers, 'STOP_POLL_S', 0.001)
    monkeypatch.setattr(_Client, 'dials', 0)
    relay = SimpleNamespace(host='127.0.0.1', port=4433, path='', use_quic=True)
    t0 = time.monotonic()
    await workers._slot_supervisor({'namespace': 'ns', 'trackname': 't'}, relay,
                                   stop, stats, state, 0)
    assert time.monotonic() - t0 < 5.0, 'no self-heal backoff'
    assert _Client.dials == 2
    assert state['tally']['joins'] == 1 and state['tally']['join_failed'] == 0
    assert state['stats'] is not stats and state['carried']['total_objs'] == 1
