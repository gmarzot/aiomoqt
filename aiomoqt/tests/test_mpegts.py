"""MPEG-TS demux: synthetic packets built in-test (PAT/PMT/PES), plus an
ffmpeg-generated stream when ffmpeg is available."""
import shutil
import struct
import subprocess

import pytest

from aiomoqt.media.mpegts import (
    STREAM_TYPE_AAC_ADTS, STREAM_TYPE_H264, TsClock, TsDemuxer, adts_split,
)
from aiomoqt.media.sources import adts_frame

PMT_PID = 0x100
VIDEO_PID = 0x101
AUDIO_PID = 0x102
_NAL = b'\x00\x00\x00\x01'
_SPS = b'\x67\x64\x00\x1f\xac\xd9\x40\x50\x05\xbb'
_PPS = b'\x68\xeb\xe3\xcb\x22\xc0'
_ASC_STEREO = bytes([0x11, 0x90])   # AAC-LC, 48 kHz, 2 ch
_ASC_MONO = bytes([0x11, 0x88])


# ── in-test muxer ─────────────────────────────────────────────────────

def packet(pid: int, payload: bytes, *, pusi=False, cc=0) -> bytes:
    """One 188-byte packet; short payloads are padded with an adaptation field."""
    assert len(payload) <= 184
    if len(payload) < 184:
        af_len = 184 - len(payload) - 1
        af = bytes([af_len]) + (b'\x00' + b'\xff' * (af_len - 1) if af_len else b'')
        afc = 0x30
    else:
        af, afc = b'', 0x10
    hdr = bytes([0x47, (0x40 if pusi else 0) | (pid >> 8), pid & 0xFF, afc | (cc & 0xF)])
    pkt = hdr + af + payload
    assert len(pkt) == 188
    return pkt


def section(table_id: int, body: bytes) -> bytes:
    inner = struct.pack('>HBBB', 1, 0xC1, 0, 0) + body + b'\0\0\0\0'
    return b'\x00' + bytes([table_id]) + struct.pack('>H', 0xB000 | len(inner)) + inner


def pat() -> bytes:
    return packet(0, section(0x00, struct.pack('>HH', 1, 0xE000 | PMT_PID)), pusi=True)


def pmt(streams) -> bytes:
    body = struct.pack('>HH', 0xE000 | VIDEO_PID, 0xF000)
    for stype, pid in streams:
        body += bytes([stype]) + struct.pack('>HH', 0xE000 | pid, 0xF000)
    return packet(PMT_PID, section(0x02, body), pusi=True)


def pes(stream_id: int, data: bytes, pts=None, bounded=False) -> bytes:
    hdr = b''
    flags = 0
    if pts is not None:
        flags = 0x80
        hdr = bytes([0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF,
                     0x01 | ((pts >> 14) & 0xFE), (pts >> 7) & 0xFF,
                     0x01 | ((pts << 1) & 0xFE)])
    rest = bytes([0x80, flags, len(hdr)]) + hdr + data
    return b'\x00\x00\x01' + bytes([stream_id]) + struct.pack('>H', len(rest) if bounded else 0) + rest


def packetize(pid: int, data: bytes, cc0=0):
    """Split a PES over packets; returns (packets, next cc)."""
    out, cc = [], cc0
    for i in range(0, len(data), 184):
        out.append(packet(pid, data[i:i + 184], pusi=(i == 0), cc=cc))
        cc = (cc + 1) & 0xF
    return out, cc


