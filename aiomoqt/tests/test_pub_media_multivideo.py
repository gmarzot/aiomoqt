"""pub_media with several video tracks: --mp4 repeated and every video
stream of an MPEG-TS, catalog names, labels and altGroups, the usage
errors, one loop period, and end to end through the interop relay over
raw QUIC and WebTransport: for one capture time every video track carries
the same group and object IDs."""
import asyncio
import contextlib
import json
import logging
import shutil
import struct
import subprocess
from types import SimpleNamespace as NS

import pytest

from aiomoqt.media import Catalog, LocFrame
from aiomoqt.media.cmaf import CmafChunker
from aiomoqt.media.sources import Mp4Reader
from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_pub_media_frindell import _argv
from aiomoqt.tests.test_pub_media_loop import _Rec
from aiomoqt.tests.test_pub_media_mtt import _paced_fifo
from aiomoqt.tests.test_pub_media_service import (
    _pub_events, _relay_end_of_track, _serve, _stop, _until, _url,
)
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tools import pub_media, sub_media
from aiomoqt.tools.sub_media import _Writers
from aiomoqt.utils.media_stats import MediaAnalysis

_BASE_PORT = 30200
_X264 = ['-c:v', 'libx264', '-preset', 'ultrafast', '-sc_threshold', '0',
         '-bf', '0']


def _ffmpeg(*argv):
    if shutil.which('ffmpeg') is None:
        pytest.skip('ffmpeg not installed')
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                    *argv], check=True)


def _source(seconds, rate=30):
    return ['-f', 'lavfi', '-i',
            f'testsrc=size=320x180:rate={rate}:duration={seconds}',
            '-f', 'lavfi', '-i',
            f'sine=frequency=440:sample_rate=48000:duration={seconds}']


def _rendition(path, size, seconds=2, rate=30, gop=30):
    """One rendition of the synthetic source: a key frame every `gop`
    frames, no B-frames, 20 ms Opus."""
    _ffmpeg(*_source(seconds, rate), '-map', '0:v', '-map', '1:a',
            '-vf', f'scale={size}', *_X264, '-g', str(gop),
            '-keyint_min', str(gop), '-c:a', 'libopus', '-b:a', '48k',
            str(path))
    return str(path)


def _two_stream_ts(path, seconds):
    """Two H.264 streams of one source (320x180, 160x96) and AAC."""
    _ffmpeg(*_source(seconds), '-filter_complex',
            '[0:v]split=2[a][b];[b]scale=160:96[c]', '-map', '[a]',
            '-map', '[c]', '-map', '1:a', *_X264, '-g', '30',
            '-keyint_min', '30', '-c:a', 'aac', '-b:a', '64k',
            '-f', 'mpegts', '-pes_payload_size', '0',
            '-omit_video_pes_length', '0', '-muxdelay', '0', str(path))
    return str(path)


@pytest.fixture(scope='module')
def ladder(tmp_path_factory):
    d = tmp_path_factory.mktemp('ladder')
    return (_rendition(d / 'hi.mp4', '320:180'),
            _rendition(d / 'lo.mp4', '160:96'))


def _pub_args(monkeypatch, *argv):
    return _argv(monkeypatch, 'pub_media', 'moqt://localhost:1/', *argv)


def _entries(cat):
    return {t['name']: t for t in cat.to_dict()['tracks']}


# -- catalog ---------------------------------------------------------------

@pytest.mark.parametrize('alt,groups', [('one', (1, 1)), ('each', (1, 2))])
def test_catalog_ladder_and_views(monkeypatch, ladder, alt, groups):
    hi, lo = (Mp4Reader(p) for p in ladder)
    args = _pub_args(monkeypatch, '--mp4', ladder[0], '--mp4', ladder[1],
                     '--video-alt', alt, '--video-label', 'high',
                     '--video-label', 'low', '--catalog-bitrate', '900000',
                     '--catalog-bitrate', '300000')
    cat = pub_media._build_catalog(args, [hi.video, lo.video], hi.audio)
    assert cat.validate() == []
    assert [t.name for t in cat.tracks] == ['video', 'video_1', 'audio']
    e = _entries(cat)
    for name, label, group, bps, size, ref in (
            ('video', 'high', groups[0], 900_000, (320, 180), 'v0'),
            ('video_1', 'low', groups[1], 300_000, (160, 96), 'v1')):
        t = e[name]
        assert (t['label'], t['altGroup'], t['bitrate']) == (label, group,
                                                             bps)
        assert (t['width'], t['height'], t['initRef']) == (*size, ref)
        assert t['renderGroup'] == 1
    assert cat.resolve_init(cat.find('video')) == hi.video.config
    assert cat.resolve_init(cat.find('video_1')) == lo.video.config
    assert 'altGroup' not in e['audio'] and 'label' not in e['audio']
    assert e['audio']['bitrate'] == hi.audio.avg_bitrate


