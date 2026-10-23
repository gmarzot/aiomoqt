"""draft-frindell-moq-timestamp-00 validity rules (§3-§5). A subscriber
treats a track that breaks them as malformed: SubscribedTrack cancels it
(MOQT §12.1) by default and only reports it with
timestamp_errors='report'. The other track on the session is untouched."""
import asyncio

import pytest

from aiomoqt.messages.base import KeyValuePairs
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.utils.stats import TrackStats
from aiomoqt.utils.timing import (
    CLOCK_ID, LOC_TIMESTAMP, OBJECT_TIMESTAMP, TIMESCALE, TIMESTAMP_MAPPING,
    TIMESTAMP_ORIGIN, TimestampMalformed, TrackTiming, capture_time_us,
    check_object, encode_mapping, zigzag_encode,
)

from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_immutable_properties import _loopback

_BASE_PORT = 17700
_NS = "malformed"


def _dups(props, *types):
    kv = KeyValuePairs(props)
    kv.duplicates = frozenset(types)
    return kv


# -- track level (§3, §4) ----------------------------------------------

@pytest.mark.parametrize("vi64", [False, True], ids=["d16", "d18"])
@pytest.mark.parametrize("props, dups, why", [
    ({TIMESCALE: 1000}, {TIMESCALE}, "more than once"),
    ({TIMESCALE: 1000, OBJECT_TIMESTAMP: 5}, (), "as a Track Property"),
    ({CLOCK_ID: 0}, (), "TIMESCALE missing"),
    ({TIMESCALE: 0, CLOCK_ID: 0}, (), "TIMESCALE missing or 0"),
    ({TIMESCALE: 1000, TIMESTAMP_ORIGIN: 5}, (), "without CLOCK_ID"),
    ({TIMESCALE: 1000, TIMESTAMP_MAPPING: "3"}, (), "four integers"),
    ({TIMESCALE: 1000, TIMESTAMP_MAPPING: "5"}, (), "four integers"),
    ({TIMESCALE: 1000, TIMESTAMP_MAPPING: "trail"}, (), "four integers"),
    ({TIMESCALE: 1000, TIMESTAMP_MAPPING: "cut"}, (), "four integers"),
], ids=["dup", "object-ts-on-track", "no-timescale", "timescale-0",
        "origin-without-clock", "mapping-3", "mapping-5", "mapping-trail",
        "mapping-truncated"])
def test_malformed_track_properties(vi64, props, dups, why):
    props = dict(props)
    raw = props.get(TIMESTAMP_MAPPING)
    if raw is not None:
        four = encode_mapping(0, 1, 2, 3, vi64)
        props[TIMESTAMP_MAPPING] = {
            "3": four[:-1], "5": four + four[-1:], "trail": four + b"\xff",
            "cut": four[:-1] + (b"\xc0" if not vi64 else b"\xff")}[raw]
    with pytest.raises(TimestampMalformed, match=why):
        TrackTiming.from_properties(props, vi64, dups)


def test_clock_id_zero_with_origin_is_valid():
    t = TrackTiming.from_properties(
        {TIMESCALE: 1000, CLOCK_ID: 0, TIMESTAMP_ORIGIN: 5}, True)
    assert t.wall_clock and t.origin == 5


# -- object level (§3, §4.4, §5) ---------------------------------------

_MAPPED = TrackTiming(timescale=1000, mapping=(10, 0, 1000, 10))


@pytest.mark.parametrize("timing, gid, exts, why", [
    (_MAPPED, 10, {TIMESCALE: 1000}, "TIMESCALE as an Object Property"),
    (None, 0, {CLOCK_ID: 1}, "CLOCK_ID as an Object Property"),
    (None, 0, {OBJECT_TIMESTAMP: 5}, "without TIMESCALE"),
    (TrackTiming(), 0, {OBJECT_TIMESTAMP: 5}, "without TIMESCALE"),
    (_MAPPED, 9, None, "outside"),                      # mapped -1000
    (_MAPPED, 9, {OBJECT_TIMESTAMP: zigzag_encode(999)}, "outside"),
    (TrackTiming(timescale=1000), 0, {OBJECT_TIMESTAMP: 0},
     None),
    (_MAPPED, 10, _dups({OBJECT_TIMESTAMP: 2}, OBJECT_TIMESTAMP),
     "more than once"),
], ids=["track-id-on-object", "clock-id-on-object", "no-track-timing",
        "undeclared-track", "mapped-negative", "correction-short",
        "explicit-zero-ok", "duplicate-object-timestamp"])
def test_malformed_objects(timing, gid, exts, why):
    if why is None:
        check_object(timing, gid, 0, exts)
        return
    with pytest.raises(TimestampMalformed, match=why):
        check_object(timing, gid, 0, exts)
    with pytest.raises(TimestampMalformed):
        capture_time_us(timing, gid, 0, exts)


def test_negative_mapping_is_valid_once_corrected():
    exts = {OBJECT_TIMESTAMP: zigzag_encode(1000)}
    assert _MAPPED.timestamp(9, 0, exts) == 0


def test_timestamp_past_2_64_is_malformed():
    t = TrackTiming(timescale=1, mapping=(0, (1 << 64) - 1, 1, 0))
    assert t.timestamp(0, 0, None) == (1 << 64) - 1
    with pytest.raises(TimestampMalformed, match="outside"):
        t.timestamp(1, 0, None)


# -- the library cancels, tools may report -----------------------------

