"""Unified track statistics.

These pin the behaviours the two former implementations disagreed on —
delta-vs-cumulative accounting, stride-aware loss, datagram keying, and
status-object exclusion — so a future split can't quietly reintroduce
two answers for the same traffic.
"""
import time


from aiomoqt.utils.stats import TrackStats, SenderStats

TS_EXT = 0x20


class Obj:
    """Minimal stand-in for a subgroup-stream object."""

    def __init__(self, group_id, object_id, subgroup_id=0,
                 send_us=None, status=None):
        self.group_id = group_id
        self.object_id = object_id
        self.subgroup_id = subgroup_id
        self.extensions = {TS_EXT: send_us} if send_us else None
        if status is not None:
            self.status = status


class Dgram:
    """Datagram object — no subgroup_id attribute at all."""

    def __init__(self, group_id, object_id, send_us=None):
        self.group_id = group_id
        self.object_id = object_id
        self.extensions = {TS_EXT: send_us} if send_us else None


def _now_us():
    return int(time.time() * 1_000_000)


# -- accounting ------------------------------------------------------

def test_counts_objects_bytes_and_groups():
    s = TrackStats()
    for g in range(3):
        for o in range(4):
            s.on_object(Obj(g, o), 100, _now_us())
    summ = s.summary()
    assert summ['objects'] == 12
    assert summ['bytes'] == 1200
    assert summ['groups'] == 3


def test_snapshot_returns_deltas_not_totals():
    s = TrackStats()
    us = _now_us()
    for o in range(5):
        s.on_object(Obj(0, o), 10, us)
    first = s.snapshot()
    assert first['rx_objs'] == 5 and first['rx_bytes'] == 50
    second = s.snapshot()
    assert second['rx_objs'] == 0 and second['rx_bytes'] == 0
    assert second['total_objs'] == 5      # totals stay cumulative
    for o in range(5, 8):
        s.on_object(Obj(0, o), 10, us)
    third = s.snapshot()
    assert third['rx_objs'] == 3


def test_snapshot_and_interval_row_are_independent():
    """A controller polling snapshot() must not consume the deltas a
    display is accumulating for its next row, and vice versa."""
    s = TrackStats()
    us = _now_us()
    for o in range(6):
        s.on_object(Obj(0, o), 10, us)
    s.snapshot()                       # consume the snapshot delta
    time.sleep(0.01)
    row = s.interval_row()             # row still sees all 6
    assert '6' in row


# -- loss ------------------------------------------------------------

def test_detects_gap_as_loss():
    s = TrackStats()
    us = _now_us()
    for o in (0, 1, 3, 4):             # 2 missing
        s.on_object(Obj(0, o), 10, us)
    assert s.summary()['lost'] == 1


def test_stride_aware_loss_no_false_positive():
    """Objects striped across 4 subgroups skip by 4 — not loss."""
    s = TrackStats()
    us = _now_us()
    for o in range(0, 40, 4):
        s.on_object(Obj(0, o, subgroup_id=0), 10, us)
    assert s.summary()['lost'] == 0


def test_stride_aware_loss_detects_real_gap():
    s = TrackStats()
    us = _now_us()
    for o in (0, 4, 8, 16):            # 12 missing at stride 4
        s.on_object(Obj(0, o, subgroup_id=0), 10, us)
    assert s.summary()['lost'] == 1


def test_subgroups_tracked_independently():
    s = TrackStats()
    us = _now_us()
    for sg in (0, 1):
        for o in range(3):
            s.on_object(Obj(0, o, subgroup_id=sg), 10, us)
    assert s.summary()['lost'] == 0
    assert s.summary()['objects'] == 6


def test_datagram_keys_on_group_without_subgroup():
    s = TrackStats()
    us = _now_us()
    for o in (0, 1, 3):
        s.on_object(Dgram(0, o), 10, us)
    assert s.summary()['lost'] == 1


def test_out_of_order_counted_not_lost():
    s = TrackStats()
    us = _now_us()
    for o in (0, 1, 2, 1):
        s.on_object(Obj(0, o), 10, us)
    assert s.summary()['ooo'] >= 1


