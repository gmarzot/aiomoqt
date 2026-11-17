"""Sans-I/O moq-fabric session: one QUIC connection to one peer.

Bytes and stream events go in; parsed state, SessionEvents and control
bytes to send (take_output) come out. The session records what it learns
through this peer and forwards none of it: the node is a leaf.
"""
import time
from collections import Counter
from dataclasses import dataclass
from typing import (Any, Callable, Container, Dict, List, NamedTuple,
                    Optional, Tuple)

from aiomoqt.peering.hashing import node_id_format, track_hashes
from aiomoqt.peering.ib import subscribe_outcome
from aiomoqt.peering.inner import SubgroupReader, decode_datagram
from aiomoqt.peering.report import describe, hash_text
from aiomoqt.peering.wire import (AnnounceInfo, Connect, ConnectCode,
                                  ConnectResponse, ControlReader, DataHeader,
                                  Message, MsgType, NodeInfo, NodeSet,
                                  SubscribeInfo, WireError, encode_message,
                                  parse_data_header)
from aiomoqt.utils.logger import TRACE, get_category_logger

log = get_category_logger("peering")

Stamp = Tuple[int, int]


def now_stamp() -> Stamp:
    """(monotonic ns, wall-clock ns) at one instant."""
    return time.monotonic_ns(), time.time_ns()


@dataclass
class Learned:
    """A record learned from a peer, stamped when its bytes arrived."""
    body: Any
    mono_ns: int
    unix_ns: int
    hash_ok: Optional[bool] = None
    updates: int = 0


@dataclass(frozen=True)
class SessionEvent:
    kind: str
    stamp: Stamp
    body: Any = None
    hash_ok: Optional[bool] = None
    note: str = ""


@dataclass
class DataFlow:
    """Peering data received for one track hash; the inner bytes are not
    decoded."""
    first_mono_ns: int
    first_unix_ns: int
    sns: int = 0
    streams: int = 0
    datagrams: int = 0
    bytes: int = 0


_MAX_SNS_ID = 0xFFFFFFFE


class Inbound(NamedTuple):
    """Where and when an inner object arrived: the chunk's stamp, and for
    a stream the stamp of its NEW_STREAM header."""
    session: "PeerSession"
    stream_id: Optional[int]
    sns_id: int
    fullname_hash: int
    stamp: Stamp
    header_stamp: Optional[Stamp]


class _InStream:
    __slots__ = ("buf", "header", "bad", "reader", "header_stamp")

    def __init__(self):
        self.buf = bytearray()
        self.header: Optional[DataHeader] = None
        self.bad = False
        self.reader: Optional[SubgroupReader] = None
        self.header_stamp: Optional[Stamp] = None


def _client_bidi(stream_id: int) -> bool:
    return stream_id & 3 == 0


