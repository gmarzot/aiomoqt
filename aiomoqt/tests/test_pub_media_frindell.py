"""pub_media --timestamp-mapping: draft-frindell-moq-timestamp Track
Properties and corrections beside LOC TIMESTAMP, a prft on each CMAF and
LOCMAF group start, end to end through the interop relay into sub_media
--analyze over raw QUIC and WebTransport, audio and video on one
session."""
import asyncio
import csv
import struct
import sys
from types import SimpleNamespace as NS

import pytest

from aiomoqt.media.cmaf import CmafChunker, prft_box
from aiomoqt.media.locmaf import (
    GroupState, LocmafDecoder, LocmafEncoder, TrackContext,
)
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests.test_media_locmaf import clip  # noqa: F401  (fixture)
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tools import pub_media, sub_media
from aiomoqt.utils.timing import (
    OBJECT_TIMESTAMP, TIMESCALE, MediaTimestamps,
    TrackTiming, capture_time_us, zigzag_decode,
)

W = 1_767_225_600_000_000
NTP_UNIX = 2_208_988_800


def _read_prft(chunk: bytes):
    """(flags, track id, NTP µs since the epoch, media time) of a leading
    version-1 prft, read as a player does."""
    size, btype = struct.unpack_from('>I4s', chunk)
    assert btype == b'prft' and size == 32
    version, flags = chunk[8], int.from_bytes(chunk[9:12], 'big')
    assert version == 1
    track_id, sec, frac, media = struct.unpack_from('>IIIQ', chunk, 12)
    return (flags, track_id,
            (sec - NTP_UNIX) * 1_000_000 + ((frac * 1_000_000) >> 32), media)


def test_prft_carries_the_capture_time_as_ntp():
    box = prft_box(W + 123_456, 90_000)
    assert _read_prft(box) == (0x18, 1, W + 123_456, 90_000)


def test_mapped_media_track_sends_corrections_only_off_schedule():
    m = MediaTimestamps(W, 15360, 30 * 512, 512)
    assert m.track_properties(True)[TIMESCALE] == 15360
    on = W + (3 * 30 + 4) * 512 * 1_000_000 // 15360
    assert m.group_for(W + 3 * 1_000_000) == 3
    assert m.object_properties(3, 4, on) == {}
    late = m.object_properties(3, 4, on + 1000)
    timing = TrackTiming.from_properties(m.track_properties(True), True)
    tick_us = 1_000_000 / 15360
    assert abs(capture_time_us(timing, 3, 4, late)[0] - (on + 1000)) < tick_us
    assert zigzag_decode(late[OBJECT_TIMESTAMP]) > 0


def test_explicit_media_track_stamps_every_object():
    m = MediaTimestamps(W)
    assert m.track_properties(False) == {TIMESCALE: 1_000_000}
    assert m.object_properties(7, 3, W + 5) == {OBJECT_TIMESTAMP: W + 5}
    assert m.group_for(W) is None


def test_cadence_needs_one_duration_and_even_keyframes():
    def src(stts, sync, n=90):
        return NS(_stts=stts, _sync=sync, _sizes=[1] * n, timescale=15360)
    assert pub_media._sample_cadence(src([(90, 512)], {1, 31, 61})) == (
        512, 30)
    assert pub_media._sample_cadence(src([(90, 512)], None)) == (512, 1)
    assert pub_media._sample_cadence(src([(90, 512)], {1, 31, 50})) is None
    assert pub_media._sample_cadence(
        src([(40, 512), (40, 500), (10, 512)], {1, 31, 61})) is None
    explicit = pub_media._frindell_for(src([(90, 512)], {1, 40, 60}), W)
    assert not explicit.mapped
    audio = pub_media._frindell_for(
        NS(_stts=[(100, 1024)], _sync=None, _sizes=[1] * 100,
           timescale=48000), W, per_group=47)
    assert (audio.group_multiplier, audio.object_multiplier) == (
        47 * 1024, 1024)


def test_media_clock_lands_on_whole_ticks():
    _, wall0 = pub_media._media_clock([15360, 48000, 44100], True)
    for t in (15360, 48000, 44100):
        assert wall0 * t % 1_000_000 == 0


def test_locmaf_carries_prft_as_a_genbox():
    ck = CmafChunker(NS(timescale=90000, width=64, height=64,
                        sample_entry_bytes=_AVC1))
    init = ck.init_segment()
    chunk = prft_box(W, 0) + ck.chunk(b'k' * 20, 3000, True)
    enc = LocmafEncoder(TrackContext.from_init(init))
    obj = enc.encode(chunk, GroupState(), 0)
    assert enc.raw_fallbacks == 0
    rebuilt = LocmafDecoder(init).push(0, 0, obj)
    assert rebuilt.startswith(prft_box(W, 0))


