"""The relay edge API of aiomoqt.fabric: FabricHop bytes against
FabricTrack's and the moqx vectors, the datagram cap, nowait refusal and
drain, FabricTrack's nowait-then-gather writes, the learned-state and SNS
callbacks, FabricSource pulls, and two tracks on one node. Sans-I/O
against an in-memory peer, then over localhost raw QUIC on one shared
client engine."""
import asyncio

import pytest
from aiopquic.asyncio.client import QuicClientEngine

from aiomoqt.messages import (SUBGROUP_ID_EXPLICIT, SUBGROUP_ID_ZERO,
                              ObjectStatus)
from aiomoqt.messages.data import ObjectDatagram, ObjectHeader, SubgroupHeader
from aiomoqt.fabric import (FabricHop, FabricNode, FabricSource,
                             relay_object, track_hashes)
from aiomoqt.fabric.inner import (DATAGRAM_MAX_INNER, P18, DatagramObject,
                                   StreamObject, SubgroupWriter,
                                   encode_datagram)
from aiomoqt.fabric.node import _PeerProtocol
from aiomoqt.fabric.session import PeerSession
from aiomoqt.fabric.wire import (ANNOUNCE_NAMESPACE, AnnounceInfo,
                                  DataHeader, DataType, MsgType, NodeInfo,
                                  PathItem, SubscribeInfo, WireError)

from aiomoqt.tests._certs import CERT, KEY, requires_certs

NS = (b"moq-test", b"live")
H = track_hashes(NS, b"video").fullname
# moqx's frames (wire-spec §10.1), whose alias and header hash were
# 0xf9deacf2c69f506d: here the track's chained hash.
_MOQX_HASH = "f9deacf2c69f506d"
NEW_STREAM = "130200000001f9deacf2c69f506d8000001388"
SG0_HEADER = "51fff9deacf2c69f506d0580"
OBJ_HELLO = "00000568656c6c6f"
OBJ_WORLD = "000005776f726c64"
OBJ_EOG = "00000003"
DGRAM_OBJ0 = "0e0000000001f9deacf2c69f506d04fff9deacf2c69f506d07806869"


def _vec(hexed: str) -> str:
    return hexed.replace(_MOQX_HASH, f"{H:016x}")

_BASE_PORT = 33580
TIMEOUT = 10


# -- in-memory peer --------------------------------------------------

class _Quic:
    """The aiopquic connection calls a fabric session makes."""

    def __init__(self):
        self.data = {}
        self.fins = set()
        self.resets = {}
        self.datagrams = []
        self.drained = []
        self.refuse = 0
        self.fill = 0.0
        self.dgram_full = False
        self.max_dgram = 1200
        self.gate = None
        self._next_uni = 2

    def get_next_available_stream_id(self, is_unidirectional=False):
        sid, self._next_uni = self._next_uni, self._next_uni + 4
        return sid

    def _put(self, sid, data, end_stream):
        self.data.setdefault(sid, bytearray()).extend(data)
        if end_stream:
            self.fins.add(sid)

    def send_stream_data(self, sid, data, end_stream=False, priority=None):
        if self.refuse:
            self.refuse -= 1
            raise BufferError("ring full")
        self._put(sid, data, end_stream)

    async def send_stream_data_drained(self, sid, data, end_stream=False,
                                       **kw):
        self.drained.append(sid)
        if self.gate is not None:
            await self.gate.wait()
        await asyncio.sleep(0)
        self._put(sid, data, end_stream)

    def tx_event_ring_fill(self):
        return self.fill

    def send_datagram_frame(self, frame):
        if self.dgram_full:
            return False
        self.datagrams.append(bytes(frame))
        return True

    def reset_stream(self, sid, code):
        self.resets[sid] = code

    def max_datagram_payload(self):
        return self.max_dgram

    def peer_address(self):
        return ("192.0.2.1", 33434)

    def path_quality(self):
        return {}


class _Proto:
    send_data = _PeerProtocol.send_data
    send_drained = _PeerProtocol.send_drained
    send_nowait = _PeerProtocol.send_nowait

    def __init__(self, quic, wire):
        self._quic = quic
        self.wire = wire
        self.session = None
        self.dialer = True
        self.terminated = asyncio.Event()
        self.dial_key = None
        self.tx_streams = self.tx_bytes = self.tx_datagrams = 0

    def write_control(self, data):
        self.wire.control(data)

    def close(self):
        self.wire.close()


