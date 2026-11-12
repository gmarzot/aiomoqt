"""adaptive_bench --join-burst: the largest burst of joins a relay serves."""
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


class FakeRelay:
    """A subs actuator whose bursts pass per ok(b, i) for the i-th trial,
    with watcher p90 per p90(b, ticks since the burst)."""
    unit = 'subs'
    min_level = 1.0
    step_floor = initial_step = 5.0
    initial_level = 1.0

    def __init__(self, ok=lambda b, i: True, first_ms=lambda b: 100.0, pool=1000,
                 p90=lambda b, ticks: 20.0, resolves=True):
        self.ok, self.first_ms, self.p90, self.resolves = ok, first_ms, p90, resolves
        self.eligible_total = pool
        self.max_level, self.level = 20.0, 0.0
        self.tried, self.ticks = [], 0

    async def apply(self, level):
        self.level = float(level)

    def settled(self):
        return True

    @property
    def lost_total(self):
        return 0, 1000

    def start_trial(self, b):
        self.tried.append(b)
        self.ticks = 0
        return time.monotonic()

    def trial_view(self):
        b = self.tried[-1]
        first = self.first_ms(b) if self.ok(b, len(self.tried) - 1) else 5000.0
        return dict(picked=b, joins=[(10.0, first)] * b, failed=0, resolved=self.resolves)

    async def observe(self):
        self.ticks += 1
        p90 = self.p90(self.tried[-1] if self.tried else 0, self.ticks)
        return ab.Signal(t=time.monotonic(), level=self.level, latency_mean_ms=p90,
                         latency_p90_ms=p90, loss_pct=0, shortfall=False,
                         target_mbps=1, tx_mbps=1, rx_mbps=1)


def _ctl(relay, b0=4, join_max=None, duration_s=30):
    state = ab.BenchState()
    ctl = ab.JoinSearchController(relay, state, b0=b0, join_max=join_max,
                                  join_bound_ms=1000, trial_gap_s=0.003, base_s=0.005,
                                  interval_s=0.001, latency_threshold_ms=500,
                                  duration_s=duration_s)
    ctl.RESET_TIMEOUT_S = 0.05
    ctl._emit = lambda sig, action, force=False: None
    return ctl, state


def _upto(n):
    return lambda b, i: b <= n


async def test_grows_by_half_then_bisects_then_confirms_three_times():
    relay = FakeRelay(ok=_upto(20))
    ctl, state = _ctl(relay)
    await ctl.run()
    assert relay.tried == [4, 6, 9, 14, 21, 17, 19, 19, 19, 19]
    assert (ctl.burst, ctl.confirmed, ctl.cap_flag) == (19, 3, '')
    assert state.ceiling_reason == 'join burst of 19 confirmed'


async def test_grows_by_a_fifth_near_the_bound():
    relay = FakeRelay(ok=_upto(15), first_ms=lambda b: 600.0)
    ctl, _ = _ctl(relay, b0=10)
    await ctl.run()
    assert relay.tried[:4] == [10, 12, 15, 18]
    assert ctl.burst == 15


async def test_a_failed_confirmation_steps_down():
    relay = FakeRelay(ok=lambda b, i: b <= 20 and not (b == 19 and relay.tried.count(19) >= 3))
    ctl, _ = _ctl(relay)
    await ctl.run()
    assert relay.tried[-6:] == [19, 19, 19, 17, 17, 17]
    assert ctl.burst == 17


async def test_confirmation_step_downs_are_bounded(capsys):
    relay = FakeRelay(ok=lambda b, i: b <= 20 and i < 7)
    ctl, state = _ctl(relay)
    await ctl.run()
    assert ctl.burst is None
    assert ctl.not_demonstrated == 'confirmation failed after 3 step(s) down'
    ab._print_summary(state, ctl, 5, None)
    assert ('  Join burst:  not demonstrated (confirmation failed after 3 step(s) down)\n'
            in capsys.readouterr().out)


async def test_a_failing_first_burst_halves_down():
    relay = FakeRelay(ok=_upto(3))
    ctl, _ = _ctl(relay, b0=16)
    await ctl.run()
    assert relay.tried == [16, 8, 4, 2, 3, 3, 3, 3]
    assert ctl.burst == 3


async def test_failing_down_to_one_is_not_demonstrated():
    relay = FakeRelay(ok=lambda b, i: False)
    ctl, _ = _ctl(relay, b0=4)
    await ctl.run()
    assert relay.tried == [4, 2, 1]
    assert ctl.not_demonstrated == 'B=1 failed: joins late or failed'


@pytest.mark.parametrize('join_max, pool, flag', [(10, 1000, ' (at --join-max)'),
                                                  (None, 7, ' (pool-limited)')])
