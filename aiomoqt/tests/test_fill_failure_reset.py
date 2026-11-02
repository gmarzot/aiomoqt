"""A failed d20+ fill resets its stream after the FETCH_HEADER (d22
§3.4.1); the reset keeps the header (RESET_STREAM_AT), so the subscriber
ties it to its request at once rather than at its timeout. Also the
receive side of a reset with a reliable size, and the fallback to
RESET_STREAM when RESET_STREAM_AT is not negotiated. Two tracks per
session, over raw QUIC and WebTransport.
"""
import asyncio
import contextlib
import ssl
import time
from types import SimpleNamespace

import pytest
from aiopquic.quic.configuration import QuicConfiguration

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for, stream_reset_code
from aiomoqt.messages import SubgroupHeader
from aiomoqt.messages.params import D20ParamType, LocationFilter
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.server import MOQTServer
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack
from aiomoqt.types import (
    MOQTMessageType, ObjectStatus, ParamType, StreamResetCode,
    moqt_alpn_for_version,
)

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_PORT = 25400
_NS = "fill/reset"
_SIZES = {"video": 300, "audio": 100}
_GROUP = 5
_FILL = D20ParamType.FILL_PARAMETERS
_FAILED_WITHIN_S = 1.0
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_DRAFTS = pytest.mark.parametrize("draft", [20, 22])


def _port(case, use_quic):
    return _PORT + 2 * case + (not use_quic)


async def _until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


def _fill_subscribe(session, name):
    return session.subscribe(
        _NS, name, location_filter=LocationFilter.next_object(),
        parameters={_FILL: {ParamType.LOCATION_FILTER:
                            LocationFilter.relative(2)}},
        wait_response=True)


async def _fills_fail(session, request_ids, since):
    """Each fill's await_fetch_done() result, and the seconds from
    `since` until all are known."""
    done = await asyncio.gather(*(session.await_fetch_done(rid, timeout=3)
                                  for rid in request_ids))
    return done, time.monotonic() - since


# -- direct publisher ---------------------------------------------------------

@contextlib.asynccontextmanager
async def _direct(port, draft, use_quic):
    """The client publishes two tracks with no FETCH history, three groups
    in; yields (tracks, the server-side subscriber)."""
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
            tracks = {}
            for name, size in _SIZES.items():
                track = PublishedTrack(pub, namespace=_NS, trackname=name,
                                       object_size=size, group_size=_GROUP,
                                       rate=200)
                # Published for a while, but nothing kept to FETCH.
                track._note_largest(2, _GROUP - 1)
                track.attach()
                tracks[name] = track
            srv = await asyncio.wait_for(peer, 5.0)
            yield tracks, srv
            assert pub._close_err is None, pub._close_err
            assert srv._close_err is None, srv._close_err
    finally:
        handle.close()


@requires_certs
@_TRANSPORTS
@_DRAFTS
async def test_failed_fill_is_seen_at_once(draft, use_quic):
    async with _direct(_port(draft == 22, use_quic), draft, use_quic) as (
            tracks, session):
        live = {name: [] for name in _SIZES}
        t0 = time.monotonic()
        oks = await asyncio.gather(*(_fill_subscribe(session, name)
                                     for name in _SIZES))
        for name, ok in zip(_SIZES, oks):
            session.register_object_handler(
                ok.track_alias,
                lambda obj, *_a, name=name: live[name].append(obj))
        done, elapsed = await _fills_fail(
            session, [ok.request_id for ok in oks], t0)
        assert done == [False, False]
        assert elapsed < _FAILED_WITHIN_S, (
            f"fill failure seen after {elapsed:.2f}s")
        for track in tracks.values():
            assert track._history is None
            assert track.subscriptions[0].fill_streams == 1
        def normal(objs):
            return [o for o in objs if o.status == ObjectStatus.NORMAL]
        assert await _until(
            lambda: all(len(normal(v)) >= 3 for v in live.values()))
        for name, objs in live.items():
            assert {len(o.payload) for o in normal(objs)} == {_SIZES[name]}