class _Wire:
    """A node session dialed to a remote PeerSession, in memory."""

    def __init__(self, node, sid, remote, dial_key=None):
        self.node = node
        self.quic = _Quic()
        self.proto = _Proto(self.quic, self)
        self.proto.dial_key = dial_key
        self.session = s = PeerSession(node.local, dialer=True,
                                       session_id=sid, peer=f"peer{sid}",
                                       on_event=node._event)
        s.decode = node._sinks
        s.on_object = node._on_object
        s.on_stream_end = node._on_stream_end
        self.proto.session = s
        node.sessions[sid] = s
        node._protocols[sid] = self.proto
        self.remote = PeerSession(remote, dialer=False, session_id=1)
        s.start(0)
        node._flush(self.proto)

    def control(self, data):
        self.remote.control_data(0, data)
        for out in self.remote.take_output():
            self.session.control_data(0, out)
        self.node._flush(self.proto)

    def send(self, msg_type, body):
        self.remote.send(msg_type, body)
        self.control(b"")

    def close(self):
        self.proto.terminated.set()
        self.node._close_session(self.session, "test")


def _node(label="edge"):
    return FabricNode(label=label)


def _remote(node_id, label="far"):
    return NodeInfo(node_id, contact=label)


def _sub(source, ns, name, seq=1, forward=1):
    th = track_hashes(ns, name)
    return SubscribeInfo(source, ns, name, th.namespace, th.name,
                         th.fullname, epoch=0, seq=seq, priority=128,
                         delivery_timeout_ms=5000, expires_ms=5000,
                         forward=forward)


def _ann(source, ns, name=b""):
    return AnnounceInfo(source, track_hashes(ns, name).fullname, ns, name,
                        0 if name else ANNOUNCE_NAMESPACE)


def _relay_header(alias, gid, sgid=0, first_object=True):
    """A d18 subgroup header written by aiomoqt's codec, shaped as moqx's."""
    return SubgroupHeader(track_alias=alias, group_id=gid, subgroup_id=sgid,
                          publisher_priority=128, extensions_present=True,
                          end_of_group=False, first_object=first_object,
                          subgroup_id_mode=(SUBGROUP_ID_ZERO if sgid == 0
                                            else SUBGROUP_ID_EXPLICIT),
                          prof=P18)


async def _relay_group(hop, alias, gid, payloads, sgid=0):
    """One subgroup the way a relay forwards it: header and first object
    open the stream, objects nowait (drained when refused), then the
    END_OF_GROUP marker with the FIN."""
    h = _relay_header(alias, gid, sgid)
    initial = h.serialize().data + h.next_object_bytes(
        payload=payloads[0], extensions=None, object_id=0)
    sid = await hop.open_uni_stream(initial=initial, priority=4)
    for oid, payload in enumerate(payloads[1:], 1):
        data = h.next_object_bytes(payload=payload, extensions=None,
                                   object_id=oid)
        if not hop.stream_write_nowait(sid, data):
            await hop.stream_write_drain(sid, data)
    tail = h.next_object(payload=b"", status=ObjectStatus.END_OF_GROUP,
                         object_id=len(payloads)).data
    hop.stream_write(sid, tail, end_stream=True)
    return sid


def _subscribed_edge(*tracks):
    """A node publishing `tracks`, each subscribed by the far node on one
    session; FabricHops kept by on_sns."""
    node = _node()
    far = 0x0a0a0a0a0b0b0b0b
    wire = _Wire(node, 1, _remote(far))
    hops, calls = {}, []

    def on_sns(track, sid, sns):
        calls.append((track.fullname_hash, sid, sns))
        key = (track.fullname_hash, sid)
        if sns is None:
            hop = hops.pop(key, None)
            if hop is not None:
                hop.close()
        elif key not in hops:
            hops[key] = FabricHop(node, track, sid)

    node.on_sns = on_sns
    published = []
    for ns, name in tracks:
        published.append(node.publish(ns, name))
        wire.send(MsgType.SUBSCRIBE_INFO_ADV, _sub(far, ns, name))
    return node, wire, far, published, hops, calls


