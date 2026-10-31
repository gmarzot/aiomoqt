"""A PUBLISH_DONE that arrives while its PUBLISH still waits in the
announcement queue reaches the SubscribedTrack that discovers it. Two
tracks on one session, over raw QUIC and WebTransport."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import SubscribedTrack
from aiomoqt.types import D18MessageType, MOQTMessageType, SubscribeDoneCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 29320
_DRAFTS = [18, 22]
_NAMESPACES = ("early/a", "early/b")


async def _publish_then_done(session, msg):
    """PUBLISH, then PUBLISH_DONE as soon as its stream is bound."""
    session.subscribe_tracks_ok(msg)
    ns = "/".join(p.decode() for p in msg.namespace_prefix)
    pub = session.publish(ns, "t" + ns[-1])
    while pub.request_id not in session._bidi_streams:
        await asyncio.sleep(0.001)
    session.subscribe_done(pub.request_id,
                           status_code=SubscribeDoneCode.TRACK_ENDED)


@contextlib.asynccontextmanager
async def _peers(port, use_quic, draft):
    """Yields the subscriber: the server session of a publishing client."""
    peer = asyncio.get_running_loop().create_future()

    async def _on_publish_namespace(session, msg):
        session.publish_namespace_ok(msg)
        if not peer.done():
            peer.set_result(session)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            _on_publish_namespace)
    handle = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        client.register_handler(D18MessageType.SUBSCRIBE_TRACKS,
                                _publish_then_done)
        async with client.connect() as pub:
            await pub.client_session_init()
            for ns in _NAMESPACES:
                await pub.publish_namespace(namespace=ns, wait_response=True)
            yield await asyncio.wait_for(peer, 5.0)
    finally:
        handle.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", _DRAFTS)
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_publish_done_behind_a_queued_publish(draft, use_quic):
    port = _PORT + 2 * _DRAFTS.index(draft) + (not use_quic)
    async with _peers(port, use_quic, draft) as rx:
        tracks = []
        for ns in _NAMESPACES:
            done_calls = []
            track = SubscribedTrack(rx, ns, on_done=done_calls.append)
            track._quiet = True
            await track.subscribe(timeout=5)
            await asyncio.wait_for(track.wait_closed(), 2.0)
            assert track.trackname == "t" + ns[-1]
            assert track.publish_done is not None, f"{ns}: no PUBLISH_DONE"
            assert track.publish_done.request_id == track.request_id
            assert (track.publish_done.status_code
                    == SubscribeDoneCode.TRACK_ENDED)
            assert done_calls == [track.publish_done]
            tracks.append(track)
        assert len({t.request_id for t in tracks}) == 2
        assert not rx._publish_done_handlers
        assert not rx._unclaimed_publishes
        assert rx._close_err is None
