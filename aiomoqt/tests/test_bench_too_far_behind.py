"""adaptive_bench --too-far-behind: back-off on the share of viewers past the
limit, and a slot past it resubscribing on its session."""
import asyncio
import contextlib
import sys
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.tools import adaptive_bench as ab
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack
from aiomoqt.utils import workers
from aiomoqt.utils.stats import TrackStats
from aiomoqt.utils.url import parse_relay_url

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 25310
_NS = ("behind", "live")


def _controller(samples, too_far_behind=True):
    """A controller fed (worst p90, viewers behind) per tick, of 100 viewers."""
    state = ab.BenchState()
    samples = list(samples)

    async def observe():
        p90, behind = samples.pop(0) if samples else (0.0, 0)
        if not samples:
            state.stop.set()
        return ab.Signal(t=time.monotonic(), level=100, latency_mean_ms=p90,
                         latency_p90_ms=p90, loss_pct=0, shortfall=False,
                         target_mbps=100, tx_mbps=100, rx_mbps=100,
                         target_subs=100, active_subs=100, delivering_subs=100,
                         behind_subs=behind)

    actuator = SimpleNamespace(initial_level=100, unit='subs', min_level=10,
                               max_level=100, initial_step=10, step_floor=10,
                               per_sub_mbps=1.0, apply=AsyncMock(), observe=observe)
    ctl = ab.AIMDController(actuator, state, interval_s=0.001,
                            latency_threshold_ms=250,
                            too_far_behind=too_far_behind)
    actions = []
    ctl._emit = lambda sig, action, force=False: actions.append(action)
    return ctl, actions, actuator


async def test_a_lagging_minority_does_not_shrink_the_audience():
    ctl, actions, actuator = _controller([(5000.0, 1)] * 10)
    await ctl.run()
    assert not any('back-off' in a or 'watch' in a for a in actions), actions
    assert all(call.args[0] >= 100 for call in actuator.apply.await_args_list)
    assert ctl.peak_behind == (1, 100)


async def test_a_share_over_the_limit_backs_off():
    ctl, actions, actuator = _controller([(5000.0, 10)] * 10)
    await ctl.run()
    assert 'watch (too far behind)' in actions
    assert 'back-off (too far behind)' in actions
    assert any(call.args[0] < 100 for call in actuator.apply.await_args_list)


async def test_off_by_default_the_worst_viewer_still_decides():
    ctl, actions, _ = _controller([(5000.0, 1)] * 10, too_far_behind=False)
    await ctl.run()
    assert 'back-off (p90 over)' in actions


async def test_the_summary_totals_what_arrived():
    ctl, _, _ = _controller([(0.0, 0)] * 4)
    await ctl.run()
    assert ctl.wanted_mbps > 0
    assert ctl.delivered_mbps == pytest.approx(ctl.wanted_mbps)


def _slot_state(active=True):
    return {'active': active, 'since': 0.0, 'hist': deque(), 'refused': 0,
            'refusing': False, 'refused_reason': None, 'behind_since': None,
            'resubscribe_at': 0.0, 'resubscribe': False, 'resubscribed': 0}


def test_a_slot_resubscribes_only_after_staying_behind():
    state = _slot_state()
    check = workers.slot_too_far_behind
    assert check(state, 3000.0, 100.0, 2000.0)
    assert not state['resubscribe']
    assert not check(state, 100.0, 101.0, 2000.0)     # caught up: starts over
    assert check(state, 3000.0, 102.0, 2000.0)
    assert check(state, 3000.0, 102.0 + workers.RESUBSCRIBE_AFTER_S, 2000.0)
    assert state['resubscribe']
    assert state['resubscribe_at'] >= (
        102.0 + workers.RESUBSCRIBE_AFTER_S + workers.RESUBSCRIBE_EVERY_S[0])


def test_a_slot_waits_between_resubscribes():
    state = _slot_state()
    state['resubscribe_at'] = 200.0
    check = workers.slot_too_far_behind
    assert check(state, 3000.0, 100.0, 2000.0)
    assert check(state, 3000.0, 110.0, 2000.0)
    assert not state['resubscribe']
    assert check(state, 3000.0, 201.0, 2000.0)
    assert state['resubscribe']


@pytest.mark.parametrize("p90", [None, 3000.0])
def test_an_inactive_or_silent_slot_is_not_behind(p90):
    state = _slot_state(active=p90 is None)
    assert not workers.slot_too_far_behind(state, p90, 100.0, 2000.0)
    assert state['behind_since'] is None


def test_the_flag_parses_in_subs_mode(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["adaptive_bench", "moqt://h:1", "--mode", "subs",
                                      "--sub-mbps", "1", "--too-far-behind", "2"])
    assert ab.parse_args().too_far_behind == 2.0


@pytest.mark.parametrize("argv,expected", [
    (["--mode", "bw", "--too-far-behind", "2"], "subs-mode only"),
    (["--mode", "subs", "--sub-mbps", "1", "--too-far-behind", "-1"], "seconds"),
    (["--mode", "subs", "--sub-mbps", "1", "--role", "sub", "--too-far-behind", "2"],
     "--role both"),
])
def test_bad_flags_are_refused(argv, expected, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["adaptive_bench", "moqt://h:1"] + argv)
    with pytest.raises(SystemExit):
        ab.parse_args()
    assert expected in capsys.readouterr().err


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_slot_resubscribes_on_its_session(use_quic):
    for table in (relay._announced, relay._announce_requests, relay._tracks,
                  relay._sessions, relay._catalogs):
        table.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()
    port = _BASE_PORT + 2 * (not use_quic)
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=22)
    handle = await server.serve()
    stop = asyncio.Event()
    stats, state = TrackStats(), _slot_state(active=False)
    url = (f"moqt://localhost:{port}" if use_quic
           else f"https://localhost:{port}/")
    config = {'namespace': _NS, 'trackname': "video", 'insecure': True,
              'draft': 22}
    slot = None
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=22)
        async with client.connect() as pub:
            await pub.client_session_init()
            PublishedTrack(pub, namespace=_NS, trackname="video",
                           object_size=1000, group_size=30, rate=30).attach()
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            slot = asyncio.create_task(workers._slot_supervisor(
                config, parse_relay_url(url), stop, stats, state, 0))
            await _until(lambda: state['active'] and stats.snapshot()['rx_objs'])
            track = relay._tracks[(relay._ns_tuple(_NS), b"video")]
            await _until(lambda: track.downstream)
            sessions = len(relay._sessions)
            before = {id(d) for d in track.downstream}
            state['resubscribe'] = True
            await _until(lambda: state['resubscribed'] == 1 and state['active'])
            await _until(lambda: track.downstream and
                         not before & {id(d) for d in track.downstream})
            assert state['resubscribed'] == 1 and state['active']
            assert not before & {id(d) for d in track.downstream}
            assert len(relay._sessions) == sessions
            stats.snapshot()
            await asyncio.sleep(0.5)
            assert stats.snapshot()['rx_objs'] > 0
            stop.set()
            await asyncio.wait_for(slot, 5)
            slot = None
    finally:
        stop.set()
        if slot is not None:
            slot.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await slot
        handle.close()


async def _until(pred, timeout=8.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()
