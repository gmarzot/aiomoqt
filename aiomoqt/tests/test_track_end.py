"""PublishedTrack.end(): each subscription's streams close, then it gets
PUBLISH_DONE TRACK_ENDED with the streams it was sent (§10.11, §5.1.1);
the session and the other tracks carry on."""
import asyncio

from aiomoqt.types import SubscribeDoneCode

from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_subscription_filter import (
    _NS, _TRANSPORTS, _loopback,
)

_BASE_PORT = 16360


def _port(case: int, use_quic: bool) -> int:
    return _BASE_PORT + 2 * case + (not use_quic)


async def _done_for(rx, request_id):
    done = asyncio.get_running_loop().create_future()
    rx.register_publish_done_handler(request_id, done.set_result)
    return done


@requires_certs
@_TRANSPORTS
async def test_ending_a_track_ends_only_its_subscription(use_quic):
    async with _loopback(_port(0, use_quic), use_quic) as (tracks, rx, objects):
        await rx.subscribe(_NS, "audio", forward=1, wait_response=True)
        video = await rx.subscribe(_NS, "video", forward=1, wait_response=True)
        done = await _done_for(rx, video.request_id)
        await asyncio.sleep(0.1)
        assert objects["video"]
        tracks["video"].end()
        status = await asyncio.wait_for(done, 5.0)
        assert status.status_code == SubscribeDoneCode.TRACK_ENDED
        assert status.stream_count >= 1
        assert tracks["video"]._subs[0].open_streams == 0
        video_seen, audio_seen = len(objects["video"]), len(objects["audio"])
        await asyncio.sleep(0.1)
        assert len(objects["video"]) == video_seen
        assert len(objects["audio"]) > audio_seen
        assert rx._close_err is None


@requires_certs
@_TRANSPORTS
async def test_a_subscription_sent_nothing_ends_with_no_streams(use_quic):
    async with _loopback(_port(1, use_quic), use_quic) as (tracks, rx, _):
        video = await rx.subscribe(_NS, "video", forward=0, wait_response=True)
        done = await _done_for(rx, video.request_id)
        tracks["video"].end()
        status = await asyncio.wait_for(done, 5.0)
        assert status.status_code == SubscribeDoneCode.TRACK_ENDED
        assert status.stream_count == 0
