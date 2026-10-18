"""A REQUEST_UPDATE that fails ends the request it updated (§10.9.1): a
subscription with REQUEST_ERROR then PUBLISH_DONE UPDATE_FAILED, a FETCH
by resetting its data stream. Only a priority change to a FETCH is
accepted."""
import asyncio

import pytest

from aiomoqt.messages import AuthTokenRef
from aiomoqt.types import (
    AuthTokenAliasType, MOQTRequestError, ParamType, RequestErrorCode,
    SessionCloseCode, StreamResetCode, SubscribeDoneCode,
)

from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_subscription_filter import _NS, _TRANSPORTS
from aiomoqt.tests.test_subscription_filter import _loopback as _filter_loopback
from aiomoqt.tests.test_fetch_serving import _NS as _FETCH_NS
from aiomoqt.tests.test_fetch_serving import _loopback as _fetch_loopback

_BASE_PORT = 16340


def _port(case: int, use_quic: bool) -> int:
    return _BASE_PORT + 2 * case + (not use_quic)


@requires_certs
@_TRANSPORTS
async def test_a_refused_update_ends_only_its_subscription(use_quic):
    async with _filter_loopback(_port(0, use_quic), use_quic) as (
            tracks, rx, objects):
        await rx.subscribe(_NS, "audio", forward=1, wait_response=True)
        video = await rx.subscribe(_NS, "video", forward=1,
                                   wait_response=True)
        done = asyncio.get_running_loop().create_future()
        rx.register_publish_done_handler(video.request_id, done.set_result)
        unknown = AuthTokenRef(AuthTokenAliasType.USE_ALIAS, 5)
        with pytest.raises(MOQTRequestError) as err:
            await rx.request_update(
                video.request_id,
                parameters={ParamType.AUTH_TOKEN: unknown},
                wait_response=True)
        assert err.value.error_code == SessionCloseCode.UNKNOWN_AUTH_TOKEN_ALIAS
        status = await asyncio.wait_for(done, 5.0)
        assert status.status_code == SubscribeDoneCode.UPDATE_FAILED
        await asyncio.sleep(0.05)
        video_seen, audio_seen = len(objects["video"]), len(objects["audio"])
        await asyncio.sleep(0.1)
        assert len(objects["video"]) == video_seen
        assert len(objects["audio"]) > audio_seen
        assert rx._close_err is None


async def _stalled_fetch(tracks, rx):
    """A FETCH whose objects are stalled, so its data stream stays open.
    Returns (FETCH_OK, publisher session, reset codes sent)."""
    pub = tracks["video"].session
    stalled = asyncio.Event()

    async def _stall(stream_id, data, end_stream=False):
        stalled.set()
        await asyncio.Event().wait()
    pub.stream_write_drain = _stall
    resets = []
    reset = pub.stream_reset
    pub.stream_reset = lambda sid, code=0: (resets.append(int(code)),
                                            reset(sid, code))
    ok = await rx.fetch(_FETCH_NS, "video", start_group=0, start_object=0,
                        end_group=2, end_object=0, wait_response=True)
    await asyncio.wait_for(stalled.wait(), 5)
    return ok, pub, resets


@requires_certs
@_TRANSPORTS
async def test_a_failed_fetch_update_resets_its_data_stream(use_quic):
    async with _fetch_loopback(_port(1, use_quic), use_quic) as (
            tracks, rx, _):
        ok, pub, resets = await _stalled_fetch(tracks, rx)
        with pytest.raises(MOQTRequestError) as err:
            await rx.request_update(ok.request_id, forward=1,
                                    wait_response=True)
        assert err.value.error_code == RequestErrorCode.NOT_SUPPORTED
        assert await rx.await_fetch_done(ok.request_id, timeout=5) is False
        assert StreamResetCode.CANCELLED in resets
        assert pub._close_err is None and rx._close_err is None


@requires_certs
@_TRANSPORTS
async def test_a_priority_update_to_a_fetch_is_accepted(use_quic):
    async with _fetch_loopback(_port(2, use_quic), use_quic) as (
            tracks, rx, _):
        ok, pub, resets = await _stalled_fetch(tracks, rx)
        await rx.request_update(
            ok.request_id, parameters={ParamType.SUBSCRIBER_PRIORITY: 7},
            wait_response=True)
        assert StreamResetCode.CANCELLED not in resets
        assert pub._close_err is None and rx._close_err is None
