"""Viewer qualification: per-track progress, unmeasured latency, and the
batch worker's join and reaper edges."""
import asyncio
import contextlib
import csv
import io
import multiprocessing as mp
import time
from collections import deque
from types import SimpleNamespace

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._procs import free_udp_port
from aiomoqt.tools import adaptive_bench as ab
from aiomoqt.tools import load_sim
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack
from aiomoqt.types import ObjectStatus
from aiomoqt.utils import workers
from aiomoqt.utils.stats import TrackStats

_AUDIO = ('audio', 341, 46.875, False)
_RECV_US = 1_800_000_000_000_000


class _Silent(PublishedTrack):
    async def produce(self, out):
        await asyncio.Event().wait()


@requires_certs
@pytest.mark.parametrize('silent_audio', (True, False),
                         ids=('missing-audio', 'healthy'))
@pytest.mark.parametrize('raw', (True, False), ids=('quic', 'wt'))
async def test_a_viewer_missing_audio_is_not_delivering(raw, silent_audio):
    port = free_udp_port()
    ns = 'qual/av'
    stop = asyncio.Event()
    cfg = dict(namespace=ns, trackname='video', insecure=True, draft=22,
               side_tracks=[_AUDIO])
    stats = workers.new_viewer_stats(cfg)
    state = workers.new_slot_state(workers.new_join_tally())
    endpoint = SimpleNamespace(host='localhost', port=port, path='/',
                               use_quic=raw)
    expected_bps = 10000 * 100 + 341 * 46.875
    relay._announced.clear()
    relay._tracks.clear()
    server = relay._build_server('localhost', port, CERT, KEY,
                                 use_quic=raw, draft=22)
    handle = await server.serve()
    slot = None
    verdicts = []
    try:
        pub = MOQTClient('localhost', port, path='/', use_quic=raw,
                         verify_tls=False, supported_drafts=22)
        async with pub.connect() as session:
            await session.client_session_init()
            PublishedTrack(session, ns, 'video', object_size=10000, rate=100,
                           group_size=20).attach()
            (_Silent if silent_audio else PublishedTrack)(
                session, ns, 'audio', object_size=341, rate=46.875,
                group_size=5).attach()
            await session.publish_namespace(namespace=ns, wait_response=True)
            slot = asyncio.create_task(workers._slot_supervisor(
                cfg, endpoint, stop, stats, state, 0))
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and len(verdicts) < 4:
                await asyncio.sleep(0.25)
                now = time.monotonic()
                snap = stats.snapshot()
                state['hist'].append((now, snap['total_bytes']))
                verdict = workers.judge_slot(state, snap, now, 3.0,
                                             expected_bps, 0)
                if state['active'] and now - state['since'] >= 4:
                    verdicts.append(verdict)
            stop.set()
            await asyncio.wait_for(slot, 10)
    finally:
        stop.set()
        if slot is not None and not slot.done():
            slot.cancel()
            await asyncio.gather(slot, return_exceptions=True)
        handle.close()
        for track in list(relay._tracks.values()):
            track.close()
        relay._tracks.clear()
        relay._announced.clear()
    assert len(verdicts) == 4
    assert stats.tracks['video'].total_objects > 100
    if silent_audio:
        assert stats.tracks['audio'].total_objects == 0
        assert all(v == (False, False, False, True) for v in verdicts)
    else:
        assert stats.tracks['audio'].total_objects > 50
        assert all(v == (True, False, False, False) for v in verdicts)


def _progress_state():
    state = workers.new_slot_state(workers.new_join_tally())
    state.update(active=True, since=100.0)
    return state


def _snap(video, audio, catalog=None):
    tracks = {'video': (video, video * 1000), 'audio': (audio, audio * 341)}
    if catalog is not None:
        tracks['catalog'] = (catalog, catalog * 512)
    return {'loss': 0, 'lat_n': 0, 'lat_p90_ms': 0.0, 'tracks': tracks}