def test_json_round_trip_keeps_labels_and_alt_groups(monkeypatch, ladder):
    hi, lo = (Mp4Reader(p) for p in ladder)
    args = _pub_args(monkeypatch, '--mp4', ladder[0], '--mp4', ladder[1],
                     '--video-label', 'high', '--video-label', 'low')
    cat = pub_media._build_catalog(args, [hi.video, lo.video], hi.audio)
    back = Catalog.from_json(json.dumps(cat.to_dict()))
    assert [(t.name, t.label, t.altGroup) for t in back.tracks] == [
        ('video', 'high', 1), ('video_1', 'low', 1), ('audio', None, None)]


def test_catalog_bitrates_fill_in_order(monkeypatch, ladder):
    hi, lo = (Mp4Reader(p) for p in ladder)
    args = _pub_args(monkeypatch, '--mp4', ladder[0], '--mp4', ladder[1],
                     '--catalog-bitrate', '900000', '--video-label', 'high')
    e = _entries(pub_media._build_catalog(args, [hi.video, lo.video],
                                          hi.audio))
    assert (e['video']['bitrate'], e['video_1']['bitrate']) == (
        900_000, lo.video.avg_bitrate)
    assert e['video']['label'] == 'high' and 'label' not in e['video_1']


def test_cmaf_catalog_has_one_init_per_video_track(monkeypatch, ladder):
    hi, lo = (Mp4Reader(p) for p in ladder)
    args = _pub_args(monkeypatch, '--mp4', ladder[0], '--mp4', ladder[1],
                     '--packaging', 'cmaf')
    chunkers = {'video': CmafChunker(hi.video),
                'video_1': CmafChunker(lo.video),
                'audio': CmafChunker(hi.audio)}
    cat = pub_media._build_catalog(args, [hi.video, lo.video], hi.audio,
                                   chunkers)
    assert [i.id for i in cat.initDataList] == ['a0', 'v0', 'v1']
    for name in ('video', 'video_1', 'audio'):
        assert cat.resolve_init(cat.find(name)) == (
            chunkers[name].init_segment())


def test_one_video_track_catalog_is_unchanged(monkeypatch, ladder):
    hi = Mp4Reader(ladder[0])
    args = _pub_args(monkeypatch, '--mp4', ladder[0], '--catalog-bitrate',
                     '700000')
    one = pub_media._build_catalog(args, hi.video, hi.audio).to_dict()
    listed = pub_media._build_catalog(args, [hi.video], hi.audio).to_dict()
    for d in (one, listed):
        d.pop('generatedAt')
    assert one == listed
    video = one['tracks'][0]
    assert video['name'] == 'video' and video['bitrate'] == 700_000
    assert 'altGroup' not in video and 'label' not in video
    assert [i['id'] for i in one['initDataList']] == ['v0']
    # Without video, the one value is audio's.
    audio = pub_media._build_catalog(
        _pub_args(monkeypatch, '--catalog-bitrate', '64000'), None, hi.audio)
    assert audio.find('audio').bitrate == 64_000


def test_ts_catalog_takes_every_video_stream(monkeypatch, tmp_path):
    path = _two_stream_ts(tmp_path / 'two.ts', 1)
    args = _pub_args(monkeypatch, '--ts', path, '--video-alt', 'each',
                     '--video-label', 'wide', '--video-label', 'small',
                     '--ts-cadence', 'fps=30,gop=30')
    fh, dmx, _, videos, audio = pub_media._open_live_ts(args)
    fh.close()
    assert dmx.video_pid == dmx.video_pids[0] and len(videos) == 2
    e = _entries(pub_media._build_catalog(args, videos, audio))
    assert list(e) == ['video', 'video_1', 'audio']
    assert [(e[n]['label'], e[n]['altGroup'], e[n]['width'])
            for n in ('video', 'video_1')] == [('wide', 1, 320),
                                               ('small', 2, 160)]
    assert e['video']['initRef'] == 'v0' and e['video_1']['initRef'] == 'v1'