# -- through the relay ----------------------------------------------------------

def _relay_reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
    relay._ns_subs.clear()


class _MidGroup(PublishedTrack):
    """PUBLISHes objects 3 and 4 of group 2 only, so the relay's cache
    starts mid-group and nothing upstream can FETCH the rest."""

    written = False

    async def generate(self, session, track_alias):
        if self.written:
            return
        self.written = True
        sid = await session.open_uni_stream()
        hdr = SubgroupHeader(track_alias=track_alias, group_id=2,
                             subgroup_id=0, publisher_priority=128,
                             extensions_present=True, prof=session._profile)
        session.stream_write(sid, hdr.serialize().data)
        for oid in (3, 4):
            await session.stream_write_drain(sid, hdr.next_object(
                payload=b"x" * _SIZES[self.trackname], extensions=None,
                object_id=oid).data)


@requires_certs
@_TRANSPORTS
@_DRAFTS
async def test_relay_failed_fill_is_seen_at_once(draft, use_quic):
    port = _port(2 + (draft == 22), use_quic)
    _relay_reset()
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
            pub = await connect()
            for name in _SIZES:
                await _MidGroup(pub, namespace=_NS, trackname=name).publish(
                    publish_track=True, forward=1)
            assert await _until(lambda: all(
                (t := relay._tracks.get(((b"fill", b"reset"), n.encode())))
                is not None and t.largest == (2, 4) for n in _SIZES))
            sub = await connect()
            t0 = time.monotonic()
            oks = await asyncio.gather(*(
                sub.subscribe(_NS, name,
                              location_filter=LocationFilter.next_object(),
                              parameters={_FILL: {ParamType.LOCATION_FILTER:
                                                  LocationFilter.relative(1)}},
                              wait_response=True)
                for name in _SIZES))
            done, elapsed = await _fills_fail(
                sub, [ok.request_id for ok in oks], t0)
            assert done == [False, False]
            assert elapsed < _FAILED_WITHIN_S, (
                f"relayed fill failure seen after {elapsed:.2f}s")
            assert sub._close_err is None
            assert pub._close_err is None
    finally:
        handle.close()
        _relay_reset()


# -- receive side of a reset with a reliable size ------------------------------

_CODE = StreamResetCode.DELIVERY_TIMEOUT
_PARTIAL_PAYLOAD = 400


def _resetting_publisher(record, settle):
    """SUBSCRIBE handler: object 0 and the first half of object 1 on a
    subgroup stream, then a reset keeping all of those bytes. `settle`
    seconds pass before the reset."""
    async def on_subscribe(session, msg):
        ok = session.subscribe_ok(request_msg=msg, content_exists=0)
        await asyncio.sleep(0.3)  # the subscriber binds its handlers
        sid = await session.open_uni_stream()
        hdr = SubgroupHeader(track_alias=ok.track_alias, group_id=0,
                             subgroup_id=0, publisher_priority=128,
                             extensions_present=False, prof=session._profile)
        head = hdr.serialize().data
        first = hdr.next_object(payload=b"x" * 50, extensions=None,
                                object_id=0).data
        second = hdr.next_object(payload=b"y" * _PARTIAL_PAYLOAD,
                                 extensions=None, object_id=1).data
        partial = second[:len(second) - _PARTIAL_PAYLOAD // 2]
        for chunk in (head, first, partial):
            session.stream_write(sid, chunk)
        if settle:
            await asyncio.sleep(settle)
        try:
            session.stream_reset(sid, _CODE, reliable_size=len(head)
                                 + len(first) + len(partial))
        except Exception as e:
            record["errors"].append(e)
        record["negotiated"].append(session._quic.reset_stream_at_negotiated())
    return on_subscribe


async def _subscribe_two_reset_tracks(port, use_quic, settle,
                                      configuration=None):
    record = {"errors": [], "negotiated": []}
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=22)
    server.register_handler(MOQTMessageType.SUBSCRIBE,
                            _resetting_publisher(record, settle))
    handle = await server.serve()
    names = ("a", "b")
    objects = {n: [] for n in names}
    ended = {n: [] for n in names}
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=22,
                            configuration=configuration)
        async with client.connect() as session:
            await session.client_session_init()
            oks = await asyncio.gather(*(session.subscribe(
                _NS, n, wait_response=True) for n in names))
            for n, ok in zip(names, oks):
                session.register_object_handler(
                    ok.track_alias,
                    lambda obj, *_a, n=n: objects[n].append(obj))
                session.register_stream_end_handler(
                    ok.track_alias,
                    lambda g, sg, clean, reset_code, n=n: ended[n].append(
                        (clean, reset_code)))
            assert await _until(lambda: all(ended.values()))
            await asyncio.sleep(0.2)  # a short object would arrive by now
            record["local_negotiated"] = (
                session._quic.reset_stream_at_negotiated())
            record["code"] = stream_reset_code(_CODE, session._profile)
            record["leftover"] = (dict(session._data_streams),
                                  session._rx_held_bytes,
                                  dict(session._uni_peek_stash))
            assert session._close_err is None
    finally:
        handle.close()
    return record, objects, ended


