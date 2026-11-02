"""pub_media --timestamp-mapping anchored at the Unix epoch
(draft-marzot-moq-test-tracks Timing): Group ID = capture ticks // Group
Multiplier, the run starts on a group boundary or at --start-at, loops
stay on the grid, and the anchor event; through the relay end to end."""
import asyncio
import json
import logging
import time
from fractions import Fraction
from types import SimpleNamespace as NS
from typing import Any, cast

import pytest

from aiomoqt.media.loc import LocTrackSubscriber
from aiomoqt.media.mpegts import TsCadence, TsClock, TsUnit
from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_pub_media_frindell import _argv
from aiomoqt.tests.test_pub_media_loop import _Rec, _clip, _feed, _numbered
from aiomoqt.tests.test_pub_media_mtt import (  # noqa: F401  (fixtures)
    _pub_args, arrivals, h264_aac,
)
from aiomoqt.tests.test_pub_media_service import _serve, _stop, _until, _url
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tools import pub_media, sub_media
from aiomoqt.utils.timing import (
    OBJECT_TIMESTAMP, TIMESCALE, TIMESTAMP_MAPPING, decode_mapping,
)

_BASE_PORT = 28800


def _ticks(m, capture_us):
    t = Fraction(capture_us) * m.timescale / 1_000_000
    assert t.denominator == 1, capture_us        # whole ticks
    return int(t)


def _check_mapped(rec):
    """Every object on the mapping, its group the epoch's."""
    m = rec.frindell
    for group, obj, s in _numbered(rec):
        assert OBJECT_TIMESTAMP not in m.object_properties(
            group, obj, s.capture), (group, obj)
        if obj == 0:
            assert group == _ticks(m, s.capture) // m.group_multiplier


# -- mp4 ----------------------------------------------------------------------

async def test_mp4_groups_are_epoch_ticks_over_the_group_multiplier(
        tmp_path):
    path = _clip(tmp_path / 'c.mp4', 90, acodec='libopus')
    (video, audio), mp4s = await _feed(path, 3, mapping=True)
    wall0 = video.sent[0].capture
    v, a = video.frindell, audio.frindell
    assert _ticks(v, wall0) % v.group_multiplier == 0       # on a boundary
    assert video.sent[0].hint == _ticks(v, wall0) // v.group_multiplier
    first = audio.sent[0].capture
    assert 0 <= first - wall0 <= 1_000_000 * a.object_multiplier / a.timescale
    assert _ticks(a, first) % a.object_multiplier == 0
    for rec in (video, audio):
        _check_mapped(rec)                  # across both seams too


def test_properties_do_not_depend_on_the_start(tmp_path, monkeypatch):
    from aiomoqt.media.sources import Mp4Reader
    path = _clip(tmp_path / 'c.mp4', 30)
    props = []
    for now in (1_767_225_600.25, 1_767_229_999.9):
        monkeypatch.setattr(time, 'time', lambda now=now: now)
        reader = Mp4Reader(path)
        timings = [pub_media._frindell_for(p) for p in
                   pub_media._mp4_passes(reader, reader.audio, False)]
        _, wall0 = pub_media._media_clock(pub_media._grid(*timings))
        assert wall0 >= now * 1_000_000
        props.append([m.track_properties(True) for m in timings])
    assert props[0] == props[1]
    assert decode_mapping(props[0][0][TIMESTAMP_MAPPING], True)[:2] == (0, 0)


async def test_a_loop_trimmed_to_the_grid_warns_and_needs_no_correction(
        tmp_path, caplog):
    # 2.5 s of 1 s GOPs and 20 ms Opus: trimmed to 2 s.
    path = _clip(tmp_path / 'c.mp4', 75, acodec='libopus')
    with caplog.at_level(logging.WARNING, logger=pub_media.__name__):
        recs, mp4s = await _feed(path, 3, mapping=True)
    assert [p.period for p in mp4s] == [2, 2]
    warned = [r.getMessage() for r in caplog.records]
    assert len(warned) == 1 and 'trimmed from 2.500 to 2.000' in warned[0]
    assert len(recs[0].sent) == 3 * 60
    for rec in recs:
        _check_mapped(rec)


