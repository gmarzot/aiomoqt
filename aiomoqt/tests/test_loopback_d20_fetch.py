"""FETCH at drafts 20 and 22 over raw QUIC and WebTransport: the
range in LOCATION_FILTER with an inclusive End, joining by SUBSCRIBE and
a FETCH after LARGEST_OBJECT, fill fetch streams, and End of Timed-Out
Range. Two tracks publish on one session; their payload sizes differ, so
an object served from the wrong track shows.
"""
import asyncio
import contextlib

import pytest

from aiomoqt.agent import AgentSession, FetchSpec, StartAt, TrackRef
from aiomoqt.client import MOQTClient
from aiomoqt.messages import FetchOk, LocationFilter, RangeFilter
from aiomoqt.messages.params import D20ParamType, D20RequestErrorCode
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import (
    FetchType, GroupOrder, MOQTMessageType, MOQTRequestError, ParamType,
    RequestErrorCode, SessionCloseCode,
)

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 16750
_NS = "d20/fetch"
_SIZES = {"video": 300, "audio": 100}
_GROUP = 5
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_DRAFTS = pytest.mark.parametrize("draft", [20, 22])


def _port(case: int, use_quic: bool) -> int:
    return _BASE_PORT + 2 * case + (not use_quic)


def _locations(objs):
    return [(o.group_id, o.object_id) for o in objs]


def _range(start, end):
    """Every (group, object) from start to end inclusive, _GROUP a group."""
    return [(g, o) for g in range(start[0], end[0] + 1) for o in range(_GROUP)
            if start <= (g, o) <= end]


class _Rx:
    """What reached the fetching session: fetch objects and range
    markers by the Request ID on their stream."""

    def __init__(self, session):
        self.session = session
        self.objects: dict = {}
        self.gaps: dict = {}
        session.on_fetch_object = (
            lambda obj, _n, _ts, rid: self.objects.setdefault(rid, [])
            .append(obj))
        session.on_fetch_gap = (
            lambda marker, rid: self.gaps.setdefault(rid, []).append(
                (marker.end_of_range, marker.group_id, marker.object_id)))

    async def done(self, request_id) -> list:
        assert await self.session.await_fetch_done(request_id, timeout=5)
        return self.objects.get(request_id, [])


@contextlib.asynccontextmanager
async def _loopback(port, draft, use_quic, prefill=3, history=None,
                    clean=True):
    """The client publishes video and audio under _NS, `prefill` groups
    already published; yields (tracks, rx) with rx the server-side
    fetching session's _Rx."""
    peer = asyncio.get_running_loop().create_future()

    async def on_setup(session, msg):
        _, default = session.CONTROL_REGISTRY[session.negotiated_draft][
            MOQTMessageType.SETUP]
        await default(session, msg)
        if not peer.done():
            peer.set_result(session)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.SETUP, on_setup)
    handle = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as pub:
            await pub.client_session_init()
            assert pub.negotiated_draft == draft
            tracks = {}
            for name, size in _SIZES.items():
                kw = {} if history is None else dict(
                    fetch_history_bytes=history)
                track = PublishedTrack(pub, namespace=_NS, trackname=name,
                                       object_size=size, group_size=_GROUP,
                                       rate=200, **kw)
                if prefill:
                    track.prefill(prefill)
                track.attach()
                tracks[name] = track
            srv = await asyncio.wait_for(peer, 5.0)
            yield tracks, _Rx(srv)
            if clean:
                assert pub._close_err is None, pub._close_err
                assert srv._close_err is None, srv._close_err
    finally:
        handle.close()


async def _refusal(request) -> int:
    with pytest.raises(MOQTRequestError) as err:
        await request
    assert err.value.response is not None, "timed out, no reply"
    return err.value.error_code


# -- standalone FETCH ----------------------------------------------------------