# -- FabricHop bytes ---------------------------------------------------

@pytest.mark.asyncio
async def test_hop_bytes_equal_fabric_track_bytes_and_moqx_vectors():
    node, wire, _, (track,), hops, _ = _subscribed_edge((NS, b"video"))
    assert track.fullname_hash == H
    await track.write(5, 0, 0, b"hello")
    await track.write(5, 0, 1, b"world")
    await track.end_subgroup(5, 0, end_of_group=2)
    (tsid,) = wire.quic.data
    hop = hops[(H, 1)]
    assert hop.sns_id == 1 and hop._profile is P18
    hsid = await _relay_group(hop, H, 5, [b"hello", b"world"])
    assert H >> 56     # a 9-byte varint alias, as in the vectors
    want = _vec(NEW_STREAM + SG0_HEADER + OBJ_HELLO + OBJ_WORLD + OBJ_EOG)
    assert wire.quic.data[tsid].hex() == want
    assert wire.quic.data[hsid].hex() == want
    assert {tsid, hsid} <= wire.quic.fins
    assert hop.streams == 1

    assert track.datagram(7, 0, b"hi") == 1
    dgram = ObjectDatagram(track_alias=H, group_id=7, object_id=0,
                           publisher_priority=128, payload=b"hi")
    assert hop.send_dgram_message(dgram.serialize(prof=P18)) == 14
    assert [d.hex() for d in wire.quic.datagrams] == [_vec(DGRAM_OBJ0)] * 2


@pytest.mark.asyncio
async def test_hop_datagram_cap():
    node, wire, _, (track,), hops, _ = _subscribed_edge((NS, b"video"))
    hop = hops[(H, 1)]
    assert hop.datagram_max_payload() == DATAGRAM_MAX_INNER == 1186
    assert hop.send_dgram_message(b"\x00" * 1186) == 1186
    with pytest.raises(WireError):
        hop.send_dgram_message(b"\x00" * 1187)
    assert isinstance(WireError("x"), ValueError)
    wire.quic.max_dgram = 1000
    assert hop.datagram_max_payload() == 986
    wire.quic.dgram_full = True
    assert hop.send_dgram_message(b"\x00" * 10) == 0
    assert (hop.datagrams, hop.datagrams_dropped) == (1, 1)
    assert all(len(d) == 1200 for d in wire.quic.datagrams)


@pytest.mark.asyncio
async def test_hop_nowait_refusal_then_drain():
    node, wire, _, (track,), hops, _ = _subscribed_edge((NS, b"video"))
    hop, quic = hops[(H, 1)], wire.quic
    sid = await hop.open_uni_stream(initial=b"\x01")
    quic.refuse = 1
    assert hop.stream_write_nowait(sid, b"\x02") is False
    assert bytes(quic.data[sid])[-1:] == b"\x01"
    await hop.stream_write_drain(sid, b"\x02")
    assert bytes(quic.data[sid])[-2:] == b"\x01\x02"
    quic.fill = 0.95
    assert hop.stream_write_nowait(sid, b"\x03") is False
    quic.fill = 0.0
    assert hop.stream_write_nowait(sid, b"\x03", end_stream=True)
    assert sid in quic.fins
    # A FIN the ring refuses goes out once it drains.
    sid2 = await hop.open_uni_stream(initial=b"\x01")
    quic.refuse = 1
    hop.stream_write(sid2, b"", end_stream=True)
    await asyncio.sleep(0.01)
    assert sid2 in quic.fins
    hop.stream_reset(sid2)
    assert quic.resets[sid2] == 0x1     # CANCELLED at d18


@pytest.mark.asyncio
async def test_hop_closes_with_its_sns_and_session():
    node, wire, far, (track,), hops, calls = _subscribed_edge((NS, b"video"))
    hop = hops[(H, 1)]
    wire.send(MsgType.SUBSCRIBE_INFO_WD, _sub(far, NS, b"video", seq=2))
    assert calls[-1] == (H, 1, None) and hop.closed
    assert hop._moqt_session_closed.done()
    assert hop.datagram_max_payload() == 0
    with pytest.raises(asyncio.CancelledError):
        await hop.open_uni_stream(initial=b"x")
    wire.send(MsgType.SUBSCRIBE_INFO_ADV, _sub(far, NS, b"video", seq=3))
    again = hops[(H, 1)]
    assert again is not hop and again.sns_id == 2
    wire.close()
    assert calls[-1] == (H, 1, None) and again.closed


