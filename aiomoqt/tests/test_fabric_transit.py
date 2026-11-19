"""FabricNode(transit=True): node re-advertisement and routes, flooding,
SNS mapping, raw forwarding with the SNS id rewritten, the stash, and
make-before-break path moves. Sans-I/O against in-memory peers, then
lines, a diamond and a triangle over localhost raw QUIC. Every data case
asserts objects arrived and none twice."""
import asyncio
from collections import Counter

import pytest
from aiopquic.quic.events import (DatagramFrameReceived, StreamDataReceived,
                                  StreamReset)

from aiomoqt.messages import ObjectStatus
from aiomoqt.fabric import FabricHop, FabricNode, HopRetiring, track_hashes
from aiomoqt.fabric import transit as transit_mod
from aiomoqt.fabric.inner import SubgroupWriter, encode_datagram
from aiomoqt.fabric.node import _PeerProtocol
from aiomoqt.fabric.wire import (DataHeader, DataType, MsgType, NodeInfo,
                                  NodeSet, PathItem, rewrite_sns)
from aiomoqt.tests.test_fabric_edge import _ann, _remote, _sub, _Wire

from aiomoqt.tests._certs import CERT, KEY, requires_certs

NS = (b"t", b"live")
_BASE_PORT = 33620
TIMEOUT = 10
A, B, C, X, P = (0x0a0a0a0a0a0a0a0a, 0x0b0b0b0b0b0b0b0b, 0x0c0c0c0c0c0c0c0c,
                 0x0d0d0d0d0d0d0d0d, 0x0e0e0e0e0e0e0e0e)


class _Sink:
    """Records every object a pulled track delivers."""

    def __init__(self):
        self.objects = []
        self.ends = Counter()

    def on_object(self, where, obj):
        if obj.status == ObjectStatus.NORMAL:
            self.objects.append((obj.group_id,
                                 getattr(obj, "subgroup_id", None),
                                 obj.object_id))

    def on_stream_end(self, session_id, stream_id, how):
        self.ends[how] += 1

    @property
    def dups(self):
        return sum(n - 1 for n in Counter(self.objects).values() if n > 1)


def _transit(label="T", metric=100):
    return FabricNode(label=label, transit=True, metric=metric)


def _sent(wire, msg_type):
    return wire.remote.rx_msgs[msg_type.name]


def _stream(sns_id, h, gid, n, sgid=0, size=10):
    """(header chunk, object chunks, END_OF_GROUP chunk) of a subgroup."""
    w = SubgroupWriter(h, gid, sgid)
    head = DataHeader(DataType.NEW_STREAM, sns_id, h, 128, 5000).encode()
    objs = [w.object(o, bytes([o]) * size) for o in range(n)]
    return head + w.header, objs, w.status(n, ObjectStatus.END_OF_GROUP)


def _chunk(node, wire, stream_id, data, fin=False):
    node._on_stream_data(wire.proto, StreamDataReceived(
        stream_id=stream_id, data=data, end_stream=fin))


# -- nodes ---------------------------------------------------------------

def test_transit_defaults_and_leaf_unchanged():
    t = _transit()
    assert t.transit is not None and t.local.metric == 100
    leaf = FabricNode(label="leaf")
    assert leaf.transit is None and leaf.local.metric == 1 << 32


def test_nodes_readvertised_with_path_and_withdrawn_when_unreachable():
    t = _transit()
    wa = _Wire(t, 1, _remote(A, "a"))
    wb = _Wire(t, 2, _remote(B, "b"))
    # The session that came up later learns the earlier peer, path [T].
    rec = wb.remote.nodes[A].body
    assert rec.path == (PathItem(t.node_id, 0, 100),)
    assert A not in wa.remote.nodes or wa.remote.nodes[A].body.path == ()
    assert wa.remote.nodes[B].body.path == (PathItem(t.node_id, 0, 100),)
    wc = _Wire(t, 3, _remote(C, "c"))
    # X: two hops via A, three via C.
    wa.send(MsgType.NODE_INFO_ADV, NodeInfo(
        X, contact="x", path=(PathItem(A, 5, 100),)))
    wc.send(MsgType.NODE_INFO_ADV, NodeInfo(
        X, contact="x", path=(PathItem(P, 5, 100), PathItem(C, 5, 100))))
    assert t.transit.best[X] == 1
    assert [p.node_id for p in wb.remote.nodes[X].body.path] == [
        A, t.node_id]
    # The peer on the path does not get it back.
    assert X not in wa.remote.nodes
    wd_before = _sent(wb, MsgType.NODE_INFO_WD)
    wa.send(MsgType.NODE_INFO_WD, NodeInfo(X, contact="x"))
    assert t.transit.best[X] == 3
    assert _sent(wb, MsgType.NODE_INFO_WD) == wd_before
    assert [p.node_id for p in wb.remote.nodes[X].body.path] == [
        P, C, t.node_id]
    wc.send(MsgType.NODE_INFO_WD, NodeInfo(X, contact="x"))
    assert X not in t.transit.best
    assert X not in wb.remote.nodes
    # A path through this node is a loop: never a route.
    wa.send(MsgType.NODE_INFO_ADV, NodeInfo(
        X, contact="x", path=(PathItem(t.node_id, 1, 1),)))
    assert X not in t.transit.best


