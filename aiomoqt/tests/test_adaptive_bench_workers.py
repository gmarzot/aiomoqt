"""CLI outcomes and transport settings survive the worker boundary."""
import asyncio
import csv
import queue
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aiomoqt.tools import adaptive_bench as ab
from aiomoqt.types import MOQTRequestError
from aiomoqt.utils import workers


class _Queue(queue.Queue):
    def cancel_join_thread(self):
        pass

    def close(self):
        pass


def _processes(monkeypatch, *, receive=True, fixed=False, published=True):
    configs = []

    class Process:
        def __init__(self, target, args, **kwargs):
            self.target = target
            self.args = args
            self.alive = False
            self.exitcode = None
            configs.append((target.__name__, args[0]))

        def start(self):
            self.alive = not fixed
            self.exitcode = 0 if fixed else None
            events = self.args[-1]
            cfg = self.args[0]
            if self.target.__name__ in ('pub_worker_entry', 'loopback_server_entry'):
                if fixed and not published:
                    events.put({'kind': 'pub_stats', 'cumulative_objs': 1})
                events.put({'kind': 'pub_health',
                            'state': 'published' if published else 'failed'})
            elif receive:
                batch = self.target.__name__ == 'sub_batch_worker_entry'
                events.put({'kind': 'batch_stats' if batch else 'stats',
                            'worker_id': cfg.get('worker_id', 0),
                            'active': 1, 't': time.monotonic(),
                            'rx_bytes': 2_000_000, 'rx_objs': 1,
                            'total_objs': 1})

        def is_alive(self):
            return self.alive

        def join(self, **kwargs):
            self.alive = False
            self.exitcode = 0

        def terminate(self):
            self.alive = False
            self.exitcode = -15

    monkeypatch.setattr(workers, 'MP_CTX', SimpleNamespace(
        Queue=_Queue, Event=threading.Event, Process=Process))
    return configs


def _argv(monkeypatch, *extra):
    monkeypatch.setattr(sys, 'argv', [
        'moq-adaptive-bench', 'moqt://127.0.0.1:4433',
        '--interval', '0.001', '--start-mbps', '1', '--max-mbps', '1',
        '--max-subs', '1', '-j', '0.001', '--duration', '1', *extra,
    ])


@pytest.mark.parametrize('receive, expected', [(False, 1), (True, 0)])
async def test_main_exit_status_requires_received_data(monkeypatch, capsys, receive, expected):
    _processes(monkeypatch, receive=receive)
    _argv(monkeypatch, *([] if receive else ['--duration', '30']))
    assert await ab.main() == expected
    output = capsys.readouterr().out
    if not receive:
        assert 'ABORT: rx=0' in output
    else:
        assert 'duration cap reached' in output


async def test_exit_max_ends_the_run(monkeypatch, capsys):
    _processes(monkeypatch)
    _argv(monkeypatch, '--exit', 'max', '--duration', '30')
    assert await ab.main() == 0
    assert 'reached max' in capsys.readouterr().out


@pytest.mark.parametrize('signal, reason', [
    (dict(latency_mean_ms=900.0, latency_p90_ms=900.0, shortfall=False), 'p90 over threshold'),
    (dict(latency_mean_ms=1.0, latency_p90_ms=1.0, shortfall=True), 'delivery short of target'),
])
async def test_exit_max_ends_the_run_at_the_first_back_off(capsys, signal, reason):
    state = ab.BenchState()
    ticks = [0]

    async def observe():
        ticks[0] += 1
        if ticks[0] >= 50:
            state.stop.set()
        return ab.Signal(t=time.monotonic(), level=10, loss_pct=0, target_mbps=10,
                         tx_mbps=10, rx_mbps=10, **signal)

    actuator = SimpleNamespace(initial_level=10, unit='subs', min_level=1, max_level=20,
                               initial_step=1, step_floor=1, apply=AsyncMock(), observe=observe)
    controller = ab.AIMDController(actuator, state, interval_s=0.001,
                                   latency_threshold_ms=500.0, exit_at_max=True)
    await controller.run()
    assert state.ceiling_reason == reason and ticks[0] < 50
    assert f'ceiling ({reason})' in capsys.readouterr().out


@pytest.mark.parametrize('extra, why', [
    (['--exit', 'max', '--mode', 'subs', '--role', 'pub'], 'no max to reach'),
    (['-t', '-1'], 'seconds, or 0'),
])
def test_refuses(monkeypatch, capsys, extra, why):
    _argv(monkeypatch, *extra)
    with pytest.raises(SystemExit):
        ab.parse_args()
    assert why in capsys.readouterr().err