async def test_passing_at_a_cap_confirms_there_flagged(capsys, join_max, pool, flag):
    relay = FakeRelay(pool=pool)
    ctl, state = _ctl(relay, join_max=join_max)
    await ctl.run()
    cap = min(pool, join_max or pool)
    assert relay.tried[-3:] == [cap] * 3 and ctl.burst == cap
    ab._print_summary(state, ctl, 5, None)
    assert (f'  Join burst:  {cap} subscribers at once (confirmed 3x){flag}\n'
            in capsys.readouterr().out)


async def test_a_watcher_fault_fails_the_burst():
    relay = FakeRelay(p90=lambda b, ticks: 600.0 if b >= 9 and ticks == 1 else 20.0)
    ctl, _ = _ctl(relay, join_max=12)
    await ctl.run()
    nine = next(t for t in ctl.trials if t.b == 9)
    assert nine.fault == 'p90 over' and not nine.passed and nine.ok == 9
    assert ctl.burst == 8


def test_late_joins_count_against_the_share():
    trial = ab.JoinTrial(100, 1000.0)
    trial.judge([(5.0, 200.0)] * 99 + [(5.0, 1200.0)], 0.99)
    assert trial.passed and trial.ok == 99 and trial.burst_ms == 200.0
    trial = ab.JoinTrial(100, 1000.0)
    trial.judge([(5.0, 200.0)] * 98 + [(5.0, 1200.0)] * 2, 0.99)
    assert not trial.passed and trial.fault == 'joins late or failed'
    trial = ab.JoinTrial(100, 1000.0)
    trial.judge([(5.0, 200.0)] * 98, 0.99)
    assert not trial.passed, 'a join that never got there counts against it'


async def test_a_relay_that_does_not_recover_ends_the_search(capsys):
    relay = FakeRelay(p90=lambda b, ticks: 600.0 if b >= 9 else 20.0)
    ctl, state = _ctl(relay)
    await ctl.run()
    assert relay.tried == [4, 6, 9]
    assert ctl.not_demonstrated == 'relay did not recover after B=9'
    ab._print_summary(state, ctl, 5, None)
    assert ('  Join burst:  not demonstrated (relay did not recover after B=9)\n'
            in capsys.readouterr().out)


@pytest.mark.parametrize('how', ['duration', 'stop'])
async def test_an_open_burst_at_the_end_never_passes(capsys, how):
    relay = FakeRelay(resolves=False)
    ctl, state = _ctl(relay, duration_s=0.05 if how == 'duration' else 30)
    if how == 'stop':
        asyncio.get_running_loop().call_later(0.05, state.stop.set)
    await ctl.run()
    assert len(ctl.trials) == 1 and not ctl.trials[0].passed
    assert ctl.trials[0].fault == 'run ended' and ctl.burst is None
    ab._print_summary(state, ctl, 5, None)
    out = capsys.readouterr().out
    assert '  Join trial:  B=4 failed (run ended), 0/4 joined within 1000 ms,' in out
    assert '  Join burst:  not demonstrated (run ended)\n' in out
    assert 'passed' not in out


async def test_a_base_audience_that_does_not_hold_is_not_demonstrated(capsys):
    relay = FakeRelay(p90=lambda b, ticks: 600.0)
    ctl, state = _ctl(relay)
    await ctl.run()
    assert relay.tried == [] and relay.level == 20, 'the base is the max level'
    assert state.ceiling_reason == 'not demonstrated: base audience did not hold (p90 over)'
    ab._print_summary(state, ctl, 5, None)
    out = capsys.readouterr().out
    assert '  Join burst:  not demonstrated (base audience did not hold: p90 over)\n' in out
    assert 'Join trial' not in out and 'Sustained' not in out


def test_summary_lines(capsys):
    ctl, state = _ctl(FakeRelay())
    passed = ab.JoinTrial(20, 1000.0)
    passed.judge([(4.0, 8.0)] * 10 + [(6.0, 12.0)] * 10, 0.99)
    failed = ab.JoinTrial(30, 1000.0, fault='p90 over')
    failed.judge([(4.0, 8.0)] * 30, 0.99)
    ctl.trials, ctl.burst, ctl.confirmed = [passed, failed], 20, 3
    ab._print_summary(state, ctl, 5, None)
    lines = capsys.readouterr().out.splitlines()
    assert ('  Join trial:  B=20 passed, 20/20 joined within 1000 ms, SUBSCRIBE_OK p50/p99 '
            '4/6 ms, first object p50/p99 8/12 ms, burst 12 ms') in lines
    assert ('  Join trial:  B=30 failed (p90 over), 30/30 joined within 1000 ms, SUBSCRIBE_OK '
            'p50/p99 4/4 ms, first object p50/p99 8/8 ms, burst 8 ms') in lines
    assert '  Join burst:  20 subscribers at once (confirmed 3x)' in lines
    assert not any('High-water' in x or 'Sustained' in x for x in lines)


