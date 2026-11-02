"""Track and subscription lifetimes on a session carrying two tracks."""
import asyncio
import contextlib
from unittest.mock import MagicMock

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import SubscribeDoneCode
from aiomoqt.tests._certs import CERT, KEY, requires_certs


_BASE_PORT = 28400
_DRAFTS = (14, 16, 18, 20, 22)
_CASES = pytest.mark.parametrize(
    "draft, use_quic",
    [(draft, raw) for draft in _DRAFTS for raw in (True, False)],
    ids=[f"d{draft}-{'quic' if raw else 'wt'}"
         for draft in _DRAFTS for raw in (True, False)],
)


def _port(case, draft, use_quic):
    return _BASE_PORT + 20 * case + 2 * _DRAFTS.index(draft) + (not use_quic)


async def _until(check, timeout=5):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def _session(case, draft, use_quic):
    port = _port(case, draft, use_quic)
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    handle = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as rx:
            await rx.client_session_init()
            await _until(lambda: server._live)
            tx = next(iter(server._live))
            pubs = {}
            for name in ("audio", "video"):
                pub = PublishedTrack(tx, "close/test", name, object_size=100,
                                     group_size=5, rate=100)
                pub._quiet = True
                pub.attach()
                pubs[name] = pub
            try:
                yield tx, rx, pubs
            finally:
                for pub in pubs.values():
                    pub.end()
    finally:
        handle.close()


async def _subscribe(rx, name, counts):
    def received(obj, *args):
        if obj.payload:
            counts[name] += 1

    sub = SubscribedTrack(rx, "close/test", name, on_object=received)
    await sub.subscribe()
    return sub


@contextlib.asynccontextmanager
async def _tracks(case, draft, use_quic):
    async with _session(case, draft, use_quic) as (tx, rx, pubs):
        subs, counts = {}, {"audio": 0, "video": 0}
        for name in counts:
            subs[name] = await _subscribe(rx, name, counts)
        assert [subs[name].request_id for name in counts] == [0, 2]
        await _until(lambda: min(counts.values()) >= 3)
        yield rx, pubs, subs, counts


@requires_certs
@_CASES
async def test_first_client_subscription_gets_publish_done(draft, use_quic):
    async with _tracks(0, draft, use_quic) as (rx, pubs, subs, counts):
        pubs["audio"].end()
        await _until(lambda: subs["audio"].publish_done is not None)
        done = subs["audio"].publish_done
        assert done.request_id == 0
        assert done.status_code == SubscribeDoneCode.TRACK_ENDED
        assert done.stream_count >= 1
        before = counts["video"]
        await _until(lambda: counts["video"] >= before + 2)
        assert subs["video"].publish_done is None
        assert rx._close_err is None


@requires_certs
@_CASES
async def test_ending_one_track_preserves_other_close_waiters(draft, use_quic):
    async with _tracks(1, draft, use_quic) as (rx, pubs, subs, counts):
        audio_wait = asyncio.create_task(subs["audio"].wait_closed())
        video_wait = asyncio.create_task(subs["video"].wait_closed())
        session_wait = asyncio.create_task(rx.async_closed())
        try:
            pubs["video"].end()
            await asyncio.wait_for(video_wait, 5)
            assert subs["video"].completed
            before = counts["audio"]
            await _until(lambda: counts["audio"] >= before + 2)
            assert not audio_wait.done()
            assert not session_wait.done()
            assert not rx._moqt_session_closed.done()
            assert subs["audio"].publish_done is None
            assert not subs["audio"].completed
            assert rx._close_err is None

            pubs["audio"].end()
            await asyncio.wait_for(audio_wait, 5)
            assert subs["audio"].completed
            rx.close()
            assert await asyncio.wait_for(session_wait, 5)
        finally:
            for task in (audio_wait, video_wait, session_wait):
                task.cancel()
            await asyncio.gather(audio_wait, video_wait, session_wait,
                                 return_exceptions=True)


def test_unannounced_track_sends_no_publish_done():
    session = MagicMock()
    track = PublishedTrack(session, "close/test", "unannounced")
    track.end()
    session._send_reply.assert_not_called()


def test_publish_request_zero_gets_publish_done():
    session = MagicMock()
    track = PublishedTrack(session, "close/test", "published")
    track.request_id = 0
    track.end()
    session._send_reply.assert_called_once()
    request_id, done = session._send_reply.call_args.args
    assert request_id == done.request_id == 0
    assert done.status_code == SubscribeDoneCode.TRACK_ENDED
    assert session._send_reply.call_args.kwargs == {"fin": True}


@requires_certs
@_CASES
async def test_unsubscribe_ends_only_that_track(draft, use_quic):
    async with _tracks(2, draft, use_quic) as (rx, pubs, subs, counts):
        audio_wait = asyncio.create_task(subs["audio"].wait_closed())
        video_wait = asyncio.create_task(subs["video"].wait_closed())
        try:
            subs["video"].unsubscribe()
            await asyncio.wait_for(video_wait, 5)
            assert subs["video"].cancelled
            assert not subs["video"].completed
            before = counts["audio"]
            await _until(lambda: counts["audio"] >= before + 2)
            assert not audio_wait.done()
            assert not subs["audio"].cancelled
            assert rx._close_err is None
        finally:
            for task in (audio_wait, video_wait):
                task.cancel()
            await asyncio.gather(audio_wait, video_wait,
                                 return_exceptions=True)


@requires_certs
@_CASES
async def test_cancelled_subscriptions_release_receive_state(draft, use_quic):
    async with _session(3, draft, use_quic) as (tx, rx, pubs):
        rx.PUBLISH_DONE_GRACE_S = 0.2
        counts = {"audio": 0, "video": 0}
        rids = []
        for i in range(6):
            name = ("audio", "video")[i % 2]
            before = counts[name]
            sub = await _subscribe(rx, name, counts)
            await _until(lambda: counts[name] > before)
            rid = sub.request_id
            rids.append(rid)
            assert rx._track_aliases and rx._timing_watch
            if i < 3:
                sub.unsubscribe()
            else:
                rx.unsubscribe(rid)
            await _until(lambda: rid not in tx._peer_requests
                         and rid not in rx._bidi_streams)
        await _until(lambda: not rx._track_aliases)
        assert not rx._object_handlers
        assert not rx._timing_watch
        assert not rx._stream_end_handlers
        assert not rx._track_default_priority
        assert not rx._subscriptions
        assert not rx._bidi_streams
        assert not set(rids) & set(rx._publish_done_handlers)
        assert not set(rids) & set(rx._pending_requests)
        assert rx._close_err is None
