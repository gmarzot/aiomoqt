"""pub_media as a service: exit codes, no ready until a relay is up, a
signal shutdown that withdraws the namespace, refuses late requests and
closes once the relay has acknowledged everything, and --reconnect
keeping the media timeline across a relay restart. Audio and video on one
session, over raw QUIC and WebTransport, through the interop relay."""
import asyncio
import csv
import os
import signal
import subprocess
import sys
import time

import pytest

from aiomoqt.media.broadcast import CatalogTrackPublisher
from aiomoqt.media.catalog import Catalog
from aiomoqt.fanout import _FanoutCatalog
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests.test_fanout import _Session
from aiomoqt.tests.test_pub_media_frindell import _argv
from aiomoqt.tests.test_pub_media_mtt import (  # noqa: F401  (fixtures)
    _mtt_events, arrivals, h264_aac,
)
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tools import pub_media, sub_media
from aiomoqt.track import PublishedTrack
from aiomoqt.types import MOQTRequestError, ObjectStatus, RequestErrorCode

_BASE_PORT = 21000


def _url(port, use_quic):
    return f"{'moqt' if use_quic else 'https'}://localhost:{port}/"


async def _serve(port, use_quic, draft):
    """The interop relay on `port`, retried while the port frees up."""
    relay._announced.clear()
    relay._tracks.clear()
    srv = relay._build_server('localhost', port, CERT, KEY,
                              use_quic=use_quic, draft=draft)
    for _ in range(40):
        try:
            return srv, await srv.serve()
        except OSError:
            await asyncio.sleep(0.05)
    return srv, await srv.serve()


async def _stop(srv, handle):
    await srv.close_sessions()
    handle.close()
    relay._announced.clear()
    relay._tracks.clear()


def _pub_events(out: str):
    """pub_media's events; an in-process sub_media prints there too."""
    return _mtt_events('\n'.join(ln for ln in out.splitlines()
                                  if ln.startswith('@mtt ')))


def _relay_end_of_track(monkeypatch) -> set:
    """Names of the tracks whose End of Track reached the relay."""
    seen = set()
    remember = relay._RelayedTrack._remember

    def spy(self, gid, sgid, oid, payload, exts, prio, status, *a, **kw):
        if status == ObjectStatus.END_OF_TRACK:
            seen.add(self.key[1].decode())
        return remember(self, gid, sgid, oid, payload, exts, prio, status,
                        *a, **kw)
    monkeypatch.setattr(relay._RelayedTrack, '_remember', spy)
    return seen


async def _until(cond, timeout):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.05)


# -- exit codes --------------------------------------------------------------

@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_no_relay_reachable_exits_69_without_ready(
        monkeypatch, capsys, use_quic):
    monkeypatch.setattr(pub_media, '_DIAL_TIMEOUT_S', 1.0)
    args = _argv(monkeypatch, 'pub_media', _url(_BASE_PORT + 1, use_quic),
                 '-k', '-t', '5', '--stats-json')
    with pytest.raises(SystemExit) as e:
        await pub_media.run(args)
    assert e.value.code == pub_media.EXIT_UNAVAILABLE
    events = _mtt_events(capsys.readouterr().out)
    assert events == [{'event': 'done', 'reason': 'error'}]


@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_redial_gives_up_each_dead_attempt_within_the_timeout(
        monkeypatch, use_quic):
    assert pub_media._DIAL_TIMEOUT_S == 4.0
    url = _url(_BASE_PORT + 3, use_quic)
    args = _argv(monkeypatch, 'pub_media', url, '-k', '--reconnect')
    ep = pub_media._Endpoint(url, pub_media.parse_relay_url(url), args,
                             False)
    attempts = []
    open_orig = ep.open

    async def timed_open():
        t0 = time.monotonic()
        s = await open_orig()
        attempts.append((t0, time.monotonic(), s))
        return s
    ep.open = timed_open
    redial = asyncio.ensure_future(pub_media._redial(ep, None, {}, 0.0))
    try:
        await _until(lambda: len(attempts) >= 2, 15)
    finally:
        redial.cancel()
        await asyncio.gather(redial, return_exceptions=True)
    (a0, a1, s0), (b0, _, s1) = attempts[:2]
    assert s0 is None and s1 is None
    # A WebTransport client takes ~1 s more to close.
    assert a1 - a0 < pub_media._DIAL_TIMEOUT_S + 1.5
    # Backoff after a 0 s first delay is 1 s.
    assert 0.9 < b0 - a1 < 2.0


