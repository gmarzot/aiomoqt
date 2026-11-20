"""Several concurrent subscriptions to one track on one session (d20 and
d22 §3.1): each SUBSCRIBE has its own Request ID, filter, Forward State
and PUBLISH_DONE, and an Object matching several is sent once for each.
Before d20 a second SUBSCRIBE is refused with DUPLICATE_SUBSCRIPTION
(§5.1).

The client publishes and the server's session subscribes. Every case
runs two tracks on the session, over raw QUIC and WebTransport, with a
generate() and a produce() publisher.
"""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import (FilterType, MOQTMessageType, MOQTRequestError,
                           RequestErrorCode)

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 35200
_NS = "multi/sub"
# Distinct sizes tell the tracks' objects apart.
_SIZES = {"audio": 100, "video": 200}
_GROUP = 10
_RATE = 200
_MIN_OBJECTS = 20
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_D20 = pytest.mark.parametrize("draft", [20, 22])
_KINDS = pytest.mark.parametrize("kind", ["generate", "produce"])


def _port(case: int, draft: int, kind: str, use_quic: bool) -> int:
    return (_BASE_PORT + 20 * case + 4 * [16, 18, 20, 22].index(draft)
            + 2 * ["generate", "produce"].index(kind)
            + (0 if use_quic else 1))


class _Produced(PublishedTrack):
    """Numbers each object once, whatever the subscriptions."""

    async def produce(self, out):
        payload = bytes(self.object_size)
        group_id = 0
        while True:
            for object_id in range(self.group_size):
                await out.write(group_id, object_id, payload,
                                group_start=object_id == 0)
                await asyncio.sleep(1 / self.rate)
            group_id += 1


def _track(session, name: str, kind: str) -> PublishedTrack:
    cls = _Produced if kind == "produce" else PublishedTrack
    track = cls(session, namespace=_NS, trackname=name,
                object_size=_SIZES[name], group_size=_GROUP,
                num_subgroups=1, rate=_RATE)
    track._quiet = True
    return track


class _Rx:
    """One subscription's objects and PUBLISH_DONE."""

    def __init__(self, name: str):
        self.size = _SIZES[name]
        self.objects: list = []
        self.sizes: set = set()
        self.done = asyncio.get_running_loop().create_future()

    def on_object(self, obj, _size, _ts, group_id, *_):
        if obj.payload:
            self.objects.append((group_id, obj.object_id))
            self.sizes.add(len(obj.payload))

    def on_done(self, msg) -> None:
        if not self.done.done():
            self.done.set_result(msg)

    def groups(self) -> list:
        return sorted({g for g, _ in self.objects})

    def check(self, first_group=None, whole: bool = True) -> None:
        """Each object once, of this track; with `whole`, every group
        from the first on, complete but for the last two in flight."""
        assert len(self.objects) >= _MIN_OBJECTS, len(self.objects)
        assert len(set(self.objects)) == len(self.objects), "duplicates"
        assert self.sizes == {self.size}
        groups = self.groups()
        if first_group is not None:
            assert groups[0] == first_group, (groups[0], first_group)
        if not whole:
            return
        assert groups == list(range(groups[0], groups[-1] + 1)), groups
        seen = set(self.objects)
        for g in groups[:-2]:
            missing = [o for o in range(_GROUP) if (g, o) not in seen]
            assert not missing, f"group {g} lacks {missing}"


class _Peer:
    """Server side: the subscribing session and the PUBLISHes it got."""

    def __init__(self):
        self.session = None
        self.publishes: asyncio.Queue = asyncio.Queue()

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

    async def subscribe(self, name: str, **kw):
        rx = _Rx(name)
        ok = await self.session.subscribe(
            _NS, name, forward=1, wait_response=True,
            on_object=rx.on_object, on_done=rx.on_done, **kw)
        return ok, rx


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


async def _grows(rx, by: int = _MIN_OBJECTS) -> bool:
    base = len(rx.objects)
    return await _until(lambda: len(rx.objects) >= base + by)


async def _stalls(rx) -> bool:
    """No object arrives after the in-flight ones settle."""
    await asyncio.sleep(0.2)
    base = len(rx.objects)
    await asyncio.sleep(0.3)
    return len(rx.objects) == base


