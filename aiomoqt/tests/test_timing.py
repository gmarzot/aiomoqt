"""utils.timing: one resolver for object capture times — frindell
(draft-frindell-moq-timestamp-00) over LOC TIMESTAMP over bench 0x20 —
and the tools that report latency from it."""
from types import SimpleNamespace

import pytest

from aiomoqt.agent.reader import Reader
from aiomoqt.agent.spec import SubscribeSpec
from aiomoqt.media.loc import LocFrame
from aiomoqt.messages.subscribe import SubscribeOk
from aiomoqt.utils.media_stats import MediaAnalysis
from aiomoqt.utils.stats import TrackStats, send_time_us
from aiomoqt.utils.timing import (
    BENCH_TIMESTAMP, CLOCK_ID, LOC_TIMESCALE, LOC_TIMESTAMP,
    OBJECT_TIMESTAMP, SOURCE_BENCH, SOURCE_FRINDELL, SOURCE_LOC, TIMESCALE,
    TIMESTAMP_MAPPING, TIMESTAMP_ORIGIN, TrackTiming, capture_time_us,
    encode_mapping, zigzag_encode,
)

W = 1_767_225_600_000_000      # 2026-01-01T00:00:00Z in µs


def _timing(props, vi64=True):
    return TrackTiming.from_properties(props, vi64)


@pytest.mark.parametrize("v, u", [(0, 0), (-1, 1), (1, 2), (-2, 3), (2, 4)])
def test_zigzag_order(v, u):
    assert zigzag_encode(v) == u


