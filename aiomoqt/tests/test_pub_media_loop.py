"""pub_media --mp4 timing: the edit list (priming and delay), one loop
period for every track of a clip, group IDs across loop seams, and
near-regular audio mapped on its frame grid."""
import asyncio
import logging
import random
import struct
from fractions import Fraction
from types import SimpleNamespace as NS

import pytest

from aiomoqt.media.loc import LocTrackPublisher
from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_pub_media_frindell import _argv
from aiomoqt.tests.test_pub_media_service import _serve, _stop, _until
from aiomoqt.tools import sub_media
from aiomoqt.utils.media_stats import MediaAnalysis
from aiomoqt.media import sources
from aiomoqt.media.sources import Mp4Reader, _boxes, _find
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tools import pub_media
from aiomoqt.utils.timing import OBJECT_TIMESTAMP

_FPS = 30


def _clip(path, frames, acodec='aac'):
    """No B-frames, a key frame every second, audio of distinct frames."""
    av = pytest.importorskip('av')
    out = av.open(str(path), 'w', format='mp4')
    v = out.add_stream('libx264', rate=_FPS, options={
        'g': str(_FPS), 'keyint_min': str(_FPS), 'sc_threshold': '0',
        'bf': '0', 'preset': 'ultrafast'})
    v.width, v.height, v.pix_fmt = 160, 96, 'yuv420p'
    a = out.add_stream(acodec, rate=48000, layout='mono')
    for i in range(frames):
        f = av.VideoFrame(160, 96, 'yuv420p')
        for plane, val in zip(f.planes, (16 + (i * 7) % 200, 128, 128)):
            plane.update(bytes([val]) * plane.buffer_size)
        f.pts, f.time_base = i, Fraction(1, _FPS)
        for pkt in v.encode(f):
            out.mux(pkt)
    n = a.codec_context.frame_size or 1024
    rng = random.Random(1)
    for i in range(frames * 48000 // _FPS // n):
        f = av.AudioFrame(format='fltp', layout='mono', samples=n)
        f.planes[0].update(struct.pack(
            f'<{n}f', *(rng.uniform(-0.3, 0.3) for _ in range(n))))
        f.sample_rate, f.pts, f.time_base = 48000, i * n, Fraction(1, 48000)
        for pkt in a.encode(f):
            out.mux(pkt)
    for s in (v, a):
        for pkt in s.encode():
            out.mux(pkt)
    out.close()
    return str(path)


def _audio_elst(data):
    """Header offsets of moov, the audio trak and its edts, and the span
    of its elst box (header included)."""
    moov = _find(data, 0, len(data), b'moov')
    traks = [(b, e) for t, b, e in _boxes(data, *moov) if t == b'trak']
    body, end = traks[1]
    edts = _find(data, body, end, b'edts')
    elst = _find(data, *edts, b'elst')
    return [moov[0] - 8, body - 8, edts[0] - 8], (elst[0] - 8, elst[1])


def _with_audio_elst(data, entries, version=0):
    """`data` with the audio track's elst replaced (moov follows mdat, so
    no chunk offset moves)."""
    parents, (start, end) = _audio_elst(data)
    assert data.find(b'mdat') < parents[0], "moov before mdat"
    fmt = '>Qqhh' if version else '>Iihh'
    body = bytes([version, 0, 0, 0]) + struct.pack('>I', len(entries))
    body += b''.join(struct.pack(fmt, *e) for e in entries)
    box = struct.pack('>I4s', 8 + len(body), b'elst') + body
    out = bytearray(data[:start] + box + data[end:])
    delta = len(box) - (end - start)
    for pos in parents:
        size = struct.unpack_from('>I', out, pos)[0]
        struct.pack_into('>I', out, pos, size + delta)
    return bytes(out)


class _Rec:
    """A track that records what _send hands it."""
    demand = [1]
    timescale = None

    def __init__(self, frindell=None):
        self.frindell = frindell
        self.sent = []

    async def send_frame(self, payload, *, key_frame, timestamp,
                         group_id=None, capture_us=None):
        self.sent.append(NS(payload=payload, key=key_frame, ts=timestamp,
                            hint=group_id, capture=capture_us))

    async def finish(self):
        pass


async def _feed(path, passes, mapping=False):
    """Loop a clip `passes` times as pub_media does: (video, audio)
    recorders and their _Mp4Pass."""
    reader = Mp4Reader(path)
    mp4s = pub_media._mp4_passes(reader, reader.audio, True)
    _, wall0 = pub_media._media_clock([p.timescale for p in mp4s], True)
    period = mp4s[0].period
    args = NS(duration=float(passes * period) - 0.001, no_pace=True,
              loop=True)
    recs = []
    for mp4, frames in zip(mp4s, (None, 1)):
        rec = _Rec(pub_media._frindell_for(mp4, wall0) if mapping else None)
        await pub_media._feed_mp4_track(
            rec, mp4, args, pub_media._TrackStats(), group_frames=frames,
            clock=(0, wall0))
        recs.append(rec)
    return recs, mp4s


def _numbered(rec):
    """(group, object, sent) as LocTrackPublisher numbers them."""
    group, obj, out = -1, 0, []
    for s in rec.sent:
        if s.key:
            group = LocTrackPublisher._next_group(NS(group_id=s.hint), group)
            obj = 0
        else:
            obj += 1
        out.append((group, obj, s))
    return out


# -- edit list -----------------------------------------------------------

@pytest.mark.parametrize('version', [0, 1])
def test_edit_list_is_parsed(version):
    fmt = '>Qqhh' if version else '>Iihh'
    entries = [(500, -1, 1, 0), (144_000, 1024, 1, 0)]
    body = bytes([version, 0, 0, 0]) + struct.pack('>I', 2) + b''.join(
        struct.pack(fmt, *e) for e in entries)
    assert sources.parse_elst(body, 0, len(body)) == entries
    assert sources.edit_offset(entries, 1000) == (Fraction(1, 2), 1024)
    assert sources.edit_offset(entries[1:], 1000) == (0, 1024)


@pytest.mark.parametrize('entries', [
    [(100, 0, 2, 0)],                         # rate 2
    [(100, 0, 1, 0), (100, 50, 1, 0)],        # two media edits
    [(100, 0, 1, 0), (100, -1, 1, 0)],        # trailing empty edit
    [(100, -1, 1, 0)],                        # no media edit
    [],
])
def test_other_edit_list_shapes_are_unsupported(entries):
    assert sources.edit_offset(entries, 1000) is None


def test_clip_has_aac_priming_in_its_edit_list(tmp_path):
    audio = Mp4Reader(_clip(tmp_path / 'c.mp4', 30)).audio
    assert (audio.media_time, audio.edit_delay) == (1024, 0)


def test_unsupported_edit_list_is_ignored_with_a_warning(tmp_path, caplog):
    path = tmp_path / 'c.mp4'
    data = open(_clip(path, 30), 'rb').read()
    path.write_bytes(_with_audio_elst(data, [(143_360, 1024, 2, 0)]))
    with caplog.at_level(logging.WARNING, logger='aiomoqt.media.sources'):
        audio = Mp4Reader(str(path)).audio
    assert audio.media_time == 0
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1 and 'track 2' in warned[0].getMessage()


@pytest.mark.parametrize('version', [0, 1])
def test_empty_edit_delays_the_track(tmp_path, version):
    path = tmp_path / 'c.mp4'
    data = open(_clip(path, 30), 'rb').read()
    mvhd = _find(data, 0, len(data), b'moov', b'mvhd')
    movie = struct.unpack_from('>I', data, mvhd[0] + 12)[0]
    path.write_bytes(_with_audio_elst(
        data, [(movie // 4, -1, 1, 0), (movie, 1024, 1, 0)], version))
    audio = Mp4Reader(str(path)).audio
    starts = [t for _, t in audio.presented()]
    assert starts[:2] == [Fraction(1, 4), Fraction(1, 4) + Fraction(1024,
                                                                    48000)]


# -- looping -------------------------------------------------------------

async def test_no_priming_packet_is_sent_on_any_pass(tmp_path):
    path = _clip(tmp_path / 'c.mp4', 90)
    priming = next(Mp4Reader(path).audio.samples()).payload
    (video, audio), _ = await _feed(path, 3)
    assert len(video.sent) == 3 * 90
    assert len(audio.sent) == 3 * 140
    assert all(s.payload != priming for s in audio.sent)


async def test_loop_passes_start_together_and_groups_increase(tmp_path):
    # 2.4 s: the last GOP is short, so a seam falls off the group grid.
    path = _clip(tmp_path / 'c.mp4', 72)
    (video, audio), _ = await _feed(path, 3, mapping=True)
    starts = []
    for rec in (video, audio):
        first = rec.sent[0].payload
        starts.append([s.ts for s in rec.sent if s.payload == first])
    assert [len(s) for s in starts] == [3, 3]
    assert all(abs(v - a) <= 1_000_000 / 48000 for v, a in zip(*starts))
    for rec in (video, audio):
        hints = [s.hint for s in rec.sent if s.key]
        assert all(a < b for a, b in zip(hints, hints[1:]))
        assert [g for g, o, _ in _numbered(rec) if o == 0] == hints


async def test_whole_gop_loop_needs_no_object_timestamp(tmp_path):
    path = _clip(tmp_path / 'c.mp4', 90, acodec='libopus')
    recs, _ = await _feed(path, 3, mapping=True)
    for rec in recs:
        assert rec.frindell.mapped
        for group, obj, s in _numbered(rec):
            assert OBJECT_TIMESTAMP not in rec.frindell.object_properties(
                group, obj, s.capture), (group, obj)


# -- near-regular audio --------------------------------------------------

def _audio(starts, timescale):
    samples = [(NS(key_frame=True, duration=0, payload=bytes([i % 256])),
                Fraction(t, timescale)) for i, t in enumerate(starts)]
    return NS(presented=lambda: iter(samples), timescale=timescale,
              samplerate=48000)


@pytest.mark.parametrize('starts,timescale', [
    ([(n * 64 + 1) // 3 for n in range(300)], 1000),    # 21/22 ms
    ([n * 1024 + (n == 50) for n in range(300)], 48000),  # one odd
])
async def test_near_regular_audio_maps_on_its_frame_grid(starts, timescale):
    mp4 = pub_media._Mp4Pass(_audio(starts, timescale), Fraction(0),
                             samplerate=48000)
    assert mp4.cadence == (48000, 1024, 1)
    assert mp4.timescale == 48000
    m = pub_media._frindell_for(mp4, 1_767_225_600_000_000)
    rec = _Rec(m)
    await pub_media._feed_mp4_track(
        rec, mp4, NS(duration=10.0, no_pace=True, loop=False),
        pub_media._TrackStats(), group_frames=1,
        clock=(0, 1_767_225_600_000_000))
    assert len(rec.sent) == len(starts)
    for group, obj, s in _numbered(rec):
        assert m.object_properties(group, obj, s.capture) == {}


def test_irregular_audio_falls_back_to_explicit_timestamps():
    starts = [n * 1024 + (n >= 150) * 614 for n in range(300)]  # 0.6 frame
    mp4 = pub_media._Mp4Pass(_audio(starts, 48000), Fraction(0),
                             samplerate=48000)
    assert mp4.cadence is None
    assert not pub_media._frindell_for(mp4, 0).mapped


# -- end to end ----------------------------------------------------------

_PORT = 21950


@requires_certs
async def test_looped_clip_through_the_relay_keeps_audio_on_video(
        tmp_path, monkeypatch):
    path = _clip(tmp_path / 'c.mp4', 90)                  # 3 s, AAC + H.264
    reader = Mp4Reader(path)
    v_first = next(reader.video.samples()).payload
    a_first = list(reader.audio.samples())[1].payload    # after the priming
    seen = []
    orig = MediaAnalysis.on_arrival

    def record(self, name, msg, recv_us, group_id, subgroup_id, frame):
        if frame is not None:
            seen.append((name, group_id, frame.timestamp,
                         bytes(frame.payload)))
        return orig(self, name, msg, recv_us, group_id, subgroup_id, frame)
    monkeypatch.setattr(MediaAnalysis, 'on_arrival', record)
    url = f'moqt://localhost:{_PORT}/'
    ns = 'loop/e2e'
    pub_args = _argv(monkeypatch, 'pub_media', url, '-k', '--draft', '22',
                     '-t', '10', '--stats', '0', '--pub-ns', '-N', ns,
                     '--mp4', path, '--loop')
    sub_args = _argv(monkeypatch, 'sub_media', url, '-k', '--draft', '22',
                     '-N', ns, '--analyze', '-t', '12', '-i', '1')
    srv, handle = await _serve(_PORT, True, 22)
    try:
        pub = asyncio.ensure_future(pub_media.run(pub_args))
        await _until(lambda: relay._announced or pub.done(), 10)
        sub = asyncio.ensure_future(sub_media.run(sub_args))
        await asyncio.wait_for(pub, 15)
        await asyncio.sleep(0.5)
        sub.cancel()
    finally:
        await _stop(srv, handle)
    frame_us = 1024 * 1_000_000 / 48000
    seams = {}
    for kind, first in (('video', v_first), ('audio', a_first)):
        objs = sorted((ts, g) for n, g, ts, _ in seen if n == kind)
        assert objs and all(ts is not None for ts, _ in objs), kind
        groups = [g for _, g in objs]
        firsts = [g for g, prev in zip(groups[1:], groups) if g != prev]
        assert all(b > a for a, b in zip(firsts, firsts[1:])), kind
        assert all(b >= a for a, b in zip(groups, groups[1:])), kind
        seams[kind] = sorted(ts for n, _, ts, p in seen
                             if n == kind and p == first)
    video, audio = seams['video'], seams['audio']
    pairs = [(v, min(audio, key=lambda a: abs(a - v))) for v in video]
    assert len(pairs) >= 2 and audio, seams
    for v, a in pairs:
        assert abs(a - v) < frame_us, (v, a)
    for (v0, a0), (v1, a1) in zip(pairs, pairs[1:]):
        assert abs((a1 - a0) - (v1 - v0)) < frame_us, (v0, v1, a0, a1)
