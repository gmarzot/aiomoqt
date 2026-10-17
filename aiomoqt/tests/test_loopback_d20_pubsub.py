"""Loopback publish and subscribe at drafts 20 and 22, over raw QUIC
and WebTransport, with two tracks on one session.

Each track writes objects whose payload names the track, group and
object, so the receiver can tell an object that reached the wrong track
or carried the wrong ids.
"""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import (
    LocationFilter, PublishStateNotify, RangeFilter, Subscribe,
)
from aiomoqt.messages.params import (
    D20ParamType, D20RequestErrorCode, LocationFilterType,
)
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import (
    FilterType, MOQTMessageType, MOQTRequestError, ParamType,
    SessionCloseCode,
)
from aiomoqt.utils.buffer import Buffer

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 16650
_DRAFTS = [20, 22]
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_NS = "d20/pubsub"
_NAMES = ("audio", "video")
# Groups from 100: the SUBGROUP_HEADER Group ID needs two vi64 bytes.
_GROUPS = range(100, 103)
_OBJECTS = range(4)
_EXPECTED = len(_GROUPS) * len(_OBJECTS)


def _port(offset: int, use_quic: bool) -> int:
    return _BASE_PORT + offset + (0 if use_quic else 1)


def _payload(name: str, group_id: int, object_id: int) -> bytes:
    return f"{name}:{group_id}:{object_id}".encode()


class _Finite(PublishedTrack):
    """_GROUPS x _OBJECTS objects once `go` is set, then PUBLISH_DONE."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.go = asyncio.Event()

    async def produce(self, out):
        await self.go.wait()
        for group_id in _GROUPS:
            for object_id in _OBJECTS:
                await out.write(group_id, object_id,
                                _payload(self.trackname, group_id, object_id),
                                group_start=object_id == 0)
            await asyncio.sleep(0.01)


class _Seen(_Finite):
    """Records the SUBSCRIBEs that reach it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.subscribes = []

    async def _on_subscribe(self, session, msg):
        self.subscribes.append(msg)
        await super()._on_subscribe(session, msg)


def _track(session, name, cls=_Finite):
    return cls(session, namespace=_NS, trackname=name, object_size=32,
               group_size=len(_OBJECTS), num_subgroups=1, rate=0)


class _Objects:
    """(group, object, payload) per alias, from one session."""

    def __init__(self):
        self.by_alias = {}

    def watch(self, session, alias: int) -> None:
        def on_object(obj, _size, _ts, group_id, _subgroup_id):
            if obj.payload:
                self.by_alias.setdefault(alias, []).append(
                    (group_id, obj.object_id, bytes(obj.payload)))
        session.register_object_handler(alias, on_object)

    def count(self, alias) -> int:
        return len(self.by_alias.get(alias, ()))

    def check(self, alias, name) -> None:
        got = sorted(self.by_alias.get(alias, ()))
        want = [(g, o, _payload(name, g, o))
                for g in _GROUPS for o in _OBJECTS]
        assert got == want, f"{name}: {got}"


async def _until(cond, timeout: float = 5.0) -> bool:
    try:
        async with asyncio.timeout(timeout):
            while not cond():
                await asyncio.sleep(0.02)
    except TimeoutError:
        return False
    return True


@contextlib.asynccontextmanager
async def _loopback(port, draft, use_quic, handlers=(), clean=True):
    """A client session to a server pinned to `draft`; yields (client
    session, server session). With `clean`, both must end without an
    error and the server must see NO_ERROR."""
    peers = []

    def wrap(handler):
        async def on_msg(session, msg):
            if session not in peers:
                peers.append(session)
            await handler(session, msg)
        return on_msg

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)

    async def on_setup(session, msg):
        if session not in peers:
            peers.append(session)
        _, default = session.CONTROL_REGISTRY[session.negotiated_draft][
            MOQTMessageType.SETUP]
        await default(session, msg)

    server.register_handler(MOQTMessageType.SETUP, on_setup)
    for msg_type, handler in handlers:
        server.register_handler(msg_type, wrap(handler))
    handle = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            assert session.negotiated_draft == draft
            assert await _until(lambda: bool(peers), 3.0)
            yield session, peers[0]
            if clean:
                assert session._close_err is None, session._close_err
                assert peers[0]._close_err is None, peers[0]._close_err
        if clean:
            async with asyncio.timeout(3):
                code, _ = await peers[0]._moqt_session_closed
            assert code == SessionCloseCode.NO_ERROR
    finally:
        handle.close()


# -- SUBSCRIBE-initiated ---------------------------------------------------