def test_reorder_that_fills_a_gap_is_not_lost():
    s = TrackStats()
    us = _now_us()
    for o in (0, 1, 3, 2, 4):
        s.on_object(Obj(0, o), 10, us)
    summ = s.summary()
    assert (summ['lost'], summ['ooo'], summ['objects']) == (0, 1, 5)


def test_repeat_of_a_received_object_does_not_reverse_loss():
    s = TrackStats()
    us = _now_us()
    for o in (0, 1, 3, 1):
        s.on_object(Obj(0, o), 10, us)
    assert (s.summary()['lost'], s.summary()['ooo']) == (1, 1)


def test_filled_gap_reverses_loss_in_snapshot_deltas():
    s = TrackStats()
    us = _now_us()
    for o in (0, 1, 3):
        s.on_object(Obj(0, o), 10, us)
    assert s.snapshot()['iv_lost'] == 1
    s.on_object(Obj(0, 2), 10, us)
    snap = s.snapshot()
    assert (snap['iv_lost'], snap['loss']) == (-1, 0)


def test_gap_across_a_snapshot_is_detected():
    s = TrackStats()
    us = _now_us()
    for o in (0, 1):
        s.on_object(Obj(0, o), 10, us)
    s.snapshot()
    s.on_object(Obj(0, 3), 10, us)
    assert s.summary()['lost'] == 1


def test_filled_gap_across_subgroup_switch():
    s = TrackStats()
    us = _now_us()
    for o in (0, 1, 3):
        s.on_object(Obj(0, o, subgroup_id=0), 10, us)
    s.on_object(Obj(0, 0, subgroup_id=1), 10, us)
    s.on_object(Obj(0, 2, subgroup_id=0), 10, us)
    assert s.summary()['lost'] == 0


# -- latency / jitter ------------------------------------------------

def test_latency_from_timestamp_extension():
    s = TrackStats()
    us = _now_us()
    for o in range(20):
        s.on_object(Obj(0, o, send_us=us - 5000), 10, us)   # 5 ms
    summ = s.summary()
    assert 4.5 <= summ['lat_mean'] <= 5.5
    assert 4.5 <= summ['lat_p50'] <= 5.5


def test_latency_from_loc_timestamp_unless_timescaled():
    # loc-04: TIMESTAMP (0x10) is wall-clock µs only without TIMESCALE (0x08).
    s = TrackStats()
    us = _now_us()
    o = Obj(0, 0)
    o.extensions = {0x10: us - 7000}
    s.on_object(o, 10, us)
    o = Obj(0, 1)
    o.extensions = {0x10: 90_000, 0x08: 90_000}
    s.on_object(o, 10, us)
    assert s.lat_count == 1
    assert 6.5 <= s.summary()['lat_mean'] <= 7.5


def test_absurd_timestamps_rejected():
    """Clock skew / deframer garbage must not poison the stats."""
    s = TrackStats()
    us = _now_us()
    s.on_object(Obj(0, 0, send_us=us - 5000), 10, us)        # good
    s.on_object(Obj(0, 1, send_us=us + 10_000_000), 10, us)  # far future
    s.on_object(Obj(0, 2, send_us=us - 999_000_000), 10, us) # far past
    assert s.lat_count == 1


def test_objects_without_timestamps_still_counted():
    s = TrackStats()
    us = _now_us()
    for o in range(4):
        s.on_object(Obj(0, o), 10, us)      # no extensions
    assert s.summary()['objects'] == 4
    assert s.lat_count == 0


def test_status_objects_excluded_from_data_counters():
    """Status objects are wire signals, not media — counting them
    would inflate object counts and corrupt loss detection."""
    s = TrackStats()
    us = _now_us()
    s.on_object(Obj(0, 0), 10, us)
    s.on_object(Obj(0, 1, status=3), 10, us)   # non-NORMAL
    assert s.summary()['objects'] == 1


def test_windowed_opt_out_still_reports_interval_latency():
    """Table consumers skip rolling-window upkeep but must keep their
    own interval percentiles."""
    s = TrackStats(windowed=False)
    us = _now_us()
    for o in range(10):
        s.on_object(Obj(0, o, send_us=us - 3000), 10, us)
    time.sleep(0.01)
    row = s.interval_row()
    assert 'p99' in row
    assert s.summary()['lat_mean'] > 0


# -- memory ----------------------------------------------------------