def _exit_status(*argv, timeout=30):
    return subprocess.run(
        [sys.executable, '-m', 'aiomoqt.tools.pub_media', *argv],
        capture_output=True, text=True, timeout=timeout)


def test_unreachable_relay_process_exits_69():
    r = _exit_status(f'moqt://localhost:{_BASE_PORT + 2}/', '-k', '-t', '2')
    assert r.returncode == 69, r.stderr[-400:]
    assert 'no relay reachable' in r.stdout


@pytest.mark.parametrize('source', ['h264', 'ts', 'mp4'])
def test_bad_source_exits_70(tmp_path, source):
    empty = tmp_path / 'empty'
    empty.write_bytes(b'')
    path = str(tmp_path / 'missing.mp4') if source == 'mp4' else str(empty)
    r = _exit_status('moqt://localhost:1/', '-k', '-t', '1',
                     f'--{source}', path)
    assert r.returncode == 70, r.stderr[-400:]
    assert ('error' in r.stderr or 'Error' in r.stderr), r.stderr[-400:]


def test_exit_codes_are_in_the_help(capsys, monkeypatch):
    with pytest.raises(SystemExit):
        _argv(monkeypatch, 'pub_media', '-h')
    out = capsys.readouterr().out
    for code in ('64', '69', '70'):
        assert code in out.split('Exit status:')[1]


# -- signal shutdown ----------------------------------------------------------

@requires_certs
@pytest.mark.parametrize('draft,use_quic', [
    (16, True), (18, True), (18, False), (22, True)])
