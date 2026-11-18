"""moq-peering data plane over localhost raw QUIC: a publishing
FabricNode, a receiving one, and between them a pure via (a test helper
here, not product logic). Two tracks, datagrams and their cap, FIN, reset
and end of group, SNS withdraw, and the information base's rules. Zero
objects received fails every data test."""
import asyncio
import json
import logging
import time

import pytest

from aiomoqt.messages import ObjectStatus
from aiomoqt.peering import FabricNode
from aiomoqt.peering.ib import (OriginTrack, best_session, reconcile,
                                subscribe_outcome)
from aiomoqt.peering.session import Learned, PeerSession
from aiomoqt.peering.wire import (MsgType, NodeInfo, PathItem, SubscribeInfo,
                                  WireError, parse_data_header, rewrite_sns)
from aiomoqt.tools import peer_bench
from aiomoqt.types import LOC_TIMESTAMP

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 33520
TIMEOUT = 10

_FLOOD = {"announce": MsgType.ANNOUNCE_INFO_ADV,
          "announce_withdrawn": MsgType.ANNOUNCE_INFO_WD,
          "subscribe": MsgType.SUBSCRIBE_INFO_ADV,
          "subscribe_withdrawn": MsgType.SUBSCRIBE_INFO_WD}


class _Via(FabricNode):
    """Test-only transit node: floods announces and subscribes, maps each
    ingress SNS to an egress SNS toward the directly connected subscriber
    node, and pipes data, rewriting the SNS id."""

    def __init__(self, **kw):
        super().__init__(**kw)
        # (ingress session, ingress sns) -> {egress session: egress sns}
        self.routes = {}
        # (ingress session, ingress stream) -> {egress session: stream}
        self.pipes = {}
        self._heads = {}
        # Flooded messages in arrival order: (ingress session id, type,
        # body), replayed to a peer that connects later.
        self.flooded = []

    def _event(self, session, ev):
        super()._event(session, ev)
        if ev.kind == "connected":
            for origin, kind, body in self.flooded:
                if origin != session.id:
                    session.send(kind, body)
            self._flush(self._protocols[session.id])
        elif ev.kind in _FLOOD:
            self.flooded.append((session.id, _FLOOD[ev.kind], ev.body))
            for other in self.connected():
                if other is not session:
                    other.send(_FLOOD[ev.kind], ev.body)
                    self._flush(self._protocols[other.id])
        elif ev.kind in ("sns", "sns_withdrawn"):
            self._route(session, ev.body, ev.kind == "sns_withdrawn")

    def _route(self, ingress, sns, withdraw):
        key = (ingress.id, sns.sns_id)
        held = self.routes.setdefault(key, {})
        for egress in self.connected():
            members = () if withdraw else tuple(
                n for n in sns.nodes
                if egress.remote and egress.remote.node_id == n)
            out = held.get(egress.id)
            if not members:
                if out is not None:
                    egress.send(MsgType.SNS_WD, out)
                    del held[egress.id]
                continue
            if out is None:
                out = held[egress.id] = type(sns)(egress.next_sns_id(),
                                                  sns.priority, members)
            egress.send(MsgType.SNS_ADV, out)
            self._flush(self._protocols[egress.id])

    def _on_stream_data(self, proto, event):
        if not event.stream_id & 2:
            return super()._on_stream_data(proto, event)
        key = (proto.session.id, event.stream_id)
        pipes = self.pipes.get(key)
        if pipes is None:
            head = self._heads.get(key, b"") + bytes(event.data)
            parsed = parse_data_header(head)
            if parsed is None:
                self._heads[key] = head
                return
            self._heads.pop(key, None)
            hdr, _ = parsed
            pipes = self.pipes[key] = {}
            for sid, out in self.routes.get(
                    (proto.session.id, hdr.sns_id), {}).items():
                egress = self._protocols[sid]
                stream = egress._quic.get_next_available_stream_id(True)
                pipes[sid] = stream
                egress._quic.send_stream_data(
                    stream, rewrite_sns(head, out.sns_id),
                    end_stream=event.end_stream)
            return
        for sid, stream in pipes.items():
            self._protocols[sid]._quic.send_stream_data(
                stream, bytes(event.data), end_stream=event.end_stream)

    def _on_stream_reset(self, proto, event):
        for sid, stream in self.pipes.pop(
                (proto.session.id, event.stream_id), {}).items():
            self._protocols[sid]._quic.reset_stream(stream, 0)

    def _on_datagram(self, proto, event):
        data = bytes(event.data)
        hdr, _ = parse_data_header(data, datagram=True)
        for sid, out in self.routes.get((proto.session.id, hdr.sns_id),
                                        {}).items():
            self._protocols[sid]._quic.send_datagram_frame(
                rewrite_sns(data, out.sns_id))


