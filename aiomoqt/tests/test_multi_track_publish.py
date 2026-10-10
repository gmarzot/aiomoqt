"""Several PublishedTracks on one session: PUBLISH_OK, SUBSCRIBE and
updates reach the track they address, and every SUBSCRIBE gets a
terminal reply.
"""
import asyncio
import contextlib
from pathlib import Path

import pytest

import aiomoqt.track as track_module
from aiomoqt.client import MOQTClient
from aiomoqt.messages import SubscribeUpdate
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import (
    MOQTMessageType, MOQTRequestError, RequestErrorCode, SubscribeErrorCode,
)

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 15900
_DRAFTS = [14, 16, 18]
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_NS = "multi/track"
_NAMES = ("catalog", "audio", "video")
# Distinct per track so the receiver can tell whose objects it got.
_SIZES = (100, 200, 300)
_MIN_OBJECTS = 5


def _port(case: int, draft: int, use_quic: bool = True) -> int:
    return (_BASE_PORT + 3 * case + _DRAFTS.index(draft)
            + (0 if use_quic else 40))


def _does_not_exist(draft: int) -> int:
    return (SubscribeErrorCode.TRACK_DOES_NOT_EXIST if draft == 14
            else RequestErrorCode.DOES_NOT_EXIST)


def _track(session, namespace, name, size) -> PublishedTrack:
    return PublishedTrack(session, namespace=namespace, trackname=name,
                          object_size=size, group_size=10,
                          num_subgroups=1, rate=200)


class _Finite(PublishedTrack):
    """Two objects, then PUBLISH_DONE."""

    async def produce(self, out):
        for group_id in range(2):
            await out.write(group_id, 0, bytes(self.object_size),
                            group_start=True)


class _Receiver:
    """Server side: accepts PUBLISHes, answers PUBLISH_NAMESPACE, and
    records payload sizes per alias."""

    def __init__(self, on_namespace=None):
        self.session = None
        self.publishes: asyncio.Queue = asyncio.Queue()
        self.sizes: dict = {}
        self._on_namespace = on_namespace

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
        if self._on_namespace is not None:
            await self._on_namespace(self, session, msg)
        session.publish_namepace_ok(msg)


@contextlib.asynccontextmanager
async def _loopback(port, draft, rx, subscribe_handler=None, use_quic=True):
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.PUBLISH, rx.on_publish)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            rx.on_publish_namespace)
    quic_server = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    if subscribe_handler is not None:
        client.register_handler(MOQTMessageType.SUBSCRIBE, subscribe_handler)
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


async def _publish_all(session, rx):
    """PUBLISH three tracks with forward=0 and accept each with
    forward=1. Returns the tracks and the PUBLISHes as received."""
    tracks = [_track(session, _NS, n, s) for n, s in zip(_NAMES, _SIZES)]
    for track in tracks:
        await track.publish(forward=0)
    msgs = []
    for _ in tracks:
        msg = await asyncio.wait_for(rx.publishes.get(), 5.0)
        rx.watch(rx.session, msg.track_alias)
        rx.session.publish_ok(msg, forward=1)
        msgs.append(msg)
    return tracks, msgs


@pytest.mark.asyncio
@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_publish_ok_starts_every_track(draft, use_quic):
    rx = _Receiver()
    async with _loopback(_port(0, draft, use_quic), draft, rx,
                         use_quic=use_quic) as session:
        _, msgs = await _publish_all(session, rx)
        flowing = await _until(lambda: all(
            rx.count(m.track_alias) >= _MIN_OBJECTS for m in msgs))
        counts = {m.track_name: rx.count(m.track_alias) for m in msgs}
        assert flowing, f"objects per track: {counts}"
        expect = dict(zip(_NAMES, _SIZES))
        for m in msgs:
            assert set(rx.sizes[m.track_alias]) == {
                expect[m.track_name.decode()]}


@pytest.mark.asyncio
@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_subscribe_reaches_its_own_track(draft, use_quic):
    # One trackname in three namespaces: routing keys on the full name.
    rx = _Receiver()
    async with _loopback(_port(1, draft, use_quic), draft, rx,
                         use_quic=use_quic) as session:
        tracks = [_track(session, f"{_NS}/{i}", "video", size)
                  for i, size in enumerate(_SIZES)]
        for track in tracks:
            await track.publish(announce_namespace=True, publish_track=False)
        oks = []
        for track in tracks:
            ok = await rx.session.subscribe(track.namespace, "video",
                                            forward=1, wait_response=True)
            rx.watch(rx.session, ok.track_alias)
            oks.append(ok)
        flowing = await _until(lambda: all(
            rx.count(ok.track_alias) >= _MIN_OBJECTS for ok in oks))
        assert flowing, (
            f"objects per subscription: "
            f"{[rx.count(ok.track_alias) for ok in oks]}")
        for ok, size in zip(oks, _SIZES):
            assert set(rx.sizes[ok.track_alias]) == {size}


@pytest.mark.asyncio
@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_attached_tracks_share_one_namespace(draft, use_quic):
    # One PUBLISH_NAMESPACE; each SUBSCRIBE reaches the track it names.
    rx = _Receiver()
    async with _loopback(_port(7, draft, use_quic), draft, rx,
                         use_quic=use_quic) as session:
        for name, size in zip(_NAMES, _SIZES):
            _track(session, _NS, name, size).attach()
        await session.publish_namespace(namespace=_NS, wait_response=True)
        oks = []
        for name in _NAMES:
            ok = await rx.session.subscribe(_NS, name, forward=1,
                                            wait_response=True)
            rx.watch(rx.session, ok.track_alias)
            oks.append(ok)
        flowing = await _until(lambda: all(
            rx.count(ok.track_alias) >= _MIN_OBJECTS for ok in oks))
        assert flowing, (
            f"objects per subscription: "
            f"{[rx.count(ok.track_alias) for ok in oks]}")
        for ok, size in zip(oks, _SIZES):
            assert set(rx.sizes[ok.track_alias]) == {size}