async def test_signal_withdraws_refuses_and_closes(
        monkeypatch, capsys, h264_aac, arrivals, draft,  # noqa: F811
        use_quic):
    port = _BASE_PORT + 10 + 2 * [16, 18, 22].index(draft) + (not use_quic)
    url = _url(port, use_quic)
    ns = f'svc/sig{draft}'
    pub_args = _argv(monkeypatch, 'pub_media', url, '-k', '--draft',
                     str(draft), '-t', '30', '--stats', '0', '--pub-ns',
                     '-N', ns, '--mp4', h264_aac, '--loop', '--stats-json')
    sub_args = _argv(monkeypatch, 'sub_media', url, '-k', '--draft',
                     str(draft), '-N', ns, '--analyze', '-t', '3', '-i', '1')
    log = []
    done_orig = PublishedTrack._send_publish_done

    def publish_done(self, session, status_code=0x2, final=True,
                     reason=None):
        log.append(('publish_done', time.monotonic(), self.trackname,
                    int(status_code)))
        return done_orig(self, session, status_code, final, reason=reason)
    monkeypatch.setattr(PublishedTrack, '_send_publish_done', publish_done)
    withdraw_orig = relay._withdraw_namespace

    def withdraw(session, request_id=None, ns=None):
        log.append(('withdraw', time.monotonic(), session._close_err is None))
        return withdraw_orig(session, request_id=request_id, ns=ns)
    monkeypatch.setattr(relay, '_withdraw_namespace', withdraw)
    probes = []
    drain_orig = _MOQTSessionMixin.drain

    def drain(self):
        drain_orig(self)

        async def late_subscribe():
            # As a relay forwarding a subscriber that arrived late.
            try:
                await upstream.subscribe(ns, 'late', wait_response=True)
                probes.append(None)
            except MOQTRequestError as e:
                probes.append(e.error_code)
        probes.append(asyncio.ensure_future(late_subscribe()))
    monkeypatch.setattr(_MOQTSessionMixin, 'drain', drain)
    ended = _relay_end_of_track(monkeypatch)
    delivered = {}
    deliver_orig = _MOQTSessionMixin.wait_delivered

    async def wait_delivered(self, *a, **kw):
        ok = await deliver_orig(self, *a, **kw)
        delivered[id(self)] = ok
        return ok
    monkeypatch.setattr(_MOQTSessionMixin, 'wait_delivered', wait_delivered)
    close_orig = _MOQTSessionMixin.close

    def close(self, *a, **kw):
        log.append(('close', id(self), delivered.get(id(self))))
        return close_orig(self, *a, **kw)
    monkeypatch.setattr(_MOQTSessionMixin, 'close', close)

    srv, handle = await _serve(port, use_quic, draft)
    try:
        pub = asyncio.ensure_future(pub_media.run(pub_args))
        await _until(lambda: relay._announced or pub.done(), 10)
        upstream = next(iter(next(iter(relay._announced.values()))))
        closed = asyncio.ensure_future(upstream.async_closed())
        sub = asyncio.ensure_future(sub_media.run(sub_args))
        await asyncio.sleep(2.0)
        t_signal = time.monotonic()
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(pub, 5)
        t_done = time.monotonic()
        await asyncio.wait_for(closed, 2)
        t_closed = time.monotonic()
        await asyncio.wait_for(sub, 10)
        await asyncio.wait_for(probes[0], 2)
    finally:
        await _stop(srv, handle)
    assert t_done - t_signal < 2.0, t_done - t_signal
    assert t_closed - t_signal < 2.0, t_closed - t_signal
    dones = [e for e in log if e[0] == 'publish_done' and e[1] >= t_signal]
    assert {e[2] for e in dones} >= {'video', 'audio'}
    assert all(e[3] == 0x2 for e in dones)
    withdrawals = [e for e in log if e[0] == 'withdraw' and e[2]]
    assert len(withdrawals) == 1, log
    assert max(e[1] for e in dones) <= withdrawals[0][1] <= t_closed
    want = (RequestErrorCode.GOING_AWAY if draft >= 18
            else RequestErrorCode.INTERNAL_ERROR)
    assert probes[1:] == [int(want)], probes
    assert {n for n, *_ in arrivals} >= {'video', 'audio'}
    assert ended == {'video', 'audio', 'catalog'}, ended
    # The publisher closes only once its peer has acknowledged everything.
    assert delivered and all(delivered.values()), delivered
    assert [e[2] for e in log if e[0] == 'close' and e[1] in delivered] \
        == [True] * len(delivered), log
    events = _pub_events(capsys.readouterr().out)
    assert events[-1] == {'event': 'done', 'reason': 'signal'}


@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_the_duration_end_sends_end_of_track(
        monkeypatch, h264_aac, arrivals, use_quic):  # noqa: F811
    port = _BASE_PORT + 30 + (not use_quic)
    url = _url(port, use_quic)
    ns = 'svc/duration'
    pub_args = _argv(monkeypatch, 'pub_media', url, '-k', '--draft', '22',
                     '-t', '3', '--stats', '0', '--pub-ns', '-N', ns,
                     '--mp4', h264_aac, '--loop')
    sub_args = _argv(monkeypatch, 'sub_media', url, '-k', '--draft', '22',
                     '-N', ns, '--analyze', '-t', '6', '-i', '1')
    ended = _relay_end_of_track(monkeypatch)
    srv, handle = await _serve(port, use_quic, 22)
    try:
        pub = asyncio.ensure_future(pub_media.run(pub_args))
        await _until(lambda: relay._announced or pub.done(), 10)
        sub = asyncio.ensure_future(sub_media.run(sub_args))
        await asyncio.wait_for(pub, 10)
        sub.cancel()
    finally:
        await _stop(srv, handle)
    assert {n for n, *_ in arrivals} >= {'video', 'audio'}
    assert ended == {'video', 'audio', 'catalog'}, ended