class _Stamped(PublishedTrack):
    """Objects carrying `exts`, one per group, paced."""

    def __init__(self, *a, exts=None, **kw):
        super().__init__(*a, **kw)
        self.exts = exts

    async def produce(self, out):
        for group_id in range(100):
            await out.write(group_id, 0, b"x" * 16,
                            extensions=dict(self.exts), group_start=True)
            await asyncio.sleep(0.01)


def _port(case, draft, use_quic):
    return _BASE_PORT + 10 * case + 2 * [16, 18].index(draft) + (not use_quic)


_CASES = {
    # name: (track_properties, object properties)
    "good": ({TIMESCALE: 1000}, {OBJECT_TIMESTAMP: 1, LOC_TIMESTAMP: 1}),
    "bad-track": ({TIMESCALE: 1000, TIMESTAMP_ORIGIN: 3},
                  {LOC_TIMESTAMP: 1}),
    "bad-object": (None, {OBJECT_TIMESTAMP: 7}),
}


async def _run(port, draft, use_quic, policy):
    publishes = asyncio.Queue()
    got = {name: 0 for name in _CASES}
    async with _loopback(port, draft, use_quic, publishes) as session:
        for name, (props, exts) in _CASES.items():
            _Stamped(session, namespace=_NS, trackname=name,
                     track_properties=props, exts=exts).attach()
        await session.publish_namespace(namespace=_NS, wait_response=True)
        peer, _ = await asyncio.wait_for(publishes.get(), 5.0)
        cancelled = []
        real = peer._malformed_track

        def spy(alias, reason, stream_id=None):
            cancelled.append(alias)
            real(alias, reason, stream_id)
        peer._malformed_track = spy
        subs = {}
        for name in _CASES:
            def on_object(msg, *a, _n=name):
                got[_n] += 1
            subs[name] = SubscribedTrack(peer, _NS, name, on_object=on_object,
                                         timestamp_errors=policy)
            await subs[name].subscribe()
        for _ in range(300):
            if got["good"] >= 10 and all(
                    s.malformed or got[n] >= 3 for n, s in subs.items()):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        return subs, got, (cancelled, peer._close_err)


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", [16, 18])
async def test_malformed_timing_cancels_only_that_track(draft, use_quic):
    subs, got, (cancelled, close_err) = await _run(
        _port(0, draft, use_quic), draft, use_quic, 'cancel')
    assert subs["good"].malformed is None and got["good"] >= 10
    assert "without CLOCK_ID" in subs["bad-track"].malformed
    assert got["bad-track"] == 0
    assert "without TIMESCALE" in subs["bad-object"].malformed
    assert got["bad-object"] == 0
    assert sorted(cancelled) == sorted(
        [subs["bad-track"].track_alias, subs["bad-object"].track_alias])
    assert close_err is None


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", [16, 18])
async def test_report_policy_keeps_receiving(draft, use_quic):
    subs, got, _ = await _run(_port(1, draft, use_quic), draft,
                              use_quic, 'report')
    assert all(s.malformed is None for s in subs.values())
    assert all(n >= 3 for n in got.values()), got
    assert "without CLOCK_ID" in subs["bad-track"].timing_error
    stats = TrackStats(windowed=False)
    stats.timing = subs["bad-object"].timing
    assert stats._send_us(type("M", (), {
        "group_id": 0, "object_id": 0,
        "extensions": {OBJECT_TIMESTAMP: 7}})(), 0) is None
    assert stats.ts_malformed == 1


def _subscribed(props, policy='cancel'):
    from types import SimpleNamespace as NS
    from aiomoqt.context import profile_for
    sess = NS(_profile=profile_for(18), _pending_object_handlers={0: None},
              _timing_watch={})

    def sink(*a):
        pass
    sub = SubscribedTrack(sess, _NS, "t", on_object=sink,
                          timestamp_errors=policy)
    sess._pending_object_handlers[0] = sink
    sub.take_track_properties(NS(track_extensions=props, request_id=0,
                                 track_alias=3))
    return sub, sess, sink


def test_a_track_without_timing_keeps_its_handler_unwrapped():
    sub, sess, sink = _subscribed({0x3F: b"x"})
    assert sess._pending_object_handlers[0] is sink
    assert sub._handler() is sink
    # Only objects carrying a timing property are routed to the check.
    assert sess._timing_watch == {3: sub._checked_object}


def test_a_track_with_timing_checks_every_object():
    sub, sess, _ = _subscribed({TIMESCALE: 1000})
    assert sess._pending_object_handlers[0] == sub._checked_object
    assert sub._handler() == sub._checked_object
    assert sess._timing_watch == {}


def test_report_policy_installs_no_check():
    sub, sess, sink = _subscribed({TIMESCALE: 1000}, 'report')
    assert sess._pending_object_handlers[0] is sink
    assert sub._handler() is sink and sess._timing_watch == {}


@pytest.mark.parametrize("props, exts", [
    ({0x3F: b"x"}, {OBJECT_TIMESTAMP: 7}),
    ({TIMESCALE: 1000}, {TIMESCALE: 1000}),
], ids=["watched", "checked"])
def test_object_before_subscribe_returns_cancels_its_own_alias(props, exts):
    from types import SimpleNamespace as NS
    sub, sess, _ = _subscribed(props)
    cancelled = []
    sess._malformed_track = lambda alias, *a: cancelled.append(alias)
    check = sess._timing_watch.get(3) or sess._pending_object_handlers[0]
    check(NS(object_id=0, group_id=0, extensions=exts), 16, 0, 0, 0)
    assert sub.malformed and cancelled == [3]
    assert sess._timing_watch == {}