@_TRANSPORTS
@_DRAFTS
async def test_standalone_fetch_two_tracks(draft, use_quic):
    """Two FETCHes in flight on one session, one per track, each returning
    exactly its inclusive range; FETCH_OK reports the inclusive End."""
    async with _loopback(_port(draft - 20, use_quic), draft,
                         use_quic) as (tracks, rx):
        ranges = {"video": ((0, 3), (2, 1)), "audio": ((1, 0), (1, 0))}
        oks = await asyncio.gather(*(
            rx.session.fetch(_NS, name, location_filter=LocationFilter.absolute(
                *start, *end), wait_response=True)
            for name, (start, end) in ranges.items()))
        for ok, (name, (start, end)) in zip(oks, ranges.items()):
            # FetchOk keeps the d14-d18 form: the last Object plus one.
            assert (ok.largest_group_id, ok.largest_object_id) == (
                end[0], end[1] + 1)
            objs = await rx.done(ok.request_id)
            assert _locations(objs) == _range(start, end)
            assert {len(o.payload) for o in objs} == {_SIZES[name]}
        # The d14-d18 arguments: End Object 0 is the whole End Group.
        ok = await rx.session.fetch(_NS, "audio", start_group=1,
                                    start_object=2, end_group=2, end_object=0,
                                    wait_response=True)
        assert (ok.largest_group_id, ok.largest_object_id) == (2, _GROUP)
        assert _locations(await rx.done(ok.request_id)) == _range((1, 2), (2, 4))


@_TRANSPORTS
async def test_relative_unfiltered_and_descending_fetch(use_quic):
    async with _loopback(_port(3, use_quic), 22, use_quic) as (tracks, rx):
        session = rx.session
        # Relative 1: the current group, through Largest Object.
        ok = await session.fetch(_NS, "video",
                                 location_filter=LocationFilter.relative(1),
                                 wait_response=True)
        assert _locations(await rx.done(ok.request_id)) == _range((2, 0), (2, 4))
        # No filter: {0, 0} through Largest Object.
        ok = await session.fetch(_NS, "audio",
                                 location_filter=LocationFilter.none(),
                                 wait_response=True)
        assert _locations(await rx.done(ok.request_id)) == _range((0, 0), (2, 4))
        # Descending: groups high to low, objects ascending in each.
        ok = await session.fetch(_NS, "video",
                                 location_filter=LocationFilter.absolute(0, 2, 1),
                                 group_order=GroupOrder.DESCENDING,
                                 wait_response=True)
        assert (ok.largest_group_id, ok.largest_object_id) == (1, _GROUP)
        assert _locations(await rx.done(ok.request_id)) == (
            _range((1, 0), (1, 4)) + _range((0, 2), (0, 4)))


@_TRANSPORTS
async def test_fetch_refusals(use_quic):
    async with _loopback(_port(4, use_quic), 20, use_quic) as (tracks, rx):
        session = rx.session
        assert await _refusal(session.fetch(
            _NS, "video", location_filter=LocationFilter.absolute(3, 0),
            wait_response=True)) == RequestErrorCode.INVALID_RANGE
        assert await _refusal(session.fetch(
            _NS, "video", location_filter=LocationFilter.absolute(1, 3, 1, 2),
            wait_response=True)) == RequestErrorCode.INVALID_RANGE
        assert await _refusal(session.fetch(
            _NS, "nope", location_filter=LocationFilter.none(),
            wait_response=True)) == RequestErrorCode.NOT_SUPPORTED
        # No MAX_FILTER_RANGES was sent: a Range Filter is refused.
        session._peer_max_filter_ranges = 4
        assert await _refusal(session.fetch(
            _NS, "video", parameters={D20ParamType.OBJECTID_FILTER:
                                      RangeFilter(0, ((0, 1),))},
            wait_response=True)) == D20RequestErrorCode.INVALID_FILTER
        # INCLUDE_PROPERTIES is accepted.
        ok = await session.fetch(_NS, "video",
                                 location_filter=LocationFilter.absolute(0, 0, 0),
                                 parameters={D20ParamType.INCLUDE_PROPERTIES: 0},
                                 wait_response=True)
        assert _locations(await rx.done(ok.request_id)) == _range((0, 0), (0, 4))