def test_best_path_rank_and_session_metric():
    t = _transit()
    wa = _Wire(t, 1, _remote(A, "a"))
    wc = _Wire(t, 2, _remote(C, "c"))
    wb = _Wire(t, 3, _remote(B, "b"))
    hop = (PathItem(P, 50, 100),)
    wa.send(MsgType.NODE_INFO_ADV, NodeInfo(X, metric=100, path=hop))
    wc.send(MsgType.NODE_INFO_ADV, NodeInfo(X, metric=100, path=hop))
    assert t.transit.best[X] == 1        # full tie: lowest session id
    wc.send(MsgType.NODE_INFO_ADV, NodeInfo(X, metric=100,
                                            path=(PathItem(P, 1, 100),)))
    assert t.transit.best[X] == 2        # lower srtt sum
    wa.send(MsgType.NODE_INFO_ADV, NodeInfo(X, metric=100, path=()))
    assert t.transit.best[X] == 1        # fewer hops
    t.set_session_metric(1, 500)
    assert t.transit.best[X] == 2        # the session metric adds cost
    assert wa.remote.nodes[t.node_id].body.metric == 500
    assert [p.metric for p in wb.remote.nodes[X].body.path][-1] == 100


def test_subscribes_and_announces_flood_once():
    t = _transit()
    wa = _Wire(t, 1, _remote(A, "a"))
    wb = _Wire(t, 2, _remote(B, "b"))
    wc = _Wire(t, 3, _remote(C, "c"))
    sub = _sub(A, NS, b"video")
    wa.send(MsgType.SUBSCRIBE_INFO_ADV, sub)
    assert _sent(wb, MsgType.SUBSCRIBE_INFO_ADV) == 1
    assert _sent(wc, MsgType.SUBSCRIBE_INFO_ADV) == 1
    assert _sent(wa, MsgType.SUBSCRIBE_INFO_ADV) == 0
    # The same advertisement on another path is not flooded again.
    wb.send(MsgType.SUBSCRIBE_INFO_ADV, sub)
    assert _sent(wa, MsgType.SUBSCRIBE_INFO_ADV) == 0
    assert _sent(wc, MsgType.SUBSCRIBE_INFO_ADV) == 1
    wa.send(MsgType.SUBSCRIBE_INFO_ADV, _sub(A, NS, b"video", seq=3))
    wb.send(MsgType.SUBSCRIBE_INFO_ADV, _sub(A, NS, b"video", seq=2))
    assert _sent(wc, MsgType.SUBSCRIBE_INFO_ADV) == 2   # stale 2 dropped
    wa.send(MsgType.SUBSCRIBE_INFO_WD, _sub(A, NS, b"video", seq=4))
    assert _sent(wc, MsgType.SUBSCRIBE_INFO_WD) == 1
    assert not t.transit.subscribes

    ann = _ann(A, NS, b"video")
    wa.send(MsgType.ANNOUNCE_INFO_ADV, ann)
    wb.send(MsgType.ANNOUNCE_INFO_ADV, ann)
    assert _sent(wc, MsgType.ANNOUNCE_INFO_ADV) == 1
    assert _sent(wa, MsgType.ANNOUNCE_INFO_ADV) == 0
    # A late peer gets what is held, but not its own.
    wd = _Wire(t, 4, _remote(A, "a2"))
    assert _sent(wd, MsgType.ANNOUNCE_INFO_ADV) == 0
    we = _Wire(t, 5, _remote(0x0f0f0f0f0f0f0f0f, "e"))
    assert _sent(we, MsgType.ANNOUNCE_INFO_ADV) == 1
    wa.send(MsgType.ANNOUNCE_INFO_WD, ann)
    assert _sent(wc, MsgType.ANNOUNCE_INFO_WD) == 1
    assert wd and not t.transit.announces


def test_source_unreachable_withdraws_its_subscribes_and_announces():
    t = _transit()
    wa = _Wire(t, 1, _remote(A, "a"))
    wb = _Wire(t, 2, _remote(B, "b"))
    wa.send(MsgType.SUBSCRIBE_INFO_ADV, _sub(A, NS, b"video"))
    wa.send(MsgType.ANNOUNCE_INFO_ADV, _ann(A, NS))
    wa.close()
    assert _sent(wb, MsgType.NODE_INFO_WD) == 1
    assert _sent(wb, MsgType.SUBSCRIBE_INFO_WD) == 1
    assert _sent(wb, MsgType.ANNOUNCE_INFO_WD) == 1
    assert not wb.remote.subscribes and not wb.remote.announces


