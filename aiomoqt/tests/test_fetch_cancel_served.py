"""d14/d16 FETCH_CANCEL resets the fetch stream the publisher serves,
and only that one."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import FetchCancel
from aiomoqt.messages.data import FetchObject
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 17230
_DRAFTS = [14, 16]
_TRACKS = ("video", "audio")


def _port(draft, use_quic):
    return _BASE_PORT + 2 * _DRAFTS.index(draft) + (not use_quic)


@contextlib.asynccontextmanager
async def _loopback(port, draft, use_quic, served):
    async def on_fetch(session, msg):
        session.fetch_ok(request_id=msg.request_id, largest_group_id=0,
                         largest_object_id=2)
        objs = [FetchObject(group_id=0, subgroup_id=0, object_id=o,
                            publisher_priority=128, payload=b"x")
                for o in range(2)]
        # Held open: only a FETCH_CANCEL or the test ends the stream.
        served[msg.track_name.decode()] = await session.serve_fetch(
            msg.request_id, objs, fin=False)
        served["session"] = session

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.FETCH, on_fetch)
    handle = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            yield session
    finally:
        handle.close()


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_fetch_cancel_resets_the_served_stream(draft, use_quic):
    served = {}
    async with _loopback(_port(draft, use_quic), draft, use_quic,
                         served) as session:
        got_video = asyncio.Event()
        fetches = {name: session.fetch(
                       "ns", name, start_group=0, start_object=0,
                       end_group=0, end_object=2,
                       on_object=(lambda *_: got_video.set())
                       if name == "video" else None)
                   for name in _TRACKS}
        async with asyncio.timeout(2.0):
            while not all(n in served for n in _TRACKS):
                await asyncio.sleep(0.01)
            # The cancel must land on a stream the client already reads.
            await got_video.wait()
        assert fetches["video"].request_id in session._fetch_stream_by_request
        session.send_control_message(
            FetchCancel(request_id=fetches["video"].request_id))
        video = fetches["video"].request_id
        assert not await session.await_fetch_done(video, timeout=2.0)
        assert video not in session._fetch_stream_by_request, "not reset"
        # The other fetch is untouched until its publisher ends it.
        audio = fetches["audio"].request_id
        assert audio in session._fetch_stream_by_request
        served["session"].stream_fin(served["audio"])
        assert await session.await_fetch_done(audio, timeout=2.0)
        assert session._close_err is None
