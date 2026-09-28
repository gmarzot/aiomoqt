"""sub_media --analyze: the delivery accounting in utils/media_stats and
the tool end to end over loopback, LOC and CMAF."""
import asyncio
import csv
import random
import sys
import time
from types import SimpleNamespace as NS

import pytest

from aiomoqt.media import (
    Catalog, CatalogTrack, InitData, LocFrame, LocTrackPublisher,
    MediaPublisher,
)
from aiomoqt.media.cmaf import CmafChunker
from aiomoqt.server import MOQTServer
from aiomoqt.tools import sub_media
from aiomoqt.types import MOQTMessageType, ObjectStatus
from aiomoqt.utils.media_stats import (
    CSV_FIELDS, MediaAnalysis, TrackAnalysis, csv_row, wall_clock_us,
)

from aiomoqt.tests._certs import CERT, KEY, requires_certs

T0 = 1_780_000_000_000_000      # wall-clock µs
MS = 1000
END = T0 + 10_000 * MS


def _track(cushion_ms=100):
    return TrackAnalysis('v', role='video', cushion_ms=cushion_ms)


def _obj(oid, status=None, flags=None, **kw):
    return NS(object_id=oid, status=status, extensions={},
              stream_flags=flags, **kw)


# -- continuity ------------------------------------------------------

def test_reorder_inside_cushion_is_not_a_loss():
    t = _track()
    t.observe(0, 0, 10, T0)
    t.observe(0, 2, 10, T0 + 2 * MS)
    assert (t.pending, t.lost) == (1, 0)
    t.observe(0, 1, 10, T0 + 30 * MS)
    s = t.summary(END)
    assert (s['lost'], s['pending'], s['reorders'], s['late_reorders']) \
        == (0, 0, 1, 0)
    assert s['settle_p50_ms'] == 28


def test_reorder_past_cushion_reverses_the_loss():
    t = _track()
    t.observe(0, 0, 10, T0)
    t.observe(0, 2, 10, T0 + 2 * MS)
    t.observe(0, 3, 10, T0 + 200 * MS)
    assert (t.pending, t.lost) == (0, 1)
    t.observe(0, 1, 10, T0 + 250 * MS)
    s = t.summary(END)
    assert (s['lost'], s['reorders'], s['late_reorders']) == (0, 1, 1)


def test_duplicate_counted_once():
    t = _track()
    for oid in (0, 1, 1, 2):
        t.observe(0, oid, 10, T0 + oid * MS)
    s = t.summary(END)
    assert (s['objects'], s['dups'], s['lost'], s['reorders']) == (3, 1, 0, 0)


def test_unfilled_gap_is_pending_then_lost():
    t = _track()
    t.observe(0, 0, 10, T0)
    t.observe(0, 3, 10, T0 + 3 * MS)
    row = t.interval(T0 + 50 * MS, 0.05)
    assert (row['pending'], row['lost']) == (2, 0)
    row = t.interval(T0 + 150 * MS, 0.1)
    assert (row['pending'], row['lost']) == (0, 2)


def test_group_integrity():
    t = _track()
    t.observe(0, 0, 10, T0)
    t.observe(0, 1, 10, T0 + 1 * MS)
    t.end_group(0, 2, T0 + 2 * MS)                  # ended by marker
    t.observe(1, 0, 10, T0 + 10 * MS)
    t.observe(1, 1, 10, T0 + 11 * MS)               # no END_OF_GROUP
    t.observe(2, 0, 10, T0 + 20 * MS)
    t.observe(2, 1, 10, T0 + 21 * MS)
    t.end_group(2, 4, T0 + 22 * MS)                 # 2 and 3 never came
    t.observe(3, 0, 10, T0 + 30 * MS)               # newest: not judged
    s = t.summary(END)
    assert (s['missing_eog'], s['partial_groups'], s['lost']) == (1, 1, 2)
    assert s['gop_p50'] == 2


def test_group_gap_lost_unless_declared_absent():
    t = _track()
    t.observe(0, 0, 10, T0)
    t.observe(3, 0, 10, T0 + 10 * MS)
    assert t.pending == 2
    t.observe(4, 0, 10, T0 + 200 * MS)
    assert (t.lost_groups, t.pending) == (2, 0)

    t = _track()
    t.observe(0, 0, 10, T0)
    t.observe(3, 0, 10, T0 + 10 * MS, prior_gap=2)
    t.observe(4, 0, 10, T0 + 200 * MS)
    assert (t.lost_groups, t.pending) == (0, 0)