class _Wire:
    """A connection whose send queue and flight read from scripted polls."""
    def __init__(self, backlog, in_transit):
        self.polls, self._backlog, self._in_transit = 0, backlog, in_transit

    def _tx_backlog(self):
        self.polls += 1
        return self._backlog(self.polls)

    def path_quality(self):
        return {'bytes_in_transit': self._in_transit(self.polls)}


class _Delivering:
    DELIVER_POLL_S = 0.001
    wait_delivered = _MOQTSessionMixin.wait_delivered

    def __init__(self, wire, deferred=None):
        self._quic, self._tx_deferred, self._close_err = wire, deferred, None

    def _session_writable(self):
        return self._close_err is None and self._quic is not None


async def test_wait_delivered_waits_for_the_queue_and_the_flight():
    wire = _Wire(lambda n: 1200 if n < 3 else 0,
                 lambda n: 3000 if n < 6 else 0)
    assert await _Delivering(wire).wait_delivered(timeout=1)
    assert wire.polls == 7   # idle from poll 6, confirmed on poll 7


async def test_wait_delivered_holds_for_a_deferred_fin():
    s = _Delivering(_Wire(lambda n: 0, lambda n: 0),
                    deferred={(2, 'send'): (('fin', b''), 0.0)})
    assert not await s.wait_delivered(timeout=0.02)
    s._tx_deferred = None
    assert await s.wait_delivered(timeout=0.02)


async def test_wait_delivered_gives_up_at_the_timeout():
    s = _Delivering(_Wire(lambda n: 0, lambda n: 1500))
    t = time.monotonic()
    assert not await s.wait_delivered(timeout=0.05)
    assert 0.05 <= time.monotonic() - t < 0.5


async def test_wait_delivered_on_a_closed_session_returns_at_once():
    s = _Delivering(_Wire(lambda n: 0, lambda n: 1500))
    s._close_err = (0, '')
    assert not await s.wait_delivered(timeout=5)
    assert s._quic.polls == 0


# -- --reconnect ---------------------------------------------------------------

def _rows(path):
    with open(path) as fh:
        return {r['track']: r for r in csv.DictReader(fh)
                if r['kind'] == 'summary'}


def _groups(seen, track):
    return {g for n, g, _sg, _o in seen if n == track}


@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_reconnect_resumes_the_timeline_after_a_relay_restart(
        monkeypatch, capsys, tmp_path, h264_aac, arrivals,  # noqa: F811
        use_quic):
    port = _BASE_PORT + 20 + (not use_quic)
    url = _url(port, use_quic)
    ns = 'svc/reconnect'
    pub_args = _argv(monkeypatch, 'pub_media', url, '-k', '--draft', '18',
                     '-t', '10', '--stats', '0', '--pub-ns', '-N', ns,
                     '--mp4', h264_aac, '--loop', '--timestamp-mapping',
                     '--reconnect', '--stats-json')

    def sub_args(report):
        return _argv(monkeypatch, 'sub_media', url, '-k', '--draft', '18',
                     '-N', ns, '--analyze', '-t', '2', '-i', '1',
                     '--report', str(report))
    before, after = tmp_path / 'before.csv', tmp_path / 'after.csv'
    srv, handle = await _serve(port, use_quic, 18)
    pub = None
    try:
        pub = asyncio.ensure_future(pub_media.run(pub_args))
        await _until(lambda: relay._announced or pub.done(), 10)
        assert await sub_media.run(sub_args(before)) == 0
        first = list(arrivals)
        await _stop(srv, handle)
        await asyncio.sleep(0.3)
        srv, handle = await _serve(port, use_quic, 18)
        await _until(lambda: relay._announced or pub.done(), 10)
        assert not pub.done()
        assert await sub_media.run(sub_args(after)) == 0
        second = arrivals[len(first):]
        await asyncio.wait_for(pub, 15)
    finally:
        if pub is not None and not pub.done():
            pub.cancel()
        await _stop(srv, handle)
    for track in ('video', 'audio'):
        assert _groups(first, track) and _groups(second, track), track
        # A join FETCH may still return the newest group from before
        # the gap; the subscription goes on past it.
        assert max(_groups(second, track)) > max(_groups(first, track))
        for report in (before, after):
            row = _rows(report)[track]
            assert int(row['objects']) > 0, row
            assert row['ts_source'] == 'frindell', row
            assert row['ts_malformed'] == '0', row
            assert 0 <= float(row['lat_p50_ms']) < 1000, row
    events = _pub_events(capsys.readouterr().out)
    kinds = [e['event'] for e in events if e['event'] != 'stats']
    assert kinds == ['ready', 'endpoint_lost', 'endpoint_up', 'done'], kinds
    up = next(e for e in events if e['event'] == 'endpoint_up')
    assert up == {'event': 'endpoint_up', 'url': url, 'draft': 18}
    assert events[-1]['reason'] == 'duration'