async def test_duration_cap_without_measurements_fails(monkeypatch):
    _processes(monkeypatch, receive=False)
    _argv(monkeypatch, '--duration', '0.000001')
    assert await ab.main() == 1


async def test_no_data_abort_stays_failed_after_earlier_delivery():
    state = ab.BenchState()
    signals = [ab.Signal(t=time.monotonic(), level=1, latency_mean_ms=0,
                         latency_p90_ms=0, loss_pct=0, shortfall=False,
                         target_mbps=1, tx_mbps=1, rx_mbps=rx)
               for rx in (1, 0, 0, 0)]
    actuator = SimpleNamespace(initial_level=1, unit='Mbps',
                               apply=AsyncMock(), observe=AsyncMock(side_effect=signals))
    controller = ab.AIMDController(actuator, state, interval_s=0.001)
    await controller.run()
    assert state.received_data and state.failed
    assert 'no data received' in state.ceiling_reason


@pytest.mark.parametrize('published, expected', [(False, 1), (True, 0)])
async def test_fixed_publisher_requires_confirmation(monkeypatch, published, expected):
    _processes(monkeypatch, fixed=True, published=published)
    _argv(monkeypatch, '--mode', 'subs', '--role', 'pub')
    assert await ab.main() == expected


@pytest.mark.parametrize('mode, role', [('bw', 'both'), ('subs', 'both'),
                                       ('subs', 'sub'), ('subs', 'pub'),
                                       ('mp', 'both')])
@pytest.mark.parametrize('budget', [0, 123456])
async def test_cli_passes_transport_settings_to_every_worker(monkeypatch, mode, role, budget):
    configs = _processes(monkeypatch, fixed=role == 'pub')
    _argv(monkeypatch, '--mode', 'bw' if mode == 'mp' else mode,
          '--role', role, '--cc-algo', 'cubic', '--cc-options', 'Q0.02',
          '--pacing-trains', '--fc-window', '33554432',
          '--max-queued-bytes', str(budget), '--max-inflight-bytes', str(budget),
          '--keepalive', '2.5')
    if mode == 'mp':
        sys.argv.remove('moqt://127.0.0.1:4433')
        sys.argv.extend(['--mp', '--cert', 'certs/cert.pem', '--key', 'certs/key.pem'])
    assert await ab.main() == 0
    assert configs
    for _, cfg in configs:
        assert cfg['congestion_control_algorithm'] == 'cubic'
        assert cfg['congestion_control_options'] == 'Q0.02'
        assert cfg['packet_train_mode'] is True
        assert cfg['flow_control_window'] == 33554432
        assert cfg['tx_max_queued_bytes'] == budget
        assert cfg['tx_max_inflight_bytes'] == (budget or None)
        assert cfg['keep_alive_interval'] == 2.5


class _Constructed(BaseException):
    """Stop at the transport constructor, before opening any socket."""


@pytest.mark.parametrize('kind', ['publisher', 'subscriber', 'batch', 'loopback'])
@pytest.mark.parametrize('budget', [None, 123456])
async def test_worker_applies_transport_settings(monkeypatch, kind, budget):
    captured = {}

    def capture(*args, **kwargs):
        captured.update(kwargs)
        raise _Constructed

    monkeypatch.setattr(workers, 'MOQTClient', capture)
    from aiomoqt import server
    monkeypatch.setattr(server, 'MOQTServer', capture)
    monkeypatch.setattr(workers, '_bridge_stop_event', lambda _: asyncio.Event())
    cfg = dict(relay_url='moqt://127.0.0.1:4433', sub_id=0,
               namespace='ns', trackname='track', port=4433,
               cert='test-cert', key='test-key',
               congestion_control_algorithm='cubic',
               congestion_control_options='Q0.02', packet_train_mode=True,
               flow_control_window=33554432, tx_max_queued_bytes=0,
               tx_max_inflight_bytes=budget, keep_alive_interval=2.5)
    with pytest.raises(_Constructed):
        if kind == 'publisher':
            await workers._publisher_task(cfg, None, None, None)
        elif kind == 'subscriber':
            await workers._subscriber_task(cfg, None, None)
        elif kind == 'batch':
            relay = SimpleNamespace(host='127.0.0.1', port=4433, path='', use_quic=True)
            await workers._slot_supervisor(cfg, relay, asyncio.Event(), None, {}, 0)
        else:
            await workers._loopback_server_task(cfg, None, None, None)
    assert captured['congestion_control_algorithm'] == 'cubic'
    assert captured['congestion_control_options'] == 'Q0.02'
    assert captured['packet_train_mode'] is True
    assert captured['flow_control_window'] == 33554432
    assert captured['tx_max_queued_bytes'] == 0
    assert captured['tx_max_inflight_bytes'] is budget
    assert captured['keep_alive_interval'] == 2.5