def test_late_group_is_a_reorder():
    t = _track()
    t.observe(0, 0, 10, T0)
    t.observe(2, 0, 10, T0 + 5 * MS)
    t.observe(1, 0, 10, T0 + 8 * MS)
    s = t.summary(END)
    assert (s['lost_groups'], s['reorders'], s['pending']) == (0, 1, 0)


def test_join_mid_group():
    t = _track()
    t.observe(5, 7, 10, T0, T0)
    t.observe(5, 8, 10, T0 + 33 * MS, T0 + 33 * MS)
    t.end_group(5, 9, T0 + 34 * MS)
    t.observe(6, 0, 10, T0 + 66 * MS, T0 + 66 * MS)
    t.observe(7, 0, 10, T0 + 99 * MS, T0 + 99 * MS)
    s = t.summary(END)
    assert (s['lost'], s['pending'], s['partial_groups']) == (0, 0, 0)
    assert s['pre_anchor'] == 2


# -- playout ---------------------------------------------------------

def test_playout_late_and_underrun():
    t = _track(cushion_ms=100)
    for i in range(10):
        t.observe(0, i, 10, T0 + i * 10 * MS, T0 + i * 10 * MS)
    # Due at +200 ms (fastest transit 0 + cushion), arrives +250 with
    # nothing newer buffered: a stall.
    t.observe(0, 10, 10, T0 + 250 * MS, T0 + 100 * MS)
    assert (t.late, t.underruns, t.stall_us) == (1, 1, 50 * MS)
    # Still 150 ms behind: late again, but the same starved stretch.
    t.observe(0, 11, 10, T0 + 260 * MS, T0 + 110 * MS)
    assert (t.late, t.underruns, t.stall_us) == (2, 1, 60 * MS)
    # Delivery recovers; 22 lands in time, then 21 misses its deadline
    # while 22 is buffered: late, not an underrun.
    t.observe(0, 20, 10, T0 + 265 * MS, T0 + 200 * MS)
    t.observe(0, 22, 10, T0 + 280 * MS, T0 + 220 * MS)
    t.observe(0, 21, 10, T0 + 315 * MS, T0 + 210 * MS)
    s = t.summary(END)
    assert (s['late'], s['underruns'], s['stall_ms']) == (3, 1, 60)
    assert s['buf_min_ms'] == 0


def test_startup_stall_does_not_inflate_the_buffer():
    """1 s clean, a 2 s outage delivered as one burst, then clean again:
    one underrun, and the buffer returns to the cushion instead of
    carrying the stall for the rest of the run."""
    t = _track(cushion_ms=100)

    def m(i):
        return T0 + i * 20 * MS
    for i in range(50):
        t.observe(i, 0, 10, m(i) + 40 * MS, m(i))
    burst = T0 + 3_500 * MS
    for i in range(50, 150):
        t.observe(i, 0, 10, burst + (i - 50) * 100, m(i))
    t.interval(burst + 20 * MS, 3.52)
    for i in range(175, 275):
        t.observe(i, 0, 10, m(i) + 40 * MS, m(i))
    row = t.interval(m(274) + 50 * MS, 2.0)
    assert (row['late'], row['underruns'], row['buf_p50_ms']) == (0, 0, 80)
    s = t.summary(END)
    assert (s['late'], s['underruns']) == (100, 1)
    assert 2350 < s['stall_ms'] < 2380


def test_clean_delivery_has_no_late_objects():
    t = _track(cushion_ms=100)
    rng = random.Random(7)
    for i in range(500):
        m = T0 + i * 20 * MS
        t.observe(i, 0, 100, m + 2 * MS + rng.randint(0, 20 * MS), m)
    s = t.summary(END)
    assert (s['late'], s['underruns'], s['lost']) == (0, 0, 0)
    assert 55 < s['buf_min_ms'] <= s['buf_p50_ms'] < 100


# -- clocks ----------------------------------------------------------

def _jittery(offset_us=0, timescale=None):
    t = _track()
    rng = random.Random(1)
    for i in range(200):
        m = T0 + i * 20 * MS
        ts = m if timescale is None else i * 20 * timescale // 1000
        t.observe(i, 0, 100, m + 2 * MS + rng.randint(0, 3 * MS) + offset_us,
                  ts, timescale)
    return t.summary(END + offset_us)