@_TRANSPORTS
async def test_fetch_on_an_empty_track_is_refused(use_quic):
    async with _loopback(_port(5, use_quic), 20, use_quic,
                         prefill=0) as (tracks, rx):
        assert await _refusal(rx.session.fetch(
            _NS, "video", location_filter=LocationFilter.none(),
            wait_response=True)) == RequestErrorCode.INVALID_RANGE


# -- joining -------------------------------------------------------------------

@_TRANSPORTS
async def test_join_at_d22(use_quic):
    """join() is one SUBSCRIBE at the Next Object with FILL_PARAMETERS:
    the fill stream, under the SUBSCRIBE's Request ID, ends at the Largest
    Object its SUBSCRIBE_OK reports and live objects follow, each once."""
    async with _loopback(_port(6, use_quic), 22, use_quic) as (tracks, rx):
        session = rx.session
        live = []
        sub_ok, fill = await session.join(
            namespace=_NS, track_name="video", joining_start=1,
            fetch_type=FetchType.RELATIVE_JOINING, wait_response=True)
        assert fill.request_id == sub_ok.request_id
        session.register_object_handler(
            sub_ok.track_alias,
            lambda obj, _s, _t, g, _sg: live.append((g, obj.object_id)))
        assert (sub_ok.largest_group_id, sub_ok.largest_object_id) == (2, 4)
        objs = await rx.done(sub_ok.request_id)
        assert _locations(objs) == _range((1, 0), (2, 4))
        assert {len(o.payload) for o in objs} == {_SIZES["video"]}
        for _ in range(100):
            if live:
                break
            await asyncio.sleep(0.02)
        assert live and min(live) >= (3, 0)
        # joining_fetch() on that subscription: a REQUEST_UPDATE whose
        # fill stream carries its own Request ID; the current group.
        ok = await session.joining_fetch(sub_ok.request_id, joining_start=0,
                                         wait_response=True)
        objs = await rx.done(ok.request_id)
        group = objs[0].group_id
        assert group >= 2
        assert _locations(objs) == [(group, o) for o in range(len(objs))]
        assert tracks["video"].subscriptions[0].fill_streams == 2


@_TRANSPORTS
async def test_join_without_waiting_keeps_the_reply(use_quic):
    """join(wait_response=False) as MediaSubscriber drives it; absolute
    joining starts at its group."""
    async with _loopback(_port(7, use_quic), 20, use_quic) as (tracks, rx):
        session = rx.session
        sub_msg, fetch_msg = await session.join(
            _NS, "audio", fetch_type=FetchType.ABSOLUTE_JOINING,
            joining_start=1)
        assert fetch_msg.request_id == sub_msg.request_id
        sub_ok = await session._await_response(sub_msg.request_id)
        assert sub_ok.request_id == sub_msg.request_id
        objs = await rx.done(sub_msg.request_id)
        assert _locations(objs) == _range((1, 0), (2, 4))
        assert {len(o.payload) for o in objs} == {_SIZES["audio"]}


@_TRANSPORTS
async def test_join_on_an_empty_track_has_an_empty_fill(use_quic):
    """Nothing published: no fill stream opens (d22 §3.4), and the join's
    fill completes, empty, once SUBSCRIBE_OK says so."""
    async with _loopback(_port(8, use_quic), 22, use_quic,
                         prefill=0) as (tracks, rx):
        sub_ok, _ = await rx.session.join(_NS, "video", wait_response=True)
        assert await rx.done(sub_ok.request_id) == []
        assert tracks["video"].subscriptions[0].fill_streams == 0


