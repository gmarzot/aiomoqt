"""A PUBLISH_DONE sent right behind SUBSCRIBE_OK reaches the
SubscribedTrack: publish_done is set and wait_closed() returns at once.
Two tracks per session, over raw QUIC and WebTransport."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import SubscribedTrack
from aiomoqt.types import MOQTMessageType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 29300
_DRAFTS = [18, 22]
_NS = "early/done"
_NAMES = ("a", "b")


async def _ok_then_done(session, msg):
    """SUBSCRIBE_OK and PUBLISH_DONE in one event-loop turn."""
    session.subscribe_ok(request_msg=msg, content_exists=0)
    session.subscribe_done(msg.request_id, stream_count=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", _DRAFTS)
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_publish_done_behind_subscribe_ok(draft, use_quic):
    port = _PORT + 2 * _DRAFTS.index(draft) + (not use_quic)
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.SUBSCRIBE, _ok_then_done)
    handle = await server.serve()
    done_calls = {n: [] for n in _NAMES}
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            await session.client_session_init()
            tracks = {n: SubscribedTrack(session, _NS, n,
                                         on_done=done_calls[n].append)
                      for n in _NAMES}
            await asyncio.gather(*(t.subscribe() for t in tracks.values()))
            await asyncio.wait_for(asyncio.gather(
                *(t.wait_closed() for t in tracks.values())), 2.0)
            for n, t in tracks.items():
                assert t.publish_done is not None, f"{n}: no PUBLISH_DONE"
                assert t.publish_done.request_id == t.request_id
                assert done_calls[n] == [t.publish_done]
            assert len({t.request_id for t in tracks.values()}) == 2
            assert not session._publish_done_handlers
    finally:
        handle.close()
