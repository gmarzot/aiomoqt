"""One publisher-role subscription per track per session (d18 §5.1): a
PUBLISH and a peer's SUBSCRIBE for the same track never both hold it.

Each case runs two tracks on one session with only one of them crossing,
over raw QUIC and WebTransport.
"""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import MOQTMessageType, MOQTRequestError, RequestErrorCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 17600
_DRAFTS = [18, 20, 22]
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_NS = "item4/cross"
# Distinct sizes tell the tracks' objects apart.
_SIZES = {"audio": 100, "video": 200}
_MIN_OBJECTS = 10


def _port(case: int, draft: int, use_quic: bool) -> int:
    return (_BASE_PORT + 20 * case + 2 * [14, 18, 20, 22].index(draft)
            + (0 if use_quic else 1))


def _duplicate(draft: int) -> int:
    return (RequestErrorCode.NOT_SUPPORTED if draft >= 20
            else RequestErrorCode.DUPLICATE_SUBSCRIPTION)


class _Gated(PublishedTrack):
    """Holds a SUBSCRIBE after the session admits it, until `release`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def _on_subscribe(self, session, msg):
        self.entered.set()
        await self.release.wait()
        await super()._on_subscribe(session, msg)


def _track(session, name, cls=PublishedTrack) -> PublishedTrack:
    return cls(session, namespace=_NS, trackname=name,
               object_size=_SIZES[name], group_size=10, num_subgroups=1,
               rate=200)


class _Peer:
    """Server side: queues PUBLISHes and counts object sizes per alias."""

    def __init__(self):
        self.session = None
        self.publishes: asyncio.Queue = asyncio.Queue()
        self.sizes: dict = {}

    def watch(self, alias: int) -> None:
        def _on_object(obj, *_):
            if obj.payload:
                self.sizes.setdefault(alias, []).append(len(obj.payload))
        self.session.register_object_handler(alias, _on_object)

    def count(self, alias) -> int:
        return len(self.sizes.get(alias, ()))

    def names(self) -> list:
        return [m.track_name.decode()
                for m in list(self.publishes._queue)]

    async def on_publish(self, session, msg):
        self.session = session
        await self.publishes.put(msg)

    def on_setup(self, setup_type):
        async def handler(session, msg):
            self.session = session
            _, default = session.CONTROL_REGISTRY[session.negotiated_draft][
                setup_type]
            await default(session, msg)
        return handler


@contextlib.asynccontextmanager
async def _loopback(port, draft, use_quic, peer):
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    setup = (MOQTMessageType.SETUP if draft >= 18
             else MOQTMessageType.CLIENT_SETUP)
    server.register_handler(setup, peer.on_setup(setup))
    server.register_handler(MOQTMessageType.PUBLISH, peer.on_publish)
    handle = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            assert session.negotiated_draft == draft
            assert await _until(lambda: peer.session is not None)
            yield session
    finally:
        handle.close()


async def _until(cond, timeout: float = 5.0) -> bool:
    try:
        async with asyncio.timeout(timeout):
            while not cond():
                await asyncio.sleep(0.02)
    except TimeoutError:
        return False
    return True


async def _keeps_flowing(peer, alias) -> bool:
    base = peer.count(alias)
    return await _until(lambda: peer.count(alias) >= base + _MIN_OBJECTS)


@pytest.mark.asyncio
@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_publish_after_subscribe_sends_no_publish(draft, use_quic):
    """SUBSCRIBE admitted, then publish(): the subscription serves the
    peer; only the other track sends PUBLISH."""
    peer = _Peer()
    async with _loopback(_port(0, draft, use_quic), draft, use_quic,
                         peer) as session:
        audio, video = _track(session, "audio"), _track(session, "video")
        video.attach()
        ok = await peer.session.subscribe(_NS, "video", forward=1,
                                          wait_response=True)
        peer.watch(ok.track_alias)
        assert await _until(lambda: peer.count(ok.track_alias) > 0)
        await video.publish(forward=0)
        await audio.publish(forward=0)
        msg = await asyncio.wait_for(peer.publishes.get(), 5.0)
        assert msg.track_name == b"audio"
        peer.watch(msg.track_alias)
        peer.session.publish_ok(msg, forward=1)
        assert await _keeps_flowing(peer, msg.track_alias)
        assert await _keeps_flowing(peer, ok.track_alias), (
            f"video stopped at {peer.count(ok.track_alias)} objects")
        assert peer.publishes.empty(), f"PUBLISHed: {peer.names()}"
        assert set(peer.sizes[ok.track_alias]) == {_SIZES["video"]}
        assert set(peer.sizes[msg.track_alias]) == {_SIZES["audio"]}


@pytest.mark.asyncio
@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_subscribe_after_publish_is_refused(draft, use_quic):
    """PUBLISH established, then a SUBSCRIBE for the track: refused, and
    the PUBLISH subscription keeps flowing."""
    peer = _Peer()
    async with _loopback(_port(1, draft, use_quic), draft, use_quic,
                         peer) as session:
        tracks = [_track(session, "audio"), _track(session, "video")]
        for track in tracks:
            await track.publish(forward=0)
        alias = {}
        for _ in tracks:
            msg = await asyncio.wait_for(peer.publishes.get(), 5.0)
            alias[msg.track_name.decode()] = msg.track_alias
            peer.watch(msg.track_alias)
            peer.session.publish_ok(msg, forward=1)
        assert await _until(lambda: all(
            peer.count(a) >= _MIN_OBJECTS for a in alias.values()))
        with pytest.raises(MOQTRequestError) as err:
            await peer.session.subscribe(_NS, "video", forward=1,
                                         wait_response=True)
        assert err.value.response is not None, "timed out, no reply"
        assert err.value.error_code == _duplicate(draft)
        for name, a in alias.items():
            assert await _keeps_flowing(peer, a), (
                f"{name} stopped at {peer.count(a)} objects")
            assert set(peer.sizes[a]) == {_SIZES[name]}


@pytest.mark.asyncio
@_TRANSPORTS
@pytest.mark.parametrize("draft", [14, *_DRAFTS])
async def test_publish_ok_pauses_only_its_own_subscription(draft, use_quic):
    """SUBSCRIBE admitted by the session but not yet by the track when
    publish() runs, then every PUBLISH accepted with forward=0: the
    SUBSCRIBE keeps flowing. From d16 the crossing track sends no
    PUBLISH; d14 has no session-level admission to consult."""
    peer = _Peer()
    async with _loopback(_port(2, draft, use_quic), draft, use_quic,
                         peer) as session:
        audio = _track(session, "audio")
        video = _track(session, "video", cls=_Gated)
        video.attach()
        pending = peer.session.subscribe(_NS, "video", forward=1)
        await asyncio.wait_for(video.entered.wait(), 5.0)
        await video.publish(forward=0)
        await audio.publish(forward=0)
        video.release.set()
        ok = await peer.session._await_response(pending.request_id,
                                                timeout=5.0)
        peer.watch(ok.track_alias)
        expect = ["audio"] if draft >= 16 else ["audio", "video"]
        assert await _until(lambda: peer.publishes.qsize() >= len(expect))
        await asyncio.sleep(0.1)
        assert sorted(peer.names()) == expect
        while not peer.publishes.empty():
            msg = peer.publishes.get_nowait()
            peer.watch(msg.track_alias)
            peer.session.publish_ok(msg, forward=0)
        await asyncio.sleep(0.2)
        assert await _keeps_flowing(peer, ok.track_alias), (
            f"video stopped at {peer.count(ok.track_alias)} objects")
        assert set(peer.sizes[ok.track_alias]) == {_SIZES["video"]}