async def test_ts_config_change_follows_each_track(monkeypatch, tmp_path,
                                                   capsys):
    path = _two_stream_ts(tmp_path / 'two.ts', 1)
    args = _pub_args(monkeypatch, '--ts', path)
    fh, dmx, _, videos, audio = pub_media._open_live_ts(args)
    fh.close()
    cat = pub_media._build_catalog(args, videos, audio)
    tracks = {pub_media._video_name(i): NS(config=b'old', annexb=False,
                                           codec_string=None)
              for i in range(2)}
    tracks['video'].config = dmx.videos[0].config
    pub = NS(catalog_track=NS(catalog=cat, state=None))
    await pub_media._apply_config_change(dmx, tracks, pub)
    assert tracks['video_1'].config == dmx.videos[1].config
    out = capsys.readouterr().out
    assert 'video_1 config changed: 160x96' in out
    assert 'video config' not in out


# -- usage errors ----------------------------------------------------------

@pytest.mark.parametrize('argv,why', [
    (['--h264', '-', '--video-alt', 'each'], '--video-alt applies to'),
    (['--h264', '-', '--video-label', 'a'], '--video-label applies to'),
    (['--input', 'x.m3u8', '--video-alt', 'one'], '--video-alt applies to'),
    (['--input', 'x.m3u8', '--video-label', 'a'], '--video-label applies to'),
    (['--catalog-bitrate', '1', '--catalog-bitrate', '2'],
     'several values need --mp4 or --ts'),
    (['--mp4', 'x.mp4', '--catalog-bitrate', '1', '--catalog-bitrate', '0'],
     'must be positive'),
])
def test_flags_refused_where_they_cannot_apply(monkeypatch, capsys, argv,
                                               why):
    with pytest.raises(SystemExit) as e:
        _pub_args(monkeypatch, *argv)
    assert e.value.code == 2 and why in capsys.readouterr().err


async def _usage_exit(monkeypatch, capsys, *argv):
    """pub_media's exit code and stderr for a run that stops before it
    dials."""
    monkeypatch.setattr(pub_media, '_DIAL_TIMEOUT_S', 0.5)
    args = _pub_args(monkeypatch, '--stats', '0', *argv)
    with pytest.raises(SystemExit) as e:
        await pub_media.run(args)
    return e.value.code, capsys.readouterr().err


@pytest.mark.parametrize('flag', ['--video-label', '--catalog-bitrate'])
async def test_more_values_than_video_tracks(monkeypatch, capsys, ladder,
                                             tmp_path, flag):
    values = [a for v in ('1', '2', '3') for a in (flag, v)]
    code, err = await _usage_exit(monkeypatch, capsys, '--mp4', ladder[0],
                                  '--mp4', ladder[1], *values)
    assert code == 2 and f'3 {flag} values for 2 video track(s)' in err
    ts = _two_stream_ts(tmp_path / 'two.ts', 1)
    code, err = await _usage_exit(monkeypatch, capsys, '--ts', ts, *values)
    assert code == 2 and f'3 {flag} values for 2 video track(s)' in err


@pytest.mark.parametrize('rate,gop', [(25, 25), (30, 60)])
async def test_mapping_needs_one_cadence_on_every_video_track(
        monkeypatch, capsys, ladder, tmp_path, rate, gop):
    other = _rendition(tmp_path / 'other.mp4', '160:96', rate=rate, gop=gop)
    code, err = await _usage_exit(monkeypatch, capsys, '--mp4', ladder[0],
                                  '--mp4', other, '--timestamp-mapping')
    assert code == 2
    assert ('needs one frame duration and GOP length on every video track: '
            'video 33.333 ms frames, 30 per GOP; '
            f'video_1 {1000 / rate:.3f} ms frames, {gop} per GOP') in err
    # Without the mapping nothing is checked: the run gets to dialing.
    code, _ = await _usage_exit(monkeypatch, capsys, '--mp4', ladder[0],
                                '--mp4', other)
    assert code == pub_media.EXIT_UNAVAILABLE


# -- loop period and track ends ----------------------------------------------