@pytest.mark.asyncio
async def test_fabric_track_write_is_nowait_then_gathered():
    node = _node()
    far1, far2 = 0x0101010101010101, 0x0202020202020202
    w1 = _Wire(node, 1, _remote(far1, "far1"))
    w2 = _Wire(node, 2, _remote(far2, "far2"))
    track = node.publish(NS, b"video")
    w1.send(MsgType.SUBSCRIBE_INFO_ADV, _sub(far1, NS, b"video"))
    w2.send(MsgType.SUBSCRIBE_INFO_ADV, _sub(far2, NS, b"video"))
    assert set(track.origin.sns) == {1, 2}
    # Session 1 is backpressured and its drain waits; session 2 still
    # gets the object at once.
    w1.quic.refuse, w1.quic.gate = 1, asyncio.Event()
    task = asyncio.ensure_future(track.write(0, 0, 0, b"a" * 10))
    await asyncio.sleep(0.01)
    assert w2.quic.data and not w1.quic.data and not task.done()
    w1.quic.gate.set()
    await asyncio.wait_for(task, 1)
    assert bytes(w1.quic.data[2]) == bytes(w2.quic.data[2])
    assert w1.quic.drained == [2] and not w2.quic.drained
    w2.quic.refuse = 1
    await track.end_subgroup(0, 0, end_of_group=1)
    assert w1.quic.fins == w2.quic.fins == {2}


# -- callbacks ---------------------------------------------------------

def test_sns_callback_on_gain_change_and_loss():
    node = _node()
    far, other = 0x0a0a0a0a0b0b0b0b, 0x0c0c0c0c0d0d0d0d
    wire = _Wire(node, 1, _remote(far))
    calls = []
    node.on_sns = lambda t, sid, sns: calls.append(
        (sid, None if sns is None else (sns.sns_id, sns.nodes)))
    node.publish(NS, b"video")
    assert calls == []
    wire.send(MsgType.SUBSCRIBE_INFO_ADV, _sub(far, NS, b"video"))
    assert calls == [(1, (1, (far,)))]
    wire.send(MsgType.NODE_INFO_ADV, NodeInfo(
        other, contact="other", path=(PathItem(far, 100, 1),)))
    wire.send(MsgType.SUBSCRIBE_INFO_ADV, _sub(other, NS, b"video"))
    assert calls[-1] == (1, (1, tuple(sorted((far, other)))))
    wire.send(MsgType.SUBSCRIBE_INFO_WD, _sub(far, NS, b"video", seq=2))
    assert calls[-1] == (1, (1, (other,)))
    wire.send(MsgType.SUBSCRIBE_INFO_WD, _sub(other, NS, b"video", seq=2))
    assert calls[-1] == (1, None)
    assert len(calls) == 4
    assert {"SNS_ADV", "SNS_WD"} <= set(wire.session.tx_msgs)


def test_sns_loss_on_unpublish_and_session_close():
    node = _node()
    far = 0x0a0a0a0a0b0b0b0b
    wire = _Wire(node, 1, _remote(far))
    calls = []
    node.on_sns = lambda t, sid, sns: calls.append((t.fullname_hash, sid,
                                                    sns))
    video = node.publish(NS, b"video")
    audio = node.publish(NS, b"audio")
    for name in (b"video", b"audio"):
        wire.send(MsgType.SUBSCRIBE_INFO_ADV, _sub(far, NS, name))
    assert [c[2].sns_id for c in calls] == [1, 2]
    node.unpublish(video)
    assert calls[-1] == (video.fullname_hash, 1, None)
    assert video.fullname_hash not in {a.fullname_hash
                                       for a in node.originated_announces()}
    wire.close()
    assert calls[-1] == (audio.fullname_hash, 1, None)
    assert len(calls) == 4