@_TRANSPORTS
async def test_a_fill_past_the_largest_object_opens_no_stream(use_quic):
    async with _loopback(_port(16, use_quic), 20, use_quic) as (tracks, rx):
        msg = rx.session.subscribe(
            _NS, "audio", location_filter=LocationFilter.next_object(),
            parameters={D20ParamType.FILL_PARAMETERS: {}})
        await rx.session._await_response(msg.request_id)
        assert await rx.done(msg.request_id) == []
        assert tracks["audio"].subscriptions[0].fill_streams == 0


# -- fill fetch streams --------------------------------------------------------

@_TRANSPORTS
@pytest.mark.parametrize("draft", [20, 22])
async def test_fill_fetch_streams_two_tracks(draft, use_quic):
    """SUBSCRIBE at the Next Object with FILL_PARAMETERS: each track's
    fill stream carries the SUBSCRIBE's Request ID and its fill range;
    PUBLISH_DONE counts it."""
    async with _loopback(_port(9 + (draft == 22), use_quic), draft,
                         use_quic) as (tracks, rx):
        session = rx.session
        fills = {"video": LocationFilter.relative(2),
                 "audio": LocationFilter.absolute(0, 3, 1, 1)}
        want = {"video": _range((1, 0), (2, 4)),
                "audio": _range((0, 3), (1, 1))}
        oks = await asyncio.gather(*(
            session.subscribe(
                _NS, name, location_filter=LocationFilter.next_object(),
                parameters={D20ParamType.FILL_PARAMETERS: {
                    ParamType.LOCATION_FILTER: lf}}, wait_response=True)
            for name, lf in fills.items()))
        for name, ok in zip(fills, oks):
            objs = await rx.done(ok.request_id)
            assert _locations(objs) == want[name]
            assert {len(o.payload) for o in objs} == {_SIZES[name]}
        for name, track in tracks.items():
            assert track.subscriptions[0].fill_streams == 1


@_TRANSPORTS
async def test_fill_by_request_update(use_quic):
    """A REQUEST_UPDATE carrying FILL_PARAMETERS opens another fill
    stream under its own Request ID; one without opens none."""
    async with _loopback(_port(11, use_quic), 22, use_quic) as (tracks, rx):
        session = rx.session
        ok = await session.subscribe(
            _NS, "video", location_filter=LocationFilter.next_object(),
            wait_response=True)
        update = session.request_update(ok.request_id, parameters={
            D20ParamType.FILL_PARAMETERS: {
                ParamType.LOCATION_FILTER: LocationFilter.absolute(1, 0, 1),
                ParamType.GROUP_ORDER: GroupOrder.DESCENDING}})
        await session._await_response(update.request_id)
        assert _locations(await rx.done(update.request_id)) == _range(
            (1, 0), (1, 4))
        await session.request_update(ok.request_id,
                                     parameters={ParamType.SUBSCRIBER_PRIORITY: 9},
                                     wait_response=True)
        await asyncio.sleep(0.1)
        assert tracks["video"].subscriptions[0].fill_streams == 1


# -- End of Timed-Out Range ----------------------------------------------------

@_TRANSPORTS
async def test_unavailable_objects_are_timed_out_under_fill_timeout(use_quic):
    """Objects the history no longer holds are unknown; under FILL_TIMEOUT
    they are reported as End of Timed-Out Range (0x20C), on a FETCH and on
    a fill stream."""
    # Room for about two groups of 300-byte objects.
    async with _loopback(_port(12, use_quic), 22, use_quic,
                         history=4000) as (tracks, rx):
        session = rx.session
        lost = tracks["video"]._history.evicted_through
        assert lost is not None and lost < (2, 0)
        everything = LocationFilter.none()
        ok = await session.fetch(_NS, "video", location_filter=everything,
                                 wait_response=True)
        kept = _locations(await rx.done(ok.request_id))
        assert rx.gaps[ok.request_id] == [(0x10C, *lost)]
        ok = await session.fetch(_NS, "video", location_filter=everything,
                                 parameters={D20ParamType.FILL_TIMEOUT: 0},
                                 wait_response=True)
        assert _locations(await rx.done(ok.request_id)) == kept
        assert rx.gaps[ok.request_id] == [(0x20C, *lost)]
        assert kept[0] > lost
        msg = session.subscribe(
            _NS, "video", location_filter=LocationFilter.next_object(),
            parameters={D20ParamType.FILL_PARAMETERS: {
                ParamType.LOCATION_FILTER: everything,
                D20ParamType.FILL_TIMEOUT: 0}})
        await session._await_response(msg.request_id)
        assert _locations(await rx.done(msg.request_id)) == kept
        assert rx.gaps[msg.request_id] == [(0x20C, *lost)]