async def _two_subscriptions(peer, start_ahead: int = 10):
    """Audio once; video twice, the second from a group well ahead."""
    _, audio = await peer.subscribe("audio")
    ok_a, a = await peer.subscribe(
        "video", filter_type=FilterType.NEXT_GROUP_START)
    assert await _until(lambda: len(a.objects) >= _MIN_OBJECTS)
    start = a.groups()[-1] + start_ahead
    ok_b, b = await peer.subscribe(
        "video", filter_type=FilterType.ABSOLUTE_START,
        start_group=start, start_object=0)
    assert ok_a.request_id != ok_b.request_id
    assert ok_a.track_alias != ok_b.track_alias
    assert await _until(lambda: len(b.objects) >= _MIN_OBJECTS)
    return audio, (ok_a, a), (ok_b, b), start


def _sub_at(track, alias):
    return next(s for s in track.subscriptions if s.track_alias == alias)


@pytest.mark.asyncio
@_TRANSPORTS
@_KINDS
@_D20
async def test_each_subscription_gets_its_own_objects(draft, kind, use_quic):
    """Two SUBSCRIBEs from different start groups: each gets every
    object its filter matches, once; the later one's are the earlier
    one's from its start on."""
    peer = _Peer()
    async with _loopback(_port(0, draft, kind, use_quic), draft, use_quic,
                         peer) as session:
        tracks = [_track(session, n, kind) for n in ("audio", "video")]
        for track in tracks:
            track.attach()
        audio, (_, a), (_, b), start = await _two_subscriptions(peer)
        assert await _grows(a) and await _grows(audio)
        a.check()
        b.check(first_group=start)
        audio.check()
        upto = min(a.groups()[-1], b.groups()[-1]) - 2
        assert upto >= start
        assert ({o for o in a.objects if start <= o[0] <= upto}
                == {o for o in b.objects if o[0] <= upto})


@pytest.mark.asyncio
@_TRANSPORTS
@_KINDS
@_D20
async def test_forward_state_is_per_subscription(draft, kind, use_quic):
    """REQUEST_UPDATE forward=0 on one subscription pauses only it;
    forward=1 resumes it."""
    peer = _Peer()
    async with _loopback(_port(1, draft, kind, use_quic), draft, use_quic,
                         peer) as session:
        for n in ("audio", "video"):
            _track(session, n, kind).attach()
        audio, (ok_a, a), (_, b), _ = await _two_subscriptions(
            peer, start_ahead=2)
        await peer.session.request_update(ok_a.request_id, forward=0,
                                          wait_response=True)
        assert await _stalls(a), "paused subscription still receives"
        assert await _grows(b), f"other stopped at {len(b.objects)}"
        assert await _grows(audio)
        await peer.session.request_update(ok_a.request_id, forward=1,
                                          wait_response=True)
        assert await _grows(a), "resumed subscription receives nothing"
        a.check(whole=False)
        b.check()


@pytest.mark.asyncio
@_TRANSPORTS
@_KINDS
@_D20
async def test_unsubscribing_one_keeps_the_other(draft, kind, use_quic):
    peer = _Peer()
    async with _loopback(_port(2, draft, kind, use_quic), draft, use_quic,
                         peer) as session:
        video = _track(session, "video", kind)
        for track in (_track(session, "audio", kind), video):
            track.attach()
        audio, (ok_a, a), (ok_b, b), _ = await _two_subscriptions(
            peer, start_ahead=2)
        peer.session.unsubscribe(ok_a.request_id)
        sub_a = _sub_at(video, ok_a.track_alias)
        assert await _until(lambda: not sub_a.generating and not sub_a.tasks)
        assert _sub_at(video, ok_b.track_alias).generating
        assert await _grows(b), f"other stopped at {len(b.objects)}"
        assert await _grows(audio)
        b.check()


@pytest.mark.asyncio
@_TRANSPORTS
@_KINDS
@_D20
async def test_each_subscription_gets_its_own_publish_done(draft, kind,
                                                           use_quic):
    """The track ends: each subscription gets PUBLISH_DONE on its own
    request, counting its own streams."""
    peer = _Peer()
    async with _loopback(_port(3, draft, kind, use_quic), draft, use_quic,
                         peer) as session:
        video = _track(session, "video", kind)
        for track in (_track(session, "audio", kind), video):
            track.attach()
        audio, (ok_a, a), (ok_b, b), start = await _two_subscriptions(peer)
        video.end()
        done_a = await asyncio.wait_for(a.done, 5.0)
        done_b = await asyncio.wait_for(b.done, 5.0)
        assert done_a.request_id == ok_a.request_id
        assert done_b.request_id == ok_b.request_id
        groups_a, groups_b = len(a.groups()), len(b.groups())
        for count, groups in ((done_a.stream_count, groups_a),
                              (done_b.stream_count, groups_b)):
            assert groups <= count <= groups + 2, (count, groups)
        assert abs((done_a.stream_count - done_b.stream_count)
                   - (groups_a - groups_b)) <= 1
        assert done_a.stream_count > done_b.stream_count
        assert await _grows(audio), "the other track ended too"
        a.check(whole=False)
        b.check(first_group=start, whole=False)


