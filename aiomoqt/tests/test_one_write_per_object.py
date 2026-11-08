"""A group's last object, its END_OF_GROUP marker and the FIN leave in one
write: a one-object group costs one stream write per peer, at the
publisher and at the relay."""
import asyncio
import contextlib
from collections import defaultdict
from types import SimpleNamespace

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.delivery import FanoutDelivery, StreamMapping, SubgroupDelivery
from aiomoqt.messages import SubgroupHeader
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tools.moq_interop_relay import _RelayedTrack
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import ObjectStatus

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 31700
_NS = ("one", "write")


class _Peer:
    """A session that records each stream write as (bytes, fin)."""

    def __init__(self, nowait=False):
        self._profile = profile_for(18)
        self._next = 3
        self.streams = defaultdict(list)
        self.resets = []
        self.gate = asyncio.Event()
        self.gate.set()
        if nowait:
            self.stream_write_nowait = self._nowait

    def set_stream_priority(self, _sid, _band):
        return True

    async def open_uni_stream(self, initial=b"", priority=None,
                              end_stream=False):
        sid = self._next
        self._next += 4
        self.streams[sid].append((bytes(initial), end_stream))
        return sid

    async def stream_write_drain(self, sid, data, end_stream=False):
        await self.gate.wait()
        self.streams[sid].append((bytes(data), end_stream))

    def stream_write(self, sid, data, end_stream=False):
        self.streams[sid].append((bytes(data), end_stream))

    def _nowait(self, sid, data, end_stream=False):
        self.streams[sid].append((bytes(data), end_stream))
        return True

    def stream_reset(self, sid, code):
        self.resets.append(sid)

    def subscribe_done(self, *_a, **_kw):
        pass


def _header():
    return SubgroupHeader(track_alias=1, group_id=0, subgroup_id=0,
                          publisher_priority=128, extensions_present=True,
                          prof=profile_for(18), first_object=True)


def _objects(hdr, payloads, end=True):
    data = hdr.serialize().data + b"".join(
        hdr.next_object_bytes(p, None, object_id=i)
        for i, p in enumerate(payloads))
    return data + hdr.end_group(object_id=len(payloads)).data if end else data