class _DryingQueue(_Queue):
    """Reads Empty between bursts, as mp.Queue does when its pipe runs dry."""

    def __init__(self, burst):
        super().__init__()
        self.burst = burst
        self.left = burst

    def get_nowait(self):
        if self.left == 0:
            self.left = self.burst
            raise queue.Empty
        self.left -= 1
        return super().get_nowait()


def _subs_actuator(monkeypatch, mbps=16.0):
    _processes(monkeypatch, receive=False)
    return ab.SubsActuator('moqt://127.0.0.1:4433', 'ns', 'track', None, True,
                           force_quic=False, min_subs=1, max_subs=4, start_subs=1,
                           step_subs=1, object_size=1000, per_sub_mbps=mbps,
                           interval_s=1.0, stagger=0.0)


def _report(rx_mbps=16.0, p90=None, **extra):
    return {'kind': 'batch_stats', 'worker_id': 0, 'batch_size': 1, 'active': 1,
            'delivering': 1, 't': time.monotonic(), 'dt': 1.0,
            'rx_bytes': int(rx_mbps * 1e6 / 8), 'rx_objs': 30, 'loss': 0,
            'lat_n': 0 if p90 is None else 30, 'lat_p90_ms': p90 or 0.0,
            'lat_mean_ms': p90 or 0.0, 'jitter_ms': 0.0, 'total_objs': 30, **extra}


async def _mature_worker(act):
    await act.apply(1)
    act._spawned_t[0] -= 100.0
    return act._events_q


async def test_reports_are_read_between_ticks_past_a_dry_pipe(monkeypatch):
    act = _subs_actuator(monkeypatch)
    act._events_q = _DryingQueue(burst=5)
    await _mature_worker(act)
    for i in range(20):
        act._events_q.put(_report(seq=i))
    deadline = time.monotonic() + 5.0
    while act._events_q.qsize() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert act._events_q.qsize() == 0
    assert act._latest_batch[0]['seq'] == 19
    await act.shutdown()


async def test_rx_covers_the_tick_on_a_two_second_gop(monkeypatch):
    act = _subs_actuator(monkeypatch)
    events = await _mature_worker(act)
    for _ in range(3):
        # The second without the I-frame last: under the shortfall ratio alone.
        events.put(_report(rx_mbps=18.6))
        events.put(_report(rx_mbps=13.4))
        sig = await act.observe()
        assert sig.rx_mbps == pytest.approx(16.0, abs=0.01)
        assert not sig.shortfall
    await act.shutdown()


async def test_controller_acts_on_the_worst_p90_of_the_tick(monkeypatch):
    act = _subs_actuator(monkeypatch)
    events = await _mature_worker(act)
    events.put(_report(p90=900.0))
    events.put(_report(p90=100.0))
    sig = await act.observe()
    assert (sig.latency_p90_ms, sig.latest_p90_ms) == (900.0, 100.0)
    events.put(_report(p90=100.0))
    sig = await act.observe()
    assert (sig.latency_p90_ms, sig.latest_p90_ms) == (100.0, 100.0)
    await act.shutdown()


async def test_a_young_workers_latency_is_not_counted(monkeypatch):
    act = _subs_actuator(monkeypatch)
    await act.apply(1)
    act._events_q.put(_report(p90=5000.0))
    act._drain_events()
    act._spawned_t[0] -= 100.0
    act._events_q.put(_report(p90=100.0))
    sig = await act.observe()
    assert sig.latency_p90_ms == 100.0
    await act.shutdown()


async def test_a_tick_without_a_report_keeps_the_last_figures(monkeypatch):
    act = _subs_actuator(monkeypatch)
    events = await _mature_worker(act)
    events.put(_report(rx_mbps=16.0, p90=100.0))
    await act.observe()
    sig = await act.observe()
    assert sig.rx_mbps == pytest.approx(16.0, abs=0.01)
    assert sig.latency_p90_ms == 100.0 and sig.latency_known
    await act.shutdown()