class PeerSession:
    """States: quic-up -> connecting (dialer, CONNECT sent) -> connected
    -> closed. An acceptor goes quic-up -> connected on CONNECT."""

    def __init__(self, local: NodeInfo, *, dialer: bool, session_id: int = 0,
                 peer: str = "", clock: Callable[[], Stamp] = now_stamp,
                 start: Optional[Stamp] = None, strict: bool = False,
                 on_event: Optional[Callable[["PeerSession", SessionEvent],
                                             None]] = None):
        self.local = local
        self.dialer = dialer
        self.id = session_id
        self.peer = peer
        self._clock = clock
        # Takes control events as they happen; unset, control_data()
        # returns them.
        self.on_event = on_event
        # Inner objects of the hashes in `decode` go to on_object; their
        # streams' ends ("fin", "reset" or "bad") to on_stream_end.
        self.decode: Container[int] = ()
        self.on_object: Optional[Callable[[Inbound, Any], None]] = None
        self.on_stream_end: Optional[
            Callable[["PeerSession", int, int, str], None]] = None
        self._last_sns_id = 0
        self.start_mono_ns, self.start_unix_ns = start or clock()
        self.state = "quic-up"
        self.remote: Optional[NodeInfo] = None
        self.control_stream: Optional[int] = None
        self.reader = ControlReader(strict=strict)
        self.connect_ms: Optional[float] = None
        self.srtt_us: Optional[int] = None
        self.close_reason: Optional[str] = None
        self.closed_stamp: Optional[Stamp] = None
        self.rx_msgs: Counter = Counter()
        self.tx_msgs: Counter = Counter()
        self.nodes: Dict[int, Learned] = {}
        self.announces: Dict[Tuple[int, int], Learned] = {}
        self.subscribes: Dict[Tuple[int, int], Learned] = {}
        self.sns_in: Dict[int, Learned] = {}
        self.flows: Dict[int, DataFlow] = {}
        self.hash_mismatches = 0
        self.duplicates = 0
        self.stale = 0
        self.self_echoes = 0
        self.stray = 0
        self.bad_data = 0
        self._streams: Dict[int, _InStream] = {}
        self._out: List[bytes] = []
        self._rejected_seen = 0

    def __repr__(self) -> str:
        return f"PeerSession({self.id}, {self.peer}, {self.state})"

    @property
    def tag(self) -> str:
        return f"session={self.id} peer={self.peer}"

    # -- output ------------------------------------------------------

    def send(self, msg_type: MsgType, body: Any) -> None:
        self._out.append(encode_message(msg_type, body))
        self.tx_msgs[msg_type.name] += 1
        log.info(f"peering send: {msg_type.name} {self.tag} {describe(body)}")

    def take_output(self) -> List[bytes]:
        out, self._out = self._out, []
        return out

    def start(self, control_stream: int) -> None:
        """Dialer: open the control stream with CONNECT."""
        self.control_stream = control_stream
        self.state = "connecting"
        self.send(MsgType.CONNECT, Connect(self.local))

    # -- control plane -----------------------------------------------

    def control_data(self, stream_id: int, data) -> List[SessionEvent]:
        if stream_id != self.control_stream:
            if self.dialer or not _client_bidi(stream_id):
                self.stray += 1
                return []
            # The acceptor follows the bidi stream that last delivered
            # bytes, as the library does.
            self.control_stream = stream_id
        stamp = self._clock()
        if log.isEnabledFor(TRACE):
            log.log(TRACE, f"peering control chunk: {self.tag} "
                    f"stream={stream_id} len={len(data)} {bytes(data).hex()}")
        msgs = self.reader.feed(data)
        if self.reader.rejected != self._rejected_seen:
            self._rejected_seen = self.reader.rejected
            log.warning(f"peering: {self.tag} control deviation: "
                        f"{self.reader.last_error}")
        events: List[SessionEvent] = []
        for msg in msgs:
            self.rx_msgs[msg.type.name] += 1
            found: List[SessionEvent] = []
            outcome = self._handle(msg, stamp, found)
            log.info(f"peering message parsed: {msg.type.name} {self.tag} "
                     f"{describe(msg.body)} -> {outcome}")
            if msg.type == MsgType.CONNECT:
                self.send(MsgType.CONNECT_RESPONSE,
                          ConnectResponse(ConnectCode.OK, self.local))
            if self.on_event is None:
                events.extend(found)
            else:
                for ev in found:
                    self.on_event(self, ev)
        return events

    def _handle(self, msg: Message, stamp: Stamp,
                events: List[SessionEvent]) -> str:
        t, body = msg.type, msg.body
        if isinstance(body, Connect):
            return self._up(body.node, stamp, events)
        if isinstance(body, ConnectResponse):
            if body.code != ConnectCode.OK or body.node is None:
                events.append(SessionEvent("connect_error", stamp, body))
                return f"refused code {body.code}"
            return self._up(body.node, stamp, events)
        if isinstance(body, NodeInfo):
            return self._node(body, stamp, events,
                              t == MsgType.NODE_INFO_WD)
        if isinstance(body, AnnounceInfo):
            return self._announce(body, stamp, events,
                                  t == MsgType.ANNOUNCE_INFO_WD)
        if isinstance(body, SubscribeInfo):
            return self._subscribe(body, stamp, events,
                                   t == MsgType.SUBSCRIBE_INFO_WD)
        return self._sns(body, stamp, events, t == MsgType.SNS_WD)

    def _up(self, node: NodeInfo, stamp: Stamp,
            events: List[SessionEvent]) -> str:
        self.remote = node
        self.nodes[node.node_id] = Learned(node, *stamp)
        if self.state == "connected":
            return "repeat"
        self.state = "connected"
        self.connect_ms = round((stamp[0] - self.start_mono_ns) / 1e6, 3)
        events.append(SessionEvent("connected", stamp, node))
        return "connected"

    def _node(self, node: NodeInfo, stamp: Stamp, events, withdraw) -> str:
        if node.node_id == self.local.node_id:
            self.self_echoes += 1
            return "self, ignored"
        prev = self.nodes.get(node.node_id)
        if withdraw:
            if prev is None:
                return "unknown"
            del self.nodes[node.node_id]
            events.append(SessionEvent("node_withdrawn", stamp, node))
            return "withdrawn"
        self.nodes[node.node_id] = Learned(
            node, *stamp, updates=prev.updates + 1 if prev else 0)
        events.append(SessionEvent("node", stamp, node,
                                   note="update" if prev else ""))
        return "updated" if prev else "learned"

    def _announce(self, ann: AnnounceInfo, stamp: Stamp, events,
                  withdraw) -> str:
        ok = track_hashes(ann.namespace, ann.name).fullname \
            == ann.fullname_hash
        key = (ann.fullname_hash, ann.source)
        if withdraw:
            if self.announces.pop(key, None) is None:
                return "unknown"
            events.append(SessionEvent("announce_withdrawn", stamp, ann, ok))
            return "withdrawn"
        if not ok:
            self.hash_mismatches += 1
        if key in self.announces:
            self.duplicates += 1
            return "duplicate"
        self.announces[key] = Learned(ann, *stamp, hash_ok=ok)
        events.append(SessionEvent("announce", stamp, ann, ok))
        return "learned" + ("" if ok else f" HASH MISMATCH {self._want(ann)}")

    def _subscribe(self, sub: SubscribeInfo, stamp: Stamp, events,
                   withdraw) -> str:
        want = track_hashes(sub.namespace, sub.name)
        ok = want == (sub.namespace_hash, sub.name_hash, sub.fullname_hash)
        key = (sub.fullname_hash, sub.source)
        prev = self.subscribes.get(key)
        if withdraw:
            if prev is None:
                return "unknown"
            del self.subscribes[key]
            events.append(SessionEvent("subscribe_withdrawn", stamp, sub, ok))
            return "withdrawn"
        if not ok:
            self.hash_mismatches += 1
        outcome = subscribe_outcome(prev.body if prev else None, sub)
        if outcome == "duplicate":
            self.duplicates += 1
            return outcome
        if outcome == "stale" and prev is not None:
            self.stale += 1
            return (f"stale (have epoch {prev.body.epoch} "
                    f"seq {prev.body.seq})")
        self.subscribes[key] = Learned(
            sub, *stamp, hash_ok=ok,
            updates=prev.updates + 1 if prev else 0)
        events.append(SessionEvent("subscribe", stamp, sub, ok,
                                   note="update" if prev else ""))
        return outcome + ("" if ok else f" HASH MISMATCH {self._want(sub)}")

    @staticmethod
    def _want(body) -> str:
        want = track_hashes(body.namespace, body.name)
        return (f"(computed ns={hash_text(want.namespace)} "
                f"name={hash_text(want.name)} "
                f"full={hash_text(want.fullname)})")

    def _sns(self, sns: NodeSet, stamp: Stamp, events, withdraw) -> str:
        prev = self.sns_in.get(sns.sns_id)
        if withdraw:
            if prev is None:
                return "unknown"
            del self.sns_in[sns.sns_id]
            events.append(SessionEvent("sns_withdrawn", stamp, prev.body))
            return "withdrawn"
        self.sns_in[sns.sns_id] = Learned(
            sns, *stamp, updates=prev.updates + 1 if prev else 0)
        events.append(SessionEvent("sns", stamp, sns,
                                   note="update" if prev else ""))
        mine = self.local.node_id in sns.nodes
        return ("updated" if prev else "learned") + (
            "" if mine else f", {node_id_format(self.local.node_id)} not in set")

    def next_sns_id(self) -> int:
        """This session's next egress SNS id: from 1, skipping 0 on wrap."""
        self._last_sns_id = (self._last_sns_id + 1
                             if self._last_sns_id < _MAX_SNS_ID else 1)
        return self._last_sns_id

    # -- data plane in ----------------------------------------------

    def _flow(self, hdr: DataHeader, stamp: Stamp, events) -> DataFlow:
        flow = self.flows.get(hdr.fullname_hash)
        if flow is None:
            flow = self.flows[hdr.fullname_hash] = DataFlow(*stamp)
            events.append(SessionEvent("data_first", stamp, hdr))
        flow.sns = hdr.sns_id
        return flow

    def stream_data(self, stream_id: int, data, fin: bool
                    ) -> List[SessionEvent]:
        events: List[SessionEvent] = []
        st = self._streams.get(stream_id)
        if st is None:
            st = self._streams[stream_id] = _InStream()
        if log.isEnabledFor(TRACE):
            log.log(TRACE, f"peering data chunk: {self.tag} "
                    f"stream={stream_id} len={len(data)} fin={fin}")
        stamp = self._clock()
        inner = data
        if st.header is not None:
            self.flows[st.header.fullname_hash].bytes += len(data)
        elif not st.bad:
            st.buf += data
            inner = b""
            try:
                parsed = parse_data_header(st.buf)
            except WireError as e:
                st.bad, st.buf = True, bytearray()
                self.bad_data += 1
                log.warning(f"peering: {self.tag} data stream {stream_id} "
                            f"ignored: {e}")
                parsed = None
            if parsed is not None:
                hdr, offset = parsed
                st.header = hdr
                st.header_stamp = stamp
                flow = self._flow(hdr, stamp, events)
                flow.streams += 1
                inner = bytes(st.buf[offset:])
                flow.bytes += len(inner)
                st.buf = bytearray()
                if self.on_object is not None and hdr.fullname_hash in self.decode:
                    st.reader = SubgroupReader()
        if st.reader is not None and inner:
            self._decode(st, stream_id, inner, stamp)
        if fin:
            if st.reader is not None:
                try:
                    st.reader.finish()
                    self._stream_end(st, stream_id, "fin")
                except WireError as e:
                    self._bad_inner(st, stream_id, e)
            self._streams.pop(stream_id, None)
        return events

    def _decode(self, st: _InStream, stream_id: int, data, stamp: Stamp
                ) -> None:
        hdr = st.header
        reader = st.reader
        if hdr is None or reader is None or self.on_object is None:
            return
        try:
            objects = reader.feed(data)
        except WireError as e:
            self._bad_inner(st, stream_id, e)
            return
        if objects:
            where = Inbound(self, stream_id, hdr.sns_id, hdr.fullname_hash,
                            stamp, st.header_stamp)
            for obj in objects:
                self.on_object(where, obj)

    def _bad_inner(self, st: _InStream, stream_id: int, error) -> None:
        self.bad_data += 1
        log.warning(f"peering: {self.tag} data stream {stream_id}: {error}")
        self._stream_end(st, stream_id, "bad")
        st.bad, st.reader = True, None

    def _stream_end(self, st: _InStream, stream_id: int, how: str) -> None:
        if self.on_stream_end is not None and st.header is not None:
            self.on_stream_end(self, st.header.fullname_hash, stream_id, how)

    def stream_reset(self, stream_id: int) -> None:
        st = self._streams.pop(stream_id, None)
        if st is not None and st.reader is not None:
            self._stream_end(st, stream_id, "reset")

    def datagram(self, data) -> List[SessionEvent]:
        events: List[SessionEvent] = []
        try:
            hdr, offset = parse_data_header(data, datagram=True)
        except WireError as e:
            self.bad_data += 1
            log.debug(f"peering: {self.tag} datagram ignored: {e}")
            return events
        stamp = self._clock()
        flow = self._flow(hdr, stamp, events)
        flow.datagrams += 1
        flow.bytes += len(data) - offset
        if self.on_object is not None and hdr.fullname_hash in self.decode:
            try:
                obj = decode_datagram(data[offset:])
            except WireError as e:
                self.bad_data += 1
                log.debug(f"peering: {self.tag} datagram ignored: {e}")
                return events
            self.on_object(Inbound(self, None, hdr.sns_id, hdr.fullname_hash,
                                   stamp, None), obj)
        return events

    # -- close -------------------------------------------------------

    def close(self, reason: str) -> SessionEvent:
        """Forget everything learned through this peer."""
        stamp = self._clock()
        purged = (f"nodes={len(self.nodes)} announces={len(self.announces)} "
                  f"subscribes={len(self.subscribes)} sns={len(self.sns_in)}")
        self.state = "closed"
        self.close_reason = reason
        self.closed_stamp = stamp
        self.nodes.clear()
        self.announces.clear()
        self.subscribes.clear()
        self.sns_in.clear()
        self._streams.clear()
        return SessionEvent("closed", stamp, note=f"{reason}; purged {purged}")
