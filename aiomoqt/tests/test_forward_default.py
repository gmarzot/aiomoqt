"""An omitted FORWARD means 1 everywhere but REQUEST_UPDATE (§10.2.12).
From d20 PUBLISH_OK carries no FORWARD, so the PUBLISH's own value
stands. Two tracks on one session: a Forward State reaching the wrong
track shows."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import MOQTMessageType, ParamType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 19300
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_NS = "fwd/default"
_SIZES = {"audio": 100, "video": 200}
_MIN_OBJECTS = 5


def _port(case: int, draft: int, use_quic: bool) -> int:
    return _BASE_PORT + 20 * case + 2 * (draft - 14) + (not use_quic)


class _Receiver:
    """Server side: records PUBLISHes and payload sizes per alias."""

    def __init__(self):
        self.session = None
        self.publishes: asyncio.Queue = asyncio.Queue()
        self.sizes: dict = {}

    def watch(self, session, alias: int) -> None:
        def _on_object(obj, *_):
            if obj.payload:
                self.sizes.setdefault(alias, []).append(len(obj.payload))
        session.register_object_handler(alias, _on_object)

    def count(self, alias) -> int:
        return len(self.sizes.get(alias, ()))

    async def on_publish(self, session, msg):
        self.session = session
        await self.publishes.put(msg)

    async def on_publish_namespace(self, session, msg):
        self.session = session
        session.publish_namepace_ok(msg)


@contextlib.asynccontextmanager
async def _loopback(port, draft, rx, use_quic):
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.PUBLISH, rx.on_publish)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            rx.on_publish_namespace)
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


def _tracks(session):
    return [PublishedTrack(session, namespace=_NS, trackname=name,
                           object_size=size, group_size=10,
                           num_subgroups=1, rate=200)
            for name, size in _SIZES.items()]


async def _publish_paused(session, rx):
    """PUBLISH both tracks with forward=0 and accept each with a
    PUBLISH_OK that omits FORWARD."""
    for track in _tracks(session):
        await track.publish(forward=0)
    msgs = []
    for _ in _SIZES:
        msg = await asyncio.wait_for(rx.publishes.get(), 5.0)
        rx.watch(rx.session, msg.track_alias)
        rx.session.publish_ok(msg, forward=None)
        msgs.append(msg)
    return msgs


def _assert_own_sizes(rx, alias, name) -> None:
    assert set(rx.sizes[alias]) == {_SIZES[name]}


@_TRANSPORTS
@pytest.mark.parametrize("draft", [16, 18])
async def test_publish_ok_without_forward_starts_every_track(draft,
                                                             use_quic):
    rx = _Receiver()
    async with _loopback(_port(0, draft, use_quic), draft, rx,
                         use_quic) as session:
        msgs = await _publish_paused(session, rx)
        flowing = await _until(lambda: all(
            rx.count(m.track_alias) >= _MIN_OBJECTS for m in msgs))
        assert flowing, {m.track_name: rx.count(m.track_alias) for m in msgs}
        for m in msgs:
            _assert_own_sizes(rx, m.track_alias, m.track_name.decode())


@_TRANSPORTS
@pytest.mark.parametrize("draft", [20, 22])
async def test_publish_ok_from_d20_keeps_the_publish_forward(draft,
                                                             use_quic):
    rx = _Receiver()
    async with _loopback(_port(1, draft, use_quic), draft, rx,
                         use_quic) as session:
        first, second = await _publish_paused(session, rx)
        await asyncio.sleep(0.3)
        assert rx.count(first.track_alias) == rx.count(second.track_alias) == 0
        rx.session.request_update(first.request_id,
                                  parameters={ParamType.FORWARD: 1})
        assert await _until(
            lambda: rx.count(first.track_alias) >= _MIN_OBJECTS)
        assert rx.count(second.track_alias) == 0
        _assert_own_sizes(rx, first.track_alias, first.track_name.decode())


@_TRANSPORTS
@pytest.mark.parametrize("draft", [16, 18, 20, 22])
async def test_subscribe_without_forward_resumes_a_paused_track(draft,
                                                                use_quic):
    rx = _Receiver()
    async with _loopback(_port(2, draft, use_quic), draft, rx,
                         use_quic) as session:
        tracks = _tracks(session)
        for track in tracks:
            track.attach()
        await session.publish_namespace(namespace=_NS, wait_response=True)
        sub = rx.session
        paused = []
        for name in _SIZES:
            msg = sub.subscribe(_NS, name, forward=0)
            ok = await sub._await_response(msg.request_id)
            rx.watch(sub, ok.track_alias)
            paused.append((msg, ok))
        await asyncio.sleep(0.3)
        assert all(rx.count(ok.track_alias) == 0 for _, ok in paused)
        for msg, _ in paused:
            sub.unsubscribe(msg.request_id)
        assert await _until(lambda: not any(
            t._subs[0].subscribers for t in tracks))
        resumed = {}
        for name in _SIZES:
            ok = await sub.subscribe(_NS, name, wait_response=True)
            rx.watch(sub, ok.track_alias)
            resumed[name] = ok.track_alias
        flowing = await _until(lambda: all(
            rx.count(a) >= _MIN_OBJECTS for a in resumed.values()))
        assert flowing, {n: rx.count(a) for n, a in resumed.items()}
        for name, alias in resumed.items():
            _assert_own_sizes(rx, alias, name)