def test_reservoir_bounds_memory():
    s = TrackStats(reservoir=100, iv_reservoir=100)
    us = _now_us()
    for o in range(5000):
        s.on_object(Obj(0, o, send_us=us - 1000), 10, us)
    assert len(s._lat_res.items) == 100
    assert s.lat_count == 5000          # counted, not stored


# -- sender ----------------------------------------------------------

def test_sender_tracks_rates_without_latency():
    p = SenderStats()
    for g in range(2):
        p.on_group()
        for _ in range(10):
            p.on_object(4096)
    assert p.total_groups == 2
    assert p.total_objects == 20
    assert p.total_bytes == 81920
    assert not hasattr(p, 'jitter')     # no receive clock, no latency


def test_headers_share_column_names():
    """Sender and receiver tables must line up on the shared columns."""
    for col in ('Interval', 'Grps', 'GrpRate', 'Objs', 'ObjRate',
                'Bitrate'):
        assert col in TrackStats.header()
        assert col in SenderStats.header()
    for col in ('Latency', 'Jitter', 'Loss'):
        assert col in TrackStats.header()
        assert col not in SenderStats.header()


def test_empty_summary_is_falsy():
    assert TrackStats().summary() == {}


def test_datagram_groups_reported_as_not_available():
    """A receiver cannot count groups for datagram delivery: there is no
    subgroup stream to observe opening, and a fully-lost group is never
    seen at all. Reporting a number would look authoritative when it is
    only a lower bound."""
    s = TrackStats(windowed=False)
    us = _now_us()
    for g in range(3):
        for o in range(4):
            s.on_object(Dgram(g, o), 100, us)
    time.sleep(0.01)
    row = s.interval_row()
    assert 'n/a' in row, row
    assert s.summary()['datagram'] is True


def test_stream_groups_still_reported():
    s = TrackStats(windowed=False)
    us = _now_us()
    for g in range(3):
        for o in range(4):
            s.on_object(Obj(g, o), 100, us)
    time.sleep(0.01)
    row = s.interval_row()
    assert 'n/a' not in row, row
    assert s.summary()['datagram'] is False
    assert s.summary()['groups'] == 3


# -- whole-group loss ------------------------------------------------

def _feed_groups(s, groups, size=8, subgroups=4, skip=()):
    """Interleave `subgroups` streams per group; `skip` holds
    (group, subgroup) pairs that never arrive."""
    us = _now_us()
    for g in groups:
        for o in range(size):
            sg = o % subgroups
            if (g, sg) not in skip:
                s.on_object(Obj(g, o, subgroup_id=sg), 10, us)


def test_a_missing_subgroup_counts_as_lost():
    s = TrackStats(group_loss=True)
    _feed_groups(s, range(6), skip={(2, 1)})
    assert s.summary()['lost'] == 2


def test_full_groups_count_no_loss():
    s = TrackStats(group_loss=True)
    _feed_groups(s, range(6))
    assert s.summary()['lost'] == 0


def test_a_mid_group_join_is_not_loss():
    s = TrackStats(group_loss=True)
    us = _now_us()
    for o in (5, 6, 7):
        s.on_object(Obj(0, o, subgroup_id=o % 4), 10, us)
    _feed_groups(s, range(1, 5))
    assert s.summary()['lost'] == 0


def test_a_late_subgroup_reverses_settled_loss():
    s = TrackStats(group_loss=True)
    _feed_groups(s, range(5), skip={(1, 3)})
    assert s.summary()['lost'] == 2
    us = _now_us()
    for o in (3, 7):
        s.on_object(Obj(1, o, subgroup_id=3), 10, us)
    assert s.summary()['lost'] == 0


def test_in_stream_gap_is_not_counted_twice():
    s = TrackStats(group_loss=True)
    us = _now_us()
    _feed_groups(s, range(2), size=12)
    for o in range(12):
        if o != 5:          # subgroup 1 carries 1, 5, 9: a gap, not a tail
            s.on_object(Obj(2, o, subgroup_id=o % 4), 10, us)
    _feed_groups(s, range(3, 6), size=12)
    assert s.summary()['lost'] == 1


def test_group_loss_is_opt_in():
    s = TrackStats()
    _feed_groups(s, range(6), skip={(2, 1)})
    assert s.summary()['lost'] == 0
