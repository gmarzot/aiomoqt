import asyncio
import logging
import time
from asyncio import Future
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import (Callable, DefaultDict, Dict, List, Optional, Set, Tuple,
                    Type, Union)

from aiopquic.quic.connection import QuicErrorCode, stream_is_unidirectional
from aiopquic.quic.events import (
    DatagramFrameReceived, ProtocolNegotiated, QuicEvent,
    StopSendingReceived, StreamDataReceived, StreamReset,
)

from importlib.metadata import version

from .context import *
from .messages import *
from .messages.d18 import Setup
from .types import *
from .utils.buffer import Buffer, BufferReadError
from .utils.logger import *
from aiopquic.streamchain import StreamChain
from aiopquic.asyncio.webtransport import WebTransportError

USER_AGENT = f"aiomoqt/{version('aiomoqt')}"


# WebTransport stream header wire constants (draft-ietf-webtrans-http3).
# Every WT stream is prefixed with <type, session_id> as two QUIC varints.
logger = get_logger(__name__)


# Per-stream producer cap on TOTAL bytes in flight — Python sc->tx
# queue PLUS picoquic's per-stream retransmit buffer (sent but not
# acked by peer). Bounds actual memory commitment per stream, not
# just the Python-side queue depth.
#
# Per-stream → preserves QUIC stream independence (HOLB-free
# backpressure). Aggregate per-cnx memory bound is delivered by the
# QUIC protocol mechanisms — initial_max_streams_uni/bidi (cap on
# concurrent stream count) and initial_max_data (cap on aggregate
# unacked bytes) — both exposed via QuicConfiguration.
#
# Hysteresis: producer parks at HIGH water (this value), resumes at
# LOW water (this value // 2). Prevents the packet-granularity wake-
# bounce that would otherwise reduce effective throughput to
# ~packet_size / asyncio_turn.
#
# Sizing: target_latency_ms * target_throughput_bytes_per_sec.
# 1 MiB ≈ 2.7 ms standing-queue contribution per stream at 3 Gbps.
# Works with the aiopquic aggregate gate (tx_max_queued_bytes): this
# knob bounds ONE stream's queue (fairness, binds on long-lived
# streams); the aggregate bounds the sum across streams. Pass None
# to opt out. Future API: tx_target_latency_ms with auto-tracked
# drain rate derives both.
DEFAULT_TX_MAX_INFLIGHT_BYTES = 1 * 1024 * 1024


class MOQTStreamReject(Exception):
    """Raised by the data-stream parser when an incoming uni stream fails
    MoQT-level admission (unknown request_id/track_alias, budget exceeded,
    or reuse of a (track_alias, group_id, subgroup_id) tuple).

    The caller is expected to catch this, send STOP_SENDING via
    _reject_stream(), and end the stream task. This is strictly a
    stream-level rejection — it never closes the session.
    """
    def __init__(self, error_code: int, reason: str):
        super().__init__(reason)
        self.error_code = error_code
        self.reason = reason
    

# base class for client and server session objects
class MOQTPeer:
    """MOQT client and server base-class."""
    def __init__(self, allow_optional_dgram: bool = False,
                 libquicr_compat: bool = False,
                 tx_max_inflight_bytes: Optional[int] =
                     DEFAULT_TX_MAX_INFLIGHT_BYTES):
        #  message handlers
        self._control_msg_handlers: Dict[int, Callable] = {}
        self.allow_optional_dgram = allow_optional_dgram
        self.libquicr_compat = libquicr_compat
        # Per-stream producer soft cap on bytes pending in the sc->tx
        # data ring. Engages only when set BELOW
        # QuicConfiguration.stream_ring_cap (the hard cap). Producer
        # parks on the per-stream drain event with hysteresis: park
        # at HIGH (this value), resume at LOW (this value // 2).
        # Per-stream → preserves QUIC stream independence (no HOLB).
        # Aggregate per-cnx memory bound is delivered by the QUIC
        # protocol mechanisms: max_data caps cnx-level bytes in
        # flight, initial_max_streams_uni/bidi caps concurrent
        # streams (both in QuicConfiguration). Pass None to opt out
        # entirely.
        self.tx_max_inflight_bytes = tx_max_inflight_bytes

    def register_handler(self, msg_type: int, handler: Callable) -> None:
        """Register a custom handler that overrides the default handler
        for this message type. The message class is taken from the
        session's negotiated draft at dispatch time."""
        self._control_msg_handlers[msg_type] = handler


@dataclass(slots=True)
class _DataStreamState:
    """Per-uni-data-stream state.

    chain:  per-stream StreamChain accumulator (held by ref; chunks
            walk across boundaries during parse).
    parser: FetchHeader or SubgroupHeader after admission, holding
            per-stream runtime state (_prior_obj, _last_object_id).
    key:    binding tuple for the reverse maps —
            ('fetch', request_id) or
            ('subgroup', (track_alias, group_id, subgroup_id)).
    bytes_total: cumulative bytes successfully parsed (forensic).
    last_activity: monotonic ts of last successful parse (idle reaper).
    group_id / subgroup_id / object_id: progress across parse calls
            (was function-local in the deleted _process_data_stream).
    """
    chain: 'StreamChain'
    parser: Optional['MOQTMessage'] = None
    key: Optional[tuple] = None
    bytes_total: int = 0
    last_activity: float = 0.0
    group_id: Optional[int] = None
    subgroup_id: Optional[int] = None
    object_id: Optional[int] = None