@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_subscribe_two_tracks(draft, use_quic):
    """The server subscribes to two tracks the client publishes; each
    SUBSCRIBE carries a LOCATION_FILTER and each track's objects arrive
    under its own alias with their own ids."""
    async with _loopback(_port(2 * (draft - 20), use_quic), draft,
                         use_quic) as (session, srv):
        tracks = [_track(session, name) for name in _NAMES]
        for track in tracks:
            track.attach()
        rx = _Objects()
        oks = []
        for name in _NAMES:
            ok = await srv.subscribe(_NS, name, forward=1,
                                     filter_type=FilterType.LATEST_OBJECT,
                                     wait_response=True)
            rx.watch(srv, ok.track_alias)
            oks.append(ok)
        assert len({ok.track_alias for ok in oks}) == 2
        for track in tracks:
            track.go.set()
        assert await _until(lambda: all(
            rx.count(ok.track_alias) >= _EXPECTED for ok in oks))
        for ok, name in zip(oks, _NAMES):
            rx.check(ok.track_alias, name)


@_TRANSPORTS
async def test_subscribe_routes_objects_and_done_per_request(use_quic):
    """subscribe(on_object=, on_done=) at d22: each of two tracks' objects
    and its PUBLISH_DONE reach that subscription's own handlers."""
    async with _loopback(_port(34, use_quic), 22, use_quic) as (session, srv):
        tracks = [_track(session, name) for name in _NAMES]
        for track in tracks:
            track.attach()
        got = {name: [] for name in _NAMES}
        done = {name: asyncio.get_running_loop().create_future()
                for name in _NAMES}

        def on_object(name):
            def cb(obj, _size, _ts, group_id, _subgroup_id):
                if obj.payload:
                    got[name].append(
                        (group_id, obj.object_id, bytes(obj.payload)))
            return cb

        oks = {}
        for name in _NAMES:
            oks[name] = await srv.subscribe(
                _NS, name, forward=1, wait_response=True,
                on_object=on_object(name), on_done=done[name].set_result)
        assert len({ok.track_alias for ok in oks.values()}) == 2
        for track in tracks:
            track.go.set()
        for name in _NAMES:
            status = await asyncio.wait_for(done[name], 5)
            assert status.request_id == oks[name].request_id
            assert status.stream_count == len(_GROUPS)
        assert await _until(lambda: all(
            len(got[name]) >= _EXPECTED for name in _NAMES))
        for name in _NAMES:
            assert sorted(got[name]) == [
                (g, o, _payload(name, g, o)) for g in _GROUPS for o in _OBJECTS]


# -- PUBLISH-initiated -----------------------------------------------------

@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_publish_two_tracks(draft, use_quic):
    """The client PUBLISHes two tracks paused; the server's PUBLISH_OK
    carries no FORWARD from d20, so its REQUEST_UPDATE starts each."""
    publishes: asyncio.Queue = asyncio.Queue()

    async def on_publish(session, msg):
        await publishes.put(msg)

    async with _loopback(_port(6 + 2 * (draft - 20), use_quic), draft,
                         use_quic,
                         [(MOQTMessageType.PUBLISH, on_publish)]
                         ) as (session, srv):
        tracks = [_track(session, name) for name in _NAMES]
        for track in tracks:
            await track.publish(forward=0)
        rx = _Objects()
        msgs = []
        for _ in tracks:
            msg = await asyncio.wait_for(publishes.get(), 5.0)
            assert msg.forward == 0
            rx.watch(srv, msg.track_alias)
            sent = srv.publish_ok(msg, forward=1)
            assert sent.forward is None and sent.filter_type is None
            msgs.append(msg)
        for track in tracks:
            track.go.set()
        assert await _until(lambda: all(
            rx.count(m.track_alias) >= _EXPECTED for m in msgs))
        for m in msgs:
            rx.check(m.track_alias, m.track_name.decode())


# -- LOCATION_FILTER: d20 and d22 differ where the spec says ----------------

def _filter_bytes(msg: Subscribe, prof) -> bytes:
    """The LOCATION_FILTER value as `msg` goes on the wire."""
    raw = bytes(msg.serialize(prof=prof).data)
    buf = Buffer(data=raw, vi64=True)
    buf.pull_vint()
    end = buf.tell() + buf.pull_uint16() + 2
    buf.pull_vint()                      # Request ID
    for _ in range(buf.pull_vint()):     # Track Namespace
        buf.pull_bytes(buf.pull_vint())
    buf.pull_bytes(buf.pull_vint())      # Track Name
    count, key = buf.pull_vint(), 0
    for _ in range(count):
        key += buf.pull_vint()
        start = buf.tell()
        if key == ParamType.LOCATION_FILTER:
            LocationFilter.decode(buf, prof=prof, buf_end=end)
            return raw[start:buf.tell()]
        raise AssertionError(f"unexpected parameter 0x{key:x}")
    return b""


