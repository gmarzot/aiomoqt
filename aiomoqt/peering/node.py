"""FabricNode: a moq-peering leaf node on aiopquic raw QUIC.

Dials peers (re-dialing after a close), optionally listens, learns nodes,
announces, subscribes and SNS sets per session, and originates its own
announces and subscribes. It advertises only itself, with a high metric,
and never forwards what it learns, so no relay routes through it.

publish() makes a FabricTrack that sends d18 objects to every subscriber
node, one SNS per session; receive() subscribes and measures a track's
objects with TrackStats.
"""
import asyncio
import dataclasses
import functools
import ipaddress
import ssl
from collections import Counter
from typing import (Any, Callable, Dict, Iterator, List, Optional, Sequence,
                    Set, Tuple, Union, cast)

from aiopquic.asyncio import QuicConnectionProtocol
from aiopquic.asyncio import connect as quic_connect
from aiopquic.asyncio import serve as quic_serve
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import (ConnectionTerminated, DatagramFrameReceived,
                                  HandshakeCompleted, StreamDataReceived,
                                  StreamReset)

from aiomoqt.messages import ObjectStatus
from aiomoqt.peering.hashing import (fresh_node_id, node_id_format,
                                     node_id_parse, track_hashes)
from aiomoqt.peering.ib import DEFAULT_TTL_MS, OriginTrack, reconcile
from aiomoqt.peering.inner import (DEFAULT_PRIORITY, DatagramObject,
                                   StreamObject, SubgroupWriter,
                                   encode_datagram)
from aiomoqt.peering.report import describe, event_fields, track_text
from aiomoqt.peering.session import (DataFlow, Inbound, Learned, PeerSession,
                                     SessionEvent, Stamp, log, now_stamp)
from aiomoqt.peering.wire import (ALPN, ANNOUNCE_NAMESPACE, AnnounceInfo,
                                  DataHeader, DataType, MsgType, NodeInfo,
                                  NodeType, SubscribeInfo)
from aiomoqt.utils.payload import PayloadCheck
from aiomoqt.utils.stats import TrackStats

LEAF_METRIC = 1 << 32
DEFAULT_LABEL = "aiomoqt-peer"
IDLE_TIMEOUT = 5.0
REDIAL_INTERVAL = 2.0
CONNECT_TIMEOUT = 5.0
MAX_EVENTS = 100_000
MAX_CLOSED_SESSIONS = 100
# Samples kept of the gap from a stream's NEW_STREAM header to its first
# whole object.
HEADER_GAP_SAMPLES = 10_000
# Longest a stop waits for its withdraws to be sent and acked.
WITHDRAW_GRACE = 0.25

Name = Union[str, bytes, Sequence[Union[str, bytes]]]


def _bytes(value: Union[str, bytes]) -> bytes:
    return value.encode() if isinstance(value, str) else bytes(value)


def namespace_tuple(namespace: Name) -> Tuple[bytes, ...]:
    """'a/b' or ('a', 'b') as wire tuples."""
    if isinstance(namespace, str):
        return tuple(t.encode() for t in namespace.split("/"))
    if isinstance(namespace, (bytes, bytearray)):
        return (bytes(namespace),)
    return tuple(_bytes(t) for t in namespace)


def parse_track(text: str) -> Tuple[Tuple[bytes, ...], bytes]:
    """'ns/track' -> (namespace tuples, name); no '/' or a trailing '/'
    names a namespace alone (empty name)."""
    namespace, sep, name = text.rpartition("/")
    if not sep:
        namespace, name = text, ""
    if not namespace:
        raise ValueError(f"{text!r} has no namespace")
    return namespace_tuple(namespace), name.encode()


def parse_identity(spec: Optional[str], label: str = DEFAULT_LABEL
                   ) -> Tuple[int, str]:
    """--node-id: (node id, contact label). An 'a.b:c.d' / 'high:low' id is
    used as given; anything else is a label, and the id is fresh."""
    if spec is None:
        return fresh_node_id(label), label
    try:
        node_id = node_id_parse(spec)
    except ValueError:
        return fresh_node_id(spec), spec
    if node_id == 0:
        raise ValueError("node id 0 is reserved")
    return node_id, label


