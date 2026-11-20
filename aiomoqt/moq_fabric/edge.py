"""The relay edge of a FabricNode: fabric sessions as MoQT d18 sessions.

FabricHop is one fabric session's share of a track this node publishes,
shaped as the downstream session a relay writes a subscriber's d18
streams and datagrams to; it prepends the moq-fabric data headers with
that session's SNS id. FabricSource is the fabric as one upstream
publisher session: subscribe() pulls a track, and its objects reach the
handlers registered under its fullname hash as aiomoqt message objects
(relay_object()).
"""
import asyncio
from typing import Any, Callable, Dict, Optional, Tuple, Union

from aiomoqt.context import stream_reset_code
from aiomoqt.messages.data import ObjectDatagram, ObjectHeader
from aiomoqt.moq_fabric.hashing import track_hashes
from aiomoqt.moq_fabric.inner import (DATAGRAM_MAX_FRAME, P18, DatagramObject,
                                   StreamObject)
from aiomoqt.moq_fabric.node import (FabricNode, FabricTrack, Name, _bytes,
                                  namespace_tuple)
from aiomoqt.moq_fabric.session import Inbound, log
from aiomoqt.moq_fabric.wire import (DATAGRAM_HEADER_LEN, DataHeader, DataType,
                                  WireError)
from aiomoqt.types import MOQTStreamStopped, StreamResetCode

class HopRetiring(MOQTStreamStopped):
    """FabricHop.open_uni_stream on a hop whose SNS moved to another
    session: open no new stream here; the hop's open streams still
    write until they end, then on_sns(track, session, None) fires."""


# Fraction of the TX event ring past which a nowait write refuses.
TX_RING_WAIT_AT = 0.9
# How long a stream FIN or reset refused on a full ring is retried.
RETRY_FOR_S = 2.0
RETRY_POLL_S = 0.01


def relay_object(obj: Union[StreamObject, DatagramObject],
                 fullname_hash: int) -> Union[ObjectHeader, ObjectDatagram]:
    """A fabric object as the message a session's object handler gets:
    ObjectDatagram for a datagram, else ObjectHeader (no stream flags)."""
    if isinstance(obj, DatagramObject):
        return ObjectDatagram(
            track_alias=fullname_hash, group_id=obj.group_id,
            object_id=obj.object_id, publisher_priority=obj.priority,
            extensions=obj.extensions, payload=obj.payload,
            end_of_group=obj.end_of_group, status=obj.status)
    return ObjectHeader(object_id=obj.object_id, extensions=obj.extensions,
                        status=obj.status, payload=obj.payload,
                        publisher_priority=obj.priority)


