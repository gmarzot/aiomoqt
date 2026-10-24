"""pub_media as the moq-test-tracks reference engine: Opus and H.265
sources, object-to-subgroup and audio group mapping, the datagram size
limit, signal shutdown and --stats-json, end to end through the interop
relay into sub_media --analyze where a session is involved."""
import asyncio
import contextlib
import csv
import json
import os
import threading
import time
from fractions import Fraction
from types import SimpleNamespace

import pytest

from aiomoqt.media.mpegts import TsDemuxer, opus_samples, opus_split
from aiomoqt.media.sources import Mp4Reader
from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_pub_media_frindell import _argv, _through_relay
from aiomoqt.tools import pub_media

_FPS = 30
_BASE_PORT = 16800


def _encode(path, fmt, *, vcodec='libx264', acodec='libopus',
            layout='stereo', seconds=3):
    """A clip as the moq-test-tracks generator makes them: no B-frames,
    a key frame every second, 20 ms Opus or AAC."""
    av = pytest.importorskip('av')
    out = av.open(str(path), 'w', format=fmt)
    if vcodec == 'libx265':
        opts = {'preset': 'ultrafast', 'x265-params':
                f'log-level=error:bframes=0:keyint={_FPS}:'
                f'min-keyint={_FPS}:scenecut=0'}
    else:
        opts = {'g': str(_FPS), 'keyint_min': str(_FPS),
                'sc_threshold': '0', 'bf': '0', 'preset': 'ultrafast'}
    v = out.add_stream(vcodec, rate=_FPS, options=opts)
    v.width, v.height, v.pix_fmt = 160, 96, 'yuv420p'
    if fmt == 'mp4' and vcodec == 'libx265':
        v.codec_tag = 'hvc1'
    a = out.add_stream(acodec, rate=48000, layout=layout)
    for i in range(seconds * _FPS):
        f = av.VideoFrame(160, 96, 'yuv420p')
        for plane, val in zip(f.planes, (16 + (i * 7) % 200, 128, 128)):
            plane.update(bytes([val]) * plane.buffer_size)
        f.pts, f.time_base = i, Fraction(1, _FPS)
        for pkt in v.encode(f):
            out.mux(pkt)
    n = a.codec_context.frame_size or 1024
    for i in range(seconds * 48000 // n):
        f = av.AudioFrame(format=a.codec_context.format.name, layout=layout,
                          samples=n)
        for p in f.planes:
            p.update(b'\x00' * p.buffer_size)
        f.sample_rate, f.pts, f.time_base = 48000, i * n, Fraction(1, 48000)
        for pkt in a.encode(f):
            out.mux(pkt)
    for s in (v, a):
        for pkt in s.encode():
            out.mux(pkt)
    out.close()
    return str(path)


def _paced_fifo(tmp_path, data: bytes, seconds: float) -> str:
    """A FIFO fed with `data` spread over `seconds`, as a live encoder
    feeds pub_media --ts."""
    path = str(tmp_path / 'live.ts')
    os.mkfifo(path)

    def _write():
        step = 188 * 7
        chunks = [data[i:i + step] for i in range(0, len(data), step)]
        try:
            with open(path, 'wb', buffering=0) as fh:
                t0 = time.monotonic()
                for k, chunk in enumerate(chunks):
                    delay = t0 + seconds * k / len(chunks) - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    fh.write(chunk)
        except (BrokenPipeError, OSError):
            pass
    threading.Thread(target=_write, daemon=True).start()
    return path


def _pub_args(monkeypatch, *argv):
    return _argv(monkeypatch, 'pub_media', 'moqt://localhost:1/', *argv)


def _summary(report):
    with open(report) as fh:
        return {r['track']: r for r in csv.DictReader(fh)
                if r['kind'] == 'summary'}


async def _analyze(monkeypatch, tmp_path, port, pub_argv, *, draft=18,
                   use_quic=True, sub_argv=()):
    """Run pub_media through the relay into sub_media --analyze; the
    per-track summary rows."""
    url = f"{'moqt' if use_quic else 'https'}://localhost:{port}/"
    ns = f'mtt/{port}'
    report = tmp_path / f'analyze-{port}.csv'
    rc = await _through_relay(
        monkeypatch, url, port, draft, use_quic, ['-N', ns, *pub_argv],
        ['-N', ns, '--analyze', '-t', '2', '-i', '1', '--report',
         str(report), *sub_argv])
    assert rc == 0
    return _summary(report)


# -- T1: Opus -----------------------------------------------------------

@pytest.mark.parametrize('layout,channels', [('mono', 1), ('stereo', 2)])
def test_mp4_opus_track_is_read(tmp_path, monkeypatch, layout, channels):
    clip = _encode(tmp_path / 'o.mp4', 'mp4', layout=layout)
    audio = Mp4Reader(clip).audio
    assert audio.codec_string == 'opus' and audio.config is None
    assert (audio.channels, audio.samplerate) == (channels, 48000)
    assert {s.duration for s in list(audio.samples())[:-1]} == {960}
    args = _pub_args(monkeypatch, '--mp4', clip)
    cat = pub_media._build_catalog(args, None, audio)
    entry = cat.find('audio')
    assert (entry.codec, entry.samplerate, entry.channelConfig) == (
        'opus', 48000, str(channels))
    assert entry.initRef is None and not cat.initDataList


@pytest.mark.parametrize('layout,channels', [('mono', 1), ('stereo', 2)])
def test_ts_opus_is_demuxed_one_unit_per_packet(tmp_path, layout,
                                                channels):
    data = open(_encode(tmp_path / 'o.ts', 'mpegts', layout=layout),
                'rb').read()
    d = TsDemuxer()
    units = []
    for i in range(0, len(data), 1316):
        units += d.feed(data[i:i + 1316])
    units += d.close()
    audio = [u for u in units if u.kind == 'audio']
    assert d.audio_codec == 'opus' and d.audio_channels == channels
    assert d.audio_samplerate == 48000 and d.audio_asc is None
    assert len(audio) >= 145
    assert {b.pts - a.pts for a, b in zip(audio, audio[1:])} == {1800}
    assert all(opus_samples(u.payload) == 960 for u in audio)
    view = pub_media._LiveAudio(d)
    assert (view.codec_string, view.channels) == ('opus', channels)


def test_opus_control_headers_are_stripped():
    pkt = bytes([0xFC]) + b'\x11' * 300
    trimmed = bytes([0x7F, 0xF0, 0xFF, len(pkt) - 255]) + b'\x01\x38' + pkt
    plain = bytes([0x7F, 0xE0, 0x05]) + bytes([0xFC]) + b'\x22' * 4
    assert opus_split(trimmed + plain) == [pkt, bytes([0xFC]) + b'\x22' * 4]
    assert opus_samples(bytes([0xFC])) == 960       # CELT FB 20 ms
    assert opus_samples(bytes([0x0B, 0x03])) == 2880  # SILK 60 ms, 2 frames


@requires_certs
@pytest.mark.parametrize('source,packaging,layout,use_quic', [
    ('mp4', 'loc', 'mono', True),
    ('mp4', 'cmaf', 'stereo', False),
    ('mp4', 'locmaf', 'mono', True),
    ('ts', 'loc', 'stereo', True),
    ('ts', 'loc', 'mono', False),
])
async def test_opus_reaches_analyze_at_50_objects_per_second(
        tmp_path, monkeypatch, capsys, source, packaging, layout, use_quic):
    port = _BASE_PORT + 2 * ['mp4-loc', 'mp4-cmaf', 'mp4-locmaf', 'ts-loc'
                             ].index(f'{source}-{packaging}') + (not use_quic)
    if source == 'mp4':
        clip = _encode(tmp_path / 'o.mp4', 'mp4', layout=layout)
        pub = ['--mp4', clip, '--loop', '--packaging', packaging]
    else:
        data = open(_encode(tmp_path / 'o.ts', 'mpegts', layout=layout,
                            seconds=6), 'rb').read()
        pub = ['--ts', _paced_fifo(tmp_path, data, 6)]
    rows = await _analyze(monkeypatch, tmp_path, port,
                          [*pub, '--timestamp-mapping'], use_quic=use_quic,
                          sub_argv=['--show-catalog'])
    catalog = capsys.readouterr().err
    assert '"codec": "opus"' in catalog
    assert f'"channelConfig": "{1 if layout == "mono" else 2}"' in catalog
    audio = rows['audio']
    assert int(audio['objects']) > 20, audio
    assert 30 <= float(audio['obj_rate']) <= 70, audio
    # LOCMAF audio defaults to ~1 s groups.
    assert float(audio['gop_p50']) == (50 if packaging == 'locmaf' else 1)
    assert int(rows['video']['objects']) > 0


# -- stats line ----------------------------------------------------------

class _Out:
    def __init__(self):
        self.written = []

    async def write(self, group_id, object_id, payload, **kw):
        self.written.append((group_id, object_id))

    def reset_group(self):
        pass


async def test_stats_line_counts_objects_produced_not_frames_discarded(
        capsys):
    from aiomoqt.media import LocTrackPublisher
    track = LocTrackPublisher(None, 'ns', 'video')
    track._subs[0].forward = track._subs[0].generating = True
    st = pub_media._TrackStats()
    for key in (False, False, False, True, False):
        await track.send_frame(b'x', key_frame=key, timestamp=1)
        st.count(b'x')
    await track.finish()
    out = _Out()
    await track.produce(out)
    assert len(out.written) == 2 and track.frames_dropped == 3
    pub = SimpleNamespace(tracks={'video': track}, sessions=[None])
    task = asyncio.ensure_future(
        pub_media._report_stats({'video': st}, 0.01, pub))
    await asyncio.sleep(0.05)
    task.cancel()
    line = capsys.readouterr().out.splitlines()[0]
    assert 'video: 2 obj' in line and 'discarded 3' in line, line


# -- T2/T3: object mapping -------------------------------------------------

@pytest.fixture
def arrivals(monkeypatch):
    """(track, group, subgroup, object) of every media object sub_media
    --analyze sees; subgroup None for a datagram."""
    from aiomoqt.utils.media_stats import MediaAnalysis
    seen = []
    orig = MediaAnalysis.on_arrival

    def record(self, name, msg, recv_us, group_id, subgroup_id, frame):
        if frame is not None:
            seen.append((name, group_id, subgroup_id, msg.object_id))
        return orig(self, name, msg, recv_us, group_id, subgroup_id, frame)
    monkeypatch.setattr(MediaAnalysis, 'on_arrival', record)
    return seen


@pytest.fixture(scope='module')
def h264_aac(tmp_path_factory):
    return _encode(tmp_path_factory.mktemp('mtt') / 'clip.mp4', 'mp4',
                   acodec='aac', layout='mono')


def _subgroups(seen, track):
    return [(g, sg, o) for n, g, sg, o in seen if n == track]


@requires_certs
@pytest.mark.parametrize('packaging,use_quic', [
    ('loc', True), ('loc', False), ('cmaf', True)])
async def test_video_subgroup_per_object(tmp_path, monkeypatch, arrivals,
                                         h264_aac, packaging, use_quic):
    port = _BASE_PORT + 20 + 2 * (packaging == 'cmaf') + (not use_quic)
    rows = await _analyze(
        monkeypatch, tmp_path, port,
        ['--mp4', h264_aac, '--loop', '--packaging', packaging,
         '--video-subgroups', 'object'], use_quic=use_quic)
    video = _subgroups(arrivals, 'video')
    assert int(rows['video']['objects']) > 10 and video
    assert all(sg == o for _, sg, o in video), video[:5]
    assert len({sg for _, sg, _ in video}) > 10
    # Audio keeps its own mapping: one subgroup stream per group.
    assert {sg for _, sg, _ in _subgroups(arrivals, 'audio')} == {0}


@requires_certs
async def test_video_subgroups_default_to_one_per_group(
        tmp_path, monkeypatch, arrivals, h264_aac):
    await _analyze(monkeypatch, tmp_path, _BASE_PORT + 24,
                   ['--mp4', h264_aac, '--loop'])
    assert {sg for _, sg, _ in _subgroups(arrivals, 'video')} == {0}


@pytest.mark.parametrize('argv,why', [
    (['--mp4', 'x.mp4', '--packaging', 'locmaf', '--video-subgroups',
      'object'], 'does not apply to locmaf'),
    (['--input', 'x.m3u8', '--video-subgroups', 'object'],
     'does not apply to --input'),
])
def test_video_subgroups_object_refused_where_it_cannot_apply(
        monkeypatch, capsys, argv, why):
    with pytest.raises(SystemExit):
        _pub_args(monkeypatch, *argv)
    assert why in capsys.readouterr().err


@requires_certs
@pytest.mark.parametrize('packaging', ['loc', 'cmaf', 'locmaf'])
@pytest.mark.parametrize('per_group', [1, 10, 50])
async def test_audio_frames_per_group(tmp_path, monkeypatch, arrivals,
                                      packaging, per_group):
    port = (_BASE_PORT + 30 + 3 * [1, 10, 50].index(per_group)
            + ['loc', 'cmaf', 'locmaf'].index(packaging))
    clip = _encode(tmp_path / 'o.mp4', 'mp4', layout='stereo')
    rows = await _analyze(
        monkeypatch, tmp_path, port,
        ['--mp4', clip, '--loop', '--packaging', packaging,
         '--audio-frames-per-group', str(per_group), '--timestamp-mapping'],
        use_quic=per_group != 10)
    audio = rows['audio']
    assert float(audio['gop_p50']) == per_group, audio
    assert 30 <= float(audio['obj_rate']) <= 70, audio
    assert audio['ts_malformed'] == '0'
    groups = {}
    for g, sg, o in _subgroups(arrivals, 'audio'):
        groups.setdefault(g, []).append(o)
        assert sg == 0
    assert all(max(objs) < per_group for objs in groups.values())


@requires_certs
@pytest.mark.parametrize('source,use_quic', [('ts', True), ('tone', False)])
async def test_audio_subgroup_per_object_in_groups_of_n(
        tmp_path, monkeypatch, arrivals, h264_aac, source, use_quic):
    port = _BASE_PORT + 40 + (source == 'tone')
    if source == 'ts':
        data = open(_encode(tmp_path / 'o.ts', 'mpegts', seconds=6),
                    'rb').read()
        pub = ['--ts', _paced_fifo(tmp_path, data, 6)]
    else:
        pub = ['--mp4', h264_aac, '--loop', '--tone']
    rows = await _analyze(
        monkeypatch, tmp_path, port,
        [*pub, '--audio-frames-per-group', '10', '--audio-subgroups',
         'object'], use_quic=use_quic)
    assert float(rows['audio']['gop_p50']) == 10
    audio = _subgroups(arrivals, 'audio')
    assert all(sg == o for _, sg, o in audio)
    assert {sg for _, sg, _ in audio} == set(range(10))
    assert {sg for _, sg, _ in _subgroups(arrivals, 'video')} == {0}


@pytest.mark.parametrize('argv,why', [
    (['--audio-frames-per-group', '0'], '1-100'),
    (['--audio-frames-per-group', '101'], '1-100'),
    (['--mp4', 'x.mp4', '--packaging', 'locmaf', '--audio-subgroups',
      'object'], 'does not apply to locmaf'),
    (['-D', '--audio-subgroups', 'object'], 'exclusive'),
    (['--input', 'x.m3u8', '--audio-frames-per-group', '5'],
     'not apply to --input'),
])
def test_audio_mapping_refused_where_it_cannot_apply(monkeypatch, capsys,
                                                     argv, why):
    with pytest.raises(SystemExit):
        _pub_args(monkeypatch, *argv)
    assert why in capsys.readouterr().err


# -- T4: datagram size limit ---------------------------------------------

class _DgramSession:
    _profile = None

    def __init__(self, limit):
        self.limit = limit
        self.dgrams = []
        self.streams = 0

    def datagram_max_payload(self):
        return self.limit

    async def dgram_write_drain(self, data):
        self.dgrams.append(data)

    async def open_uni_stream(self):
        self.streams += 1
        return 4 * self.streams


async def test_oversize_datagram_is_refused_not_streamed():
    from aiomoqt.context import profile_for
    from aiomoqt.delivery import (
        DatagramTooLarge, FanoutDelivery, StreamMapping, SubgroupDelivery,
    )
    session = _DgramSession(200)
    session._profile = profile_for(18)
    delivery = SubgroupDelivery(session, 1, mapping=StreamMapping.DATAGRAM)
    assert await delivery.write(0, 0, b'x' * 100, group_start=True)
    with pytest.raises(DatagramTooLarge) as e:
        await delivery.write(1, 0, b'x' * 200, group_start=True)
    assert e.value.limit == 200 and e.value.size > 200
    assert len(session.dgrams) == 1 and session.streams == 0
    # Through a fan-out lane, the error ends the track's production.
    out = FanoutDelivery()
    out.add(SubgroupDelivery(session, 1, mapping=StreamMapping.DATAGRAM),
            joining=False)
    await out.write(2, 0, b'x' * 300, group_start=True)
    await asyncio.sleep(0.01)
    assert isinstance(out.error, DatagramTooLarge)
    with pytest.raises(DatagramTooLarge):
        await out.write(3, 0, b'x', group_start=True)
    assert len(session.dgrams) == 1 and session.streams == 0
    await out.close()


@pytest.fixture
def publish_dones(monkeypatch):
    from aiomoqt.track import PublishedTrack
    seen = []
    orig = PublishedTrack._send_publish_done

    def record(self, session, status_code=0x2, final=True, reason=None):
        seen.append((self.trackname, int(status_code), reason))
        return orig(self, session, status_code, final, reason=reason)
    monkeypatch.setattr(PublishedTrack, '_send_publish_done', record)
    return seen


@requires_certs
async def test_datagram_too_large_exits_64(tmp_path, monkeypatch, arrivals,
                                           publish_dones, h264_aac, capsys):
    from aiomoqt.tests._certs import CERT, KEY
    from aiomoqt.tools import moq_interop_relay as relay
    from aiomoqt.tools import sub_media
    port = _BASE_PORT + 50
    url = f'moqt://localhost:{port}/'
    relay._announced.clear()
    relay._tracks.clear()
    handle = await relay._build_server('localhost', port, CERT, KEY,
                                       use_quic=True, draft=18).serve()
    # pcm-s16 tone: 3840 B per 20 ms frame, over any datagram limit.
    pub_args = _argv(monkeypatch, 'pub_media', url, '-k', '--draft', '18',
                     '-t', '6', '--stats', '0', '--pub-ns', '-N', 'mtt/dg',
                     '--mp4', h264_aac, '--loop', '--tone', '-D')
    sub_args = _argv(monkeypatch, 'sub_media', url, '-k', '--draft', '18',
                     '-N', 'mtt/dg', '--analyze', '-t', '3', '-i', '1')

    async def _sub():
        for _ in range(200):
            if relay._announced:
                break
            await asyncio.sleep(0.05)
        await sub_media.run(sub_args)
    # SystemExit must not escape a task: pub_media runs in this one.
    sub = asyncio.create_task(_sub())
    try:
        with pytest.raises(SystemExit) as e:
            await asyncio.wait_for(pub_media.run(pub_args), 10)
        assert e.value.code == 64
        await asyncio.wait_for(sub, 10)
    finally:
        if not sub.done():
            sub.cancel()
        handle.close()
        relay._announced.clear()
        relay._tracks.clear()
    assert 'exceeds the' in capsys.readouterr().out
    assert not _subgroups(arrivals, 'audio')
    done = [d for d in publish_dones if d[0] == 'audio']
    assert done and done[0][1] == 0x0 and 'datagram limit' in done[0][2]


@requires_certs
async def test_aac_and_opus_fit_datagrams(tmp_path, monkeypatch, arrivals,
                                          h264_aac):
    opus = _encode(tmp_path / 'o.mp4', 'mp4', layout='stereo')
    for k, clip in enumerate((h264_aac, opus)):
        arrivals.clear()
        rows = await _analyze(monkeypatch, tmp_path, _BASE_PORT + 51 + k,
                              ['--mp4', clip, '--loop', '-D'])
        assert int(rows['audio']['objects']) > 20
        assert {sg for _, sg, _ in _subgroups(arrivals, 'audio')} == {None}


# -- T5: signals -----------------------------------------------------------

async def _with_relay(port, use_quic, body):
    from aiomoqt.tests._certs import CERT, KEY
    from aiomoqt.tools import moq_interop_relay as relay
    relay._announced.clear()
    relay._tracks.clear()
    handle = await relay._build_server('localhost', port, CERT, KEY,
                                       use_quic=use_quic, draft=18).serve()
    try:
        return await body(relay)
    finally:
        handle.close()
        relay._announced.clear()
        relay._tracks.clear()


@requires_certs
@pytest.mark.parametrize('sig,use_quic', [
    ('SIGTERM', True), ('SIGTERM', False), ('SIGINT', True)])
async def test_signal_ends_tracks_with_publish_done(
        monkeypatch, arrivals, publish_dones, h264_aac, sig, use_quic):
    import signal
    from aiomoqt.tools import sub_media
    port = _BASE_PORT + 60 + 2 * (sig == 'SIGINT') + (not use_quic)
    url = f"{'moqt' if use_quic else 'https'}://localhost:{port}/"
    pub_args = _argv(monkeypatch, 'pub_media', url, '-k', '--draft', '18',
                     '-t', '30', '--stats', '0', '--pub-ns', '-N', 'mtt/sig',
                     '--mp4', h264_aac, '--loop')
    sub_args = _argv(monkeypatch, 'sub_media', url, '-k', '--draft', '18',
                     '-N', 'mtt/sig', '--analyze', '-t', '4', '-i', '1')

    async def body(relay):
        async def _sub_then_signal():
            while not relay._announced:
                await asyncio.sleep(0.05)
            sub = asyncio.ensure_future(sub_media.run(sub_args))
            await asyncio.sleep(2.0)
            os.kill(os.getpid(), getattr(signal, sig))
            return await sub
        driver = asyncio.ensure_future(_sub_then_signal())
        t0 = time.monotonic()
        await asyncio.wait_for(pub_media.run(pub_args), 20)
        elapsed = time.monotonic() - t0
        await asyncio.wait_for(driver, 10)
        return elapsed
    elapsed = await _with_relay(port, use_quic, body)
    assert elapsed < 10, elapsed           # not the 30 s duration
    assert {n for n, *_ in publish_dones} >= {'video', 'audio'}
    assert all(status == 0x2 for _, status, _ in publish_dones)
    assert _subgroups(arrivals, 'video') and _subgroups(arrivals, 'audio')


# -- T6: --stats-json --------------------------------------------------------

def _mtt_events(out: str):
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines and all(ln.startswith('@mtt {') for ln in lines), lines[:3]
    return [json.loads(ln[len('@mtt '):]) for ln in lines]


@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_stats_json_events(monkeypatch, capsys, h264_aac, use_quic):
    from aiomoqt.tools import sub_media
    port = _BASE_PORT + 70 + (not use_quic)
    scheme = 'moqt' if use_quic else 'https'
    url = f'{scheme}://localhost:{port}/'
    # Nothing listens there: the dial fails after ~10 s, so only once.
    dead = [] if use_quic else [f'https://localhost:{port + 5}/']
    pub_args = _argv(monkeypatch, 'pub_media', url, *dead, '-k', '--draft',
                     '18', '-t', '3', '--stats', '0.5', '--pub-ns', '-N',
                     'mtt/json', '--mp4', h264_aac, '--loop', '--stats-json')
    sub_args = _argv(monkeypatch, 'sub_media', url, '-k', '--draft', '18',
                     '-N', 'mtt/json', '--analyze', '-t', '2', '-i', '1')

    async def body(relay):
        async def _sub():
            while not relay._announced:
                await asyncio.sleep(0.05)
            return await sub_media.run(sub_args)
        sub = asyncio.ensure_future(_sub())
        await asyncio.wait_for(pub_media.run(pub_args), 20)
        return await asyncio.wait_for(sub, 10)
    await _with_relay(port, use_quic, body)
    captured = capsys.readouterr()
    events = _mtt_events(captured.out)
    assert 'publishing...' in captured.err and '[pub ' in captured.err
    kinds = [e['event'] for e in events]
    assert kinds[0] == 'ready' and kinds[-1] == 'done'
    assert events[-1]['reason'] == 'duration'
    assert events[0]['endpoints'] == [
        {'url': url, 'state': 'up', 'draft': 18}] + [
        {'url': u, 'state': 'down', 'draft': None} for u in dead]
    stats = [e for e in events if e['event'] == 'stats']
    assert len(stats) >= 3
    last = stats[-1]
    assert set(last['tracks']) == {'video', 'audio'}
    assert last['tracks']['video']['objects'] > 0
    assert last['tracks']['video']['groups'] >= 1
    assert last['tracks']['audio']['kbps'] > 0
    assert {'dropped', 'discarded'} <= set(last['tracks']['audio'])
    assert any(s['subscribers'] == 1 for s in stats)
    assert all(s['t'] > 0 for s in stats)


@requires_certs
async def test_stats_json_reports_a_lost_endpoint(monkeypatch, capsys,
                                                  h264_aac):
    from aiomoqt.tests._certs import CERT, KEY
    from aiomoqt.tools import moq_interop_relay as relay
    port = _BASE_PORT + 72
    urls = [f'moqt://localhost:{port}/', f'moqt://localhost:{port + 1}/']
    servers = [await relay._build_server('localhost', p, CERT, KEY,
                                         use_quic=True, draft=18).serve()
               for p in (port, port + 1)]
    sessions = []
    connect = pub_media.MOQTClient.connect

    @contextlib.asynccontextmanager
    async def recording(self):
        async with connect(self) as session:
            sessions.append((self.port, session))
            yield session
    monkeypatch.setattr(pub_media.MOQTClient, 'connect', recording)
    pub_args = _argv(monkeypatch, 'pub_media', *urls, '-k', '--draft', '18',
                     '-t', '4', '--stats', '0', '--pub-ns', '-N', 'mtt/lost',
                     '--mp4', h264_aac, '--loop', '--stats-json')
    try:
        pub = asyncio.ensure_future(pub_media.run(pub_args))
        await asyncio.sleep(1.5)
        dict(sessions)[port + 1].close(reason_phrase='lost in test')
        await asyncio.wait_for(pub, 20)
    finally:
        for s in servers:
            s.close()
        relay._announced.clear()
        relay._tracks.clear()
    events = _mtt_events(capsys.readouterr().out)
    lost = [e for e in events if e['event'] == 'endpoint_lost']
    assert [e['url'] for e in lost] == [urls[1]] and lost[0]['reason']
    assert events[-1] == {'event': 'done', 'reason': 'duration'}


# -- T7: H.265 ---------------------------------------------------------------

def test_mp4_hevc_track_is_read(tmp_path):
    from aiomoqt.media.cmaf import CmafChunker, init_codec_string
    video = Mp4Reader(_encode(tmp_path / 'h.mp4', 'mp4',
                              vcodec='libx265')).video
    assert video.codec_string == 'hvc1.1.6.L30.90'
    assert video.config == video.hvcc and video.config[0] == 1
    assert (video.width, video.height) == (160, 96)
    samples = list(video.samples())
    assert len(samples) == 90 and sum(s.key_frame for s in samples) == 3
    assert init_codec_string(CmafChunker(video).init_segment()) == (
        'hvc1.1.6.L30.90')


def test_ts_hevc_is_demuxed_with_parameter_sets_in_band(tmp_path):
    data = open(_encode(tmp_path / 'h.ts', 'mpegts', vcodec='libx265'),
                'rb').read()
    d = TsDemuxer()
    units = []
    for i in range(0, len(data), 1316):
        units += d.feed(data[i:i + 1316])
    units += d.close()
    video = [u for u in units if u.kind == 'video']
    assert d.video_codec == 'hevc' and d.cc_errors == 0
    assert len(video) == 90 and sum(u.key for u in video) == 3
    assert {b.pts - a.pts for a, b in zip(video, video[1:])} == {3000}
    first = video[0].payload
    assert first[4] >> 1 & 0x3F == 32           # VPS leads the first AU
    view = pub_media._LiveVideo(d.video)
    assert view.codec_string == 'hev1.1.6.L30.90'
    assert (view.width, view.height) == (160, 96)
    assert view.config[22] == 3                 # VPS, SPS, PPS arrays


@requires_certs
@pytest.mark.parametrize('source,packaging,use_quic', [
    ('mp4', 'loc', True), ('mp4', 'cmaf', False), ('mp4', 'locmaf', True),
    ('ts', 'loc', False)])
async def test_hevc_reaches_analyze(tmp_path, monkeypatch, capsys, source,
                                    packaging, use_quic):
    port = (_BASE_PORT + 80
            + ['mp4-loc', 'mp4-cmaf', 'mp4-locmaf', 'ts-loc'].index(
                f'{source}-{packaging}'))
    if source == 'mp4':
        clip = _encode(tmp_path / 'h.mp4', 'mp4', vcodec='libx265')
        pub = ['--mp4', clip, '--loop', '--packaging', packaging]
        codec = 'hvc1.1.6.L30.90'
    else:
        data = open(_encode(tmp_path / 'h.ts', 'mpegts', vcodec='libx265',
                            seconds=6), 'rb').read()
        pub = ['--ts', _paced_fifo(tmp_path, data, 6)]
        codec = 'hev1.1.6.L30.90'
    rows = await _analyze(monkeypatch, tmp_path, port,
                          [*pub, '--timestamp-mapping'], use_quic=use_quic,
                          sub_argv=['--show-catalog'])
    assert f'"codec": "{codec}"' in capsys.readouterr().err
    video = rows['video']
    assert int(video['objects']) > 20, video
    if source == 'mp4':
        # A paced FIFO feeds TS in bursts on slow runners; only the mp4 rate is steady.
        assert 25 <= float(video['obj_rate']) <= 35, video
    assert int(rows['audio']['objects']) > 0


# -- player link ---------------------------------------------------------------

@pytest.mark.parametrize("env,flag,want", [
    (None, None, None),
    ("https://player.example/g5-player/", None,
     "https://player.example/g5-player/"),
    ("https://player.example/g5-player/", "http://localhost:5173/g5-player/",
     "http://localhost:5173/g5-player/")])
def test_player_base_from_flag_then_environment(monkeypatch, env, flag, want):
    if env is None:
        monkeypatch.delenv('AIOMOQT_PLAYER_URL', raising=False)
    else:
        monkeypatch.setenv('AIOMOQT_PLAYER_URL', env)
    args = _argv(monkeypatch, 'pub_media', 'moqt://localhost:1/',
                 *(['--player-base', flag] if flag else []))
    assert args.player_base == want


@requires_certs
@pytest.mark.parametrize("base", [None, "https://player.example/g5-player/"])
async def test_player_line_only_with_a_player_url(monkeypatch, capsys,
                                                  h264_aac, base):
    port = _BASE_PORT + 90 + (base is not None)
    url = f'moqt://localhost:{port}/'
    if base is None:
        monkeypatch.delenv('AIOMOQT_PLAYER_URL', raising=False)
    else:
        monkeypatch.setenv('AIOMOQT_PLAYER_URL', base)
    pub_args = _argv(monkeypatch, 'pub_media', url, '-k', '--draft', '18',
                     '-t', '1', '--pub-ns', '-N', 'mtt/player',
                     '--mp4', h264_aac)

    async def body(relay):
        await asyncio.wait_for(pub_media.run(pub_args), 15)
    await _with_relay(port, True, body)
    players = [ln.split() for ln in capsys.readouterr().out.splitlines()
               if ln.strip().startswith('player:')]
    if base is None:
        assert players == []
    else:
        assert len(players) == 1 and players[0][1].startswith(base + '?')