def idr(size=300) -> bytes:
    return b'\x65\x88' + bytes(range(256)) * (size // 256 + 1)


def video_pes(nals, pts) -> bytes:
    return pes(0xE0, b''.join(_NAL + n for n in nals), pts)


def audio_pes(frames, pts, asc=_ASC_STEREO) -> bytes:
    return pes(0xC0, b''.join(adts_frame(asc, f) for f in frames), pts, bounded=True)


def av_stream():
    return pat() + pmt([(STREAM_TYPE_H264, VIDEO_PID), (STREAM_TYPE_AAC_ADTS, AUDIO_PID)])


# ── tests ──────────────────────────────────────────────────────────────

def test_pat_pmt_video_only():
    d = TsDemuxer()
    assert d.has_audio is None
    d.feed(pat() + pmt([(STREAM_TYPE_H264, VIDEO_PID)]))
    assert d.pmt_seen and d.video_pid == VIDEO_PID and d.audio_pid is None
    assert d.has_audio is False


def test_unsupported_stream_type_is_ignored():
    d = TsDemuxer()
    d.feed(pat() + pmt([(0x10, VIDEO_PID), (STREAM_TYPE_AAC_ADTS, AUDIO_PID)]))
    assert d.video_pid is None and d.audio_pid == AUDIO_PID
    assert d.unsupported == {0x10}


def test_video_au_spanning_packets_with_config():
    d = TsDemuxer()
    pkts, cc = packetize(VIDEO_PID, video_pes([b'\x09\xf0', _SPS, _PPS, idr(500)], 90_000))
    assert len(pkts) == 4                    # 514-byte IDR + params span four packets
    assert d.feed(av_stream() + b''.join(pkts[:3])) == []
    units = d.feed(pkts[3])                  # the stuffed last packet closes the PES
    assert len(units) == 1
    u = units[0]
    assert u.kind == 'video' and u.key and u.pts == 90_000
    body = idr(500)
    assert u.payload == struct.pack('>I', len(body)) + body   # AUD/SPS/PPS excluded
    assert d.video.config is not None and d.video.config[0] == 1
    assert d.config_changed is False
    units = d.feed(b''.join(packetize(VIDEO_PID, video_pes([b'\x41\x9a\x00'], 93_000), cc)[0]))
    assert len(units) == 1 and not units[0].key and units[0].pts == 93_000
    assert d.close() == []


def test_audio_frames_in_one_bounded_pes():
    d = TsDemuxer()
    d.feed(av_stream())
    pkts, _ = packetize(AUDIO_PID, audio_pes([b'\x01' * 40, b'\x02' * 40], 180_000))
    units = d.feed(b''.join(pkts))           # bounded: emitted without a next PUSI
    assert [u.kind for u in units] == ['audio', 'audio']
    assert units[0].payload == b'\x01' * 40 and units[1].payload == b'\x02' * 40
    assert units[0].pts == 180_000 and units[1].pts == 180_000 + 1920
    assert all(u.key for u in units)
    assert d.audio_asc == _ASC_STEREO and d.audio_samplerate == 48000 and d.audio_channels == 2


def test_adts_split_inverts_adts_frame():
    frames = [b'x' * 10, b'y' * 300]
    data = b''.join(adts_frame(_ASC_MONO, f) for f in frames) + b'\xff\xf1\x50'  # truncated tail
    got = adts_split(data)
    assert [g[3] for g in got] == frames
    assert got[0][:3] == (_ASC_MONO, 48000, 1)


def test_audio_config_change_sets_flag():
    d = TsDemuxer()
    d.feed(av_stream())
    d.feed(b''.join(packetize(AUDIO_PID, audio_pes([b'a' * 8], 0))[0]))
    assert d.config_changed is False
    d.feed(b''.join(packetize(AUDIO_PID, audio_pes([b'b' * 8], 1920, asc=_ASC_MONO), 1)[0]))
    assert d.config_changed is True and d.audio_channels == 1


def test_pts_wrap_is_unwrapped():
    d = TsDemuxer()
    d.feed(av_stream())
    top = (1 << 33) - 1000
    cc = 0
    for pts in (top, 500):                   # 1500 ticks apart across the wrap
        pkts, cc = packetize(AUDIO_PID, audio_pes([b'a' * 8], pts), cc)
        d.feed(b''.join(pkts))
    units = d.feed(b''.join(packetize(AUDIO_PID, audio_pes([b'a' * 8], 2000), cc)[0]))
    assert units[0].pts == (1 << 33) + 2000


def test_clock_maps_pts_to_epoch_and_reanchors_on_restart():
    wall = [1_000.0]
    clk = TsClock(restart_s=5.0, now=lambda: wall[0])
    assert clk.to_us(90_000) == 1_000_000_000
    wall[0] = 1_000.05
    assert clk.to_us(90_000 + 3000) == 1_000_000_000 + 33_333
    wall[0] = 1_010.0
    assert clk.to_us(90_000 + 90_000 * 9) == 1_000_000_000 + 9_000_000   # anchor kept
    assert clk.to_us(0) == 1_010_000_000                                 # restart: re-anchored


def test_clock_never_stamps_ahead_of_arrival():
    """A late first unit must not future-date the rest of the stream."""
    wall = [1_000.2]                       # unit 0, captured at 1000.0, arrives 200 ms late
    clk = TsClock(now=lambda: wall[0])
    assert clk.to_us(0) == 1_000_200_000
    wall[0] = 1_001.01                     # unit at +1 s arrives 10 ms after capture
    assert clk.to_us(90_000) == 1_001_010_000                            # anchor pulled back
    wall[0] = 1_002.05                     # +2 s arrives 50 ms late: anchor stays
    assert clk.to_us(180_000) == 1_002_010_000
    for i in range(3, 50):
        wall[0] = 1_000.0 + i + 0.01 + (i % 4) * 0.005
        assert clk.to_us(90_000 * i) == 1_000_010_000 + i * 1_000_000


def test_clock_tracks_a_fast_source():
    """PTS outrunning the wall clock (ffmpeg -re pacing on a fast monotonic
    clock) must not drift the stamps into the future."""
    wall = [1_000.0]
    clk = TsClock(now=lambda: wall[0])
    fast = 1e-3                            # 1000 ppm: 60 ms/min
    for i in range(30 * 120):              # 2 min at 30 fps
        media_s = i / 30
        wall[0] = 1_000.0 + 0.02 + media_s * (1 - fast)
        arrival = int(wall[0] * 1_000_000)
        stamp = clk.to_us(i * 3000)
        assert stamp <= arrival
        assert arrival - stamp < 1_000 or i == 0


def test_continuity_gap_discards_the_pes_and_recovers():
    d = TsDemuxer()
    d.feed(av_stream())
    pkts, cc = packetize(VIDEO_PID, video_pes([_SPS, _PPS, idr(500)], 0))
    d.feed(pkts[0] + pkts[2])                # middle packet lost
    assert d.cc_errors == 1
    nxt, _ = packetize(VIDEO_PID, video_pes([b'\x41\x9a\x00'], 3000), cc)
    units = d.feed(b''.join(nxt))
    assert [u.pts for u in units] == [3000]  # the damaged PES was dropped; the next is intact


def test_duplicate_packet_is_ignored():
    d = TsDemuxer()
    d.feed(av_stream())
    pkts, cc = packetize(AUDIO_PID, audio_pes([b'a' * 8], 0))
    units = d.feed(pkts[0] + pkts[0])
    assert len(units) == 1 and d.cc_errors == 0


def test_resync_after_garbage():
    d = TsDemuxer()
    units = d.feed(b'\x47garbage\x00' * 3 + av_stream()
                   + b''.join(packetize(AUDIO_PID, audio_pes([b'a' * 8], 0))[0]))
    assert d.pmt_seen and len(units) == 1


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='ffmpeg not installed')
def test_ffmpeg_generated_stream(tmp_path):
    out = tmp_path / 't.ts'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=30',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
        '-t', '2', '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
        '-bf', '0', '-g', '30', '-c:a', 'aac', '-b:a', '64k', '-f', 'mpegts', str(out),
    ], check=True)
    d = TsDemuxer()
    units = []
    data = out.read_bytes()
    for i in range(0, len(data), 1316):
        units += d.feed(data[i:i + 1316])
    units += d.close()
    video = [u for u in units if u.kind == 'video']
    audio = [u for u in units if u.kind == 'audio']
    assert d.cc_errors == 0 and d.video.config is not None
    assert len(video) == 60 and sum(u.key for u in video) == 2
    assert {b - a for a, b in zip(video, video[1:]) for a, b in [(a.pts, b.pts)]} == {3000}
    assert d.audio_asc == _ASC_MONO and d.audio_samplerate == 48000
    assert len(audio) > 90
    assert {b.pts - a.pts for a, b in zip(audio, audio[1:])} == {1920}