def test_loop_runs_every_track_on_the_shortest_clip(tmp_path, ladder,
                                                    caplog):
    longer = _rendition(tmp_path / 'long.mp4', '160:96', seconds=3)
    hi, lo = Mp4Reader(ladder[0]), Mp4Reader(longer)
    with caplog.at_level(logging.WARNING, logger=pub_media.__name__):
        videos, audio = pub_media._mp4_ladder([hi.video, lo.video],
                                              hi.audio, True, True)
    assert [p.period for p in (*videos, audio)] == [2, 2, 2]
    assert all(t < 2 for p in videos for t in p.times)
    assert len(videos[1].times) == 60
    warned = [r.getMessage() for r in caplog.records]
    assert len(warned) == 1 and 'loop on the shortest, 2.000 s' in warned[0]
    caplog.clear()
    pub_media._mp4_ladder([hi.video, Mp4Reader(ladder[1]).video], hi.audio,
                          True, True)
    assert not caplog.records


async def test_without_loop_each_track_ends_at_its_own_end(tmp_path,
                                                           ladder):
    longer = _rendition(tmp_path / 'long.mp4', '160:96', seconds=3)
    hi, lo = Mp4Reader(ladder[0]), Mp4Reader(longer)
    videos, _ = pub_media._mp4_ladder([hi.video, lo.video], None, False)
    args = NS(duration=10.0, no_pace=True, loop=False)
    recs = []
    for p in videos:
        recs.append(_Rec())
        await pub_media._feed_mp4_track(recs[-1], p, args,
                                        pub_media._TrackStats(),
                                        clock=(0, 1_767_225_600_000_000))
    assert [len(r.sent) for r in recs] == [60, 90]


# -- sub_media ---------------------------------------------------------------

def _two_video_catalog(*names):
    return Catalog.from_dict({'version': '1', 'tracks': [
        {'name': n, 'packaging': 'loc', 'isLive': True, 'role': 'video',
         'codec': 'avc1.640028', 'bitrate': 1} for n in names]})


def test_writers_write_each_loc_video_track(tmp_path):
    lp = struct.pack('>I', 4) + b'\x65\x88\x80\x00'
    for names, files in ((('video', 'video_1'), {'video.h264',
                                                 'video_1.h264'}),
                         (('video',), {'video.h264'})):
        out = tmp_path / str(len(names))
        out.mkdir()
        sub = NS(catalog=_two_video_catalog(*names),
                 tracks={n: NS(config=None) for n in names})
        w = _Writers(str(out), sub)
        for n in names:
            w.on_frame(n, LocFrame(payload=lp, key_frame=True), 0, 0)
        w.close()
        assert {p.name for p in out.iterdir()} == files
        for f in files:
            assert (out / f).read_bytes() == b'\x00\x00\x00\x01' + lp[4:]
    # A track left out by -T keeps video.h264.
    out = tmp_path / 'one'
    out.mkdir()
    sub = NS(catalog=_two_video_catalog('video', 'video_1'),
             tracks={'video_1': NS(config=None)})
    w = _Writers(str(out), sub, wanted=lambda e: e.name == 'video_1')
    w.on_frame('video_1', LocFrame(payload=lp, key_frame=True), 0, 0)
    w.close()
    assert [p.name for p in out.iterdir()] == ['video.h264']


# -- end to end ---------------------------------------------------------------

@pytest.fixture
def seen(monkeypatch):
    """(track, group, object, LOC timestamp, payload) of every media
    object sub_media --analyze sees."""
    out = []
    orig = MediaAnalysis.on_arrival

    def record(self, name, msg, recv_us, group_id, subgroup_id, frame):
        if frame is not None:
            out.append((name, group_id, msg.object_id, frame.timestamp,
                        bytes(frame.payload)))
        return orig(self, name, msg, recv_us, group_id, subgroup_id, frame)
    monkeypatch.setattr(MediaAnalysis, 'on_arrival', record)
    return out


async def _through_relay(monkeypatch, port, use_quic, seconds, pub_argv,
                         sub_argv):
    url = _url(port, use_quic)
    ns = f'multivideo/{port}'
    pub_args = _argv(monkeypatch, 'pub_media', url, '-k', '--draft', '22',
                     '-t', str(seconds), '--pub-ns', '-N', ns, *pub_argv)
    sub_args = _argv(monkeypatch, 'sub_media', url, '-k', '--draft', '22',
                     '-N', ns, *sub_argv)
    srv, handle = await _serve(port, use_quic, 22)
    try:
        pub = asyncio.ensure_future(pub_media.run(pub_args))
        await _until(lambda: relay._announced or pub.done(), 10)
        sub = asyncio.ensure_future(sub_media.run(sub_args))
        await asyncio.wait_for(pub, seconds + 15)
        await asyncio.sleep(0.5)
        sub.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sub
    finally:
        await _stop(srv, handle)