def test_publish_without_announce_keeps_the_namespace_announce():
    node = _node()
    node.announce(NS)
    track = node.publish(NS, b"video", announce=False)
    assert [a.name for a in node.originated_announces()] == [b""]
    node.unpublish(track)
    assert [a.name for a in node.originated_announces()] == [b""]


def test_learned_announces_and_subscribes_aggregate_across_sessions():
    node = _node()
    pub = 0x0e0e0e0e0f0f0f0f
    w1 = _Wire(node, 1, _remote(0x0101010101010101, "a"))
    w2 = _Wire(node, 2, _remote(0x0202020202020202, "b"))
    anns, subs = [], []
    node.on_announce = lambda a, up: anns.append((a.name, up))
    node.on_subscribe = lambda s, up: subs.append((s.name, s.seq, up))
    for w in (w1, w2):
        w.send(MsgType.ANNOUNCE_INFO_ADV, _ann(pub, NS))
        w.send(MsgType.ANNOUNCE_INFO_ADV, _ann(pub, NS, b"video"))
    assert anns == [(b"", True), (b"video", True)]
    w1.send(MsgType.ANNOUNCE_INFO_WD, _ann(pub, NS, b"video"))
    assert len(anns) == 2
    w2.send(MsgType.ANNOUNCE_INFO_WD, _ann(pub, NS, b"video"))
    assert anns[-1] == (b"video", False)
    assert [a.name for a in node.remote_announces()] == [b""]
    w2.close()
    assert len(anns) == 3
    w1.close()
    assert anns[-1] == (b"", False) and not node.remote_announces()

    node = _node()
    w1 = _Wire(node, 1, _remote(0x0101010101010101, "a"))
    w2 = _Wire(node, 2, _remote(0x0202020202020202, "b"))
    node.on_subscribe = lambda s, up: subs.append((s.name, s.seq, up))
    w1.send(MsgType.SUBSCRIBE_INFO_ADV, _sub(pub, NS, b"video", seq=1))
    w2.send(MsgType.SUBSCRIBE_INFO_ADV, _sub(pub, NS, b"video", seq=1))
    assert subs == [(b"video", 1, True)]
    w1.send(MsgType.SUBSCRIBE_INFO_ADV,
            _sub(pub, NS, b"video", seq=2, forward=0))
    assert subs[-1] == (b"video", 2, True)
    assert node.remote_subscribes()[0].forward == 0
    # The older copy on the other path is no change.
    w1.close()
    assert len(subs) == 2
    w2.send(MsgType.SUBSCRIBE_INFO_WD, _sub(pub, NS, b"video", seq=2))
    assert subs[-1] == (b"video", 2, False)
    assert not node.remote_subscribes()


# -- FabricSource ------------------------------------------------------

def _stream_bytes(sns_id, alias, gid, payloads, sgid=0, eog=True):
    w = SubgroupWriter(alias, gid, sgid)
    out = DataHeader(DataType.NEW_STREAM, sns_id, alias, 128,
                     5000).encode() + w.header
    for oid, p in enumerate(payloads):
        out += w.object(oid, p)
    if eog:
        out += w.status(len(payloads), ObjectStatus.END_OF_GROUP)
    return out