@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_reconnect_cancels_the_lost_sessions_catalog_emitter(
        monkeypatch, h264_aac, arrivals, use_quic):  # noqa: F811
    port = _BASE_PORT + 22 + (not use_quic)
    url = _url(port, use_quic)
    ns = 'svc/emitter'
    pub_args = _argv(monkeypatch, 'pub_media', url, '-k', '--draft', '18',
                     '-t', '10', '--stats', '0', '--pub-ns', '-N', ns,
                     '--mp4', h264_aac, '--loop', '--reconnect')
    sub_args = _argv(monkeypatch, 'sub_media', url, '-k', '--draft', '18',
                     '-N', ns, '--analyze', '-t', '2', '-i', '1')
    emitters = []   # (catalog track, its session, emitter task)
    generate = CatalogTrackPublisher.generate

    async def recording(self, session, track_alias):
        await generate(self, session, track_alias)
        emitters.append((self, session, self._emitter))
    monkeypatch.setattr(CatalogTrackPublisher, 'generate', recording)
    srv, handle = await _serve(port, use_quic, 18)
    pub = None
    try:
        pub = asyncio.ensure_future(pub_media.run(pub_args))
        await _until(lambda: relay._announced or pub.done(), 10)
        assert await sub_media.run(sub_args) == 0
        old = list(emitters)
        assert old
        await _stop(srv, handle)
        await asyncio.sleep(0.3)
        srv, handle = await _serve(port, use_quic, 18)
        await _until(lambda: relay._announced or pub.done(), 10)
        assert not pub.done()
        seen = len(arrivals)
        assert await sub_media.run(sub_args) == 0
        assert len(arrivals) > seen
        for _, _, task in old:
            assert task.done(), task
        new = [(c, t) for c, s, t in emitters[len(old):]
               if s not in {s for _, s, _ in old}]
        assert new and any(c._total_sent for c, _ in new), emitters
        await asyncio.wait_for(pub, 15)
    finally:
        if pub is not None and not pub.done():
            pub.cancel()
        await _stop(srv, handle)


# -- catalog numbering across sessions -----------------------------------------

async def test_catalog_groups_continue_on_a_new_session():
    cat = Catalog(generatedAt=0, tracks=[])
    a = CatalogTrackPublisher(_Session('a'), 'svc/cat', cat)
    a.next_group = 5
    fan = _FanoutCatalog([a])
    fan.drop_session(a.session)
    assert fan.members == [] and fan.catalog is cat
    b = CatalogTrackPublisher(_Session('b'), 'svc/cat', fan.catalog)
    fan.add(b)
    assert b.next_group == 5