class FabricHop:
    """A published track's data on one fabric session, as a d18 MoQT
    downstream session. The track alias is the fullname hash. Each
    stream opens with NEW_STREAM carrying the session's current SNS id;
    datagrams get the DATAGRAM header. When a route move takes the SNS,
    the hop is retiring: open_uni_stream raises HopRetiring while its
    open streams finish, within the subscribers' delivery timeout. close()
    it on FabricNode.on_sns with None, which follows."""

    _profile = P18
    stream_scheduling = "fifo"
    _close_err = None
    peer_address: Optional[Tuple[str, int]] = None

    def __init__(self, node: FabricNode, track: FabricTrack,
                 session_id: int):
        self.node = node
        self.track = track
        self.session_id = session_id
        self._proto = node._protocols.get(session_id)
        self._moqt_session_closed = \
            asyncio.get_running_loop().create_future()
        self._ring_cap: Optional[int] = None
        self.streams = 0
        self.datagrams = 0
        self.datagrams_dropped = 0
        if self._proto is not None:
            peer = getattr(self._proto._quic, "peer_address", None)
            addr = peer() if peer is not None else None
            self.peer_address = tuple(addr) if addr else None
        if not self._writable():
            self.close()

    def __repr__(self) -> str:
        return (f"FabricHop(session={self.session_id}, "
                f"hash=0x{self.track.fullname_hash:016x})")

    @property
    def _quic(self):
        return None if self._proto is None else self._proto._quic

    @property
    def sns_id(self) -> Optional[int]:
        sns = self.track.origin.sns.get(self.session_id)
        return None if sns is None else sns.sns_id

    @property
    def closed(self) -> bool:
        return not self._writable()

    @property
    def retiring(self) -> bool:
        return self.track.retiring(self.session_id)

    def close(self) -> None:
        if not self._moqt_session_closed.done():
            self._moqt_session_closed.set_result(None)
        for key in [k for k, (hop, _) in self.track._hop_streams.items()
                    if hop is self]:
            self.track._hop_ended(*key)

    def _ended(self, stream_id: int) -> None:
        self.track._hop_ended(self.session_id, stream_id)

    def _count(self, n: int, datagram: bool = False) -> None:
        proto = self._proto
        if proto is not None:
            proto.tx_bytes += n
            proto.tx_datagrams += datagram

    def _writable(self) -> bool:
        proto = self._proto
        return (proto is not None and not proto.terminated.is_set()
                and not self._moqt_session_closed.done())

    def _stream_ring_cap(self) -> int:
        if self._ring_cap is None:
            cfg = getattr(self._quic, "_configuration", None)
            self._ring_cap = getattr(cfg, "stream_ring_cap", 0) or 0
        return self._ring_cap

    # -- streams -----------------------------------------------------

    async def open_uni_stream(self, initial: bytes = b"",
                              priority: Optional[int] = None,
                              end_stream: bool = False,
                              chain: Optional[int] = None,
                              order: int = 0) -> int:
        """Open a data stream: NEW_STREAM, then `initial` (a d18
        subgroup header and its first objects). Returns the stream id.
        HopRetiring while the hop is retiring; CancelledError once
        closed or without an SNS."""
        sns_id = self.sns_id
        if sns_id is None and self._writable() and self.retiring:
            raise HopRetiring()
        if not self._writable() or sns_id is None:
            self.close()
            raise asyncio.CancelledError()
        origin = self.track.origin
        head = DataHeader(DataType.NEW_STREAM, sns_id, origin.fullname_hash,
                          origin.priority, origin.ttl_ms).encode()
        sid = self._quic.get_next_available_stream_id(True)
        self.streams += 1
        assert self._proto is not None
        self._proto.tx_streams += 1
        if not end_stream:
            self.track._hop_opened(self, sid, sns_id)
        try:
            await self.stream_write_drain(sid, head + bytes(initial),
                                          end_stream=end_stream,
                                          priority=priority, chain=chain,
                                          order=order)
        except BaseException:
            self.stream_reset(sid, StreamResetCode.CANCELLED)
            raise
        return sid

    def stream_write(self, stream_id: int, data: bytes,
                     end_stream: bool = False) -> None:
        """Write now, or once the transport takes it."""
        if not self._writable():
            if end_stream:
                self._ended(stream_id)
            return
        try:
            self._quic.send_stream_data(stream_id, data,
                                        end_stream=end_stream)
            self._count(len(data))
        except BufferError:
            self.node._spawn(self._write_later(stream_id, data, end_stream))
            return
        except (AssertionError, AttributeError, ConnectionError) as e:
            log.debug(f"fabric: {self!r} stream {stream_id} write: {e!r}")
        if end_stream:
            self._ended(stream_id)

    async def _write_later(self, stream_id: int, data: bytes,
                           end_stream: bool) -> None:
        try:
            await self.stream_write_drain(stream_id, data,
                                          end_stream=end_stream)
        except asyncio.CancelledError:
            if end_stream:
                self._ended(stream_id)

    def stream_write_nowait(self, stream_id: int, data: bytes,
                            end_stream: bool = False,
                            wait_at: float = TX_RING_WAIT_AT) -> bool:
        """True once the transport holds `data`; False, nothing written,
        when closed, the TX event ring is past `wait_at`, `data` exceeds
        the stream's ring or the ring is full."""
        if not self._writable():
            return False
        quic = self._quic
        cap = self._stream_ring_cap()
        if cap and len(data) > cap:
            return False
        if quic.tx_event_ring_fill() > wait_at:
            return False
        try:
            quic.send_stream_data(stream_id, data, end_stream)
        except BufferError:
            return False
        except (AssertionError, AttributeError, ConnectionError) as e:
            self._ended(stream_id)
            if self._writable():
                log.debug(f"fabric: {self!r} stream {stream_id} write: "
                          f"{e!r}")
                raise MOQTStreamStopped() from e
            return False
        self._count(len(data))
        if end_stream:
            self._ended(stream_id)
        return True

    async def stream_write_drain(self, stream_id: int, data: bytes,
                                 end_stream: bool = False,
                                 priority: Optional[int] = None,
                                 chain: Optional[int] = None,
                                 order: int = 0) -> None:
        """Write, waiting out backpressure. CancelledError once closed;
        MOQTStreamStopped when the stream is gone on a live session."""
        if not self._writable():
            self._ended(stream_id)
            raise asyncio.CancelledError()
        cap = self._stream_ring_cap()
        if cap and len(data) > cap:
            # aiopquic refuses one write larger than the stream's ring.
            piece = cap // 2
            view = memoryview(data)
            for at in range(0, len(data), piece):
                await self.stream_write_drain(
                    stream_id, bytes(view[at:at + piece]),
                    end_stream=end_stream and at + piece >= len(data),
                    priority=None if at else priority,
                    chain=None if at else chain, order=order)
            return
        kw: Dict[str, Any] = {}
        if priority is not None:
            kw["priority"] = priority
        if chain is not None:
            kw.update(chain=chain, order=order)
        try:
            await self._quic.send_stream_data_drained(
                stream_id, data, end_stream=end_stream, **kw)
        except (AssertionError, AttributeError, ConnectionError) as e:
            self._ended(stream_id)
            if self._writable():
                raise MOQTStreamStopped() from e
            raise asyncio.CancelledError() from e
        self._count(len(data))
        if end_stream:
            self._ended(stream_id)

    def stream_reset(self, stream_id: int,
                     error_code: int = StreamResetCode.CANCELLED, *,
                     reliable_size: int = 0) -> None:
        """RESET_STREAM with the d18 code; retried while the ring is
        full."""
        self._ended(stream_id)
        if not self._writable():
            return
        code = stream_reset_code(error_code, P18)
        if not self._reset_now(stream_id, code):
            self.node._spawn(self._reset_later(stream_id, code))

    def _reset_now(self, stream_id: int, code: int) -> bool:
        try:
            self._quic.reset_stream(stream_id, code)
        except BufferError:
            return False
        except (AssertionError, AttributeError, ConnectionError) as e:
            log.debug(f"fabric: {self!r} stream {stream_id} reset: {e!r}")
        return True

    async def _reset_later(self, stream_id: int, code: int) -> None:
        loop = asyncio.get_running_loop()
        give_up = loop.time() + RETRY_FOR_S
        while self._writable() and loop.time() < give_up:
            await asyncio.sleep(RETRY_POLL_S)
            if self._reset_now(stream_id, code):
                return

    # -- datagrams ---------------------------------------------------

    def datagram_max_payload(self) -> int:
        """Largest inner d18 datagram: the frame cap less the 14-byte
        header; 0 once closed."""
        if not self._writable():
            return 0
        quic = self._quic
        frame = DATAGRAM_MAX_FRAME
        peer = getattr(quic, "max_datagram_payload", None)
        if peer is not None:
            frame = min(frame, peer())
        return max(0, frame - DATAGRAM_HEADER_LEN)

    def send_dgram_message(self, buf) -> int:
        """One d18 object datagram (a Buffer or bytes). Bytes accepted;
        0 when closed, without an SNS, or the datagram ring is full."""
        data = bytes(getattr(buf, "data", buf))
        limit = self.datagram_max_payload()
        if limit and len(data) > limit:
            raise WireError(f"datagram of {len(data)} bytes over {limit}")
        sns_id = self.sns_id
        if not limit or sns_id is None:
            return 0
        head = DataHeader(DataType.DATAGRAM, sns_id,
                          self.track.fullname_hash).encode()
        if self._quic.send_datagram_frame(head + data):
            self.datagrams += 1
            self._count(0, datagram=True)
            return len(data)
        self.datagrams_dropped += 1
        return 0

    # -- control -----------------------------------------------------

    def subscribe_done(self, request_id: Optional[int] = None,
                       status_code: int = 0, stream_count: int = 0,
                       reason: str = "", **_kw) -> None:
        """No fabric message: a subscription ends with its streams."""