class _MOQTSessionMixin:
    """MoQT session methods. Mix with an aiopquic transport base —
    QuicConnectionProtocol for raw QUIC, WebTransportSession (via
    _WTSessionMixin) for WebTransport. Concrete classes are
    MOQTSessionQuic / MOQTSessionWTClient / MOQTSessionWTServer at
    the end of this file.
    """

    # Transport identity of the SESSION (not the server: a dual-stack
    # server hosts both kinds on one port, so its use_quic flag cannot
    # speak for any one session). WT-based classes flip this via
    # _WTSessionMixin.
    _is_wt = False

    @property
    def _is_client(self) -> bool:
        """Transport-agnostic is-client signal. Raw-QUIC bases expose
        self._quic.configuration.is_client; WT bases expose
        self.is_client directly."""
        quic = getattr(self, '_quic', None)
        if quic is not None:
            cfg = getattr(quic, 'configuration', None)
            if cfg is not None:
                return cfg.is_client
        return self.is_client

    @property
    def _control_write_stream_id(self) -> Optional[int]:
        """Stream we write control messages on. d18 runs control over a
        pair of uni streams (we write one, read the peer's); pre-d18 it
        is the single bidi control stream."""
        if self._profile.control_uni_pair:
            return self._d18_control_write_sid
        return self._control_stream_id

    @property
    def _control_read_stream_id(self) -> Optional[int]:
        """Stream we read the peer's control messages from. See
        _control_write_stream_id."""
        if self._profile.control_uni_pair:
            return self._d18_control_read_sid
        return self._control_stream_id

    @property
    def negotiated_draft(self) -> int:
        """Negotiated MoQT draft number for this session. Derived from
        _profile (the single stored version field) so the two can never
        drift; assigning it resolves _profile. The wire IETF version code
        is materialized only when (de)serializing SETUP / building ALPN."""
        prof = getattr(self, '_profile', None)
        return prof.draft if prof is not None else get_major_version(MOQT_VERSION_DRAFT14)

    @negotiated_draft.setter
    def negotiated_draft(self, value: int) -> None:
        # _profile is the resolved effective DraftProfile for the session;
        # every per-message codec lookup reads self._profile (a fast stored
        # attribute on the data hot path). The draft is set once it is
        # known: a single supported draft is pinned at __init__, otherwise
        # it is locked when negotiation completes (ALPN / WT-Protocol).
        self._profile = profile_for(value) if value is not None else None

    def __init__(self, *args, session: 'MOQTPeer', **kwargs):
        super().__init__(*args, **kwargs)
        self._session: MOQTPeer = session  # backref to session object with config
        self._session_id: Optional[int] = None
        self._control_stream_id: Optional[int] = None
        # d18 control = pair of uni streams; pre-d18 the
        # _control_write/read_stream_id properties both resolve to the
        # single bidi _control_stream_id above.
        self._d18_control_write_sid: Optional[int] = None
        self._d18_control_read_sid: Optional[int] = None
        # One SETUP per direction — set on first receipt, dups are a
        # protocol violation (also closes the double-bring-up window).
        self._d18_setup_seen = False
        self._loop = asyncio.get_running_loop()
        self._wt_session_setup: Future[bool] = self._loop.create_future()
        # Raw-QUIC sessions have no WT setup phase. Pre-resolve so
        # StreamDataReceived processing isn't gated on a never-resolved
        # future. WT sessions resolve it in _moqt_wt_finalize(). Keyed
        # off the session class, not the owning server's use_quic — a
        # dual-stack server hosts both transports.
        if not self._is_wt:
            self._wt_session_setup.set_result(True)
        # A pinned draft is known before the handshake, so lock _draft now:
        # over raw QUIC a peer's SETUP (d18 control uni) can arrive in the
        # same batch as — or before — ProtocolNegotiated, and it must be
        # demuxed under the right draft (control_uni_pair) rather than the
        # default. Auto (unpinned) clients still resolve from the
        # negotiated ALPN / WT-Protocol.
        _drafts = getattr(session, 'supported_drafts', None)
        _pinned = _drafts[0] if _drafts and len(_drafts) == 1 else None
        self.negotiated_draft = (get_major_version(_pinned) if _pinned is not None
                                 else get_major_version(MOQT_VERSION_DRAFT14))
        self._moqt_session_setup: Future[bool] = self._loop.create_future()
        self._moqt_session_closed: Future[Tuple[int,str]] = self._loop.create_future()
        self._next_request_id = 0 if self._is_client else 1
        self._next_track_alias = 0
        # Single dict per stream — _DataStreamState consolidates queue,
        # task, parser, binding-key, and forensic counter into one
        # slotted object. See class docstring above.
        self._data_streams: Dict[int, _DataStreamState] = {}
        # Per control/request stream reassembly accumulator — a control
        # message can arrive split across StreamDataReceived events, so
        # bytes are held here until whole messages can be parsed (mirror
        # of the data-plane _data_streams chain).
        self._control_chains: Dict[int, StreamChain] = {}
        # Control messages generated before the control write stream is
        # up (the d18 server opens its write-uni inside the SETUP
        # handler; replies to a pipelined client can race it). Flushed
        # right after our SETUP goes out.
        self._pending_control_msgs: List[MOQTMessage] = []
        # Undecided d18 uni-stream classification prefixes: a peer may
        # split even the stream-type vint across packets, so the first
        # bytes are held here until SETUP-or-data can be decided.
        self._uni_peek_stash: Dict[int, bytes] = {}
        # Tombstone map: stream_ids whose state was popped in response
        # to RESET / STOP_SENDING / UNSUBSCRIBE / SubscribeDone teardown.
        # _on_stream_data drops chunks for these — the publisher's
        # in-flight bytes can arrive AFTER the local state pop (a few
        # hundred ms of in-flight + buffered data at multi-Gbps).
        # Recreating fresh state would race with the already-parsed
        # prefix and parse-fail at byte 0 with the WRONG-looking
        # SubgroupHeader signature.
        #
        # Time-based eviction (30s window) bounds memory in long-running
        # sessions and is far longer than any reasonable in-flight
        # window. At 100K streams/s churn the dict holds ~3M entries =
        # ~84 MB peak — acceptable for a bench, and a real workload
        # at sustained 100K stream/s would have other priorities. For
        # production tracks the entries get evicted eventually.
        self._stream_torn_down: Dict[int, float] = {}
        self._stream_torn_down_evict_after: float = 30.0
        self._stream_torn_down_last_sweep: float = 0.0
        self._tasks: Set[asyncio.Task] = set()
        self._close_err = None  # tuple holding latest (error_code, Reason_phrase)

        self._bidi_streams: Dict[int, int] = {}  # map request_id to bidi stream_id (d16)
        self._bidi_stream_requests: Dict[int, int] = {}  # map bidi stream_id to request_id (d16)
        self._track_aliases: Dict[int, int] = {}  # map alias to subscription_id
        self._subscriptions: Dict[int, List] = {}  # map subscription_id to request
        self._pending_requests: Dict[int, Future[MOQTMessage]] = {}  # unified response futures
        # Bounded record of request ids WE issued (recorded at allocation).
        # A response for one of these with no live future is an ack we did
        # not await — a fire-and-forget publish / publish_namespace
        # (wait_response=False), or a late / duplicate reply — logged at
        # DEBUG. A response for an id we never issued stays WARNING.
        self._sent_requests: deque = deque(maxlen=1024)

        # Reverse maps for binding lookups. The forward direction is
        # _streams[sid].key = ('fetch', request_id) or
        # ('subgroup', (track_alias, group_id, subgroup_id));
        # _unbind_stream uses .key to drop the matching reverse entry.
        self._fetch_stream_by_request: Dict[int, int] = {}
        self._subgroup_stream_by_key: Dict[Tuple[int, int, Optional[int]], int] = {}

        # Per-session custom handler overrides {msg_type: handler}; the
        # message class always comes from the per-draft CONTROL_REGISTRY.
        self._control_msg_overrides = dict(session._control_msg_handlers)

        self._stream_data_registry = dict(_MOQTSessionMixin.MOQT_STREAM_DATA_REGISTRY)
        self._dgram_data_registry = dict(_MOQTSessionMixin.MOQT_DGRAM_DATA_REGISTRY)

        # Optional callback for received data objects:
        #   fn(msg, size_bytes, recv_time_ms, group_id, subgroup_id)
        self.on_object_received: Optional[Callable] = None
        # Optional callback for received FetchObjects (fetch uni stream):
        #   fn(msg, size_bytes, recv_time_ms, request_id)
        # Fires for normal objects only (end-of-range markers are logged
        # and the stream is expected to FIN shortly after).
        self.on_fetch_object: Optional[Callable] = None

        # Per-fetch completion futures. Resolved when the fetch uni
        # stream's processing task exits (FIN, RESET, or error).
        # JoinedTrack registers a future here to get notified when
        # the fetch buffer fill is done.
        self._fetch_done_futures: Dict[int, Future] = {}  # request_id → Future

        # Queue for namespace announcements (populated by _handle_namespace)
        self._namespace_announcements: asyncio.Queue = asyncio.Queue()
        # Queue for track announcements (populated by _handle_publish)
        self._publish_announcements: asyncio.Queue = asyncio.Queue()

    # -- Error response types (any version) --
    _ERROR_TYPES = (SubscribeError, PublishError, FetchError,
                    PublishNamespaceError, SubscribeNamespaceError,
                    TrackStatusError, RequestError)

    @staticmethod
    def _is_error_response(msg: MOQTMessage) -> bool:
        """Check if a message is any kind of error response (d14 or d16)."""
        return isinstance(msg, _MOQTSessionMixin._ERROR_TYPES)

    def _resolve_request(self, request_id: int, msg: MOQTMessage) -> None:
        """Resolve a pending request future by request_id.

        A response for an id we issued but are not awaiting — a
        fire-and-forget publish / publish_namespace (wait_response=False),
        or a late / duplicate reply — is expected protocol traffic and
        logged at DEBUG. Only a response for an id we never issued is
        WARNING-worthy (a genuine peer/demux anomaly)."""
        future = self._pending_requests.get(request_id)
        if future and not future.done():
            future.set_result(msg)
        elif request_id in self._sent_requests:
            logger.debug(f"MOQT event: response for un-awaited/duplicate request_id={request_id}: {type(msg).__name__}")
        else:
            logger.warning(f"MOQT event: unsolicited response for request_id={request_id}: {type(msg).__name__}")

    async def _await_response(self, request_id: int, timeout: float = 10.0):
        """Await a pending request response, raise MOQTRequestError on error.

        Returns the OK response message. Raises MOQTRequestError if the
        response is an error (any draft version), or on timeout.
        Honors a future pre-registered by the sender (see join()) so
        responses arriving on the loopback hot path before the awaiter
        registers don't get marked unsolicited.
        """
        fut = self._pending_requests.get(request_id)
        if fut is None:
            fut = self._loop.create_future()
            self._pending_requests[request_id] = fut
        try:
            async with asyncio.timeout(timeout):
                response = await fut
        except asyncio.TimeoutError:
            raise MOQTRequestError(
                error_code=0x02,  # TIMEOUT
                reason="Request timed out",
                retry_interval=0,
            )
        finally:
            self._pending_requests.pop(request_id, None)

        if self._is_error_response(response):
            raise MOQTRequestError(
                error_code=getattr(response, 'error_code', 0),
                reason=getattr(response, 'reason', ''),
                retry_interval=getattr(response, 'retry_interval', 0),
                response=response,
            )
        return response

    async def await_fetch_done(self, request_id: int,
                              timeout: float = 10.0) -> bool:
        """Wait for a fetch stream to complete (FIN).

        The future is pre-registered by join() or fetch() before
        messages are sent, so there's no race with fast completions.
        Returns True if the stream completed cleanly, False on
        error/timeout.

        Args:
            request_id: The fetch request_id (from the Fetch message
                or FetchOk.request_id).
            timeout: seconds to wait.
        """
        fut = self._fetch_done_futures.get(request_id)
        if fut is None:
            # No future registered — check if stream already gone
            if request_id not in self._fetch_stream_by_request:
                return True
            fut = self._loop.create_future()
            self._fetch_done_futures[request_id] = fut

        if fut.done():
            return fut.result()

        try:
            async with asyncio.timeout(timeout):
                return await fut
        except asyncio.TimeoutError:
            return False

    def _get_control_entry(self, msg_type: int) -> Tuple[Type[MOQTMessage], Callable]:
        """(message_class, handler) for a control message type on the
        session's negotiated draft.

        Single keyed lookup into the per-draft CONTROL_REGISTRY — no
        version branching. A per-session custom handler (register_handler)
        overrides the default handler; the class always comes from the
        draft's table. An unknown code point for this draft is a wire
        violation.
        """
        try:
            msg_class, handler = self.CONTROL_REGISTRY[self.negotiated_draft][msg_type]
        except KeyError:
            raise MOQTProtocolViolation(
                f"control message type {int(msg_type):#x} not valid for "
                f"draft-{self.negotiated_draft}")
        override = self._control_msg_overrides.get(msg_type)
        if override is not None:
            handler = override
        return (msg_class, handler)

    async def __aexit__(self, exc_type, exc, tb):
        # Clean up the context when the session exits

        return await super().__aexit__(exc_type, exc, tb)

    @staticmethod
    def _make_namespace_tuple(namespace: Union[str, Tuple[str, ...]]) -> Tuple[bytes, ...]:
        """Convert string or tuple into bytes tuple.

        Splits on '/' by default. Use '\\/' to escape a literal slash
        within a namespace element (e.g. 'live\\/stream1' → single
        element b'live/stream1').
        """
        if isinstance(namespace, str):
            # Split on unescaped '/' only
            parts = []
            current = []
            i = 0
            while i < len(namespace):
                if namespace[i] == '\\' and i + 1 < len(namespace) and namespace[i + 1] == '/':
                    current.append('/')
                    i += 2
                elif namespace[i] == '/':
                    parts.append(''.join(current))
                    current = []
                    i += 1
                else:
                    current.append(namespace[i])
                    i += 1
            parts.append(''.join(current))
            return tuple(p.encode() for p in parts if p)
        elif isinstance(namespace, tuple):
            if all(isinstance(x, bytes) for x in namespace):
                return namespace
            return tuple(part.encode() if isinstance(part, str) else part for part in namespace)
        raise ValueError("namespace must be string with '/' delimiters or tuple")

    def _allocate_request_id(self) -> int:
        """Get next available subscribe ID."""
        request_id = self._next_request_id
        self._next_request_id += 2
        self._sent_requests.append(request_id)
        return request_id

    def _allocate_track_alias(self, request_id: int = 1) -> int:
        """Get next available track alias."""
        track_alias = self._next_track_alias
        self._next_track_alias += 1
        self._track_aliases[track_alias] = request_id
        return track_alias

    def _control_task_done(self, task: asyncio.Task) -> None:
        """Remove control task from set."""
        self._tasks.discard(task)
        if task.cancelled():
            logger.warning("MOQT warn: control task cancelled")
        else:
            e = task.exception()
            if e: logger.error(f"MOQT error: control task failed with exception: {e}")

    def _path_match(self, path: Union[bytes,str]):
        configured = getattr(self._session, 'path')
        if configured is None:
            return False
        if isinstance(configured, bytes):
            configured = configured.decode('utf-8')
        if isinstance(path, bytes):
            path = path.decode('utf-8')

        configured = configured.strip('/')
        path = path.strip('/')
        logger.debug(f"H3 event: configured path: {configured} request path: {path}")
        return configured == path

    # Drain-loop sentinel: an ignorable (skip-unknown) message was
    # consumed — distinct from None, which is fatal.
    _MSG_SKIPPED = object()

    def _moqt_handle_control_message(self, buf: Buffer, *,
                                     request_id: Optional[int] = None
                                     ) -> Optional[MOQTMessage]:
        """Process an incoming message.

        request_id, when supplied (d18 request-stream replies), is the id
        the stream is bound to. d18 replies omit the Request ID on the
        wire (demuxed by stream); injecting it lets handlers that key on
        msg.request_id work unchanged."""
        buf_len = buf.capacity
        if buf_len == 0:
            logger.warning("MOQT event: handle control message: no data")
            return None

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"MOQT event: handle control message: ({buf_len} bytes) "
                f"0x{buf.data_slice(0, min(buf_len, 64)).hex()}")
        start_pos = buf.tell()
        # d18 control framing uses vi64 for the Message Type (0x2F00
        # -> AF 00); pre-d18 uses the RFC9000 varint. Length stays
        # 16-bit in all drafts (§10.1). This is the single chokepoint
        # for ALL control messages (the bidi, d18 uni, and per-request
        # paths all route here), so tagging buf.vi64 once here makes
        # every downstream message deserialize read its body integers
        # in the negotiated flavor via buf.pull_vint — control message
        # bodies do not each re-tag.
        prof = self._profile
        buf.vi64 = prof.vi64
        # Header pulls and the declared-length guard are the ONLY
        # legitimate "need more bytes" signals — normalized to
        # MOQTUnderflow so the drain loop retains the bytes and
        # reassembles on the next StreamDataReceived event.
        try:
            msg_type = buf.pull_vint()
            msg_len = buf.pull_uint16()
        except (MOQTUnderflow, BufferReadError):
            raise MOQTUnderflow(start_pos, 3) from None
        hdr_len = buf.tell() - start_pos
        end_pos = start_pos + hdr_len + msg_len
        if buf.tell() + msg_len > buf_len:
            raise MOQTUnderflow(buf.tell(), msg_len)
        try:
            # Resolve the message type. RFC9000 drafts keep the lenient
            # enum-coercion + skip-unknown surface. vi64 drafts (d18) let
            # the per-draft CONTROL_REGISTRY be the authority — renumbered
            # code points (SETUP 0x2F00, SUBSCRIBE_NAMESPACE 0x50, ...) fall
            # outside MOQTMessageType's contiguous range, and an unknown
            # type closes the session via _get_control_entry (§10.1 MUST).
            if prof.varint != "vi64":
                try:
                    msg_type = MOQTMessageType(msg_type)
                except ValueError:
                    logger.error(f"MOQT error: unknown control message: type: {hex(msg_type)} start: {start_pos} len: {msg_len}")
                    # Skip-unknown surface: consume the message and let
                    # the drain loop continue (None means fatal there).
                    buf.seek(end_pos)
                    return self._MSG_SKIPPED
            # Look up message class (version-aware for shared code points)
            message_class, handler = self._get_control_entry(msg_type)
            logger.debug(f"MOQT event: control message: {message_class.__name__} ({msg_len} bytes)")
            # Deserialize message. Pass end_pos so trailing fields
            # without a wire-level length prefix (d16 Track Extensions
            # in PUBLISH / SUBSCRIBE_OK / FETCH_OK) know where to stop
            # instead of reading past the payload. Other message types
            # accept and ignore it for signature uniformity.
            msg = message_class.deserialize(buf, prof=self._profile, buf_end=end_pos)
            # d18 replies omit the Request ID (demuxed by request stream);
            # inject the stream-bound id so handlers key on it unchanged.
            if (request_id is not None and
                    not self._profile.reply_has_request_id and
                    hasattr(msg, 'request_id')):
                msg.request_id = request_id
            msg_len += hdr_len
            if end_pos > buf.tell():
                logger.debug(f"MOQT event: control message: seeking msg end: {end_pos}")
                buf.seek(end_pos)
            # assert start_pos + msg_len == (buf.tell())
            logger.info(f"MOQT event: control message parsed: {msg})")

            # Schedule handler if one exists
            if handler is not None:
                logger.debug(f"MOQT event: creating handler task: {getattr(handler, '__name__', repr(handler))}")
                task = asyncio.create_task(handler(self, msg))
                task.add_done_callback(self._control_task_done)
                self._tasks.add(task)

            return msg

        except (MOQTUnderflow, BufferReadError) as e:
            # The full declared body was present (guard above), so a
            # short read inside deserialize is a malformed body — not
            # fragmentation. Waiting for more bytes would stall the
            # control stream forever.
            error = (f"malformed control message: type=0x{int(msg_type):x} "
                     f"len={msg_len}: {type(e).__name__}")
            logger.error(f"handle_control_message: {error}")
            self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
            return None
        except MOQTProtocolViolation as e:
            logger.error(
                f"handle_control_message: protocol violation: "
                f"{e.reason_phrase}"
            )
            self._close_session(e.error_code, e.reason_phrase)
            return None
        except Exception as e:
            import traceback
            logger.error(
                f"handle_control_message: error handling control "
                f"message: {type(e).__name__}: {e!r}"
            )
            logger.error(
                f"handle_control_message: traceback:\n"
                f"{''.join(traceback.format_exception(type(e), e, e.__traceback__))}"
            )
            raise

    def _dump_data_streams(self, file=None) -> dict:
        """Per-stream chain-introspection. Returns and prints a dict of
        {stream_id: (chain_chunks, chain_total_bytes, bytes_parsed,
                     group_id, object_id, parser_type)}.
        Used by SIGUSR2 to localize where bytes are pinned downstream
        of aiopquic's drain. Called from aiomoqt.utils.taskdump."""
        import sys
        if file is None:
            file = sys.stderr
        out = {}
        total_chains = 0
        total_chunks = 0
        total_bytes = 0
        for sid, state in self._data_streams.items():
            chain = state.chain
            # _chunks is the internal deque; len() is the count.
            try:
                chunks_n = len(chain._chunks)
            except Exception:
                chunks_n = -1
            total_chunks += max(0, chunks_n)
            total_bytes += chain.capacity
            total_chains += 1
            out[sid] = (chunks_n, chain.capacity, state.bytes_total,
                        state.group_id, state.object_id,
                        type(state.parser).__name__ if state.parser
                        else None)
        print(f"=== aiomoqt chain-dump "
              f"({total_chains} active streams, "
              f"{total_chunks} chunks pinned, "
              f"{total_bytes} bytes pending parse) ===",
              file=file, flush=True)
        for sid in sorted(out.keys()):
            n_chunks, cap, parsed, gid, oid, pt = out[sid]
            print(f"  sid={sid:<5} chunks={n_chunks:<6} "
                  f"chain_bytes={cap:<10} parsed={parsed:<12} "
                  f"group={gid} object={oid} parser={pt}",
                  file=file, flush=True)
        print(f"=== end chain-dump ===", file=file, flush=True)
        return out

    def _cleanup_stream(self, stream_id: int,
                        error_code: int = QuicErrorCode.NO_ERROR) -> None:
        """Per-uni-stream end-of-life. Replaces the per-task done
        callback now that there is no per-stream task — every place
        that used to cancel a task or push a FIN sentinel calls this
        directly. Tombstones the stream so late chunks are dropped,
        resolves any fetch-completion future, and unbinds the
        reverse-map entry."""
        state = self._data_streams.pop(stream_id, None)
        self._control_chains.pop(stream_id, None)
        self._uni_peek_stash.pop(stream_id, None)
        self._mark_stream_torn_down(stream_id)
        key = state.key if state is not None else None
        if key and len(key) == 2 and key[0] == 'fetch':
            request_id = key[1]
            fut = self._fetch_done_futures.pop(request_id, None)
            if fut and not fut.done():
                fut.set_result(error_code == QuicErrorCode.NO_ERROR)
        self._unbind_key(key)

    def _mark_stream_torn_down(self, stream_id: int) -> None:
        """Mark a stream torn down so subsequent in-flight chunks are
        dropped rather than recreating fresh state (which would parse-
        fail at byte 0). Time-based eviction (sweep at most once per
        second; entries older than _stream_torn_down_evict_after are
        dropped)."""
        now = time.monotonic()
        self._stream_torn_down[stream_id] = now
        # Sweep at most once per second to bound CPU under high churn.
        if now - self._stream_torn_down_last_sweep > 1.0:
            self._stream_torn_down_last_sweep = now
            cutoff = now - self._stream_torn_down_evict_after
            # Build the evict list inline to avoid mutating during iter.
            stale = [sid for sid, t in self._stream_torn_down.items()
                       if t < cutoff]
            for sid in stale:
                self._stream_torn_down.pop(sid, None)

    def _on_stream_data(self, stream_id: int, data: bytes,
                        end_stream: bool) -> None:
        """Per-chunk handler. Synchronous: no per-stream asyncio task.
        Extends the chain, parses every complete message in-place,
        cleans up on FIN. Reject/desync paths call _cleanup_stream
        via _reject_stream; session-fatal paths call _close_session.
        """
        if stream_id in self._stream_torn_down:
            return
        state = self._data_streams.get(stream_id)
        if state is None:
            state = _DataStreamState(chain=StreamChain())
            self._data_streams[stream_id] = state

        if data and len(data) > 0:
            state.chain.extend(data)

        if state.chain.capacity > 0:
            self._drain_stream(stream_id, state)

        if end_stream and stream_id in self._data_streams:
            self._cleanup_stream(stream_id)

    def _drain_stream(self, stream_id: int, state: _DataStreamState) -> None:
        """Drain as many complete messages as the chain currently holds.

        Synchronous — runs inside the asyncio event-loop thread. The
        save/rollback/commit pattern survives across calls because the
        chain is per-state. On underflow: chain.rollback() and return
        (wait for more chunks). On stream-level reject: _reject_stream.
        On session-fatal: _close_session.
        """
        chain = state.chain
        while chain.capacity > 0:
            chain.save()
            try:
                msg_obj = self._moqt_handle_data_stream(
                    stream_id, chain, chain.capacity  # type: ignore[arg-type]
                )
            except MOQTStreamReject as e:
                self._reject_stream(stream_id, e.error_code, e.reason)
                return
            except MOQTUnderflow:
                chain.rollback()
                return
            except BufferReadError:
                chain.rollback()
                return
            except Exception as e:
                tell = chain.tell()
                hex_anchor = chain.data_slice(
                    0, min(64, chain.capacity)
                ).hex()
                last_obj = getattr(self, '_last_parse_obj_id', None)
                logger.error(
                    f"MOQT stream({stream_id}): PARSE EXCEPTION "
                    f"at tell={tell} chain_total={chain.capacity} "
                    f"last_obj_id={last_obj} "
                    f"stream_bytes={state.bytes_total} "
                    f"object_id={state.object_id} "
                    f"group_id={state.group_id} "
                    f"exc={type(e).__name__}: {e}"
                )
                logger.error(
                    f"MOQT stream({stream_id}): hex anchor (first 64): "
                    f"{hex_anchor}"
                )
                self._reject_stream(
                    stream_id,
                    SessionCloseCode.PROTOCOL_VIOLATION,
                    f"parse error: {type(e).__name__}",
                )
                return

            if msg_obj is None:
                error = (
                    f"MOQT error: data stream({stream_id}): parsing "
                    f"returned None at position: {chain.tell()} of "
                    f"{chain.capacity} bytes"
                )
                logger.error(error)
                self._close_session(
                    SessionCloseCode.PROTOCOL_VIOLATION, error
                )
                return

            consumed = chain.tell()
            state.bytes_total += consumed
            self._last_parse_obj_id = (
                msg_obj.object_id
                if hasattr(msg_obj, 'object_id') else None
            )
            chain.commit()

            if isinstance(msg_obj, ObjectHeader):
                assert (state.object_id is None
                        or msg_obj.object_id > state.object_id)
                state.object_id = msg_obj.object_id
                if msg_obj.status in (
                    ObjectStatus.END_OF_GROUP,
                    ObjectStatus.END_OF_TRACK,
                ):
                    self._cleanup_stream(stream_id)
                    return
                if self.on_object_received:
                    now = int(time.time() * 1_000_000)
                    self.on_object_received(
                        msg_obj, consumed, now,
                        state.group_id, state.subgroup_id
                    )
            elif isinstance(msg_obj, SubgroupHeader):
                assert (state.group_id is None
                        or msg_obj.group_id > state.group_id)
                state.group_id = msg_obj.group_id
                state.subgroup_id = msg_obj.subgroup_id
            elif isinstance(msg_obj, FetchHeader):
                pass
            elif isinstance(msg_obj, FetchObject):
                if msg_obj.end_of_range is None and self.on_fetch_object:
                    parser = state.parser
                    request_id = (
                        parser.request_id
                        if isinstance(parser, FetchHeader)
                        else None
                    )
                    now = int(time.time() * 1_000_000)
                    self.on_fetch_object(
                        msg_obj, consumed, now, request_id
                    )
            else:
                logger.error(
                    f"MOQT stream({stream_id}): unexpected msg "
                    f"{type(msg_obj).__name__} size: {consumed} bytes"
                )

    def _reject_stream(self, stream_id: int, error_code: int, reason: str) -> None:
        """Terminate a uni stream at the MoQT layer via STOP_SENDING.

        Used for admission failures (unknown request_id/track_alias, stream
        reuse, budget exceeded). Stream-level only — never closes the
        session. Removes any binding table entries for the stream.
        """
        logger.warning(f"MOQT stream({stream_id}): rejecting: "
                       f"{reason} (code=0x{error_code:x})")
        self.stream_stop_sending(stream_id, error_code)
        self._cleanup_stream(stream_id, QuicErrorCode.APPLICATION_ERROR)

    def _unbind_key(self, key) -> None:
        """Drop the reverse-map entry for a binding key tuple.

        key is the StreamState.key field — ('fetch', request_id) or
        ('subgroup', (track_alias, group_id, subgroup_id)) — or None.
        """
        if key is None:
            return
        if len(key) == 2 and key[0] == 'fetch':
            self._fetch_stream_by_request.pop(key[1], None)
        elif len(key) == 2 and key[0] == 'subgroup':
            self._subgroup_stream_by_key.pop(key[1], None)

    def _unbind_stream(self, stream_id: int) -> None:
        """Compat shim: look up the stream's binding key and drop the
        reverse-map entry. Prefer _unbind_key when the caller already
        has the key in hand."""
        state = self._data_streams.get(stream_id)
        if state is not None:
            self._unbind_key(state.key)
            state.key = None

    def _admit_fetch_stream(self, stream_id: int, header: 'FetchHeader') -> None:
        """Admit a FETCH_HEADER stream or raise MOQTStreamReject.

        Admission rules:
        - request_id MUST match an outstanding FETCH we sent
          (present in _subscriptions with a Fetch message).
        - at most one uni stream per FETCH request (no reuse).
        """
        request_id = header.request_id
        outstanding = self._subscriptions.get(request_id)
        is_fetch = (outstanding is not None
                    and any(isinstance(m, Fetch) for m in outstanding))
        if not is_fetch:
            raise MOQTStreamReject(
                SessionCloseCode.PROTOCOL_VIOLATION,
                f"fetch stream for unknown request_id={request_id}")
        if request_id in self._fetch_stream_by_request:
            existing = self._fetch_stream_by_request[request_id]
            if existing != stream_id:
                raise MOQTStreamReject(
                    SessionCloseCode.PROTOCOL_VIOLATION,
                    f"duplicate fetch stream for request_id={request_id} "
                    f"(already bound to stream {existing})")
        self._fetch_stream_by_request[request_id] = stream_id
        state = self._data_streams.get(stream_id)
        if state is not None:
            state.key = ('fetch', request_id)

    def _admit_subgroup_stream(self, stream_id: int,
                                header: 'SubgroupHeader') -> None:
        """Admit a SubgroupHeader stream or raise MOQTStreamReject.

        Admission rules:
        - track_alias SHOULD map to a live subscription, but per spec
          §10.4.2 data may arrive before the control message that
          establishes the alias — tolerate with a warning.
        - at most one stream per (track_alias, group_id, subgroup_id)
          tuple. In FIRST_OBJ mode subgroup_id is None at header time
          and the uniqueness check is deferred until the first object.
        """
        if header.track_alias not in self._track_aliases:
            logger.warning(
                f"MOQT stream({stream_id}): subgroup with "
                f"unknown track_alias={header.track_alias} "
                f"(may resolve via pending control message)")
        # subgroup_id is None in FIRST_OBJ mode — resolved on first object
        key = (header.track_alias, header.group_id, header.subgroup_id)
        if header.subgroup_id is not None:
            if key in self._subgroup_stream_by_key:
                existing = self._subgroup_stream_by_key[key]
                if existing != stream_id:
                    raise MOQTStreamReject(
                        SessionCloseCode.PROTOCOL_VIOLATION,
                        f"duplicate subgroup stream for {key} "
                        f"(already bound to stream {existing})")
            self._subgroup_stream_by_key[key] = stream_id
            state = self._data_streams.get(stream_id)
            if state is not None:
                state.key = ('subgroup', key)

    def _bind_subgroup_first_obj(self, stream_id: int,
                                  header: 'SubgroupHeader') -> None:
        """Complete FIRST_OBJ-mode subgroup binding once subgroup_id is
        resolved from the first object."""
        key = (header.track_alias, header.group_id, header.subgroup_id)
        if key in self._subgroup_stream_by_key:
            existing = self._subgroup_stream_by_key[key]
            if existing != stream_id:
                raise MOQTStreamReject(
                    SessionCloseCode.PROTOCOL_VIOLATION,
                    f"duplicate subgroup stream for {key} "
                    f"(already bound to stream {existing})")
        self._subgroup_stream_by_key[key] = stream_id
        state = self._data_streams.get(stream_id)
        if state is not None:
            state.key = ('subgroup', key)

    def _moqt_handle_data_stream(self, stream_id: int, buf: Buffer, len: int) -> MOQTMessage:
        """Process incoming data messages (not control messages).

        Raises MOQTStreamReject on MoQT-level admission failure. Caller
        must catch and invoke _reject_stream(). Data-plane parse errors
        still return None (session-closing behavior).
        """
        if buf.capacity == 0 or buf.tell() >= buf.capacity:
            logger.warning(f"MOQT stream({stream_id}): no data at position: {buf.tell()}")
            return

        try:
            pos = buf.tell()
            msg_header = None
            # new data streams will not yet have a parser bound
            stream_state = self._data_streams.get(stream_id)
            if stream_state is None or stream_state.parser is None:
                # Get stream type from first varint. d18 uses vi64 for the
                # stream type (and all data-plane ints); pre-d18 RFC9000.
                buf.vi64 = self._profile.vi64
                stream_type = buf.pull_vint()
                # SubgroupHeader form 0b0XX1XXXX (bit 4 set): d14 0x10-0x1D,
                # d16 += 0x30-0x3D (bit 5 DEFAULT_PRIORITY), d18 += 0x50-0x5D
                # / 0x70-0x7D (bit 6 FIRST_OBJECT) and excludes bit 7.
                # Reserved subgroup_id_mode 0b11 (bits 1-2) is invalid.
                is_subgroup = (
                    (stream_type & 0x10) and not (stream_type & 0x80)
                    and ((stream_type >> 1) & 0x03) != 3
                )
                if stream_type in (PADDING_STREAM_TYPE,):
                    # d18 PADDING stream: drain + discard (MUST keep flow
                    # control moving). No parser bound; bytes ignored.
                    data_type = "PADDING"
                    msg_header = None
                    raise MOQTStreamReject(
                        SessionCloseCode.NO_ERROR, "padding stream drained")
                elif is_subgroup:
                    msg_header = SubgroupHeader.deserialize(
                        buf, type_val=stream_type, prof=self._profile)
                    data_type = "SUBGROUP_HEADER"
                    self._admit_subgroup_stream(stream_id, msg_header)
                elif stream_type == DataStreamType.FETCH_HEADER:
                    msg_header = FetchHeader.deserialize(buf)
                    data_type = "FETCH_HEADER"
                    self._admit_fetch_stream(stream_id, msg_header)
                else:
                    data_type = f"0x{stream_type:x}"
                    logger.warning(f"MOQT stream({stream_id}): unexpected data stream type: {data_type}")

                if msg_header is None:
                    # Stream-level parse failure on the data stream type
                    # byte. Caused by data-corruption races at high
                    # stream-churn rates (see issue: known framer desync
                    # under load). Reject this stream and let the session
                    # keep running rather than tearing the whole session
                    # down for one corrupt stream — the publisher will
                    # see STOP_SENDING and abandon, the rest of the
                    # session continues. Forensic anchor logged below.
                    try:
                        anchor = buf.data_slice(0, min(64, buf.capacity)).hex()
                    except Exception:
                        anchor = "<unavailable>"
                    logger.warning(
                        f"MOQT stream({stream_id}): {data_type} parse "
                        f"failed at {buf.tell()} of {buf.capacity}; "
                        f"rejecting (head_hex={anchor})"
                    )
                    raise MOQTStreamReject(
                        SessionCloseCode.PROTOCOL_VIOLATION,
                        f"stream-type parse failed: {data_type}",
                    )

                # record that the data stream header has been processed
                consumed = buf.tell() - pos
                logger.debug(f"MOQT stream({stream_id}): {msg_header} consumed: {consumed} bytes")
                if stream_state is not None:
                    stream_state.parser = msg_header
            else:
                parser = stream_state.parser
                if isinstance(parser, SubgroupHeader):
                    sg_header: SubgroupHeader = parser
                    # Reuse a per-stream cached ObjectHeader — saves the
                    # dataclass allocation on every object. Subscriber
                    # callback contract is "msg valid until next call".
                    obj = sg_header._obj_cache
                    if obj is None:
                        obj = ObjectHeader.__new__(ObjectHeader)
                        sg_header._obj_cache = obj
                    obj.deserialize_into(
                        buf, len,
                        extensions_present=sg_header.extensions_present,
                        prev_object_id=sg_header._last_object_id,
                        vi64=sg_header._vi64,
                    )
                    msg_header = obj
                    # Update delta tracking state
                    sg_header._last_object_id = msg_header.object_id
                    # Resolve subgroup_id for FIRST_OBJ mode and complete
                    # the deferred subgroup binding
                    if (sg_header.subgroup_id_mode == SUBGROUP_ID_FIRST_OBJ
                            and sg_header.subgroup_id is None):
                        sg_header.subgroup_id = msg_header.object_id
                        self._bind_subgroup_first_obj(stream_id, sg_header)

                elif isinstance(parser, FetchHeader):
                    fh: FetchHeader = parser
                    msg_header = FetchObject.deserialize(buf, prior=fh._prior_obj, prof=self._profile)
                    # Track prior object for d16 delta-encoded references
                    if msg_header.end_of_range is None:
                        fh._prior_obj = msg_header

                if msg_header is None:
                    error = f"MOQT stream({stream_id}): ObjectHeader parse failed at: {buf.tell()}"
                    logger.error(f"MOQT error: " + error)
                    self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
                    return None


            return msg_header
        except MOQTStreamReject:
            raise
        except Exception:
            raise

    def _moqt_handle_data_dgram(self, buf: Buffer) -> MOQTMessageType:
        """Process incoming datagram messages."""
        if buf.capacity == 0 or buf.tell() >= buf.capacity:
            logger.error(f"MOQT datagram: no data {buf.tell()}")
            return
        logger.debug(f"MOQT handle datagram: 0x{buf.data_slice(0,min(buf.capacity,12))}")
        # Get datagram type from first varint (vi64 for d18).
        pos = buf.tell()
        prof = self._profile
        buf.vi64 = prof.vi64
        dgram_type = buf.pull_vint()
        if prof.varint == "vi64":
            return self._moqt_handle_data_dgram_d18(buf, pos, dgram_type)
        # Draft-14: ObjectDatagram types 0x00-0x07 (payload datagrams)
        if 0x00 <= dgram_type <= 0x07:
            msg = ObjectDatagram.deserialize(buf, buf.capacity, type_val=dgram_type, prof=self._profile)
            if msg is None:
                error = f"datagram parsing failed at: {buf.tell()}"
                logger.error(f"MOQT error: " + error)
                self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
                return msg

            consumed = buf.tell() - pos
            group_id = msg.group_id
            object_id = msg.object_id
            id = f"{group_id}.{object_id}"
            now = int(time.time() * 1_000_000)
            msg_ts = msg.extensions.get(MOQT_TIMESTAMP_EXT) if msg.extensions else None
            delay = f"delay: {now - msg_ts} ms" if msg_ts else ""
            logstr = f"{id} size: {consumed} bytes {delay}"

            logger.debug(f"MOQT event: ObjectDatagram: {logstr}")
            if self.on_object_received:
                self.on_object_received(msg, consumed, now, group_id, None)
            return msg
        # Draft-14: ObjectDatagramStatus types 0x20-0x21 (status datagrams)
        elif 0x20 <= dgram_type <= 0x21:
            msg = ObjectDatagramStatus.deserialize(buf, type_val=dgram_type)
            if msg is None:
                error = f"datagram parsing failed at: {buf.tell()}"
                logger.error(f"MOQT error: " + error)
                self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
                return msg

            consumed = buf.tell() - pos
            group_id = msg.group_id
            object_id = msg.object_id
            id = f"{group_id}.{object_id}"
            now = int(time.time() * 1_000_000)
            msg_ts = msg.extensions.get(MOQT_TIMESTAMP_EXT) if msg.extensions else None
            delay = f"delay: {now - msg_ts} ms" if msg_ts else ""
            logstr = f"{id} size: {consumed} bytes {delay}"

            logger.debug(f"MOQT event: ObjectDatagramStatus: {logstr}")
            return msg
        else:
            error = f"datagram type unknown: 0x{dgram_type:x}"
            logger.error(f"MOQT error: " + error)
            self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
            return

    # d18 OBJECT_DATAGRAM invalid types: STATUS (0x20) + END_OF_GROUP (0x02)
    # both set — a status datagram cannot signal end of group (§11.3.1).
    _D18_DGRAM_INVALID = frozenset(
        {0x22, 0x23, 0x26, 0x27, 0x2A, 0x2B, 0x2E, 0x2F})

    def _moqt_handle_data_dgram_d18(self, buf: Buffer, pos: int,
                                    dgram_type: int) -> MOQTMessage:
        """d18 datagram dispatch. OBJECT_DATAGRAM form 0b00X0XXXX
        (0x00-0x0F / 0x20-0x2F); PADDING datagram 0x132B3E29 is drained."""
        if dgram_type == PADDING_DATAGRAM_TYPE:
            logger.debug("MOQT event: PADDING datagram drained")
            return
        valid_form = (dgram_type & ~0x2F) == 0 and (dgram_type & 0x10) == 0
        if (not valid_form) or dgram_type in self._D18_DGRAM_INVALID:
            error = f"invalid d18 datagram type: 0x{dgram_type:x}"
            logger.error(f"MOQT error: " + error)
            self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
            return
        msg = ObjectDatagram.deserialize(
            buf, buf.capacity, type_val=dgram_type, prof=self._profile)
        if msg is None:
            error = f"datagram parsing failed at: {buf.tell()}"
            logger.error(f"MOQT error: " + error)
            self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
            return msg
        consumed = buf.tell() - pos
        now = int(time.time() * 1_000_000)
        logger.debug(
            f"MOQT event: d18 ObjectDatagram: {msg.group_id}.{msg.object_id} "
            f"size: {consumed} bytes status: {msg.status}")
        if msg.status == ObjectStatus.NORMAL and self.on_object_received:
            self.on_object_received(msg, consumed, now, msg.group_id, None)
        return msg

    def _on_control_data(self, stream_id: int, data, end_stream: bool,
                         *, is_request_bidi: bool = False) -> None:
        """Accumulate a control/request stream's bytes and parse every WHOLE
        control message, retaining any partial trailing message for the next
        StreamDataReceived event. Mirrors the data-plane _on_stream_data /
        _drain_stream save/rollback/commit reassembly so a control message
        fragmented across events (even mid-header) is reassembled instead of
        crashing on a short read. Serves the d18 uni control stream, the d18
        request bidi streams (is_request_bidi), and the d14/d16 bidi control
        stream."""
        chain = self._control_chains.get(stream_id)
        if chain is None:
            chain = StreamChain()
            self._control_chains[stream_id] = chain
        if data:
            chain.extend(data)
        while chain.capacity > 0:
            request_id = (self._bidi_stream_requests.get(stream_id)
                          if is_request_bidi else None)
            chain.save()
            try:
                msg = self._moqt_handle_control_message(
                    chain, request_id=request_id)
            except MOQTUnderflow:
                chain.rollback()   # partial message — await the next chunk
                if end_stream:
                    # FIN'd mid-message: nothing more can arrive. Release
                    # the chain; a truncated CONTROL stream is fatal.
                    logger.error(
                        f"MOQT control stream({stream_id}): FIN with "
                        f"truncated message "
                        f"({chain.capacity - chain.tell()} residual bytes)")
                    self._control_chains.pop(stream_id, None)
                    if stream_id == self._control_read_stream_id:
                        self._close_session(
                            SessionCloseCode.PROTOCOL_VIOLATION,
                            "control stream FIN with truncated message")
                return
            except Exception as e:
                # Containment (data-plane _drain_stream parity): a parse
                # exception must not escape into the transport event loop
                # (WT would drop the rest of its batch) nor leave the
                # chain mid-message. A broken control stream is fatal.
                logger.error(
                    f"MOQT control stream({stream_id}): parse exception at "
                    f"tell={chain.tell()} of {chain.capacity}: "
                    f"{type(e).__name__}: {e} head_hex="
                    f"{chain.data_slice(0, min(64, chain.capacity)).hex()}")
                self._close_session(
                    SessionCloseCode.PROTOCOL_VIOLATION,
                    f"control parse error: {type(e).__name__}")
                return
            if msg is None:
                error = (f"control stream({stream_id}): parse failed at "
                         f"{chain.tell()} of {chain.capacity} bytes")
                logger.error(f"MOQT error: {error}")
                self._close_session(
                    SessionCloseCode.PROTOCOL_VIOLATION, error)
                return
            chain.commit()
            if msg is self._MSG_SKIPPED:
                continue
            # The first request-opener on a bidi request stream binds it to
            # its Request ID; later replies demux by the bound stream.
            if (is_request_bidi and
                    self._bidi_stream_requests.get(stream_id) is None and
                    isinstance(msg, self._REQUEST_OPENERS) and
                    getattr(msg, 'request_id', None) is not None):
                self._bidi_stream_requests[stream_id] = msg.request_id
                self._bidi_streams[msg.request_id] = stream_id
        if end_stream:
            self._control_chains.pop(stream_id, None)

    @staticmethod
    def _d18_peek_is_setup(data) -> Optional[bool]:
        """Classify a uni stream's leading bytes against the d18 control
        stream type (SETUP, vi64 0x2F00). True/False once decidable;
        None = not enough bytes yet (a vi64 needs up to 9 — a peer may
        split even the type across packets). Non-consuming."""
        if not data:
            return None
        if not isinstance(data, (bytes, bytearray)):
            data = bytes(data)
        try:
            return Buffer(data=data).pull_uint_vi64() == MOQTMessageType.SETUP
        except BufferReadError:
            return None   # with 9 bytes buffered a vi64 always decodes

    def _classify_d18_uni(self, stream_id: int, data, end_stream: bool) -> None:
        """Classify a d18 uni stream by its leading type vint and route
        its bytes: bind the control read-uni on SETUP, else the data
        path. Only the first <=9 bytes are ever materialized for the
        peek; the original chunk is forwarded as-is (zero-copy into the
        data plane). While the vint is undecidable the (<9-byte) prefix
        is stashed. Classification is sticky — once a stream reaches the
        data path it is never re-peeked (a later chunk could start with
        the SETUP type)."""
        stash = self._uni_peek_stash.pop(stream_id, None) or b""
        head = stash + bytes(data[:9])
        verdict = self._d18_peek_is_setup(head)
        if verdict is None:        # total buffered < 9 ⇒ head is all of it
            if end_stream:
                logger.debug(f"MOQT: uni stream({stream_id}) FIN inside "
                             f"its type vint; dropped {len(head)} bytes")
            else:
                self._uni_peek_stash[stream_id] = head
            return
        if verdict:
            self._d18_control_read_sid = stream_id
            logger.debug(f"MOQT: bound d18 control read-uni: {stream_id}")
        ingest = self._on_control_data if verdict else self._on_stream_data
        if stash:
            ingest(stream_id, stash, False)
        ingest(stream_id, data, end_stream)

    # primary event handling for all QUIC messaging
    def quic_event_received(self, event: QuicEvent) -> None:
        """Handle incoming QUIC events."""

        # CONNECTION_CLOSE events terminate the session.
        # StopSendingReceived and StreamReset also have error_code but
        # are stream-level events handled below — do NOT catch them here.
        if (hasattr(event, 'error_code')
                and not isinstance(event, (StopSendingReceived, StreamReset))):
            error = getattr(event, 'error_code', QuicErrorCode.INTERNAL_ERROR)
            reason = getattr(event, 'reason_phrase', None) or class_name(event)
            if error == 0:
                logger.info(f"QUIC: connection closed: code: {error}")
            else:
                logger.error(f"QUIC error: code: {error} reason: {reason}")
            self._close_session(error, reason)
            return

        if isinstance(event, ProtocolNegotiated):
            # Enforce supported ALPN. WT path runs over H3 inside picoquic;
            # only MoQT-on-raw-QUIC ALPNs surface here.
            alpn = event.alpn_protocol
            if alpn == MOQT_ALPN or (alpn and alpn.startswith("moqt-")):
                logger.debug(f"QUIC event: ALPN ProtocolNegotiated alpn: {alpn}")
                try:
                    draft = get_major_version(moqt_version_from_alpn(alpn))
                    if draft not in PROFILES:
                        raise ValueError(f"unsupported draft-{draft}")
                    self.negotiated_draft = draft
                    logger.info(f"MOQT: version set from ALPN: {alpn} -> draft-{self.negotiated_draft}")
                except ValueError:
                    logger.error(f"QUIC error: unsupported ALPN version: {alpn}")
                    self._close_session(
                        SessionCloseCode.VERSION_NEGOTIATION_FAILED,
                        f"unsupported ALPN: {alpn}"
                    )
            else:
                logger.error(f"QUIC error: unknown ALPN: {alpn}")
                self._close_session(
                    SessionCloseCode.UNAUTHORIZED,
                    f"unsupported ALPN: {alpn}"
                )
            return
        elif isinstance(event, StreamDataReceived) and self._wt_session_setup.done():
            stream_id = event.stream_id

            if self._close_err is not None or (
                    hasattr(self, '_closed') and self._closed.is_set()):
                # Rate-limit: log once per stream_id to avoid drowning
                # other diagnostics when a closed-but-still-arriving
                # stream gets flooded with packets.
                seen = getattr(self, '_post_close_warned', None)
                if seen is None:
                    seen = set()
                    self._post_close_warned = seen
                if stream_id not in seen:
                    seen.add(stream_id)
                    logger.warning(
                        f"QUIC event: stream data after close: stream {stream_id} "
                        f"(rate-limited to once per stream)"
                    )
                return

            data = event.data if event.data is not None else b""

            # Abrupt close of a critical stream
            if (event.end_stream and len(data) == 0 and
                    stream_id in (self._control_read_stream_id, self._session_id)):
                self._close_session(
                    SessionCloseCode.INTERNAL_ERROR,
                    f"critical stream closed by remote peer: {stream_id}"
                )
                return

            # Uni MoQT streams. In d18 the peer's control stream is a uni
            # stream beginning with SETUP (vi64 0x2F00): bind it on first
            # contact and route it to the control parser. Every other uni
            # stream is data (object subgroups / fetch) -> StreamChain hot
            # path (memoryview-native; no Buffer construction).
            if stream_is_unidirectional(stream_id):
                if self._profile.control_uni_pair:
                    if stream_id in self._uni_peek_stash or (
                            self._d18_control_read_sid is None and
                            stream_id not in self._data_streams and
                            stream_id not in self._stream_torn_down):
                        self._classify_d18_uni(
                            stream_id, data, event.end_stream)
                        return
                    if stream_id == self._d18_control_read_sid:
                        self._on_control_data(
                            stream_id, data, event.end_stream)
                        return
                self._on_stream_data(
                    stream_id, data, event.end_stream)
                return

            # Bidi streams: d18 request streams, or the d14/d16 control
            # stream + d16 request streams. All parse control messages via
            # the reassembling chain ingest (_on_control_data).
            logger.debug(
                f"MOQT event: StreamDataReceived: stream: {stream_id} "
                f"len: {len(data)}")

            # d18: control is a uni pair, so every bidi stream is a request
            # stream (SUBSCRIBE/FETCH/...). Pre-d18 the first bidi stream is
            # latched as the single control stream.
            if self._profile.control_uni_pair:
                self._on_control_data(stream_id, data, event.end_stream,
                                      is_request_bidi=True)
                return

            if self._control_stream_id is None:
                self._control_stream_id = stream_id
                logger.debug(f"QUIC event: detecting control stream: {stream_id}")
            elif stream_id != self._control_stream_id:
                if is_draft16_or_later(self.negotiated_draft):
                    self._on_control_data(stream_id, data, event.end_stream,
                                          is_request_bidi=True)
                    return
                logger.warning(f"MOQT event: unrecognized bidirectional stream({stream_id})")
                return

            if stream_id == self._control_read_stream_id:
                self._on_control_data(stream_id, data, event.end_stream)
                return

        elif isinstance(event, DatagramFrameReceived) and self._wt_session_setup.done():
            msg_buf = Buffer(data=event.data)
            logger.debug(f"MOQT event: DatagramFrameReceived: 0x{msg_buf.data_slice(0,min(msg_buf.capacity,16)).hex()}")
            self._moqt_handle_data_dgram(msg_buf)
            return
        elif isinstance(event, StopSendingReceived):
            logger.debug(f"MOQT event: StopSendingReceived: stream {event.stream_id}")
            # RFC 9000: STOP_SENDING from peer → reciprocal RESET_STREAM.
            self.stream_reset(event.stream_id, event.error_code)
            self._cleanup_stream(
                event.stream_id, QuicErrorCode.APPLICATION_ERROR)
            return
        elif isinstance(event, StreamReset):
            logger.debug(f"MOQT event: StreamReset: stream {event.stream_id}")
            self._cleanup_stream(
                event.stream_id, QuicErrorCode.APPLICATION_ERROR)
            return

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"QUIC event: event not handled({class_name(event)})")

    def _close_session(self, 
              error_code: SessionCloseCode = SessionCloseCode.NO_ERROR, 
              reason_phrase: str = "no error") -> None:
        """Close the MoQT session."""
        if error_code == SessionCloseCode.NO_ERROR:
            logger.info(f"MOQT: closing: {reason_phrase} ({error_code})")
        else:
            logger.error(f"MOQT error: closing: {reason_phrase} ({error_code})")
        self._close_err = (error_code, reason_phrase)

        # Tombstone every stream_id so any in-flight chunks landing
        # after this point are dropped rather than recreating state.
        for stream_id in list(self._data_streams.keys()):
            self._mark_stream_torn_down(stream_id)
        self._data_streams.clear()
        self._control_chains.clear()
        self._pending_control_msgs.clear()
        self._uni_peek_stash.clear()

        if not self._wt_session_setup.done():
            self._wt_session_setup.set_result(False)
        if not self._moqt_session_setup.done():
            self._moqt_session_setup.set_result(False)
        if not self._moqt_session_closed.done():
            self._moqt_session_closed.set_result((error_code, reason_phrase))

    def close(self,
              error_code: SessionCloseCode = SessionCloseCode.NO_ERROR,
              reason_phrase: str = "no error"
        ) -> None:
        """Session Protocol Close"""
        if self._close_err is not None:
            error_code, reason_phrase = self._close_err
        logger.info(f"MOQT session: closing: {reason_phrase} ({error_code})")

        # Gracefully FIN open streams before closing the connection.
        # Transmit FINs separately so they don't get batched with
        # CONNECTION_CLOSE (which causes reset_stream on the peer).
        # Only FIN streams we own the write side of — sending
        # end_stream on peer-initiated uni streams produces
        # RESET_STREAM which confuses relays.
        is_client = self._is_client
        try:
            if self._control_write_stream_id is not None:
                self._quic.send_stream_data(
                    self._control_write_stream_id, b"", end_stream=True)
                self._control_stream_id = None
                self._d18_control_write_sid = None
            for stream_id in list(self._data_streams.keys()):
                # We own the write side if we initiated the stream.
                # QUIC stream ID bits 0-1: 0=client-bidi, 1=server-bidi,
                # 2=client-uni, 3=server-uni
                locally_initiated = (
                    (is_client and (stream_id & 0x1) == 0) or
                    (not is_client and (stream_id & 0x1) == 1)
                )
                if locally_initiated:
                    self._quic.send_stream_data(
                        stream_id, b"", end_stream=True)
            for sid in list(self._data_streams.keys()):
                self._mark_stream_torn_down(sid)
            self._data_streams.clear()
        except Exception:
            pass  # best-effort during teardown

        self._session_id = None

        # set the async exit condition for session
        if not self._moqt_session_closed.done():
            self._moqt_session_closed.set_result((error_code, reason_phrase))
        # close QUIC connection
        super().close()
        
    async def async_closed(self) -> bool:
        if not self._moqt_session_closed.done():
            self._close_err = await self._moqt_session_closed
        return True


    async def client_session_init(self, timeout: int = 10,
                                  parameters: Optional[Dict[int, bytes]] = None
                                  ) -> bool:
        """Initialize WebTransport and MoQT client session.

        parameters are extra SETUP options merged into the CLIENT_SETUP /
        Setup message — e.g. {SetupParamType.AUTH_TOKEN: b"..."} for
        session-level auth (Token-wrapped on the wire)."""

        use_quic = not self._is_wt
        params = {}
        if use_quic:
            # Raw QUIC flow. _wt_session_setup is pre-resolved in __init__.
            logger.info(f"MOQT: Using raw QUIC transport")
            if self._profile.control_uni_pair:
                # d18: control is a uni-stream pair. Open our write-control
                # uni now; the peer's read-control uni is bound on the first
                # SETUP it sends (see quic_event_received).
                self._d18_control_write_sid = (
                    self._quic.get_next_available_stream_id(
                        is_unidirectional=True))
                logger.info(
                    f"MOQT: d18 control write-uni: "
                    f"{self._d18_control_write_sid}")
            else:
                self._control_stream_id = (
                    self._quic.get_next_available_stream_id(
                        is_unidirectional=False))
                logger.info(
                    f"MOQT: QUIC control stream created stream id: "
                    f"{self._control_stream_id}")
            req_path = self._session.path or ""
            params[SetupParamType.PATH] = f"/{req_path}"
            params[SetupParamType.AUTHORITY] = (
                f"{self._session.host}:{self._session.port}")
        else:
            # WebTransport over aiopquic. The WT session is open by the
            # time client_session_init runs (open() awaited already);
            # _wt_session_setup is resolved in _moqt_wt_finalize. Resolve
            # the draft from the negotiated WT-Protocol (in-band
            # WT-Available-Protocols -> WT-Protocol selection, surfaced by
            # aiopquic as negotiated_protocol); a single supported draft
            # (a pin) still wins.
            _drafts = getattr(self._session, 'supported_drafts', None)
            draft = _drafts[0] if _drafts and len(_drafts) == 1 else None
            if draft is not None:
                self.negotiated_draft = get_major_version(draft)
            else:
                negotiated = self.negotiated_protocol
                if negotiated:
                    self.negotiated_draft = get_major_version(
                        moqt_version_from_alpn(negotiated))
                    logger.info(
                        f"MOQT: version set from WT-Protocol: "
                        f"{negotiated} -> draft-{self.negotiated_draft}")
            if self._profile.control_uni_pair:
                # d18 over WT: control is a uni-stream pair, same as raw
                # QUIC — open our write-control uni (WT create_stream); the
                # peer's read-control uni is bound on the first SETUP it
                # sends (the demux in quic_event_received is transport-
                # agnostic). The data plane already proves WT uni streams.
                self._d18_control_write_sid = await self.open_uni_stream()
                logger.info(
                    f"MOQT: d18 WT control write-uni: "
                    f"{self._d18_control_write_sid}")
            else:
                self._control_stream_id = await self.open_bidi_stream()
                logger.info(
                    f"MOQT: WT control stream created stream id: "
                    f"{self._control_stream_id}")

        # Send SETUP. d18 SETUP is symmetric (Setup, type 0x2F00): no
        # version array (negotiated via ALPN), no MAX_REQUEST_ID Setup
        # Option (removed), count-less Setup Options. Pre-d18 sends
        # CLIENT_SETUP with the version list.
        params[SetupParamType.IMPLEMENTATION] = USER_AGENT.encode()
        if parameters:
            params.update(parameters)  # caller SETUP options (e.g. AUTH_TOKEN)
        if self._profile.control_uni_pair:
            self.send_control_message(Setup(options=params))
        else:
            params[SetupParamType.MAX_REQUEST_ID] = 10000
            # For raw QUIC with explicit draft: single version
            # For H3/WT: version list depends on whether WT protocol was
            # negotiated (negotiated -> no version array; else d14 format)
            drafts = getattr(self._session, 'supported_drafts', [])
            if use_quic and drafts:
                versions = [moqt_version_from_draft(d) for d in drafts]
            else:
                versions = MOQT_VERSIONS
            client_setup = self.client_setup(
                versions=versions,
                parameters=params
            )
        # SETUP is on the wire; release anything deferred while the
        # control write stream was coming up.
        self._flush_pending_control()

        # Wait for SERVER_SETUP
        session_setup = False
        try: 
            async with asyncio.timeout(timeout):
                session_setup = await self._moqt_session_setup
        except asyncio.TimeoutError:
            error = "timeout waiting for SERVER_SETUP"
            logger.error("MOQT error: " + error)
            self._close_session(SessionCloseCode.CONTROL_MESSAGE_TIMEOUT, error)
            pass
        
        if not session_setup or self._close_err is not None:
            logger.error(f"MOQT error: session setup failed: {session_setup}")
            raise MOQTException(*self._close_err)
        
        logger.info(f"MOQT session: setup complete: {session_setup}")



    async def open_uni_stream(self) -> int:
        """Open a unidirectional data stream. Returns the stream ID."""
        if not self._is_wt:
            return self._quic.get_next_available_stream_id(is_unidirectional=True)
        # aiopquic-WT: round-trip to picoquic for stream-id allocation
        try:
            return await self.create_stream(bidir=False)
        except WebTransportError:
            # Session torn down while a producer was at a stream
            # rollover. Convert to the cancellation path the track
            # generators already handle cleanly instead of surfacing
            # a task exception at every shutdown.
            if not self._session_writable():
                raise asyncio.CancelledError()
            raise

    async def open_bidi_stream(self) -> int:
        """Open a bidirectional stream. Returns the stream ID."""
        if not self._is_wt:
            return self._quic.get_next_available_stream_id(is_unidirectional=False)
        # aiopquic-WT
        return await self.create_stream(bidir=True)

    # -- spec-aligned stream lifecycle primitives ---------------------
    # All three short-circuit when the session is closing/closed so a
    # task racing a teardown doesn't blow up the asyncio loop. The
    # underlying aiopquic calls also raise on stale streams; we swallow
    # those at this layer because by the time we got here the stream
    # is already gone — nothing useful left for the caller to do.

    def _session_writable(self) -> bool:
        return self._close_err is None and self._quic is not None

    def stream_write(self, stream_id: int, data: bytes,
                      end_stream: bool = False) -> None:
        """Write bytes (and optional FIN) to stream_id."""
        if not self._session_writable():
            return
        try:
            self._quic.send_stream_data(
                stream_id, data, end_stream=end_stream)
        except (AssertionError, AttributeError, BufferError,
                WebTransportError) as e:
            logger.debug(f"stream({stream_id}): write race: {e}")

    async def stream_write_drain(self, stream_id: int, data: bytes,
                                  end_stream: bool = False) -> None:
        """Write bytes with backpressure.

        aiomoqt-owned policy: optional per-stream byte-budget cap
        with hysteresis (MOQTPeer.tx_max_inflight_bytes). The
        wire-level mechanic (TX-ring saturation guard, BufferError
        retry, soft post-send yield, close-time clean exit) is
        delegated to aiopquic's send_stream_data_drained — single
        source of truth per the backpressure-placement principle.
        """
        if (not self._session_writable()
                or getattr(self._quic, 'closed', False)):
            # Dead session or closed connection: convert to the
            # cancellation path the track generators handle (mirror
            # of the WT open_uni_stream guard). A silent no-op here
            # livelocks unpaced producers — the await completes
            # without ever suspending, so the event loop starves and
            # task cancellation can never be delivered.
            raise asyncio.CancelledError()
        quic = self._quic
        max_bytes = self._session.tx_max_inflight_bytes
        # Byte-budget gate (aiomoqt-owned policy). Per-stream sc->tx
        # queue depth — preserves QUIC stream independence (no HOLB).
        # Hysteresis: park at HIGH water (max_bytes), resume at LOW
        # water (max_bytes // 2). max_bytes MUST be <
        # QuicConfiguration.stream_ring_cap to engage (the cap is
        # bounded by ring capacity).
        if (max_bytes is not None
                and quic.tx_data_ring_used(stream_id) > max_bytes):
            low_water = max_bytes // 2
            sc_event = quic.get_tx_data_drain_event(stream_id)
            while quic.tx_data_ring_used(stream_id) > low_water:
                if not self._session_writable():
                    return
                sc_event.clear()
                quic.set_tx_data_drain_pending(stream_id)
                if quic.tx_data_ring_used(stream_id) <= low_water:
                    quic.clear_tx_data_drain_pending(stream_id)
                    break
                await sc_event.wait()
        # Delegate wire-level send + backpressure to aiopquic.
        try:
            await quic.send_stream_data_drained(
                stream_id, data, end_stream=end_stream)
        except (AssertionError, AttributeError) as e:
            logger.debug(f"stream({stream_id}): write race: {e}")

    def stream_fin(self, stream_id: int) -> None:
        """End-of-data on a sender-owned stream. Subgroup last object,
        fetch range complete, control GOAWAY+close use this."""
        if not self._session_writable():
            return
        try:
            self._quic.send_stream_data(stream_id, b"", end_stream=True)
        except (AssertionError, AttributeError, BufferError,
                RuntimeError, WebTransportError) as e:
            logger.debug(f"stream({stream_id}): fin race: {e}")

    def stream_reset(self, stream_id: int,
                      error_code: int = SessionCloseCode.NO_ERROR) -> None:
        """RESET_STREAM. Sender-side abrupt termination — UNSUBSCRIBE,
        FETCH_CANCEL on the publisher, track teardown, error abort.
        Also the canonical reply to a peer's STOP_SENDING."""
        if not self._session_writable():
            return
        try:
            self._quic.reset_stream(stream_id, error_code)
        except (AssertionError, AttributeError, RuntimeError) as e:
            logger.debug(f"stream({stream_id}): reset race: {e}")

    def stream_stop_sending(self, stream_id: int,
                             error_code: int = SessionCloseCode.NO_ERROR) -> None:
        """STOP_SENDING. Receiver-side request to cancel a peer-owned
        stream — UNSUBSCRIBE on the subscriber, FETCH_CANCEL on the
        subscriber, FETCH_ERROR while the fetch stream is still open."""
        if not self._session_writable():
            return
        try:
            self._quic.stop_stream(stream_id, error_code)
        except (AssertionError, AttributeError, RuntimeError) as e:
            logger.debug(f"stream({stream_id}): stop_sending race: {e}")

    def _stream_is_writable(self, stream_id: int) -> bool:
        """Whether send_stream_data on this session can be expected to
        succeed. Stream-id-specific state lives in picoquic; we only
        check session-level closed/torn-down here."""
        return self._session_writable()

    _PENDING_CONTROL_MAX = 128

    def send_control_message(self, msg: MOQTMessage) -> None:
        """Serialize and send a MoQT control message on the control
        stream. Serialization happens here at the session's negotiated
        profile (`self._profile`), so call sites pass the message object
        and never thread prof= — they cannot forget it or pass the wrong
        one. While the control write stream is still coming up (the d18
        server opens its write-uni inside the SETUP handler, so replies
        to a pipelined client can race it), the message is deferred and
        flushed right after our SETUP so SETUP stays the first message
        on the control stream."""
        if self._quic is None:
            raise MOQTException(SessionCloseCode.INTERNAL_ERROR,
                                "session not initialized")
        if self._control_write_stream_id is None:
            # Deferral exists for one race: the d18 server opens its
            # write-uni inside the SETUP handler. Pre-d18 the control
            # stream latches before any handler runs, so a missing
            # stream is a programming error — fail loudly (there is no
            # pre-d18 flush site to ever drain a deferred message).
            if (not self._profile.control_uni_pair or
                    len(self._pending_control_msgs) >= self._PENDING_CONTROL_MAX):
                raise MOQTException(SessionCloseCode.INTERNAL_ERROR,
                                    "control stream not initialized")
            logger.debug(
                f"MOQT send: deferred until control write stream is up: "
                f"{msg}")
            self._pending_control_msgs.append(msg)
            return
        buf = msg.serialize(prof=self._profile)
        logger.debug(f"QUIC send: control message: {buf.tell()} bytes")
        self._quic.send_stream_data(
            stream_id=self._control_write_stream_id,
            data=buf.data,
            end_stream=False
        )

    def _flush_pending_control(self) -> None:
        """Send control messages deferred while the control write stream
        was coming up. Call sites run this right after SETUP is written,
        keeping SETUP the first message on the control stream. The
        stream-id guard prevents a re-append busy loop if ever invoked
        before the write stream is up."""
        while (self._pending_control_msgs and
               self._control_write_stream_id is not None):
            self.send_control_message(self._pending_control_msgs.pop(0))

    def send_stream_message(self, stream_id: int, msg: MOQTMessage) -> None:
        """Serialize and send a MoQT message on a per-request bidi
        stream (d16+ SUBSCRIBE_NAMESPACE response routing). Like
        send_control_message, serialization happens here at the session
        profile so callers never thread prof=."""
        self.stream_write(stream_id, msg.serialize(prof=self._profile).data)

    def subgroup_header(self, track_alias: int, group_id: int,
                        **kwargs) -> SubgroupHeader:
        """Build a SubgroupHeader bound to this session's negotiated draft
        profile — the data-plane mirror of send_control_message. Higher
        APIs (Track, apps) call this and never thread draft/profile; the
        header's serialize()/next_object* then use the baked-in codec."""
        return SubgroupHeader(track_alias=track_alias, group_id=group_id,
                              prof=self._profile, **kwargs)

    def _send_reply(self, request_id: int, msg: MOQTMessage) -> None:
        """Send a response to a request. In d18 responses travel on the
        request's own bidi stream (demuxed by stream, no in-band Request
        ID); pre-d18 they go on the single control stream."""
        sid = self._bidi_streams.get(request_id)
        if self._profile.control_uni_pair and sid is not None:
            self.send_stream_message(sid, msg)
        else:
            self.send_control_message(msg)

    def _send_request(self, request_id: int, msg: MOQTMessage) -> None:
        """Send a request-stream opener. In d18 every request (SUBSCRIBE,
        PUBLISH, FETCH, TRACK_STATUS, PUBLISH_NAMESPACE, ...) opens its own
        bidi stream and the response returns on it (no in-band Request ID);
        pre-d18 requests go on the single control stream. Raw-QUIC stream-id
        allocation is synchronous, so callers stay sync."""
        if self._profile.control_uni_pair:
            if not self._is_wt:
                # Raw QUIC: stream-id allocation is synchronous (lazy
                # materialization on first send), so stay sync.
                stream_id = self._quic.get_next_available_stream_id(
                    is_unidirectional=False)
                self._bidi_streams[request_id] = stream_id
                self._bidi_stream_requests[stream_id] = request_id
                self.send_stream_message(stream_id, msg)
            else:
                # WebTransport: bidi-stream open is async (create_stream
                # round-trips to picoquic), so defer the open+send to a
                # task. The reply correlates by request_id once the stream
                # is bound, and it cannot arrive before we send on it.
                task = asyncio.create_task(
                    self._d18_open_request_stream(request_id, msg))
                self._tasks.add(task)
                task.add_done_callback(self._control_task_done)
        else:
            self.send_control_message(msg)

    async def _d18_open_request_stream(self, request_id: int,
                                       msg: MOQTMessage) -> None:
        """Open a d18 request bidi stream over WebTransport (async
        create_stream) and send the opener on it. Raw QUIC uses the
        synchronous path in _send_request instead."""
        stream_id = await self.open_bidi_stream()
        self._bidi_streams[request_id] = stream_id
        self._bidi_stream_requests[stream_id] = request_id
        self.send_stream_message(stream_id, msg)

    # Messages that open a request stream (must be the first message on a
    # new bidi stream). d16 only uses SUBSCRIBE_NAMESPACE here; d18 routes
    # all requests onto their own bidi stream. (SUBSCRIBE_TRACKS 0x51 lands
    # with the d18 control-message set in a later phase.)
    _REQUEST_OPENERS = (Subscribe, Fetch, Publish, TrackStatus,
                        PublishNamespace, SubscribeNamespace, SubscribeTracks)

    def send_dgram_message(self, buf: Buffer) -> None:
        """Send a MoQT message via QUIC datagram (best-effort)."""
        if self._quic is None:
            raise MOQTException(SessionCloseCode.INTERNAL_ERROR,
                                "QUIC not initialized")
        logger.debug(f"QUIC send: datagram message: {buf.capacity} bytes")
        self._quic.send_datagram_frame(data=buf.data)

    ################################################################################################
    #  Outbound control message API - note: awaitable messages support 'wait_response' param       #
    ################################################################################################
    def client_setup(
        self,
        versions: List[int] = MOQT_VERSIONS,
        parameters: Optional[Dict[int, bytes]] = None,
    ) -> None:
        """Send CLIENT_SETUP message and optionally wait for SERVER_SETUP response."""
        if parameters is None:
            parameters = {}

        message = ClientSetup(
            versions=versions,
            parameters=parameters
        )
        logger.info(f"MOQT send: {message}")
        self.send_control_message(message)

        return message

    def server_setup(
        self,
        selected_version: int = MOQT_VERSION_DRAFT14,
        parameters: Optional[Dict[int, bytes]] = None
    ) -> ServerSetup:
        """Send SERVER_SETUP message in response to CLIENT_SETUP."""
        if parameters is None:
            parameters = {}

        message = ServerSetup(
            selected_version=selected_version,
            parameters=parameters
        )
        logger.info(f"MOQT send: {message}")
        self.send_control_message(message)
        return message

    def subscribe(
        self,
        namespace: str,
        track_name: str,
        # Default to None = "unspecified": omit the param from the wire and
        # let the relay apply the protocol default (forward=true, publisher
        # group order, default priority). This matches moxygen/moqx/moq-rs,
        # which omit default-valued SUBSCRIBE params — a conservative sender
        # keeps the message to just the SUBSCRIPTION_FILTER. Emitting the
        # defaults is legal but trips stricter parsers (e.g. moqtail drops a
        # multi-param SUBSCRIBE). Pass explicit values to override.
        priority: Optional[int] = None,
        group_order: Optional[GroupOrder] = None,
        forward: Optional[int] = None,
        filter_type: FilterType = FilterType.LATEST_OBJECT,
        start_group: Optional[int] = 0,
        start_object: Optional[int] = 0,
        end_group: Optional[int] = 0,
        parameters: Optional[Dict[int, bytes]] = None,
        wait_response: Optional[bool] = False,
    ) -> Optional[MOQTMessage]:
        """Subscribe to a track with configurable options."""
        if parameters is None:
            parameters = {}
        request_id = self._allocate_request_id()
        track_alias = self._allocate_track_alias(request_id)
        namespace_tuple = self._make_namespace_tuple(namespace)
        track_name = track_name.encode() if isinstance(track_name, str) else track_name

        message = Subscribe(
            request_id=request_id,
            track_namespace=namespace_tuple,
            track_name=track_name,
            priority=priority,
            group_order=group_order,
            forward=forward,
            filter_type=filter_type,
            start_group=start_group,
            start_object=start_object,
            end_group=end_group,
            parameters=parameters
        )
        message.libquicr_compat = self._session.libquicr_compat
        self._subscriptions[request_id] = [message]
        logger.info(f"MOQT send: {message}")
        self._send_request(request_id, message)

        if not wait_response:
            return message

        return self._await_response(request_id)

    def subscribe_ok(
        self,
        request_msg: Subscribe,
        expires: int = 0,  # 0 means no expiry
        group_order: int = GroupOrder.ASCENDING,
        content_exists: int = 0,
        largest_group_id: Optional[int] = None,
        largest_object_id: Optional[int] = None,
        parameters: Optional[Dict[int, bytes]] = None
    ) -> Optional[MOQTMessage]:
        """Create and send a SUBSCRIBE_OK response."""
        track_alias = self._allocate_track_alias(request_msg.request_id)
        # Set track_alias on the Subscribe msg so custom handlers can use it
        request_msg.track_alias = track_alias
        message = SubscribeOk(
            request_id=request_msg.request_id,
            track_alias=track_alias,
            expires=expires,
            group_order=group_order,
            content_exists=content_exists,
            largest_group_id=largest_group_id,
            largest_object_id=largest_object_id,
            parameters=parameters or {}
        )
        logger.info(f"MOQT send: {message}")
        self._send_reply(request_msg.request_id, message)
        return message

    def subscribe_done(
        self,
        request_id: int,
        status_code: int = SubscribeDoneCode.SUBSCRIPTION_ENDED,
        stream_count: int = 0,
        reason: str = "",
    ) -> Optional[MOQTMessage]:
        """Create and send a PUBLISH_DONE (SUBSCRIBE_DONE) response on the
        subscription's request stream (d18) or the control stream."""
        message = SubscribeDone(
            request_id=request_id,
            status_code=status_code,
            stream_count=stream_count,
            reason=reason,
        )
        logger.info(f"MOQT send: {message}")
        self._send_reply(request_id, message)
        return message

    def subscribe_error(
        self,
        request_id: int,
        error_code: int = SubscribeErrorCode.INTERNAL_ERROR,
        reason: str = "Internal error",
    ) -> Optional[MOQTMessage]:
        """Create and send a SUBSCRIBE_ERROR response."""
        message = SubscribeError(
            request_id=request_id,
            error_code=error_code,
            reason=reason,
        )
        logger.info(f"MOQT send: {message}")
        self.send_control_message(message)
        return message
    
    def unsubscribe(
        self,
        request_id: int,
    ) -> Optional[MOQTMessage]:
        """Unsubscribe from a track."""
        message = Unsubscribe(request_id=request_id)
        logger.info(f"MOQT send: {message}")
        self.send_control_message(message)
 
        return message       

    async def join(
        self,
        namespace: Union[Tuple[bytes, ...], List[Union[bytes, str]], str],
        track_name: Union[bytes, str],
        # None = unspecified: omitted from the d16 wire (conservative
        # sender — see subscribe()). Pass a value to force it on the wire.
        subscriber_priority: Optional[int] = None,
        group_order: Optional[GroupOrder] = None,
        fetch_type: FetchType = FetchType.RELATIVE_JOINING,
        joining_start: int = 0,
        parameters: Optional[Dict[int, bytes]] = None,
        wait_response: Optional[bool] = False,
    ) -> Union[Tuple[MOQTMessage, MOQTMessage], Tuple[MOQTMessage, MOQTMessage, 'asyncio.Future']]:
        """Subscribe + Joining Fetch (atomic pair).

        Sends a SUBSCRIBE with filter=LATEST_OBJECT (required by spec for
        joining fetches) followed immediately by a FETCH of type
        RELATIVE_JOINING (default) or ABSOLUTE_JOINING referencing the
        subscribe's request_id.

        Args:
            fetch_type: FetchType.RELATIVE_JOINING (spec: start =
                largest_group - joining_start) or FetchType.ABSOLUTE_JOINING
                (start = joining_start).
            joining_start: relative offset (relative) or absolute group
                id (absolute) — see spec §9.16.2.1.
            wait_response: if True, awaits and returns
                (subscribe_ok, fetch_ok). If False, returns
                (subscribe_msg, fetch_msg) sent messages.

        Returns:
            (subscribe_response, fetch_response) when wait_response=True,
            (subscribe_msg, fetch_msg) when wait_response=False.
        """
        if fetch_type not in (FetchType.RELATIVE_JOINING,
                               FetchType.ABSOLUTE_JOINING):
            raise ValueError(
                f"join() requires a joining fetch_type, got {fetch_type}")

        parameters = {} if parameters is None else parameters
        sub_request_id = self._allocate_request_id()
        self._allocate_track_alias(sub_request_id)
        namespace_tuple = self._make_namespace_tuple(namespace)
        if isinstance(track_name, str):
            track_name = track_name.encode()

        sub_msg = Subscribe(
            request_id=sub_request_id,
            track_namespace=namespace_tuple,
            track_name=track_name,
            priority=subscriber_priority,
            group_order=group_order,
            forward=None,  # omit (default true) unless caller set it
            filter_type=FilterType.LATEST_OBJECT,  # spec §9.16.2
            parameters=parameters,
        )
        self._subscriptions[sub_request_id] = [sub_msg]
        # Pre-register response futures before send so loopback / low-RTT
        # peers can't resolve them before our awaiter registers.
        self._pending_requests[sub_request_id] = self._loop.create_future()
        logger.info(f"MOQT send: {sub_msg}")
        self._send_request(sub_request_id, sub_msg)

        fetch_request_id = self._allocate_request_id()
        fetch_msg = Fetch(
            request_id=fetch_request_id,
            fetch_type=fetch_type,
            subscriber_priority=subscriber_priority,
            group_order=group_order,
            joining_request_id=sub_request_id,
            joining_start=joining_start,
            parameters=dict(parameters),
        )
        self._subscriptions[fetch_request_id] = [fetch_msg]
        self._pending_requests[fetch_request_id] = self._loop.create_future()
        # Pre-register the fetch-done future before sending so we
        # don't miss the stream FIN in fast-completion scenarios.
        self._fetch_done_futures[fetch_request_id] = \
            self._loop.create_future()
        logger.info(f"MOQT send: {fetch_msg}")
        self._send_request(fetch_request_id, fetch_msg)

        if not wait_response:
            return (sub_msg, fetch_msg)

        sub_response = await self._await_response(sub_request_id)
        fetch_response = await self._await_response(fetch_request_id)
        return (sub_response, fetch_response)

    def fetch(
        self,
        namespace: Union[Tuple[bytes, ...], List[Union[bytes, str]], str],
        track_name: Union[bytes, str],
        # None = unspecified: omit from the d16 wire, relay applies its
        # default (conservative sender — see subscribe()). Pass a value to
        # force it on the wire.
        subscriber_priority: Optional[int] = None,
        group_order: Optional[GroupOrder] = None,
        start_group: int = 0,
        start_object: int = 0,
        end_group: int = 0,
        end_object: int = 0,
        parameters: Optional[Dict[int, bytes]] = None,
        wait_response: Optional[bool] = False,
    ) -> Optional[MOQTMessage]:
        """Standalone FETCH of a range of objects from a track.

        Per spec §9.16.1: End Location.Object of 0 means the entire end
        group is requested.
        """
        parameters = {} if parameters is None else parameters
        request_id = self._allocate_request_id()
        namespace = self._make_namespace_tuple(namespace)
        if isinstance(track_name, str):
            track_name = track_name.encode()

        message = Fetch(
            request_id=request_id,
            fetch_type=FetchType.STANDALONE,
            subscriber_priority=subscriber_priority,
            group_order=group_order,
            namespace=namespace,
            track_name=track_name,
            start_group=start_group,
            start_object=start_object,
            end_group=end_group,
            end_object=end_object,
            parameters=parameters,
        )
        self._subscriptions[request_id] = [message]
        self._fetch_done_futures[request_id] = \
            self._loop.create_future()
        logger.info(f"MOQT send: {message}")
        # FETCH is a request opener: at d18 it must open its own bidi
        # request stream (like SUBSCRIBE/PUBLISH); pre-d18 _send_request
        # falls back to the single control stream.
        self._send_request(request_id, message)

        if not wait_response:
            return message
        return self._await_response(request_id)

    def fetch_ok(
        self,
        request_id: int,
        end_of_track: int = 0,
        largest_group_id: int = 0,
        largest_object_id: int = 0,
        group_order: int = GroupOrder.ASCENDING,
        parameters: Optional[Dict[int, bytes]] = None,
        track_extensions: Optional[Dict[int, Any]] = None,
    ) -> Optional[MOQTMessage]:
        """Create and send a FETCH_OK response (spec §9.17)."""
        message = FetchOk(
            request_id=request_id,
            end_of_track=end_of_track,
            largest_group_id=largest_group_id,
            largest_object_id=largest_object_id,
            group_order=group_order,
            parameters=parameters or {},
            track_extensions=track_extensions or {},
        )
        logger.info(f"MOQT send: {message}")
        # FETCH_OK is a response: at d18 it returns on the request's own
        # bidi stream; pre-d18 _send_reply falls back to the control stream.
        self._send_reply(request_id, message)
        return message

    def fetch_error(
        self,
        request_id: int,
        error_code: int = 0,
        reason: str = "Internal error",
    ) -> Optional[MOQTMessage]:
        """Create and send a FETCH_ERROR response (spec §9.16)."""
        message = FetchError(
            request_id=request_id,
            error_code=error_code,
            reason=reason,
        )
        logger.info(f"MOQT send: {message}")
        self.send_control_message(message)
        return message

    def publish_namespace(
        self,
        namespace: Union[str, Tuple[str, ...]],
        parameters: Optional[Dict[int, bytes]] = None,
        wait_response: Optional[bool] = False
    ) -> Optional[MOQTMessage]:
        """PublishNamespace track namespace availability."""
        namespace_tuple = self._make_namespace_tuple(namespace)
        request_id = self._allocate_request_id()
        message = PublishNamespace(
            request_id=request_id,
            namespace=namespace_tuple,
            parameters=parameters or {}
        )
        # Pre-register response future before send so a fast peer can't
        # resolve before the awaiter registers (race we hit in join()).
        if wait_response:
            self._pending_requests[request_id] = self._loop.create_future()
        logger.info(f"MOQT send: {message}")
        self._send_request(request_id, message)

        if not wait_response:
            return message

        return self._await_response(request_id)

    def publish(
        self,
        namespace: Union[str, Tuple[str, ...]],
        track_name: str,
        forward: int = 0,
        content_exists: int = 0,
        parameters: Optional[Dict[int, Any]] = None,
        wait_response: Optional[bool] = False,
    ) -> Optional[MOQTMessage]:
        """PUBLISH — announce a specific track to the relay/subscriber.

        Args:
            forward: 0 = announce availability only, 1 = start data.
                     Subscriber responds with PUBLISH_OK(forward=1)
                     to request data flow.
        """
        namespace_tuple = self._make_namespace_tuple(namespace)
        request_id = self._allocate_request_id()
        track_alias = self._allocate_track_alias(request_id)
        track_name_bytes = track_name.encode() if isinstance(track_name, str) else track_name
        message = Publish(
            request_id=request_id,
            track_namespace=namespace_tuple,
            track_name=track_name_bytes,
            track_alias=track_alias,
            group_order=GroupOrder.ASCENDING,
            forward=forward,
            content_exists=content_exists,
            parameters=parameters or {},
        )
        logger.info(f"MOQT send: {message}")
        # Pre-register the response future before send (like subscribe /
        # fetch / publish_namespace) so a fast peer ack can't resolve
        # before the awaiter registers — a race on loopback.
        if wait_response:
            self._pending_requests[request_id] = self._loop.create_future()
        self._send_request(request_id, message)

        if not wait_response:
            return message

        return self._await_response(request_id)

    def publish_namepace_ok(
        self,
        msg: PublishNamespace,
    ) -> Optional[MOQTMessage]:
        """Send a positive response to PUBLISH_NAMESPACE.

        Draft-14 sends PublishNamespaceOk on code point 0x07.
        Draft-16+ reuses 0x07 as REQUEST_OK (universal positive
        response — adds Num Parameters).
        """
        if is_draft16_or_later(self.negotiated_draft):
            message = RequestOk(request_id=msg.request_id)
        else:
            message = PublishNamespaceOk(request_id=msg.request_id)
        logger.info(f"MOQT send: {message} request_id: {msg.request_id} namespace: {msg.namespace}")
        self.send_control_message(message)
        return message

    def publish_namespace_done(
        self,
        namespace: Tuple[bytes, ...] = None,
        request_id: int = None,
    ) -> Optional[MOQTMessage]:
        """Withdraw track namespace announcement. (no reply expected)

        Draft-14: takes namespace tuple.
        Draft-16: takes request_id of the original PUBLISH_NAMESPACE.
        """
        message = PublishNamespaceDone(namespace=namespace, request_id=request_id)
        logger.info(f"MOQT send: {message}")
        self.send_control_message(message)
        return message

    async def subscribe_namespace(
        self,
        namespace_prefix: str,
        parameters: Optional[Dict[int, bytes]] = None,
        subscribe_options: int = 0,
        wait_response: Optional[bool] = False
    ) -> Optional[MOQTMessage]:
        """Subscribe to announcements for a namespace prefix.

        In d16+, sends on a new bidirectional stream per spec Section 9.25.
        In d14, sends on the control stream.

        Args:
            subscribe_options: d16 only — 0=PUBLISH, 1=NAMESPACE, 2=both
        """
        if parameters is None:
            parameters = {}

        prefix = self._make_namespace_tuple(namespace_prefix)
        request_id = self._allocate_request_id()
        message = SubscribeNamespace(
            request_id=request_id,
            namespace_prefix=prefix,
            subscribe_options=subscribe_options,
            parameters=parameters
        )
        logger.info(f"MOQT send: {message}")

        if is_draft16_or_later(self.negotiated_draft):
            stream_id = await self.open_bidi_stream()
            self.send_stream_message(stream_id, message)
            # Track bidi stream ↔ request mapping for response routing
            self._bidi_streams[request_id] = stream_id
            self._bidi_stream_requests[stream_id] = request_id
        else:
            self.send_control_message(message)

        if not wait_response:
            return message

        return await self._await_response(request_id)

    def subscribe_namespace_ok(
        self,
        msg: SubscribeNamespace,
        stream_id: int = None,
    ) -> Optional[MOQTMessage]:
        """Create and send a SUBSCRIBE_NAMESPACE_OK response.

        In d16+, responds on the same bidi stream the request came on.
        """
        message = SubscribeNamespaceOk(request_id=msg.request_id)
        logger.info(f"MOQT send: {message}")
        if stream_id is not None and is_draft16_or_later(self.negotiated_draft):
            self.send_stream_message(stream_id, message)
        else:
            self.send_control_message(message)
        return message

    async def await_namespace(self, timeout: float = 10.0):
        """Wait for a NAMESPACE announcement from the relay.

        Returns the Namespace message with namespace_suffix, or raises
        TimeoutError if no announcement arrives within timeout.
        """
        return await asyncio.wait_for(
            self._namespace_announcements.get(), timeout=timeout)

    async def await_publish(self, timeout: float = 10.0,
                            trackname: Optional[str] = None):
        """Wait for a PUBLISH (track announcement) from the relay.

        If `trackname` is given, announcements whose track_name does not
        match are discarded until a matching one arrives (or timeout).
        Without it, returns the first announcement off the queue.
        """
        async def _next_matching():
            while True:
                msg = await self._publish_announcements.get()
                if trackname is None:
                    return msg
                name = (msg.track_name.decode()
                        if isinstance(msg.track_name, bytes)
                        else msg.track_name)
                if name == trackname:
                    return msg
                logger.debug(
                    f"await_publish: dropping announcement for "
                    f"'{name}' (waiting for '{trackname}')")
        return await asyncio.wait_for(_next_matching(), timeout=timeout)

    def unsubscribe_namespace(
        self,
        namespace_prefix: str
    ) -> Optional[MOQTMessage]:
        """Unsubscribe from announcements for a namespace prefix."""        
        prefix = self._make_namespace_tuple(namespace_prefix)
        message = UnsubscribeNamespace(namespace_prefix=prefix)
        logger.info(f"MOQT send: {message}")
        self.send_control_message(message)
        return message


    ###############################################################################################
    #  Inbound MoQT message handlers                                                              #
    ###############################################################################################
    
    def register_handler(self, msg_type: int, handler: Callable) -> None:
        """Register a custom handler, overriding the default handler for
        this message type on this session."""
        self._control_msg_overrides[msg_type] = handler
    
    async def _handle_server_setup(self, msg: ServerSetup) -> None:
        logger.info(f"MOQT event: handle {msg}")

        if not self._is_client:
            error = "MOQT event: received SERVER_SETUP message as server"
            logger.debug(error)
            self._close_session(
                error_code=SessionCloseCode.PROTOCOL_VIOLATION,
                reason_phrase=error
            )
        elif self._moqt_session_setup.done():
            error = "MOQT event: received multiple SERVER_SETUP messages"
            logger.debug(error)
            self._close_session(
                error_code=SessionCloseCode.PROTOCOL_VIOLATION,
                reason_phrase=error
            )
        else:
            selected_version = msg.selected_version
            if selected_version is None:
                # Draft-16+ carries no version in ServerSetup: it was
                # negotiated out-of-band — QUIC ALPN ("moqt-NN") for raw
                # QUIC, WT-Available-Protocols for WebTransport. These are
                # distinct mechanisms; don't conflate them.
                selected_draft = self.negotiated_draft
                _src = ("WT-Available-Protocols" if self._is_wt else "ALPN")
                logger.info(f"MOQT event: d16+ ServerSetup (version from {_src}: draft-{selected_draft})")
            else:
                selected_draft = get_major_version(selected_version)
            if selected_draft not in PROFILES:
                error = f"MOQT event: unsupported version in ServerSetup draft-{selected_draft}"
                logger.debug(error)
                self._close_session(
                    error_code=SessionCloseCode.PROTOCOL_VIOLATION,
                    reason_phrase=error
                )
            else:
                self.negotiated_draft = selected_draft

            # indicate moqt session setup is complete
            self._moqt_session_setup.set_result(True)

    async def _handle_client_setup(self, msg: ClientSetup) -> None:
        logger.info(f"MOQT event: handle {msg}")
        # Send SERVER_SETUP in response
        if self._is_client:
            error = "MOQT event: received CLIENT_SETUP message as client"
            logger.error(error)
            self._close_session(
                error_code=SessionCloseCode.PROTOCOL_VIOLATION,
                reason_phrase=error
            )
        elif self._moqt_session_setup.done():
            error = "MOQT event: received multiple CLIENT_SETUP messages"
            logger.error(error)
            self.close(
                error_code=SessionCloseCode.PROTOCOL_VIOLATION,
                reason_phrase=error
            )
        else:
            # d14: version negotiated via msg.versions list.
            # d16+: version negotiated via ALPN; msg.versions is empty
            # (the wire format omits it). Trust the ALPN-resolved version
            # already set in self.negotiated_draft on ProtocolNegotiated.
            version_ok = (
                is_draft16_or_later(self.negotiated_draft) or MOQT_VERSION_DRAFT14 in msg.versions
            )
            if version_ok:
                self.server_setup()
                self._moqt_session_setup.set_result(True)
        
    async def _handle_subscribe(self, msg: Subscribe) -> None:
        logger.info(f"MOQT receive: {msg}")
        self.subscribe_ok(
            request_msg=msg,
            expires=0,
            group_order=GroupOrder.ASCENDING,
            content_exists=ContentExistsCode.NO_CONTENT,
        )

    async def _handle_publish_namepace(self, msg: PublishNamespace) -> None:
        logger.info(f"MOQT receive: {msg}")
        if is_draft16_or_later(self.negotiated_draft):
            # d16: respond with REQUEST_OK instead of PublishNamespaceOk
            message = RequestOk(request_id=msg.request_id)
            logger.info(f"MOQT send: {message} request_id: {msg.request_id} namespace: {msg.namespace}")
            self.send_control_message(message)
        else:
            self.publish_namepace_ok(msg)

    async def _handle_subscribe_update(self, msg: SubscribeUpdate) -> None:
        logger.info(f"MOQT event: handle {msg}")
        # Handle subscription update

    async def _handle_subscribe_ok(self, msg: SubscribeOk) -> None:
        logger.info(f"MOQT event: handle {msg}")
        if msg.request_id in self._subscriptions:
            self._subscriptions[msg.request_id].append(msg)
        self._resolve_request(msg.request_id, msg)

    async def _handle_subscribe_error(self, msg: SubscribeError) -> None:
        logger.info(f"MOQT event: handle {msg}")
        if msg.request_id in self._subscriptions:
            self._subscriptions[msg.request_id].append(msg)
        self._resolve_request(msg.request_id, msg)

    async def _handle_publish_namepace_ok(self, msg: PublishNamespaceOk) -> None:
        logger.info(f"MOQT event: handle {msg}")
        self._resolve_request(msg.request_id, msg)

    async def _handle_publish_namepace_error(self, msg: PublishNamespaceError) -> None:
        logger.info(f"MOQT event: handle {msg}")
        self._resolve_request(msg.request_id, msg)

    async def _handle_publish_namepace_done(self, msg: PublishNamespaceDone) -> None:
        logger.info(f"MOQT event: handle {msg}")
        # Publisher is releasing the namespace — for subscribers whose
        # only interest is this namespace, that's a clean end-of-track
        # signal. Close the session so bench tools exit promptly
        # instead of hanging for the QUIC idle timeout.
        self._close_session(SessionCloseCode.NO_ERROR,
                            "publisher namespace done")

    async def _handle_publish_namepace_cancel(self, msg: PublishNamespaceCancel) -> None:
        logger.info(f"MOQT event: handle {msg}")
        # Handle announcement cancellation

    async def _handle_unsubscribe(self, msg: Unsubscribe) -> None:
        """Publisher-side: subscriber wants out. RESET every subgroup uni
        stream we still have open for this subscription's track_alias,
        then drop the subscription state. The subscriber's matching
        STOP_SENDING (if it raced) is reciprocated separately by the
        StopSendingReceived handler."""
        logger.info(f"MOQT event: handle {msg}")
        # Map request_id back to the track_alias we issued in subscribe_ok.
        track_alias = next(
            (ta for ta, rid in self._track_aliases.items()
             if rid == msg.request_id),
            None)
        if track_alias is not None:
            for key, sid in list(self._subgroup_stream_by_key.items()):
                if key[0] == track_alias:
                    self.stream_reset(sid, SessionCloseCode.NO_ERROR)
                    self._cleanup_stream(
                        sid, QuicErrorCode.APPLICATION_ERROR)
        self._subscriptions.pop(msg.request_id, None)

    async def _handle_subscribe_done(self, msg: SubscribeDone) -> None:
        """Subscriber-side: publisher signals end-of-subscription.
        STOP_SENDING any subgroup stream still receiving for this
        subscribe's track_alias so the publisher's write side can
        release cleanly."""
        logger.info(f"MOQT event: handle SubscribeDone "
                     f"request_id={msg.request_id} "
                     f"status={msg.status_code} "
                     f"streams={msg.stream_count}")
        track_alias = next(
            (ta for ta, rid in self._track_aliases.items()
             if rid == msg.request_id),
            None)
        if track_alias is not None:
            for key, sid in list(self._subgroup_stream_by_key.items()):
                if key[0] == track_alias:
                    self.stream_stop_sending(sid, SessionCloseCode.NO_ERROR)
                    # Tombstone here too: the publisher's RESET that
                    # results from our STOP_SENDING will arrive after
                    # any in-flight bytes are already at our doorstep.
                    # Without this mark, those bytes recreate state
                    # with parser=None and parse-fail.
                    self._mark_stream_torn_down(sid)
        future = self._pending_requests.get(msg.request_id)
        if future and not future.done():
            future.set_result(msg)
        # Per spec, every SubscribeDone is terminal for that subscribe.
        # Sessions with a single active subscribe (bench tools) treat
        # it as a clean end-of-session signal.
        self._close_session(SessionCloseCode.NO_ERROR,
                            f"subscribe done: {msg.status_code}")

    async def _handle_max_request_id(self, msg: MaxSubscribeId) -> None:
        logger.info(f"MOQT event: handle {msg}")
        # Update maximum subscribe ID

    async def _handle_subscribes_blocked(self, msg: SubscribesBlocked) -> None:
        logger.info(f"MOQT event: handle {msg}")
        # Handle subscribes blocked notification

    async def _handle_track_status(self, msg: TrackStatus) -> None:
        logger.info(f"MOQT event: handle {msg}")
        # Handle track status request (same format as SUBSCRIBE)

    async def _handle_track_status_ok(self, msg: TrackStatusOk) -> None:
        logger.info(f"MOQT event: handle {msg}")
        # Handle track status response (same format as SUBSCRIBE_OK)

    async def _handle_track_status_error(self, msg: TrackStatusError) -> None:
        logger.info(f"MOQT event: handle {msg}")
        # Handle track status error (same format as SUBSCRIBE_ERROR)

    async def _handle_goaway(self, msg: GoAway) -> None:
        logger.info(f"MOQT event: handle {msg}")
        # Handle session migration request

    async def _handle_subscribe_namespace(self, msg: SubscribeNamespace) -> None:
        logger.info(f"MOQT event: handle {msg}")
        stream_id = self._bidi_streams.get(msg.request_id)
        logger.debug(f"MOQT event: subscribe_namespace bidi_stream={stream_id} request_id={msg.request_id} bidi_streams={self._bidi_streams}")
        self.subscribe_namespace_ok(msg, stream_id=stream_id)

    async def _handle_subscribe_tracks(self, msg: SubscribeTracks) -> None:
        # d18 SUBSCRIBE_TRACKS (0x51) — publisher-side track-prefix subscribe.
        # Track-publish wiring is a publisher feature; log-only for now.
        logger.info(f"MOQT event: handle {msg}")

    async def _handle_publish_blocked(self, msg: PublishBlocked) -> None:
        # d18 PUBLISH_BLOCKED (0x0F) — subscriber-side notice that a track
        # cannot be published. Log-only for now.
        logger.info(f"MOQT event: handle {msg}")

    async def _handle_subscribe_namespace_ok(self, msg: SubscribeNamespaceOk) -> None:
        logger.info(f"MOQT event: handle {msg}")
        self._resolve_request(msg.request_id, msg)

    async def _handle_subscribe_namespace_error(self, msg: SubscribeNamespaceError) -> None:
        logger.info(f"MOQT event: handle {msg}")
        self._resolve_request(msg.request_id, msg)

    async def _handle_unsubscribe_namespace(self, msg: UnsubscribeNamespace) -> None:
        logger.info(f"MOQT event: handle {msg}")
        # No response required per draft-14

    async def _handle_publish(self, msg: Publish) -> None:
        logger.info(f"MOQT event: handle {msg}")
        self._publish_announcements.put_nowait(msg)

    async def _handle_publish_ok(self, msg: PublishOk) -> None:
        logger.info(f"MOQT event: handle {msg}")
        # Subscriber accepted our PUBLISH

    async def _handle_publish_error(self, msg: PublishError) -> None:
        logger.info(f"MOQT event: handle {msg}")
        # Subscriber rejected our PUBLISH

    async def _handle_fetch(self, msg: Fetch) -> None:
        """Default handler for incoming FETCH.

        Auto-accepts with FETCH_OK. Override via register_handler(FETCH, ...)
        for custom fetch handling (e.g. open uni stream and send objects).
        """
        logger.info(f"MOQT event: handle {msg}")
        self.fetch_ok(request_id=msg.request_id)

    async def _handle_fetch_cancel(self, msg: FetchCancel) -> None:
        """Publisher-side: FETCH_CANCEL received for a fetch we are
        serving. Reset the associated uni stream to release our write
        side, and resolve any pending request state.
        """
        logger.info(f"MOQT event: handle {msg}")
        stream_id = self._fetch_stream_by_request.pop(msg.request_id, None)
        if stream_id is not None:
            state = self._data_streams.get(stream_id)
            if state is not None:
                state.key = None  # reverse map already popped above
            self.stream_reset(stream_id, SessionCloseCode.NO_ERROR)
            logger.debug(f"MOQT stream({stream_id}): reset on FETCH_CANCEL "
                         f"for request_id={msg.request_id}")
        self._resolve_request(msg.request_id, msg)

    async def _handle_fetch_ok(self, msg: FetchOk) -> None:
        logger.info(f"MOQT event: handle {msg}")
        self._resolve_request(msg.request_id, msg)

    async def _handle_fetch_error(self, msg: FetchError) -> None:
        """Subscriber-side: FETCH_ERROR received for a fetch we issued.

        If the publisher already opened a fetch uni stream (legitimate
        per spec §9.16.3 — FETCH_OK/ERROR can arrive at any time relative
        to object delivery), send STOP_SENDING to release the peer's
        write side. Then resolve the pending request future with the
        error.
        """
        logger.info(f"MOQT event: handle {msg}")
        stream_id = self._fetch_stream_by_request.pop(msg.request_id, None)
        if stream_id is not None:
            state = self._data_streams.get(stream_id)
            if state is not None:
                state.key = None
            self.stream_stop_sending(stream_id, msg.error_code)
            logger.debug(f"MOQT stream({stream_id}): stop_sending on "
                         f"FETCH_ERROR request_id={msg.request_id}")
        self._resolve_request(msg.request_id, msg)

    # Data handlers need full update - stream reader in progress
    async def _handle_subgroup_header(self, msg: SubgroupHeader, buf: Buffer) -> None:
        """Handle subgroup header message."""
        logger.info(f"MOQT event: handle {msg}")
        # Process subgroup header - 
        sub_id = self._track_aliases.get(msg.track_alias)
        if sub_id is not None:
            sub_state = self._subscriptions[sub_id]
        else:
            logger.warning(f"MOQT: unrecognized track alias: {msg.track_alias}")

    async def _handle_fetch_header(self, msg: FetchHeader) -> None:
        """Registry handler for FetchHeader. Admission + binding happen
        inline in _moqt_handle_data_stream — unknown request_ids are
        rejected at the stream level via STOP_SENDING. Kept here for
        registry completeness so user overrides can hook in."""
        logger.info(f"MOQT event: handle {msg}")
        return

    async def _handle_object_datagram(self, msg: ObjectDatagram) -> None:
        """Handle object datagram message."""
        logger.info(f"MOQT event: handle {msg}")
        # Process object datagram
        # Validate track alias exists
        request_id = self._track_aliases.get(msg.track_alias)
        if request_id is None:
            logger.error(f"MOQT error: datagram for unknown track: {msg.track_alias}")
            self._close_session(
                error_code=SessionCloseCode.PROTOCOL_VIOLATION,
                reason_phrase="Invalid track alias in datagram"
            )
            return
        logger.debug(f"MOQT event: datagram object: {msg.group_id}.{msg.object_id}")
        # Process object data
        # Could add to local storage or forward to subscribers

    async def _handle_object_datagram_status(self, msg: ObjectDatagramStatus) -> None:
        """Handle object datagram status message."""
        logger.info(f"MOQT event: handle {msg}")
        # Process object status
        # Update status in local tracking
        subscibe_id = self._track_aliases.get(msg.track_alias)
        if subscibe_id is None:
            logger.error(f"MOQT error: datagram status for unknown track: {msg.track_alias}")
            self._close_session(
                error_code=SessionCloseCode.PROTOCOL_VIOLATION,
                reason_phrase="Invalid track alias in status"
            )
            return
        # Update object status in local storage or notify subscribers            


    async def _handle_request_ok(self, msg: RequestOk) -> None:
        logger.info(f"MOQT event: handle {msg}")
        self._resolve_request(msg.request_id, msg)

    async def _handle_request_error(self, msg: RequestError) -> None:
        logger.info(f"MOQT event: handle {msg}")
        self._resolve_request(msg.request_id, msg)

    async def _handle_namespace(self, msg) -> None:
        logger.info(f"MOQT event: handle Namespace: {msg}")
        self._namespace_announcements.put_nowait(msg)

    async def _handle_namespace_done(self, msg) -> None:
        logger.info(f"MOQT event: handle NamespaceDone: {msg}")

    async def _handle_request_update(self, msg: RequestUpdate) -> None:
        logger.info(f"MOQT event: handle RequestUpdate: {msg}")

    async def _handle_d18_setup(self, msg) -> None:
        """draft-18 SETUP receive (§10.3). SETUP is symmetric: each peer
        sends its own SETUP on its control write-uni (the client at
        session init, the server on binding the control read-uni). On
        receipt of the peer's SETUP the session is established."""
        logger.info(f"MOQT event: handle d18 Setup: {msg}")
        # One SETUP per direction (§10.3). The flag is set synchronously
        # at first entry, closing the reentry window two pipelined SETUP
        # handler tasks would otherwise share while the server's
        # write-uni open is awaited (WT round-trip).
        if self._d18_setup_seen:
            error = "received multiple SETUP messages"
            logger.error(f"MOQT error: {error}")
            self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
            return
        self._d18_setup_seen = True
        # Server side has no separate session-init step: bring up our own
        # control write-uni and send SETUP on first receipt of the peer's
        # SETUP. The client brings its write-uni up eagerly in
        # client_session_init.
        if not self._is_client and self._d18_control_write_sid is None:
            # Transport-aware: raw QUIC allocates a uni id synchronously,
            # WT round-trips via create_stream. open_uni_stream handles both.
            self._d18_control_write_sid = await self.open_uni_stream()
            logger.info(
                f"MOQT: d18 server control write-uni: "
                f"{self._d18_control_write_sid}")
            self.send_control_message(Setup(options={
                SetupParamType.IMPLEMENTATION: USER_AGENT.encode()}))
            # Replies that raced the write-uni bring-up were deferred;
            # SETUP is on the wire, so release them now.
            self._flush_pending_control()
        if not self._moqt_session_setup.done():
            self._moqt_session_setup.set_result(True)

    # MoQT message classes for serialize/deserialize, message handler methods (unbound)
    MOQT_CONTROL_MESSAGE_REGISTRY: Dict[MOQTMessageType, Tuple[Type[MOQTMessage], Callable]] = {
       # Setup messages
       MOQTMessageType.CLIENT_SETUP: (ClientSetup, _handle_client_setup),
       MOQTMessageType.SERVER_SETUP: (ServerSetup, _handle_server_setup),

       # Subscribe messages
       MOQTMessageType.SUBSCRIBE_UPDATE: (SubscribeUpdate, _handle_subscribe_update),
       MOQTMessageType.SUBSCRIBE: (Subscribe, _handle_subscribe),
       MOQTMessageType.SUBSCRIBE_OK: (SubscribeOk, _handle_subscribe_ok), 
       MOQTMessageType.SUBSCRIBE_ERROR: (SubscribeError, _handle_subscribe_error),

       # PublishNamespace messages
       MOQTMessageType.PUBLISH_NAMESPACE: (PublishNamespace, _handle_publish_namepace),
       MOQTMessageType.PUBLISH_NAMESPACE_OK: (PublishNamespaceOk, _handle_publish_namepace_ok),
       MOQTMessageType.PUBLISH_NAMESPACE_ERROR: (PublishNamespaceError, _handle_publish_namepace_error),
       MOQTMessageType.PUBLISH_NAMESPACE_DONE: (PublishNamespaceDone, _handle_publish_namepace_done),
       MOQTMessageType.PUBLISH_NAMESPACE_CANCEL: (PublishNamespaceCancel, _handle_publish_namepace_cancel),

       # Subscribe control messages
       MOQTMessageType.UNSUBSCRIBE: (Unsubscribe, _handle_unsubscribe),
       MOQTMessageType.PUBLISH_DONE: (SubscribeDone, _handle_subscribe_done),
       MOQTMessageType.MAX_REQUEST_ID: (MaxSubscribeId, _handle_max_request_id),
       MOQTMessageType.REQUESTS_BLOCKED: (SubscribesBlocked, _handle_subscribes_blocked),

       # Status messages
       MOQTMessageType.TRACK_STATUS: (TrackStatus, _handle_track_status),
       MOQTMessageType.TRACK_STATUS_OK: (TrackStatusOk, _handle_track_status_ok),
       MOQTMessageType.TRACK_STATUS_ERROR: (TrackStatusError, _handle_track_status_error),

       # Session control messages
       MOQTMessageType.GOAWAY: (GoAway, _handle_goaway),

       # Subscribe namespace messages
       MOQTMessageType.SUBSCRIBE_NAMESPACE: (SubscribeNamespace, _handle_subscribe_namespace),
       MOQTMessageType.SUBSCRIBE_NAMESPACE_OK: (SubscribeNamespaceOk, _handle_subscribe_namespace_ok),
       MOQTMessageType.SUBSCRIBE_NAMESPACE_ERROR: (SubscribeNamespaceError, _handle_subscribe_namespace_error),
       MOQTMessageType.UNSUBSCRIBE_NAMESPACE: (UnsubscribeNamespace, _handle_unsubscribe_namespace),

       # Fetch messages
       MOQTMessageType.FETCH: (Fetch, _handle_fetch),
       MOQTMessageType.FETCH_CANCEL: (FetchCancel, _handle_fetch_cancel),
       MOQTMessageType.FETCH_OK: (FetchOk, _handle_fetch_ok),
       MOQTMessageType.FETCH_ERROR: (FetchError, _handle_fetch_error),

       # Publish messages (draft-14)
       MOQTMessageType.PUBLISH: (Publish, _handle_publish),
       MOQTMessageType.PUBLISH_OK: (PublishOk, _handle_publish_ok),
       MOQTMessageType.PUBLISH_ERROR: (PublishError, _handle_publish_error),
    }

    # Draft-16 deltas over the draft-14 base: 5 code points repurposed,
    # 6 d14-only points dropped. The per-draft CONTROL_REGISTRY below is
    # built from these once at class-definition time and is read-only at
    # runtime, so a draft-14 session and a draft-16 session in the same
    # event loop never share mutable dispatch state.
    _D16_DELTA: Dict[int, Tuple[Type[MOQTMessage], Callable]] = {
        0x02: (RequestUpdate, _handle_request_update),   # was SUBSCRIBE_UPDATE
        0x05: (RequestError, _handle_request_error),     # was SUBSCRIBE_ERROR
        0x07: (RequestOk, _handle_request_ok),           # was PUBLISH_NAMESPACE_OK
        0x08: (Namespace, _handle_namespace),            # was PUBLISH_NAMESPACE_ERROR
        0x0E: (NamespaceDone, _handle_namespace_done),   # was TRACK_STATUS_OK
    }
    # d16 = d14 base + repurposed code points. The d14-only points d16
    # dropped (0x0F/0x12/0x13/0x14/0x19/0x1F) are RETAINED here as a
    # lenient receive surface: the high-level namespace send paths still
    # emit some of them on d16 (SUBSCRIBE_NAMESPACE_OK 0x12,
    # UNSUBSCRIBE_NAMESPACE 0x14). Spec-strict pruning lands together
    # with the d16 send-side corrections (REQUEST_OK 0x07 for
    # namespace-ok; stream-reset for unsubscribe) as a d16-interop
    # follow-up — pruning them now would reject those frames and break
    # d16 namespace discovery against this library.
    _D16_REGISTRY = {**MOQT_CONTROL_MESSAGE_REGISTRY, **_D16_DELTA}

    # draft-18 dispatch tier (Phase 2 scaffold). Starts from the d16
    # registry — most control messages carry over — and is amended with the
    # d18 wire deltas as the d18 message classes land: type renumbering
    # (SUBSCRIBE_NAMESPACE 0x11->0x50, SUBSCRIBE_TRACKS 0x51,
    # PUBLISH_BLOCKED 0xF, SETUP 0x2F00), Request-ID-dropping replies, and
    # removed messages (Phases 3/5/6, under aiomoqt/messages/d18/). Gated by
    # AIOMOQT_ENABLE_D18 at the session API so it stays inert until the d18
    # wire is complete — a per-draft dict, so it never shares mutable state
    # with d14/d16 sessions.
    _D18_REGISTRY = dict(_D16_REGISTRY)
    # d18 SETUP is symmetric (type 0x2F00, also the control uni-stream
    # type). The d16 CLIENT_SETUP/SERVER_SETUP code points (0x20/0x21) are
    # RESERVED in d18 and never used.
    _D18_REGISTRY.pop(MOQTMessageType.CLIENT_SETUP, None)
    _D18_REGISTRY.pop(MOQTMessageType.SERVER_SETUP, None)
    _D18_REGISTRY[MOQTMessageType.SETUP] = (Setup, _handle_d18_setup)
    # d18 renumbers SUBSCRIBE_NAMESPACE 0x11 -> 0x50 and adds SUBSCRIBE_TRACKS
    # 0x51 + PUBLISH_BLOCKED 0x0F (§10.18-10.20). 0x0F overwrites the d14
    # TRACK_STATUS_ERROR point carried over leniently in the d16 base.
    _D18_REGISTRY.pop(MOQTMessageType.SUBSCRIBE_NAMESPACE, None)  # old 0x11
    _D18_REGISTRY[D18MessageType.SUBSCRIBE_NAMESPACE] = (
        SubscribeNamespace, _handle_subscribe_namespace)
    _D18_REGISTRY[D18MessageType.SUBSCRIBE_TRACKS] = (
        SubscribeTracks, _handle_subscribe_tracks)
    _D18_REGISTRY[D18MessageType.PUBLISH_BLOCKED] = (
        PublishBlocked, _handle_publish_blocked)

    CONTROL_REGISTRY: Dict[int, Dict[int, Tuple[Type[MOQTMessage], Callable]]] = {
        MOQTDraft.DRAFT_14: MOQT_CONTROL_MESSAGE_REGISTRY,
        MOQTDraft.DRAFT_16: _D16_REGISTRY,
        MOQTDraft.DRAFT_18: _D18_REGISTRY,
    }

    # Stream data message types (dispatch by range check, not registry lookup)
    MOQT_STREAM_DATA_REGISTRY: Dict[int, Tuple[Type[MOQTMessage], Callable]] = {
        DataStreamType.FETCH_HEADER: (FetchHeader, _handle_fetch_header),
        # SubgroupHeader: types 0x10-0x1D dispatched by range check
    }

    # Datagram data message types (dispatch by range check, not registry lookup)
    MOQT_DGRAM_DATA_REGISTRY: Dict[int, Tuple[Type[MOQTMessage], Callable]] = {
        # ObjectDatagram: types 0x00-0x07 dispatched by range check
        # ObjectDatagramStatus: types 0x20-0x21 dispatched by range check
    }