# -- data ----------------------------------------------------------------

def _line(*downstream):
    """T with an upstream P on session 1 and `downstream` peers after."""
    t = _transit()
    wp = _Wire(t, 1, _remote(P, "p"))
    wires = [_Wire(t, i + 2, _remote(n, f"d{i}"))
             for i, n in enumerate(downstream)]
    return t, wp, wires


def test_sns_maps_per_next_hop_and_bytes_differ_only_in_sns_id():
    t, wp, (wb, wc) = _line(B, C)
    h = track_hashes(NS, b"video").fullname
    sink = _Sink()
    t.pull(NS, b"video", sink, advertise=False)
    wp.send(MsgType.SNS_ADV, NodeSet(7, 128, (B, C, t.node_id)))
    (ing,) = t.transit.ingress.values()
    assert ing.local and set(ing.egress) == {2, 3}
    assert wb.remote.sns_in and wc.remote.sns_in
    b_sns = next(iter(wb.remote.sns_in.values())).body
    assert b_sns.nodes == (B,) and b_sns.sns_id == 1
    head, objs, eog = _stream(7, h, 0, 4)
    wire_in = head + b"".join(objs) + eog
    _chunk(t, wp, 3, head[:5])
    _chunk(t, wp, 3, head[5:] + objs[0])
    for o in objs[1:]:
        _chunk(t, wp, 3, o)
    _chunk(t, wp, 3, eog, fin=True)
    (sb,) = wb.quic.data
    (sc,) = wc.quic.data
    assert bytes(wb.quic.data[sb]) == rewrite_sns(wire_in, 1)
    assert bytes(wc.quic.data[sc]) == rewrite_sns(wire_in, 1)
    assert {sb} == wb.quic.fins and {sc} == wc.quic.fins
    assert sink.objects == [(0, 0, o) for o in range(4)] and not sink.dups
    # A reset is carried as a reset.
    head2, objs2, _ = _stream(7, h, 1, 2)
    _chunk(t, wp, 7, head2 + objs2[0])
    t._on_stream_reset(wp.proto, StreamReset(stream_id=7, error_code=5))
    assert set(wb.quic.resets.values()) == {5}
    # Datagrams: header id rewritten, payload copied, decoded locally once.
    dg = DataHeader(DataType.DATAGRAM, 7, h).encode() + encode_datagram(
        h, 9, 0, b"dd")
    t._on_datagram(wp.proto, DatagramFrameReceived(data=dg))
    assert wb.quic.datagrams == [rewrite_sns(dg, 1)]
    assert wc.quic.datagrams == [rewrite_sns(dg, 1)]
    assert (9, None, 0) in sink.objects and not sink.dups
    # Withdraw: the egress sets go too.
    wp.send(MsgType.SNS_WD, NodeSet(7))
    assert not t.transit.ingress
    assert not wb.remote.sns_in and not wc.remote.sns_in


def test_sns_update_keeps_the_remaining_node_whole():
    t, wp, (wb, wc) = _line(B, C)
    h = track_hashes(NS, b"video").fullname
    wp.send(MsgType.SNS_ADV, NodeSet(7, 128, (B, C)))
    head, objs, eog = _stream(7, h, 0, 6)
    _chunk(t, wp, 3, head + objs[0])
    _chunk(t, wp, 3, objs[1])
    wp.send(MsgType.SNS_ADV, NodeSet(7, 128, (B,)))
    # C's set drains: the stream in flight finishes there, then SNS_WD.
    assert _sent(wc, MsgType.SNS_WD) == 0
    for o in objs[2:]:
        _chunk(t, wp, 3, o)
    _chunk(t, wp, 3, eog, fin=True)
    assert _sent(wc, MsgType.SNS_WD) == 1
    head2, objs2, eog2 = _stream(7, h, 1, 3)
    _chunk(t, wp, 11, head2 + b"".join(objs2) + eog2, fin=True)
    want = rewrite_sns(head + b"".join(objs) + eog, 1)
    b_streams = [bytes(v) for v in wb.quic.data.values()]
    assert b_streams[0] == want
    assert b_streams[1] == rewrite_sns(head2 + b"".join(objs2) + eog2, 1)
    assert [bytes(v) for v in wc.quic.data.values()] == [want]
    assert len(wb.quic.fins) == 2 and len(wc.quic.fins) == 1