@pytest.mark.parametrize("vi64", [False, True], ids=["d16", "d18"])
def test_mapped_track_takes_schedule_plus_correction(vi64):
    # 30 fps at 30000 ticks/s, 60-object groups, anchored at wall W.
    t = _timing({TIMESCALE: 30000, TIMESTAMP_MAPPING: encode_mapping(
        10, W * 30000 // 1_000_000, 60060, 1001, vi64)}, vi64)
    sched = W + (2 * 60060 + 3 * 1001) * 1_000_000 // 30000
    assert capture_time_us(t, 12, 3, None) == (sched, SOURCE_FRINDELL)
    late = {OBJECT_TIMESTAMP: zigzag_encode(30)}       # +1 ms
    assert capture_time_us(t, 12, 3, late)[0] == sched + 1000
    early = {OBJECT_TIMESTAMP: zigzag_encode(-30)}
    assert capture_time_us(t, 12, 3, early)[0] == sched - 1000


def test_explicit_track_reads_object_timestamp():
    t = _timing({TIMESCALE: 1_000_000})
    assert capture_time_us(t, 0, 0, {OBJECT_TIMESTAMP: W + 5}) == (
        W + 5, SOURCE_FRINDELL)
    # No OBJECT_TIMESTAMP: no frindell time, LOC answers.
    assert capture_time_us(t, 0, 0, {LOC_TIMESTAMP: W}) == (W, SOURCE_LOC)


def test_origin_with_clock_id_zero_is_wall_clock():
    t = _timing({TIMESCALE: 1000, CLOCK_ID: 0,
                 TIMESTAMP_ORIGIN: W // 1000})
    assert capture_time_us(t, 0, 0, {OBJECT_TIMESTAMP: 7}) == (
        W + 7000, SOURCE_FRINDELL)


def test_media_clock_gives_no_latency_figure():
    t = _timing({TIMESCALE: 90000, CLOCK_ID: 0x1A3F5C9E07B2D461})
    exts = {OBJECT_TIMESTAMP: 90000, LOC_TIMESTAMP: W}
    assert not t.wall_clock
    assert capture_time_us(t, 0, 0, exts) == (W, SOURCE_LOC)
    assert capture_time_us(t, 0, 0, {OBJECT_TIMESTAMP: 90000}) == (
        None, None)
    assert t.media_us(0, 0, {OBJECT_TIMESTAMP: 90000}) == 1_000_000


def test_loc_timestamp_needs_no_loc_timescale():
    assert capture_time_us(None, 0, 0, {LOC_TIMESTAMP: W}) == (
        W, SOURCE_LOC)
    # A LOC TIMESCALE Track Property makes 0x10 media time.
    t = _timing({LOC_TIMESCALE: 90000})
    assert capture_time_us(t, 0, 0, {LOC_TIMESTAMP: 9}) == (None, None)
    assert capture_time_us(t, 0, 0, {LOC_TIMESTAMP: 9,
                                     BENCH_TIMESTAMP: W}) == (
        W, SOURCE_BENCH)


def test_loc_timescale_on_one_object_sticks_to_the_track():
    t = TrackTiming()
    capture_time_us(t, 0, 0, {LOC_TIMESTAMP: 9, LOC_TIMESCALE: 90000})
    assert capture_time_us(t, 0, 1, {LOC_TIMESTAMP: 99}) == (None, None)


def test_frindell_outranks_loc_and_bench():
    t = _timing({TIMESCALE: 1_000_000})
    exts = {OBJECT_TIMESTAMP: W, LOC_TIMESTAMP: W + 1,
            BENCH_TIMESTAMP: W + 2}
    assert capture_time_us(t, 0, 0, exts) == (W, SOURCE_FRINDELL)
    assert send_time_us(exts, t, 0, 0) == W
    assert send_time_us({LOC_TIMESTAMP: W + 1, BENCH_TIMESTAMP: W + 2}) \
        == W + 1


def _msg(gid, oid, exts):
    return SimpleNamespace(group_id=gid, object_id=oid, extensions=exts,
                           status=0, subgroup_id=0, payload=b"x")


def test_track_stats_measures_from_frindell_and_reports_the_source():
    stats = TrackStats(windowed=False)
    stats.timing.assign(_timing({TIMESCALE: 1_000_000}))
    stats.on_object(_msg(0, 0, {OBJECT_TIMESTAMP: W,
                                LOC_TIMESTAMP: W - 50_000}),
                    10, W + 2000)
    s = stats.summary()
    assert s['ts_source'] == SOURCE_FRINDELL
    assert s['lat_min'] == pytest.approx(2.0)


def test_track_stats_counts_malformed_timestamps():
    stats = TrackStats(windowed=False)
    stats.on_object(_msg(0, 0, {OBJECT_TIMESTAMP: W}), 10, W)
    assert stats.ts_malformed == 1 and stats.lat_count == 0


def test_media_analysis_reports_frindell_latency_and_source():
    timing = _timing({TIMESCALE: 48000, TIMESTAMP_MAPPING: encode_mapping(
        0, W * 48000 // 1_000_000, 960, 0, True)})
    a = MediaAnalysis(timing_of=lambda name: timing)
    a.add_track('audio', role='audio')
    a.start(W)
    for g in range(5):
        cap = W + g * 20_000
        a.on_arrival('audio', _msg(g, 0, {LOC_TIMESTAMP: cap - 7_000}),
                     cap + 3_000, g, 0,
                     LocFrame(b"x", True, timestamp=cap - 7_000))
    rows, _ = a.summary(W + 200_000)
    assert rows[0]['ts_source'] == SOURCE_FRINDELL
    assert rows[0]['wall'] and rows[0]['lat_p50_ms'] == pytest.approx(3.0)


def test_agent_reader_ages_objects_from_the_reply_timing():
    rdr = Reader(SubscribeSpec.from_dict(
        {"track": {"namespace": "n", "name": "t"}}), "n/t")
    rdr.on_reply(SubscribeOk(track_extensions={TIMESCALE: 1_000_000}),
                 True)
    rdr.on_object(_msg(0, 0, {OBJECT_TIMESTAMP: W}), 1, W + 4000, 0, 0)
    obj = rdr._ring[0]
    assert obj.sent_us == W and obj.age_ms == pytest.approx(4.0)


def test_track_stats_loc_fast_path_reports_and_respects_loc_timescale():
    stats = TrackStats(windowed=False)
    stats.on_object(_msg(0, 0, {LOC_TIMESTAMP: W}), 10, W + 1000)
    assert stats.ts_source == SOURCE_LOC and stats.lat_count == 1
    # A LOC TIMESCALE makes later 0x10 values media time: no sample.
    stats.on_object(_msg(0, 1, {LOC_TIMESTAMP: 9, LOC_TIMESCALE: 90000}),
                    10, W + 2000)
    stats.on_object(_msg(0, 2, {LOC_TIMESTAMP: 99}), 10, W + 3000)
    assert stats.lat_count == 1