def _addr_text(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _sni(host: str) -> str:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    return "localhost"


class _PeerProtocol(QuicConnectionProtocol):
    """One QUIC connection: routes events to its node and writes the
    session's control bytes in order."""

    def __init__(self, quic, stream_handler=None, *, node: "FabricNode",
                 dialer: bool, peer: Optional[str] = None,
                 dial_start: Optional[Stamp] = None):
        super().__init__(quic, stream_handler=stream_handler)
        self.node = node
        self.dialer = dialer
        self.peer = peer
        self.dial_start = dial_start
        self.session: Optional[PeerSession] = None
        self.terminated = asyncio.Event()
        self._pending: List[bytes] = []
        self._writer: Optional[asyncio.Task] = None
        self._priority_set = False

    def quic_event_received(self, event) -> None:
        if isinstance(event, StreamDataReceived):
            self.node._on_stream_data(self, event)
        elif isinstance(event, HandshakeCompleted):
            self.node._on_quic_up(self)
        elif isinstance(event, DatagramFrameReceived):
            self.node._on_datagram(self, event)
        elif isinstance(event, StreamReset):
            self.node._on_stream_reset(self, event)
        elif isinstance(event, ConnectionTerminated):
            self.node._on_terminated(self, event)
            self.terminated.set()

    def write_control(self, data: bytes) -> None:
        if not self._pending:
            try:
                self._send(data)
                return
            except BufferError:
                pass
        self._pending.append(data)
        if self._writer is None or self._writer.done():
            self._writer = asyncio.ensure_future(self._drain())

    def _control_stream(self) -> int:
        sid = self.session.control_stream if self.session else None
        if sid is None:
            raise RuntimeError("control write before the control stream")
        return sid

    def _send(self, data: bytes) -> None:
        # Control stream priority 0, as the library sets it.
        self._quic.send_stream_data(
            self._control_stream(), data,
            priority=None if self._priority_set else 0)
        self._priority_set = True

    async def _drain(self) -> None:
        while self._pending and not self.terminated.is_set():
            await self._quic.send_stream_data_drained(
                self._control_stream(), self._pending[0],
                priority=None if self._priority_set else 0)
            self._priority_set = True
            self._pending.pop(0)

    async def send_data(self, stream_id: int, data: bytes,
                        fin: bool = False) -> None:
        """Data stream bytes, waiting out aiopquic backpressure."""
        try:
            self._quic.send_stream_data(stream_id, data, end_stream=fin)
        except BufferError:
            await self._quic.send_stream_data_drained(stream_id, data,
                                                      end_stream=fin)

    def control_flushed(self) -> bool:
        """Every control byte written has left aiopquic's ring and nothing
        is in flight unacked."""
        if self._pending:
            return False
        sid = self.session.control_stream if self.session else None
        if sid is not None and self._quic.tx_data_ring_used(sid):
            return False
        return not self._quic.path_quality().get("bytes_in_transit")


class _Egress:
    """One subgroup in flight: its encoder and its stream per session."""
    __slots__ = ("writer", "streams")

    def __init__(self, writer: SubgroupWriter):
        self.writer = writer
        self.streams: Dict[int, int] = {}


class FabricTrack:
    """A track this node publishes (FabricNode.publish). Objects go to the
    sessions holding an SNS for it; a session gains a subgroup's stream
    only when that subgroup opens."""

    def __init__(self, node: "FabricNode", origin: OriginTrack):
        self.node = node
        self.origin = origin
        self._subgroups: Dict[Tuple[int, int], _Egress] = {}
        self.objects = 0
        self.bytes = 0
        self.streams = 0
        self.datagrams = 0
        self.datagrams_dropped = 0

    @property
    def fullname_hash(self) -> int:
        return self.origin.fullname_hash

    @property
    def subscribed(self) -> bool:
        return bool(self.origin.sns)

    def _sessions(self) -> List[Tuple[int, "_PeerProtocol", int]]:
        out = []
        for sid, sns in list(self.origin.sns.items()):
            proto = self.node._protocols.get(sid)
            if proto is not None and not proto.terminated.is_set():
                out.append((sid, proto, sns.sns_id))
        return out

    async def write(self, group_id: int, subgroup_id: int, object_id: int,
                    payload: bytes, extensions=None) -> None:
        """One object, on one stream per subscribed session."""
        key = (group_id, subgroup_id)
        eg = self._subgroups.get(key)
        opening = eg is None
        if eg is None:
            eg = self._subgroups[key] = _Egress(SubgroupWriter(
                self.fullname_hash, group_id, subgroup_id,
                self.origin.priority))
        data = eg.writer.object(object_id, payload, extensions)
        self.objects += 1
        self.bytes += len(payload)
        if opening:
            for sid, proto, sns_id in self._sessions():
                stream = proto._quic.get_next_available_stream_id(True)
                head = DataHeader(DataType.NEW_STREAM, sns_id,
                                  self.fullname_hash, self.origin.priority,
                                  self.origin.ttl_ms).encode()
                eg.streams[sid] = stream
                self.streams += 1
                await proto.send_data(stream, head + eg.writer.header + data)
            return
        for sid, stream in list(eg.streams.items()):
            proto = self.node._protocols.get(sid)
            if proto is None or proto.terminated.is_set():
                eg.streams.pop(sid, None)
                continue
            await proto.send_data(stream, data)

    async def end_subgroup(self, group_id: int, subgroup_id: int, *,
                           end_of_group: Optional[int] = None,
                           reset: bool = False) -> None:
        """FIN the subgroup's streams, after an END_OF_GROUP status object
        with id `end_of_group` when given; or reset them."""
        eg = self._subgroups.pop((group_id, subgroup_id), None)
        if eg is None:
            return
        tail = b""
        if end_of_group is not None and not reset:
            tail = eg.writer.status(end_of_group, ObjectStatus.END_OF_GROUP)
        for sid, stream in eg.streams.items():
            proto = self.node._protocols.get(sid)
            if proto is None or proto.terminated.is_set():
                continue
            if reset:
                proto._quic.reset_stream(stream, 0)
            else:
                await proto.send_data(stream, tail, fin=True)

    def datagram(self, group_id: int, object_id: int, payload: bytes,
                 extensions=None, end_of_group: bool = False) -> int:
        """One object as a datagram to each subscribed session; returns the
        sessions it was queued on. A full datagram ring drops it."""
        inner = encode_datagram(self.fullname_hash, group_id, object_id,
                                payload, extensions, self.origin.priority,
                                end_of_group)
        self.objects += 1
        self.bytes += len(payload)
        sent = 0
        for _, proto, sns_id in self._sessions():
            head = DataHeader(DataType.DATAGRAM, sns_id,
                              self.fullname_hash).encode()
            if proto._quic.send_datagram_frame(head + inner):
                sent += 1
                self.datagrams += 1
            else:
                self.datagrams_dropped += 1
        return sent

    def _drop_session(self, session_id: int) -> None:
        """FIN this track's streams on a session that lost its SNS."""
        proto = self.node._protocols.get(session_id)
        for eg in self._subgroups.values():
            stream = eg.streams.pop(session_id, None)
            if stream is not None and proto is not None \
                    and not proto.terminated.is_set():
                self.node._spawn(proto.send_data(stream, b"", fin=True))


class RxTrack:
    """A track this node receives (FabricNode.receive): TrackStats for
    objects, loss, dups and latency; stream ends; the NEW_STREAM header
    to first object gap."""

    def __init__(self, namespace: Tuple[bytes, ...], name: bytes,
                 verify_every: int = 0, **stats_kw):
        self.namespace = namespace
        self.name = name
        self.fullname_hash = track_hashes(namespace, name).fullname
        self.stats = TrackStats(**stats_kw)
        self.check = PayloadCheck(verify_every) if verify_every else None
        self.ends: Counter = Counter()
        self.statuses: Counter = Counter()
        self.per_session: Counter = Counter()
        self.datagrams = 0
        self.header_gap_ms: List[float] = []
        self._open: Set[Tuple[int, Optional[int]]] = set()

    def on_object(self, where: Inbound,
                  obj: Union[StreamObject, DatagramObject]) -> None:
        if obj.status != ObjectStatus.NORMAL:
            self.statuses[obj.status.name] += 1
            return
        self.per_session[where.session.id] += 1
        t_us = where.stamp[1] // 1000
        if isinstance(obj, DatagramObject):
            self.datagrams += 1
            self.stats.on_object(obj, len(obj.payload), t_us, obj.group_id)
        else:
            key = (where.session.id, where.stream_id)
            if key not in self._open and where.header_stamp is not None:
                self._open.add(key)
                if len(self.header_gap_ms) < HEADER_GAP_SAMPLES:
                    self.header_gap_ms.append(
                        (where.stamp[0] - where.header_stamp[0]) / 1e6)
            self.stats.on_object(obj, len(obj.payload), t_us, obj.group_id,
                                 obj.subgroup_id)
        if self.check is not None:
            self.check(obj.group_id, obj.object_id, obj.payload)

    def on_stream_end(self, session_id: int, stream_id: int, how: str) -> None:
        self.ends[how] += 1
        self._open.discard((session_id, stream_id))


class FabricNode:
    """A moq-peering leaf: dial `peers`, listen on `port` (0: do not)."""

    def __init__(self, *, node_id: Optional[int] = None,
                 label: str = DEFAULT_LABEL, metric: int = LEAF_METRIC,
                 node_type: int = NodeType.STUB,
                 peers: Sequence[Tuple[str, int]] = (),
                 bind: Optional[str] = None, port: int = 0,
                 cert: Optional[str] = None, key: Optional[str] = None,
                 keepalive: Optional[float] = 1.5,
                 idle_timeout: float = IDLE_TIMEOUT,
                 redial_interval: float = REDIAL_INTERVAL,
                 connect_timeout: float = CONNECT_TIMEOUT,
                 clock: Callable[[], Stamp] = now_stamp):
        self.local = NodeInfo(node_id or fresh_node_id(label), node_type,
                              label, 0.0, 0.0, metric)
        self.peers = [(h, int(p)) for h, p in peers]
        self.bind = bind
        self.port = port
        self.cert = cert
        self.key = key
        self.keepalive = keepalive or None
        self.idle_timeout = idle_timeout
        self.redial_interval = redial_interval
        self.connect_timeout = connect_timeout
        self.clock = clock
        self.start_mono_ns, self.start_unix_ns = clock()
        self.last_rx_mono_ns = self.start_mono_ns
        self.sessions: Dict[int, PeerSession] = {}
        self.events: List[dict] = []
        self.events_dropped = 0
        self.dial_failures: Counter = Counter()
        self._announces: Dict[int, AnnounceInfo] = {}
        self._subscribes: Dict[int, SubscribeInfo] = {}
        self._origins: Dict[int, FabricTrack] = {}
        self._rx: Dict[int, RxTrack] = {}
        # Called for each received object of a receive() track.
        self.object_hook: Optional[Callable[[Inbound, Any, RxTrack],
                                            None]] = None
        self._tasks: Set[asyncio.Task] = set()
        self._protocols: Dict[int, _PeerProtocol] = {}
        self._next_session = 1
        self._dial_tasks: List[asyncio.Task] = []
        self._server = None
        self._stopping = False
        self._changed = asyncio.Event()

    @property
    def node_id(self) -> int:
        return self.local.node_id

    def listen_text(self) -> Optional[str]:
        if not self.port:
            return None
        return _addr_text(self.bind or "*", self.port)

    # -- lifecycle ---------------------------------------------------

    async def start(self) -> None:
        log.info(f"peering: node {node_id_format(self.node_id)} "
                 f"contact={self.local.contact!r} metric={self.local.metric} "
                 f"listen={self.listen_text() or 'off'} "
                 f"peers={','.join(_addr_text(h, p) for h, p in self.peers) or '-'}")
        self._record("start", node=node_id_format(self.node_id),
                     node_int=self.node_id, contact=self.local.contact)
        if self.port:
            if self.cert is None or self.key is None:
                raise ValueError("listening needs cert and key")
            cfg = self._quic_config(client=False)
            cfg.load_cert_chain(self.cert, self.key)
            # aiopquic takes host None (all interfaces); it annotates str.
            self._server = await quic_serve(
                self.bind,  # ty: ignore[invalid-argument-type]
                self.port, configuration=cfg,
                create_protocol=functools.partial(self._new_protocol,
                                                  dialer=False))
        for host, port in self.peers:
            self._dial_tasks.append(
                asyncio.ensure_future(self._dial_loop(host, port)))

    async def stop(self) -> None:
        if self._stopping:
            return
        await self.withdraw_originated()
        self._stopping = True
        for task in self._dial_tasks:
            task.cancel()
        await asyncio.gather(*self._dial_tasks, return_exceptions=True)
        accepted = [p for p in self._protocols.values() if not p.dialer]
        for proto in accepted:
            proto.close()
        if accepted:
            waits = [asyncio.ensure_future(p.terminated.wait())
                     for p in accepted]
            _, pending = await asyncio.wait(waits, timeout=1.0)
            for w in pending:
                w.cancel()
        if self._server is not None:
            self._server.close()
            self._server = None
        for session in list(self.sessions.values()):
            if session.state != "closed":
                self._close_session(session, "node stopped")
        self._record("stop")

    async def __aenter__(self) -> "FabricNode":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    def _quic_config(self, client: bool, host: str = "") -> QuicConfiguration:
        cfg = QuicConfiguration(alpn_protocols=[ALPN], is_client=client,
                                idle_timeout=self.idle_timeout,
                                keep_alive_interval=self.keepalive)
        if client:
            # moq-peering never verifies certificates.
            cfg.verify_mode = ssl.CERT_NONE
            cfg.server_name = _sni(host)
        return cfg

    def _new_protocol(self, quic, stream_handler=None, **kw) -> _PeerProtocol:
        return _PeerProtocol(quic, stream_handler, node=self, **kw)

    async def _dial_loop(self, host: str, port: int) -> None:
        peer = _addr_text(host, port)
        failing = False
        while not self._stopping:
            try:
                await self._dial_once(host, port, peer)
                failing = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.dial_failures[peer] += 1
                if not failing:
                    log.info(f"peering: dial {peer} failed: "
                             f"{type(e).__name__}: {e}; retrying every "
                             f"{self.redial_interval:g}s")
                    self._record("dial_failed", peer=peer,
                                 error=f"{type(e).__name__}: {e}")
                else:
                    log.debug(f"peering: dial {peer} failed again: {e}")
                failing = True
            if self._stopping:
                break
            await asyncio.sleep(self.redial_interval)

    async def _dial_once(self, host: str, port: int, peer: str) -> None:
        factory = functools.partial(self._new_protocol, dialer=True,
                                    peer=peer, dial_start=self.clock())
        async with asyncio.timeout(self.connect_timeout) as limit:
            async with quic_connect(host, port,
                                    configuration=self._quic_config(True, host),
                                    create_protocol=factory) as conn:
                limit.reschedule(None)
                await cast(_PeerProtocol, conn).terminated.wait()

    # -- QUIC events -------------------------------------------------

    def _on_quic_up(self, proto: _PeerProtocol) -> None:
        sid = self._next_session
        self._next_session += 1
        peer = proto.peer
        if peer is None:
            addr = proto._quic.peer_address()
            peer = _addr_text(*addr) if addr else "?"
        session = PeerSession(self.local, dialer=proto.dialer,
                              session_id=sid, peer=peer, clock=self.clock,
                              start=proto.dial_start, on_event=self._event)
        session.decode = self._rx
        session.on_object = self._on_object
        session.on_stream_end = self._on_stream_end
        proto.session = session
        self.sessions[sid] = session
        self._protocols[sid] = proto
        log.debug(f"peering: QUIC up {session.tag} "
                  f"origin={'dialed' if proto.dialer else 'accepted'}")
        self._record("quic_up", session,
                     origin="dialed" if proto.dialer else "accepted")
        if proto.dialer:
            session.start(proto._quic.get_next_available_stream_id(False))
            self._flush(proto)

    def _on_stream_data(self, proto: _PeerProtocol,
                        event: StreamDataReceived) -> None:
        session = proto.session
        if session is None or session.state == "closed":
            return
        if event.stream_id & 2:
            events = session.stream_data(event.stream_id, event.data,
                                         event.end_stream)
        else:
            self.last_rx_mono_ns = self.clock()[0]
            events = session.control_data(event.stream_id, event.data)
        self._apply(session, events)
        self._flush(proto)

    def _on_stream_reset(self, proto: _PeerProtocol,
                         event: StreamReset) -> None:
        if proto.session is not None:
            proto.session.stream_reset(event.stream_id)

    def _on_datagram(self, proto: _PeerProtocol,
                     event: DatagramFrameReceived) -> None:
        if proto.session is not None:
            self._apply(proto.session, proto.session.datagram(event.data))

    def _on_terminated(self, proto: _PeerProtocol,
                       event: ConnectionTerminated) -> None:
        session = proto.session
        if session is None or session.state == "closed":
            return
        reason = f"code {event.error_code}"
        if event.local_reason:
            reason += f" {event.local_reason}"
        if event.reason_phrase:
            reason += f" {event.reason_phrase!r}"
        self._close_session(session, reason)

    def _flush(self, proto: _PeerProtocol) -> None:
        if proto.session is None:
            return
        for data in proto.session.take_output():
            proto.write_control(data)

    def _apply(self, session: PeerSession, events: List[SessionEvent]) -> None:
        for ev in events:
            self._event(session, ev)

    def _event(self, session: PeerSession, ev: SessionEvent) -> None:
        if ev.kind == "connected":
            self.sample_srtt(session)
            log.info(f"peering: connected {session.tag} "
                     f"origin={'dialed' if session.dialer else 'accepted'} "
                     f"{describe(ev.body)} connect_ms={session.connect_ms}")
            self._sync(session)
        elif ev.kind == "connect_error":
            log.warning(f"peering: {session.tag} CONNECT refused, "
                        f"code {ev.body.code}")
        self._record(ev.kind, session, ev.stamp, **event_fields(ev))
        if ev.kind in ("subscribe", "subscribe_withdrawn"):
            track = self._origins.get(ev.body.fullname_hash)
            if track is not None:
                self._reconcile(track)
        elif ev.kind in ("connected", "node", "node_withdrawn"):
            for track in list(self._origins.values()):
                self._reconcile(track)

    def _sync(self, session: PeerSession) -> None:
        """A new session gets our own announces and subscribes only."""
        for ann in self._announces.values():
            session.send(MsgType.ANNOUNCE_INFO_ADV, ann)
        for sub in self._subscribes.values():
            session.send(MsgType.SUBSCRIBE_INFO_ADV, sub)

    def _close_session(self, session: PeerSession, reason: str) -> None:
        ev = session.close(reason)
        log.info(f"peering: closed {session.tag} "
                 f"node={node_id_format(session.remote.node_id) if session.remote else '-'} "
                 f"{ev.note}")
        self._record("closed", session, ev.stamp, note=ev.note)
        self._protocols.pop(session.id, None)
        for track in list(self._origins.values()):
            self._reconcile(track)
        closed = [s for s in self.sessions.values() if s.state == "closed"]
        for old in closed[:-MAX_CLOSED_SESSIONS]:
            del self.sessions[old.id]

    def _record(self, kind: str, session: Optional[PeerSession] = None,
                stamp: Optional[Stamp] = None, **fields) -> None:
        mono, unix = stamp or self.clock()
        ev = {"t_ms": round((mono - self.start_mono_ns) / 1e6, 3),
              "t_unix_ns": unix, "kind": kind}
        if session is not None:
            ev["session"] = session.id
            ev["peer"] = session.peer
            if session.remote is not None:
                ev["remote_node"] = node_id_format(session.remote.node_id)
                ev["remote_contact"] = session.remote.contact
        ev.update(fields)
        if len(self.events) < MAX_EVENTS:
            self.events.append(ev)
        else:
            self.events_dropped += 1
        self._changed.set()

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_object(self, where: Inbound, obj) -> None:
        rx = self._rx.get(where.fullname_hash)
        if rx is None:
            return
        rx.on_object(where, obj)
        if self.object_hook is not None:
            self.object_hook(where, obj, rx)

    def _on_stream_end(self, session: PeerSession, fullname_hash: int,
                       stream_id: int, how: str) -> None:
        rx = self._rx.get(fullname_hash)
        if rx is not None:
            rx.on_stream_end(session.id, stream_id, how)

    def _reconcile(self, track: FabricTrack) -> None:
        """Send the SNS changes the track's subscribers call for."""
        for ch in reconcile(track.origin, self.connected(), self.node_id,
                            lambda s: s.next_sns_id()):
            session = self.sessions.get(ch.session_id)
            live = session is not None and session.state == "connected"
            if ch.withdraw:
                track._drop_session(ch.session_id)
                kind = "sns_withdrawn_originated"
            else:
                kind = "sns_originated"
            self._record(kind, session, hash=f"0x{track.fullname_hash:016x}",
                         sns=ch.sns.sns_id,
                         nodes=[node_id_format(n) for n in ch.sns.nodes])
            if live and session is not None:
                session.send(MsgType.SNS_WD if ch.withdraw else MsgType.SNS_ADV,
                             ch.sns)
                self._flush(self._protocols[session.id])

    def sample_srtt(self, session: PeerSession) -> None:
        proto = self._protocols.get(session.id)
        if proto is not None:
            rtt = proto._quic.path_quality().get("rtt")
            if rtt:
                session.srtt_us = int(rtt)

    # -- origination -------------------------------------------------

    def _send_all(self, msg_type: MsgType, body) -> None:
        for session in self.connected():
            session.send(msg_type, body)
            self._flush(self._protocols[session.id])

    def announce(self, namespace: Name, name: Union[str, bytes] = b""
                 ) -> AnnounceInfo:
        """Announce a track, or with an empty name its namespace."""
        ns, nm = namespace_tuple(namespace), _bytes(name)
        ann = AnnounceInfo(self.node_id, track_hashes(ns, nm).fullname, ns,
                           nm, 0 if nm else ANNOUNCE_NAMESPACE)
        self._announces[ann.fullname_hash] = ann
        self._record("announce_originated", hash=f"0x{ann.fullname_hash:016x}",
                     track=track_text(ns, nm), namespace=not nm)
        self._send_all(MsgType.ANNOUNCE_INFO_ADV, ann)
        return ann

    async def withdraw_originated(self, grace: float = WITHDRAW_GRACE) -> None:
        """Withdraw every announce and subscribe this node originated, on
        every connected session, and wait up to `grace` seconds for the
        withdraws to be sent and acked."""
        for track in list(self._origins.values()):
            self.unpublish(track)
        for ann in list(self._announces.values()):
            self.withdraw_announce(ann.namespace, ann.name)
        for sub in list(self._subscribes.values()):
            self.unsubscribe(sub.namespace, sub.name)
        protos = [p for p in self._protocols.values()
                  if p.session is not None and p.session.state == "connected"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace
        while loop.time() < deadline and any(
                not p.control_flushed() for p in protos
                if not p.terminated.is_set()):
            await asyncio.sleep(0.005)

    def publish(self, namespace: Name, name: Union[str, bytes], *,
                priority: int = DEFAULT_PRIORITY,
                ttl_ms: int = DEFAULT_TTL_MS) -> FabricTrack:
        """Announce a track and return its sender."""
        ns, nm = namespace_tuple(namespace), _bytes(name)
        if not nm:
            raise ValueError("publish names a track")
        track = FabricTrack(self, OriginTrack(ns, nm, priority, ttl_ms))
        self._origins[track.fullname_hash] = track
        self.announce(ns, nm)
        self._reconcile(track)
        return track

    def unpublish(self, track: FabricTrack) -> None:
        """SNS_WD on every session, FIN the track's streams, withdraw the
        announce."""
        if self._origins.pop(track.fullname_hash, None) is None:
            return
        for sid, sns in list(track.origin.sns.items()):
            track._drop_session(sid)
            session = self.sessions.get(sid)
            if session is not None and session.state == "connected":
                session.send(MsgType.SNS_WD, sns)
                self._flush(self._protocols[sid])
        track.origin.sns.clear()
        if track.fullname_hash in self._announces:
            self.withdraw_announce(track.origin.namespace, track.origin.name)

    def receive(self, namespace: Name, name: Union[str, bytes], *,
                verify_every: int = 0, stats: Optional[dict] = None,
                **subscribe_kw) -> RxTrack:
        """Subscribe to a track and measure its objects."""
        ns, nm = namespace_tuple(namespace), _bytes(name)
        rx = RxTrack(ns, nm, verify_every, **(stats or {}))
        self._rx[rx.fullname_hash] = rx
        self.subscribe(ns, nm, **subscribe_kw)
        return rx

    def withdraw_announce(self, namespace: Name,
                          name: Union[str, bytes] = b"") -> None:
        ns, nm = namespace_tuple(namespace), _bytes(name)
        ann = self._announces.pop(track_hashes(ns, nm).fullname, None)
        if ann is None:
            raise KeyError(f"not announced: {track_text(ns, nm)}")
        self._record("announce_withdrawn_originated",
                     hash=f"0x{ann.fullname_hash:016x}",
                     track=track_text(ns, nm))
        self._send_all(MsgType.ANNOUNCE_INFO_WD, ann)

    def subscribe(self, namespace: Name, name: Union[str, bytes], *,
                  priority: int = 128, forward: int = 1,
                  delivery_timeout_ms: int = 5000,
                  expires_ms: int = 5000) -> SubscribeInfo:
        """Subscribe from this node; again for the same track, an update
        at the next seq. The same seq goes on every session."""
        ns, nm = namespace_tuple(namespace), _bytes(name)
        if not nm:
            raise ValueError("a subscribe names a track")
        th = track_hashes(ns, nm)
        prev = self._subscribes.get(th.fullname)
        sub = SubscribeInfo(
            self.node_id, ns, nm, th.namespace, th.name, th.fullname,
            epoch=0, seq=(prev.seq + 1) & 0xFFFF if prev else 1,
            priority=priority, delivery_timeout_ms=delivery_timeout_ms,
            expires_ms=expires_ms, forward=forward)
        self._subscribes[th.fullname] = sub
        self._record("subscribe_originated", hash=f"0x{th.fullname:016x}",
                     track=track_text(ns, nm), seq=sub.seq)
        self._send_all(MsgType.SUBSCRIBE_INFO_ADV, sub)
        return sub

    def unsubscribe(self, namespace: Name, name: Union[str, bytes]) -> None:
        """SUBSCRIBE_INFO_WD at seq + 1."""
        ns, nm = namespace_tuple(namespace), _bytes(name)
        prev = self._subscribes.pop(track_hashes(ns, nm).fullname, None)
        if prev is None:
            raise KeyError(f"not subscribed: {track_text(ns, nm)}")
        wd = dataclasses.replace(prev, seq=(prev.seq + 1) & 0xFFFF)
        self._record("subscribe_withdrawn_originated",
                     hash=f"0x{wd.fullname_hash:016x}",
                     track=track_text(ns, nm), seq=wd.seq)
        self._send_all(MsgType.SUBSCRIBE_INFO_WD, wd)

    # -- views -------------------------------------------------------

    def connected(self) -> List[PeerSession]:
        return [s for s in self.sessions.values() if s.state == "connected"]

    def originated_announces(self) -> List[AnnounceInfo]:
        return list(self._announces.values())

    def originated_subscribes(self) -> List[SubscribeInfo]:
        return list(self._subscribes.values())

    def published(self) -> List[FabricTrack]:
        return list(self._origins.values())

    def receiving(self) -> List[RxTrack]:
        return list(self._rx.values())

    def _learned(self, table: str) -> Iterator[Tuple[PeerSession, Learned]]:
        for s in self.sessions.values():
            for rec in getattr(s, table).values():
                yield s, rec

    def learned_nodes(self) -> Iterator[Tuple[PeerSession, Learned]]:
        return self._learned("nodes")

    def learned_announces(self) -> Iterator[Tuple[PeerSession, Learned]]:
        return self._learned("announces")

    def learned_subscribes(self) -> Iterator[Tuple[PeerSession, Learned]]:
        return self._learned("subscribes")

    def learned_sns(self) -> Iterator[Tuple[PeerSession, Learned]]:
        return self._learned("sns_in")

    def data_flows(self) -> Iterator[Tuple[PeerSession, int, DataFlow]]:
        for s in self.sessions.values():
            for full_hash, flow in s.flows.items():
                yield s, full_hash, flow

    def notify(self) -> None:
        """Wake wait_changed()."""
        self._changed.set()

    async def wait_changed(self, timeout: Optional[float] = None) -> None:
        """Return on the next recorded event, or after timeout."""
        self._changed.clear()
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def wait_until(self, predicate: Callable[[], bool],
                         timeout: Optional[float]) -> None:
        """Until predicate() holds; TimeoutError after timeout (None:
        none)."""
        async with asyncio.timeout(timeout):
            while not predicate():
                self._changed.clear()
                await self._changed.wait()
