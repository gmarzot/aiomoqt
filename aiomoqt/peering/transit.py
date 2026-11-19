"""Transit: the moq-fabric forwarding base of a FabricNode(transit=True).

Nodes learned from peers are re-advertised with this node appended to
the path; a node's route is the session with the best path rank. Accepted
announces and subscribes are flooded to every other session. Each ingress
SUBSCRIBE_NODE_SET maps to one egress set per next-hop session, and data
is forwarded as raw chunks: a stream's NEW_STREAM header and a datagram's
header get the egress SNS id, every other byte is copied. Data that
arrives before its SNS waits in a bounded stash.

When a route moves, new streams take the new next hop and streams in
flight finish on the old one, whose set is withdrawn once they end.
Tracks this node receives are decoded by the session as on a leaf.
"""
import asyncio
import dataclasses
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Deque, Dict, List, Optional, Set, Tuple

from aiomoqt.peering.hashing import node_id_format
from aiomoqt.peering.ib import best_route, seq_newer
from aiomoqt.peering.session import PeerSession, SessionEvent, log
from aiomoqt.peering.wire import (AnnounceInfo, DataHeader, MsgType, NodeInfo,
                                  NodeSet, PathItem, SubscribeInfo,
                                  WireError, parse_data_header, rewrite_sns)

if TYPE_CHECKING:
    from aiomoqt.peering.node import FabricNode, _PeerProtocol

# Data held per session for SNS ids not yet advertised.
STASH_MAX_BYTES = 1 << 20
STASH_MAX_ITEMS = 1024

Key = Tuple[int, int]


class _Out:
    """One egress stream: written in order, waiting out backpressure."""
    __slots__ = ("proto", "stream_id", "queue", "task", "ended")

    def __init__(self, proto: "_PeerProtocol", stream_id: int):
        self.proto = proto
        self.stream_id = stream_id
        self.queue: Deque[Tuple[bytes, bool, Optional[int]]] = deque()
        self.task: Optional[asyncio.Task] = None
        self.ended = False

    def write(self, data: bytes, fin: bool = False,
              priority: Optional[int] = None) -> None:
        if self.ended or self.proto.terminated.is_set():
            return
        self.ended = fin
        if not self.queue:
            kw = {} if priority is None else {"priority": priority}
            try:
                self.proto._quic.send_stream_data(self.stream_id, data,
                                                  end_stream=fin, **kw)
                self.proto.tx_bytes += len(data)
                return
            except BufferError:
                pass
            except (AssertionError, AttributeError, ConnectionError) as e:
                log.debug(f"peering transit: stream {self.stream_id} "
                          f"write: {e!r}")
                self.ended = True
                return
        self.queue.append((data, fin, priority))
        if self.task is None or self.task.done():
            self.task = asyncio.ensure_future(self._drain())

    async def _drain(self) -> None:
        quic = self.proto._quic
        while self.queue:
            data, fin, priority = self.queue[0]
            kw = {} if priority is None else {"priority": priority}
            try:
                await quic.send_stream_data_drained(self.stream_id, data,
                                                    end_stream=fin, **kw)
            except (asyncio.CancelledError, AssertionError, AttributeError,
                    ConnectionError):
                self.queue.clear()
                self.ended = True
                return
            self.queue.popleft()
            self.proto.tx_bytes += len(data)

    def reset(self, code: int = 0) -> None:
        self.queue.clear()
        if self.task is not None and not self.task.done():
            self.task.cancel()
        if self.proto.terminated.is_set():
            return
        try:
            self.proto._quic.reset_stream(self.stream_id, code)
        except (AssertionError, AttributeError, BufferError,
                ConnectionError) as e:
            log.debug(f"peering transit: stream {self.stream_id} reset: "
                      f"{e!r}")
        self.ended = True


class Egress:
    """One ingress set's share on one next-hop session. A draining one
    opens no streams and is withdrawn once its streams end."""
    __slots__ = ("session_id", "sns", "streams", "drain", "stream_count",
                 "bytes")

    def __init__(self, session_id: int, sns: NodeSet):
        self.session_id = session_id
        self.sns = sns
        # Ingress stream id -> its egress stream.
        self.streams: Dict[int, _Out] = {}
        self.drain = False
        self.stream_count = 0
        self.bytes = 0