class FabricPull:
    """One track FabricSource.subscribe() pulls: the node's sink for it,
    and the request handle a relay keeps (session, request_id,
    track_alias)."""

    largest = None

    def __init__(self, source: "FabricSource", request_id: int,
                 namespace: Tuple[bytes, ...], name: bytes,
                 subscribe_kw: Dict[str, Any]):
        self.session = source
        self.request_id = request_id
        self.namespace = namespace
        self.name = name
        self.track_alias = track_hashes(namespace, name).fullname
        self.subscribe_kw = subscribe_kw
        self.forward = bool(subscribe_kw.get("forward", 1))
        # (fabric session id, stream id) -> (group, subgroup).
        self._streams: Dict[Tuple[int, Optional[int]], Tuple[int, int]] = {}
        self.objects = 0

    def on_object(self, where: Inbound,
                  obj: Union[StreamObject, DatagramObject]) -> None:
        self.objects += 1
        cb = self.session._object_handlers.get(self.track_alias)
        if cb is None:
            return
        msg = relay_object(obj, self.track_alias)
        ts_us = where.stamp[1] // 1000
        if isinstance(obj, DatagramObject):
            cb(msg, len(obj.payload), ts_us, obj.group_id, None)
            return
        key = (where.session.id, where.stream_id)
        if key not in self._streams:
            self._streams[key] = (obj.group_id, obj.subgroup_id)
        cb(msg, len(obj.payload), ts_us, obj.group_id, obj.subgroup_id)

    def on_stream_end(self, session_id: int, stream_id: int, how: str
                      ) -> None:
        where = self._streams.pop((session_id, stream_id), None)
        if where is None:
            return
        cb = self.session._stream_end_handlers.get(self.track_alias)
        if cb is not None:
            cb(where[0], where[1], clean=how == "fin", reset_code=0)


