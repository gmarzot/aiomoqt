"""Track Properties through the relay (d22 §8.4, §3.7): each track's set
reaches every reply for that track unchanged, the newest set replaces the
cached one, and a Mandatory Track Property stops the track."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import request_error_code
from aiomoqt.messages import MOQTMessage
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack
from aiomoqt.types import GroupOrder, MOQTRequestError, RequestErrorCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 19700
_NS = "relay/props"
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_DRAFTS = pytest.mark.parametrize("draft", [18, 22])
# A registered property, an even and an odd application-range one, and
# per track something the other lacks.
_PROPS = {
    "video": {0x0E: 200, 0x3800: 4242, 0x3801: b"\xca\xfe", 0x7B: b"v"},
    "audio": {0x0E: 64, 0x3800: 7, 0x3801: b"audio-meta", 0x22: 2},
}
_IMMUTABLE = {"video": {0x04: 2000}, "audio": {}}


def _port(case, draft, use_quic):
    return _PORT + 8 * case + 2 * (draft == 22) + (not use_quic)


def _reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
    relay._ns_subs.clear()


@contextlib.asynccontextmanager
async def _relay(port, use_quic, draft):
    """A relay; yields connect(), which opens a session to it."""
    _reset()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s
            yield connect
    finally:
        handle.close()
        _reset()


class _Propertied(PublishedTrack):
    """A track whose PUBLISH, SUBSCRIBE_OK and FETCH_OK carry `props`
    besides its Immutable Properties."""

    def __init__(self, *args, props=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.props = props or {}

    def _track_properties(self, session):
        if not session._profile.track_properties:
            return None
        return {**(super()._track_properties(session) or {}), **self.props}


def _tracks(pub, sets=_PROPS):
    return {name: _Propertied(pub, namespace=_NS, trackname=name,
                              object_size=100, group_size=5, rate=50,
                              props=props,
                              immutable_properties=_IMMUTABLE.get(name))
            for name, props in sets.items()}


def _received(msg) -> dict:
    """The set a SUBSCRIBE_OK carried; the codec lifts a Descending group
    order out of it."""
    props = dict(msg.track_extensions or {})
    if getattr(msg, "group_order", None) == GroupOrder.DESCENDING:
        props[0x22] = int(GroupOrder.DESCENDING)
    return props


def _sent(track, session) -> dict:
    return track._track_properties(session)


def _relayed(name):
    return relay._tracks.get(((b"relay", b"props"), name.encode()))


async def _until(cond, tries=200):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


@pytest.mark.parametrize("path", ["publish", "subscribe"])
@_DRAFTS
@_TRANSPORTS
async def test_each_track_gets_its_own_properties(path, draft, use_quic):
    # Two tracks on one publisher session, both subscribed on one
    # subscriber session: SUBSCRIBE_OK, TRACK_STATUS and a cached FETCH
    # each carry that track's set, the Immutable Properties bytes intact.
    port = _port(0 if path == "publish" else 1, draft, use_quic)
    async with _relay(port, use_quic, draft) as connect:
        pub = await connect()
        tracks = _tracks(pub)
        if path == "publish":
            for t in tracks.values():
                await t.publish()
            assert await _until(lambda: all(_relayed(n) for n in tracks))
        else:
            for t in tracks.values():
                t.attach()
            await pub.publish_namespace(namespace=_NS, wait_response=True)
        want = {n: _sent(t, pub) for n, t in tracks.items()}
        assert MOQTMessage.IMMUTABLE_PROPERTIES in want["video"]
        rx = await connect()
        oks = {n: await rx.subscribe(_NS, n, forward=1, wait_response=True)
               for n in tracks}
        assert {n: _received(ok) for n, ok in oks.items()} == want

        for n in tracks:
            status = await rx.track_status(_NS, n, wait_response=True)
            assert status.track_properties == want[n], n
        assert await _until(lambda: all(
            _relayed(n) and _relayed(n).largest for n in tracks))
        for n in tracks:
            g = _relayed(n).largest[0]
            ok = await rx.fetch(_NS, n, start_group=g, end_group=g,
                                wait_response=True)
            assert ok.track_extensions == want[n], n
        assert rx._close_err is None and pub._close_err is None


@_DRAFTS
@_TRANSPORTS
async def test_a_publish_offer_carries_each_tracks_properties(draft,
                                                              use_quic):
    # The PUBLISH a SUBSCRIBE_TRACKS subscriber is offered carries the set
    # its publisher's PUBLISH did.
    async with _relay(_port(4, draft, use_quic), use_quic, draft) as connect:
        pub = await connect()
        tracks = _tracks(pub)
        for t in tracks.values():
            await t.publish()
        assert await _until(lambda: all(_relayed(n) for n in tracks))
        want = {n: _sent(t, pub) for n, t in tracks.items()}
        rx = await connect()
        await rx.subscribe_tracks(namespace=_NS)
        got = {}
        for _ in tracks:
            offer = await rx.await_publish(timeout=5.0)
            got[offer.track_name.decode()] = _received(offer)
        assert got == want
        assert rx._close_err is None and pub._close_err is None


@_TRANSPORTS
async def test_a_new_publisher_replaces_the_cached_set(use_quic):
    # §8.4: the newest set replaces the cached one, never merged with it.
    first = {"video": {0x0E: 10, 0x3800: 1, 0x3803: b"first"}}
    second = {"video": {0x3800: 2, 0x3805: b"second"}}
    async with _relay(_port(2, 22, use_quic), use_quic, 22) as connect:
        rx = await connect()
        pub = await connect()
        track = _tracks(pub, first)["video"]
        await track.publish()
        assert await _until(lambda: _relayed("video"))
        ok = await rx.subscribe(_NS, "video", forward=1, wait_response=True)
        assert _received(ok) == _sent(track, pub)

        pub2 = await connect()
        old = _relayed("video")
        track = _tracks(pub2, second)["video"]
        await track.publish()
        assert await _until(lambda: _relayed("video") not in (None, old))
        rx2 = await connect()
        ok = await rx2.subscribe(_NS, "video", forward=1, wait_response=True)
        assert _received(ok) == _sent(track, pub2)
        assert 0x0E not in _received(ok) and 0x3803 not in _received(ok)


@pytest.mark.parametrize("path", ["publish", "subscribe"])
@_TRANSPORTS
async def test_a_mandatory_track_property_stops_the_track(path, use_quic):
    # §3.7: a Mandatory Track Property the relay does not understand gets
    # UNSUPPORTED_EXTENSION; the track is never offered downstream.
    sets = {"video": {0x4000: 1, 0x3800: 3}}
    async with _relay(_port(3, 22, use_quic), use_quic, 22) as connect:
        pub = await connect()
        code = request_error_code(RequestErrorCode.UNSUPPORTED_EXTENSION,
                                  pub._profile)
        track = _tracks(pub, sets)["video"]
        rx = await connect()
        if path == "publish":
            with pytest.raises(MOQTRequestError) as refused:
                await pub.publish(_NS, "video",
                                  track_properties=_sent(track, pub),
                                  wait_response=True)
            assert refused.value.error_code == code
            assert _relayed("video") is None
        else:
            track.attach()
            await pub.publish_namespace(namespace=_NS, wait_response=True)
        with pytest.raises(MOQTRequestError) as refused:
            await rx.subscribe(_NS, "video", forward=1, wait_response=True)
        if path == "subscribe":
            assert refused.value.error_code == code
        assert rx._close_err is None and pub._close_err is None
