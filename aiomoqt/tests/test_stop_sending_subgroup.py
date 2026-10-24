"""STOP_SENDING on one subgroup stream ends that stream, not the
subscription (d22 §11.3.2): later groups keep arriving, through the
relay and from a PublishedTrack directly, with two tracks on the
session and the other track untouched."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack
from aiomoqt.types import MOQTMessageType, StreamResetCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 20100
_NS = "ss/subgroup"
_NAMES = ("video", "audio")
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _port(case, use_quic):
    return _PORT + 2 * case + (not use_quic)


def _reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
    relay._ns_subs.clear()


class _Produced(PublishedTrack):
    """Groups of 10 objects at 50/s through produce(), so each peer is a
    FanoutDelivery lane."""

    async def produce(self, out):
        group = 0
        while True:
            for oid in range(10):
                await out.write(group, oid, b"x" * 64, group_start=oid == 0)
                await asyncio.sleep(0.02)
            group += 1


def _tracks(pub, cls=PublishedTrack):
    out = {}
    for name in _NAMES:
        t = cls(pub, namespace=_NS, trackname=name,
                object_size=64, group_size=10, rate=50)
        t._quiet = True
        t.attach()
        out[name] = t
    return out


@contextlib.asynccontextmanager
async def _via_relay(port, use_quic):
    """Yields the subscriber session, the publisher's tracks served
    through the relay."""
    _reset()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=22)
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=22)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s
            pub = await connect()
            _tracks(pub)
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            yield await connect()
    finally:
        handle.close()
        _reset()


@contextlib.asynccontextmanager
async def _direct(port, use_quic, cls):
    """Yields the server-side session, subscribing to the client's
    PublishedTracks with no relay between."""
    peer = asyncio.get_running_loop().create_future()

    async def on_pub_ns(session, msg):
        session.publish_namepace_ok(msg)
        if not peer.done():
            peer.set_result(session)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=22)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE, on_pub_ns)
    handle = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=22)
        async with client.connect() as pub:
            await pub.client_session_init()
            _tracks(pub, cls)
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            yield await asyncio.wait_for(peer, 5.0)
    finally:
        handle.close()


async def _until(cond, tries=250):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


async def _stop_one_subgroup(rx):
    got = {n: [] for n in _NAMES}
    aliases = {}
    for name in _NAMES:
        ok = await rx.subscribe(
            _NS, name, forward=1, wait_response=True,
            on_object=lambda m, _s, _t, g, _sg, n=name: got[n].append(
                (g, m.object_id)))
        aliases[name] = ok.track_alias
    assert await _until(lambda: all(
        any(o >= 2 for _, o in got[n]) for n in _NAMES)), got
    group = got["video"][-1][0]
    sids = [sid for sid, st in rx._data_streams.items()
            if st.key == ("subgroup", (aliases["video"], group, 0))]
    assert sids
    for sid in sids:
        rx.stream_stop_sending(sid, StreamResetCode.CANCELLED)
    # Five groups' worth at 50 objects/s in groups of 10.
    assert await _until(lambda: all(
        any(g >= group + 3 for g, _ in got[n]) for n in _NAMES)), got
    assert rx._close_err is None
    return group


@_TRANSPORTS
async def test_relay_keeps_the_subscription_after_stop_sending(use_quic):
    async with _via_relay(_port(0, use_quic), use_quic) as rx:
        group = await _stop_one_subgroup(rx)
        video = relay._tracks[((b"ss", b"subgroup"), b"video")]
        assert len(video.downstream) == 1
        assert video._drops == 0
        assert any(k[1] == group for k in video._stopped)


@pytest.mark.parametrize("cls", [PublishedTrack, _Produced],
                         ids=["generate", "produce"])
@_TRANSPORTS
async def test_published_track_keeps_the_subscription_after_stop_sending(
        use_quic, cls):
    case = 1 if cls is PublishedTrack else 2
    async with _direct(_port(case, use_quic), use_quic, cls) as rx:
        await _stop_one_subgroup(rx)