async def _settle(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


# -- publisher: SubgroupDelivery --------------------------------------

@pytest.mark.parametrize("n", [1, 3])
async def test_end_of_group_closes_the_stream_in_the_last_write(n):
    peer = _Peer()
    d = SubgroupDelivery(peer, 1)
    for oid in range(n):
        await d.write(0, oid, b"p%d" % oid, group_start=oid == 0,
                      end_of_group=oid == n - 1)
    (writes,) = peer.streams.values()
    assert [f for _, f in writes] == [False] * (n - 1) + [True]
    assert b"".join(b for b, _ in writes) == _objects(
        _header(), [b"p%d" % i for i in range(n)])
    assert d.open_group is None
    d.end_group()
    assert len(writes) == n


async def test_lane_merges_an_object_with_the_end_group_behind_it():
    peer = _Peer()
    fan = FanoutDelivery([SubgroupDelivery(peer, 1)])
    peer.gate.clear()
    for oid, payload in enumerate((b"a", b"b", b"c")):
        await fan.write(0, oid, payload, group_start=oid == 0)
    fan.end_group()
    (lane,) = fan.lanes
    assert lane.busy and len(lane.items) == 2
    peer.gate.set()
    await _settle()
    assert lane.done == 4 and lane.idle
    await fan.close()
    (writes,) = peer.streams.values()
    assert [f for _, f in writes] == [False, False, True]
    assert b"".join(b for b, _ in writes) == _objects(
        _header(), [b"a", b"b", b"c"])


async def test_write_through_carries_the_end_of_group():
    peer = _Peer(nowait=True)
    fan = FanoutDelivery([SubgroupDelivery(peer, 1)])
    await fan.write(0, 0, b"a", group_start=True)
    await _settle()
    await fan.write(0, 1, b"b", end_of_group=True)
    (writes,) = peer.streams.values()
    assert [f for _, f in writes] == [False, True]
    assert b"".join(b for b, _ in writes) == _objects(_header(), [b"a", b"b"])
    await fan.close()


async def test_per_object_stream_opens_with_its_fin():
    peer = _Peer()
    d = SubgroupDelivery(peer, 1, mapping=StreamMapping.PER_OBJECT)
    await d.write(0, 0, b"a", group_start=True)
    await d.write(0, 1, b"b")
    assert [[f for _, f in w] for w in peer.streams.values()] == [[True]] * 2


# -- relay -----------------------------------------------------------

class _Obj(SimpleNamespace):
    def __init__(self, object_id, status=None, payload=b"x" * 8):
        super().__init__(group_id=0, object_id=object_id, payload=payload,
                         extensions=None, publisher_priority=128,
                         status=status, stream_flags=None)


def _relayed():
    track = _RelayedTrack(("ns", "t"))
    peer = _Peer()
    track.add_downstream(peer, 7, 1)
    return track, peer


async def test_relay_forwards_an_object_and_its_fin_in_one_write():
    track, peer = _relayed()
    try:
        track.on_object(_Obj(0), 0, 0, 0, 0)
        track.on_stream_end(0, 0)
        await _settle()
    finally:
        track.task.cancel()
    assert [[f for _, f in w] for w in peer.streams.values()] == [[True]]


async def test_relay_forwards_the_marker_with_the_last_object():
    track, peer = _relayed()
    try:
        track.on_object(_Obj(0), 0, 0, 0, 0)
        await _settle()
        track.on_object(_Obj(1), 0, 0, 0, 0)
        track.on_object(_Obj(2, ObjectStatus.END_OF_GROUP, b""), 0, 0, 0, 0)
        track.on_stream_end(0, 0)
        await _settle()
    finally:
        track.task.cancel()
    (writes,) = peer.streams.values()
    assert [f for _, f in writes] == [False, True]
    marker = writes[1][0]
    assert marker.endswith(SubgroupHeader(
        track_alias=7, group_id=0, subgroup_id=0, publisher_priority=128,
        extensions_present=True, prof=profile_for(18)).end_group(
            object_id=2).data[-2:])
    assert track._objects_out == 3


async def test_relay_sends_a_late_end_alone():
    track, peer = _relayed()
    try:
        track.on_object(_Obj(0), 0, 0, 0, 0)
        await _settle()
        track.on_stream_end(0, 0)
        await _settle()
    finally:
        track.task.cancel()
    (writes,) = peer.streams.values()
    assert writes[1] == (b"", True) and [f for _, f in writes] == [False, True]


async def test_relay_resets_a_short_stream_instead_of_merging_its_fin():
    track, peer = _relayed()
    track.downstream[0].lost = {0}
    try:
        track.on_object(_Obj(0), 0, 0, 0, 0)
        track.on_stream_end(0, 0)
        await _settle()
    finally:
        track.task.cancel()
    (sid, writes), = peer.streams.items()
    assert not any(f for _, f in writes) and peer.resets == [sid]


# -- transport -------------------------------------------------------

class _Quic:
    closed = False

    def __init__(self, cap):
        self._configuration = SimpleNamespace(stream_ring_cap=cap)
        self.writes = []

    def get_next_available_stream_id(self, is_unidirectional=True):
        return 3

    async def send_stream_data_drained(self, sid, data, end_stream=False):
        assert len(data) <= self._configuration.stream_ring_cap
        self.writes.append((sid, bytes(data), end_stream))

    def tx_data_ring_used(self, sid):
        return 0


@pytest.mark.parametrize("size", [100, 2500])
async def test_an_open_with_its_fin_splits_past_the_ring(size):
    s = object.__new__(_MOQTSessionMixin)
    s._close_err = None
    s._is_wt = False
    s._session = SimpleNamespace(tx_max_inflight_bytes=None)
    s._quic = _Quic(1000)
    data = bytes(range(250)) * (size // 250)
    assert await s.open_uni_stream(initial=data, end_stream=True) == 3
    writes = s._quic.writes
    assert b"".join(d for _, d, _ in writes) == data
    assert [f for *_, f in writes] == [False] * (len(writes) - 1) + [True]
    assert len(writes) == (1 if size <= 1000 else 5)


# -- loopback through the relay ----------------------------------------

def _count_writes(session):
    """Stream writes per stream id as they reach the transport, the FIN
    with them, and which streams opened after this call."""
    t = session if session._quic is session else session._quic
    writes, fins, opened = defaultdict(int), set(), set()
    send = t.send_stream_data

    def send_stream_data(sid, data, end_stream=False):
        send(sid, data, end_stream)
        writes[sid] += 1
        if end_stream:
            fins.add(sid)
    t.send_stream_data = send_stream_data
    if session._is_wt:
        create = t.create_stream

        async def create_stream(*a, initial=b"", end_stream=False, **kw):
            sid = await create(*a, initial=initial, end_stream=end_stream,
                               **kw)
            opened.add(sid)
            if initial or end_stream:
                writes[sid] += 1
            if end_stream:
                fins.add(sid)
            return sid
        t.create_stream = create_stream
    else:
        next_id = t.get_next_available_stream_id

        def get_next_available_stream_id(is_unidirectional=False):
            sid = next_id(is_unidirectional=is_unidirectional)
            opened.add(sid)
            return sid
        t.get_next_available_stream_id = get_next_available_stream_id
    return lambda: sorted(writes[s] for s in fins & opened)


@pytest.fixture
def relay_tables():
    tables = (relay._announced, relay._announce_requests, relay._tracks,
              relay._sessions, relay._catalogs, relay._ns_subs,
              relay._track_subs)
    for t in tables:
        t.clear()
    yield
    for t in tables:
        t.clear()


@requires_certs
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_two_tracks_one_write_per_object_through_the_relay(
        use_quic, draft, relay_tables):
    port = _BASE_PORT + 2 * (draft == 22) + (not use_quic)
    group = 5
    got = defaultdict(set)
    ends = {}
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                s = await stack.enter_async_context(MOQTClient(
                    "localhost", port, path="/", use_quic=use_quic,
                    verify_tls=False, supported_drafts=draft).connect())
                await s.client_session_init()
                return s
            pub = await connect()
            pub_writes = _count_writes(pub)
            for name, size, gsize, rate in (("audio", 200, 1, 50),
                                            ("video", 3000, group, 30)):
                PublishedTrack(pub, namespace=_NS, trackname=name,
                               object_size=size, group_size=gsize,
                               rate=rate).attach()
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            sub = await connect()
            relay_writes = None
            for name in ("audio", "video"):
                def on_object(msg, size, ts, gid, sgid, name=name):
                    if msg.payload:
                        got[(name, gid)].add(msg.object_id)
                track = SubscribedTrack(sub, _NS, name, on_object=on_object)
                await track.subscribe(timeout=5)
                sub.register_stream_end_handler(
                    track.track_alias,
                    lambda gid, sgid, clean=True, reset_code=0, name=name:
                    ends.__setitem__((name, gid), clean))
                if relay_writes is None:
                    (d,) = relay._tracks[(relay._ns_tuple(_NS),
                                          name.encode())].downstream
                    relay_writes = _count_writes(d.session)
            await asyncio.sleep(1.5)
            at_pub, at_relay = pub_writes(), relay_writes()
    finally:
        handle.close()

    for counts in (at_pub, at_relay):
        assert counts.count(1) >= 20 and counts.count(group) >= 3, counts
        assert set(counts) <= {1, group}, counts
    assert ends and all(ends.values()), ends
    for (name, gid), clean in ends.items():
        assert got[(name, gid)] == set(range(1 if name == "audio" else group))
