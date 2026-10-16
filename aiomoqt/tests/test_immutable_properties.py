"""PublishedTrack(immutable_properties=) rides each track's own PUBLISH and
SUBSCRIBE_OK as Immutable Properties (0x0B); d14 carries none."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.messages import MOQTMessage
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import MOQTMessageType
from aiomoqt.utils.buffer import Buffer

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 17500
_DRAFTS = [14, 16, 18, 20]
_NS = "immutable"
_PROPS = {
    "video": {0x04: 2000, 0x3B: b"video-meta"},
    "audio": {0x06: 50, 0x3D: b"audio-meta"},
}


def _port(case, draft, use_quic):
    return _BASE_PORT + 10 * case + 2 * _DRAFTS.index(draft) + (not use_quic)


def _immutable(msg, draft):
    exts = msg.track_extensions or {}
    raw = exts.get(MOQTMessage.IMMUTABLE_PROPERTIES)
    if raw is None:
        return None
    return MOQTMessage._extensions_decode(
        Buffer(data=bytes(raw), vi64=profile_for(draft).vi64),
        with_length=False, buf_end=len(raw), delta=True)


@contextlib.asynccontextmanager
async def _loopback(port, draft, use_quic, publishes):
    async def on_publish(session, msg):
        await publishes.put((session, msg))

    async def on_publish_namespace(session, msg):
        await publishes.put((session, msg))
        session.publish_namepace_ok(msg)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.PUBLISH, on_publish)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            on_publish_namespace)
    handle = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            yield session
    finally:
        handle.close()


def _tracks(session):
    return {name: PublishedTrack(session, namespace=_NS, trackname=name,
                                 immutable_properties=props)
            for name, props in _PROPS.items()}


def _expected(name, draft):
    return _PROPS[name] if draft >= 16 else None


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_each_publish_carries_its_tracks_properties(draft, use_quic):
    publishes = asyncio.Queue()
    async with _loopback(_port(0, draft, use_quic), draft, use_quic,
                         publishes) as session:
        for track in _tracks(session).values():
            await track.publish(forward=0)
        for _ in _PROPS:
            _, msg = await asyncio.wait_for(publishes.get(), 5.0)
            name = msg.track_name.decode()
            assert _immutable(msg, draft) == _expected(name, draft), name
        assert session._close_err is None


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_each_subscribe_ok_carries_its_tracks_properties(draft,
                                                                use_quic):
    publishes = asyncio.Queue()
    async with _loopback(_port(1, draft, use_quic), draft, use_quic,
                         publishes) as session:
        tracks = _tracks(session)
        for track in tracks.values():
            track.attach()
        await session.publish_namespace(namespace=_NS, wait_response=True)
        peer, _ = await asyncio.wait_for(publishes.get(), 5.0)
        # Reverse order: a reply carrying the other track's set shows.
        for name in reversed(list(_PROPS)):
            ok = await peer.subscribe(_NS, name, forward=0,
                                      wait_response=True)
            assert _immutable(ok, draft) == _expected(name, draft), name
        assert session._close_err is None
        assert peer._close_err is None


@pytest.mark.parametrize("props, match", [
    ({0x0B: b""}, "cannot contain"),
    ({0x04: b"x"}, "takes an int"),
    ({0x3B: 7}, "takes bytes"),
    ({0x04: -1}, "outside"),
    ({0x04: 1 << 62}, "outside"),
    ({0x22: 3}, "group order"),
])
def test_unsendable_properties_are_refused(props, match):
    with pytest.raises(ValueError, match=match):
        PublishedTrack(None, namespace=_NS, trackname="t",
                       immutable_properties=props)


def test_largest_varint_value_is_accepted():
    track = PublishedTrack(None, namespace=_NS, trackname="t",
                           immutable_properties={0x04: (1 << 62) - 1})
    assert track.immutable_properties == {0x04: (1 << 62) - 1}