def test_a_track_that_stops_mid_run_starves_the_slot():
    state = _progress_state()
    out = []
    for i in range(12):
        audio = min(i, 6) * 47     # audio stops at t=106
        out.append(workers.judge_slot(state, _snap(i * 100, audio),
                                      100.0 + i, 3.0, 0, 0))
    assert [v[3] for v in out[3:9]] == [False] * 6
    # A full window (3 s) after its last object.
    assert all(v == (False, False, False, True) for v in out[9:])


def test_a_sparse_track_is_exempt():
    state = _progress_state()
    for i in range(8):
        v = workers.judge_slot(state, _snap(i * 100, i * 47, catalog=1),
                               100.0 + i, 3.0, 0, 0, sparse={'catalog'})
    assert v == (True, False, False, False)
    assert workers.starved_tracks(state, _snap(800, 376, catalog=1), 108.0,
                                  3.0) == ['catalog']


def test_no_starvation_inside_the_grace():
    state = _progress_state()
    for i in range(3):
        v = workers.judge_slot(state, _snap(0, 0), 100.0 + i, 3.0, 0, 0)
        assert not v[3]


async def test_the_actuator_reports_starved_slots(monkeypatch):
    from aiomoqt.tests.test_adaptive_bench_workers import (
        _mature_worker, _report, _subs_actuator)
    act = _subs_actuator(monkeypatch)
    events = await _mature_worker(act)
    events.put(_report(delivering=0, track_starved=1))
    await asyncio.sleep(0.3)
    sig = await act.observe()
    assert sig.starved_subs == 1
    assert ab._hold_label(sig, 'hold') == 'hold (subs missing a track)'
    await act.shutdown()


def _pub(track, fps=10.0):
    return load_sim.PubSpec({'namespace': 'ns', 'track': track,
                             'bitrate_kbps': 100, 'rate': fps}, 1.0)


def _load_group(*pubs):
    g = load_sim.GroupRT(load_sim.GroupSpec(0, {'subs': 1}, *pubs))
    shard = load_sim.ShardRT(0, 1, None, None, None)
    g.shards.append(shard)
    g.max_live = 1
    prts = {}
    for ps in pubs:
        prts[ps.key] = load_sim.PubRT(ps)
        prts[ps.key].published = True
    return g, shard, prts


def _run_load(g, shard, prts, seconds, audio_stops_at=None):
    """One live subscriber getting each track's rate each second."""
    shard.live = 1
    got = {ps.track: 0 for ps in g.spec.pubs}
    for t in range(1, seconds + 1):
        load_sim._accrue_expected({0: g}, prts, float(t), 1.0)
        for ps in g.spec.pubs:
            if ps.track != 'audio' or audio_stops_at is None or t <= audio_stops_at:
                got[ps.track] += ps.fps
        shard.stats = {'total_objs': sum(got.values()),
                       'total_bytes': sum(got.values()) * 1000,
                       'track_objs': dict(got)}
        load_sim._note_recent({0: g}, float(t), float(t))


@pytest.mark.parametrize('multi', (True, False), ids=('av', 'single'))
def test_load_sim_fails_a_track_that_stops_midway(multi):
    pubs = (_pub('video', 30.0), _pub('audio')) if multi else (_pub('audio'),)
    g, shard, prts = _load_group(*pubs)
    _run_load(g, shard, prts, 60, audio_stops_at=35)
    # Cumulatively audio got 35 of 60 s, above the 50% floor.
    assert shard.stats['track_objs']['audio'] >= 0.5 * g.expected_track['audio']
    lines, failed = load_sim._verdict({0: g}, prts)
    text = '\n'.join(lines)
    assert failed, text
    assert 'BELOW 50% OF EXPECTED over 10s' in text


def test_load_sim_passes_steady_tracks():
    g, shard, prts = _load_group(_pub('video', 30.0), _pub('audio'))
    _run_load(g, shard, prts, 60)
    lines, failed = load_sim._verdict({0: g}, prts)
    assert not failed, lines


def _offset_objects(stats, offset_us, n=100):
    for oid in range(n):
        msg = SimpleNamespace(group_id=0, object_id=oid, subgroup_id=0,
                              extensions={0x10: _RECV_US + offset_us},
                              status=0)
        stats.on_object(msg, 1000, _RECV_US + oid * 10_000)