@pytest.mark.asyncio
@_TRANSPORTS
@_KINDS
@_D20
async def test_subscribe_beside_our_publish_is_served(draft, kind, use_quic):
    """PUBLISH accepted, then a SUBSCRIBE for the same track: a second
    subscription, and both get the objects."""
    peer = _Peer()
    async with _loopback(_port(4, draft, kind, use_quic), draft, use_quic,
                         peer) as session:
        tracks = [_track(session, n, kind) for n in ("audio", "video")]
        for track in tracks:
            await track.publish(forward=0)
        pushed = {}
        for _ in tracks:
            msg = await asyncio.wait_for(peer.publishes.get(), 5.0)
            name = msg.track_name.decode()
            rx = pushed[name] = _Rx(name)
            peer.session.register_object_handler(msg.track_alias,
                                                 rx.on_object)
            peer.session.publish_ok(msg, forward=1)
            pushed[name, "alias"] = msg.track_alias
        assert await _until(lambda: all(
            len(pushed[n].objects) >= _MIN_OBJECTS
            for n in ("audio", "video")))
        ok, rx = await peer.subscribe("video")
        assert ok.track_alias != pushed["video", "alias"]
        assert await _until(lambda: len(rx.objects) >= _MIN_OBJECTS)
        for r in (rx, pushed["video"], pushed["audio"]):
            assert await _grows(r)
            r.check()


@pytest.mark.asyncio
@_TRANSPORTS
@pytest.mark.parametrize("draft", [16, 18])
async def test_second_subscribe_refused_before_d20(draft, use_quic):
    peer = _Peer()
    async with _loopback(_port(5, draft, "generate", use_quic), draft,
                         use_quic, peer) as session:
        for n in ("audio", "video"):
            _track(session, n, "generate").attach()
        _, audio = await peer.subscribe("audio")
        _, video = await peer.subscribe("video")
        assert await _until(lambda: len(video.objects) >= _MIN_OBJECTS)
        with pytest.raises(MOQTRequestError) as err:
            await peer.subscribe("video")
        assert err.value.response is not None, "timed out, no reply"
        assert err.value.error_code == RequestErrorCode.DUPLICATE_SUBSCRIPTION
        for rx in (video, audio):
            assert await _grows(rx)
            rx.check()


@pytest.mark.asyncio
@_TRANSPORTS
@_D20
async def test_relay_refuses_a_second_downstream_subscribe(draft, use_quic):
    """aiomoqt-relay keeps one downstream subscription per track and
    session: a second is NOT_SUPPORTED, and the first keeps flowing."""
    from aiomoqt.tools import moq_interop_relay as relay
    tables = (relay._announced, relay._tracks, relay._announce_requests,
              relay._ns_subs, relay._track_subs)
    for t in tables:
        t.clear()
    port = _port(6, draft, "generate", use_quic)
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    pub = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                     verify_tls=False, supported_drafts=draft)
    sub = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                     verify_tls=False, supported_drafts=draft)
    try:
        async with pub.connect() as ps, sub.connect() as ss:
            await ps.client_session_init()
            await ss.client_session_init()
            for n in ("audio", "video"):
                await _track(ps, n, "generate").publish(forward=1)
            peer = _Peer()
            peer.session = ss
            _, audio = await peer.subscribe("audio")
            _, video = await peer.subscribe("video")
            assert await _until(lambda: len(video.objects) >= _MIN_OBJECTS)
            with pytest.raises(MOQTRequestError) as err:
                await peer.subscribe("video")
            assert err.value.response is not None, "timed out, no reply"
            assert err.value.error_code == RequestErrorCode.NOT_SUPPORTED
            for rx in (video, audio):
                assert await _grows(rx)
                rx.check(whole=False)
    finally:
        handle.close()
        for t in tables:
            t.clear()