_SEPARATING = [
    # (filter, d20 value, d22 value)
    (LocationFilter.next_object(), "020000", "05"),
    (LocationFilter.absolute(0, 0), "00", "020000"),
    (LocationFilter.relative(128), "028080", "018080"),
]


@_TRANSPORTS
async def test_d20_and_d22_location_filters_differ_on_the_wire(use_quic):
    """Next Object, an absolute {0, 0} start and a value >= 128 encode
    differently at d20 and d22; each draft's peer reads back the filter
    that was sent, and objects flow under it."""
    sent = {}
    for draft in (20, 22):
        port = _port(12 + (draft - 20), use_quic)
        async with _loopback(port, draft, use_quic) as (session, srv):
            tracks = [_track(session, f"f{i}", _Seen)
                      for i in range(len(_SEPARATING))]
            for track in tracks:
                track.attach()
            rx = _Objects()
            oks = []
            for track, (lf, *_) in zip(tracks, _SEPARATING):
                # FORWARD omitted (1): LOCATION_FILTER is the only
                # parameter.
                msg = srv.subscribe(_NS, track.trackname,
                                    location_filter=lf)
                sent[draft, lf] = _filter_bytes(msg, srv._profile).hex()
                ok = await srv._await_response(msg.request_id)
                rx.watch(srv, ok.track_alias)
                oks.append(ok)
            for track, (lf, *_) in zip(tracks, _SEPARATING):
                got = track.subscribes[0].location_filter
                if lf.type == LocationFilterType.ABSOLUTE_START and draft == 20:
                    # d20 sends an open filter from {0, 0} as no filter.
                    assert got.type == LocationFilterType.NONE
                else:
                    assert got == lf
                track.go.set()
            assert await _until(lambda: all(
                rx.count(ok.track_alias) >= _EXPECTED for ok in oks))
            for ok, track in zip(oks, tracks):
                rx.check(ok.track_alias, track.trackname)
    for lf, d20, d22 in _SEPARATING:
        assert sent[20, lf] == d20 and sent[22, lf] == d22
        assert d20 != d22


# -- SUBSCRIBE_TRACKS parameters reach the PUBLISH ---------------------------