def test_percentile_is_nearest_rank():
    assert ab._pctl([], 99) == 0.0
    assert ab._pctl([3.0, 1.0, 2.0], 50) == 2.0
    assert ab._pctl(list(range(1, 101)), 99) == 99
    assert ab._pctl(list(range(1, 101)), 100) == 100


@pytest.mark.parametrize('extra, why', [
    (['--join-burst', '5'], 'subs-mode only'),
    (['--join-burst', '5', '--mode', 'subs', '--hold', '30'], 'drop --hold'),
    (['--join-burst', '5', '--mode', 'subs', '--exit', 'max'], 'drop --exit'),
    (['--join-burst', '5', '--mode', 'subs', '--exit', 'sustained'], 'drop --exit'),
    (['--join-burst', '5', '--mode', 'subs', '--too-far-behind', '2'],
     'drop --too-far-behind'),
    (['--join-burst', '0', '--mode', 'subs'], 'at least 1'),
    (['--join-burst', '5', '--join-max', '4', '--mode', 'subs'], 'at least --join-burst'),
    (['--join-burst', '5', '--mode', 'subs', '--trial-gap', '0'], 'positive'),
    (['--join-burst', '5', '--mode', 'subs', '--watchers', '1'], 'between 0 and 1'),
    (['--join-steps', '5,10', '--mode', 'subs'], 'unrecognized arguments'),
])
def test_join_burst_refuses(monkeypatch, capsys, extra, why):
    monkeypatch.setattr(sys, 'argv', ['moq-adaptive-bench', 'moqt://127.0.0.1:4433', *extra])
    with pytest.raises(SystemExit):
        ab.parse_args()
    assert why in capsys.readouterr().err


def test_the_banner_says_start_subs_is_ignored(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['moq-adaptive-bench', 'moqt://127.0.0.1:4433',
                                      '--mode', 'subs', '--join-burst', '5',
                                      '--join-max', '60', '--max-subs', '80',
                                      '--start-subs', '7'])
    ab._print_banner(ab.parse_args())
    assert ('  join search:        80 subs (--start-subs ignored) held 30 s, then bursts '
            'from B=5 to B=60, bound 1000 ms, gap 5 s, 25% watchers'
            in capsys.readouterr().out)


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


class _Array(list):
    def __init__(self, typ, n):
        super().__init__([0.0] * n)
        self._lock = threading.Lock()

    def get_lock(self):
        return self._lock


def _actuator(monkeypatch, step_subs=1, max_subs=4):
    monkeypatch.setattr(workers, 'MP_CTX', SimpleNamespace(
        Queue=_Queue, Event=threading.Event, Process=_Process, Array=_Array))
    return ab.SubsActuator('moqt://127.0.0.1:4433', 'ns', 'track', None, True,
                           force_quic=False, min_subs=1, max_subs=max_subs, start_subs=1,
                           step_subs=step_subs, object_size=1000, per_sub_mbps=8.0,
                           interval_s=1.0, stagger=0.0, join_burst=True)


def _report(wid=0, **extra):
    return {'kind': 'batch_stats', 'worker_id': wid, 'batch_size': 2, 'active': 2,
            'delivering': 2, 't': time.monotonic(), 'dt': 1.0, 'rx_bytes': 2_000_000,
            'rx_objs': 30, 'loss': 0, 'lat_n': 0, 'lat_p90_ms': 0.0, 'lat_mean_ms': 0.0,
            'jitter_ms': 0.0, 'total_objs': 30, **extra}


def test_a_burst_is_apportioned_by_eligible_joiners():
    assert ab._apportion(10, {0: 15, 1: 5}) == {0: 8, 1: 2}
    assert ab._apportion(7, {0: 3, 1: 3, 2: 3}) == {0: 3, 1: 2, 2: 2}
    assert ab._apportion(50, {0: 4, 1: 1}) == {0: 4, 1: 1}
    assert ab._apportion(5, {0: 0}) == {}