# ── unit completion and the capture instant ───────────────────────────

def _video_au(n: int, pts: int) -> bytes:
    """Frame `n` at `pts`, sized never to fill its last packet exactly."""
    if n == 0:
        return video_pes([_SPS, _PPS, idr(700)], pts)
    au = video_pes([b'\x41\x9a' + bytes(300 + n)], pts)
    return au if len(au) % 184 else video_pes([b'\x41\x9a' + bytes(301 + n)], pts)


def test_video_and_audio_are_stamped_at_their_arrival():
    """Each frame's packets arrive on schedule; its unit is stamped when its
    last packet is in, so video lags audio by no frame."""
    wall = [1_000.0]
    d = TsDemuxer(TsClock(now=lambda: wall[0]))
    d.feed(av_stream())
    lag = {'video': [], 'audio': []}
    vcc = acc = 0
    for i in range(60):                       # 2 s: video 30 fps, audio 1024 @ 48 kHz
        t_v, t_a = i / 30, i * 1024 / 48000
        for t, kind in sorted(((t_v, 'video'), (t_a, 'audio'))):
            wall[0] = 1_000.0 + t
            if kind == 'video':
                pkts, vcc = packetize(VIDEO_PID, _video_au(i, int(t_v * 90_000)), vcc)
            else:
                pkts, acc = packetize(AUDIO_PID, audio_pes([b'\x01' * 300], round(t_a * 90_000)), acc)
            for u in d.feed(b''.join(pkts)):
                lag[u.kind].append(int(wall[0] * 1_000_000) - d.to_us(u.pts))
    assert len(lag['video']) == 60 and len(lag['audio']) == 60
    assert max(map(abs, lag['video'] + lag['audio'])) <= 20   # rounding only