def _listener(cls, port, label):
    return cls(label=label, bind="127.0.0.1", port=port, cert=CERT, key=KEY)


def _dialer(port, label):
    return FabricNode(label=label, peers=[("127.0.0.1", port)],
                      redial_interval=0.2)


def _ext():
    return {LOC_TIMESTAMP: time.time_ns() // 1000}


def _story(nodes) -> str:
    """The nodes' events in one timeline, for a failure message."""
    skip = ("t_ms", "t_unix_ns", "kind", "node_int", "path")
    rows = sorted(
        (ev["t_unix_ns"], f"{n.local.contact} {ev['kind']} "
         f"{ {k: v for k, v in ev.items() if k not in skip} }"[:160])
        for n in nodes for ev in n.events)
    t0 = rows[0][0] if rows else 0
    return "\n".join(f"{(t - t0) / 1e6:9.1f} ms {line}" for t, line in rows)


async def _send_groups(track, groups, per_group, lanes=1, size=64):
    for g in range(groups):
        for o in range(per_group):
            await track.write(g, o % lanes, o, bytes([o % 256]) * size, _ext())
        last = per_group - 1
        for lane in range(lanes):
            await track.end_subgroup(
                g, lane, end_of_group=per_group if lane == last % lanes
                else None)


async def _until(predicate):
    """Poll a condition that several nodes' events complete."""
    async with asyncio.timeout(TIMEOUT):
        while not predicate():
            await asyncio.sleep(0.01)


async def _all_up(*nodes):
    await _until(lambda: all(n.connected() for n in nodes))


async def _received(rx, n):
    await _until(lambda: rx.stats.total_objects >= n)


@pytest.mark.asyncio
async def test_direct_pub_to_sub_two_tracks():
    port = _BASE_PORT
    pub, sub = _listener(FabricNode, port, "pub"), _dialer(port, "sub")
    async with pub, sub:
        await _all_up(pub, sub)
        video = pub.publish("d/live", "video")
        audio = pub.publish("d/live", "audio")
        rv = sub.receive("d/live", "video")
        ra = sub.receive("d/live", "audio")
        await pub.wait_until(lambda: video.subscribed and audio.subscribed,
                             TIMEOUT)
        await _send_groups(video, 3, 20, lanes=2, size=900)
        await _send_groups(audio, 6, 1)
        await _received(rv, 60)
        await _received(ra, 6)
    for rx, objects, fins in ((rv, 60, 6), (ra, 6, 6)):
        st = rx.stats
        assert st.total_objects == objects > 0
        assert (st.total_lost, st.total_dups) == (0, 0)
        assert st.lat_count == objects and st.ts_source == "loc"
        assert rx.ends["fin"] == fins and not rx.ends["reset"]
        assert rx.statuses["END_OF_GROUP"] == objects // (
            20 if rx is rv else 1)
    assert rv.stats.total_groups == 3