async def test_a_trial_reaches_every_worker_and_collects_its_joins(monkeypatch):
    act = _actuator(monkeypatch, step_subs=4, max_subs=8)
    await act.apply(4)
    await act.apply(8)
    assert all(p.cfg['watchers'] == 0.25 and p.cfg['trial'] is act._trial_arrays[w]
               for w, (p, _) in act._workers.items())
    act._latest_batch = {0: {'eligible': 3}, 1: {'eligible': 1}}
    assert act.eligible_total == 4
    t = act.start_trial(4)
    assert [list(act._trial_arrays[w]) for w in (0, 1)] == [[t, 3], [t, 1]]
    act._events_q.put(_report(0, trial_picked=[(t, 3)], trial_joins=[(t, 5.0, 9.0)] * 2,
                              trial_failed=[t]))
    act._events_q.put(_report(1, trial_picked=[(t - 1, 2)], trial_joins=[(t - 1, 1.0, 1.0)]))
    act._drain_events()
    view = act.trial_view()
    assert (view['picked'], view['joins'], view['failed']) == (3, [(5.0, 9.0)] * 2, 1)
    assert not view['resolved'], 'worker 1 has not picked'
    act._events_q.put(_report(1, trial_picked=[(t, 1)], trial_joins=[(t, 6.0, 10.0)]))
    act._drain_events()
    assert act.trial_view()['resolved']
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
    state = _slot()
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


def test_a_burst_picks_eligible_joiners_only():
    rng = random.Random(1)
    states = [_slot(active=True, joined_at=0.0), _joiner(active=False),
              _joiner(join_t0=5.0), _joiner(), _joiner(), _joiner()]
    assert workers.pick_burst(states, 2, 7.0, 10.0, rng) == 2
    picked = [s for s in states if s['cycle']]
    assert len(picked) == 2 and all(s['trial_t'] == 7.0 and s['wake'].is_set() for s in picked)
    assert workers.pick_burst(states, 5, 8.0, 10.0, rng) == 1, 'a watcher is never cycled'
    assert workers.pick_burst(states, 5, 9.0, 10.0, rng) == 0


def test_a_joiner_is_eligible_after_its_first_object_and_the_dwell():
    state = _joiner(joined_at=None)
    workers.join_begin(state, 100.0)
    workers.join_mark(state, 'join_sub_ms', 100.1)
    assert not workers.eligible(state, 105.0), 'no first object yet'
    workers.join_first_object(state, 100.2)
    assert not workers.eligible(state, 100.2 + workers.JOINER_DWELL_S - 0.01)
    assert workers.eligible(state, 100.2 + workers.JOINER_DWELL_S)


def test_trial_joins_are_timed_from_the_trial_start():
    ok, late = _joiner(trial_t=99.5), _joiner(trial_t=99.5)
    ok['tally'] = late['tally']
    for s in (ok, late):
        workers.join_begin(s, 100.0)
        assert s['trial_t'] == 99.5, 'the redial keeps its trial'
    workers.join_mark(ok, 'join_sub_ms', 100.1)
    workers.join_first_object(ok, 100.3)
    workers.join_fail(late)
    tally = ok['tally']
    assert tally['trial_joins'] == [(99.5, pytest.approx(600), pytest.approx(800))]
    assert tally['trial_failed'] == [99.5]
    assert ok['trial_t'] is None and late['trial_t'] is None



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




@pytest.mark.parametrize('trial_t, judged, lossy', [(0.0, 4, 3), (50.0, 1, 0)])
async def test_health_is_judged_on_watchers_once_a_trial_starts(
        monkeypatch, trial_t, judged, lossy):
    async def supervisor(config, relay, stop_ev, stats, state, delay, shared):
        state['active'], state['joined_at'] = True, -10.0
        _feed(stats, (0, 1, 2, 3, 4, 5) if state['watcher'] else (0, 1, 3, 4, 5))
        await stop_ev.wait()

    monkeypatch.setattr(workers, '_slot_supervisor', supervisor)
    monkeypatch.setattr(workers, 'SharedTransport',
                        lambda **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(workers, 'STATS_INTERVAL_S', 0.05)
    trial = _Array('d', 2)
    trial[0], trial[1] = trial_t, 2
    stop, reports = threading.Event(), queue.Queue()
    task = asyncio.create_task(workers._subscriber_batch_task(
        dict(worker_id=0, batch_size=4, relay_url='moqt://127.0.0.1:4433', watchers=0.25,
             trial=trial), stop, reports))
    seen = []
    while not any(r['trial_picked'] for r in seen) and len(seen) < 3:
        seen.append(await asyncio.to_thread(reports.get, timeout=5))
    stop.set()
    await task
    report = seen[0]
    assert (report['judged'], report['lossy'], report['active']) == (judged, lossy, judged)
    assert report['loss'] == 8 * lossy
    assert report['total_objs'] == 4 * 40 + 8
    picked = [p for r in seen for p in r['trial_picked']]
    assert picked == ([(50.0, 2)] if trial_t else []), 'two of the three joiners'


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

        async def wait_closed(self):
            await asyncio.Event().wait()

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