async def test_a_stale_report_warns_once(monkeypatch, capsys):
    act = _subs_actuator(monkeypatch)
    events = await _mature_worker(act)
    events.put(_report(t=time.monotonic() - 30.0))
    await act.observe()
    assert 'worker report 30s old' in capsys.readouterr().out
    events.put(_report())
    await act.observe()
    assert 'worker report' not in capsys.readouterr().out
    await act.shutdown()


@pytest.mark.parametrize('mode, has_tick', [('subs', True), ('bw', False)])
async def test_report_csv_keeps_its_columns(monkeypatch, tmp_path, mode, has_tick):
    _processes(monkeypatch)
    path = tmp_path / 'report.csv'
    _argv(monkeypatch, '--mode', mode, '--report', str(path))
    assert await ab.main() == 0
    with path.open(newline='') as f:
        rows = list(csv.DictReader(f))
    assert rows
    for row in rows:
        assert None not in row and None not in row.values()
        assert ('tick_p90_ms' in row) is has_tick
        assert ('worst_p90_ms' if has_tick else 'win_p90_ms') in row


class _Session:
    async def client_session_init(self):
        pass

    def close(self):
        pass


class _Client:
    connects = 0

    def __init__(self, *args, **kwargs):
        pass

    def connect(self):
        _Client.connects += 1
        return self

    async def __aenter__(self):
        return _Session()

    async def __aexit__(self, *exc):
        return False


def _slot_state():
    return {'active': False, 'since': 0.0, 'hist': None, 'refused': 0,
            'refusing': False, 'refused_reason': None}


async def test_slot_counts_a_refusal_until_it_subscribes(monkeypatch):
    stop = asyncio.Event()
    state = _slot_state()
    seen = []

    class Track:
        calls = 0
        state = workers.TrackState.CLOSED

        def __init__(self, *args, **kwargs):
            pass

        async def subscribe(self, **kwargs):
            Track.calls += 1
            if Track.calls == 1:
                raise MOQTRequestError(9, 'relay overloaded', retry_interval=201)
            seen.append(dict(state))
            stop.set()

    monkeypatch.setattr(workers, 'MOQTClient', _Client)
    monkeypatch.setattr(workers, 'SubscribedTrack', Track)
    monkeypatch.setattr(workers, 'SELF_HEAL_BACKOFF_S', 0.0)
    monkeypatch.setattr(_Client, 'connects', 0)
    relay = SimpleNamespace(host='127.0.0.1', port=4433, path='', use_quic=True)
    t0 = time.monotonic()
    await workers._slot_supervisor(dict(namespace='ns', trackname='t'), relay,
                                   stop, None, state, 0)
    assert time.monotonic() - t0 >= 0.19
    assert _Client.connects == 1
    assert seen[0]['refusing'] and seen[0]['refused'] == 1
    assert seen[0]['refused_reason'] == 'relay overloaded'
    assert state['refused'] == 1 and not state['refusing']


async def test_a_track_not_yet_published_is_not_a_refusal(monkeypatch):
    stop = asyncio.Event()
    state = _slot_state()

    class Track:
        calls = 0
        state = workers.TrackState.CLOSED

        def __init__(self, *args, **kwargs):
            pass

        async def subscribe(self, **kwargs):
            Track.calls += 1
            if Track.calls == 1:
                raise MOQTRequestError(4, 'does not exist')
            stop.set()

    real_sleep = asyncio.sleep
    monkeypatch.setattr(workers, 'MOQTClient', _Client)
    monkeypatch.setattr(workers, 'SubscribedTrack', Track)
    monkeypatch.setattr(workers.asyncio, 'sleep', lambda _s: real_sleep(0))
    relay = SimpleNamespace(host='127.0.0.1', port=4433, path='', use_quic=True)
    await workers._slot_supervisor(dict(namespace='ns', trackname='t'), relay,
                                   stop, None, state, 0)
    assert state['refused'] == 0 and not state['refusing']


async def test_actuator_totals_refusals_past_a_reaped_worker(monkeypatch):
    act = _subs_actuator(monkeypatch)
    events = await _mature_worker(act)
    events.put(_report(refused=5, refusing=3, refused_reason='relay overloaded'))
    sig = await act.observe()
    assert sig.refusing_subs == 3
    act._kill_worker(0)
    sig = await act.observe()
    assert sig.refusing_subs == 0
    assert act.refused_total == 5 and act.refused_reason == 'relay overloaded'
    await act.shutdown()