# Minimal avc1 sample entry: enough for a CMAF header TrackContext reads.
_AVC1 = (struct.pack('>I4s', 86, b'avc1') + b'\x00' * 6 + b'\x00\x01'
         + b'\x00' * 16 + struct.pack('>HH', 64, 64) + b'\x00' * 50)


# -- end to end --------------------------------------------------------

_BASE_PORT = 16540
_SECONDS = 4


def _argv(monkeypatch, tool, *argv):
    monkeypatch.setattr(sys, 'argv', [tool, *argv])
    return (pub_media if tool == 'pub_media' else sub_media).parse_args()


async def _through_relay(monkeypatch, url, port, draft, use_quic, pub_argv,
                         sub_argv):
    relay._announced.clear()
    relay._tracks.clear()
    handle = await relay._build_server('localhost', port, CERT, KEY,
                                       use_quic=use_quic, draft=draft).serve()
    pub_args = _argv(monkeypatch, 'pub_media', url, '-k', '--draft',
                     str(draft), '-t', str(_SECONDS), '--stats', '0',
                     '--catalog-interval', '0.5', '--pub-ns', *pub_argv)
    sub_args = _argv(monkeypatch, 'sub_media', url, '-k', '--draft',
                     str(draft), *sub_argv)
    pub = asyncio.create_task(pub_media.run(pub_args))
    try:
        for _ in range(200):
            if relay._announced or pub.done():
                break
            await asyncio.sleep(0.05)
        assert relay._announced, "publisher never announced"
        rc = await sub_media.run(sub_args)
        await asyncio.wait_for(pub, 30)
    finally:
        if not pub.done():
            pub.cancel()
        handle.close()
        relay._announced.clear()
        relay._tracks.clear()
    return rc


@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
@pytest.mark.parametrize('draft', [18, 22])
async def test_analyze_reports_frindell_latency(clip, tmp_path,  # noqa: F811
                                                monkeypatch, draft,
                                                use_quic):
    pytest.importorskip('av')
    port = _BASE_PORT + 2 * [18, 22].index(draft) + (not use_quic)
    url = f"{'moqt' if use_quic else 'https'}://localhost:{port}/"
    ns = f'frindell/e2e{draft}'
    report = tmp_path / 'analyze.csv'
    rc = await _through_relay(
        monkeypatch, url, port, draft, use_quic,
        ['-N', ns, '--mp4', clip, '--timestamp-mapping'],
        ['-N', ns, '--analyze', '-t', '2', '-i', '1', '--report',
         str(report)])
    assert rc == 0
    with report.open() as fh:
        rows = [r for r in csv.DictReader(fh) if r['kind'] == 'summary']
    assert {r['track'] for r in rows} == {'video', 'audio'}
    for r in rows:
        assert r['ts_source'] == 'frindell', r
        assert r['ts_malformed'] == '0', r
        assert int(r['objects']) > 0, r
        assert 0 <= float(r['lat_p50_ms']) < 1000, r


@requires_certs
@pytest.mark.parametrize('packaging', ['cmaf', 'locmaf'])
async def test_chunked_groups_open_with_a_prft(clip, tmp_path,  # noqa: F811
                                               monkeypatch, packaging):
    pytest.importorskip('av')
    port = _BASE_PORT + 10 + (packaging == 'locmaf')
    url = f"moqt://localhost:{port}/"
    ns = f'frindell/{packaging}'
    rc = await _through_relay(
        monkeypatch, url, port, 18, True,
        ['-N', ns, '--mp4', clip, '--packaging', packaging,
         '--timestamp-mapping'],
        ['-N', ns, '--packaging', packaging, '--out', str(tmp_path),
         '-t', '2'])
    assert not rc
    data = (tmp_path / 'video.mp4').read_bytes()
    pos, prfts = 0, []
    while pos < len(data):
        size, btype = struct.unpack_from('>I4s', data, pos)
        if btype == b'prft':
            prfts.append(_read_prft(data[pos:pos + size]))
        pos += size
    assert prfts, "no prft"
    assert all(p[0] == 0x18 and p[1] == 1 for p in prfts)
    # One GOP a second: the NTP times step by one second.
    steps = {round((b[2] - a[2]) / 1e5) for a, b in zip(prfts, prfts[1:])}
    assert steps <= {10}, steps


def test_clip_on_its_cadence_needs_no_corrections(clip):  # noqa: F811
    from aiomoqt.media.sources import Mp4Reader
    reader = Mp4Reader(clip)
    _, wall0 = pub_media._media_clock(
        [reader.video.timescale, reader.audio.timescale], True)
    for source, all_key in ((reader.video, False), (reader.audio, True)):
        m = pub_media._frindell_for(source, wall0)
        assert m.mapped
        group = obj = None
        for s in source.samples():
            capture = wall0 + s.timestamp_us
            if all_key or s.key_frame:
                group, obj = m.group_for(capture), 0
            else:
                obj += 1
            assert m.object_properties(group, obj, capture) == {}, (
                source, group, obj)
