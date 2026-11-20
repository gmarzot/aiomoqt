"""WebTransport sessions queue nothing per stream: the relay's session
with a publisher and a viewer's session each take one stream per object
and keep aiopquic's events() queue empty."""
import asyncio
import contextlib

from aiomoqt.client import MOQTClient
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tests.test_relay_forwarding import _reset_relay_state

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_PORT = 33720
_NS = "wt/queue"


@requires_certs
async def test_peer_opened_streams_leave_no_queued_events():
    _reset_relay_state()
    server = relay._build_server("localhost", _PORT, CERT, KEY,
                                 use_quic=False, draft=22)
    handle = await server.serve()
    got = []
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                s = await stack.enter_async_context(MOQTClient(
                    "localhost", _PORT, path="/", use_quic=False,
                    verify_tls=False, supported_drafts=22).connect())
                await s.client_session_init()
                return s
            pub = await connect()
            # One object per group, so a stream per object.
            PublishedTrack(pub, namespace=_NS, trackname="audio",
                           object_size=100, group_size=1, rate=200).attach()
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            sub = await connect()
            await SubscribedTrack(sub, _NS, "audio",
                                  on_object=lambda *a: got.append(a)
                                  ).subscribe(timeout=5)
            async with asyncio.timeout(10):
                while len(got) < 40:
                    await asyncio.sleep(0.02)
            track = relay._tracks[(relay._ns_tuple(_NS), b"audio")]
            upstream = relay._upstream_session(track)
            assert upstream is not None and upstream._is_wt
            for s in (upstream, sub):
                assert s._event_queue.qsize() == 0, s._event_queue.qsize()
    finally:
        handle.close()
        _reset_relay_state()