@requires_certs
@_TRANSPORTS
async def test_reliable_size_delivers_the_whole_objects(use_quic):
    """Reset right after the writes: the kept bytes still arrive, the
    whole object is delivered, the cut one is dropped, and the stream
    ends as a reset with nothing left behind."""
    record, objects, ended = await _subscribe_two_reset_tracks(
        _port(4, use_quic), use_quic, settle=0)
    assert not record["errors"]
    assert record["negotiated"] == [True, True]
    assert record["local_negotiated"]
    for name in objects:
        assert [(o.object_id, len(o.payload)) for o in objects[name]] == [
            (0, 50)], f"track {name}: {objects[name]}"
        assert ended[name] == [(False, record["code"])]
    assert record["leftover"] == ({}, 0, {})


@requires_certs
async def test_reliable_size_falls_back_to_a_plain_reset():
    """Without RESET_STREAM_AT the reset is RESET_STREAM: it does not
    raise and the stream still ends as a reset. Bytes not yet delivered
    may be lost, the header among them, so the writes settle first."""
    cfg = QuicConfiguration(
        alpn_protocols=[moqt_alpn_for_version(22)], is_client=True,
        max_data=2**24, max_stream_data=2**24, server_name="localhost",
        reset_stream_at=False)
    cfg.verify_mode = ssl.CERT_NONE
    record, objects, ended = await _subscribe_two_reset_tracks(
        _port(5, True), True, settle=0.3, configuration=cfg)
    assert not record["errors"]
    assert record["negotiated"] == [False, False]
    assert not record["local_negotiated"]
    for name in objects:
        assert [o.object_id for o in objects[name]] == [0]
        assert ended[name] == [(False, record["code"])]
    assert record["leftover"] == ({}, 0, {})


# -- a deferred reset keeps its reliable size -------------------------------------

class _FullOnceQuic:
    """Refuses the first op as a full TX ring, then records each."""

    closed = False

    def __init__(self):
        self.full = True
        self.ops = []

    def reset_stream(self, sid, code, **kw):
        if self.full:
            self.full = False
            raise BufferError("TX ring full")
        self.ops.append((sid, int(code), kw))


@pytest.mark.parametrize("reliable_size", [0, 7])
async def test_deferred_reset_keeps_its_reliable_size(reliable_size):
    s = object.__new__(_MOQTSessionMixin)
    s._close_err = None
    s._session = SimpleNamespace(tx_max_inflight_bytes=None)
    s._profile = profile_for(22)
    s._quic = _FullOnceQuic()
    s.stream_reset(3, _CODE, reliable_size=reliable_size)
    assert s._quic.ops == []
    assert await _until(lambda: s._quic.ops, timeout=1.0)
    kw = {"reliable_size": reliable_size} if reliable_size else {}
    assert s._quic.ops == [(3, stream_reset_code(_CODE, s._profile), kw)]
