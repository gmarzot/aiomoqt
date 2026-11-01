"""A REQUEST_UPDATE right behind its SUBSCRIBE, in the same read, reaches
the PublishedTrack: the SUBSCRIBE's handler binds the request before the
update's runs."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import RequestOk
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import MOQTMessageType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 23140
_NS = "update/same-read"
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


@requires_certs
@_TRANSPORTS
@pytest.mark.parametrize("draft", [16, 18, 22])
async def test_update_behind_its_subscribe_reaches_the_track(use_quic, draft):
    port = _BASE_PORT + 2 * (16, 18, 22).index(draft) + (not use_quic)
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
        async with client.connect() as pub:
            await pub.client_session_init()
            tracks = {}
            for name in ("video", "audio"):
                track = PublishedTrack(pub, namespace=_NS, trackname=name,
                                       object_size=200, group_size=5,
                                       rate=200)
                track.attach()
                tracks[name] = track
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            rx = await asyncio.wait_for(peer, 5.0)
            replies = []
            for name in tracks:
                sub = rx.subscribe(_NS, name, wait_response=False)
                replies.append(rx.request_update(sub.request_id, forward=0,
                                                 wait_response=True))
            got = await asyncio.wait_for(asyncio.gather(*replies), 5.0)
            assert all(isinstance(r, RequestOk) for r in got), got
            await asyncio.sleep(0.05)
            for name, track in tracks.items():
                assert len(track._subs) == 1, name
                assert track._subs[0].senders == set(), name
            assert pub._close_err is None and rx._close_err is None
    finally:
        quic_server.close()
