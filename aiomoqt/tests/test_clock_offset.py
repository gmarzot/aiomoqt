"""A negative sender-stamped latency (receiver clock behind the sender's)
warns once and is marked; the figures stay."""
import time

import pytest

from aiomoqt.tools.sub_bench import BenchReporter
from aiomoqt.utils import media_stats, stats
from aiomoqt.utils.stats import LOC_TIMESTAMP, TrackStats

WARNING = "latency < 0: sender and receiver clocks differ by at least"


@pytest.fixture(autouse=True)
def _fresh_warning(monkeypatch):
    monkeypatch.setattr(stats, "_clock_warned", False, raising=False)


class Obj:
    def __init__(self, oid, send_us):
        self.group_id, self.object_id, self.subgroup_id = 0, oid, 0
        self.extensions = {LOC_TIMESTAMP: send_us}


def _feed(sink, n=20, ahead_ms=455):
    now = int(time.time() * 1e6)
    for oid in range(n):
        sink(Obj(oid, now + ahead_ms * 1000 + oid), 100, now + oid)


def test_track_stats_warns_once_and_marks(capsys):
    s = TrackStats()
    _feed(s.on_object)
    _feed(s.on_object)
    err = capsys.readouterr().err
    assert err.count(WARNING) == 1, err
    assert "at least 455 ms" in err
    assert "chrony/NTP" in err
    summ = s.summary()
    assert summ["clock_offset"] and summ["lat_mean"] < 0
    assert s.interval_row(time.monotonic() + 1).endswith(
        stats.CLOCK_OFFSET_MARK)


def test_positive_latency_is_not_marked(capsys):
    s = TrackStats()
    _feed(s.on_object, ahead_ms=-5)
    assert WARNING not in capsys.readouterr().err
    assert not s.summary()["clock_offset"]
    assert stats.CLOCK_OFFSET_MARK not in s.interval_row(
        time.monotonic() + 1)


def test_sub_bench_summary_marks_the_latency(capsys):
    r = BenchReporter()
    _feed(r.on_object)
    r.print_summary()
    out = capsys.readouterr()
    assert out.err.count(WARNING) == 1
    lat = [ln for ln in out.out.splitlines() if "Latency:" in ln]
    assert lat and lat[0].endswith("(clock offset)"), out.out


def test_media_analysis_warns_once_and_marks(capsys):
    t = media_stats.TrackAnalysis("video")
    now = int(time.time() * 1e6)
    for oid in range(20):
        t.observe(0, oid, 100, now + oid * 1000,
                  ts=now + 351_000 + oid * 1000, wall=True)
    err = capsys.readouterr().err
    assert err.count(WARNING) == 1, err
    iv = t.interval(now + 30_000, 1.0)
    assert iv["clock_offset"] and iv["lat_avg_ms"] < 0
    assert media_stats.format_interval(0, 1, [iv])[0].endswith(
        stats.CLOCK_OFFSET_MARK)
    summ = t.summary(now + 30_000)
    assert summ["clock_offset"]
    text = "\n".join(media_stats._track_summary(summ))
    assert "Latency:" in text and stats.CLOCK_OFFSET_MARK in text, text


def test_sub_millisecond_negative_is_rounding(capsys):
    s = TrackStats()
    now = int(time.time() * 1e6)
    for oid in range(20):
        s.on_object(Obj(oid, now + 400 + oid), 100, now + oid)
    assert WARNING not in capsys.readouterr().err
    assert not s.summary()["clock_offset"]
    assert stats.CLOCK_OFFSET_MARK not in s.interval_row(
        time.monotonic() + 1)


def test_media_analysis_ignores_sub_millisecond_negatives(capsys):
    t = media_stats.TrackAnalysis("video")
    now = int(time.time() * 1e6)
    for oid in range(20):
        t.observe(0, oid, 100, now + oid * 1000,
                  ts=now + 300 + oid * 1000, wall=True)
    assert WARNING not in capsys.readouterr().err
    assert not t.interval(now + 30_000, 1.0)["clock_offset"]
    assert not t.summary(now + 30_000)["clock_offset"]