def test_route_change_moves_new_streams_and_finishes_old_ones():
    t, wp, (wx, wy) = _line(A, C)
    h = track_hashes(NS, b"video").fullname
    for w, metric in ((wx, 100), (wy, 200)):
        w.send(MsgType.NODE_INFO_ADV, NodeInfo(
            B, contact="b", path=(PathItem(w.remote.local.node_id, 1,
                                           metric),)))
    wp.send(MsgType.SNS_ADV, NodeSet(4, 128, (B,)))
    (ing,) = t.transit.ingress.values()
    assert set(ing.egress) == {2}
    head, objs, eog = _stream(4, h, 0, 3)
    _chunk(t, wp, 3, head + objs[0])
    wx.send(MsgType.NODE_INFO_WD, NodeInfo(B, contact="b"))
    assert set(ing.egress) == {2, 3} and ing.egress[2].drain
    head2, objs2, eog2 = _stream(4, h, 1, 2)
    _chunk(t, wp, 7, head2 + b"".join(objs2) + eog2, fin=True)
    for o in objs[1:]:
        _chunk(t, wp, 3, o)
    _chunk(t, wp, 3, eog, fin=True)
    assert [bytes(v) for v in wx.quic.data.values()] == [
        rewrite_sns(head + b"".join(objs) + eog, 1)]
    assert [bytes(v) for v in wy.quic.data.values()] == [
        rewrite_sns(head2 + b"".join(objs2) + eog2, 1)]
    assert set(ing.egress) == {3} and _sent(wx, MsgType.SNS_WD) == 1


def test_stash_replays_data_before_its_sns_and_is_bounded(monkeypatch):
    t, wp, (wb,) = _line(B)
    h = track_hashes(NS, b"video").fullname
    head, objs, eog = _stream(9, h, 0, 3)
    _chunk(t, wp, 3, head + objs[0])
    _chunk(t, wp, 3, objs[1])
    dg = DataHeader(DataType.DATAGRAM, 9, h).encode() + encode_datagram(
        h, 1, 0, b"x")
    t._on_datagram(wp.proto, DatagramFrameReceived(data=dg))
    assert not wb.quic.data and not wb.quic.datagrams
    wp.send(MsgType.SNS_ADV, NodeSet(9, 128, (B,)))
    _chunk(t, wp, 3, objs[2] + eog, fin=True)
    (data,) = wb.quic.data.values()
    assert bytes(data) == rewrite_sns(head + b"".join(objs) + eog, 1)
    assert wb.quic.datagrams == [rewrite_sns(dg, 1)]
    assert t.transit.stash_dropped == 0
    assert not t.transit._stash_bytes[1]

    monkeypatch.setattr(transit_mod, "STASH_MAX_BYTES", 100)
    head, objs, eog = _stream(10, h, 0, 3, size=60)
    _chunk(t, wp, 7, head + objs[0])
    _chunk(t, wp, 7, objs[1])           # over the bound: dropped
    _chunk(t, wp, 7, objs[2] + eog, fin=True)
    assert t.transit.stash_dropped == 1
    wp.send(MsgType.SNS_ADV, NodeSet(10, 128, (B,)))
    assert len(wb.quic.data) == 1


@pytest.mark.asyncio
async def test_origin_finishes_open_subgroups_on_the_old_session():
    pub = FabricNode(label="pub")
    w1 = _Wire(pub, 1, _remote(A, "t1"))
    w2 = _Wire(pub, 2, _remote(C, "t2"))
    for w, metric in ((w1, 100), (w2, 200)):
        w.send(MsgType.NODE_INFO_ADV, NodeInfo(
            B, contact="b", path=(PathItem(w.remote.local.node_id, 1,
                                           metric),)))
        w.send(MsgType.SUBSCRIBE_INFO_ADV, _sub(B, NS, b"video"))
    track = pub.publish(NS, b"video")
    assert set(track.origin.sns) == {1}
    await track.write(0, 0, 0, b"a")
    w1.send(MsgType.NODE_INFO_ADV, NodeInfo(
        B, contact="b", path=(PathItem(A, 1, 900),)))
    assert set(track.origin.sns) == {2}
    assert _sent(w1, MsgType.SNS_WD) == 0
    await track.write(0, 0, 1, b"b")
    await track.write(1, 0, 0, b"c")
    await track.end_subgroup(0, 0, end_of_group=2)
    assert _sent(w1, MsgType.SNS_WD) == 1
    (s1,) = w1.quic.data
    assert s1 in w1.quic.fins and len(w2.quic.data) == 1


# -- over localhost QUIC ---------------------------------------------------

