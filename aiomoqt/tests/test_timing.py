"""utils.timing: one resolver for object capture times — frindell
(draft-frindell-moq-timestamp-00) over LOC TIMESTAMP over bench 0x20 —
and the tools that report latency from it."""
import logging
from types import SimpleNamespace

import pytest

from aiomoqt.agent.reader import Reader
from aiomoqt.agent.spec import SubscribeSpec
from aiomoqt.media.loc import LocFrame
from aiomoqt.messages.subscribe import SubscribeOk
from aiomoqt.utils.media_stats import SOURCE_FRINDELL_MEDIA, MediaAnalysis
from aiomoqt.utils.stats import TrackStats, send_time_us
from aiomoqt.utils.timing import (
    BENCH_TIMESTAMP, CLOCK_ID, LOC_TIMESCALE, LOC_TIMESTAMP,
    OBJECT_TIMESTAMP, SOURCE_BENCH, SOURCE_FRINDELL, SOURCE_LOC, TIMESCALE,
    TIMESTAMP_MAPPING, TIMESTAMP_ORIGIN, TrackTiming,
    capture_time_us,
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
        10, W * 30000 // 1_000_000, 60060, 1001)}, vi64)
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
        0, W * 48000 // 1_000_000, 960, 0)})
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


@pytest.mark.parametrize("base_s", [
    9e16 / 1000,                    # ~2.8 million years ahead
    1.0,                            # 1970
    946_684_799,                    # just before 2000
], ids=["far-future", "1970", "pre-2000"])
def test_mapping_anchor_is_not_restricted_to_recent_wall_clock(base_s):
    base = int(base_s * 1000)
    timing = _timing({TIMESCALE: 1000, TIMESTAMP_MAPPING: encode_mapping(
        0, base, 33, 0)})
    assert timing.timestamp(1, 0, None) == base + 33


@pytest.mark.parametrize("clock", [{}, {CLOCK_ID: 0}])
def test_timestamp_group_ids_accept_zero_mapping_anchor(clock):
    # draft-frindell-moq-timestamp-00 Appendix A.3.
    timing = _timing({TIMESCALE: 1_000_000, **clock,
                      TIMESTAMP_MAPPING: encode_mapping(0, 0, 1, 0)})
    assert capture_time_us(timing, W, 0, None) == (W, SOURCE_FRINDELL)


def test_fixed_cadence_accepts_zero_anchor_with_wall_clock_origin():
    # draft-frindell-moq-timestamp-00 Appendix A.1.
    origin = W * 30_000 // 1_000_000
    timing = _timing({TIMESCALE: 30_000, CLOCK_ID: 0,
                      TIMESTAMP_ORIGIN: origin,
                      TIMESTAMP_MAPPING: encode_mapping(0, 0, 60060, 1001)})
    assert timing.media_us(1, 1, None) == (origin + 60060 + 1001) * 1_000_000 // 30_000


def test_plausible_wall_clock_mapping_and_clock_id_bases_pass():
    _timing({TIMESCALE: 30000, TIMESTAMP_MAPPING: encode_mapping(
        0, W * 30000 // 1_000_000, 60060, 1001)})
    # An archive captured years ago is still wall clock.
    _timing({TIMESCALE: 1000, TIMESTAMP_MAPPING: encode_mapping(
        0, 1_262_304_000_000, 33, 0)})                  # 2010
    # With CLOCK_ID the base has no wall-clock meaning: any value.
    _timing({TIMESCALE: 1000, CLOCK_ID: 7,
             TIMESTAMP_MAPPING: encode_mapping(0, 1, 33, 0)})



@pytest.mark.parametrize("props", [
    # A.1 mapping in RFC 9000 varints, read as vi64: (0, 0, 0, 178013161).
    {TIMESCALE: 30_000, CLOCK_ID: 0, TIMESTAMP_ORIGIN: W * 30_000 // 1_000_000,
     TIMESTAMP_MAPPING: bytes.fromhex("00008000ea9c43e9")},
    {TIMESCALE: 1000, TIMESTAMP_MAPPING: encode_mapping(0, 9 * 10**16, 33, 0)},
], ids=["rfc9000-as-vi64", "base-9e16"])
def test_media_analysis_reports_far_off_capture_times_without_latency(
        props, caplog):
    timing = _timing(props)
    a = MediaAnalysis(timing_of=lambda name: timing)
    a.add_track('video', role='video')
    a.start(W)
    logger = logging.getLogger('aiomoqt.utils.media_stats')
    logger.addHandler(caplog.handler)
    try:
        # rfc9000-as-vi64 stays within a day until object 15, which drops it.
        for g in range(2):
            for o in range(20):
                recv = W + (20 * g + o) * 33_000
                a.on_arrival('video', _msg(g, o, {}), recv, g, 0,
                             LocFrame(b"x", o == 0))
    finally:
        logger.removeHandler(caplog.handler)
    rows, _ = a.summary(W + 1_000_000)
    r = rows[0]
    assert r['objects'] == 40 and r['ts_malformed'] == 0
    assert SOURCE_FRINDELL_MEDIA in r['ts_source']
    assert not r['wall'] and r['lat_p50_ms'] is None
    assert r['lat_max_ms'] is None and r['ts_implausible'] >= 10
    warns = [x for x in caplog.records if 'not within a day' in x.message]
    assert len(warns) == 1 and warns[0].levelno == logging.WARNING