from aiopquic.asyncio.protocol import (
    QuicConnectionProtocol as _AioPQuicConnectionProtocol,
)
from aiopquic.asyncio.webtransport import (
    WebTransportClient as _AioPWTClient,
    WebTransportServerSession as _AioPWTServerSession,
    _EVT_WT_STREAM_DATA, _EVT_WT_STREAM_FIN,
    _EVT_WT_STREAM_RESET, _EVT_WT_STOP_SENDING,
    _EVT_WT_DATAGRAM,
)


class MOQTSessionQuic(_MOQTSessionMixin, _AioPQuicConnectionProtocol):
    """aiopquic-backed MoQT session — used for raw QUIC (use_quic=True)."""
    pass


class _WTSessionMixin:
    """Shared init for WT-base MOQTSession subclasses.

    Sets self._quic = self so the mixin's transport-agnostic
    send-side calls (self._quic.send_stream_data etc.) land on
    WebTransportSession's matching API. Resolves
    self._wt_session_setup once the WT session is established
    (set on the server at construction; on the client by open()
    after the CONNECT round-trip).
    """

    _is_wt = True

    def _moqt_wt_finalize(self) -> None:
        self._quic = self
        if not self._wt_session_setup.done():
            self._wt_session_setup.set_result(True)

    def _on_event(self, ev_tuple) -> None:
        """Translate WT data-path events into the QuicEvent classes
        the mixin's quic_event_received already handles — one event
        vocabulary for both transports, so everything above this
        adapter is transport-agnostic. Session-level signals
        (ready/closed/draining, stream-created, tx-drained) fall
        through to the base WebTransportSession handling.

        Translated events MUST NOT also reach super()._on_event —
        the base enqueues them into per-stream inbox queues nothing
        here drains, which pins the data memoryview (and the
        StreamChunk behind it) forever.
        """
        evt_type, sid, data, _is_fin, error_code, _cnx, _, _sc = ev_tuple
        if evt_type == _EVT_WT_STREAM_DATA:
            self.quic_event_received(StreamDataReceived(
                stream_id=sid, data=data, end_stream=False))
        elif evt_type == _EVT_WT_STREAM_FIN:
            self.quic_event_received(StreamDataReceived(
                stream_id=sid, data=data, end_stream=True))
        elif evt_type == _EVT_WT_STREAM_RESET:
            self.quic_event_received(StreamReset(
                stream_id=sid, error_code=error_code))
        elif evt_type == _EVT_WT_STOP_SENDING:
            self.quic_event_received(StopSendingReceived(
                stream_id=sid, error_code=error_code))
        elif evt_type == _EVT_WT_DATAGRAM:
            self.quic_event_received(DatagramFrameReceived(data=data))
        else:
            super()._on_event(ev_tuple)