@pytest.mark.asyncio
async def test_pub_via_sub_two_tracks():
    port = _BASE_PORT + 1
    via = _listener(_Via, port, "via")
    pub, sub = _dialer(port, "pub"), _dialer(port, "sub")
    async with via, pub, sub:
        await _all_up(pub, sub)
        await via.wait_until(lambda: len(via.connected()) == 2, TIMEOUT)
        video = pub.publish("v/live", "video")
        audio = pub.publish("v/live", "audio")
        rv = sub.receive("v/live", "video")
        ra = sub.receive("v/live", "audio")
        await pub.wait_until(lambda: video.subscribed and audio.subscribed,
                             TIMEOUT)
        await sub.wait_until(lambda: sum(1 for _ in sub.learned_sns()) == 2,
                             TIMEOUT)
        await _send_groups(video, 4, 15, lanes=3, size=1200)
        await _send_groups(audio, 8, 1)
        await _received(rv, 60)
        await _received(ra, 8)
        (sns,) = video.origin.sns.values()
        assert sns.nodes == (sub.node_id,)
    assert rv.stats.total_objects == 60 and ra.stats.total_objects == 8
    for rx in (rv, ra):
        assert (rx.stats.total_lost, rx.stats.total_dups) == (0, 0)
        assert rx.stats.lat_count > 0
    assert rv.ends["fin"] == 12 and ra.ends["fin"] == 8


@pytest.mark.asyncio
async def test_datagrams_through_via_and_cap():
    port = _BASE_PORT + 2
    via = _listener(_Via, port, "via")
    pub, sub = _dialer(port, "pub"), _dialer(port, "sub")
    async with via, pub, sub:
        await _all_up(pub, sub)
        await via.wait_until(lambda: len(via.connected()) == 2, TIMEOUT)
        track = pub.publish("g/live", "dgram")
        rx = sub.receive("g/live", "dgram")
        await pub.wait_until(lambda: track.subscribed, TIMEOUT)
        await sub.wait_until(lambda: any(True for _ in sub.learned_sns()),
                             TIMEOUT)
        for g in range(5):
            for o in range(4):
                assert track.datagram(g, o, b"d" * 1000, _ext(),
                                      end_of_group=o == 3) == 1
            await asyncio.sleep(0.002)
        with pytest.raises(WireError):
            track.datagram(9, 0, b"d" * 1200)
        await _received(rx, 20)
    assert rx.stats.total_objects == rx.datagrams == 20
    assert rx.stats.total_lost == 0 and rx.stats.lat_count == 20
    assert track.datagrams == 20 and track.datagrams_dropped == 0


@pytest.mark.asyncio
async def test_fin_reset_and_end_of_group_through_via():
    port = _BASE_PORT + 3
    via = _listener(_Via, port, "via")
    pub, sub = _dialer(port, "pub"), _dialer(port, "sub")
    async with via, pub, sub:
        await _all_up(pub, sub)
        await via.wait_until(lambda: len(via.connected()) == 2, TIMEOUT)
        track = pub.publish("e/live", "video")
        rx = sub.receive("e/live", "video")
        await pub.wait_until(lambda: track.subscribed, TIMEOUT)
        await sub.wait_until(lambda: any(True for _ in sub.learned_sns()),
                             TIMEOUT)
        for o in range(5):
            await track.write(0, 0, o, b"a" * 50, _ext())
            await track.write(0, 1, o, b"b" * 50, _ext())
        await track.end_subgroup(0, 0, end_of_group=5)
        # A reset may discard bytes still in flight: reset once they land.
        await _received(rx, 10)
        await track.end_subgroup(0, 1, reset=True)
        await _until(lambda: rx.ends["fin"] + rx.ends["reset"] == 2)
    assert rx.ends["fin"] == 1 and rx.ends["reset"] == 1
    assert rx.statuses["END_OF_GROUP"] == 1
    assert rx.stats.total_objects == 10


@pytest.mark.asyncio
async def test_unsubscribe_withdraws_sns_and_ends_streams():
    port = _BASE_PORT + 4
    pub, sub = _listener(FabricNode, port, "pub"), _dialer(port, "sub")
    async with pub, sub:
        await _all_up(pub, sub)
        track = pub.publish("u/live", "video")
        rx = sub.receive("u/live", "video")
        await pub.wait_until(lambda: track.subscribed, TIMEOUT)
        for o in range(3):
            await track.write(0, 0, o, b"z" * 10, _ext())
        await _received(rx, 3)
        sub.unsubscribe("u/live", "video")
        await pub.wait_until(lambda: not track.subscribed, TIMEOUT)
        await sub.wait_until(lambda: not any(True for _ in sub.learned_sns()),
                             TIMEOUT)
        async with asyncio.timeout(TIMEOUT):
            while not rx.ends["fin"]:
                await asyncio.sleep(0.01)
        kinds = [e["kind"] for e in pub.events]
        assert kinds.count("sns_originated") == 1
        assert kinds.count("sns_withdrawn_originated") == 1
        await track.write(0, 0, 3, b"z" * 10, _ext())
    assert rx.stats.total_objects == 3


