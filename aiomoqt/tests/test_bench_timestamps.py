"""Generated tracks can carry draft-frindell-moq-timestamp-00 timing
(PublishedTrack(timestamps=), --timestamps on the bench tools), and the
receiving resolver reproduces the send times LOC TIMESTAMP carries."""
import asyncio

import pytest

from aiomoqt.track import PublishedTrack, SubscribedTrack, VideoTrack
from aiomoqt.utils.stats import TrackStats
from aiomoqt.utils.timing import (
    LOC_TIMESTAMP, OBJECT_TIMESTAMP, SOURCE_FRINDELL, TIMESCALE,
    TIMESTAMP_MAPPING, TimestampWriter, TrackTiming, capture_time_us,
    decode_mapping, zigzag_decode,
)

from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_immutable_properties import _loopback

W = 1_767_225_600_000_000


@pytest.mark.parametrize("rate, timescale, om", [
    (30, 3_000_000, 100_000), (200, 1_000_000, 5_000),
    (29.97, 2_997_000_000, 100_000_000), (0.5, 1_000_000, 2_000_000)])
def test_paced_writer_picks_exact_ticks(rate, timescale, om):
    w = TimestampWriter('frindell', rate, 60, 4, W)
    assert w.timescale == timescale
    assert w.mapping == (4, W * (timescale // 1_000_000), 60 * om, om)
    assert w.scheduled_us(5, 2) == W + int((60 + 2) * 1e6 / rate)


@pytest.mark.parametrize("vi64", [False, True], ids=["d16", "d18"])
@pytest.mark.parametrize("rate", [0, 30, 200])
def test_written_stamps_resolve_to_the_send_time(vi64, rate):
    w = TimestampWriter('both', rate, 10, 0, W)
    props = w.track_properties(vi64)
    assert props[TIMESCALE] == w.timescale
    assert (TIMESTAMP_MAPPING in props) == (rate > 0)
    if rate:
        assert decode_mapping(props[TIMESTAMP_MAPPING], vi64) == w.mapping
    timing = TrackTiming.from_properties(props, vi64)
    for g, o in ((0, 0), (3, 7), (1000, 9)):
        exts = w.stamp(g, o)
        assert capture_time_us(timing, g, o, exts) == (
            exts[LOC_TIMESTAMP], SOURCE_FRINDELL)


def test_on_schedule_objects_carry_no_correction(monkeypatch):
    import aiomoqt.utils.timing as timing
    w = TimestampWriter('frindell', 100, 10, 0, W)
    monkeypatch.setattr(timing.time, "time", lambda: (W + 130_000) / 1e6)
    assert w.stamp(1, 3) == {}                       # due at W + 130 ms
    assert zigzag_decode(w.stamp(1, 2)[OBJECT_TIMESTAMP]) == 10_000
    assert zigzag_decode(w.stamp(1, 4)[OBJECT_TIMESTAMP]) == -10_000


def test_loc_mode_writes_only_loc():
    assert set(TimestampWriter('loc', 30, 10, 0, W).stamp(0, 0)) == {
        LOC_TIMESTAMP}
    assert TimestampWriter('loc', 30, 10, 0, W).track_properties(True) == {}


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        PublishedTrack(None, "n", "t", timestamps="ntp")


_TRACKS = {
    # name: (rate, video)
    "paced": (100, False),
    "unpaced": (0, False),
    "video": (50, True),
}


def _port(draft, use_quic):
    return 17800 + 2 * [16, 18, 22].index(draft) + (not use_quic)


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", [16, 18, 22])
async def test_generated_frindell_round_trips(draft, use_quic):
    publishes = asyncio.Queue()
    seen = {name: [] for name in _TRACKS}
    async with _loopback(_port(draft, use_quic), draft, use_quic,
                         publishes) as session:
        for name, (rate, video) in _TRACKS.items():
            if video:
                VideoTrack(session, "ts", name, resolution="240p", fps=rate,
                           gop_seconds=0.2, timestamps='both').attach()
            else:
                PublishedTrack(session, "ts", name, object_size=32,
                               group_size=8, num_subgroups=2, rate=rate,
                               timestamps='both').attach()
        await session.publish_namespace(namespace="ts", wait_response=True)
        peer, _ = await asyncio.wait_for(publishes.get(), 5.0)
        subs, stats = {}, {}
        for name in _TRACKS:
            stats[name] = TrackStats(windowed=False)
            sub = SubscribedTrack(peer, "ts", name)

            def on_object(msg, size, recv, gid, sg, _n=name, _s=sub):
                if msg.status:
                    return
                stats[_n].on_object(msg, size, recv, gid, sg)
                if len(seen[_n]) < 400:
                    seen[_n].append((msg.extensions, capture_time_us(
                        _s.timing, gid, msg.object_id, msg.extensions)))
            sub.on_object = on_object
            stats[name].timing = sub.timing
            subs[name] = sub
            await sub.subscribe()
        for _ in range(300):
            if all(len(v) >= 30 for v in seen.values()):
                break
            await asyncio.sleep(0.01)
    for name, (rate, _) in _TRACKS.items():
        assert len(seen[name]) >= 30, name
        assert subs[name].timing.timescale is not None
        assert (subs[name].timing.mapping is not None) == (rate > 0), name
        for exts, (cap, src) in seen[name]:
            assert src == SOURCE_FRINDELL and cap == exts[LOC_TIMESTAMP]
        assert stats[name].ts_source == SOURCE_FRINDELL
        assert stats[name].lat_count > 0
        if rate:
            # A paced source stays near its schedule; loaded CI runners lag tens of ms.
            corr = [abs(zigzag_decode(e.get(OBJECT_TIMESTAMP, 0)))
                    for e, _ in seen[name]]
            ticks = subs[name].timing.timescale // 1000 * 250
            assert sorted(corr)[len(corr) // 2] < ticks, name