def test_a_full_name_serves_one_track_per_session():
    session = object.__new__(_MOQTSessionMixin)
    session._published_tracks = {}
    video = PublishedTrack(session, namespace=_NS, trackname="video")
    video.attach(session)
    video.attach(session)
    PublishedTrack(session, namespace=_NS, trackname="audio").attach(session)
    with pytest.raises(ValueError):
        PublishedTrack(session, namespace=_NS,
                       trackname="video").attach(session)


def test_published_tracks_take_no_session_handler_slot():
    # register_handler holds one handler per message type, so a track
    # that registers one takes it from every other track on the session.
    source = Path(track_module.__file__).read_text()
    assert "register_handler(" not in source


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_update_reaches_its_own_track(draft):
    rx = _Receiver()
    async with _loopback(_port(2, draft), draft, rx) as session:
        tracks, msgs = await _publish_all(session, rx)
        assert await _until(lambda: all(
            rx.count(m.track_alias) >= _MIN_OBJECTS for m in msgs))
        by_name = {t.trackname: t for t in tracks}
        target = by_name[msgs[0].track_name.decode()]
        sub = rx.session
        if draft >= 16:
            sub.request_update(msgs[0].request_id, forward=0)
        else:
            sub.send_control_message(SubscribeUpdate(
                request_id=sub._allocate_request_id(),
                subscription_request_id=msgs[0].request_id,
                start_group=0, start_object=0, end_group=0,
                priority=128, forward=0))
        paused = await _until(lambda: not target.forward)
        forward = {t.trackname: t.forward for t in tracks}
        assert paused, f"forward per track: {forward}"
        assert all(t.forward for t in tracks if t is not target)


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_subscribe_before_namespace_ok_is_served(draft):
    # The receiver subscribes from its PUBLISH_NAMESPACE handler and
    # replies OK only after SUBSCRIBE_OK: the track must already serve.
    alias = {}

    async def _subscribe_first(rx, session, msg):
        ok = await session.subscribe(msg.namespace, "video",
                                     forward=1, wait_response=True)
        rx.watch(session, ok.track_alias)
        alias['video'] = ok.track_alias

    rx = _Receiver(on_namespace=_subscribe_first)
    async with _loopback(_port(3, draft), draft, rx) as session:
        track = _track(session, _NS, "video", _SIZES[0])
        await track.publish(announce_namespace=True, publish_track=False)
        flowing = await _until(
            lambda: rx.count(alias.get('video')) >= _MIN_OBJECTS)
        assert flowing, f"objects: {rx.count(alias.get('video'))}"
        assert set(rx.sizes[alias['video']]) == {_SIZES[0]}


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_unknown_track_is_refused(draft):
    rx = _Receiver()
    async with _loopback(_port(4, draft), draft, rx) as session:
        track = _track(session, _NS, "video", _SIZES[0])
        await track.publish(announce_namespace=True, publish_track=False)
        with pytest.raises(MOQTRequestError) as err:
            await rx.session.subscribe(_NS, "nope", forward=1,
                                       wait_response=True)
        assert err.value.response is not None, "timed out, no reply"
        assert err.value.error_code == _does_not_exist(draft)


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_finished_track_gives_terminal_reply(draft):
    rx = _Receiver()
    async with _loopback(_port(5, draft), draft, rx) as session:
        track = _Finite(session, namespace=_NS, trackname="video",
                        object_size=_SIZES[0])
        await track.publish(announce_namespace=True, publish_track=False)
        sub = rx.session
        await sub.subscribe(_NS, "video", forward=1, wait_response=True)
        assert await _until(lambda: track._done), "track never finished"

        msg = sub.subscribe(_NS, "video", forward=1)
        done = asyncio.get_running_loop().create_future()
        sub.register_publish_done_handler(
            msg.request_id,
            lambda m: done.done() or done.set_result(m))
        try:
            await sub._await_response(msg.request_id, timeout=5.0)
        except MOQTRequestError as e:
            assert e.response is not None, "timed out, no reply"
            return
        # Accepted: PUBLISH_DONE must end it.
        await asyncio.wait_for(done, 5.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_app_handler_sees_unpublished_names(draft):
    seen = []

    async def _app(session, msg):
        seen.append(msg.track_name)
        session.subscribe_error(msg.request_id,
                                error_code=_does_not_exist(draft),
                                reason="not here")

    rx = _Receiver()
    async with _loopback(_port(6, draft), draft, rx,
                         subscribe_handler=_app) as session:
        track = _track(session, _NS, "video", _SIZES[0])
        await track.publish(announce_namespace=True, publish_track=False)
        ok = await rx.session.subscribe(_NS, "video", forward=1,
                                        wait_response=True)
        rx.watch(rx.session, ok.track_alias)
        with pytest.raises(MOQTRequestError) as err:
            await rx.session.subscribe(_NS, "other", forward=1,
                                       wait_response=True)
        assert err.value.response is not None, "timed out, no reply"
        assert seen == [b"other"]
        assert await _until(
            lambda: rx.count(ok.track_alias) >= _MIN_OBJECTS)
        assert set(rx.sizes[ok.track_alias]) == {_SIZES[0]}
