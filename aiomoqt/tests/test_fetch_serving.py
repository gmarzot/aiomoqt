"""A PublishedTrack answers FETCH from its object history (§10.12):
standalone by name, joining against its subscription, refusing with
INVALID_RANGE or INVALID_JOINING_REQUEST_ID, and reporting LARGEST_OBJECT
in its replies (§10.2.11)."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.delivery import ObjectHistory
from aiomoqt.messages.data import FETCH_FLAGS_END_UNKNOWN
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import (
    FetchType, GroupOrder, MOQTMessageType, MOQTRequestError, ParamType,
    RequestErrorCode, StreamResetCode,
)

from aiomoqt.tests._certs import CERT, KEY, requires_certs


# -- ObjectHistory ------------------------------------------------------

def _filled(groups=3, objects=2, size=10, cap=1 << 20):
    h = ObjectHistory(cap)
    for g in range(groups):
        for o in range(objects):
            h.add(g, o, bytes([g, o]) * (size // 2))
    return h


def _locations(objects):
    return [(o.group_id, o.object_id) for o in objects]


def test_history_serves_groups_in_either_order():
    h = _filled()
    assert _locations(h.fetch((0, 1), (2, 0))) == [(0, 1), (1, 0), (1, 1), (2, 0)]
    assert _locations(h.fetch((0, 0), (2, 1), descending=True)) == [
        (2, 0), (2, 1), (1, 0), (1, 1), (0, 0), (0, 1)]


def test_history_keeps_the_first_record_of_a_location():
    h = ObjectHistory(1 << 20)
    h.add(0, 0, b"first")
    h.add(0, 0, b"second")
    assert [o.payload for o in h.fetch((0, 0), (0, 0))] == [b"first"]


@pytest.mark.parametrize("descending", [False, True])
def test_dropped_objects_are_marked_unknown(descending):
    h = _filled(cap=4 * (10 + 64))           # room for 4 of the 6 objects
    assert len(h) == 4 and h.evicted_through == (0, 1)
    got = h.fetch((0, 0), (2, 1), descending=descending)
    marker = got[-1] if descending else got[0]
    assert marker.end_of_range == FETCH_FLAGS_END_UNKNOWN
    assert (marker.group_id, marker.object_id) == (0, 1)
    assert sorted(_locations(o for o in got if o.end_of_range is None)) == [
        (1, 0), (1, 1), (2, 0), (2, 1)]


def test_history_resolves_extensions_and_marks_datagrams():
    h = ObjectHistory(1 << 20)
    h.add(0, 0, b"x", subgroup_id=None, extensions=lambda s: {6: s})
    obj, = h.fetch((0, 0), (0, 0), "peer")
    assert obj.datagram and obj.extensions == {6: "peer"}


# -- over a real session -------------------------------------------------

_BASE_PORT = 16200
_NS = "fetch/serve"
_SIZES = {"video": 300, "audio": 100}
_GROUP = 5
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _port(case: int, use_quic: bool) -> int:
    return _BASE_PORT + 2 * case + (not use_quic)


@contextlib.asynccontextmanager
async def _loopback(port, use_quic, prefill=3):
    """The client publishes video and audio under _NS, each with `prefill`
    groups already published; yields (tracks, server session, fetched),
    the server being the side that subscribes and fetches. `fetched` maps
    a FETCH request id to the objects it delivered."""
    peer = asyncio.get_running_loop().create_future()
    fetched: dict = {}

    async def _on_publish_namespace(session, msg):
        session.on_fetch_object = (
            lambda obj, _n, _ts, rid: fetched.setdefault(rid, []).append(obj))
        session.publish_namepace_ok(msg)
        if not peer.done():
            peer.set_result(session)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            _on_publish_namespace)
    quic_server = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as pub:
            await pub.client_session_init()
            tracks = {}
            for name, size in _SIZES.items():
                track = PublishedTrack(pub, namespace=_NS, trackname=name,
                                       object_size=size, group_size=_GROUP,
                                       rate=200)
                if prefill:
                    track.prefill(prefill)
                track.attach()
                tracks[name] = track
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            yield tracks, await asyncio.wait_for(peer, 5.0), fetched
    finally:
        quic_server.close()


async def _fetch(rx, fetched, request):
    ok = await request
    assert await rx.await_fetch_done(ok.request_id, timeout=5)
    return ok, fetched.get(ok.request_id, [])


async def _refusal(request) -> int:
    with pytest.raises(MOQTRequestError) as err:
        await request
    assert err.value.response is not None, "timed out, no reply"
    return err.value.error_code


@requires_certs
@_TRANSPORTS
@pytest.mark.parametrize("order", [GroupOrder.ASCENDING, GroupOrder.DESCENDING])
async def test_standalone_fetch_serves_the_track_it_names(use_quic, order):
    async with _loopback(_port(order == GroupOrder.DESCENDING, use_quic),
                         use_quic) as (tracks, rx, fetched):
        for name, size in _SIZES.items():
            # End Location {2, 0}: the whole of group 2.
            ok, objs = await _fetch(rx, fetched, rx.fetch(
                _NS, name, start_group=0, start_object=3, end_group=2,
                end_object=0, group_order=order, wait_response=True))
            assert (ok.largest_group_id, ok.largest_object_id) == (2, _GROUP)
            groups = [0, 1, 2] if order == GroupOrder.ASCENDING else [2, 1, 0]
            want = [(g, o) for g in groups for o in range(_GROUP)
                    if (g, o) >= (0, 3)]
            assert _locations(objs) == want
            assert {len(o.payload) for o in objs} == {size}
        assert rx._close_err is None


@requires_certs
@_TRANSPORTS
async def test_joining_fetch_ends_at_the_joining_location(use_quic):
    async with _loopback(_port(2, use_quic), use_quic) as (tracks, rx, fetched):
        sub_ok, fetch_ok = await rx.join(
            namespace=_NS, track_name="video", joining_start=1,
            fetch_type=FetchType.RELATIVE_JOINING, wait_response=True)
        assert (sub_ok.largest_group_id, sub_ok.largest_object_id) == (2, 4)
        assert (fetch_ok.largest_group_id, fetch_ok.largest_object_id) == (2, 5)
        assert await rx.await_fetch_done(fetch_ok.request_id, timeout=5)
        assert _locations(fetched[fetch_ok.request_id]) == [
            (g, o) for g in (1, 2) for o in range(_GROUP)]
        # Live objects number on from the prefilled groups.
        for _ in range(100):
            if tracks["video"]._largest[0] >= 3:
                break
            await asyncio.sleep(0.02)
        assert tracks["video"]._largest[0] >= 3
        assert tracks["audio"]._largest == (2, 4)


@requires_certs
@_TRANSPORTS
async def test_fetch_refusals(use_quic):
    async with _loopback(_port(3, use_quic), use_quic) as (tracks, rx, _):
        assert await _refusal(rx.fetch(
            _NS, "video", start_group=3, start_object=0, end_group=4,
            end_object=0, wait_response=True)) == RequestErrorCode.INVALID_RANGE
        assert await _refusal(rx.fetch(
            _NS, "nope", start_group=0, start_object=0, end_group=1,
            end_object=0, wait_response=True)) == RequestErrorCode.NOT_SUPPORTED
        # A subscription with Forward State 0 has no Joining Location.
        sub = await rx.subscribe(_NS, "audio", forward=0, wait_response=True)
        assert await _refusal(rx.joining_fetch(
            sub.request_id, joining_start=0, wait_response=True)) \
            == RequestErrorCode.INVALID_RANGE
        assert await _refusal(rx.joining_fetch(
            999, joining_start=0, wait_response=True)) \
            == RequestErrorCode.INVALID_JOINING_REQUEST_ID
        assert rx._close_err is None


@requires_certs
@_TRANSPORTS
async def test_an_empty_track_refuses_fetch(use_quic):
    async with _loopback(_port(4, use_quic), use_quic, prefill=0) as (_, rx, __):
        assert await _refusal(rx.fetch(
            _NS, "video", start_group=0, start_object=0, end_group=1,
            end_object=0, wait_response=True)) == RequestErrorCode.INVALID_RANGE


@requires_certs
@_TRANSPORTS
async def test_replies_carry_the_largest_object(use_quic):
    async with _loopback(_port(5, use_quic), use_quic) as (tracks, rx, _):
        status = await rx.track_status(_NS, "audio", wait_response=True)
        assert status.parameters[ParamType.LARGEST_OBJECT] == (2, 4)
        sub = await rx.subscribe(_NS, "audio", forward=0, wait_response=True)
        update = await rx.request_update(sub.request_id, forward=1,
                                         wait_response=True)
        assert update.parameters[ParamType.LARGEST_OBJECT] == (2, 4)
        # The update set the Joining Location: a joining FETCH ends there.
        ok = await rx.joining_fetch(sub.request_id, joining_start=0,
                                    wait_response=True)
        assert (ok.largest_group_id, ok.largest_object_id) == (2, 5)


async def test_serve_fetch_resets_its_stream_when_cancelled():
    from aiomoqt.context import profile_for
    from aiomoqt.messages.data import FetchObject
    from aiomoqt.protocol import _MOQTSessionMixin

    class _Stub:
        _profile = profile_for(18)
        written, resets, fins = [], [], []

        async def open_uni_stream(self):
            return 9

        def stream_write(self, sid, data, end_stream=False):
            self.written.append(sid)

        async def stream_write_drain(self, sid, data, end_stream=False):
            await asyncio.Event().wait()

        def stream_reset(self, sid, code):
            self.resets.append((sid, code))

        def stream_fin(self, sid):
            self.fins.append(sid)

    stub = _Stub()
    serve = asyncio.ensure_future(_MOQTSessionMixin.serve_fetch(
        stub, 1, [FetchObject(group_id=0, object_id=0, payload=b"x")]))
    await asyncio.sleep(0)
    serve.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serve
    assert stub.resets == [(9, StreamResetCode.CANCELLED)] and stub.fins == []


@requires_certs
@_TRANSPORTS
async def test_a_cancelled_fetch_resets_its_data_stream(use_quic):
    # §5.2: STOP_SENDING on the request stream; the publisher resets both
    # streams. Writes are stalled so the data stream is still open.
    async with _loopback(_port(6, use_quic), use_quic) as (tracks, rx, _):
        pub = tracks["video"].session
        stalled = asyncio.Event()

        async def _stall(stream_id, data, end_stream=False):
            stalled.set()
            await asyncio.Event().wait()
        pub.stream_write_drain = _stall
        resets = []
        reset = pub.stream_reset
        pub.stream_reset = lambda sid, code=0: (resets.append(int(code)),
                                                reset(sid, code))
        ok = await rx.fetch(_NS, "video", start_group=0, start_object=0,
                            end_group=2, end_object=0, wait_response=True)
        await asyncio.wait_for(stalled.wait(), 5)
        rx.stream_stop_sending(rx._bidi_streams[ok.request_id], 1)
        assert await rx.await_fetch_done(ok.request_id, timeout=5) is False
        assert StreamResetCode.CANCELLED in resets
        assert pub._close_err is None and rx._close_err is None
