"""A Forward State 0 that lands while a fan-out lane is suspended in its
first header write resets that group without stopping the lane: a later
Forward State 1 starts objects flowing. Two tracks on one session; the
header write is held so the race is deterministic."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import MOQTMessageType, ParamType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 20600
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_NS = "lane/reset"
_NAMES = ("video", "audio")
_MIN_OBJECTS = 5


def _port(draft: int, use_quic: bool) -> int:
    return _BASE_PORT + 2 * (draft - 14) + (not use_quic)


class _Produced(PublishedTrack):
    """Groups of 10 objects at 100/s through produce(), so the peer is a
    FanoutDelivery lane."""

    async def produce(self, out):
        group = 0
        while True:
            for oid in range(10):
                await out.write(group, oid, self.trackname.encode() * 8,
                                group_start=oid == 0)
                await asyncio.sleep(0.01)
            group += 1


class _Receiver:
    def __init__(self):
        self.session = None
        self.publishes: asyncio.Queue = asyncio.Queue()
        self.payloads: dict = {}

    def watch(self, alias: int) -> None:
        def _on_object(obj, *_):
            if obj.payload:
                self.payloads.setdefault(alias, []).append(bytes(obj.payload))
        self.session.register_object_handler(alias, _on_object)

    def count(self, alias) -> int:
        return len(self.payloads.get(alias, ()))

    async def on_publish(self, session, msg):
        self.session = session
        await self.publishes.put(msg)


def _hold_drains(session):
    """Park each drained write on a unidirectional (data) stream, after
    it is written, until the returned event is set: the writer is
    suspended as on backpressure. The list holds the streams parked."""
    release, parked = asyncio.Event(), []
    real = session.stream_write_drain

    async def held(stream_id, data, *args, **kw):
        await real(stream_id, data, *args, **kw)
        if stream_id & 0x2 and not release.is_set():
            parked.append(stream_id)
            await release.wait()

    session.stream_write_drain = held
    return release, parked


@contextlib.asynccontextmanager
async def _loopback(port, draft, rx, use_quic):
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.PUBLISH, rx.on_publish)
    quic_server = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            yield session
    finally:
        quic_server.close()


async def _until(cond, timeout: float = 5.0) -> bool:
    try:
        async with asyncio.timeout(timeout):
            while not cond():
                await asyncio.sleep(0.02)
    except TimeoutError:
        return False
    return True


@_TRANSPORTS
@pytest.mark.parametrize("draft", [18, 22])
async def test_forward_zero_during_the_first_header_write(draft, use_quic):
    rx = _Receiver()
    async with _loopback(_port(draft, use_quic), draft, rx,
                         use_quic) as session:
        tracks = [_Produced(session, namespace=_NS, trackname=name)
                  for name in _NAMES]
        release, parked = _hold_drains(session)
        for track in tracks:
            await track.publish(forward=1)
        msgs = [await asyncio.wait_for(rx.publishes.get(), 5.0)
                for _ in _NAMES]
        assert await _until(lambda: len(parked) >= len(_NAMES)), parked
        for msg in msgs:
            rx.watch(msg.track_alias)
            if draft < 20:
                rx.session.publish_ok(msg, forward=0)
            else:
                # PUBLISH_OK carries no FORWARD from d20.
                rx.session.publish_ok(msg)
                rx.session.request_update(msg.request_id,
                                          parameters={ParamType.FORWARD: 0})
        assert await _until(lambda: not any(t.forward for t in tracks))
        release.set()
        await asyncio.sleep(0.1)
        for msg in msgs:
            rx.session.request_update(msg.request_id,
                                      parameters={ParamType.FORWARD: 1})
        flowing = await _until(lambda: all(
            rx.count(m.track_alias) >= _MIN_OBJECTS for m in msgs))
        assert flowing, {m.track_name: rx.count(m.track_alias) for m in msgs}
        for m in msgs:
            assert set(rx.payloads[m.track_alias]) == {m.track_name * 8}