class Ingress:
    """An SNS a peer holds toward this node: (session, sns id)."""
    __slots__ = ("session_id", "sns", "egress", "withdrawn", "local",
                 "hashes", "streams", "bytes")

    def __init__(self, session_id: int, sns: NodeSet):
        self.session_id = session_id
        self.sns = sns
        self.egress: Dict[int, Egress] = {}
        self.withdrawn = False
        # This node is in the set; the session decodes for its sinks.
        self.local = False
        # Fullname hashes seen on it.
        self.hashes: Set[int] = set()
        self.streams = 0
        self.bytes = 0


class _In:
    """One ingress data stream."""
    __slots__ = ("buf", "header", "ingress", "outs", "stash", "fin",
                 "dropped")

    def __init__(self):
        self.buf = bytearray()
        self.header: Optional[DataHeader] = None
        self.ingress: Optional[Ingress] = None
        self.outs: List[Tuple[Egress, _Out]] = []
        # Chunks held while the SNS is unknown.
        self.stash: Optional[List[bytes]] = None
        self.fin = False
        self.dropped = False


class Transit:
    """The forwarding state of one FabricNode."""

    def __init__(self, node: "FabricNode"):
        self.node = node
        # Node id -> session id of its route.
        self.best: Dict[int, int] = {}
        # Node id -> (route session id, advertisement) last propagated.
        self.advertised: Dict[int, Tuple[int, NodeInfo]] = {}
        # Accepted learned announces and subscribes, by (hash, source).
        self.announces: Dict[Key, AnnounceInfo] = {}
        self.subscribes: Dict[Key, SubscribeInfo] = {}
        # (hash, source) -> (epoch, seq, withdrawn) last accepted.
        self._sub_marks: Dict[Key, Tuple[int, int, bool]] = {}
        self.ingress: Dict[Key, Ingress] = {}
        self._in: Dict[Key, _In] = {}
        # Session id -> sns id -> stashed ("s", stream id) / ("d", bytes).
        self._stash: Dict[int, Dict[int, list]] = defaultdict(dict)
        self._stash_bytes: Dict[int, int] = defaultdict(int)
        self._stash_items: Dict[int, int] = defaultdict(int)
        self.stash_dropped = 0
        self.unrouted = 0
        self.datagrams = 0
        self.datagrams_dropped = 0

    @property
    def node_id(self) -> int:
        return self.node.node_id

    # -- control events ------------------------------------------------

    def on_event(self, session: PeerSession, ev: SessionEvent) -> None:
        kind = ev.kind
        if kind == "connected":
            self._select(session.remote.node_id if session.remote else 0)
            self._sync(session)
        elif kind in ("node", "node_withdrawn"):
            self._select(ev.body.node_id)
        elif kind == "announce":
            self._announce(session, ev.body)
        elif kind == "announce_withdrawn":
            self._announce_withdrawn(session, ev.body)
        elif kind == "subscribe":
            self._subscribe(session, ev.body)
        elif kind == "subscribe_withdrawn":
            self._subscribe_withdrawn(session, ev.body)
        elif kind == "sns":
            self._sns(session, ev.body)
        elif kind == "sns_withdrawn":
            self._sns_withdrawn(session, ev.body)

    def _send(self, session: PeerSession, msg_type: MsgType, body) -> None:
        if session.state != "connected":
            return
        session.send(msg_type, body)
        proto = self.node._protocols.get(session.id)
        if proto is not None:
            self.node._flush(proto)

    def _flood(self, origin: Optional[PeerSession], msg_type: MsgType,
               body) -> None:
        for s in self.node.connected():
            if s is not origin:
                self._send(s, msg_type, body)

    # -- nodes ---------------------------------------------------------

    def _forwarded(self, info: NodeInfo, via: int, to: PeerSession
                   ) -> Optional[NodeInfo]:
        """`info` learned on session `via` as sent to `to`, this node
        appended to its path; None where the loop rule skips it."""
        remote = to.remote
        if remote is None or to.id == via:
            return None
        rid = remote.node_id
        if rid == info.node_id or any(p.node_id == rid for p in info.path):
            return None
        arrival = self.node.sessions.get(via)
        srtt = (arrival.srtt_us or 0) if arrival is not None else 0
        item = PathItem(self.node_id, srtt, self.node.advertised_metric(to.id))
        return dataclasses.replace(info, path=info.path + (item,))

    def _propagate(self, via: int, info: NodeInfo,
                   withdraw: bool = False) -> None:
        kind = MsgType.NODE_INFO_WD if withdraw else MsgType.NODE_INFO_ADV
        for s in self.node.connected():
            out = self._forwarded(info, via, s)
            if out is not None:
                self._send(s, kind, out)

    def _route(self, node_id: int) -> Optional[PeerSession]:
        sid = self.best.get(node_id)
        return None if sid is None else self.node.sessions.get(sid)

    def _select(self, node_id: int) -> None:
        """Re-rank a node's routes. A new route or advertisement is
        propagated, a lost one withdrawn, and sets through it remapped."""
        if not node_id or node_id == self.node_id:
            return
        current = self.best.get(node_id)
        s = best_route(node_id, self.node.connected(), current,
                       cost=self.node._session_cost, local_id=self.node_id)
        if s is None:
            self.best.pop(node_id, None)
            held = self.advertised.pop(node_id, None)
            if held is not None:
                log.info(f"peering transit: node {node_id_format(node_id)} "
                         f"unreachable; withdrawn")
                self._propagate(held[0], held[1], withdraw=True)
                self._source_gone(node_id)
        else:
            self.best[node_id] = s.id
            info = s.nodes[node_id].body
            if self.advertised.get(node_id) != (s.id, info):
                if current != s.id:
                    log.info(f"peering transit: node "
                             f"{node_id_format(node_id)} via {s.tag}")
                self.advertised[node_id] = (s.id, info)
                self._propagate(s.id, info)
        if self.best.get(node_id) != current:
            for ing in list(self.ingress.values()):
                if node_id in ing.sns.nodes:
                    self._map(ing)

    def reselect(self) -> None:
        """Re-rank every node, as after a session metric change."""
        for node_id in list(set(self.best) | set(self.advertised)):
            self._select(node_id)

    def _sync(self, session: PeerSession) -> None:
        """A new session gets the known nodes, then the learned announces
        and subscribes not sourced by its peer."""
        self.sync_nodes(session)
        rid = session.remote.node_id if session.remote else None
        for (_, source), ann in self.announces.items():
            if source != rid:
                self._send(session, MsgType.ANNOUNCE_INFO_ADV, ann)
        for (_, source), sub in self.subscribes.items():
            if source != rid:
                self._send(session, MsgType.SUBSCRIBE_INFO_ADV, sub)

    def sync_nodes(self, session: PeerSession) -> None:
        for via, info in list(self.advertised.values()):
            out = self._forwarded(info, via, session)
            if out is not None:
                self._send(session, MsgType.NODE_INFO_ADV, out)

    def _source_gone(self, node_id: int) -> None:
        """Withdraw what an unreachable node announced and subscribed."""
        for key in [k for k in self.subscribes if k[1] == node_id]:
            sub = self.subscribes.pop(key)
            self._sub_marks.pop(key, None)
            self._flood(None, MsgType.SUBSCRIBE_INFO_WD, sub)
        for key in [k for k in self.announces if k[1] == node_id]:
            self._flood(None, MsgType.ANNOUNCE_INFO_WD,
                        self.announces.pop(key))

    # -- announces and subscribes -----------------------------------------

    def _announce(self, session: PeerSession, ann: AnnounceInfo) -> None:
        key = (ann.fullname_hash, ann.source)
        if ann.source == self.node_id or key in self.announces:
            return
        self.announces[key] = ann
        self._flood(session, MsgType.ANNOUNCE_INFO_ADV, ann)

    def _announce_withdrawn(self, session: PeerSession,
                            ann: AnnounceInfo) -> None:
        if self.announces.pop((ann.fullname_hash, ann.source), None) is None:
            return
        self._flood(session, MsgType.ANNOUNCE_INFO_WD, ann)

    def _subscribe(self, session: PeerSession, sub: SubscribeInfo) -> None:
        if sub.source == self.node_id:
            return
        key = (sub.fullname_hash, sub.source)
        mark = self._sub_marks.get(key)
        if mark is not None:
            epoch, seq, withdrawn = mark
            if sub.epoch != epoch:
                return
            resume = (sub.seq == seq and not withdrawn
                      and key not in self.subscribes)
            if not seq_newer(sub.seq, seq) and not resume:
                return
        self.subscribes[key] = sub
        self._sub_marks[key] = (sub.epoch, sub.seq, False)
        self._flood(session, MsgType.SUBSCRIBE_INFO_ADV, sub)

    def _subscribe_withdrawn(self, session: PeerSession,
                             sub: SubscribeInfo) -> None:
        key = (sub.fullname_hash, sub.source)
        held = self.subscribes.get(key)
        if held is None or held.epoch != sub.epoch or not (
                sub.seq == held.seq or seq_newer(sub.seq, held.seq)):
            return
        del self.subscribes[key]
        self._sub_marks[key] = (sub.epoch, sub.seq, True)
        self._flood(session, MsgType.SUBSCRIBE_INFO_WD, sub)

    # -- subscribe node sets ---------------------------------------------

    def _sns(self, session: PeerSession, sns: NodeSet) -> None:
        key = (session.id, sns.sns_id)
        ing = self.ingress.get(key)
        new = ing is None
        if ing is None:
            ing = self.ingress[key] = Ingress(session.id, sns)
        ing.sns = sns
        ing.withdrawn = False
        ing.local = self.node_id in sns.nodes
        self._map(ing)
        if new:
            self._replay(session, ing)

    def _map(self, ing: Ingress) -> None:
        """One egress set per next-hop session reaching the ingress set's
        nodes. A set no longer wanted drains its open streams first."""
        want: Dict[int, Set[int]] = defaultdict(set)
        if not ing.withdrawn:
            for node_id in ing.sns.nodes:
                if node_id == self.node_id:
                    continue
                s = self._route(node_id)
                if s is None or s.state != "connected":
                    self.unrouted += 1
                    log.debug(f"peering transit: no route to "
                              f"{node_id_format(node_id)} for sns "
                              f"{ing.sns.sns_id} of session "
                              f"{ing.session_id}; dropped")
                    continue
                if s.id != ing.session_id:
                    want[s.id].add(node_id)
        for sid, nodes in want.items():
            members = tuple(sorted(nodes))
            eg = ing.egress.get(sid)
            session = self.node.sessions[sid]
            if eg is None:
                eg = ing.egress[sid] = Egress(sid, NodeSet(
                    session.next_sns_id(), ing.sns.priority, members))
            elif (eg.sns.nodes == members and not eg.drain
                  and eg.sns.priority == ing.sns.priority):
                continue
            else:
                eg.sns = NodeSet(eg.sns.sns_id, ing.sns.priority, members)
                eg.drain = False
            self._send(session, MsgType.SNS_ADV, eg.sns)
        for sid, eg in list(ing.egress.items()):
            if sid in want:
                continue
            if eg.streams:
                eg.drain = True
            else:
                self._release(ing, eg)

    def _release(self, ing: Ingress, eg: Egress) -> None:
        if ing.egress.get(eg.session_id) is eg:
            del ing.egress[eg.session_id]
        session = self.node.sessions.get(eg.session_id)
        if session is not None:
            self._send(session, MsgType.SNS_WD, eg.sns)
        if ing.withdrawn and not ing.egress and \
                self.ingress.get((ing.session_id, ing.sns.sns_id)) is ing:
            del self.ingress[(ing.session_id, ing.sns.sns_id)]

    def _sns_withdrawn(self, session: PeerSession, sns: NodeSet) -> None:
        key = (session.id, sns.sns_id)
        self._stash_drop(session.id, sns.sns_id)
        ing = self.ingress.get(key)
        if ing is None:
            return
        ing.withdrawn = True
        self._map(ing)
        if not ing.egress:
            self.ingress.pop(key, None)

    # -- data ----------------------------------------------------------

    def stream_data(self, session: PeerSession, stream_id: int, data: bytes,
                    fin: bool) -> None:
        key = (session.id, stream_id)
        st = self._in.get(key)
        if st is None:
            st = self._in[key] = _In()
        if st.dropped:
            if fin:
                self._in.pop(key, None)
            return
        if st.header is None:
            st.buf += data
            try:
                parsed = parse_data_header(st.buf)
            except WireError:
                # The session logs the malformed stream.
                st.dropped = True
                parsed = None
            if parsed is None:
                if fin:
                    self._in.pop(key, None)
                return
            st.header = parsed[0]
            head, st.buf = bytes(st.buf), bytearray()
            ing = self.ingress.get((session.id, st.header.sns_id))
            if ing is None:
                st.stash = []
                self._stash_add(session, st, ("s", stream_id), head)
                st.fin = fin
                return
            self._open(ing, st, stream_id, head, fin)
            return
        if st.stash is not None:
            self._stash_add(session, st, None, data)
            st.fin = fin
            return
        ing = st.ingress
        if ing is not None:
            ing.bytes += len(data)
        for eg, out in st.outs:
            eg.bytes += len(data)
            out.write(data, fin)
        if fin:
            self._end(st, stream_id, key)

    def _open(self, ing: Ingress, st: _In, stream_id: int, head: bytes,
              fin: bool) -> None:
        hdr = st.header
        assert hdr is not None
        st.ingress = ing
        ing.hashes.add(hdr.fullname_hash)
        ing.streams += 1
        ing.bytes += len(head)
        if not ing.withdrawn:
            for eg in ing.egress.values():
                if eg.drain:
                    continue
                proto = self.node._protocols.get(eg.session_id)
                if proto is None or proto.terminated.is_set():
                    continue
                out = _Out(proto,
                           proto._quic.get_next_available_stream_id(True))
                eg.streams[stream_id] = out
                eg.stream_count += 1
                proto.tx_streams += 1
                eg.bytes += len(head)
                st.outs.append((eg, out))
                out.write(rewrite_sns(head, eg.sns.sns_id), fin,
                          priority=hdr.priority)
        if fin:
            self._end(st, stream_id, (ing.session_id, stream_id))

    def _end(self, st: _In, stream_id: int, key: Key,
             reset: Optional[int] = None) -> None:
        self._in.pop(key, None)
        ing = st.ingress
        for eg, out in st.outs:
            if reset is not None:
                out.reset(reset)
            if eg.streams.get(stream_id) is out:
                del eg.streams[stream_id]
            if eg.drain and not eg.streams and ing is not None:
                self._release(ing, eg)
        st.outs = []

    def stream_reset(self, session: PeerSession, stream_id: int,
                     code: int) -> None:
        key = (session.id, stream_id)
        st = self._in.get(key)
        if st is None:
            return
        if st.stash is not None:
            st.dropped = True
            self._in.pop(key, None)
            return
        self._end(st, stream_id, key, reset=code)

    def datagram(self, session: PeerSession, data: bytes) -> None:
        try:
            hdr, _ = parse_data_header(data, datagram=True)
        except WireError:
            return
        ing = self.ingress.get((session.id, hdr.sns_id))
        if ing is None:
            self._stash_add(session, None, ("d", hdr.sns_id), data)
            return
        self._forward_datagram(ing, hdr.fullname_hash, data)

    def _forward_datagram(self, ing: Ingress, fullname_hash: int,
                          data: bytes) -> None:
        ing.hashes.add(fullname_hash)
        ing.bytes += len(data)
        if ing.withdrawn:
            return
        for eg in ing.egress.values():
            if eg.drain:
                continue
            proto = self.node._protocols.get(eg.session_id)
            if proto is None or proto.terminated.is_set():
                continue
            if proto._quic.send_datagram_frame(rewrite_sns(data,
                                                           eg.sns.sns_id)):
                self.datagrams += 1
                proto.tx_datagrams += 1
                eg.bytes += len(data)
            else:
                self.datagrams_dropped += 1

    # -- stash -----------------------------------------------------------

    def _stash_add(self, session: PeerSession, st: Optional[_In],
                   item: Optional[tuple], data: bytes) -> None:
        sid = session.id
        full = (self._stash_bytes[sid] + len(data) > STASH_MAX_BYTES
                or (item is not None
                    and self._stash_items[sid] >= STASH_MAX_ITEMS))
        if full:
            self.stash_dropped += 1
            (log.warning if self.stash_dropped == 1 else log.debug)(
                f"peering transit: {session.tag} data before its SNS over "
                f"the stash bound ({STASH_MAX_BYTES} bytes, "
                f"{STASH_MAX_ITEMS} items); dropped "
                f"{self.stash_dropped} so far")
            if st is not None:
                st.dropped = True
                if st.stash:
                    self._stash_bytes[sid] -= sum(len(c) for c in st.stash)
                st.stash = None
            return
        self._stash_bytes[sid] += len(data)
        if st is not None:
            assert st.stash is not None
            st.stash.append(data)
        if item is not None:
            self._stash_items[sid] += 1
            hdr = st.header if st is not None else None
            sns_id = hdr.sns_id if hdr is not None else item[1]
            self._stash[sid].setdefault(sns_id, []).append(
                item if item[0] == "s" else ("d", data))

    def _stash_drop(self, sid: int, sns_id: int) -> None:
        for kind, value in self._stash.get(sid, {}).pop(sns_id, []):
            self._stash_items[sid] -= 1
            if kind == "d":
                self._stash_bytes[sid] -= len(value)
                continue
            st = self._in.pop((sid, value), None)
            if st is not None and st.stash:
                self._stash_bytes[sid] -= sum(len(c) for c in st.stash)

    def _replay(self, session: PeerSession, ing: Ingress) -> None:
        sid = session.id
        items = self._stash.get(sid, {}).pop(ing.sns.sns_id, [])
        for kind, value in items:
            self._stash_items[sid] -= 1
            if kind == "d":
                self._stash_bytes[sid] -= len(value)
                hdr, _ = parse_data_header(value, datagram=True)
                self._forward_datagram(ing, hdr.fullname_hash, value)
                continue
            st = self._in.get((sid, value))
            if st is None or st.stash is None:
                continue
            chunks, st.stash = st.stash, None
            self._stash_bytes[sid] -= sum(len(c) for c in chunks)
            self._open(ing, st, value, b"".join(chunks), st.fin)

    # -- sessions --------------------------------------------------------

    def session_closed(self, session: PeerSession,
                       learned: List[int]) -> None:
        """Forget a closed session's sets and streams, then re-route the
        nodes it reached."""
        sid = session.id
        for key, ing in list(self.ingress.items()):
            if ing.session_id == sid:
                for eg in list(ing.egress.values()):
                    for out in eg.streams.values():
                        out.reset(0)
                    eg.streams.clear()
                ing.withdrawn = True
                for eg in list(ing.egress.values()):
                    self._release(ing, eg)
                self.ingress.pop(key, None)
            else:
                ing.egress.pop(sid, None)
        for key in [k for k in self._in if k[0] == sid]:
            del self._in[key]
        self._stash.pop(sid, None)
        self._stash_bytes.pop(sid, None)
        self._stash_items.pop(sid, None)
        for node_id in learned:
            self._select(node_id)
        for node_id in [n for n, s in self.best.items() if s == sid]:
            self._select(node_id)
        for ing in list(self.ingress.values()):
            self._map(ing)

    # -- views -----------------------------------------------------------

    def open_streams(self, session_id: int) -> int:
        """Egress streams open toward a session."""
        return sum(len(eg.streams) for ing in self.ingress.values()
                   for sid, eg in ing.egress.items() if sid == session_id)

    def routes(self) -> List[Tuple[int, int, NodeInfo]]:
        """(node id, route session id, its advertisement) per known node."""
        return [(n, s, info) for n, (s, info) in self.advertised.items()]
