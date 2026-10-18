"""PublishedTrack.end(): each subscription's streams close, then it gets
PUBLISH_DONE TRACK_ENDED with the streams it was sent (§10.11, §5.1.1);
the session and the other tracks carry on."""
import asyncio

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import MOQTMessageType, SubscribeDoneCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs
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


@requires_certs
async def test_a_d14_teardown_withdraws_nothing_onto_the_wire():
    """A d14 PUBLISH_NAMESPACE_DONE ends the whole session at an aiomoqt
    peer. A track withdraws its namespace only once its session has
    closed, when the control stream takes no more messages."""
    port = _port(2, True)
    peer = asyncio.get_running_loop().create_future()
    withdrawn = []

    async def _on_publish_namespace(session, msg):
        session.publish_namepace_ok(msg)
        if not peer.done():
            peer.set_result(session)

    async def _on_publish_namespace_done(session, msg):
        withdrawn.append(msg)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=True,
                        supported_drafts=14)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            _on_publish_namespace)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE_DONE,
                            _on_publish_namespace_done)
    quic_server = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=14)
        async with client.connect() as pub:
            await pub.client_session_init()
            track = PublishedTrack(pub, namespace="teardown/d14",
                                   trackname="t", object_size=100,
                                   group_size=5, rate=200)
            await track.publish(announce_namespace=True, publish_track=False)
            rx = await asyncio.wait_for(peer, 5.0)
            got = asyncio.Event()
            await rx.subscribe("teardown/d14", "t", forward=1,
                               on_object=lambda *_: got.set(),
                               wait_response=True)
            await asyncio.wait_for(got.wait(), 5.0)
            pub.close()
            await asyncio.wait_for(rx.async_closed(), 5.0)
            await asyncio.sleep(0.1)
        assert withdrawn == []
        assert not pub.is_announced("teardown/d14")
    finally:
        quic_server.close()
