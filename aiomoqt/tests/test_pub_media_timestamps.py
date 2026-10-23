"""pub_media's paced sources stamp each frame with its scheduled
wall-clock capture time (start anchor + media offset), so the spacing of
LOC timestamps is exact whatever the send-time jitter."""
import asyncio
import struct
import time
from types import SimpleNamespace

import pytest

from aiomoqt.media.cmaf import CmafChunker, chunk_decode_time
from aiomoqt.tools import pub_media


class _Track:
    demand = [1]
    timescale = None

    def __init__(self):
        self.stamps = []
        self.payloads = []

    async def send_frame(self, payload, *, key_frame, timestamp,
                         group_id=None):
        self.stamps.append(timestamp)
        self.payloads.append(payload)

    async def finish(self):
        pass


def _steps(stamps):
    return {b - a for a, b in zip(stamps, stamps[1:])}


@pytest.mark.asyncio
async def test_a_paced_tone_is_stamped_on_schedule():
    track = _Track()
    before = int(time.time() * 1_000_000)
    await pub_media._feed_tone(
        track, SimpleNamespace(duration=0.2, freq=440.0, no_pace=False),
        pub_media._TrackStats())
    assert len(track.stamps) >= 5
    assert _steps(track.stamps) == {pub_media._FRAME_MS * 1000}
    assert 0 <= track.stamps[0] - before < 1_000_000      # wall clock


@pytest.mark.asyncio
async def test_a_paced_mp4_track_is_stamped_on_schedule():
    offsets = [0, 33_366, 66_733, 100_100, 133_466]       # 29.97 fps
    source = SimpleNamespace(samples=lambda: iter(
        SimpleNamespace(timestamp_us=t, payload=b"x", key_frame=not i)
        for i, t in enumerate(offsets)))
    track = _Track()
    await pub_media._feed_mp4_track(
        track, source,
        SimpleNamespace(duration=1.0, no_pace=False, loop=False),
        pub_media._TrackStats())
    assert [s - track.stamps[0] for s in track.stamps] == offsets


# trun first-sample duration in CmafChunker's fixed one-sample moof.
_TRUN_DURATION = 88


@pytest.mark.asyncio
async def test_looped_cmaf_tfdt_follows_the_paced_timestamps():
    # AAC-like: whole frames, then a short last one (padding trimmed).
    scale, durs = 48_000, [1024, 1024, 1024, 256]
    starts = [sum(durs[:i]) for i in range(len(durs))]
    source = SimpleNamespace(timescale=scale, samples=lambda: iter(
        SimpleNamespace(timestamp_us=t * 1_000_000 // scale, duration=d,
                        payload=b"x", key_frame=True)
        for t, d in zip(starts, durs)))
    gap_us = 1024 * 1_000_000 // scale
    pass_us = starts[-1] * 1_000_000 // scale + gap_us
    track = _Track()
    await pub_media._feed_mp4_track(
        track, source,
        SimpleNamespace(duration=(3 * pass_us - gap_us // 2) / 1e6,
                        no_pace=True, loop=True),
        pub_media._TrackStats(), gap_us=gap_us,
        wrap=pub_media._cmaf_wrap(CmafChunker(source), video=False),
        clock=(time.monotonic(), 0))
    assert len(track.payloads) == 3 * len(durs)
    tfdt = [chunk_decode_time(p) for p in track.payloads]
    dur = [struct.unpack_from('>I', p, _TRUN_DURATION)[0]
           for p in track.payloads]
    # Gapless across loops, and on the pacing clock within a tick.
    assert all(a + d == b for a, d, b in zip(tfdt, dur, tfdt[1:]))
    assert all(abs(t - us * scale / 1e6) <= 1
               for t, us in zip(tfdt, track.stamps))


@pytest.mark.asyncio
async def test_a_loc_timescale_stamps_in_its_ticks():
    track = _Track()
    track.timescale = 90_000
    await pub_media._send(track, pub_media._TrackStats(), b"x", True,
                          timestamp=2_000_000)
    assert track.stamps == [180_000]                     # 2 s at 90 kHz


def test_an_unstamped_frame_takes_the_wall_clock_in_track_ticks():
    from types import SimpleNamespace as NS
    from aiomoqt.media import LocTrackPublisher
    session = NS(negotiated_draft=18)
    now = time.time()
    assert abs(LocTrackPublisher(session, "ns", "v")._now()
               - now * 1_000_000) < 1_000_000
    assert abs(LocTrackPublisher(session, "ns", "v", timescale=90_000)._now()
               - now * 90_000) < 90_000


@pytest.mark.asyncio
async def test_the_stats_line_reports_shed_frames(capsys):
    # One relay: a lane that stopped writing sheds, and the line says so.
    track = SimpleNamespace(shed={0: 12}, demand=(1, 1))
    pub = SimpleNamespace(tracks={"video": track}, sessions=[object()])
    stats = {"video": pub_media._TrackStats()}
    task = asyncio.ensure_future(pub_media._report_stats(stats, 0.01, pub))
    await asyncio.sleep(0.03)
    task.cancel()
    line = capsys.readouterr().out.splitlines()[0]
    assert "shed 0:12" in line