def test_a_signed_skew_bound_keeps_offset_samples():
    default, wide = TrackStats(), TrackStats(lat_min_us=ab.SKEW_LAT_MIN_US)
    for stats in (default, wide):
        _offset_objects(stats, 5_000_000)
    assert default.snapshot()['lat_n'] == 0
    snap = wide.snapshot()
    assert snap['lat_n'] == 100 and snap['lat_p90_ms'] < -4000


def test_worker_stats_take_the_bound_from_config():
    cfg = dict(trackname='video', side_tracks=[_AUDIO], lat_min_us=-60_000_000)
    viewer = workers.new_viewer_stats(cfg)
    assert all(t.lat_min_us == -60_000_000 for t in viewer.tracks.values())
    assert workers.new_viewer_stats({'trackname': 'v'}).lat_min_us == -1_000_000


class _Actuator:
    unit = 'subs'
    min_level = 1
    max_level = 12
    initial_level = 10
    initial_step = 1
    step_floor = 1

    def __init__(self, snap):
        self.snap = snap

    async def apply(self, level):
        self.level = level

    async def observe(self):
        s, lv = self.snap, self.level
        return ab.Signal(t=time.monotonic(), level=lv,
                         latency_mean_ms=s['lat_mean_ms'],
                         latency_p90_ms=s['lat_p90_ms'], loss_pct=0,
                         shortfall=False, target_mbps=lv * 10, tx_mbps=lv * 10,
                         rx_mbps=lv * 10, target_subs=int(lv),
                         active_subs=int(lv), delivering_subs=int(lv),
                         latency_known=s['lat_n'] > 0)


async def _run_controller(snap):
    state = ab.BenchState()
    out, console = io.StringIO(), io.StringIO()
    controller = ab.AIMDController(_Actuator(snap), state, interval_s=0.001,
                                   writer=csv.writer(out),
                                   latency_threshold_ms=100, skew_safe=True,
                                   exit_at_max=True)
    with contextlib.redirect_stdout(console):
        await asyncio.wait_for(controller.run(), timeout=2)
        ab._print_summary(state, controller, controller.samples,
                          SimpleNamespace(mode='subs'))
    rows = list(csv.reader(io.StringIO(out.getvalue())))
    return state, controller, rows, console.getvalue()


async def test_missing_latency_is_reported_unmeasured():
    stats = TrackStats()
    _offset_objects(stats, 5_000_000)
    state, controller, rows, text = await _run_controller(stats.snapshot())
    assert ab.latency_unmeasured(state, controller)
    assert 'throughput only, latency unmeasured' in text
    assert 'Latency:     unmeasured' in text
    assert rows and all(row[4:6] == ['', ''] for row in rows)


async def test_skew_safe_offset_latency_is_measured():
    stats = TrackStats(lat_min_us=ab.SKEW_LAT_MIN_US)
    _offset_objects(stats, 5_000_000)
    snap = stats.snapshot()
    assert snap['lat_n'] > 0
    state, controller, rows, text = await _run_controller(snap)
    assert controller.latency_measured
    assert not ab.latency_unmeasured(state, controller)
    assert controller.high_water >= 11.0
    assert 'unmeasured' not in text
    assert all(row[5] != '' for row in rows)


def _stuck(ready):
    ready.set()
    time.sleep(600)


def _end(child):
    if child.is_alive():
        child.terminate()
    child.join(5)


async def test_a_starved_worker_is_terminated_after_the_reap(monkeypatch):
    from aiomoqt.tests.test_adaptive_bench_workers import _subs_actuator
    ctx = mp.get_context('fork')
    ready, stop = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_stuck, args=(ready,), daemon=True)
    child.start()
    assert ready.wait(5)
    act = _subs_actuator(monkeypatch)
    act._workers[0] = (child, stop)
    act._spawned_t[0] = time.monotonic() - 60
    act._latest_batch[0] = dict(active=1, total_objs=100)
    try:
        t0 = time.monotonic()
        sig = await act.observe()
        assert time.monotonic() - t0 < 0.4, 'the reap must not block'
        assert sig.failed_subs == 1 and stop.is_set()
        assert 0 not in act._workers and act._reaping == [child]
        await act.observe()
        assert not child.is_alive() and act._reaping == []
    finally:
        await act.shutdown()
        _end(child)