def test_an_exactly_filled_video_pes_closes_at_the_next_pes():
    d = TsDemuxer()
    d.feed(av_stream())
    body = b'\x41\x9a' + bytes(range(256)) * 3
    pes0 = video_pes([body], 0)
    pes0 = video_pes([body[:len(body) - len(pes0) % 184]], 0)
    assert len(pes0) % 184 == 0
    pkts, cc = packetize(VIDEO_PID, pes0)
    assert d.feed(b''.join(pkts)) == []       # no stuffing: completion unknown
    units = d.feed(b''.join(packetize(VIDEO_PID, _video_au(1, 3000), cc)[0]))
    assert [u.pts for u in units] == [0, 3000]


def test_a_pcr_only_adaptation_field_does_not_end_a_pes():
    pcr = bytes([7, 0x10]) + bytes(6)                       # PCR, no stuffing
    payload = video_pes([idr(400)], 0)
    first = bytes([0x47, 0x40 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0x30]) + pcr
    first += payload[:188 - len(first)]
    rest, _ = packetize(VIDEO_PID, payload[188 - 12:], cc0=1)
    rest[0] = bytes([rest[0][0], rest[0][1] & ~0x40]) + rest[0][2:]   # continuation, not PUSI
    d = TsDemuxer()
    d.feed(av_stream())
    assert d.feed(first) == []
    units = d.feed(b''.join(rest))
    assert len(units) == 1 and units[0].payload.endswith(idr(400)[-50:])


