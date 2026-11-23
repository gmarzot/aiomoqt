"""The catalog track on the shared fan-out path: every subscription, on
any session, takes each catalog object once; one that joins or resumes
opens a new group whose Object 0 is the full catalog (msf §11.2), which
every subscription takes."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.media import (
    Catalog, CatalogTrack, CatalogTrackPublisher, DeltaOp,
)
from aiomoqt.messages import LocationFilter
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, ObjectStatus

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 21500
_NS = "catalog/fanout"
_DRAFTS = [18, 20, 22]
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _port(case, draft, use_quic):
    return _BASE_PORT + 6 * case + 2 * _DRAFTS.index(draft) + (not use_quic)


def _catalog(n=0):
    return Catalog(generatedAt=n, tracks=[
        CatalogTrack(name="video", packaging="loc", isLive=True,
                     codec="avc1.640028", bitrate=1_000_000)])


def _clone(name):
    return Catalog.delta([DeltaOp("clone", [CatalogTrack(
        parentName="video", name=name, bitrate=500_000)])])


class _Rx:
    """Subscriber side: the server sessions, in connection order."""

    def __init__(self):
        self.sessions = []

    async def on_publish_namespace(self, session, msg):
        self.sessions.append(session)
        session.publish_namespace_ok(msg)


class _Sub:
    """One catalog subscription: its objects, End of Track, PUBLISH_DONE."""

    def __init__(self, session):
        self.session = session
        self.objects = []        # (group, object, payload)
        self.ended = None        # Location of End of Track
        self.done = asyncio.get_running_loop().create_future()
        self.ok = None

    def _on_object(self, obj, _size, _ts, group_id, *_):
        if getattr(obj, "status", None) == ObjectStatus.END_OF_TRACK:
            self.ended = (group_id, obj.object_id)
        elif obj.payload:
            self.objects.append((group_id, obj.object_id, bytes(obj.payload)))

    async def subscribe(self, forward=1):
        self.ok = await self.session.subscribe(
            _NS, "catalog", forward=forward, wait_response=True,
            on_object=self._on_object,
            on_done=lambda m: self.done.done() or self.done.set_result(m))
        return self

    @property
    def request_id(self):
        return self.ok.request_id

    def locations(self):
        locs = [(g, o) for g, o, _ in self.objects]
        assert len(set(locs)) == len(locs), f"a Location twice: {locs}"
        return locs

    def applied(self) -> Catalog:
        """The catalog its objects add up to; the first is independent."""
        (_, _, first), *rest = self.objects
        cat = Catalog.from_json(first)
        assert not cat.is_delta
        for _, _, payload in rest:
            update = Catalog.from_json(payload)
            if update.is_delta:
                cat.apply(update)
            else:
                cat = update
        return cat


@contextlib.asynccontextmanager
async def _loopback(port, draft, use_quic, n_sessions=1):
    """`n_sessions` publisher sessions sharing one catalog track; yields
    (track, subscriber-side sessions)."""
    rx = _Rx()
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            rx.on_publish_namespace)
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            track = None
            for i in range(n_sessions):
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                session = await stack.enter_async_context(client.connect())
                await session.client_session_init()
                if track is None:
                    track = CatalogTrackPublisher(session, _NS, _catalog())
                else:
                    track.add_session(session)
                track.attach(session)
                await session.publish_namespace(namespace=_NS,
                                                wait_response=True)
                await _until(lambda: len(rx.sessions) == i + 1)
            yield track, rx.sessions
    finally:
        handle.close()


async def _until(pred, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.01)


async def _publish_run(track):
    """A delta, a new catalog, another delta."""
    await track.publish_delta(_clone("video-2"))
    await track.publish_catalog(_catalog(1))
    await track.publish_delta(_clone("video-3"))


# The first demand numbers the current catalog at (0, 0), in the history
# for the joining FETCH; each join then opens a group.


@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_two_sessions_take_every_object_once(draft, use_quic):
    async with _loopback(_port(0, draft, use_quic), draft, use_quic,
                         n_sessions=2) as (track, sessions):
        a = await _Sub(sessions[0]).subscribe()
        await _until(lambda: a.locations() == [(1, 0)])
        b = await _Sub(sessions[1]).subscribe()
        await _until(lambda: b.locations() == [(2, 0)]
                     and len(a.objects) == 2)
        await _publish_run(track)
        await _until(lambda: len(b.objects) == 4 and len(a.objects) == 5)
        await track.finish()
        done_a, done_b = await asyncio.wait_for(
            asyncio.gather(a.done, b.done), 5.0)
        await asyncio.sleep(0.1)
        run = [(2, 0), (2, 1), (3, 0), (3, 1)]
        assert a.locations() == [(1, 0), *run]
        assert b.locations() == run
        for sub in (a, b):
            assert sub.applied().to_json() == track.catalog.to_json()
            assert sub.ended == (3, 2)
        assert (done_a.stream_count, done_b.stream_count) == (3, 2)
        assert len(track._out.lanes) == 2


@_TRANSPORTS
@pytest.mark.parametrize("draft", [20, 22])
async def test_two_subscriptions_on_one_session(draft, use_quic):
    # From d20 one session may hold several subscriptions (§3.1).
    async with _loopback(_port(6, draft, use_quic), draft, use_quic
                         ) as (track, sessions):
        a = await _Sub(sessions[0]).subscribe()
        await _until(lambda: a.locations() == [(1, 0)])
        b = await _Sub(sessions[0]).subscribe()
        assert a.ok.track_alias != b.ok.track_alias
        await _until(lambda: b.locations() == [(2, 0)]
                     and len(a.objects) == 2)
        await _publish_run(track)
        await _until(lambda: len(b.objects) == 4 and len(a.objects) == 5)
        run = [(2, 0), (2, 1), (3, 0), (3, 1)]
        assert a.locations() == [(1, 0), *run]
        assert b.locations() == run
        sessions[0].unsubscribe(a.request_id)
        await _until(lambda: len(track._out.lanes) == 2
                     and sum(s.generating for s in track.subscriptions) == 1)
        await track.publish_delta(_clone("video-4"))
        await _until(lambda: len(b.objects) == 5)
        await track.finish()
        done = await asyncio.wait_for(b.done, 5.0)
        await asyncio.sleep(0.1)
        assert len(a.objects) == 5 and a.ended is None
        assert b.locations() == [*run, (3, 2)]
        assert b.ended == (3, 3)
        assert done.stream_count == 2
        for sub in (a, b):
            assert sub.applied().find("video-3") is not None
        assert b.applied().to_json() == track.catalog.to_json()


@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_mid_group_joiner_gets_a_new_group(draft, use_quic):
    async with _loopback(_port(1, draft, use_quic), draft, use_quic,
                         n_sessions=2) as (track, sessions):
        first = await _Sub(sessions[0]).subscribe()
        for name in ("video-2", "video-3"):
            await track.publish_delta(_clone(name))
        await _until(lambda: len(first.objects) == 3)
        late = await _Sub(sessions[1]).subscribe()
        await _until(lambda: len(late.objects) == 1)
        await track.publish_delta(_clone("video-4"))
        await _until(lambda: len(late.objects) == 2
                     and len(first.objects) == 5)
        await asyncio.sleep(0.1)
        assert first.locations() == [(1, 0), (1, 1), (1, 2), (2, 0), (2, 1)]
        assert late.locations() == [(2, 0), (2, 1)]
        for sub in (first, late):
            assert sub.applied().to_json() == track.catalog.to_json()


@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_forward_resume_gets_a_new_group(draft, use_quic):
    async with _loopback(_port(2, draft, use_quic), draft, use_quic
                         ) as (track, sessions):
        sub = await _Sub(sessions[0]).subscribe()
        await track.publish_delta(_clone("video-2"))
        await _until(lambda: len(sub.objects) == 2)
        peer = track.subscriptions[0]
        sub.session.request_update(sub.request_id, forward=0)
        await _until(lambda: not peer.forward)
        await track.publish_delta(_clone("video-3"))
        await track.publish_catalog(_catalog(1))
        await track.publish_delta(_clone("video-4"))
        await asyncio.sleep(0.2)
        assert len(sub.objects) == 2                     # §5.1: none
        sub.session.request_update(sub.request_id, forward=1)
        await _until(lambda: len(sub.objects) == 3)
        await track.publish_delta(_clone("video-5"))
        await _until(lambda: len(sub.objects) == 4)
        await asyncio.sleep(0.1)
        assert sub.locations() == [(1, 0), (1, 1), (3, 0), (3, 1)]
        resumed = sub.objects[2:]
        assert not Catalog.from_json(resumed[0][2]).is_delta
        sub.objects = resumed
        assert sub.applied().to_json() == track.catalog.to_json()


@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_subscribe_at_forward_0_then_resume(draft, use_quic):
    async with _loopback(_port(3, draft, use_quic), draft, use_quic
                         ) as (track, sessions):
        sub = await _Sub(sessions[0]).subscribe(forward=0)
        await track.publish_delta(_clone("video-2"))
        await asyncio.sleep(0.2)
        assert sub.objects == []
        sub.session.request_update(sub.request_id, forward=1)
        await _until(lambda: len(sub.objects) == 1)
        await asyncio.sleep(0.1)
        assert sub.locations() == [(1, 0)]
        assert sub.applied().find("video-2") is not None


@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_fetch_serves_the_current_catalog(draft, use_quic):
    async with _loopback(_port(4, draft, use_quic), draft, use_quic
                         ) as (track, sessions):
        session = sessions[0]

        async def fetch():
            got = []
            if draft >= 20:
                msg = session.fetch(
                    _NS, "catalog",
                    location_filter=LocationFilter.absolute(0, 0))
            else:
                msg = session.fetch(_NS, "catalog", start_group=0,
                                    start_object=0, end_group=9,
                                    end_object=0)
            session.register_fetch_object_handler(
                msg.request_id, lambda obj, *_: got.append(
                    (obj.group_id, obj.object_id, bytes(obj.payload))))
            await session._await_response(msg.request_id)
            assert await session.await_fetch_done(msg.request_id, timeout=3)
            return got

        got = await fetch()                              # nothing published
        assert [(g, o) for g, o, _ in got] == [(0, 0)]
        assert Catalog.from_json(got[0][2]).to_json() == _catalog().to_json()
        await track.publish_delta(_clone("video-2"))
        got = await fetch()
        assert [(g, o) for g, o, _ in got] == [(0, 0), (0, 1)]
        assert Catalog.from_json(got[1][2]).is_delta
        assert session._close_err is None


@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_unsubscribe_resets_and_group_ids_go_on(
        draft, use_quic):
    async with _loopback(_port(5, draft, use_quic), draft, use_quic
                         ) as (track, sessions):
        first = await _Sub(sessions[0]).subscribe()
        await track.publish_delta(_clone("video-2"))
        await _until(lambda: len(first.objects) == 2)
        delivery = track.subscriptions[0].delivery
        first.session.unsubscribe(first.request_id)
        await _until(lambda: not track.subscriptions[0].generating)
        await _until(lambda: delivery.open_group is None)
        second = await _Sub(sessions[0]).subscribe()
        await _until(lambda: len(second.objects) == 1)
        await track.publish_catalog(_catalog(1))
        await _until(lambda: len(second.objects) == 2)
        await asyncio.sleep(0.1)
        assert second.locations() == [(2, 0), (3, 0)]
        assert first.locations() == [(1, 0), (1, 1)]


async def test_refresh_before_first_demand_numbers_nothing():
    track = CatalogTrackPublisher(object(), _NS, _catalog())
    track.next_group = 4
    for n in range(50):
        await track.publish_catalog(_catalog(n))
    assert track._group is None and len(track._history) == 0
    assert track.next_group == 4 and track._largest == (0, 0)
    await track.publish_delta(_clone("video-2"))         # first demand
    assert track._largest == (4, 1) and len(track._history) == 2
    assert track.catalog.generatedAt == 49
    assert track.catalog.find("video-2") is not None