def test_clock_offset_moves_latency_not_jitter():
    base = _jittery()
    shifted = _jittery(offset_us=3_000 * MS)
    media = _jittery(timescale=90000)
    assert base['jitter_ms'] > 0
    assert shifted['jitter_ms'] == pytest.approx(base['jitter_ms'])
    assert media['jitter_ms'] == pytest.approx(base['jitter_ms'], abs=0.01)
    assert shifted['lat_p50_ms'] == pytest.approx(base['lat_p50_ms'] + 3000)
    assert media['wall'] is False and media['lat_p50_ms'] is None
    assert (media['late'], media['underruns']) == (0, 0)


def test_skew_cancels_clock_offset():
    def run(offset):
        a = MediaAnalysis(cushion_ms=100)
        v = a.add_track('video', role='video')
        au = a.add_track('audio', role='audio')
        a.start(T0 + offset)
        for i in range(50):
            m = T0 + i * 20 * MS
            v.observe(i, 0, 10, m + 9 * MS + offset, m)
            au.observe(i, 0, 10, m + 4 * MS + offset, m)
        return a.interval(T0 + 1_000 * MS + offset)[3]
    assert run(0) == pytest.approx(5)
    assert run(2_000 * MS) == pytest.approx(5)


def test_wall_clock_detection():
    assert wall_clock_us(T0, None, T0 + 5 * MS) == T0
    assert wall_clock_us(T0, 1_000_000, T0) == T0
    assert wall_clock_us(T0, 90000, T0) is None
    assert wall_clock_us(123_456, None, T0) is None
    assert wall_clock_us(None, None, T0) is None


def test_inspect_reconciles_with_analysis(capsys):
    frame = LocFrame(b'x' * 10, True, T0)
    a = MediaAnalysis()
    a.add_track('video', role='video')
    a.on_arrival('video', _obj(0), T0 + 12_400, 0, 0, frame)
    sub_media._Inspector(1).on_arrival('video', _obj(0), T0 + 12_400, 0, 0,
                                       frame)
    assert 'ts_skew_ms=12 ' in capsys.readouterr().err
    assert a.summary(END)[0][0]['lat_p50_ms'] == pytest.approx(12.4)


# -- on_arrival adapter ----------------------------------------------

def test_adapter_reads_every_end_of_group_form():
    a = MediaAnalysis(cushion_ms=100)
    a.add_track('v', role='video')

    def frame(m):
        return LocFrame(b'x', False, T0 + m * MS)
    a.on_arrival('v', _obj(0), T0, 0, 0, frame(0))
    # Stream header END_OF_GROUP bit: FIN ends group 0.
    a.on_arrival('v', _obj(1, flags=(False, True, False)), T0 + 1 * MS, 0,
                 0, frame(1))
    a.on_arrival('v', _obj(0), T0 + 10 * MS, 1, 0, frame(10))
    # END_OF_GROUP status object.
    a.on_arrival('v', _obj(1, status=ObjectStatus.END_OF_GROUP),
                 T0 + 11 * MS, 1, 0, None)
    # Datagram with its end-of-group flag.
    a.on_arrival('v', _obj(0, end_of_group=True), T0 + 20 * MS, 2, None,
                 frame(20))
    a.on_arrival('v', _obj(0), T0 + 30 * MS, 3, None, frame(30))
    s = a.summary(END)[0][0]
    assert (s['missing_eog'], s['partial_groups'], s['objects']) == (0, 0, 5)


def test_adapter_uses_tfdt_for_cmaf_without_loc_timestamp():
    ck = CmafChunker(NS(timescale=90000, width=640, height=360,
                        sample_entry_bytes=b'\x00' * 8))
    a = MediaAnalysis(cushion_ms=100)
    a.add_track('c', role='video', cmaf=True, timescale=90000)
    for i in range(30):
        chunk = ck.chunk(b'x', 3000, key_frame=(i == 0))
        a.on_arrival('c', _obj(i), T0 + i * 33_333 + MS, 0, 0,
                     LocFrame(chunk, i == 0, None))
    s = a.summary(END)[0][0]
    assert s['timed'] and s['wall'] is False
    assert s['lat_p50_ms'] is None
    assert s['jitter_ms'] < 0.01
    assert (s['late'], s['underruns']) == (0, 0)


def test_csv_rows_match_fields():
    t = _track()
    t.observe(0, 0, 10, T0, T0)
    iv = t.interval(T0 + MS, 0.001)
    sm = t.summary(END)
    assert len(csv_row('interval', 0.0, 1.0, iv, None)) == len(CSV_FIELDS)
    assert len(csv_row('summary', 0.0, 1.0, sm, 1.5)) == len(CSV_FIELDS)
    assert set(CSV_FIELDS) - {'kind', 't0_s', 't1_s', 'skew_ms'} <= set(sm)