@pytest.mark.parametrize('refusing, onset, sustained', [
    (3, 'hold (relay refusing)', 'hold (relay refusing)'),
    (0, 'hold (watch)', 'hold (shortfall)'),
])
async def test_a_refused_shortfall_says_so(monkeypatch, capsys, refusing, onset, sustained):
    state = ab.BenchState()
    ticks = [0]

    async def observe():
        ticks[0] += 1
        if ticks[0] >= 8:
            state.stop.set()
        return ab.Signal(t=time.monotonic(), level=10, latency_mean_ms=1.0,
                         latency_p90_ms=1.0, loss_pct=0, shortfall=True,
                         target_mbps=10, tx_mbps=10, rx_mbps=10,
                         refusing_subs=refusing)

    actuator = SimpleNamespace(initial_level=10, unit='subs', min_level=1,
                               max_level=20, initial_step=1, step_floor=1,
                               apply=AsyncMock(), observe=observe)
    controller = ab.AIMDController(actuator, state, interval_s=0.001)
    await controller.run()
    out = capsys.readouterr().out
    assert onset in out and sustained in out
    assert ('hold (shortfall)' in out) is (not refusing)


async def test_retry_wait_honours_the_interval():
    stop = threading.Event()
    assert not await workers.retry_wait(MOQTRequestError(9, 'x', retry_interval=0), stop)
    assert not await workers.retry_wait(ValueError('x'), stop)
    t0 = time.monotonic()
    assert await workers.retry_wait(MOQTRequestError(9, 'x', retry_interval=1), stop)
    assert time.monotonic() - t0 < 0.05
    t0 = time.monotonic()
    assert await workers.retry_wait(MOQTRequestError(9, 'x', retry_interval=201), stop)
    assert time.monotonic() - t0 >= 0.19


async def test_retry_wait_stops_early():
    stop = threading.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)
    t0 = time.monotonic()
    assert await workers.retry_wait(MOQTRequestError(9, 'x', retry_interval=30000), stop)
    assert time.monotonic() - t0 < 2.0


@pytest.mark.parametrize('refusing, reaped', [(2, False), (0, True)])
async def test_only_a_silent_worker_is_reaped_as_starved(monkeypatch, refusing, reaped):
    act = _subs_actuator(monkeypatch)
    events = await _mature_worker(act)
    events.put(_report(rx_mbps=0, active=0, refused=refusing, refusing=refusing,
                       refused_reason='relay overloaded' if refusing else None))
    sig = await act.observe()
    assert (0 not in act._workers) is reaped
    assert (sig.failed_subs > 0) is reaped
    assert sig.refusing_subs == (0 if reaped else refusing)
    await act.shutdown()


def test_summary_names_the_refusals(capsys):
    state = ab.BenchState()
    actuator = SimpleNamespace(unit='subs', max_level=20, refused_total=7,
                               refused_reason='relay overloaded')
    controller = SimpleNamespace(actuator=actuator, high_water=10.0, pivots=[],
                                 held_at_max=False, eq_level=0.0, wanted_mbps=0.0,
                                 too_far_behind=False)
    ab._print_summary(state, controller, 1, None)
    assert '7 subscribes by the relay (relay overloaded)' in capsys.readouterr().out


# -- a slot that lost objects or runs late is not delivering --------------

def _feed(stats, groups, size=8):
    for g in groups:
        for o in range(size):
            stats.on_object(SimpleNamespace(group_id=g, object_id=o, subgroup_id=0,
                                            extensions=None), 100, 0)


@pytest.mark.parametrize('groups, lost', [((0, 1, 2, 3, 4, 5), 0),
                                          ((0, 1, 3, 4, 5), 8)])