async def test_shutdown_ends_a_reaped_worker(monkeypatch):
    from aiomoqt.tests.test_adaptive_bench_workers import _subs_actuator
    ctx = mp.get_context('fork')
    ready = ctx.Event()
    child = ctx.Process(target=_stuck, args=(ready,), daemon=True)
    child.start()
    assert ready.wait(5)
    act = _subs_actuator(monkeypatch)
    act._reaping.append(child)
    try:
        await act.shutdown()
        assert not child.is_alive()
    finally:
        _end(child)


def _joining_state():
    state = workers.new_slot_state(workers.new_join_tally(), watcher=False)
    workers.join_begin(state, 10.0)
    workers.join_mark(state, 'join_setup_ms', 10.1)
    workers.join_mark(state, 'join_sub_ms', 10.2)
    return state


async def test_a_status_object_does_not_complete_a_join(monkeypatch):
    stop = asyncio.Event()
    cfg = dict(namespace='ns', trackname='video', side_tracks=[_AUDIO])
    stats = workers.new_viewer_stats(cfg)
    state = workers.new_slot_state(workers.new_join_tally(), watcher=False)
    tracks = {}
    seen = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def client_session_init(self):
            pass

        def close(self):
            pass

    class Track:
        def __init__(self, session, namespace, *, trackname, on_object):
            self.on_object = on_object
            tracks[trackname] = self

        async def subscribe(self, **kwargs):
            pass

        async def wait_closed(self):
            await asyncio.Event().wait()

    async def deliver(_wake, _ends=()):
        marker = SimpleNamespace(group_id=0, object_id=0, extensions=None,
                                 status=ObjectStatus.END_OF_GROUP)
        tracks['audio'].on_object(marker, 0, _RECV_US, 0, 0)
        seen.append(state['tally']['joins'])
        normal = SimpleNamespace(group_id=1, object_id=0, extensions=None,
                                 status=ObjectStatus.NORMAL)
        tracks['audio'].on_object(normal, 341, _RECV_US, 1, 0)
        seen.append(state['tally']['joins'])
        stop.set()

    monkeypatch.setattr(workers, 'MOQTClient', Client)
    monkeypatch.setattr(workers, 'SubscribedTrack', Track)
    monkeypatch.setattr(workers, '_poll_wait', deliver)
    relay_ep = SimpleNamespace(host='127.0.0.1', port=4433, path='',
                               use_quic=True)
    await workers._slot_supervisor(cfg, relay_ep, stop, stats, state, 0)
    assert seen == [0, 1]


def test_a_failed_redial_is_not_eligible():
    state = workers.new_slot_state(workers.new_join_tally(), watcher=False)
    state.update(active=True, joined_at=100.0)
    workers.join_begin(state, 110.0)
    workers.join_mark(state, 'join_setup_ms', 110.1)
    workers.join_mark(state, 'join_sub_ms', 110.2)
    workers.join_expire(state, 121.0)
    assert state['tally']['join_failed'] == 1
    assert not workers.eligible(state, 121.0)


def test_a_completed_redial_is_eligible_after_the_dwell():
    state = _joining_state()
    state['active'] = True
    workers.join_first_object(state, 10.3)
    assert state['joined_at'] == 10.3
    assert workers.eligible(state, 10.3 + workers.JOINER_DWELL_S)


def test_viewer_snapshot_keeps_per_track_counts():
    viewer = workers.new_viewer_stats(dict(trackname='video',
                                           side_tracks=[_AUDIO]))
    msg = SimpleNamespace(group_id=0, object_id=0, extensions=None)
    viewer.tracks['video'].on_object(msg, 1000, _RECV_US, 0, 0)
    assert viewer.snapshot()['tracks'] == {'video': (1, 1000), 'audio': (0, 0)}


def test_window_prunes_track_history():
    state = _progress_state()
    for i in range(30):
        workers.judge_slot(state, _snap(i * 100, i * 47), 100.0 + i, 3.0, 0, 0)
    assert all(len(h) <= 5 for h in state['track_hist'].values())
    assert isinstance(state['track_hist']['video'], deque)