class _Recording(_PeerProtocol):
    """Records the data stream bytes a node sends and receives."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.tx = {}
        self.rx = {}

    def send_nowait(self, stream_id, data, fin=False):
        ok = super().send_nowait(stream_id, data, fin)
        if ok:
            self.tx.setdefault(stream_id, bytearray()).extend(data)
        return ok

    async def send_drained(self, stream_id, data, fin=False):
        await super().send_drained(stream_id, data, fin)
        self.tx.setdefault(stream_id, bytearray()).extend(data)

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived) and event.stream_id & 2:
            self.rx.setdefault(event.stream_id, bytearray()).extend(
                event.data)
        super().quic_event_received(event)


class _RecNode(FabricNode):
    def _new_protocol(self, quic, stream_handler=None, **kw):
        return _Recording(quic, stream_handler, node=self, **kw)


def _listen(port, label, cls=FabricNode, **kw):
    return cls(label=label, bind="127.0.0.1", port=port, cert=CERT,
               key=KEY, **kw)


def _dial(ports, label, cls=FabricNode, **kw):
    return cls(label=label, peers=[("127.0.0.1", p) for p in ports],
               redial_interval=0.2, **kw)


async def _until(predicate, timeout=TIMEOUT):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def _groups(track, first, count, per=5, size=200, gap=0.0):
    for g in range(first, first + count):
        for o in range(per):
            await track.write(g, 0, o, bytes([g % 256]) * size)
        await track.end_subgroup(g, 0, end_of_group=per)
        if gap:
            await asyncio.sleep(gap)


def _want(first, count, per=5):
    return {(g, 0, o) for g in range(first, first + count)
            for o in range(per)}


@requires_certs
@pytest.mark.asyncio
async def test_line_forwards_bytes_unchanged_but_the_sns_id():
    port = _BASE_PORT
    t = _listen(port, "T", transit=True)
    a = _dial([port], "A", _RecNode)
    b = _dial([port], "B", _RecNode)
    async with t, a, b:
        await _until(lambda: len(t.connected()) == 2 and a.connected()
                     and b.connected())
        await _until(lambda: any(True for _ in a.learned_nodes()
                                 if _[1].body.node_id == b.node_id))
        tracks = {n: a.publish("l/live", n) for n in ("video", "audio")}
        sinks = {n: _Sink() for n in tracks}
        for n, sink in sinks.items():
            b.pull("l/live", n, sink)
        await _until(lambda: all(tr.subscribed for tr in tracks.values()))
        await _until(lambda: len(t.transit.ingress) == 2)
        await _groups(tracks["video"], 0, 4)
        await _groups(tracks["audio"], 0, 4, per=1)
        audio = tracks["audio"]
        for g in range(4, 7):
            assert audio.datagram(g, 0, b"d" * 300) == 1
            await asyncio.sleep(0.005)
        await _until(lambda: set(sinks["video"].objects) == _want(0, 4)
                     and len(sinks["audio"].objects) == 7)
        (pa,) = a._protocols.values()
        (pb,) = b._protocols.values()
        await _until(lambda: sum(map(len, pb.rx.values()))
                     == sum(map(len, pa.tx.values())))
    for sink in sinks.values():
        assert sink.objects and not sink.dups
    tx = [bytes(v) for v in pa.tx.values()]
    rx = [bytes(v) for v in pb.rx.values()]
    assert len(tx) == len(rx) == 8
    assert sorted(x[:2] + x[6:] for x in tx) == sorted(
        x[:2] + x[6:] for x in rx)


@requires_certs
@pytest.mark.asyncio
async def test_transit_node_with_its_own_receiver():
    port = _BASE_PORT + 1
    t = _listen(port, "T", transit=True)
    a, b = _dial([port], "A"), _dial([port], "B")
    async with t, a, b:
        await _until(lambda: len(t.connected()) == 2)
        track = a.publish("r/live", "video")
        st, sb = _Sink(), _Sink()
        t.pull("r/live", "video", st)
        b.pull("r/live", "video", sb)
        await _until(lambda: len(track.origin.sns) == 1 and len(
            next(iter(track.origin.sns.values())).nodes) == 2)
        await _groups(track, 0, 5)
        await _until(lambda: set(st.objects) == set(sb.objects)
                     == _want(0, 5))
        (ing,) = t.transit.ingress.values()
        assert ing.local and len(ing.egress) == 1
    assert not st.dups and not sb.dups


@requires_certs
@pytest.mark.asyncio
async def test_withdraws_propagate_along_a_line():
    p1, p2 = _BASE_PORT + 2, _BASE_PORT + 3
    t1 = _listen(p1, "T1", transit=True)
    t2 = _listen(p2, "T2", transit=True, peers=[("127.0.0.1", p1)],
                 redial_interval=0.2)
    a, b = _dial([p1], "A"), _dial([p2], "B")
    async with t1, t2, a, b:
        await _until(lambda: any(rec.body.node_id == a.node_id
                                 for _, rec in b.learned_nodes()))
        a.announce("w/live", "video")
        b.subscribe("w/live", "video")
        await _until(lambda: any(True for _ in b.learned_announces())
                     and any(True for _ in a.learned_subscribes()))
        a.withdraw_announce("w/live", "video")
        b.unsubscribe("w/live", "video")
        await _until(lambda: not any(True for _ in b.learned_announces())
                     and not any(True for _ in a.learned_subscribes()))
        await a.stop()
        await _until(lambda: not any(rec.body.node_id == a.node_id
                                     for _, rec in b.learned_nodes()))


@requires_certs
@pytest.mark.asyncio
async def test_triangle_neither_loops_nor_duplicates():
    ports = [_BASE_PORT + 4 + i for i in range(3)]
    t1 = _listen(ports[0], "T1", transit=True)
    t2 = _listen(ports[1], "T2", transit=True,
                 peers=[("127.0.0.1", ports[0])], redial_interval=0.2)
    t3 = _listen(ports[2], "T3", transit=True,
                 peers=[("127.0.0.1", ports[0]), ("127.0.0.1", ports[1])],
                 redial_interval=0.2)
    a, b = _dial([ports[0]], "A"), _dial([ports[2]], "B")
    async with t1, t2, t3, a, b:
        await _until(lambda: [len(t.connected()) for t in (t1, t2, t3)]
                     == [3, 2, 3])
        await _until(lambda: any(rec.body.node_id == a.node_id
                                 for _, rec in b.learned_nodes()))
        track = a.publish("g/live", "video")
        sink = _Sink()
        b.pull("g/live", "video", sink)
        await _until(lambda: track.subscribed)
        await _until(lambda: t3.transit.ingress)
        await _groups(track, 0, 6)
        await _until(lambda: set(sink.objects) == _want(0, 6))
        await asyncio.sleep(0.2)
        node_msgs = sum(s.rx_msgs["NODE_INFO_ADV"]
                        for t in (t1, t2, t3) for s in t.connected())
    assert not sink.dups and len(sink.objects) == 30
    # T2 carries none of it: T1 reaches T3 directly.
    assert not any(eg for ing in t2.transit.ingress.values()
                   for eg in ing.egress.values())
    assert node_msgs < 40


@requires_certs
@pytest.mark.asyncio
async def test_diamond_takes_the_lower_metric_and_fails_over():
    p1, p2 = _BASE_PORT + 7, _BASE_PORT + 8
    t1 = _listen(p1, "T1", transit=True, metric=100)
    t2 = _listen(p2, "T2", transit=True, metric=200)
    a, b = _dial([p1, p2], "A"), _dial([p1, p2], "B")
    t1_id = t1.node_id
    sink = _Sink()
    async with t2, a, b:
        await t1.start()
        await _until(lambda: len(t1.connected()) == 2
                     and len(t2.connected()) == 2)
        track = a.publish("d/live", "video")
        b.pull("d/live", "video", sink)

        def via(node):
            sid = next(iter(track.origin.sns), None)
            s = a.sessions.get(sid) if sid is not None else None
            return s is not None and s.remote.node_id == node.node_id

        await _until(lambda: via(t1))
        await _groups(track, 0, 5, gap=0.01)
        await _until(lambda: set(sink.objects) >= _want(0, 5))
        # Stop T1: traffic moves to T2, the in-flight group at most lost.
        sender = asyncio.ensure_future(_groups(track, 5, 30, gap=0.01))
        await asyncio.sleep(0.05)
        await t1.stop()
        await sender
        await _until(lambda: via(t2))
        # Peers learn of T1's loss from the close, not from withdraws.
        assert set(_withdraws_from(a, t1).values()) == {0}
        assert set(_withdraws_from(b, t1).values()) == {0}
        await _groups(track, 35, 5, gap=0.01)
        await _until(lambda: set(sink.objects) >= _want(35, 5))
        missing = _want(0, 40) - set(sink.objects)
        assert len({g for g, _, _ in missing}) <= 1, sorted(missing)
        # T1 back: it takes the traffic again, nothing lost on the move.
        t1 = _listen(p1, "T1", transit=True, metric=100, node_id=t1_id)
        await t1.start()
        await _until(lambda: via(t1))
        before = set(sink.objects)
        await _groups(track, 40, 10, gap=0.01)
        await _until(lambda: set(sink.objects) >= _want(40, 10))
        assert set(sink.objects) - before == _want(40, 10)
        assert any(ing.streams for ing in t1.transit.ingress.values())
        await t1.stop()
    assert sink.objects and not sink.dups


# -- external egress (FabricHop) make before break --------------------------

class _Hops:
    """A relay-like egress: one FabricHop per (track, session) from
    on_sns, closed on its loss."""

    def __init__(self, node):
        self.node = node
        self.hops = {}
        self.calls = []
        node.on_sns = self.on_sns

    def on_sns(self, track, sid, sns):
        self.calls.append((sid, None if sns is None else sns.sns_id))
        key = (track.fullname_hash, sid)
        if sns is None:
            hop = self.hops.pop(key, None)
            if hop is not None:
                hop.close()
        elif key not in self.hops:
            self.hops[key] = FabricHop(self.node, track, sid)

    def live(self, track):
        """Hops that take new streams."""
        return [h for (fh, _), h in sorted(self.hops.items())
                if fh == track.fullname_hash and not h.retiring
                and not h.closed]


def _pub_two_paths():
    pub = FabricNode(label="pub")
    w1 = _Wire(pub, 1, _remote(A, "t1"))
    w2 = _Wire(pub, 2, _remote(C, "t2"))
    for w, metric in ((w1, 100), (w2, 200)):
        w.send(MsgType.NODE_INFO_ADV, NodeInfo(
            B, contact="b", path=(PathItem(w.remote.local.node_id, 1,
                                           metric),)))
        w.send(MsgType.SUBSCRIBE_INFO_ADV, _sub(B, NS, b"video"))
    hops = _Hops(pub)
    track = pub.publish(NS, b"video")
    return pub, w1, w2, hops, track


def _move_to_t2(w1):
    w1.send(MsgType.NODE_INFO_ADV, NodeInfo(
        B, contact="b", path=(PathItem(A, 1, 900),)))


async def _hop_open(hop, h, gid):
    from aiomoqt.tests.test_fabric_edge import _relay_header
    hdr = _relay_header(h, gid)
    sid = await hop.open_uni_stream(initial=hdr.serialize().data
                                    + hdr.next_object_bytes(
                                        payload=b"o0", extensions=None,
                                        object_id=0))
    return hdr, sid


@pytest.mark.asyncio
async def test_hop_retires_on_a_move_and_finishes_its_group():
    pub, w1, w2, hops, track = _pub_two_paths()
    h = track.fullname_hash
    (hop1,) = hops.live(track)
    hdr, s1 = await _hop_open(hop1, h, 0)
    _move_to_t2(w1)
    assert hop1.retiring and not hop1.closed
    assert hops.calls == [(1, 1), (2, 1)]      # loss of 1 not yet
    with pytest.raises(HopRetiring):
        await hop1.open_uni_stream(initial=b"x")
    (hop2,) = hops.live(track)
    assert hop2.session_id == 2
    for oid in (1, 2):
        assert hop1.stream_write_nowait(s1, hdr.next_object_bytes(
            payload=b"o", extensions=None, object_id=oid))
    assert _sent(w1, MsgType.SNS_WD) == 0
    hop1.stream_write(s1, b"", end_stream=True)
    assert hops.calls[-1] == (1, None) and hop1.closed
    assert _sent(w1, MsgType.SNS_WD) == 1
    _, s2 = await _hop_open(hop2, h, 1)
    assert s1 in w1.quic.fins and s2 in w2.quic.data
    assert not track._hop_streams.get((1, s1))


@pytest.mark.asyncio
async def test_hop_drain_is_bounded(monkeypatch):
    from aiomoqt.fabric import node as node_mod
    monkeypatch.setattr(node_mod, "DRAIN_MAX_S", 0.05)
    pub, w1, w2, hops, track = _pub_two_paths()
    (hop1,) = hops.live(track)
    _, s1 = await _hop_open(hop1, track.fullname_hash, 0)
    _move_to_t2(w1)
    assert hop1.retiring
    await asyncio.sleep(0.15)
    assert track.drain_cut == 1 and s1 in w1.quic.resets
    assert hops.calls[-1] == (1, None) and hop1.closed
    assert _sent(w1, MsgType.SNS_WD) == 1


@pytest.mark.asyncio
async def test_hop_session_close_mid_group_is_immediate():
    pub, w1, w2, hops, track = _pub_two_paths()
    (hop1,) = hops.live(track)
    await _hop_open(hop1, track.fullname_hash, 0)
    w1.close()
    assert hop1.closed and (1, None) in hops.calls
    assert not track._hop_streams and not track._draining
    (hop2,) = hops.live(track)
    assert hop2.session_id == 2


# -- peer API ------------------------------------------------------------

@pytest.mark.asyncio
async def test_peer_api_add_remove_metric_and_table():
    node = FabricNode(label="n", transit=True)
    node.add_peer("192.0.2.7", 4000, metric=300)
    node.add_peer("192.0.2.7", 4000)
    assert node.peers == [("192.0.2.7", 4000)]
    (row,) = node.peer_table()
    assert (row["status"], row["metric"], row["session"]) == (
        "connecting", 300, None)
    w = _Wire(node, 1, _remote(A, "a"), dial_key=("192.0.2.7", 4000))
    # Re-applied when the dial comes up.
    w.control(b"")
    assert node.advertised_metric(1) == 300
    assert w.remote.nodes[node.node_id].body.metric == 300
    (row,) = node.peer_table()
    assert (row["status"], row["origin"], row["remote_node_id"]) == (
        "connected", "dialed", f"{A >> 48 & 0xffff}.{A >> 32 & 0xffff}:"
        f"{A >> 16 & 0xffff}.{A & 0xffff}")
    assert node.set_peer_metric("192.0.2.7", 4000, 500)
    assert node.advertised_metric(1) == 500
    w.close()
    w2 = _Wire(node, 2, _remote(A, "a"), dial_key=("192.0.2.7", 4000))
    w2.control(b"")
    assert node.advertised_metric(2) == 500
    assert node.remove_peer("192.0.2.7", 4000)
    assert w2.session.state == "closed"
    assert not node.peers and not node.remove_peer("192.0.2.7", 4000)
    assert node.peer_table() == []


async def _hop_group(hops, track, gid, per=6, at=None, then=None):
    """One group through the egress the way a relay writes it: opened on
    a hop taking new streams, written to its end there. `then` runs
    before object `at`. False when the group could not be finished."""
    from aiomoqt.tests.test_fabric_edge import _relay_header
    live = hops.live(track)
    if not live:
        return False
    hop, h = live[0], track.fullname_hash
    hdr = _relay_header(h, gid)
    try:
        sid = await hop.open_uni_stream(
            initial=hdr.serialize().data + hdr.next_object_bytes(
                payload=b"x" * 300, extensions=None, object_id=0))
        for oid in range(1, per):
            if oid == at and then is not None:
                await then()
            data = hdr.next_object_bytes(payload=b"x" * 300, extensions=None,
                                         object_id=oid)
            if not hop.stream_write_nowait(sid, data):
                await hop.stream_write_drain(sid, data)
            await asyncio.sleep(0.005)
        hop.stream_write(sid, hdr.next_object(
            payload=b"", status=ObjectStatus.END_OF_GROUP,
            object_id=per).data, end_stream=True)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        return False
    return True


@requires_certs
@pytest.mark.asyncio
async def test_diamond_external_egress_moves_without_loss():
    p1, p2 = _BASE_PORT + 9, _BASE_PORT + 10
    t1 = _listen(p1, "T1", transit=True, metric=100)
    t2 = _listen(p2, "T2", transit=True, metric=200)
    a, b = _dial([p1, p2], "A"), _dial([p1, p2], "B")
    sink, per = _Sink(), 6
    async with t1, t2, a, b:
        await _until(lambda: len(t1.connected()) == 2
                     and len(t2.connected()) == 2)
        hops = _Hops(a)
        track = a.publish("x/live", "video")
        b.pull("x/live", "video", sink)

        def via(t):
            live = hops.live(track)
            return bool(live) and a.sessions[live[0].session_id] \
                .remote.node_id == t.node_id

        await _until(lambda: via(t1))
        for g in range(3):
            assert await _hop_group(hops, track, g, per)

        async def move():
            # A leaf advertises 2^32: a session metric counts above it.
            a.set_peer_metric("127.0.0.1", p1, (1 << 32) + 1000)
            assert via(t2)

        # Move mid-group: it finishes on T1, the next group takes T2.
        assert await _hop_group(hops, track, 3, per, at=3, then=move)
        for g in range(4, 7):
            assert await _hop_group(hops, track, g, per)
        await _until(lambda: set(sink.objects) >= _want(0, 7, per))
        assert set(sink.objects) == _want(0, 7, per) and not sink.dups
        await _until(lambda: not track._draining)
        assert track.drain_cut == 0
        for t, n in ((t1, 4), (t2, 3)):
            assert sum(f.streams for s in t.sessions.values()
                       for f in s.flows.values()) == n

        # Session close mid-group: at most that group is lost.
        async def stop_t2():
            await t2.stop()

        await _hop_group(hops, track, 7, per, at=2, then=stop_t2)
        await _until(lambda: via(t1))
        for g in range(8, 11):
            assert await _hop_group(hops, track, g, per)
        await _until(lambda: set(sink.objects) >= _want(8, 3, per))
        missing = _want(0, 11, per) - set(sink.objects)
        assert {g for g, _, _ in missing} <= {7}
    assert sink.objects and not sink.dups


def _withdraws_from(node, other):
    """Withdraws `node` received on its sessions to `other`."""
    kinds = ("NODE_INFO_WD", "SUBSCRIBE_INFO_WD", "ANNOUNCE_INFO_WD")
    return {k: sum(s.rx_msgs[k] for s in node.sessions.values()
                   if s.remote and s.remote.node_id == other.node_id)
            for k in kinds}


@requires_certs
@pytest.mark.asyncio
async def test_graceful_stop_withdraws_nothing_it_only_transits():
    port = _BASE_PORT + 11
    t = _listen(port, "T", transit=True)
    a, b = _dial([port], "A"), _dial([port], "B")
    async with a, b:
        await t.start()
        await _until(lambda: any(rec.body.node_id == a.node_id
                                 for _, rec in b.learned_nodes()))
        a.announce("s/live")
        track = a.publish("s/live", "video")
        sink = _Sink()
        b.pull("s/live", "video", sink)
        await _until(lambda: track.subscribed
                     and any(True for _ in b.learned_announces()))
        await _groups(track, 0, 2)
        await _until(lambda: set(sink.objects) == _want(0, 2))
        await t.stop()
        await _until(lambda: not a.connected() and not b.connected())
        assert _withdraws_from(a, t) == _withdraws_from(b, t) == {
            "NODE_INFO_WD": 0, "SUBSCRIBE_INFO_WD": 0,
            "ANNOUNCE_INFO_WD": 0}
    assert not sink.dups