# -- REQUEST_UPDATE of a FETCH ---------------------------------------------------

@_TRANSPORTS
async def test_fetch_priority_update_is_accepted(use_quic):
    async with _loopback(_port(13, use_quic), 22, use_quic) as (tracks, rx):
        pub = tracks["video"].session
        stalled = asyncio.Event()

        async def _stall(stream_id, data, end_stream=False):
            stalled.set()
            await asyncio.Event().wait()
        pub.stream_write_drain = _stall
        session = rx.session
        ok = await session.fetch(_NS, "video",
                                 location_filter=LocationFilter.none(),
                                 wait_response=True)
        await asyncio.wait_for(stalled.wait(), 5)
        reply = await session.request_update(
            ok.request_id, parameters={ParamType.SUBSCRIBER_PRIORITY: 3},
            wait_response=True)
        assert reply.request_id is not None
        session.stream_stop_sending(session._bidi_streams[ok.request_id], 1)
        assert await session.await_fetch_done(ok.request_id, timeout=5) is False


# -- the agent runtime -----------------------------------------------------------

@_TRANSPORTS
async def test_agent_fetches_two_tracks_at_d22(use_quic):
    async with _loopback(_port(14, use_quic), 22, use_quic) as (tracks, rx):
        agent = AgentSession(rx.session)
        video, audio = await asyncio.gather(
            agent.fetch(FetchSpec(track=TrackRef(_NS, "video"),
                                  start_at=StartAt.range(0, 1))),
            agent.fetch(FetchSpec(track=TrackRef(_NS, "audio"),
                                  start_at=StartAt(mode="range", group=1,
                                                   object=2, end_group=2))))
        got_v = await video.read(100, timeout=5)
        got_a = await audio.read(100, timeout=5)
        assert video.ended == audio.ended == "complete"
        assert [(o.group, o.object_id) for o in got_v] == _range((0, 0), (1, 4))
        assert [(o.group, o.object_id) for o in got_a] == _range((1, 2), (2, 4))
        assert {len(o.payload) for o in got_v} == {_SIZES["video"]}
        assert {len(o.payload) for o in got_a} == {_SIZES["audio"]}
        # Per-request routing: the session-wide handler saw nothing.
        assert rx.objects == {}
        await agent.close()


# -- FETCH_OK End before Start -------------------------------------------------

@_TRANSPORTS
async def test_fetch_ok_ending_before_the_start_closes_the_session(use_quic):
    async with _loopback(_port(15, use_quic), 20, use_quic,
                         clean=False) as (tracks, rx):
        pub = tracks["video"].session

        async def bad_fetch_ok(session, msg):
            session._send_reply(msg.request_id, FetchOk(
                request_id=msg.request_id, largest_group_id=1,
                largest_object_id=1))
        pub.register_handler(MOQTMessageType.FETCH, bad_fetch_ok)
        rx.session.fetch(_NS, "elsewhere",
                         location_filter=LocationFilter.absolute(2, 0))
        for _ in range(100):
            if rx.session._close_err is not None:
                break
            await asyncio.sleep(0.02)
        assert rx.session._close_err[0] == SessionCloseCode.PROTOCOL_VIOLATION