async def test_a_batch_slot_missing_a_group_is_not_delivering(monkeypatch, groups, lost):
    async def supervisor(config, relay, stop_ev, stats, state, delay, shared):
        state['active'] = True
        _feed(stats, groups)
        await stop_ev.wait()

    monkeypatch.setattr(workers, '_slot_supervisor', supervisor)
    monkeypatch.setattr(workers, 'SharedTransport',
                        lambda **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(workers, 'STATS_INTERVAL_S', 0.01)
    stop, reports = threading.Event(), queue.Queue()
    task = asyncio.create_task(workers._subscriber_batch_task(
        dict(worker_id=0, batch_size=1, relay_url='moqt://127.0.0.1:4433'),
        stop, reports))
    report = await asyncio.to_thread(reports.get, timeout=5)
    stop.set()
    await task
    assert report['loss'] == lost and report['active'] == 1
    assert report['lossy'] == (1 if lost else 0)
    assert report['delivering'] == (0 if lost else 1)


def _judged():
    return {'since': 0.0, 'hist': None, 'lost': 0, 'lost_at': float('-inf')}


def test_a_slot_is_lossy_for_a_window_after_its_last_loss():
    state, snap = _judged(), {'loss': 0, 'lat_n': 0, 'lat_p90_ms': 0.0}
    assert workers.judge_slot(state, snap, 10.0, 3.0, 0, 0) == (True, False, False)
    snap['loss'] = 2
    assert workers.judge_slot(state, snap, 11.0, 3.0, 0, 0) == (False, True, False)
    assert workers.judge_slot(state, snap, 13.5, 3.0, 0, 0) == (False, True, False)
    assert workers.judge_slot(state, snap, 14.5, 3.0, 0, 0) == (True, False, False)


@pytest.mark.parametrize('late_ms, p90, late', [(500, 501.0, True), (500, 499.0, False),
                                                (0, 5000.0, False)])
def test_a_slot_over_the_threshold_is_not_delivering(late_ms, p90, late):
    snap = {'loss': 0, 'lat_n': 30, 'lat_p90_ms': p90}
    assert workers.judge_slot(_judged(), snap, 10.0, 3.0, 0, late_ms) == (not late, False, late)


async def test_actuator_reports_lossy_and_late_slots(monkeypatch):
    act = _subs_actuator(monkeypatch)
    events = await _mature_worker(act)
    events.put(_report(delivering=0, lossy=1, late=1, loss=8, total_objs=40))
    sig = await act.observe()
    assert (sig.lossy_subs, sig.late_subs, sig.delivering_subs) == (1, 1, 0)
    assert sig.health == 'degraded'
    act._kill_worker(0)
    assert act.lost_total == (8, 40)
    await act.shutdown()


@pytest.mark.parametrize('fields, label', [
    (dict(refusing_subs=1, lossy_subs=1), 'hold (relay refusing)'),
    (dict(lossy_subs=1, late_subs=1), 'hold (subs losing objects)'),
    (dict(late_subs=1), 'hold (subs over threshold)'),
    ({}, 'hold (shortfall)'),
])
def test_a_shortfall_hold_names_its_cause(fields, label):
    sig = ab.Signal(t=0.0, level=1, latency_mean_ms=1.0, latency_p90_ms=1.0, loss_pct=0,
                    shortfall=True, target_mbps=1, tx_mbps=1, rx_mbps=1, **fields)
    assert ab._hold_label(sig, 'hold (shortfall)') == label


def test_summary_reports_the_loss(capsys):
    state = ab.BenchState()
    actuator = SimpleNamespace(unit='subs', max_level=20, refused_total=0,
                               lost_total=(8, 392))
    controller = SimpleNamespace(actuator=actuator, high_water=10.0, pivots=[],
                                 held_at_max=False, eq_level=0.0, wanted_mbps=0.0,
                                 too_far_behind=False, peak_lossy=(2, 10),
                                 peak_late=(0, 0))
    ab._print_summary(state, controller, 1, None)
    out = capsys.readouterr().out
    assert 'Lost:        8 objects (2.00%), whole groups included' in out
    assert 'peak 2 of 10 subscribers losing objects' in out
    assert 'over the threshold' not in out


@pytest.mark.parametrize('role, extra, late_ms', [
    ('both', (), 100.0),
    ('both', ('--latency-threshold', '500'), 500.0),
    ('both', ('--too-far-behind', '2'), 2000.0),
    ('sub', (), 0.0),
])
async def test_slots_judge_latency_only_on_one_host(monkeypatch, role, extra, late_ms):
    configs = _processes(monkeypatch, fixed=role == 'pub')
    _argv(monkeypatch, '--mode', 'subs', '--role', role, *extra)
    await ab.main()
    subs = [cfg for name, cfg in configs if name == 'sub_batch_worker_entry']
    assert subs and all(cfg['slot_latency_ms'] == late_ms for cfg in subs)