@pytest.mark.asyncio
async def test_fabric_source_pulls_two_tracks_on_one_node():
    node = _node()
    wire = _Wire(node, 1, _remote(0x0a0a0a0a0b0b0b0b))
    source = FabricSource(node)
    assert source._profile is P18 and not source.closed
    got, ends = [], []
    pulls = {}
    for name in (b"video", b"audio"):
        h = track_hashes(NS, name).fullname
        source.register_object_handler(
            h, lambda msg, size, ts, gid, sgid, n=name: got.append(
                (n, gid, sgid, msg.object_id, msg.status, bytes(msg.payload),
                 type(msg).__name__)))
        source.register_stream_end_handler(
            h, lambda gid, sgid, clean, reset_code, n=name: ends.append(
                (n, gid, sgid, clean)))
        pulls[name] = source.subscribe(NS, name)
    assert {p.track_alias for p in pulls.values()} == {
        track_hashes(NS, n).fullname for n in pulls}
    assert [s.name for s in node.originated_subscribes()] == [b"video",
                                                              b"audio"]
    hv, ha = (pulls[n].track_alias for n in (b"video", b"audio"))
    s = wire.session
    data = _stream_bytes(1, hv, 3, [b"v0", b"v1"], sgid=1)
    for i in range(len(data)):          # byte by byte
        s.stream_data(3, data[i:i + 1], i == len(data) - 1)
    s.stream_data(7, _stream_bytes(2, ha, 3, [b"a0"], eog=False), False)
    s.stream_reset(7)
    s.datagram(DataHeader(DataType.DATAGRAM, 2, ha).encode()
               + encode_datagram(ha, 4, 0, b"ad"))
    assert got == [
        (b"video", 3, 1, 0, ObjectStatus.NORMAL, b"v0", "ObjectHeader"),
        (b"video", 3, 1, 1, ObjectStatus.NORMAL, b"v1", "ObjectHeader"),
        (b"video", 3, 1, 2, ObjectStatus.END_OF_GROUP, b"", "ObjectHeader"),
        (b"audio", 3, 0, 0, ObjectStatus.NORMAL, b"a0", "ObjectHeader"),
        (b"audio", 4, None, 0, ObjectStatus.NORMAL, b"ad", "ObjectDatagram"),
    ]
    assert ends == [(b"video", 3, 1, True), (b"audio", 3, 0, False)]

    video = pulls[b"video"]
    assert source.request_update(video.request_id, forward=0) is None
    assert not node.subscribed(NS, b"video")
    reply = source.request_update(video.request_id, forward=1,
                                  wait_response=True)
    assert (await reply) is None
    seqs = [rec.body.seq for _, rec in wire.remote.subscribes.items()
            if rec.body.name == b"video"]
    assert seqs == [3]                  # ADV 1, WD 2, ADV 3
    source.unsubscribe(video.request_id)
    assert not node.subscribed(NS, b"video")
    assert hv not in node._sinks and ha in node._sinks
    with pytest.raises(KeyError):
        source.request_update(video.request_id, forward=1)
    source.close()
    assert source.closed and not node.originated_subscribes()
    with pytest.raises(ConnectionError):
        source.subscribe(NS, b"video")


def test_pull_binds_one_sink_per_track():
    node = _node()
    a, b = object(), object()
    node.pull(NS, b"video", a, advertise=False)
    assert not node.subscribed(NS, b"video")
    node.pull(NS, b"video", a)
    assert node.subscribed(NS, b"video")
    with pytest.raises(ValueError):
        node.pull(NS, b"video", b)
    with pytest.raises(ValueError):
        node.pull(NS, b"", a)
    node.unpull(NS, b"video")
    assert not node.subscribed(NS, b"video") and not node._sinks
    node.subscribe(NS, b"video")
    assert node.originated_subscribes()[0].seq == 3


def test_relay_object_fields():
    s = relay_object(StreamObject(5, 2, 7, ObjectStatus.NORMAL, {2: 9},
                                  b"p", 64), H)
    assert isinstance(s, ObjectHeader)
    assert (s.object_id, s.extensions, s.status, s.payload,
            s.publisher_priority, s.stream_flags) == (
                7, {2: 9}, ObjectStatus.NORMAL, b"p", 64, None)
    d = relay_object(DatagramObject(5, 3, ObjectStatus.NORMAL, None, b"q",
                                    32, True), H)
    assert isinstance(d, ObjectDatagram)
    assert (d.track_alias, d.group_id, d.object_id, d.payload,
            d.publisher_priority, d.end_of_group) == (H, 5, 3, b"q", 32,
                                                      True)


# -- over localhost QUIC -----------------------------------------------

def _listener(port, label):
    return FabricNode(label=label, bind="127.0.0.1", port=port, cert=CERT,
                      key=KEY)


async def _until(predicate):
    async with asyncio.timeout(TIMEOUT):
        while not predicate():
            await asyncio.sleep(0.01)