class _PubLate(FabricNode):
    """The bench's publisher dials 50 ms after its subscriber."""

    async def _dial_loop(self, host, port):
        if self.local.contact.endswith("-pub"):
            await asyncio.sleep(0.05)
        await super()._dial_loop(host, port)


@pytest.mark.asyncio
async def test_a_peer_connecting_late_learns_what_was_flooded(
        tmp_path, monkeypatch):
    await _bench_through_via(tmp_path, monkeypatch, _PubLate)


@pytest.mark.asyncio
async def test_peer_bench_role_both_through_via(tmp_path, monkeypatch):
    await _bench_through_via(tmp_path, monkeypatch, FabricNode)


async def _bench_through_via(tmp_path, monkeypatch, node_cls):
    port = _BASE_PORT + 5
    out = tmp_path / "bench.json"
    trace = tmp_path / "objects.jsonl"
    nodes = []

    class _Kept(node_cls):
        def __init__(self, **kw):
            super().__init__(**kw)
            nodes.append(self)
    monkeypatch.setattr(peer_bench, "FabricNode", _Kept)
    # DEBUG reaches the captured stderr a failure prints.
    peering = logging.getLogger("aiomoqt.peering")
    level = peering.level
    peering.setLevel(logging.DEBUG)
    try:
        async with _listener(_Via, port, "via") as via:
            args = peer_bench.parse_args([
                "--peer", f"127.0.0.1:{port}", "--role", "both", "-s", "512",
                "-g", "10", "-P", "2", "-r", "200", "-t", "2", "-i", "0",
                "--verify-payload", "-o", str(out),
                "--trace-objects", str(trace)])
            rc = await peer_bench.run(args)
    finally:
        peering.setLevel(level)
    assert rc == 0, _story([via, *nodes])
    report = json.loads(out.read_text())
    (track,) = report["sub"]["tracks"]
    assert track["objects"] > 0 and track["lost"] == 0
    assert track["payload_bad"] == 0
    assert track["latency_ms"]["n"] == track["objects"]
    assert track["latency_ms"]["p50"] is not None
    assert report["pub"]["published"][0]["objects"] >= track["objects"]
    lines = trace.read_text().splitlines()
    assert len(lines) >= track["objects"]
    first = json.loads(lines[0])
    assert first["t_rx_ns"] >= first["t_hdr_ns"] and first["t_tx_ns"]


@pytest.mark.asyncio
async def test_peer_bench_zero_objects_fails():
    port = _BASE_PORT + 6
    async with _listener(FabricNode, port, "empty"):
        args = peer_bench.parse_args([
            "--peer", f"127.0.0.1:{port}", "--role", "sub", "-t", "1",
            "-i", "0"])
        assert await peer_bench.run(args) == 1


@pytest.mark.parametrize("argv", [
    ["--role", "both"],
    ["--peer", "h", "--mp"],
    ["--peer", "h", "--video", "720p"],
    ["--peer", "h", "-D", "-s", "1153"],
])
def test_peer_bench_rejects(argv):
    with pytest.raises(SystemExit):
        peer_bench.parse_args(argv)


# -- information base (sans-I/O) --------------------------------------

LOCAL = NodeInfo(1, metric=1 << 32)


def _sub(source, seq=1, epoch=0, forward=1, ns=(b"t",), name=b"x"):
    from aiomoqt.peering.hashing import track_hashes
    return SubscribeInfo(source, ns, name, *track_hashes(ns, name),
                         epoch=epoch, seq=seq, forward=forward)