# -- loopback --------------------------------------------------------

_BASE_PORT = 14940
_NS = "demo/analyze"


def _catalog(chunkers):
    cmaf = bool(chunkers)
    packaging = 'cmaf' if cmaf else 'loc'
    video = CatalogTrack(
        name='video', packaging=packaging, isLive=True, role='video',
        codec='avc1.640028', width=640, height=360, framerate=30,
        bitrate=1_000_000, initRef='v0' if cmaf else None)
    audio = CatalogTrack(
        name='audio', packaging=packaging, isLive=True, role='audio',
        codec='mp4a.40.2' if cmaf else 'pcm-s16', samplerate=48000,
        channelConfig='2', bitrate=128_000, initRef='a0' if cmaf else None)
    init = ([InitData.from_bytes('v0', chunkers['video'].init_segment()),
             InitData.from_bytes('a0', chunkers['audio'].init_segment())]
            if cmaf else None)
    return Catalog(generatedAt=1, tracks=[video, audio], initDataList=init)


async def _feed(pub, chunkers):
    """30 fps video in 15-frame GOPs and 50/s audio, paced and stamped
    with the wall clock, until cancelled."""
    video, audio = pub._by_name['video'], pub._by_name['audio']
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    i = j = 0
    while True:
        now = loop.time() - t0
        while i / 30 <= now:
            key = i % 15 == 0
            payload = b'K' * 8000 if key else b'p' * 800
            if chunkers:
                payload = chunkers['video'].chunk(payload, 3000, key)
            await video.send_frame(payload, key_frame=key,
                                   timestamp=int(time.time() * 1e6))
            i += 1
        while j * 0.02 <= now:
            payload = b'a' * 200
            if chunkers:
                payload = chunkers['audio'].chunk(payload, 960)
            await audio.send_frame(payload, key_frame=True,
                                   timestamp=int(time.time() * 1e6))
            j += 1
        await asyncio.sleep(0.005)


def _server(port, chunkers, feeds):
    server = MOQTServer(
        host="localhost", port=port, certificate=CERT, private_key=KEY,
        path="/", use_quic=True, supported_drafts=18)
    pubs = {}

    async def _on_subscribe(session, msg):
        pub = pubs.get(id(session))
        if pub is None:
            pub = pubs[id(session)] = MediaPublisher(
                session, _NS, _catalog(chunkers))
            pub.add_track(LocTrackPublisher(session, _NS, 'video'))
            pub.add_track(LocTrackPublisher(session, _NS, 'audio'))
            await pub.catalog_track.finish()
            feeds.append(asyncio.ensure_future(_feed(pub, chunkers)))
        await pub._demux_subscribe(session, msg)

    server.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    return server


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("packaging", ["loc", "cmaf"])
async def test_sub_media_analyze_clean_loopback(packaging, tmp_path,
                                                monkeypatch):
    port = _BASE_PORT + (packaging == 'cmaf')
    chunkers = ({
        'video': CmafChunker(NS(timescale=90000, width=640, height=360,
                                sample_entry_bytes=b'\x00' * 8)),
        'audio': CmafChunker(NS(timescale=48000,
                                sample_entry_bytes=b'\x00' * 8)),
    } if packaging == 'cmaf' else None)
    feeds = []
    server = await _server(port, chunkers, feeds).serve()
    report = tmp_path / "analyze.csv"
    monkeypatch.setattr(sys, 'argv', [
        'sub_media', f'moqt://localhost:{port}/', '-N', _NS, '-k',
        '--analyze', '-t', '2', '-i', '1', '--report', str(report)])
    try:
        rc = await sub_media.run(sub_media.parse_args())
    finally:
        for f in feeds:
            f.cancel()
        server.close()
    assert rc == 0
    with report.open() as fh:
        rows = [r for r in csv.DictReader(fh) if r['kind'] == 'summary']
    assert {r['track'] for r in rows} == {'video', 'audio'}
    for r in rows:
        assert int(r['objects']) > 0, r
        assert (r['late'], r['underruns']) == ('0', '0'), r
        assert (r['lost'], r['lost_groups'], r['pending']) \
            == ('0', '0', '0'), r
        assert (r['missing_eog'], r['partial_groups']) == ('0', '0'), r
        assert float(r['lat_p50_ms']) >= 0