@requires_certs
@pytest.mark.asyncio
async def test_hops_to_fabric_source_two_tracks_over_quic():
    port = _BASE_PORT
    pub = _listener(port, "pub")
    sub = FabricNode(label="sub", peers=[("127.0.0.1", port)],
                     redial_interval=0.2)
    hops = {}

    def on_sns(track, sid, sns):
        key = (track.fullname_hash, sid)
        if sns is None:
            hop = hops.pop(key, None)
            if hop is not None:
                hop.close()
        elif key not in hops:
            hops[key] = FabricHop(pub, track, sid)

    pub.on_sns = on_sns
    async with pub, sub:
        await _until(lambda: pub.connected() and sub.connected())
        assert sub._engine is not None
        tracks = {n: pub.publish("q/live", n) for n in ("video", "audio")}
        source = FabricSource(sub)
        got = {h.fullname_hash: [] for h in tracks.values()}
        ends = {h: [] for h in got}
        for h in got:
            source.register_object_handler(
                h, lambda msg, size, ts, gid, sgid, h=h: got[h].append(
                    (gid, msg.object_id, msg.status)))
            source.register_stream_end_handler(
                h, lambda gid, sgid, clean, reset_code, h=h:
                ends[h].append((gid, clean)))
        pulls = {n: source.subscribe("q/live", n) for n in tracks}
        await _until(lambda: len(hops) == 2)
        groups, per = 4, 5
        for g in range(groups):
            for name, t in tracks.items():
                hop = next(v for (h, _), v in hops.items()
                           if h == t.fullname_hash)
                await _relay_group(hop, t.fullname_hash, g,
                                   [bytes([g]) * 700] * per)
        audio = tracks["audio"].fullname_hash
        ahop = next(v for (h, _), v in hops.items() if h == audio)
        for o in range(3):
            dg = ObjectDatagram(track_alias=audio, group_id=groups,
                                object_id=o, publisher_priority=128,
                                payload=b"d" * 900)
            assert ahop.send_dgram_message(dg.serialize(prof=P18)) > 0
            await asyncio.sleep(0.002)
        want = groups * (per + 1)
        await _until(lambda: len(got[tracks["video"].fullname_hash]) == want
                     and len(got[audio]) == want + 3)
        await _until(lambda: all(len(e) == groups for e in ends.values()))
        for h, objs in got.items():
            # Streams are independent: order holds within a group only.
            streamed = [o for o in objs if o[0] < groups]
            assert sorted(streamed) == [
                (g, o, ObjectStatus.NORMAL if o < per
                 else ObjectStatus.END_OF_GROUP)
                for g in range(groups) for o in range(per + 1)]
            for g in range(groups):
                assert [o[1] for o in streamed if o[0] == g] == list(
                    range(per + 1))
            assert sorted(ends[h]) == [(g, True) for g in range(groups)]
        assert sorted(o[:2] for o in got[audio] if o[0] == groups) == [
            (groups, o) for o in range(3)]
        # Forward 0 withdraws: the publisher loses the SNS and the hop.
        source.request_update(pulls["video"].request_id, forward=0)
        video = tracks["video"].fullname_hash
        await _until(lambda: all(h != video for h, _ in hops))
        source.request_update(pulls["video"].request_id, forward=1)
        await _until(lambda: any(h == video for h, _ in hops))
        source.close()
        await _until(lambda: not hops)


@requires_certs
@pytest.mark.asyncio
async def test_dials_share_one_client_engine():
    a = _listener(_BASE_PORT + 1, "a")
    b = _listener(_BASE_PORT + 2, "b")
    ext = QuicClientEngine()
    c = FabricNode(label="c", peers=[("127.0.0.1", _BASE_PORT + 1),
                                     ("127.0.0.1", _BASE_PORT + 2)],
                   redial_interval=0.2, engine=ext)
    d = FabricNode(label="d", peers=[("127.0.0.1", _BASE_PORT + 1),
                                     ("127.0.0.1", _BASE_PORT + 2)],
                   redial_interval=0.2)
    try:
        async with a, b, c, d:
            await _until(lambda: len(c.connected()) == 2
                         and len(d.connected()) == 2)
            assert {p._quic._engine for p in c._protocols.values()} == {ext}
            engines = {p._quic._engine for p in d._protocols.values()}
            assert engines == {d._engine} and d._engine is not None
            own = d._engine
        assert d._engine is None and own._transport is None
        assert c._engine is ext and ext._transport is not None
    finally:
        ext.close()