def test_subscribe_outcome():
    s1 = _sub(7, seq=1)
    assert subscribe_outcome(None, s1) == "learned"
    assert subscribe_outcome(s1, s1) == "duplicate"
    assert subscribe_outcome(s1, _sub(7, seq=2)) == "updated"
    assert subscribe_outcome(s1, _sub(7, seq=0)) == "stale"
    assert subscribe_outcome(s1, _sub(7, seq=2, epoch=1)) == "stale"
    assert subscribe_outcome(_sub(7, seq=65535), _sub(7, seq=0)) == "updated"
    assert subscribe_outcome(s1, _sub(7, seq=1 + 32768)) == "stale"


def _session(sid, nodes=(), subs=()):
    s = PeerSession(LOCAL, dialer=True, session_id=sid)
    s.state = "connected"
    for info in nodes:
        s.nodes[info.node_id] = Learned(info, 0, 0)
    for sub in subs:
        s.subscribes[(sub.fullname_hash, sub.source)] = Learned(sub, 0, 0)
    return s


def test_best_session_follows_path_rank():
    near = NodeInfo(9, metric=100, path=(PathItem(5, 100, 10),))
    far = NodeInfo(9, metric=100, path=(PathItem(5, 100, 10),
                                        PathItem(6, 100, 10)))
    costly = NodeInfo(9, metric=500)
    a, b, c = _session(1, [far]), _session(2, [near]), _session(3, [costly])
    assert best_session(9, [a], [a, b, c]).id == 2
    tie = _session(4, [near])
    assert best_session(9, [a], [b, tie], current=4).id == 4
    assert best_session(9, [a, c], [_session(5)]).id == 1


def test_reconcile_one_sns_per_session_and_withdraw():
    sub_a, sub_b = _sub(20), _sub(30)
    track = OriginTrack((b"t",), b"x")
    s1 = _session(1, subs=[sub_a, sub_b])
    s2 = _session(2, nodes=[NodeInfo(30, metric=1)], subs=[sub_b])
    changes = reconcile(track, [s1, s2], LOCAL.node_id, PeerSession.next_sns_id)
    assert {(c.session_id, c.sns.nodes, c.withdraw) for c in changes} == {
        (1, (20,), False), (2, (30,), False)}
    assert reconcile(track, [s1, s2], LOCAL.node_id,
                     PeerSession.next_sns_id) == []
    s2.subscribes.clear()
    s2.nodes.clear()
    changes = reconcile(track, [s1, s2], LOCAL.node_id,
                        PeerSession.next_sns_id)
    assert {(c.session_id, c.sns.nodes, c.sns.sns_id, c.withdraw)
            for c in changes} == {(2, (30,), 1, True),
                                  (1, (20, 30), 1, False)}
    s1.subscribes = {k: Learned(_sub(k[1], seq=2, forward=0), 0, 0)
                     for k in s1.subscribes}
    (ch,) = reconcile(track, [s1, s2], LOCAL.node_id,
                      PeerSession.next_sns_id)
    assert ch.withdraw and ch.session_id == 1 and track.sns == {}


def test_reconcile_ignores_own_subscribes():
    track = OriginTrack((b"t",), b"x")
    s1 = _session(1, subs=[_sub(LOCAL.node_id)])
    assert reconcile(track, [s1], LOCAL.node_id, PeerSession.next_sns_id) == []


def test_sns_ids_count_from_one_and_skip_zero():
    s = _session(1)
    assert [s.next_sns_id() for _ in range(3)] == [1, 2, 3]
    s._last_sns_id = 0xFFFFFFFE
    assert s.next_sns_id() == 1


def test_received_objects_keep_status_out_of_stats():
    from aiomoqt.peering.inner import StreamObject
    from aiomoqt.peering.node import RxTrack
    from aiomoqt.peering.session import Inbound
    rx = RxTrack((b"t",), b"x", verify_every=1)
    where = Inbound(_session(1), 3, 1, rx.fullname_hash, (10, 20), (5, 15))
    rx.on_object(where, StreamObject(0, 0, 0, ObjectStatus.NORMAL, None,
                                     b"0.0.1|"[:1], 128))
    rx.on_object(where, StreamObject(0, 0, 1, ObjectStatus.END_OF_GROUP,
                                     None, b"", 128))
    assert rx.stats.total_objects == 1
    assert rx.statuses["END_OF_GROUP"] == 1
    assert rx.header_gap_ms == [5e-6]
