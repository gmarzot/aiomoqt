"""A raising object callback costs only that object: the rest of the
drained batch, and every other track on the session, still arrive."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import ForwardingPreference, MOQTMessageType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 16480
_DRAFTS = [16, 18]
_OBJECTS = 20


class _Burst(PublishedTrack):
    """_OBJECTS objects in one group, written back to back."""

    async def produce(self, out):
        for object_id in range(_OBJECTS):
            await out.write(0, object_id, bytes(self.object_size),
                            group_start=object_id == 0)


@contextlib.asynccontextmanager
async def _loopback(port, draft, use_quic, on_publish):
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.PUBLISH, on_publish)
    quic_server = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            yield session
    finally:
        quic_server.close()


async def _run(port, draft, use_quic, forwarding):
    publishes: asyncio.Queue = asyncio.Queue()
    seen = {}

    async def on_publish(session, msg):
        await publishes.put((session, msg))

    async with _loopback(port, draft, use_quic, on_publish) as session:
        tracks = [_Burst(session, "cb/iso", name, object_size=100,
                         forwarding=forwarding)
                  for name in ("raises", "quiet")]
        for track in tracks:
            await track.publish(forward=0)
        for i in range(len(tracks)):
            rx, msg = await asyncio.wait_for(publishes.get(), 5.0)
            got = seen.setdefault(msg.track_name, [])

            def on_object(obj, *_, got=got, name=msg.track_name):
                if obj.payload:
                    got.append(obj.object_id)
                    if name == b"raises":
                        raise RuntimeError("consumer bug")
            rx.register_object_handler(msg.track_alias, on_object)
            rx.publish_ok(msg, forward=1)
        for _ in range(250):
            if sum(len(v) for v in seen.values()) >= 2 * _OBJECTS:
                break
            await asyncio.sleep(0.02)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_raising_callback_keeps_the_stream_batch(draft, use_quic):
    port = _BASE_PORT + _DRAFTS.index(draft) + (0 if use_quic else 4)
    seen = await _run(port, draft, use_quic, ForwardingPreference.SUBGROUP)
    assert sorted(seen[b"raises"]) == list(range(_OBJECTS))
    assert sorted(seen[b"quiet"]) == list(range(_OBJECTS))


def _serve_datagrams(tasks):
    """SUBSCRIBE handler: serve each subscribed track as datagrams."""
    async def on_subscribe(session, msg):
        ok = session.subscribe_ok(request_msg=msg, content_exists=0)
        track = PublishedTrack(session, "cb/iso", msg.track_name.decode(),
                               object_size=64, group_size=4,
                               forwarding=ForwardingPreference.DATAGRAM)
        track._quiet = True
        tasks.append(asyncio.create_task(track._generate_datagrams(
            session, ok.track_alias, bytes(64))))
    return on_subscribe


# WT datagram TX is not wired, so datagrams run over raw QUIC only.
@pytest.mark.asyncio
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_raising_callback_keeps_the_datagram_batch(draft):
    port = _BASE_PORT + 8 + _DRAFTS.index(draft)
    tasks = []
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=True,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.SUBSCRIBE,
                            _serve_datagrams(tasks))
    quic_server = await server.serve()
    seen = {b"raises": [], b"quiet": []}
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            await session.client_session_init()
            for name, got in seen.items():
                def on_object(obj, *_, got=got, name=name):
                    if obj.payload:
                        got.append(obj.object_id)
                        if name == b"raises":
                            raise RuntimeError("consumer bug")
                await session.subscribe("cb/iso", name, forward=1,
                                        on_object=on_object,
                                        wait_response=True)
            for _ in range(250):
                if all(len(v) >= _OBJECTS for v in seen.values()):
                    break
                await asyncio.sleep(0.02)
    finally:
        for task in tasks:
            task.cancel()
        quic_server.close()
    assert len(seen[b"raises"]) >= _OBJECTS
    assert len(seen[b"quiet"]) >= _OBJECTS