@_TRANSPORTS
@pytest.mark.parametrize("draft", [20, 22])
async def test_subscribe_tracks_forward_reaches_the_publish(draft, use_quic):
    """A PUBLISH answering SUBSCRIBE_TRACKS carries its FORWARD, whatever
    the publisher asked for, and never its AUTHORIZATION TOKEN; a
    REQUEST_UPDATE of FORWARD applies to later PUBLISHes."""
    async with _loopback(_port(18 + 2 * (draft - 20) // 2, use_quic), draft,
                         use_quic) as (session, srv):
        st = await session.subscribe_tracks(
            _NS, parameters={ParamType.FORWARD: 0,
                             ParamType.AUTH_TOKEN: b"token"},
            wait_response=True)
        first = _track(srv, "first")
        await first.publish(forward=1)
        pub = await session.await_publish(timeout=5)
        assert pub.forward == 0 and not first.forward
        assert ParamType.AUTH_TOKEN not in (pub.parameters or {})
        await session.request_update(st.request_id, forward=1,
                                     wait_response=True)
        second = _track(srv, "second")
        await second.publish(forward=0)
        pub = await session.await_publish(timeout=5)
        assert pub.forward == 1 and second.forward
        for p in (first, second):
            p.go.set()


# -- request rules from d20 ------------------------------------------------

@_TRANSPORTS
async def test_range_filters_are_refused_without_max_filter_ranges(use_quic):
    """Neither end sends MAX_FILTER_RANGES, so a Range Filter is refused
    before it is sent and, sent anyway, answered with INVALID_FILTER."""
    async with _loopback(_port(22, use_quic), 22, use_quic) as (session, srv):
        _track(session, "video").attach()
        params = {D20ParamType.SUBGROUP_FILTER:
                  RangeFilter(set_id=0, ranges=((0, 3),))}
        with pytest.raises(ValueError, match="MAX_FILTER_RANGES"):
            srv.subscribe(_NS, "video", parameters=dict(params))
        srv._peer_max_filter_ranges = 4
        with pytest.raises(MOQTRequestError) as err:
            await srv.subscribe(_NS, "video", parameters=dict(params),
                                wait_response=True)
        assert err.value.error_code == D20RequestErrorCode.INVALID_FILTER


@_TRANSPORTS
async def test_a_parameter_outside_the_update_scope_closes(use_quic):
    """GROUP_ORDER may not appear in a subscription's REQUEST_UPDATE."""
    async with _loopback(_port(24, use_quic), 20, use_quic,
                         clean=False) as (session, srv):
        _track(session, "video").attach()
        ok = await srv.subscribe(_NS, "video", wait_response=True)
        srv.request_update(ok.request_id,
                           parameters={ParamType.GROUP_ORDER: 1})
        assert await _until(lambda: session._close_err is not None)
        assert session._close_err[0] == SessionCloseCode.PROTOCOL_VIOLATION


@_TRANSPORTS
async def test_the_publisher_may_not_update_a_subscribe(use_quic):
    async with _loopback(_port(26, use_quic), 20, use_quic,
                         clean=False) as (session, srv):
        _track(session, "video").attach()
        ok = await srv.subscribe(_NS, "video", wait_response=True)
        session.request_update(ok.request_id, forward=0)
        assert await _until(lambda: srv._close_err is not None)
        assert srv._close_err[0] == SessionCloseCode.PROTOCOL_VIOLATION


@_TRANSPORTS
async def test_publish_state_notify_comes_only_from_the_publisher(use_quic):
    async with _loopback(_port(28, use_quic), 22, use_quic,
                         clean=False) as (session, srv):
        _track(session, "video").attach()
        ok = await srv.subscribe(_NS, "video", wait_response=True)
        rid = ok.request_id
        session.send_stream_message(
            session._bidi_streams[rid],
            PublishStateNotify(forward=1, largest_group_id=0,
                               largest_object_id=0))
        await asyncio.sleep(0.2)
        assert srv._close_err is None and session._close_err is None
        srv.send_stream_message(srv._bidi_streams[rid],
                                PublishStateNotify(forward=0))
        assert await _until(lambda: session._close_err is not None)
        assert session._close_err[0] == SessionCloseCode.PROTOCOL_VIOLATION


# -- fills and filter ends ----------------------------------------------------

@_TRANSPORTS
async def test_a_fill_is_counted_and_the_subscription_goes_on(use_quic):
    """FILL_PARAMETERS on a track with Objects: the fill fetch stream is
    counted in PUBLISH_DONE; live Objects flow."""
    async with _loopback(_port(30, use_quic), 22, use_quic) as (session, srv):
        track = _track(session, "video")
        track.prefill(1)
        track.attach()
        done = asyncio.get_running_loop().create_future()
        msg = srv.subscribe(_NS, "video", parameters={
            D20ParamType.FILL_PARAMETERS: {
                ParamType.LOCATION_FILTER: LocationFilter.relative(1)}})
        srv.register_publish_done_handler(msg.request_id, done.set_result)
        ok = await srv._await_response(msg.request_id)
        rx = _Objects()
        rx.watch(srv, ok.track_alias)
        track.go.set()
        assert await _until(lambda: rx.count(ok.track_alias) >= _EXPECTED)
        rx.check(ok.track_alias, "video")
        assert track.subscriptions[0].fill_streams == 1
        status = await asyncio.wait_for(done, 5)
        assert status.stream_count == len(_GROUPS) + 1


@_TRANSPORTS
async def test_a_subscription_outlives_its_filter(use_quic):
    """d20 §5.1.2: past the filter's end Objects stop, PUBLISH_DONE does
    not come, and the subscription stays."""
    async with _loopback(_port(32, use_quic), 20, use_quic) as (session, srv):
        track = PublishedTrack(session, namespace=_NS, trackname="gen",
                               object_size=32, group_size=4,
                               num_subgroups=1, rate=400)
        track.attach()
        got = []
        done = []
        # Bound as SUBSCRIBE_OK is parsed: the track produces at once.
        msg = srv.subscribe(
            _NS, "gen",
            location_filter=LocationFilter.absolute(1, 0, 2, 1),
            on_object=lambda obj, _s, _t, g, _sg: got.append(
                (g, obj.object_id, bool(obj.payload))),
            on_done=done.append)
        await srv._await_response(msg.request_id)
        want = [(1, o) for o in range(4)] + [(2, 0), (2, 1)]
        assert await _until(lambda: sum(p for *_, p in got) >= len(want))
        await asyncio.sleep(0.3)
        assert sorted((g, o) for g, o, p in got if p) == want
        # Nothing past the end, END_OF_GROUP included.
        assert max((g, o) for g, o, _ in got) == (2, 1)
        assert not done
        assert msg.request_id in srv._bidi_streams
