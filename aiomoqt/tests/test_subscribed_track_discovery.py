"""SubscribedTrack discovery (d18+): a peer's refusal of SUBSCRIBE_TRACKS
ends discovery with its reason, and expected_trackname picks one track out
of a namespace that offers several."""
import asyncio
import contextlib
import time

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import (
    D18MessageType, MOQTMessageType, MOQTRequestError, RequestErrorCode,
)

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 16960
_DRAFTS = (18, 22)
_CASES = pytest.mark.parametrize(
    "draft, use_quic", [(d, q) for d in _DRAFTS for q in (True, False)],
    ids=[f"d{d}-{'quic' if q else 'wt'}" for d in _DRAFTS for q in (True, False)])


def _port(offset, draft, use_quic):
    return _BASE_PORT + offset + 2 * _DRAFTS.index(draft) + (not use_quic)


@contextlib.asynccontextmanager
async def _peers(port, use_quic, draft, on_tracks):
    """Yields (publisher client session, server session) once the client
    has published namespace "disc/a"; `on_tracks` answers SUBSCRIBE_TRACKS."""
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
    quic_server = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        client.register_handler(D18MessageType.SUBSCRIBE_TRACKS, on_tracks)
        async with client.connect() as pub:
            await pub.client_session_init()
            await pub.publish_namespace(namespace="disc/a", wait_response=True)
            yield pub, await asyncio.wait_for(peer, 5.0)
    finally:
        quic_server.close()


@requires_certs
@_CASES
async def test_refused_subscribe_tracks_ends_discovery(draft, use_quic):
    async def refuse(session, msg):
        session.subscribe_error(msg.request_id, RequestErrorCode.NOT_SUPPORTED,
                                "SUBSCRIBE_TRACKS is not supported")

    async with _peers(_port(0, draft, use_quic), use_quic, draft,
                      refuse) as (pub, rx):
        sub = SubscribedTrack(rx, "disc/a")
        sub._quiet = True
        t0 = time.monotonic()
        with pytest.raises(MOQTRequestError) as err:
            await sub.subscribe(timeout=10)
        assert time.monotonic() - t0 < 3
        assert "not supported" in str(err.value)
        assert pub._close_err is None and rx._close_err is None


@requires_certs
@_CASES
async def test_expected_trackname_skips_other_tracks(draft, use_quic):
    async def offer(session, msg):
        session.subscribe_tracks_ok(msg)
        for name in ("catalog", "video"):
            await PublishedTrack(session, namespace="disc/a",
                                 trackname=name).publish(publish_track=True)

    async with _peers(_port(4, draft, use_quic), use_quic, draft,
                      offer) as (pub, rx):
        sub = SubscribedTrack(rx, "disc/a")
        sub._quiet = True
        await sub.subscribe(timeout=5, expected_trackname="video")
        assert sub.trackname == "video"
        assert sub.namespace == "disc/a"
        assert pub._close_err is None and rx._close_err is None


@requires_certs
@_CASES
async def test_expected_trackname_timeout_names_the_offered_tracks(draft,
                                                                  use_quic):
    async def offer(session, msg):
        session.subscribe_tracks_ok(msg)
        await PublishedTrack(session, namespace="disc/a",
                             trackname="track.2dvideo").publish(publish_track=True)

    async with _peers(_port(8, draft, use_quic), use_quic, draft,
                      offer) as (pub, rx):
        sub = SubscribedTrack(rx, "disc/a")
        sub._quiet = True
        with pytest.raises(TimeoutError, match="offered: track.2dvideo"):
            await sub.subscribe(timeout=1, expected_trackname="track-video")
        assert pub._close_err is None and rx._close_err is None
