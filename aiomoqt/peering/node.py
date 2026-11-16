"""FabricNode: a moq-peering leaf node on aiopquic raw QUIC.

Dials peers (re-dialing after a close), optionally listens, learns nodes,
announces, subscribes and SNS sets per session, and originates its own
announces and subscribes. It advertises only itself, with a high metric,
and never forwards what it learns, so no relay routes through it.
"""
import asyncio
import dataclasses
import functools
import ipaddress
import ssl
from collections import Counter
from typing import (Callable, Dict, Iterator, List, Optional, Sequence, Tuple,
                    Union, cast)

from aiopquic.asyncio import QuicConnectionProtocol
from aiopquic.asyncio import connect as quic_connect
from aiopquic.asyncio import serve as quic_serve
from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import (ConnectionTerminated, DatagramFrameReceived,
                                  HandshakeCompleted, StreamDataReceived,
                                  StreamReset)

from aiomoqt.peering.hashing import (fresh_node_id, node_id_format,
                                     node_id_parse, track_hashes)
from aiomoqt.peering.report import describe, event_fields, track_text
from aiomoqt.peering.session import (DataFlow, Learned, PeerSession,
                                     SessionEvent, Stamp, log, now_stamp)
from aiomoqt.peering.wire import (ALPN, ANNOUNCE_NAMESPACE, AnnounceInfo,
                                  MsgType, NodeInfo, NodeType, SubscribeInfo)

LEAF_METRIC = 1 << 32
DEFAULT_LABEL = "aiomoqt-peer"
IDLE_TIMEOUT = 5.0
REDIAL_INTERVAL = 2.0
CONNECT_TIMEOUT = 5.0
MAX_EVENTS = 100_000
MAX_CLOSED_SESSIONS = 100
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
            if self.session is not None:
                self.session.stream_reset(event.stream_id)
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

    def control_flushed(self) -> bool:
        """Every control byte written has left aiopquic's ring and nothing
        is in flight unacked."""
        if self._pending:
            return False
        sid = self.session.control_stream if self.session else None
        if sid is not None and self._quic.tx_data_ring_used(sid):
            return False
        return not self._quic.path_quality().get("bytes_in_transit")


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
                         timeout: float) -> None:
        """Until predicate() holds; TimeoutError after timeout."""
        async with asyncio.timeout(timeout):
            while not predicate():
                self._changed.clear()
                await self._changed.wait()