class FabricSource:
    """The fabric as one MoQT d18 upstream publisher session.

    subscribe() pulls a track from the node and returns its FabricPull;
    objects go to the handler registered under the track's fullname hash
    as callback(msg, size, t_us, group_id, subgroup_id), and subgroup
    stream ends to the stream-end handler as callback(group_id,
    subgroup_id, clean=, reset_code=). request_update(forward=0)
    withdraws the subscribe and forward=1 advertises it again at the next
    seq; unsubscribe() unpulls."""

    _profile = P18
    _close_err = None
    peer_address: Optional[Tuple[str, int]] = None

    def __init__(self, node: FabricNode):
        self.node = node
        self._moqt_session_closed = \
            asyncio.get_running_loop().create_future()
        self._object_handlers: Dict[int, Callable] = {}
        self._stream_end_handlers: Dict[int, Callable] = {}
        # A relay looks up SUBSCRIBE_OKs here; the fabric sends none.
        self._subscriptions: Dict[int, Any] = {}
        self._pulls: Dict[int, FabricPull] = {}
        self._next_request = 0

    def __repr__(self) -> str:
        return f"FabricSource({self.node.local.contact!r})"

    @property
    def closed(self) -> bool:
        return self._moqt_session_closed.done()

    def register_object_handler(self, track_alias: int,
                                callback: Callable) -> None:
        self._object_handlers[track_alias] = callback

    def unregister_object_handler(self, track_alias: int) -> None:
        self._object_handlers.pop(track_alias, None)

    def register_stream_end_handler(self, track_alias: int,
                                    callback: Callable) -> None:
        self._stream_end_handlers[track_alias] = callback

    def unregister_stream_end_handler(self, track_alias: int) -> None:
        self._stream_end_handlers.pop(track_alias, None)

    def pull_for(self, track_alias: int) -> Optional[FabricPull]:
        return next((p for p in self._pulls.values()
                     if p.track_alias == track_alias), None)

    def subscribe(self, namespace: Name, name: Union[str, bytes], *,
                  forward: int = 1, priority: int = 128,
                  delivery_timeout_ms: int = 5000,
                  expires_ms: int = 5000) -> FabricPull:
        """Pull a track; with forward=0 bind it without advertising."""
        if self.closed:
            raise ConnectionError(f"{self!r} is closed")
        ns, nm = namespace_tuple(namespace), _bytes(name)
        kw = dict(priority=priority, delivery_timeout_ms=delivery_timeout_ms,
                  expires_ms=expires_ms)
        self._next_request += 1
        pull = FabricPull(self, self._next_request, ns, nm, kw)
        pull.forward = bool(forward)
        self.node.pull(ns, nm, pull, advertise=pull.forward, **kw)
        self._pulls[pull.request_id] = pull
        return pull

    def request_update(self, request_id: int, forward: Optional[int] = None,
                       wait_response: bool = False, **_kw):
        """Forward 0 withdraws the subscribe, 1 re-advertises it. With
        `wait_response`, returns a future done with None (no reply)."""
        pull = self._pulls.get(request_id)
        if pull is None:
            raise KeyError(f"no fabric request {request_id}")
        if forward is not None and bool(forward) != pull.forward:
            pull.forward = bool(forward)
            if pull.forward:
                self.node.subscribe(pull.namespace, pull.name,
                                    **pull.subscribe_kw)
            elif self.node.subscribed(pull.namespace, pull.name):
                self.node.unsubscribe(pull.namespace, pull.name)
        if not wait_response:
            return None
        reply = asyncio.get_running_loop().create_future()
        reply.set_result(None)
        return reply

    def unsubscribe(self, request_id: int) -> None:
        """Unpull the track and withdraw its subscribe."""
        pull = self._pulls.pop(request_id, None)
        if pull is not None:
            self.node.unpull(pull.namespace, pull.name)

    def close(self) -> None:
        """Unpull every track; the session reads as closed."""
        for rid in list(self._pulls):
            self.unsubscribe(rid)
        if not self._moqt_session_closed.done():
            self._moqt_session_closed.set_result(None)