async def test_a_clip_shorter_than_the_grid_period_keeps_its_length(
        tmp_path, caplog):
    # 1 s GOPs and 1024-sample AAC frames meet every 8 s.
    path = _clip(tmp_path / 'c.mp4', 72)
    with caplog.at_level(logging.WARNING, logger=pub_media.__name__):
        _, mp4s = await _feed(path, 2, mapping=True)
    assert mp4s[0].period == Fraction(12, 5)
    warned = [r.getMessage() for r in caplog.records]
    assert len(warned) == 1 and 'loop seams carry' in warned[0]


async def test_loc_timestamp_is_the_capture_time_rounded_to_the_us():
    rec = _Rec()
    for capture in (Fraction(125, 2), Fraction(124, 3), Fraction(-1, 2)):
        await pub_media._send(rec, pub_media._TrackStats(), b'x', True,
                              timestamp=1_767_225_600_000_000 + capture)
    assert [s.ts - 1_767_225_600_000_000 for s in rec.sent] == [63, 41, 0]
    assert all(isinstance(s.ts, int) for s in rec.sent)


# -- --ts ---------------------------------------------------------------------

async def test_ts_groups_are_epoch_ticks_over_the_group_multiplier(
        monkeypatch):
    async def no_rates(*a):
        pass
    monkeypatch.setattr(pub_media, '_apply_rates', no_rates)
    cadence = TsCadence.parse('fps=30,gop=30')
    # Audio leads video by 7 ms; 1024-sample frames at 48 kHz.
    units = sorted(
        [TsUnit('video', b'v', i % 30 == 0, 90_000 + 3000 * i)
         for i in range(90)]
        + [TsUnit('audio', b'a', True, 90_000 - 630 + 1920 * i)
           for i in range(140)], key=lambda u: u.pts)
    dmx = cast(Any, NS(clock=TsClock(), config_changed=False,
                       feed=lambda c: [], close=lambda: [],
                       audio_samplerate=48000, audio_frame_samples=1024))
    dmx.to_us = dmx.clock.to_us
    v_time = pub_media._ts_timestamps(NS(ts_cadence=cadence), dmx, units,
                                      'video')
    a_time = pub_media._ts_timestamps(NS(ts_cadence=cadence), dmx, units,
                                      'audio')
    clock = pub_media._media_clock(pub_media._grid(v_time, a_time))
    wall0 = clock[1]
    pub_media._anchor_ts(dmx, units, True, wall0)
    tracks = {'video': _Rec(v_time), 'audio': _Rec(a_time)}
    await pub_media._feed_ts_live(
        tracks, NS(read1=lambda n: b''), dmx, units,
        NS(duration=10.0, no_pace=True), {k: pub_media._TrackStats()
                                          for k in tracks},
        None, 1, clock)
    video, audio = tracks['video'], tracks['audio']
    assert video.sent[0].capture == wall0 and video.sent[0].key
    assert video.sent[0].hint == _ticks(v_time, wall0) // 30_000
    first = audio.sent[0].capture
    assert 0 <= first - wall0 <= 1_000_000 * 1024 / 48000
    assert _ticks(a_time, first) % 1024 == 0
    for rec in (video, audio):
        assert len(rec.sent) > 60
        _check_mapped(rec)


# -- --start-at ---------------------------------------------------------------

def test_start_at_on_a_boundary_is_the_start():
    grid = (30000, 60 * 1001)                 # 29.97 fps, 2.002 s GOPs
    now = int(time.time() * 1_000_000)
    _, nxt = pub_media._media_clock(grid)
    at = nxt + 2_002_000
    pub_media._check_start_at(at, grid, True)
    mono, wall0 = pub_media._media_clock(grid, at)
    assert wall0 == at
    assert abs(mono - time.monotonic() - (at - now) / 1e6) < 0.5


def test_start_at_off_a_boundary_names_the_nearest(capsys):
    grid = (30000, 60 * 1001)
    on = pub_media._media_clock(grid)[1] + 2_002_000
    with pytest.raises(SystemExit) as e:
        pub_media._check_start_at(on + 1000, grid, True)
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert (f'not on a video group boundary; the nearest are {on} and '
            f'{on + 2_002_000}') in err


def test_start_at_in_the_past_is_refused(capsys):
    grid = (30000, 30000)
    past = (int(time.time()) - 5) * 1_000_000
    for call in (lambda: pub_media._check_start_at(past, grid, True),
                 lambda: pub_media._media_clock(grid, past)):
        with pytest.raises(SystemExit) as e:
            call()
        assert e.value.code == 2
        assert 's in the past' in capsys.readouterr().err