def test_a_pes_continuing_past_stuffing_turns_eager_completion_off(caplog):
    d = TsDemuxer()
    d.feed(av_stream())
    au = video_pes([idr(400)], 0)
    early = packet(VIDEO_PID, au[:100], pusi=True, cc=0)    # stuffed, yet not the end
    tail, cc = packetize(VIDEO_PID, au[100:], cc0=1)
    tail[0] = bytes([tail[0][0], tail[0][1] & ~0x40]) + tail[0][2:]
    assert len(d.feed(early)) == 1                          # emitted, cut short
    assert d.feed(b''.join(tail)) == [] and not d.eager_video
    assert 'continued past a stuffed packet' in caplog.text
    pkts, cc = packetize(VIDEO_PID, _video_au(1, 3000), cc)
    assert d.feed(b''.join(pkts)) == []                     # waits for the next PES now
    assert [u.pts for u in d.close()] == [3000]


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='ffmpeg not installed')
def test_ffmpeg_video_units_complete_before_the_next_frame(tmp_path):
    out = tmp_path / 'v.ts'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=30', '-t', '1',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-bf', '0',
        '-f', 'mpegts', '-muxdelay', '0', '-muxpreload', '0', str(out),
    ], check=True)
    data = out.read_bytes()
    d = TsDemuxer()
    late = n = 0
    for i in range(0, len(data), 188):
        pkt = data[i:i + 188]
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        units = [u for u in d.feed(pkt) if u.kind == 'video']
        n += len(units)
        # A unit emitted on the next frame's first packet waited a frame.
        late += bool(units) and pid == d.video_pid and bool(pkt[1] & 0x40)
    assert n >= 29 and late == 0 and d.eager_video


# ── cadence and measured rates ────────────────────────────────────────

def test_cadence_timescales():
    from fractions import Fraction
    from aiomoqt.media.mpegts import TsCadence
    c = TsCadence.parse('fps=30,gop=60')
    assert (c.timescale, c.frame_ticks, c.gop, c.framerate) == (30000, 1000, 60, 30.0)
    c = TsCadence.parse('gop=48, fps=24000/1001')
    assert (c.timescale, c.frame_ticks, c.framerate) == (24000, 1001, 23.98)
    assert c.fps == Fraction(24000, 1001)
    m = c.video_timestamps(1_000_000)
    assert (m.timescale, m.group_multiplier, m.object_multiplier) == (24000, 48 * 1001, 1001)
    a = TsCadence.audio_timestamps(1_000_000, 48000, 960, 1)
    assert (a.timescale, a.group_multiplier, a.object_multiplier) == (48000, 960, 0)
    for bad in ('fps=30', 'gop=60', 'fps=0,gop=60', 'fps=30,gop=0', 'rate=30,gop=1',
                'fps=x,gop=2'):
        with pytest.raises(ValueError):
            TsCadence.parse(bad)


def test_held_clock_keeps_the_pts_schedule():
    wall = [1_000.0]
    clk = TsClock(now=lambda: wall[0])
    assert clk.to_us(0) == 1_000_000_000
    clk.hold()
    wall[0] = 1_000.010                       # 10 ms in, but PTS says 33 ms
    assert clk.to_us(3000) == 1_000_033_333   # ahead of arrival, not slid
    wall[0] = 1_000.050
    assert clk.to_us(6000) == 1_000_066_666


def test_rates_measure_fps_and_bitrate_over_whole_groups():
    from aiomoqt.media.mpegts import TsRates, TsUnit
    r = TsRates(window_s=1.0)
    for i in range(120):                      # 30 fps, key every 15 frames
        key = i % 15 == 0
        r.add(TsUnit('video', bytes(5000 if key else 1000), key, i * 3000))
        r.add(TsUnit('audio', bytes(400), True, i * 1920))
    assert r.fps == 30.0
    assert r.bps['video'] == (5000 + 14 * 1000) * 8 * 2       # two GOPs a second
    assert r.bps['audio'] == 400 * 8 * 48000 // 1024