def _check_aligned(seen, names, at_least):
    """Every track got objects; for one capture time all carry the same
    group and object IDs; no group or object is missing."""
    ids = {n: {} for n in names}
    for name, group, obj, ts, _ in seen:
        if name in ids:
            assert ids[name].setdefault(ts, (group, obj)) == (group, obj)
    for name in names:
        assert len(ids[name]) >= at_least, (name, len(ids[name]))
        groups = {}
        for group, obj in ids[name].values():
            groups.setdefault(group, []).append(obj)
        gs = sorted(groups)
        assert gs == list(range(gs[0], gs[-1] + 1)), (name, gs)
        for g in gs:
            assert sorted(groups[g]) == list(range(len(groups[g]))), (name, g)
    common = set.intersection(*(set(ids[n]) for n in names))
    assert len(common) >= at_least
    for ts in common:
        assert len({ids[n][ts] for n in names}) == 1, ts
    return ids


@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_mp4_ladder_through_the_relay(monkeypatch, capsys, ladder,
                                            seen, use_quic):
    ended = _relay_end_of_track(monkeypatch)
    await _through_relay(
        monkeypatch, _BASE_PORT + (not use_quic), use_quic, 5,
        ['--mp4', ladder[0], '--mp4', ladder[1], '--loop',
         '--timestamp-mapping', '--stats', '0.5', '--stats-json'],
        ['--analyze', '-t', '8', '-i', '1'])
    names = ('video', 'video_1')
    ids = _check_aligned(seen, names, 60)
    assert any(n == 'audio' for n, *_ in seen)
    # The loop seams: each clip's first frame at the same capture times.
    seams = {}
    for name, path in zip(names, ladder):
        first = next(Mp4Reader(path).video.samples()).payload
        seams[name] = sorted(ts for n, _, _, ts, p in seen
                             if n == name and p == first)
    lo = max(min(ids[n]) for n in names)
    hi = min(max(ids[n]) for n in names)
    window = [[t for t in seams[n] if lo <= t <= hi] for n in names]
    assert window[0] and window[0] == window[1], seams
    events = _pub_events(capsys.readouterr().out)
    anchor = next(e for e in events if e['event'] == 'anchor')
    assert set(anchor['tracks']) == {'video', 'video_1', 'audio'}
    assert anchor['tracks']['video'] == anchor['tracks']['video_1']
    stats = [e for e in events if e['event'] == 'stats']
    assert stats and set(stats[-1]['tracks']) == {'video', 'video_1',
                                                  'audio'}
    assert {'video', 'video_1', 'audio'} <= ended


@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_two_stream_ts_through_the_relay(monkeypatch, tmp_path, seen,
                                               use_quic):
    data = open(_two_stream_ts(tmp_path / 'two.ts', 7), 'rb').read()
    await _through_relay(
        monkeypatch, _BASE_PORT + 2 + (not use_quic), use_quic, 4,
        ['--ts', _paced_fifo(tmp_path, data, 7), '--ts-cadence',
         'fps=30,gop=30', '--stats', '0'],
        ['--analyze', '-t', '8', '-i', '1'])
    _check_aligned(seen, ('video', 'video_1'), 45)
    assert any(n == 'audio' for n, *_ in seen)


@requires_certs
async def test_sub_media_writes_both_loc_video_tracks(monkeypatch, ladder,
                                                      tmp_path):
    out = tmp_path / 'out'
    await _through_relay(
        monkeypatch, _BASE_PORT + 4, True, 3,
        ['--mp4', ladder[0], '--mp4', ladder[1], '--stats', '0'],
        ['--out', str(out), '-t', '6'])
    if shutil.which('ffprobe') is None:
        pytest.skip('ffprobe not installed')
    for name, size in (('video', '320,180'), ('video_1', '160,96')):
        path = out / f'{name}.h264'
        assert path.stat().st_size > 0, name
        probe = subprocess.run(
            ['ffprobe', '-v', 'error', '-count_frames', '-show_entries',
             'stream=width,height,nb_read_frames', '-of', 'csv=p=0',
             str(path)], check=True, capture_output=True, text=True)
        width, height, frames = probe.stdout.strip().split(',')
        assert f'{width},{height}' == size and int(frames) > 10, name
