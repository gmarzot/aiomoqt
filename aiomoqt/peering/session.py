"""Sans-I/O moq-peering session: one QUIC connection to one peer.

Bytes and stream events go in; parsed state, SessionEvents and control
bytes to send (take_output) come out. The session records what it learns
through this peer and forwards none of it: the node is a leaf.
"""
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from aiomoqt.peering.hashing import node_id_format, track_hashes
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


class _InStream:
    __slots__ = ("buf", "header", "bad")

    def __init__(self):
        self.buf = bytearray()
        self.header: Optional[DataHeader] = None
        self.bad = False


def _client_bidi(stream_id: int) -> bool:
    return stream_id & 3 == 0


def _seq_newer(new: int, old: int) -> bool:
    return 1 <= (new - old) % 65536 <= 32767


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
        if prev is not None:
            old = prev.body
            if old == sub:
                self.duplicates += 1
                return "duplicate"
            if old.epoch != sub.epoch or not _seq_newer(sub.seq, old.seq):
                self.stale += 1
                return f"stale (have epoch {old.epoch} seq {old.seq})"
        self.subscribes[key] = Learned(
            sub, *stamp, hash_ok=ok,
            updates=prev.updates + 1 if prev else 0)
        events.append(SessionEvent("subscribe", stamp, sub, ok,
                                   note="update" if prev else ""))
        outcome = "updated" if prev else "learned"
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

    # -- data plane: headers counted, inner bytes not decoded ---------

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
        if st.header is not None:
            self.flows[st.header.fullname_hash].bytes += len(data)
        elif not st.bad:
            st.buf += data
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
                flow = self._flow(hdr, self._clock(), events)
                flow.streams += 1
                flow.bytes += len(st.buf) - offset
                st.buf = bytearray()
        if fin:
            self._streams.pop(stream_id, None)
        return events

    def stream_reset(self, stream_id: int) -> None:
        self._streams.pop(stream_id, None)

    def datagram(self, data) -> List[SessionEvent]:
        events: List[SessionEvent] = []
        try:
            hdr, offset = parse_data_header(data, datagram=True)
        except WireError as e:
            self.bad_data += 1
            log.debug(f"peering: {self.tag} datagram ignored: {e}")
            return events
        flow = self._flow(hdr, self._clock(), events)
        flow.datagrams += 1
        flow.bytes += len(data) - offset
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