@pytest.mark.parametrize('argv,why', [
    (['--mp4', 'x.mp4'], '--start-at needs --timestamp-mapping'),
    (['--timestamp-mapping'], '--start-at applies to --mp4 and --ts'),
])
def test_start_at_flag_refused_where_it_cannot_apply(monkeypatch, capsys,
                                                     argv, why):
    with pytest.raises(SystemExit):
        _pub_args(monkeypatch, *argv, '--start-at', '1767225600000000')
    assert why in capsys.readouterr().err


async def test_run_refuses_an_off_boundary_start_before_dialing(
        monkeypatch, capsys, h264_aac):  # noqa: F811
    start = (int(time.time()) + 60) * 1_000_000 + 500_000
    args = _pub_args(monkeypatch, '--mp4', h264_aac, '--timestamp-mapping',
                     '--start-at', str(start), '--stats', '0')
    with pytest.raises(SystemExit) as e:
        await pub_media.run(args)
    assert e.value.code == 2
    assert (f'the nearest are {start - 500_000} and {start + 500_000}'
            in capsys.readouterr().err)


# -- end to end ---------------------------------------------------------------

def _announced(ns: str) -> bool:
    return tuple(p.encode() for p in ns.split('/')) in relay._announced


@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_epoch_groups_and_anchor_through_the_relay(
        monkeypatch, capsys, h264_aac, arrivals, use_quic):  # noqa: F811
    port = _BASE_PORT + (not use_quic)
    url = _url(port, use_quic)
    order, props = [], []
    send = pub_media._send

    async def spy_send(track, *a, **kw):
        if track.demand[0]:
            order.append('object')
        return await send(track, *a, **kw)
    monkeypatch.setattr(pub_media, '_send', spy_send)
    emit = pub_media._MttEvents.emit

    def spy_emit(self, event, **fields):
        order.append(event)
        return emit(self, event, **fields)
    monkeypatch.setattr(pub_media._MttEvents, 'emit', spy_emit)
    take = LocTrackSubscriber.take_track_properties

    def track_props(self, msg):
        take(self, msg)
        if self.trackname == 'video':
            props.append(dict(self.properties or {}))
    monkeypatch.setattr(LocTrackSubscriber, 'take_track_properties',
                        track_props)
    srv, handle = await _serve(port, use_quic, 22)
    anchors, groups = [], []
    try:
        for run in range(2):
            ns = f'epoch/{run}'
            del arrivals[:]
            pub_args = _argv(monkeypatch, 'pub_media', url, '-k', '--draft',
                             '22', '-t', '2', '--stats', '0', '--pub-ns',
                             '-N', ns, '--mp4', h264_aac, '--loop',
                             '--timestamp-mapping', '--stats-json')
            sub_args = _argv(monkeypatch, 'sub_media', url, '-k', '--draft',
                             '22', '-N', ns, '--analyze', '-t', '5', '-i',
                             '1')
            begun = time.time()
            pub = asyncio.ensure_future(pub_media.run(pub_args))
            await _until(lambda: _announced(ns) or pub.done(), 10)
            sub = asyncio.ensure_future(sub_media.run(sub_args))
            await asyncio.wait_for(pub, 15)
            sub.cancel()
            groups.append(sorted({g for n, g, *_ in arrivals
                                  if n == 'video'}))
            # The groups count seconds since the epoch (1 s GOPs).
            assert groups[-1] and abs(groups[-1][0] - begun) < 5, groups
            events = [json.loads(ln[5:]) for ln in
                      capsys.readouterr().out.splitlines()
                      if ln.startswith('@mtt ')]
            anchors.append(next(e for e in events
                                if e['event'] == 'anchor'))
    finally:
        await _stop(srv, handle)
    assert order.index('anchor') < order.index('object')
    assert order.index('ready') < order.index('anchor')
    assert len(props) == 2 and props[0] == props[1]   # restart: same props
    for anchor, gs in zip(anchors, groups):
        v = anchor['tracks']['video']
        assert set(anchor['tracks']) == {'video', 'audio'}
        assert (props[0][TIMESCALE], decode_mapping(
            props[0][TIMESTAMP_MAPPING], True)) == (
            v['timescale'], (0, 0, v['group_multiplier'],
                             v['object_multiplier']))
        start = Fraction(anchor['start_us']) * v['timescale'] / 1_000_000
        assert start % v['group_multiplier'] == 0
        g0 = start // v['group_multiplier']
        assert g0 <= gs[0] and gs[-1] <= g0 + 2, (g0, gs)