class MOQTSessionWTClient(
        _WTSessionMixin, _MOQTSessionMixin, _AioPWTClient):
    """aiopquic-backed MoQT session — initiator-side WT (client)."""

    def __init__(self, transport, host: str, port: int, path: str,
                 sni: Optional[str] = None, *,
                 wt_available_protocols: Optional[list] = None,
                 session: 'MOQTPeer'):
        super().__init__(transport, host, port, path,
                         sni=sni,
                         wt_available_protocols=wt_available_protocols,
                         session=session)
        self._moqt_wt_finalize()


class MOQTSessionWTServer(
        _WTSessionMixin, _MOQTSessionMixin, _AioPWTServerSession):
    """aiopquic-backed MoQT session — acceptor-side WT (server)."""

    def __init__(self, transport, state, *, session: 'MOQTPeer'):
        super().__init__(transport, state, session=session)
        _drafts = getattr(session, 'supported_drafts', None)
        draft = _drafts[0] if _drafts and len(_drafts) == 1 else None
        if draft is not None:
            self.negotiated_draft = get_major_version(draft)
        else:
            # Learn the client's draft from the WT-Protocol this server
            # selected (in-band WT-Available-Protocols -> WT-Protocol
            # negotiation, surfaced by aiopquic as negotiated_protocol).
            # If no subprotocol was negotiated we do NOT guess a draft;
            # negotiated_draft keeps its conservative __init__ default.
            negotiated = self.negotiated_protocol
            if negotiated:
                self.negotiated_draft = get_major_version(
                    moqt_version_from_alpn(negotiated))
        self._moqt_wt_finalize()
