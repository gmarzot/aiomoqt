import asyncio
import copy
import functools
import logging
import time
from asyncio import Future
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from typing import (Any, Callable, DefaultDict, Dict, List, Optional, Set,
                    Tuple, Type, Union)

from aiopquic.quic.connection import QuicErrorCode, stream_is_unidirectional
from aiopquic.quic.events import (
    DatagramFrameReceived, ProtocolNegotiated, QuicEvent,
    StopSendingReceived, StreamDataReceived, StreamReset,
)

from importlib.metadata import version

from .context import *
from .messages import *
from .messages.d18 import Setup
from .messages.d20 import PublishStateNotify, Setup as D20Setup
from .messages.fetch import _is_joining
from .messages.data import (FETCH_FLAGS_END_TIMED_OUT,
                            FETCH_FLAGS_END_UNKNOWN, encode_padding)
from .messages.params import (
    RANGE_FILTERS, D20ParamType, D20RequestErrorCode, check_param_scope,
    fill_range, range_filter_count, range_filter_error, uses_location_filter,
)
from .messages import (
    Fetch, LocationFilter, MOQTMessage, Publish, PublishNamespace, PublishOk,
    RequestOk, RequestUpdate, Subscribe, SubscribeNamespace, SubscribeTracks,
    TrackStatus,
)
from .messages.subscribe import SubscribeOk
from .types import *
from .types import (D20MessageType, FilterType, MOQTDraft, MOQTException,
                    MOQTMessageType, MOQTProtocolViolation, MOQTRequestError,
                    ObjectStatus, ParamType, RequestErrorCode,
                    SessionCloseCode, SetupParamType, StreamResetCode,
                    SubscribeDoneCode)
from .utils.buffer import Buffer, BufferReadError
from .utils.logger import *
from .utils.logger import quiet_session
from .utils.url import moqt_setup_path
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
    def __init__(self, error_code: int, reason: str, quiet: bool = False):
        super().__init__(reason)
        self.error_code = error_code
        self.reason = reason
        self.quiet = quiet  # an expected cancel: log at debug


# How far below the largest peer request id a smaller one can still
# arrive and be believed. Requests ride separate streams with no
# ordering between them, so the depth is the peer's request
# concurrency; ids step by 2, so this is 64 outstanding requests.
PEER_REQUEST_REORDER_WINDOW = 128


def rid_floor(peer_request_max: int) -> int:
    """Oldest peer request id still inside the reorder window."""
    return peer_request_max - PEER_REQUEST_REORDER_WINDOW


# base class for client and server session objects
class MOQTPeer:
    """MOQT client and server base-class."""
    def __init__(self, libquicr_compat: bool = False,
                 tx_max_inflight_bytes: Optional[int] =
                     DEFAULT_TX_MAX_INFLIGHT_BYTES,
                 implementation: Optional[str] = USER_AGENT):
        # MOQT_IMPLEMENTATION Setup Option value; None omits it.
        self.implementation = implementation
        #  message handlers
        self._control_msg_handlers: Dict[int, Callable] = {}
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
        session's negotiated draft at dispatch time. A message addressed
        to a track the session publishes goes to that track instead.

        Registration is expanded over HANDLER_ALIASES so a type that is
        renumbered between drafts stays registered under every number.
        """
        self._control_msg_handlers[msg_type] = handler
        for alias in HANDLER_ALIASES.get(msg_type, ()):
            self._control_msg_handlers[alias] = handler


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
    last_activity: monotonic ts the stream was first seen (binding-
            deadline reaper).
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
    # From the subgroup header. A relay has to forward the publisher's
    # priority rather than substitute its own, so it has to be able to
    # see it: the object itself does not carry it.
    publisher_priority: Optional[int] = None


@dataclass(slots=True)
class _RequestEntry:
    """A request routed to the track that owns it. `sub` is the track's
    state for this peer; `alias` the track alias the request carries."""
    role: str
    track: Any
    sub: Any = None
    alias: Optional[int] = None


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

    # The peer named a MOQTServer quiet_peers implementation: this
    # session's records below WARNING are DEBUG records. The token covers
    # the rest of the event whose SETUP marked it.
    _log_quiet = False
    _quiet_token = None
    # We sent a GOAWAY: every later peer request is refused (§10.4).
    _goaway_sent = False
    # The peer's control-stream GOAWAY: no new request is sent after it.
    goaway_received: Optional['GoAway'] = None
    # Called as on_goaway(msg, request_id) for each GOAWAY received:
    # request_id None for the session, else the request to migrate. May be
    # a coroutine function.
    on_goaway: Optional[Callable] = None

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
        # The peer's d20+ SETUP limits; 0 is the spec default for both.
        self._peer_max_request_updates = 0
        self._peer_max_filter_ranges = 0
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
        # Largest request id received from the peer (§10.1: the peer
        # increments by 2 per request), and the ids actually seen within
        # the reorder window — arrival order across request streams is
        # not guaranteed, so only the set can identify a duplicate.
        self._peer_request_max = -1
        self._peer_request_seen: set = set()
        # Streams that carried a peer GOAWAY: one per control or request
        # stream (§10.4).
        self._peer_goaway_streams: Set[int] = set()
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
        # Raw-QUIC sessions offering several drafts learn theirs from
        # ProtocolNegotiated, which the data ring can outrun: a d18 peer's
        # control uni may arrive first. Its bytes wait here and replay
        # through _route_stream_data once the draft is settled.
        self._draft_settled = _pinned is not None
        self._pre_draft_stream_data: Dict[int, Tuple[bytearray, bool]] = {}
        # d18 replies ride a request's stream with no id of their own and
        # answer, in order, the request (None, until its reply) and each
        # REQUEST_UPDATE we sent on it. Keyed by the request's id.
        self._tx_updates: Dict[int, deque] = {}
        # request id -> (opener class, sent by us), for the d20 rules that
        # depend on the request a REQUEST_UPDATE or reply belongs to.
        # Bounded; the oldest are forgotten.
        self._request_kinds: "OrderedDict[int, Tuple[type, bool]]" = \
            OrderedDict()
        # The peer's d20+ SUBSCRIBE_TRACKS: request id -> the parameters a
        # resulting PUBLISH carries (§10.20.1).
        self._subscribe_tracks_params: Dict[int, Dict[int, Any]] = {}
        # Track aliases found malformed (§2.4.2): subscription cancelled,
        # further streams refused.
        self._malformed_aliases: Set[int] = set()
        # alias -> {group_id: first object id that does not exist}, from
        # END_OF_GROUP status objects and FIN on END_OF_GROUP-bit streams.
        self._group_bound: Dict[int, Dict[int, int]] = {}
        # alias -> (group_id, object_id) location from END_OF_TRACK.
        self._track_bound: Dict[int, Tuple[int, int]] = {}
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
        self._close_err = None  # first (error_code, reason_phrase) closed with

        self._bidi_streams: Dict[int, int] = {}  # map request_id to bidi stream_id (d16)
        self._bidi_stream_requests: Dict[int, int] = {}  # map bidi stream_id to request_id (d16)
        # Request streams cancelled by STOP_SENDING whose peer half is still open.
        self._cancelled_request_streams: Set[int] = set()
        # request_id -> callback fired when the request's stream is
        # terminated by the peer (§3.3.2 cancellation).
        self._request_cancel_handlers: Dict[int, Callable] = {}
        # request_id -> callback fired on PUBLISH_DONE for that request.
        self._publish_done_handlers: Dict[int, Callable] = {}
        # Our SUBSCRIBE request id -> object handler, bound to the track
        # alias where its SUBSCRIBE_OK is parsed.
        self._pending_object_handlers: Dict[int, Callable] = {}
        # Tracks this session serves, by full track name; control
        # messages addressed to one are routed to it.
        self._published_tracks: Dict[Tuple[Tuple[bytes, ...], bytes], Any] = {}
        # Request ids, ours and the peer's, to the entry that owns them.
        # Client ids are even and server ids odd, so they cannot collide.
        self._requests: Dict[int, _RequestEntry] = {}
        # Admitted peer SUBSCRIBE / SUBSCRIBE_NAMESPACE / SUBSCRIBE_TRACKS:
        # request_id -> (message class, full track name or prefix).
        self._peer_requests: Dict[int, Tuple[type, tuple]] = {}
        # The peer's registered tokens (§10.2.2): alias -> token, held
        # within the MAX_AUTH_TOKEN_CACHE_SIZE we advertised (0 = aliases
        # not accepted).
        self._auth_tokens: Dict[int, AuthToken] = {}
        # Called as auth_token_validator(token, msg) for the token a d16+
        # peer request carries, after alias resolution; a returned
        # RequestErrorCode refuses the request (§10.2.2).
        self.auth_token_validator: Optional[
            Callable[[AuthToken, MOQTMessage], Optional[int]]] = None
        self._auth_token_cache_used = 0
        self._auth_token_cache_max = 0
        # Aliases the peer assigned (SUBSCRIBE_OK / PUBLISH) -> our
        # request id: the receive-side registry.
        self._track_aliases: Dict[int, int] = {}
        # Aliases we assigned as publisher -> the peer's request id. A
        # separate number space: both ends count from 0.
        self._published_aliases: Dict[int, int] = {}
        # Set when client_session_init completes (ms, monotonic delta).
        self._established_ms: Optional[float] = None
        # Per-track object delivery keyed by track_alias; falls back to
        # the session-global on_object_received when no entry matches.
        self._object_handlers: Dict[int, Callable] = {}
        self._stream_end_handlers: Dict[int, Callable] = {}
        # Stream ends for an alias with no handler yet: a short stream can
        # end before its subscriber registers one. Replayed on registration.
        self._early_stream_ends: "OrderedDict[int, deque]" = OrderedDict()
        # Objects for an unbound alias with no handler to take them,
        # replayed on binding.
        self._reset_early_objects()
        self._early_objects_dropped: int = 0
        # track_alias -> DEFAULT_PUBLISHER_PRIORITY (property 0x0E, Track
        # scope, §12.4). A subgroup whose header sets DEFAULT_PRIORITY
        # omits the Priority field and inherits this.
        self._track_default_priority: Dict[int, int] = {}
        # Aliases seen on data streams before any control message bound
        # them: {alias: [first_seen_monotonic, streams_admitted]}. Data
        # legitimately races SUBSCRIBE_OK (§10.4.2), so an entry here is
        # only a problem if it never clears — see _check_unbound_aliases.
        # Datagrams that failed to parse — dropped objects, not
        # session errors. Surfaced in stats rather than raising.
        self._dgram_parse_errors: int = 0
        self._unbound_aliases: dict = {}
        self._unbound_escalated: set = set()
        self._subscriptions: Dict[int, List] = {}  # map subscription_id to request
        self._pending_requests: Dict[int, Future[MOQTMessage]] = {}  # unified response futures
        # Bounded record of request ids WE issued (recorded at allocation).
        # A response for one of these with no live future is an ack we did
        # not await — a fire-and-forget publish / publish_namespace
        # (wait_response=False), or a late / duplicate reply — logged at
        # DEBUG. A response for an id we never issued stays WARNING.
        self._sent_requests: deque = deque(maxlen=1024)
        # Our PUBLISH_NAMESPACE requests: request id -> namespace.
        self._announced: Dict[int, tuple] = {}
        # SUBSCRIBE_NAMESPACE requests the default handler answered: request
        # id -> prefix. They hear NAMESPACE and NAMESPACE_DONE for what this
        # session announces (d18 §6.1).
        self._discovery_subs: Dict[int, tuple] = {}
        # Our TRACK_STATUS request ids awaiting a reply: the only REQUEST_OK
        # that may carry Track Properties (§10.5).
        self._track_status_requests: set = set()

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

        # Optional callback for received data objects:
        #   fn(msg, size_bytes, recv_time_us, group_id, subgroup_id)
        self.on_object_received: Optional[Callable] = None
        self._object_cb_errors = 0
        self._padding_rx = 0  # PADDING streams and datagrams discarded
        # Optional callback for received FetchObjects (fetch uni stream):
        #   fn(msg, size_bytes, recv_time_us, request_id)
        # Fires for normal objects only; End of Range markers go to
        # on_fetch_gap as fn(marker, request_id).
        self.on_fetch_object: Optional[Callable] = None
        self.on_fetch_gap: Optional[Callable] = None
        # d20+ fill fetch streams we asked for: SUBSCRIBE or REQUEST_UPDATE
        # request id -> (FILL_PARAMETERS, subscription Location Filter,
        # group order).
        self._fill_requests: Dict[int, tuple] = {}
        # FETCH request id -> callback for its objects, ahead of
        # on_fetch_object (same signature).
        self._fetch_object_handlers: Dict[int, Callable] = {}

        # Per-fetch completion futures. Resolved when the fetch uni
        # stream's processing task exits (FIN, RESET, or error).
        # JoinedTrack registers a future here to get notified when
        # the fetch buffer fill is done.
        self._fetch_done_futures: Dict[int, Future] = {}  # request_id → Future
        # Outcome of fetch streams that ended before anyone awaited them:
        # request_id -> clean. Bounded; the oldest are forgotten.
        self._fetch_done_results: "OrderedDict[int, bool]" = OrderedDict()

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
        if self._close_err is not None and not fut.done():
            self._pending_requests.pop(request_id, None)
            raise MOQTRequestError(
                error_code=int(RequestErrorCode.INTERNAL_ERROR),
                reason=f"session closed: {self._close_err[1]}",
                retry_interval=0)
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

    async def _await_routed(self, request_id: int):
        """_await_response for a request that registered handlers: a
        wait that fails or is abandoned drops them."""
        try:
            return await self._await_response(request_id)
        except BaseException:
            self._drop_request_routes(request_id)
            raise

    async def await_fetch_done(self, request_id: int,
                              timeout: float = 10.0) -> bool:
        """Wait for a fetch stream to complete (FIN).

        The future is pre-registered by join() or fetch() before
        messages are sent, so there's no race with fast completions.
        From d20 a fill fetch stream is awaited by the Request ID of its
        SUBSCRIBE or REQUEST_UPDATE; an empty fill completes as the reply
        shows it.
        Returns True if the stream completed cleanly, False on
        error/timeout.

        Args:
            request_id: The fetch request_id (from the Fetch message
                or FetchOk.request_id).
            timeout: seconds to wait.
        """
        fut = self._fetch_done_futures.get(request_id)
        if fut is None:
            # The stream already ended, or never opened.
            if request_id in self._fetch_done_results:
                return self._fetch_done_results.pop(request_id)
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

    # d14/d16 request-id credit (§9.5): our advertised ceiling on the
    # peer's ids and theirs on ours. None = no ceiling (d18).
    REQUEST_ID_WINDOW = 10000
    _local_request_max: Optional[int] = None
    _peer_request_limit: Optional[int] = None
    _requests_blocked_sent = False

    def _allocate_request_id(self, opener: bool = True) -> int:
        """Next request id. A new request (`opener`) is refused after the
        peer's GOAWAY. At the peer's MAX_REQUEST_ID (pre-d18) send
        REQUESTS_BLOCKED once and refuse the request."""
        if opener and self.goaway_received is not None:
            raise MOQTRequestError(
                error_code=int(RequestErrorCode.GOING_AWAY),
                reason="the peer sent GOAWAY", retry_interval=0)
        limit = self._peer_request_limit
        if limit is not None and self._next_request_id >= limit:
            if not self._requests_blocked_sent:
                self._requests_blocked_sent = True
                self.send_control_message(
                    SubscribesBlocked(maximum_request_id=limit))
            raise MOQTRequestError(
                error_code=int(RequestErrorCode.INTERNAL_ERROR),
                reason=f"peer MAX_REQUEST_ID {limit} exhausted",
                retry_interval=0)
        request_id = self._next_request_id
        self._next_request_id += 2
        self._sent_requests.append(request_id)
        return request_id

    def _allocate_track_alias(self, request_id: int = 1) -> int:
        """Next alias for a track we publish, bound to the peer's
        request."""
        track_alias = self._next_track_alias
        self._next_track_alias += 1
        self._published_aliases[track_alias] = request_id
        return track_alias

    def register_object_handler(self, track_alias: int,
                                callback: Callable) -> None:
        """Route this track's objects to `callback` instead of the
        session-global on_object_received (same signature). The alias is
        known once SUBSCRIBE_OK / PUBLISH arrives.

        Objects that arrived for the alias before it was bound, in the
        last EARLY_OBJECT_HOLD_S, are delivered at once in arrival
        order."""
        self._bind_object_handler(track_alias, callback)

    def unregister_object_handler(self, track_alias: int) -> None:
        self._object_handlers.pop(track_alias, None)
        self._discard_early_objects(track_alias)

    def _bind_object_handler(self, alias: int, callback: Callable) -> None:
        """Bind `callback` to the alias and replay its held objects."""
        self._object_handlers[alias] = callback
        held = self._early_objects.pop(alias, None)
        if not held:
            return
        horizon = time.monotonic() - self.EARLY_OBJECT_HOLD_S
        for held_at, nbytes, args in held:
            self._early_objects_count -= 1
            self._early_objects_bytes -= nbytes
            if held_at < horizon:
                self._early_object_dropped(alias, "expired")
                continue
            try:
                callback(*args)
            except Exception:
                logger.warning("object handler raised on replay",
                               exc_info=True)

    # Per-session hold bounds for early objects; the count exceeds
    # EARLY_STREAM_END_MAX since one video group can.
    EARLY_OBJECT_HOLD_S = 5.0
    EARLY_OBJECT_MAX = 1024
    EARLY_OBJECT_MAX_BYTES = 4 * 1024 * 1024

    def _reset_early_objects(self) -> None:
        # {alias: deque[(held_at, nbytes, cb_args)]}
        self._early_objects: Dict[int, deque] = {}
        self._early_objects_count = 0
        self._early_objects_bytes = 0

    def _hold_early_object(self, alias: int, nbytes: int, args) -> None:
        """Hold an object for an alias no control message has bound yet;
        one for a bound alias with no handler has no taker and drops."""
        if alias is None or (alias in self._track_aliases
                             and alias not in self._unbound_aliases):
            return
        self._expire_early_objects()
        if (self._early_objects_count >= self.EARLY_OBJECT_MAX
                or self._early_objects_bytes + nbytes
                > self.EARLY_OBJECT_MAX_BYTES):
            self._early_object_dropped(alias, "hold full")
            return
        # The parser reuses the object and a payload view need not outlive
        # delivery; the hold does.
        obj = copy.copy(args[0])
        if isinstance(getattr(obj, 'payload', None), memoryview):
            obj.payload = bytes(obj.payload)
        args = (obj,) + tuple(args[1:])
        q = self._early_objects.get(alias)
        if q is None:
            q = self._early_objects[alias] = deque()
        q.append((time.monotonic(), nbytes, args))
        self._early_objects_count += 1
        self._early_objects_bytes += nbytes

    def _expire_early_objects(self) -> None:
        horizon = time.monotonic() - self.EARLY_OBJECT_HOLD_S
        for alias in list(self._early_objects):
            q = self._early_objects[alias]
            while q and q[0][0] < horizon:
                _, nbytes, _ = q.popleft()
                self._early_objects_count -= 1
                self._early_objects_bytes -= nbytes
                self._early_object_dropped(alias, "expired")
            if not q:
                del self._early_objects[alias]

    def _discard_early_objects(self, alias: int) -> None:
        q = self._early_objects.pop(alias, None)
        if q:
            self._early_objects_count -= len(q)
            self._early_objects_bytes -= sum(e[1] for e in q)
            self._early_objects_dropped += len(q)

    def _early_object_dropped(self, alias: int, why: str) -> None:
        self._early_objects_dropped += 1
        logger.debug(f"MOQT: dropped early object for track_alias={alias} "
                     f"({why}; {self._early_objects_dropped} dropped)")

    def _latch_track_priority(self, track_alias: int,
                              track_extensions) -> None:
        """Record DEFAULT_PUBLISHER_PRIORITY for a track.

        Subgroups that set the header's DEFAULT_PRIORITY bit carry no
        Priority field and inherit this value; without it they would be
        reported at the library default, which is a different number the
        publisher never chose.
        """
        if track_alias is None or not track_extensions:
            return
        prio = track_extensions.get(ParamType.PUBLISHER_PRIORITY)
        if prio is not None:
            self._track_default_priority[track_alias] = int(prio)
            logger.debug(f"track {track_alias}: default publisher "
                         f"priority {int(prio)}")

    def _track_key(self, namespace, trackname) -> Tuple[Tuple[bytes, ...], bytes]:
        name = trackname.encode() if isinstance(trackname, str) else trackname
        return (self._make_namespace_tuple(namespace), name)

    def _attach_track(self, track) -> None:
        """Route SUBSCRIBEs for the track's full name to the track. A
        full name serves one track per session."""
        key = self._track_key(track.namespace, track.trackname)
        held = self._published_tracks.get(key)
        if held is not None and held is not track:
            raise ValueError(
                f"track {track.namespace}/{track.trackname} is already "
                f"published on this session by another track")
        self._published_tracks[key] = track

    def _bind_request(self, request_id: int, track, sub=None,
                      alias: Optional[int] = None) -> None:
        """Route PUBLISH_OK and updates for this request to the track."""
        self._requests[request_id] = _RequestEntry(
            role="publisher", track=track, sub=sub, alias=alias)

    def _bind_fetch(self, request_id: int, track) -> None:
        """Route updates for a FETCH the track is serving to it."""
        self._requests[request_id] = _RequestEntry(role="fetch", track=track)

    def _unbind_request(self, request_id: int) -> None:
        self._requests.pop(request_id, None)

    def _track_handler(self, msg) -> Optional[Callable]:
        """The published track's handler for a message addressed to it,
        else None."""
        if isinstance(msg, Subscribe):
            track = self._published_tracks.get(
                (tuple(msg.track_namespace), msg.track_name))
            return None if track is None else track._on_subscribe
        if isinstance(msg, TrackStatus):
            track = (self._published_tracks.get(
                (tuple(msg.track_namespace), msg.track_name))
                if is_draft16_or_later(self.negotiated_draft) else None)
            return None if track is None else track._on_track_status
        if isinstance(msg, Fetch):
            if _is_joining(msg.fetch_type):
                # §10.12.2: joins a subscription this session serves. One
                # admitted in the same read is not bound yet; its handler
                # task runs first, so the FETCH follows it to the track.
                entry = self._requests.get(msg.joining_request_id)
                peer = self._peer_requests.get(msg.joining_request_id)
                if entry is not None and entry.sub is not None:
                    track = entry.track
                elif peer is not None and peer[0] is Subscribe:
                    track = self._published_tracks.get(peer[1])
                else:
                    track = None
            else:
                track = self._published_tracks.get(
                    (tuple(msg.namespace or ()), msg.track_name))
            return None if track is None else track._on_fetch
        if isinstance(msg, PublishOk):
            rid, name = msg.request_id, '_on_publish_ok'
        elif isinstance(msg, RequestUpdate):
            rid, name = msg.existing_request_id, '_on_request_update'
            entry = self._requests.get(rid)
            if entry is not None and entry.role == "fetch":
                name = '_on_fetch_update'
        elif isinstance(msg, SubscribeUpdate):
            rid, name = msg.subscription_request_id, '_on_subscribe_update'
        else:
            return None
        entry = self._requests.get(rid)
        return None if entry is None else getattr(entry.track, name)

    def register_request_cancel_handler(self, request_id: int,
                                        callback: Callable) -> None:
        """Called as callback(request_id) when the peer terminates the
        request's bidi stream (§3.3.2) — the d18 cancellation path for
        SUBSCRIBE/PUBLISH/etc. Publishers stop feeding on it."""
        self._request_cancel_handlers[request_id] = callback

    def register_publish_done_handler(self, request_id: int,
                                      callback: Callable) -> None:
        """Called as callback(msg) when PUBLISH_DONE ends the
        subscription `request_id` (§10.11). Once per request; the
        session stays up."""
        self._publish_done_handlers[request_id] = callback

    def register_fetch_object_handler(self, request_id: int,
                                      callback: Callable) -> None:
        """Route FETCH `request_id`'s objects to `callback` instead of the
        session-global on_fetch_object (same signature). Dropped when the
        fetch stream ends or the FETCH is refused."""
        self._fetch_object_handlers[request_id] = callback

    def unregister_fetch_object_handler(self, request_id: int) -> None:
        self._fetch_object_handlers.pop(request_id, None)

    def _drop_request_routes(self, request_id) -> None:
        """Forget the handlers a request of ours registered: it was
        refused, or its awaiter gave up."""
        self._pending_object_handlers.pop(request_id, None)
        self._publish_done_handlers.pop(request_id, None)
        self._fetch_object_handlers.pop(request_id, None)
        if (self._profile.fetch_location_filter
                and self._fill_requests.pop(request_id, None) is not None):
            self._end_fill(request_id, False)

    def _end_fill(self, request_id: int, clean: bool) -> None:
        """A fill we asked for will open no stream: settle its awaiters."""
        self._fill_requests.pop(request_id, None)
        if request_id in self._fetch_stream_by_request:
            return
        self._fetch_object_handlers.pop(request_id, None)
        fut = self._fetch_done_futures.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.set_result(clean)

    @staticmethod
    def _reply_largest(msg) -> Optional[Tuple[int, int]]:
        """The Largest Object a SUBSCRIBE_OK or REQUEST_OK reports."""
        if isinstance(msg, SubscribeOk):
            if (not msg.content_exists or msg.largest_group_id is None
                    or msg.largest_object_id is None):
                return None
            return (msg.largest_group_id, msg.largest_object_id)
        largest = (getattr(msg, 'parameters', None) or {}).get(
            ParamType.LARGEST_OBJECT)
        return tuple(largest) if isinstance(largest, (tuple, list)) else None

    def _check_fetch_ok_end(self, msg: FetchOk) -> None:
        """An End Location before the FETCH's Start closes the session
        (d18 §10.13, d20 §10.14). Before d20 End is the last Object plus
        1, and Object 0 covers the whole group."""
        sent = next((m for m in self._subscriptions.get(msg.request_id, ())
                     if isinstance(m, Fetch)), None)
        if sent is None:
            return
        lf = sent.location_filter
        if lf is not None:
            start = (lf.start_group, lf.start_object)
        else:
            start = (sent.start_group or 0, sent.start_object or 0)
        if None in start:
            return  # relative to Largest Object
        if self._profile.fetch_location_filter:
            end = (msg.largest_group_id, msg.largest_object_id - 1)
        elif msg.largest_object_id == 0:
            end = (msg.largest_group_id, float('inf'))
        else:
            end = (msg.largest_group_id, msg.largest_object_id)
        if end < start:
            raise MOQTProtocolViolation(
                f"FETCH_OK End Location {end} before the FETCH's Start "
                f"{start}")

    # Track Aliases of received tracks with an Established subscription:
    # alias -> ((namespace, name), request id).
    _established_aliases: Optional[Dict[int, Tuple[Any, int]]] = None

    @staticmethod
    def _msg_track(msg) -> Optional[Tuple[tuple, bytes]]:
        ns = getattr(msg, 'track_namespace', None)
        if ns is None:
            ns = getattr(msg, 'namespace', None)
        name = getattr(msg, 'track_name', None)
        if ns is None or name is None:
            return None
        return (tuple(ns), name)

    def _check_alias(self, alias: int, track, request_id: int) -> None:
        """§11.1: a Track Alias another track's Established subscription
        uses closes the session with DUPLICATE_TRACK_ALIAS."""
        held = (self._established_aliases or {}).get(alias)
        if (held is not None and held[1] != request_id
                and None not in (held[0], track) and held[0] != track):
            raise MOQTException(
                SessionCloseCode.DUPLICATE_TRACK_ALIAS,
                f"Track Alias {alias} already in use by request {held[1]}")

    def _establish_alias(self, alias: int, track, request_id: int) -> None:
        if self._established_aliases is None:
            self._established_aliases = {}
        self._established_aliases[alias] = (track, request_id)

    def _retire_alias(self, request_id: int) -> None:
        """A received track's subscription ended: its alias is free."""
        held = self._established_aliases
        if held:
            for alias in [a for a, v in held.items() if v[1] == request_id]:
                del held[alias]

    def _retire_published_alias(self, request_id: int) -> None:
        for alias, rid in list(self._published_aliases.items()):
            if rid == request_id:
                del self._published_aliases[alias]

    def _route_reply(self, msg) -> None:
        """Act on a reply as it is parsed, ahead of the data streams in
        its batch and of the handler task: SUBSCRIBE_OK binds the
        request's object handler to its alias; an error reply drops what
        the request registered, and a refused PUBLISH_NAMESPACE announces
        nothing. A FETCH_OK ending before its Start closes the session;
        from d20 a reply showing an empty fill range ends the fill."""
        if isinstance(msg, SubscribeOk) and msg.track_alias is not None:
            sent = next((m for m in self._subscriptions.get(
                msg.request_id, ()) if isinstance(m, Subscribe)), None)
            track = None if sent is None else self._msg_track(sent)
            self._check_alias(msg.track_alias, track, msg.request_id)
            self._establish_alias(msg.track_alias, track, msg.request_id)
        elif (isinstance(msg, Publish)
              and getattr(msg, 'track_alias', None) is not None):
            self._check_alias(msg.track_alias, self._msg_track(msg),
                              msg.request_id)
        if isinstance(msg, SubscribeOk):
            cb = self._pending_object_handlers.pop(msg.request_id, None)
            if cb is not None and msg.track_alias is not None:
                self._bind_object_handler(msg.track_alias, cb)
        elif self._is_error_response(msg):
            rid = getattr(msg, 'request_id', None)
            self._drop_request_routes(rid)
            self._announced.pop(rid, None)
            return
        if isinstance(msg, FetchOk):
            self._check_fetch_ok_end(msg)
        if not self._profile.fetch_location_filter:
            return
        fill = self._fill_requests.get(getattr(msg, 'request_id', None))
        if fill is not None and fill_range(
                fill[0], fill[1], self._reply_largest(msg)) is None:
            # d20 §5.1.3: an empty fill range opens no stream.
            self._end_fill(msg.request_id, True)

    def _on_request_stream_terminated(
            self, stream_id: int, *,
            stop_sending_code: Optional[int] = None) -> None:
        """§3.3.2: terminating a request's bidi stream cancels the
        request. Tear down its state and notify the owner.

        STOP_SENDING ends only our send half. The owner is told at once,
        but a REQUEST_ERROR or PUBLISH_DONE the peer sends after it still
        answers the request, so the binding, awaiter and PUBLISH_DONE
        handler stay until the peer's half ends (FIN or reset)."""
        if stop_sending_code is not None:
            if stream_id in self._cancelled_request_streams:
                return
            request_id = self._bidi_stream_requests.get(stream_id)
            if request_id is None:
                return
            self._cancelled_request_streams.add(stream_id)
            self._bidi_streams.pop(request_id, None)
            self._notify_request_cancelled(
                request_id, f"STOP_SENDING (stream {stream_id})",
                replies_pending=True)
            return
        request_id = self._bidi_stream_requests.pop(stream_id, None)
        self._cancelled_request_streams.discard(stream_id)
        if request_id is not None:
            self._loop.call_soon(self._settle_cancelled_request, request_id,
                                 f"stream termination (stream {stream_id})")

    def _settle_cancelled_request(self, request_id: int, why: str) -> None:
        """Tear down a request whose stream the peer has ended. Runs a
        loop turn later, so a reply parsed from that stream resolves the
        awaiter first; an awaiter nothing answered fails."""
        self._bidi_streams.pop(request_id, None)
        self._subscriptions.pop(request_id, None)
        self._retire_alias(request_id)
        self._fail_waiting_updates(request_id, why)
        alias = next((ta for ta, rid in self._track_aliases.items()
                      if rid == request_id), None)
        if alias is not None:
            self._forget_track_bounds(alias)
        fut = self._pending_requests.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.set_exception(MOQTRequestError(
                error_code=0x1, reason=f"request cancelled by peer ({why})",
                retry_interval=0))
        self._notify_request_cancelled(request_id, why)

    def _notify_request_cancelled(self, request_id: int, why: str,
                                  replies_pending: bool = False) -> None:
        """Tell the request's owner it is cancelled, however the peer
        said so: a terminated request stream at d18, UNSUBSCRIBE before
        it. Publishers stop feeding on this. With replies_pending the
        PUBLISH_DONE handler stays for a PUBLISH_DONE still inbound."""
        self._note_request_gone(request_id)
        cb = self._request_cancel_handlers.pop(request_id, None)
        self._requests.pop(request_id, None)
        self._peer_requests.pop(request_id, None)
        self._retire_published_alias(request_id)
        if not replies_pending:
            self._publish_done_handlers.pop(request_id, None)
        logger.info(f"MOQT: request {request_id} cancelled by {why}")
        if cb is not None:
            try:
                cb(request_id)
            except Exception:
                logger.debug("request-cancel handler raised", exc_info=True)

    def register_stream_end_handler(self, track_alias: int,
                                    callback: Callable) -> None:
        """Called as callback(group_id, subgroup_id, clean, reset_code)
        when one of this track's subgroup streams ends: clean=True for a
        FIN, False for a reset/STOP_SENDING/rejection (reset_code is the
        peer's code, 0 if none).

        A publisher may signal end-of-group by closing the subgroup
        stream rather than by sending an END_OF_GROUP object (§11.4.2:
        inferable from a FIN, never from a reset), so a relay that only
        watches objects never learns the group ended and leaves its
        downstream stream open to be reset at teardown.

        Streams of this track that ended in the last
        EARLY_STREAM_END_HOLD_S, before the handler existed, are
        reported at once; register the object handler first so held
        objects replay ahead of them."""
        self._stream_end_handlers[track_alias] = callback
        early = self._early_stream_ends.pop(track_alias, None)
        if not early:
            return
        horizon = time.monotonic() - self.EARLY_STREAM_END_HOLD_S
        for ended_at, group_id, subgroup_id, clean, reset_code in early:
            if ended_at < horizon:
                continue
            try:
                callback(group_id, subgroup_id, clean=clean,
                         reset_code=reset_code)
            except Exception:
                logger.debug("stream-end handler raised", exc_info=True)

    def unregister_stream_end_handler(self, track_alias: int) -> None:
        self._stream_end_handlers.pop(track_alias, None)
        self._early_stream_ends.pop(track_alias, None)

    # Early stream ends are kept this long, for at most this many
    # aliases and this many ends per alias.
    EARLY_STREAM_END_HOLD_S = 5.0
    EARLY_STREAM_END_MAX = 64

    def _note_early_stream_end(self, alias: int, group_id, subgroup_id,
                               clean: bool, reset_code: int) -> None:
        ends = self._early_stream_ends.get(alias)
        if ends is None:
            if len(self._early_stream_ends) >= self.EARLY_STREAM_END_MAX:
                self._early_stream_ends.popitem(last=False)
            ends = self._early_stream_ends[alias] = deque(
                maxlen=self.EARLY_STREAM_END_MAX)
        ends.append((time.monotonic(), group_id, subgroup_id, clean,
                     reset_code))

    def _call_object_cb(self, cb: Callable, *args) -> None:
        """Run an app object callback; a raise costs only that object."""
        try:
            cb(*args)
        except Exception:
            self._object_cb_raised()

    def _object_cb_raised(self) -> None:
        """Count and log the object callback exception being handled."""
        self._object_cb_errors += 1
        if self._object_cb_errors == 1:
            logger.warning("object callback raised", exc_info=True)
        else:
            logger.debug("object callback raised", exc_info=True)

    def _object_cb(self, track_alias) -> Optional[Callable]:
        """Delivery callback for a track: per-alias route, else global."""
        cb = (self._object_handlers.get(track_alias)
              if track_alias is not None else None)
        return cb or self.on_object_received

    def _control_task_done(self, task: asyncio.Task) -> None:
        """Remove control task from set."""
        self._tasks.discard(task)
        if task.cancelled():
            # Cancellation is teardown: the session or the loop is closing.
            logger.debug("MOQT: control task cancelled")
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
                    # §9/§10: an unknown control message type MUST close
                    # the session — parsing cannot continue past it.
                    raise MOQTProtocolViolation(
                        f"unknown control message type {hex(msg_type)} "
                        f"for draft-{self.negotiated_draft}")
            # Look up message class (version-aware for shared code points)
            message_class, handler = self._get_control_entry(msg_type)
            logger.debug(f"MOQT event: control message: {message_class.__name__} ({msg_len} bytes)")
            # Deserialize message. Pass end_pos so trailing fields
            # without a wire-level length prefix (d16 Track Extensions
            # in PUBLISH / SUBSCRIBE_OK / FETCH_OK) know where to stop
            # instead of reading past the payload. Other message types
            # accept and ignore it for signature uniformity.
            msg = message_class.deserialize(buf, prof=self._profile, buf_end=end_pos)
            # Length is the message extent: a body that parsed past it
            # consumed the next message's bytes.
            if buf.tell() > end_pos:
                raise MOQTProtocolViolation(
                    f"{message_class.__name__} body runs "
                    f"{buf.tell() - end_pos} bytes past its Length")
            # d18 replies omit the Request ID (demuxed by request stream);
            # inject the stream-bound id so handlers key on it unchanged.
            # A message that carries its own id (REQUEST_UPDATE) keeps it.
            if (request_id is not None and
                    not self._profile.reply_has_request_id and
                    hasattr(msg, 'request_id')):
                if getattr(msg, 'request_id', None) is None:
                    update_id = self._next_reply_owner(request_id, msg)
                    msg.request_id = (request_id if update_id is None
                                      else update_id)
                    if isinstance(msg, RequestOk) and prof.param_table >= 20:
                        self._check_request_ok_scope(msg, request_id,
                                                     update_id)
                elif isinstance(msg, RequestUpdate):
                    # d18 §10.9: the updated request is the stream's;
                    # answer on the same stream under the update's id.
                    if msg.existing_request_id is None:
                        msg.existing_request_id = request_id
                    sid = self._bidi_streams.get(request_id)
                    if sid is not None:
                        self._bidi_streams[int(msg.request_id)] = sid
            # Latch DEFAULT_PUBLISHER_PRIORITY synchronously: data
            # streams drain in the same event batch, and the deferred
            # handler task loses that race (group-0 objects would
            # inherit the library default).
            if isinstance(msg, (SubscribeOk, Publish)):
                self._latch_track_priority(
                    getattr(msg, 'track_alias', None),
                    getattr(msg, 'track_extensions', None))
            # §10.1: a peer's request ids keep one parity (client even,
            # server odd) and strictly increase; wrong parity or a
            # reused id MUST close the session with INVALID_REQUEST_ID.
            # Applies to request openers (never on a bound stream) and to
            # REQUEST_UPDATE, which consumes an id on every draft.
            if (((request_id is None
                    and isinstance(msg, self._REQUEST_OPENERS))
                    or isinstance(msg, RequestUpdate))
                    and getattr(msg, 'request_id', None) is not None):
                rid = int(msg.request_id)
                peer_parity = 1 if self._is_client else 0
                if (rid & 1) != peer_parity:
                    raise MOQTException(
                        SessionCloseCode.INVALID_REQUEST_ID,
                        f"peer request_id {rid} has wrong parity")
                # §10.1 closes on a DUPLICATE id — NOT on arrival order.
                # Each request rides its own bidi stream and QUIC gives no
                # ordering across streams, so the ids of requests a peer
                # issued concurrently (a relay forwarding one SUBSCRIBE per
                # track) legitimately arrive out of order. Only a repeat,
                # or an id so old the window can no longer vouch for it,
                # is a violation.
                floor = rid_floor(self._peer_request_max)
                if rid in self._peer_request_seen or rid < floor:
                    raise MOQTException(
                        SessionCloseCode.INVALID_REQUEST_ID,
                        f"duplicate peer request_id {rid} "
                        f"(max seen {self._peer_request_max})")
                if (self._local_request_max is not None
                        and rid >= self._local_request_max):
                    raise MOQTException(
                        SessionCloseCode.TOO_MANY_REQUESTS,
                        f"peer request_id {rid} beyond our MAX_REQUEST_ID "
                        f"{self._local_request_max}")
                self._peer_request_seen.add(rid)
                if rid > self._peer_request_max:
                    self._peer_request_max = rid
                    floor = rid_floor(rid)
                    if floor > 0:
                        self._peer_request_seen.difference_update(
                            [i for i in self._peer_request_seen if i < floor])
                self._extend_request_credit(rid)
            msg_len += hdr_len
            if end_pos > buf.tell():
                raise MOQTProtocolViolation(
                    f"{message_class.__name__} body ends "
                    f"{end_pos - buf.tell()} bytes short of its Length")
            self._route_reply(msg)
            if (not self._log_quiet and isinstance(msg, (Setup, ClientSetup))
                    and self._quiet_peer(msg)):
                self._log_quiet = True
                self._quiet_token = quiet_session.set(True)
            logger.info(f"MOQT event: control message parsed: {self._msgstr(msg)})")
            refusal = (self._resolve_auth_token(msg)
                       or self._refuse_request(msg))
            routed = refusal or self._track_handler(msg)
            if routed is not None:
                handler = routed

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
        except MOQTException as e:
            # MOQTProtocolViolation and peers like INVALID_REQUEST_ID:
            # close with the exception's own code.
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

    def _next_reply_owner(self, request_id: int, msg) -> Optional[int]:
        """The REQUEST_UPDATE a reply on `request_id`'s stream answers;
        None when it answers the request itself."""
        pending = self._tx_updates.get(request_id)
        if not pending:
            return None
        if not isinstance(msg, (RequestOk, RequestError)):
            if pending[0] is not None or not isinstance(
                    msg, (SubscribeOk, FetchOk)):
                return None
        owner = pending.popleft()
        if not pending:
            del self._tx_updates[request_id]
        return owner

    def _fail_waiting_updates(self, request_id: int, why: str) -> None:
        """The request's stream ended: its unanswered updates fail."""
        for update_id in self._tx_updates.pop(request_id, ()):
            fut = (self._pending_requests.pop(update_id, None)
                   if update_id is not None else None)
            if fut is not None and not fut.done():
                fut.set_exception(MOQTRequestError(
                    error_code=int(RequestErrorCode.INTERNAL_ERROR),
                    reason=f"request stream ended ({why})",
                    retry_interval=0))

    def _check_request_ok_scope(self, msg: RequestOk, request_id: int,
                                update_id: Optional[int]) -> None:
        """d20+: the parameters a REQUEST_OK may carry depend on the
        request it answers (d22 §9.3)."""
        if update_id is not None:
            scope = "RequestOk:RequestUpdate"
        else:
            kind = self._request_kinds.get(request_id)
            if kind is None:
                return
            scope = f"RequestOk:{kind[0].__name__}"
        check_param_scope(msg.parameters or {}, scope, prof=self._profile)

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
                        error_code: int = QuicErrorCode.NO_ERROR,
                        reset_code: int = 0) -> None:
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
        if key and len(key) == 2 and key[0] == 'subgroup':
            alias, group_id, subgroup_id = key[1]
            # §11.4.2: FIN on an END_OF_GROUP-bit stream fixes the
            # group's final object.
            if (error_code == QuicErrorCode.NO_ERROR
                    and state.object_id is not None
                    and getattr(state.parser, 'end_of_group', False)):
                self._set_group_bound(alias, group_id, state.object_id + 1)
            cb = self._stream_end_handlers.get(alias)
            clean = error_code == QuicErrorCode.NO_ERROR
            if cb:
                try:
                    cb(group_id, subgroup_id, clean=clean,
                       reset_code=reset_code)
                except Exception:
                    logger.debug("stream-end handler raised", exc_info=True)
            else:
                self._note_early_stream_end(alias, group_id, subgroup_id,
                                            clean, reset_code)
        if key and len(key) == 2 and key[0] == 'fetch':
            request_id = key[1]
            clean = error_code == QuicErrorCode.NO_ERROR
            if self._profile.fetch_location_filter:
                self._fill_requests.pop(request_id, None)
            self._fetch_object_handlers.pop(request_id, None)
            fut = self._fetch_done_futures.pop(request_id, None)
            if fut and not fut.done():
                fut.set_result(clean)
            self._fetch_done_results[request_id] = clean
            while len(self._fetch_done_results) > self.FETCH_RESULTS_KEPT:
                self._fetch_done_results.popitem(last=False)
        self._unbind_key(key)

    FETCH_RESULTS_KEPT = 256

    # A uni data stream whose header has not parsed within the deadline
    # is abandoned (STOP_SENDING): its bytes and stream credit would
    # otherwise stay pinned for the session's life.
    STREAM_BIND_DEADLINE_S = 5.0
    STREAM_REAPER_PERIOD_S = 1.0
    _reaper_handle = None

    def _schedule_stream_reaper(self) -> None:
        if self._reaper_handle is None:
            self._reaper_handle = self._loop.call_later(
                self.STREAM_REAPER_PERIOD_S, self._reap_streams)

    def _reap_streams(self) -> None:
        """Drop header-less uni streams past the binding deadline; runs
        once a period while any data stream is open."""
        self._reaper_handle = None
        if self._close_err is not None:
            return
        cutoff = time.monotonic() - self.STREAM_BIND_DEADLINE_S
        for sid, state in list(self._data_streams.items()):
            if state.parser is None and state.last_activity < cutoff:
                held = getattr(state.chain, 'capacity', 0)
                self._reject_stream(
                    sid, StreamResetCode.DELIVERY_TIMEOUT,
                    f"no stream header within "
                    f"{self.STREAM_BIND_DEADLINE_S:g}s ({held} bytes held)")
        if self._data_streams:
            self._schedule_stream_reaper()

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
            state = _DataStreamState(chain=StreamChain(),
                                     last_activity=time.monotonic())
            self._data_streams[stream_id] = state
            self._schedule_stream_reaper()

        if data and len(data) > 0:
            state.chain.extend(data)

        if state.chain.capacity > 0:
            self._drain_stream(stream_id, state)

        if end_stream and stream_id in self._data_streams:
            # §11.4: a FIN in the middle of a serialized Object closes
            # the session. Only once the header bound the stream — a
            # truncated header is a stream fault, not a session one.
            residual = state.chain.capacity - state.chain.tell()
            if residual > 0 and state.parser is not None:
                error = (f"FIN mid-object on stream {stream_id}: "
                         f"{residual} residual bytes")
                logger.error(f"MOQT error: {error}")
                self._close_session(SessionCloseCode.PROTOCOL_VIOLATION,
                                    error)
                return
            if residual > 0:
                logger.warning(f"MOQT stream({stream_id}): FIN with "
                               f"{residual} bytes of unparsed header")
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
                self._reject_stream(stream_id, e.error_code, e.reason,
                                    quiet=e.quiet)
                return
            except MOQTUnderflow:
                chain.rollback()
                return
            except BufferReadError:
                chain.rollback()
                return
            except MOQTProtocolViolation as e:
                logger.error(f"MOQT stream({stream_id}): {e.reason_phrase}"
                             f"{getattr(e, 'log_detail', '')}")
                self._close_session(SessionCloseCode.PROTOCOL_VIOLATION,
                                    e.reason_phrase)
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
                    StreamResetCode.INTERNAL_ERROR,
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
                hdr = state.parser
                alias = getattr(hdr, 'track_alias', None)
                if (state.object_id is not None
                        and msg_obj.object_id <= state.object_id):
                    self._malformed_track(
                        alias, f"object {msg_obj.object_id} after "
                        f"{state.object_id} on one subgroup stream",
                        stream_id)
                    return
                state.object_id = msg_obj.object_id
                if msg_obj.status:
                    fault = self._status_object_fault(msg_obj.status,
                                                      msg_obj.extensions)
                    if fault is not None:
                        logger.error(f"MOQT stream({stream_id}): {fault}")
                        self._close_session(
                            SessionCloseCode.PROTOCOL_VIOLATION, fault)
                        return
                if self._group_bound or self._track_bound:
                    reason = self._object_out_of_bounds(
                        alias, state.group_id, msg_obj.object_id,
                        msg_obj.status)
                    if reason is not None:
                        self._malformed_track(alias, reason, stream_id)
                        return
                # END_OF_GROUP / END_OF_TRACK are delivered, not
                # swallowed: they are part of the track's delivery
                # semantics and a relay has to forward them. Consumers
                # that only want media filter on status.
                terminal = msg_obj.status in (
                    ObjectStatus.END_OF_GROUP,
                    ObjectStatus.END_OF_TRACK,
                )
                cb = self._object_cb(alias)
                now = int(time.time() * 1_000_000)
                if getattr(hdr, 'default_priority', False):
                    msg_obj.publisher_priority = (
                        self._track_default_priority.get(
                            alias, MOQT_DEFAULT_PRIORITY))
                else:
                    msg_obj.publisher_priority = getattr(
                        hdr, 'publisher_priority', None)
                msg_obj.stream_flags = (
                    getattr(hdr, 'first_object', False),
                    getattr(hdr, 'end_of_group', False),
                    getattr(hdr, 'default_priority', False),
                )
                if state.subgroup_id is None:
                    # SUBGROUP_ID_FIRST_OBJ: the header carries no
                    # subgroup field and the parser resolves it from
                    # the first object, so the stream's id is known
                    # only now.
                    state.subgroup_id = getattr(
                        state.parser, 'subgroup_id', None)
                if cb:
                    try:
                        cb(msg_obj, consumed, now, state.group_id,
                           state.subgroup_id)
                    except Exception:
                        self._object_cb_raised()
                else:
                    self._hold_early_object(
                        alias, consumed, (msg_obj, consumed, now,
                                          state.group_id, state.subgroup_id))
                if terminal:
                    self._note_object_bound(alias, state.group_id,
                                            msg_obj.object_id, msg_obj.status)
                    self._cleanup_stream(stream_id)
                    return
            elif isinstance(msg_obj, SubgroupHeader):
                if state.group_id is not None:
                    self._malformed_track(
                        getattr(state.parser, 'track_alias', None),
                        f"second subgroup header (group "
                        f"{msg_obj.group_id}) on one stream", stream_id)
                    return
                state.group_id = msg_obj.group_id
                state.subgroup_id = msg_obj.subgroup_id
                state.publisher_priority = msg_obj.publisher_priority
            elif isinstance(msg_obj, FetchHeader):
                pass
            elif isinstance(msg_obj, FetchObject):
                parser = state.parser
                request_id = (
                    parser.request_id
                    if isinstance(parser, FetchHeader)
                    else None
                )
                if msg_obj.end_of_range is None:
                    cb = (self._fetch_object_handlers.get(request_id)
                          or self.on_fetch_object)
                    if cb:
                        now = int(time.time() * 1_000_000)
                        self._call_object_cb(cb, msg_obj, consumed, now,
                                             request_id)
                elif self.on_fetch_gap is not None:
                    self._call_object_cb(self.on_fetch_gap, msg_obj,
                                         request_id)
            else:
                logger.error(
                    f"MOQT stream({stream_id}): unexpected msg "
                    f"{type(msg_obj).__name__} size: {consumed} bytes"
                )

    def _reject_stream(self, stream_id: int, error_code: int, reason: str,
                       quiet: bool = False) -> None:
        """Terminate a uni stream at the MoQT layer via STOP_SENDING.

        Used for admission failures (unknown request_id/track_alias, stream
        reuse, budget exceeded). Stream-level only — never closes the
        session. Removes any binding table entries for the stream.
        """
        (logger.debug if quiet else logger.warning)(
            f"MOQT stream({stream_id}): rejecting: "
            f"{reason} (code=0x{error_code:x})")
        self.stream_stop_sending(stream_id, error_code)
        self._cleanup_stream(stream_id, QuicErrorCode.APPLICATION_ERROR,
                             reset_code=int(error_code))

    def _malformed_track(self, alias: Optional[int], reason: str,
                         stream_id: Optional[int] = None) -> None:
        """§2.4.2: reset the offending stream (and every other open
        stream of the track) with MALFORMED_TRACK, cancel the
        subscription, refuse the track's later streams; the session
        stays up. Stream-end handlers see reset_code MALFORMED_TRACK."""
        logger.error(f"MOQT: malformed track alias={alias}: {reason}")
        if stream_id is not None:
            self._reject_stream(stream_id, StreamResetCode.MALFORMED_TRACK,
                                reason)
        if alias is None or alias in self._malformed_aliases:
            return
        self._malformed_aliases.add(alias)
        for key, sid in list(self._subgroup_stream_by_key.items()):
            if key[0] == alias:
                self._reject_stream(sid, StreamResetCode.MALFORMED_TRACK,
                                    reason)
        request_id = self._track_aliases.get(alias)
        if request_id is not None:
            try:
                self.unsubscribe(request_id)
            except Exception:
                logger.debug("malformed-track cancel failed", exc_info=True)

    def _object_out_of_bounds(self, alias: int, group_id: int,
                              object_id: int, status) -> Optional[str]:
        """§2.4.2 items 4/5: an object at or past a known end of group /
        end of track (§11.2.1.1). The terminal object itself may arrive
        again."""
        groups = self._group_bound.get(alias)
        if groups is not None:
            bound = groups.get(group_id)
            if (bound is not None and object_id >= bound
                    and not (object_id == bound
                             and status == ObjectStatus.END_OF_GROUP)):
                return (f"object {group_id}.{object_id} at or past end "
                        f"of group ({bound})")
        end = self._track_bound.get(alias)
        if (end is not None and (group_id, object_id) >= end
                and not ((group_id, object_id) == end
                         and status == ObjectStatus.END_OF_TRACK)):
            return (f"object {group_id}.{object_id} at or past end "
                    f"of track {end[0]}.{end[1]}")
        return None

    def _note_object_bound(self, alias: int, group_id: int, object_id: int,
                           status, end_of_group_bit: bool = False) -> None:
        """Record the end of group / track an object announces."""
        if status == ObjectStatus.END_OF_GROUP:
            self._set_group_bound(alias, group_id, object_id)
        elif status == ObjectStatus.END_OF_TRACK:
            self._track_bound[alias] = (group_id, object_id)
        elif end_of_group_bit:
            self._set_group_bound(alias, group_id, object_id + 1)

    GROUP_BOUNDS_PER_TRACK = 256

    def _set_group_bound(self, alias: int, group_id: int, bound: int) -> None:
        groups = self._group_bound.get(alias)
        if groups is None:
            groups = self._group_bound[alias] = {}
        elif (group_id not in groups
              and len(groups) >= self.GROUP_BOUNDS_PER_TRACK):
            del groups[next(iter(groups))]
        prev = groups.get(group_id)
        groups[group_id] = bound if prev is None else min(prev, bound)

    def _status_object_fault(self, status, extensions) -> Optional[str]:
        """§11.2.1.1/§11.2.1.2: a non-Normal status must be one the
        draft defines and carries no properties; either fault closes
        the session."""
        if status not in self._profile.object_statuses:
            return (f"object status 0x{int(status):x} undefined at "
                    f"draft-{self._profile.draft}")
        if extensions:
            return "properties on a status object"
        return None

    def _forget_track_bounds(self, alias: int) -> None:
        self._group_bound.pop(alias, None)
        self._track_bound.pop(alias, None)
        self._malformed_aliases.discard(alias)

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
        fill = (self._fill_requests.get(request_id)
                if self._profile.fetch_location_filter else None)
        if fill is not None:
            # A fill fetch stream (d20 §5.1.3): ours to admit once.
            header._group_order = fill[2]
            outstanding = None
            is_fetch = True
        else:
            outstanding = self._subscriptions.get(request_id)
            is_fetch = (outstanding is not None
                        and any(isinstance(m, Fetch) for m in outstanding))
        # Group-delta decode direction: pre-d18 the FETCH_OK carries a
        # GROUP_ORDER param; at d18 it cannot (§10.2.8), so the
        # direction is the one we requested in our own FETCH.
        for m in (outstanding or ()):
            order = getattr(m, 'group_order', None)
            if type(m).__name__ == 'FetchOk' and order:
                header._group_order = int(order)
                break
        else:
            for m in (outstanding or ()):
                if isinstance(m, Fetch) and getattr(m, 'group_order', None):
                    header._group_order = int(m.group_order)
        if not is_fetch:
            raise MOQTStreamReject(
                StreamResetCode.CANCELLED,
                f"fetch stream for unknown request_id={request_id}")
        if request_id in self._fetch_stream_by_request:
            existing = self._fetch_stream_by_request[request_id]
            if existing != stream_id:
                raise MOQTStreamReject(
                    StreamResetCode.CANCELLED,
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
        if header.track_alias in self._malformed_aliases:
            raise MOQTStreamReject(StreamResetCode.MALFORMED_TRACK,
                                   "malformed track")
        if header.track_alias not in self._track_aliases:
            # Data before the control message that binds the alias is
            # legal (§10.4.2), so this alone is not an error — it is
            # only a defect if the alias NEVER binds. Record and stay
            # quiet; _check_unbound_aliases escalates on the failure
            # shape (still unbound after a grace period AND more
            # streams than a single race could explain).
            st = self._unbound_aliases.setdefault(
                header.track_alias, [time.monotonic(), 0])
            st[1] += 1
            logger.debug(
                f"MOQT stream({stream_id}): subgroup with "
                f"unknown track_alias={header.track_alias} "
                f"(stream {st[1]} under this alias; "
                f"awaiting control message)")
            self._check_unbound_aliases(header.track_alias)
        # subgroup_id is None in FIRST_OBJ mode — resolved on first object
        if header.subgroup_id is not None:
            self._bind_subgroup_stream(stream_id, header)

    # An unbound alias is normal for the moment between the first data
    # stream and the SUBSCRIBE_OK that binds it. It is a DEFECT when it
    # outlives both: more streams than one race can produce, and longer
    # than a control round trip. Either alone is inconclusive.
    UNBOUND_ALIAS_GRACE_S = 5.0
    UNBOUND_ALIAS_MAX_STREAMS = 4

    def _check_unbound_aliases(self, alias: int) -> None:
        """Escalate once per alias when it is provably stuck."""
        st = self._unbound_aliases.get(alias)
        if st is None or alias in self._unbound_escalated:
            return
        first_seen, streams = st
        age = time.monotonic() - first_seen
        if (streams >= self.UNBOUND_ALIAS_MAX_STREAMS
                and age >= self.UNBOUND_ALIAS_GRACE_S):
            self._unbound_escalated.add(alias)
            logger.warning(
                f"MOQT: track_alias={alias} still unbound after "
                f"{streams} data streams over {age:.1f}s — the peer is "
                f"sending under an alias no control message assigned. "
                f"Objects still deliver (delivery does not consult the "
                f"registry) but per-track routing cannot work. Check "
                f"that SUBSCRIBE_OK/PUBLISH carried this alias.")

    def _resolve_unbound_alias(self, alias: int) -> None:
        """Called when a control message binds an alias — clears the
        pending record and reports how long the race lasted."""
        st = self._unbound_aliases.pop(alias, None)
        self._unbound_escalated.discard(alias)
        if st is not None:
            first_seen, streams = st
            logger.debug(
                f"MOQT: track_alias={alias} bound after "
                f"{(time.monotonic() - first_seen) * 1000:.0f}ms "
                f"and {streams} early stream(s) — data/control race "
                f"resolved")

    def _bind_subgroup_first_obj(self, stream_id: int,
                                  header: 'SubgroupHeader') -> None:
        """Complete FIRST_OBJ-mode subgroup binding once subgroup_id is
        resolved from the first object."""
        self._bind_subgroup_stream(stream_id, header)

    def _bind_subgroup_stream(self, stream_id: int,
                              header: 'SubgroupHeader') -> None:
        """Bind (alias, group, subgroup) to this stream. A subgroup
        reopened after a reset (§2.2) takes the key from the old stream,
        whose later end then neither unbinds it nor reports the subgroup
        ended."""
        key = (header.track_alias, header.group_id, header.subgroup_id)
        existing = self._subgroup_stream_by_key.get(key)
        if existing is not None and existing != stream_id:
            logger.debug(f"MOQT stream({stream_id}): reopens {key}, "
                         f"superseding stream {existing}")
            old = self._data_streams.get(existing)
            if old is not None:
                old.key = None
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
                # SUBGROUP_HEADER is 0x10 plus the flag bits the draft
                # defines; reserved subgroup_id_mode 0b11 is invalid.
                mask = self._profile.subgroup_type_mask
                is_subgroup = (
                    (stream_type & ~mask) == 0x10
                    and ((stream_type >> 1) & 0x03) != 3
                )
                if stream_type in (PADDING_STREAM_TYPE,):
                    # d18 PADDING stream: drain + discard (MUST keep flow
                    # control moving). No parser bound; bytes ignored.
                    data_type = "PADDING"
                    msg_header = None
                    self._padding_rx += 1
                    raise MOQTStreamReject(
                        StreamResetCode.CANCELLED, "padding stream drained",
                        quiet=True)
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
                    raise self._unknown_stream_type(buf, pos, stream_type)

                if msg_header is None:
                    # Parse failure on the data stream type byte. Reject
                    # this one stream instead of the session: the publisher
                    # sees STOP_SENDING and abandons, other streams continue.
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
                        StreamResetCode.INTERNAL_ERROR,
                        f"stream-type parse failed: {data_type}",
                    )

                # record that the data stream header has been processed
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f"MOQT stream({stream_id}): {msg_header} consumed: "
                                 f"{buf.tell() - pos} bytes")
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
                        # Constructed, not __new__: a slots dataclass
                        # leaves unset fields unreadable, and callbacks
                        # may print or forward the object.
                        obj = ObjectHeader(object_id=0)
                        sg_header._obj_cache = obj
                    obj.deserialize_into(
                        buf, len,
                        extensions_present=sg_header.extensions_present,
                        prev_object_id=sg_header._last_object_id,
                        vi64=sg_header._vi64,
                        kvp_delta=sg_header._kvp_delta,
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
                    msg_header = FetchObject.deserialize(
                        buf, prior=fh._prior_obj, prof=self._profile,
                        group_order=fh._group_order)
                    # §11.4.4.2: a marker becomes the prior for the
                    # Group/Object deltas; its subgroup and priority
                    # stay those of the last actual object.
                    if (msg_header.end_of_range is not None
                            and fh._prior_obj is not None):
                        msg_header.subgroup_id = fh._prior_obj.subgroup_id
                        msg_header.publisher_priority = \
                            fh._prior_obj.publisher_priority
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

    @staticmethod
    def _dgram_logstr(msg, consumed: int, now: int) -> str:
        msg_ts = (msg.extensions.get(MOQT_TIMESTAMP_EXT)
                  if msg.extensions else None)
        delay = f"delay: {now - msg_ts} ms" if msg_ts else ""
        return f"{msg.group_id}.{msg.object_id} size: {consumed} bytes {delay}"

    def _moqt_handle_data_dgram(self, buf: Buffer) -> MOQTMessageType:
        """Process incoming datagram messages."""
        if buf.capacity == 0 or buf.tell() >= buf.capacity:
            logger.error(f"MOQT datagram: no data {buf.tell()}")
            return
        debug = logger.isEnabledFor(logging.DEBUG)
        if debug:
            logger.debug(f"MOQT handle datagram: 0x{buf.data_slice(0,min(buf.capacity,12))}")
        # Get datagram type from first varint (vi64 for d18).
        pos = buf.tell()
        prof = self._profile
        buf.vi64 = prof.vi64
        dgram_type = buf.pull_vint()
        if prof.merged_datagram_layout:
            return self._moqt_handle_data_dgram_d18(buf, pos, dgram_type)
        # d14: ObjectDatagram types 0x00-0x07.
        _dgram_max = 0x07
        if 0x00 <= dgram_type <= _dgram_max:
            msg = ObjectDatagram.deserialize(buf, buf.capacity, type_val=dgram_type, prof=self._profile)
            if msg is None:
                error = f"datagram parsing failed at: {buf.tell()}"
                logger.error(f"MOQT error: " + error)
                self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
                return msg

            consumed = buf.tell() - pos
            now = int(time.time() * 1_000_000)
            if debug:
                logger.debug(f"MOQT event: ObjectDatagram: "
                             f"{self._dgram_logstr(msg, consumed, now)}")
            self._deliver_datagram(msg, consumed, now)
            return msg
        # Draft-14: ObjectDatagramStatus types 0x20-0x21 (status datagrams)
        elif 0x20 <= dgram_type <= 0x21:
            msg = ObjectDatagramStatus.deserialize(buf, type_val=dgram_type, prof=self._profile)
            if msg is None:
                error = f"datagram parsing failed at: {buf.tell()}"
                logger.error(f"MOQT error: " + error)
                self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
                return msg

            consumed = buf.tell() - pos
            now = int(time.time() * 1_000_000)
            if debug:
                logger.debug(f"MOQT event: ObjectDatagramStatus: "
                             f"{self._dgram_logstr(msg, consumed, now)}")
            self._deliver_datagram(msg, consumed, now)
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
        """d16+/d18 merged datagram dispatch. OBJECT_DATAGRAM form
        0b00X0XXXX (0x00-0x0F / 0x20-0x2F minus STATUS+END_OF_GROUP);
        PADDING datagram 0x132B3E29 (d18 only) is drained."""
        if self._profile.vi64 and dgram_type == PADDING_DATAGRAM_TYPE:
            logger.debug("MOQT event: PADDING datagram drained")
            self._padding_rx += 1
            return
        valid_form = (dgram_type & ~0x2F) == 0 and (dgram_type & 0x10) == 0
        if (not valid_form) or dgram_type in self._D18_DGRAM_INVALID:
            error = f"invalid d18 datagram type: 0x{dgram_type:x}"
            logger.error(f"MOQT error: " + error)
            self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
            return
        try:
            msg = ObjectDatagram.deserialize(
                buf, buf.capacity, type_val=dgram_type, prof=self._profile)
        except (ValueError, MOQTProtocolViolation) as e:
            msg, error = None, f"datagram: {e}"
        else:
            error = (f"datagram parsing failed at: {buf.tell()}"
                     if msg is None else None)
        if error is None and (dgram_type & 0x01) and not msg.extensions:
            error = "datagram PROPERTIES bit with no properties"
        if error is None and msg.status:
            error = self._status_object_fault(msg.status, msg.extensions)
        if error is not None:
            logger.error(f"MOQT error: " + error)
            self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
            return None
        if dgram_type & 0x08:
            # DEFAULT_PRIORITY: inherit the latched track default, not
            # the library constant (§11.3.1).
            msg.publisher_priority = self._track_default_priority.get(
                msg.track_alias, MOQT_DEFAULT_PRIORITY)
        consumed = buf.tell() - pos
        now = int(time.time() * 1_000_000)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"MOQT event: d18 ObjectDatagram: {msg.group_id}.{msg.object_id} "
                f"size: {consumed} bytes status: {msg.status}")
        self._deliver_datagram(msg, consumed, now)
        return msg

    def _deliver_datagram(self, msg, consumed: int, now: int) -> None:
        """Bounds-check and deliver one datagram object. Status datagrams
        (END_OF_GROUP / END_OF_TRACK) reach the consumer like their
        subgroup-stream counterparts; consumers filter on msg.status."""
        alias = msg.track_alias
        if alias in self._malformed_aliases:
            return
        status = getattr(msg, 'status', ObjectStatus.NORMAL)
        if self._group_bound or self._track_bound:
            reason = self._object_out_of_bounds(
                alias, msg.group_id, msg.object_id, status)
            if reason is not None:
                self._malformed_track(alias, reason)
                return
        cb = self._object_cb(alias)
        if cb:
            try:
                cb(msg, consumed, now, msg.group_id, None)
            except Exception:
                self._object_cb_raised()
        else:
            self._hold_early_object(alias, consumed,
                                    (msg, consumed, now, msg.group_id, None))
        eog = getattr(msg, 'end_of_group', False)
        if eog or status != ObjectStatus.NORMAL:
            self._note_object_bound(alias, msg.group_id, msg.object_id,
                                    status, eog)

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
                    elif (is_request_bidi
                          and self._profile.truncated_request_closes):
                        self._close_session(
                            SessionCloseCode.PROTOCOL_VIOLATION,
                            f"request stream {stream_id} FIN with "
                            f"truncated message")
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
                logger.error(
                    f"MOQT error: {error} head_hex="
                    f"{chain.data_slice(0, min(64, chain.capacity)).hex()}")
                self._close_session(
                    SessionCloseCode.PROTOCOL_VIOLATION, error)
                return
            chain.commit()
            if msg is self._MSG_SKIPPED:
                continue
            # §3.3: a request bidi stream MUST open with one of the
            # request types; the first opener binds the stream to its
            # Request ID and later replies demux by that binding.
            if (is_request_bidi and
                    self._bidi_stream_requests.get(stream_id) is None):
                if not (isinstance(msg, self._REQUEST_OPENERS) and
                        getattr(msg, 'request_id', None) is not None):
                    self._close_session(
                        SessionCloseCode.PROTOCOL_VIOLATION,
                        f"request stream {stream_id} opened with "
                        f"{type(msg).__name__}, not a request")
                    return
                self._bidi_stream_requests[stream_id] = msg.request_id
                self._bidi_streams[msg.request_id] = stream_id
            # d18 §10 Table 5: only SETUP and GOAWAY ride the control
            # stream; a request there cannot own a reply stream, and
            # echoing a reply onto our control stream would mirror the
            # peer's violation.
            if (not is_request_bidi and self._profile.control_uni_pair
                    and not isinstance(msg, (Setup, GoAway))):
                self._close_session(
                    SessionCloseCode.PROTOCOL_VIOLATION,
                    f"{type(msg).__name__} on the d18 control stream")
                return
            if isinstance(msg, GoAway):
                if stream_id in self._peer_goaway_streams:
                    self._close_session(
                        SessionCloseCode.PROTOCOL_VIOLATION,
                        f"second GOAWAY on stream {stream_id}")
                    return
                self._peer_goaway_streams.add(stream_id)
                # §10.4: the control GOAWAY names one of the receiver's
                # own Request IDs, so it carries the receiver's parity.
                own_parity = 0 if self._is_client else 1
                if (not is_request_bidi and msg.request_id is not None
                        and int(msg.request_id) & 1 != own_parity):
                    self._close_session(
                        SessionCloseCode.INVALID_REQUEST_ID,
                        f"GOAWAY Request ID {msg.request_id} has the "
                        f"wrong parity")
                    return
                if is_request_bidi:
                    self._report_goaway(
                        msg, self._bidi_stream_requests.get(stream_id))
                else:
                    self.goaway_received = msg
                    self._report_goaway(msg, None)
        if end_stream:
            self._control_chains.pop(stream_id, None)
            # §3.3: the control stream is never closed while the session
            # lives.
            if not is_request_bidi and stream_id == self._control_read_stream_id:
                self._close_session(
                    SessionCloseCode.PROTOCOL_VIOLATION,
                    f"peer closed its control stream {stream_id}")
                return
            ours = self._bidi_stream_requests.get(stream_id)
            if is_request_bidi and ours in self._tx_updates:
                self._loop.call_soon(self._fail_waiting_updates, ours,
                                     f"FIN (stream {stream_id})")
            if stream_id in self._cancelled_request_streams:
                self._cancelled_request_streams.discard(stream_id)
                request_id = self._bidi_stream_requests.pop(stream_id, None)
                if request_id is not None:
                    self._loop.call_soon(
                        self._settle_cancelled_request, request_id,
                        f"FIN after STOP_SENDING (stream {stream_id})")
            held = self._peer_requests.get(
                self._bidi_stream_requests.get(stream_id))
            if (held is not None
                    and self._profile.request_fin_cancels_discovery
                    and held[0] in (SubscribeNamespace, SubscribeTracks)):
                # §6.1: a FIN cancels these. Free the prefix now; close the
                # stream a loop turn later, after replies already due.
                request_id = self._bidi_stream_requests.pop(stream_id)
                del self._peer_requests[request_id]
                self._loop.call_soon(self._end_cancelled_request,
                                     request_id, stream_id)

    def _report_goaway(self, msg: 'GoAway',
                       request_id: Optional[int]) -> None:
        cb = self.on_goaway
        if cb is None:
            return
        try:
            result = cb(msg, request_id)
            if asyncio.iscoroutine(result):
                task = asyncio.ensure_future(result)
                self._tasks.add(task)
                task.add_done_callback(self._control_task_done)
        except Exception:
            logger.debug("on_goaway raised", exc_info=True)

    def _end_cancelled_request(self, request_id: int, stream_id: int) -> None:
        """Tear down a request the peer cancelled with FIN and FIN our
        side, unless a reply has already closed it."""
        if self._bidi_streams.get(request_id) != stream_id:
            return
        self._settle_cancelled_request(request_id, f"FIN (stream {stream_id})")
        self.stream_fin(stream_id)

    def _ingest_stream_data(self, stream_id: int, data, end_stream: bool) -> None:
        """Route stream bytes, or hold them while a raw-QUIC session's
        draft is still unsettled (WT sessions settle at session setup or
        negotiate in-band, so they never wait)."""
        if self._draft_settled or self._is_wt:
            self._route_stream_data(stream_id, data, end_stream)
            return
        held = self._pre_draft_stream_data.get(stream_id)
        buf = held[0] if held is not None else bytearray()
        buf.extend(data)
        self._pre_draft_stream_data[stream_id] = (
            buf, (held[1] if held is not None else False) or end_stream)

    def _settle_draft(self, draft: int) -> None:
        """Lock the negotiated draft and replay stream bytes that arrived
        before it was known."""
        self.negotiated_draft = draft
        self._draft_settled = True
        pending = self._pre_draft_stream_data
        self._pre_draft_stream_data = {}
        for stream_id, (data, end_stream) in pending.items():
            self._route_stream_data(stream_id, bytes(data), end_stream)

    def _route_stream_data(self, stream_id: int, data, end_stream: bool) -> None:
        """Route one stream's bytes by stream direction and draft."""
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
                    self._classify_d18_uni(stream_id, data, end_stream)
                    return
                if stream_id == self._d18_control_read_sid:
                    self._on_control_data(stream_id, data, end_stream)
                    return
            self._on_stream_data(stream_id, data, end_stream)
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
            self._on_control_data(stream_id, data, end_stream,
                                  is_request_bidi=True)
            return

        if self._control_stream_id is None:
            self._control_stream_id = stream_id
            self._prioritise_control_stream(stream_id)
            logger.debug(f"QUIC event: detecting control stream: {stream_id}")
        elif stream_id != self._control_stream_id:
            if is_draft16_or_later(self.negotiated_draft):
                self._on_control_data(stream_id, data, end_stream,
                                      is_request_bidi=True)
                return
            logger.warning(f"MOQT event: unrecognized bidirectional stream({stream_id})")
            return

        if stream_id == self._control_read_stream_id:
            self._on_control_data(stream_id, data, end_stream)

    def _unknown_stream_type(self, buf, pos: int,
                             stream_type: int) -> MOQTProtocolViolation:
        """The close for an unknown uni stream type. A WT server with no
        WT-Protocol runs the d14 fallback, so a d18+ SETUP lands here; name
        the missing offer instead of the misparsed type, and the peer in
        log_detail, for the log line only."""
        if (self._is_wt and not self._is_client
                and not self._profile.control_uni_pair
                and not getattr(self, 'negotiated_protocol', None)
                and self._d18_peek_is_setup(
                    buf.data_slice(pos, min(pos + 9, buf.capacity)))):
            err = MOQTProtocolViolation(
                "SETUP on a uni stream with no WT-Protocol: offer "
                "moqt-NN in WT-Available-Protocols")
            try:
                addr = self.peer_address
            except Exception:
                addr = None
            if addr:
                host = f"[{addr[0]}]" if ":" in addr[0] else addr[0]
                err.log_detail = f" (peer {host}:{addr[1]})"
            return err
        return MOQTProtocolViolation(
            f"unknown data stream type 0x{stream_type:x}")

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
        """Handle incoming QUIC events, under the session's log scope."""
        if not self._log_quiet:
            try:
                self._quic_event(event)
            finally:
                if self._quiet_token is not None:
                    quiet_session.reset(self._quiet_token)
                    self._quiet_token = None
            return
        token = quiet_session.set(True)
        try:
            self._quic_event(event)
        finally:
            quiet_session.reset(token)

    def _quic_event(self, event: QuicEvent) -> None:

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
            self._close_session(error, reason, transmit=False)
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
                    logger.debug(f"MOQT: version set from ALPN: {alpn} -> draft-{draft}")
                    self._settle_draft(draft)
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
                    stream_id == self._session_id):
                self._close_session(
                    SessionCloseCode.INTERNAL_ERROR,
                    f"critical stream closed by remote peer: {stream_id}"
                )
                return

            self._ingest_stream_data(stream_id, data, event.end_stream)

        elif isinstance(event, DatagramFrameReceived) and self._wt_session_setup.done():
            msg_buf = Buffer(data=event.data)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"MOQT event: DatagramFrameReceived: 0x{msg_buf.data_slice(0,min(msg_buf.capacity,16)).hex()}")
            # A datagram is a self-contained, unreliable message: one we
            # cannot parse is a dropped object, never a dead session.
            # Letting the parse raise here propagated out of the asyncio
            # callback and killed the whole connection on a single bad
            # frame. Count, log the first one with its bytes for
            # diagnosis, and keep receiving.
            try:
                self._moqt_handle_data_dgram(msg_buf)
            except Exception as e:
                self._dgram_parse_errors += 1
                if self._dgram_parse_errors == 1:
                    logger.warning(
                        f"MOQT: datagram parse failed ({type(e).__name__}: "
                        f"{e}) — object dropped, session continues. "
                        f"first 32B: "
                        f"0x{msg_buf.data_slice(0, min(msg_buf.capacity, 32)).hex()}")
                else:
                    logger.debug(
                        f"MOQT: datagram parse failed "
                        f"(#{self._dgram_parse_errors}): {e}")
            return
        elif isinstance(event, StopSendingReceived):
            logger.debug(f"MOQT event: StopSendingReceived: stream {event.stream_id}")
            # RFC 9000: STOP_SENDING from peer → reciprocal RESET_STREAM.
            self.stream_reset(event.stream_id, event.error_code)
            # A request stream keeps its read chain: the peer may still send.
            bound = event.stream_id in self._bidi_stream_requests
            self._on_request_stream_terminated(
                event.stream_id,
                stop_sending_code=int(event.error_code or 0))
            if not bound:
                self._cleanup_stream(
                    event.stream_id, QuicErrorCode.APPLICATION_ERROR)
            return
        elif isinstance(event, StreamReset):
            logger.debug(f"MOQT event: StreamReset: stream {event.stream_id}")
            self._on_request_stream_terminated(event.stream_id)
            self._cleanup_stream(
                event.stream_id, QuicErrorCode.APPLICATION_ERROR,
                reset_code=int(event.error_code or 0))
            return

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"QUIC event: event not handled({class_name(event)})")

    def _fail_pending(self, reason: str) -> None:
        """A closed session answers nothing more: fail every awaited
        request and end every awaited fetch as unclean."""
        for fut in self._pending_requests.values():
            if not fut.done():
                fut.set_exception(MOQTRequestError(
                    error_code=int(RequestErrorCode.INTERNAL_ERROR),
                    reason=f"session closed: {reason}", retry_interval=0))
                fut.exception()  # marks it retrieved if nothing awaits it
        for fut in self._fetch_done_futures.values():
            if not fut.done():
                fut.set_result(False)

    def _close_session(self,
              error_code: SessionCloseCode = SessionCloseCode.NO_ERROR,
              reason_phrase: str = "no error", *,
              transmit: bool = True) -> None:
        """Close the MoQT session. The first close's code is the one sent;
        transmit=False when the peer already closed the connection."""
        if error_code == SessionCloseCode.NO_ERROR:
            logger.info(f"MOQT: closing: {reason_phrase} ({error_code})")
        else:
            logger.error(f"MOQT error: closing: {reason_phrase} ({error_code})")
        first = self._close_err is None
        if first:
            self._close_err = (error_code, reason_phrase)
            # Close before failing waiters: an app that exits on the
            # failure stops the transport.
            if transmit:
                try:
                    self.close()
                except Exception:
                    logger.debug("MOQT: close failed", exc_info=True)
            self._fail_pending(reason_phrase)
        if self._reaper_handle is not None:
            self._reaper_handle.cancel()
            self._reaper_handle = None

        # Tombstone every stream_id so any in-flight chunks landing
        # after this point are dropped rather than recreating state.
        for stream_id in list(self._data_streams.keys()):
            self._mark_stream_torn_down(stream_id)
        self._data_streams.clear()
        self._control_chains.clear()
        self._pending_control_msgs.clear()
        self._uni_peek_stash.clear()
        self._published_tracks.clear()
        self._requests.clear()
        self._early_stream_ends.clear()
        self._reset_early_objects()

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

        # A graceful close FINs the data streams we write (end_stream on a
        # peer-initiated stream is a RESET_STREAM), never the control stream
        # (§3.3). An error close sends no FINs.
        is_client = self._is_client
        graceful = int(error_code) == SessionCloseCode.NO_ERROR
        try:
            if self._control_write_stream_id is not None:
                self._control_stream_id = None
                self._d18_control_write_sid = None
            for stream_id in list(self._data_streams.keys() if graceful else ()):
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
        # The MoQT code rides CONNECTION_CLOSE (raw QUIC) or the WebTransport
        # session close. A graceful close waits a tick so its FINs leave
        # first (batched, the peer resets them); an error close goes at once.
        if self._is_wt:
            reason = (reason_phrase if isinstance(reason_phrase, bytes)
                      else str(reason_phrase).encode())
            parent_close = functools.partial(
                super().close, int(error_code), reason)
        else:
            parent_close = functools.partial(
                self._quic.close, error_code=int(error_code))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and graceful:
            loop.call_soon(parent_close)
        else:
            parent_close()

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

        self._check_setup_location(parameters)
        use_quic = not self._is_wt
        _t0 = time.monotonic()
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
                self._prioritise_control_stream(
                    self._d18_control_write_sid)
                logger.debug(
                    f"MOQT: d18 control write-uni: "
                    f"{self._d18_control_write_sid}")
            else:
                self._control_stream_id = (
                    self._quic.get_next_available_stream_id(
                        is_unidirectional=False))
                self._prioritise_control_stream(self._control_stream_id)
                logger.debug(
                    f"MOQT: QUIC control stream created stream id: "
                    f"{self._control_stream_id}")
            params[SetupParamType.PATH] = moqt_setup_path(self._session.path)
            params[SetupParamType.AUTHORITY] = (
                f"{self._session.host}:{self._session.port}")
        else:
            # WebTransport over aiopquic. The WT session is open by the
            # time client_session_init runs (open() awaited already);
            # _wt_session_setup is resolved in _moqt_wt_finalize. The draft
            # is the server's WT-Protocol choice from our offer; with none,
            # a d14 offer (versions in SETUP) proceeds. Anything else closes
            # with WT_ALPN_ERROR (webtrans-http3 §3.3), pins included.
            drafts = list(getattr(self._session, 'supported_drafts', None)
                          or ())
            offered = [f"moqt-{d}" for d in drafts
                       if not profile_for(d).setup_carries_versions]
            in_setup = [d for d in drafts
                        if profile_for(d).setup_carries_versions]
            negotiated = self.negotiated_protocol
            if negotiated in offered:
                self._settle_draft(get_major_version(
                    moqt_version_from_alpn(negotiated)))
                logger.debug(
                    f"MOQT: version set from WT-Protocol: "
                    f"{negotiated} -> draft-{self.negotiated_draft}")
            elif in_setup and (negotiated is None or not offered):
                self._settle_draft(get_major_version(in_setup[0]))
            else:
                reason = (f"WT-Protocol {negotiated!r} not in offer "
                          f"{offered}")
                self._close_session(WT_ALPN_ERROR, reason)
                raise MOQTNegotiationError(reason)
            if self._profile.control_uni_pair:
                # d18 over WT: control is a uni-stream pair, same as raw
                # QUIC — open our write-control uni (WT create_stream); the
                # peer's read-control uni is bound on the first SETUP it
                # sends (the demux in quic_event_received is transport-
                # agnostic). The data plane already proves WT uni streams.
                self._d18_control_write_sid = await self.open_uni_stream()
                self._prioritise_control_stream(
                    self._d18_control_write_sid)
                logger.debug(
                    f"MOQT: d18 WT control write-uni: "
                    f"{self._d18_control_write_sid}")
            else:
                self._control_stream_id = await self.open_bidi_stream()
                self._prioritise_control_stream(self._control_stream_id)
                logger.debug(
                    f"MOQT: WT control stream created stream id: "
                    f"{self._control_stream_id}")

        # Send SETUP. d18 SETUP is symmetric (Setup, type 0x2F00): no
        # version array (negotiated via ALPN), no MAX_REQUEST_ID Setup
        # Option (removed), count-less Setup Options. Pre-d18 sends
        # CLIENT_SETUP with the version list.
        params.update(self._implementation_option())
        if parameters:
            params.update(parameters)  # caller SETUP options (e.g. AUTH_TOKEN)
        self._auth_token_cache_max = int(
            params.get(SetupParamType.MAX_AUTH_TOKEN_CACHE_SIZE, 0))
        if self._profile.control_uni_pair:
            self.send_control_message(self._setup_message(params))
        else:
            self._local_request_max = self.REQUEST_ID_WINDOW
            params[SetupParamType.MAX_REQUEST_ID] = self._local_request_max
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
        
        self._established_ms = (time.monotonic() - _t0) * 1000.0
        self._log_setup_complete()

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
        if self._tx_deferred and (stream_id, 'send') in self._tx_deferred:
            return  # the stream's reset or FIN is queued
        if end_stream:
            if self._served_fetch_streams:
                self._forget_served_fetch(stream_id)
            self._tx_op(stream_id, 'send', 'fin', data)
            return
        try:
            self._quic.send_stream_data(stream_id, data)
        except (AssertionError, AttributeError, BufferError,
                WebTransportError) as e:
            logger.debug(f"stream({stream_id}): write race: {e}")

    def _prioritise_control_stream(self, stream_id: Optional[int]) -> None:
        """Put a control stream in its own band, above subscription data.

        Scheduling is strict, so a greedy data track sharing or undercutting
        the control band starves it: no SUBSCRIBE_OK, no PUBLISH_DONE, the
        session wedges under load. to_stream_priority() floors declared data
        above this band so that cannot happen.
        """
        if stream_id is None:
            return
        self.set_stream_priority(stream_id, STREAM_BAND_CONTROL)

    def _transport_priority_setter(self):
        """The transport's set_stream_priority, or None.

        On WebTransport `self._quic` is the session itself, so looking the
        name up there finds this method and recurses. The transport API
        lives further along the MRO, past this mixin, on aiopquic's
        WebTransportSession. Raw QUIC keeps a real connection in _quic and
        is looked up there.
        """
        if self._quic is not self:
            return getattr(self._quic, 'set_stream_priority', None)
        return getattr(super(), 'set_stream_priority', None)

    def set_stream_priority(self, stream_id: int, priority: int) -> bool:
        """Set the transport send priority for one stream.

        Advisory, and deliberately so: RFC 9000 §2.3 gives QUIC no wire
        mechanism for priority and asks only that an implementation offer
        an API. A transport without one, or a full TX event ring, leaves
        the stream at the default rather than failing the write.

        Posts without waking the worker — the header write that follows
        an open carries it — so the cost is one ring entry per stream.

        True only when the transport accepted the post. A full event ring
        returns 1, meaning the stream keeps its current priority, and is
        reported as False.
        """
        setter = self._transport_priority_setter()
        if setter is None:  # transport offers no priority API
            return False
        try:
            return setter(stream_id, priority) == 0
        except Exception as e:
            logger.debug(f"stream({stream_id}): priority not applied: {e}")
            return False

    def set_default_stream_priority(self, priority: int) -> bool:
        """Priority that newly created streams start at.

        Writes the QUIC **context** default, shared by every connection on
        this transport, and applies only to streams created after the
        call — it is reached through a session but is not scoped to one.
        Needed because picoquic's default (9) and MoQT's neutral publisher
        priority (128) are different points on the same scale, so an
        undeclared stream would otherwise outrank a track that declared
        the neutral value.
        """
        # On WT _quic is self; look past this mixin, as for set_stream_priority.
        target = super() if self._quic is self else self._quic
        setter = getattr(target, 'set_default_stream_priority', None)
        if setter is None:
            return False
        try:
            setter(priority)
        except Exception as e:
            logger.debug(f"default priority not applied: {e}")
            return False
        return True

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
        except ConnectionError as e:
            # Stream or session torn down under the write: end the
            # producer as the pre-check does, never return silently.
            logger.debug(f"stream({stream_id}): write after teardown: {e}")
            raise asyncio.CancelledError() from e

    # Fetch streams serve_fetch() has open, by the peer's request id.
    _served_fetch_streams: Optional[Dict[int, int]] = None

    def _forget_served_fetch(self, stream_id: int) -> None:
        for rid, sid in self._served_fetch_streams.items():
            if sid == stream_id:
                del self._served_fetch_streams[rid]
                return

    def stream_fin(self, stream_id: int) -> None:
        """End-of-data on a sender-owned stream. Subgroup last object,
        fetch range complete, control GOAWAY+close use this."""
        self.stream_write(stream_id, b"", end_stream=True)

    def stream_reset(self, stream_id: int,
                      error_code: int = StreamResetCode.CANCELLED) -> None:
        """RESET_STREAM. Sender-side abrupt termination — UNSUBSCRIBE,
        FETCH_CANCEL on the publisher, track teardown, error abort.
        Also the canonical reply to a peer's STOP_SENDING. A
        StreamResetCode is encoded for the negotiated draft."""
        if not self._session_writable():
            return
        error_code = stream_reset_code(error_code, self._profile)
        if self._tx_deferred:
            self._tx_deferred.pop((stream_id, 'send'), None)
        self._tx_op(stream_id, 'send', 'reset', error_code)

    def stream_stop_sending(self, stream_id: int,
                             error_code: int = StreamResetCode.CANCELLED) -> None:
        """STOP_SENDING. Receiver-side request to cancel a peer-owned
        stream — UNSUBSCRIBE on the subscriber, FETCH_CANCEL on the
        subscriber, FETCH_ERROR while the fetch stream is still open."""
        if not self._session_writable():
            return
        error_code = stream_reset_code(error_code, self._profile)
        self._tx_op(stream_id, 'recv', 'stop', error_code)

    # Stream ops the transport refused on a full TX ring, retried as it
    # drains: (stream_id, side) -> ((kind, arg), first_refused). 'send'
    # holds the stream's terminal op, so a reset replaces a pending FIN.
    _tx_deferred: Optional[Dict[Tuple[int, str], Tuple[Tuple[str, Any], float]]] = None
    _tx_retry_task: Optional[asyncio.Task] = None
    TX_RETRY_POLL_S = 0.02
    TX_RETRY_GIVE_UP_S = 10.0

    def _tx_send(self, stream_id: int, kind: str, arg: Any) -> bool:
        """Hand one op to the transport; False when the ring is full."""
        try:
            if kind == 'reset':
                self._quic.reset_stream(stream_id, arg)
            elif kind == 'stop':
                self._quic.stop_stream(stream_id, arg)
            else:
                self._quic.send_stream_data(stream_id, arg, end_stream=True)
        except BufferError:
            return False
        except (AssertionError, AttributeError, RuntimeError,
                ConnectionError) as e:
            logger.debug(f"stream({stream_id}): {kind} race: {e}")
        return True

    def _tx_op(self, stream_id: int, side: str, kind: str, arg: Any) -> None:
        """Send now, or queue for retry when the TX ring is full."""
        if self._tx_send(stream_id, kind, arg):
            return
        logger.debug(f"stream({stream_id}): {kind} deferred: TX ring full")
        if self._tx_deferred is None:
            self._tx_deferred = {}
        self._tx_deferred[(stream_id, side)] = ((kind, arg), time.monotonic())
        if self._tx_retry_task is None:
            self._tx_retry_task = asyncio.ensure_future(self._tx_retry())

    def _tx_flush_deferred(self) -> None:
        now = time.monotonic()
        for key, ((kind, arg), since) in list(self._tx_deferred.items()):
            if self._tx_send(key[0], kind, arg):
                self._tx_deferred.pop(key, None)
            elif now - since > self.TX_RETRY_GIVE_UP_S:
                logger.warning(f"stream({key[0]}): {kind} dropped: TX ring "
                               f"full for {self.TX_RETRY_GIVE_UP_S:g}s")
                self._tx_deferred.pop(key, None)

    async def _tx_retry(self) -> None:
        """Retry deferred ops on each ring or stream drain, polling as a
        fallback, until all are sent or the session closes."""
        try:
            while self._tx_deferred and self._session_writable():
                transport = getattr(self._quic, '_transport', None)
                ring_ev = getattr(transport, 'tx_event_ring_drain_event', None)
                events = [ring_ev] if ring_ev is not None else []
                get_ev = getattr(self._quic, 'get_tx_data_drain_event', None)
                if get_ev is not None:
                    events += [get_ev(sid) for (sid, _), ((kind, _a), _t)
                               in self._tx_deferred.items() if kind == 'fin']
                for ev in events:
                    ev.clear()
                if ring_ev is not None:
                    transport.arm_tx_event_ring_drain_pending()
                self._tx_flush_deferred()
                if not self._tx_deferred:
                    break
                if not events:
                    await asyncio.sleep(self.TX_RETRY_POLL_S)
                    continue
                waits = [asyncio.ensure_future(ev.wait()) for ev in events]
                try:
                    await asyncio.wait(waits, timeout=self.TX_RETRY_POLL_S,
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for w in waits:
                        w.cancel()
        finally:
            self._tx_retry_task = None
            if not self._session_writable():
                self._tx_deferred = None

    def _stream_is_writable(self, stream_id: int) -> bool:
        """Whether send_stream_data on this session can be expected to
        succeed. Stream-id-specific state lives in picoquic; we only
        check session-level closed/torn-down here."""
        return self._session_writable()

    _PENDING_CONTROL_MAX = 128

    def _assert_type_defined_by_draft(self, msg: MOQTMessage) -> None:
        """Refuse to put a message type on the wire that the negotiated
        draft does not define. A peer receiving an unknown control
        message type closes the session with PROTOCOL_VIOLATION, so
        emitting one is never recoverable and never correct — d18 in
        particular removed every cancellation message (UNSUBSCRIBE,
        PUBLISH_NAMESPACE_DONE, PUBLISH_NAMESPACE_CANCEL, FETCH_CANCEL)
        in favour of resetting the request's own stream.
        """
        draft = self.negotiated_draft
        legal = CONTROL_MESSAGE_TYPES.get(draft)
        if legal is None:
            raise MOQTException(
                SessionCloseCode.INTERNAL_ERROR,
                f"no control message table for draft-{draft}")
        if msg.type is None or wire_control_type(draft, msg.type) in legal:
            return
        name = getattr(msg.type, 'name', None) or type(msg).__name__
        raise MOQTException(
            SessionCloseCode.INTERNAL_ERROR,
            f"refusing to send {name} (type 0x{int(msg.type):02x}): "
            f"not defined by draft-{draft}")

    def _msgstr(self, msg) -> str:
        """A message rendered for logs, parameters named at this session's draft."""
        describe = getattr(msg, 'describe', None)
        if describe is None:
            return str(msg)
        return describe(getattr(self, '_profile', None))

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
        self._assert_type_defined_by_draft(msg)
        if (self._profile.control_uni_pair and
                isinstance(msg, self._REPLY_CLASSES)):
            # d18: replies ride the request's own bidi stream and carry
            # no Request ID; on the control stream they are
            # uncorrelatable.
            raise MOQTException(
                SessionCloseCode.INTERNAL_ERROR,
                f"{type(msg).__name__} is a reply: it must ride its "
                f"request stream — use _send_reply")
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
        self._assert_type_defined_by_draft(msg)
        self.stream_write(stream_id, msg.serialize(prof=self._profile).data)

    def subgroup_header(self, track_alias: int, group_id: int,
                        **kwargs) -> SubgroupHeader:
        """Build a SubgroupHeader bound to this session's negotiated draft
        profile — the data-plane mirror of send_control_message. Higher
        APIs (Track, apps) call this and never thread draft/profile; the
        header's serialize()/next_object* then use the baked-in codec."""
        return SubgroupHeader(track_alias=track_alias, group_id=group_id,
                              prof=self._profile, **kwargs)

    _REPLY_CLASSES = (RequestOk, RequestError, SubscribeOk, SubscribeDone,
                      PublishOk, FetchOk, PublishStateNotify)

    _SETUP_LOCATION = ((SetupParamType.PATH, SessionCloseCode.INVALID_PATH),
                       (SetupParamType.AUTHORITY,
                        SessionCloseCode.INVALID_AUTHORITY))

    def _refuse_setup_location(self, options: Optional[Dict]) -> bool:
        """§10.3.1.1-2: PATH and AUTHORITY are client options for native
        QUIC; from a server, or over WT, they close the session."""
        if not options or not (self._is_client or self._is_wt):
            return False
        for ptype, code in self._SETUP_LOCATION:
            if ptype in options:
                self._close_session(
                    code, f"{ptype.name} option from server or over WT")
                return True
        return False

    def _check_setup_location(self, options: Optional[Dict]) -> None:
        """Refuse to send PATH or AUTHORITY where the peer must close."""
        if options and (self._is_wt or not self._is_client):
            for ptype, _ in self._SETUP_LOCATION:
                if ptype in options:
                    raise ValueError(
                        f"{ptype.name} is a client option for native QUIC")

    def _log_setup_complete(self) -> None:
        logger.info(f"MOQT session: setup complete: "
                    f"draft-{self.negotiated_draft} "
                    f"({'WT' if self._is_wt else 'raw QUIC'})")

    def _quiet_peer(self, msg) -> bool:
        """The peer's SETUP names an implementation the server's
        quiet_peers logs at DEBUG."""
        prefixes = getattr(getattr(self, '_session', None), 'quiet_peers', ())
        if not prefixes:
            return False
        options = (msg.options if isinstance(msg, Setup)
                   else getattr(msg, 'parameters', None)) or {}
        impl = options.get(SetupParamType.IMPLEMENTATION)
        if isinstance(impl, (bytes, bytearray, memoryview)):
            impl = bytes(impl).decode(errors="replace")
        return isinstance(impl, str) and impl.startswith(tuple(prefixes))

    def _implementation_option(self) -> Dict[int, bytes]:
        impl = getattr(getattr(self, '_session', None), 'implementation',
                       USER_AGENT)
        return {} if impl is None else {SetupParamType.IMPLEMENTATION: impl.encode()}

    def _setup_message(self, options: Dict[int, Any]) -> Setup:
        """SETUP of the negotiated draft's class (d18, or d20 for d20+)."""
        cls = self.CONTROL_REGISTRY[self.negotiated_draft][
            MOQTMessageType.SETUP][0]
        return cls(options=options)

    def _resolve_auth_token(self, msg: MOQTMessage) -> Optional[Callable]:
        """Apply a token-cache operation the peer sent (§10.2.2, §10.3.1.4),
        leave the resolved AuthToken in its place, and put a request's
        token to auth_token_validator. Raises on the session errors;
        returns the handler that refuses a request naming an unregistered
        alias or carrying a token the validator rejects."""
        in_setup = isinstance(msg, (Setup, ClientSetup, ServerSetup))
        params = (msg.options if isinstance(msg, Setup)
                  else getattr(msg, 'parameters', None))
        if not params:
            return None
        key = SetupParamType.AUTH_TOKEN if in_setup else ParamType.AUTH_TOKEN
        rid = getattr(msg, 'request_id', None)
        refusable = (not in_setup and rid is not None
                     and is_draft16_or_later(self.negotiated_draft))
        ref = params.get(key)
        if isinstance(ref, AuthTokenRef):
            if ref.alias_type == AuthTokenAliasType.REGISTER:
                if ref.alias in self._auth_tokens:
                    raise MOQTException(
                        SessionCloseCode.DUPLICATE_AUTH_TOKEN_ALIAS,
                        f"token alias {ref.alias} is already registered")
                token = AuthToken(ref.value or b"", ref.token_type or 0)
                size = 16 + len(token)
                if self._auth_token_cache_used + size <= self._auth_token_cache_max:
                    # Registered even if the request then fails (§10.2.2).
                    self._auth_tokens[ref.alias] = token
                    self._auth_token_cache_used += size
                elif not in_setup:
                    raise MOQTException(
                        SessionCloseCode.AUTH_TOKEN_CACHE_OVERFLOW,
                        f"token alias {ref.alias} ({size} bytes) exceeds the "
                        f"cache ({self._auth_token_cache_max} bytes)")
                # A SETUP registration that does not fit is used as a value.
                params[key] = token
            elif in_setup:
                if not self._is_client:
                    raise MOQTException(
                        SessionCloseCode.PROTOCOL_VIOLATION,
                        "DELETE or USE_ALIAS token in SETUP")
                del params[key]
            elif ref.alias not in self._auth_tokens:
                if not refusable:
                    return None
                # d18 defines UNKNOWN_AUTH_TOKEN_ALIAS (0x17) only as a session
                # code but asks for the request to be rejected with it.
                return self._refusal(
                    msg, int(rid), SessionCloseCode.UNKNOWN_AUTH_TOKEN_ALIAS,
                    f"token alias {ref.alias} not registered")
            elif ref.alias_type == AuthTokenAliasType.DELETE:
                known = self._auth_tokens.pop(ref.alias)
                self._auth_token_cache_used -= 16 + len(known)
                del params[key]
            else:
                params[key] = self._auth_tokens[ref.alias]
        token = params.get(key)
        if (not refusable or self.auth_token_validator is None
                or not isinstance(token, bytes)):
            return None
        if not isinstance(token, AuthToken):
            token = AuthToken(token)
        code = self.auth_token_validator(token, msg)
        if code is None:
            return None
        return self._refusal(msg, int(rid), code,
                             "authorization token refused")

    @staticmethod
    def _refusal(msg: MOQTMessage, rid: int, code: int,
                 reason: str) -> Callable:
        """The handler that answers request `rid` with REQUEST_ERROR. It runs
        as a task, after the request's stream is bound."""
        logger.info(f"MOQT: refusing {type(msg).__name__} {rid}: {reason}")
        err = RequestError(request_id=rid, error_code=int(code),
                           retry_interval=0, reason=reason)

        async def refuse(session, _msg):
            if isinstance(msg, RequestUpdate):
                session._send_on_request_stream(rid, err)
                session._update_failed(msg)
            else:
                session._send_on_request_stream(rid, err, fin=True)
        return refuse

    def _update_failed(self, msg: RequestUpdate) -> None:
        """§10.9.1: a failed REQUEST_UPDATE ends the request it updated —
        a track's subscription with PUBLISH_DONE UPDATE_FAILED, a FETCH by
        resetting its data stream; any other has its stream closed."""
        existing = msg.existing_request_id
        entry = self._requests.get(existing)
        if entry is not None and entry.role == "fetch":
            entry.track._end_fetch(self, existing)
        elif entry is not None and entry.sub is not None:
            entry.track._end_subscription(entry.sub,
                                          SubscribeDoneCode.UPDATE_FAILED)
        else:
            sid = self._bidi_streams.pop(existing, None)
            self._notify_request_cancelled(existing, "a failed update")
            if sid is not None:
                self.stream_fin(sid)

    def _refuse_request(self, msg: MOQTMessage) -> Optional[Callable]:
        """Session-level request rules, applied before the application
        sees the request (d16+): a reserved namespace (d18 §3.2.1-2), a
        duplicate subscription (§5.1), an overlapping namespace prefix
        (§10.18-19). Returns the handler that answers a refused request
        with REQUEST_ERROR, else None; records an admitted subscription
        until it ends."""
        d20 = self._profile.param_table >= 20
        if isinstance(msg, PublishStateNotify):
            self._check_publish_state_notify(msg)
            return None
        if (isinstance(msg, RequestUpdate)
                and is_draft16_or_later(self.negotiated_draft)):
            refusal = self._check_d20_update(msg) if d20 else None
            return refusal or self._refuse_prefix_update(msg)
        if (not is_draft16_or_later(self.negotiated_draft)
                or not isinstance(msg, self._REQUEST_OPENERS)
                or getattr(msg, 'request_id', None) is None):
            return None
        rid = int(msg.request_id)
        if d20:
            self._note_request_kind(rid, type(msg), ours=False)
        if self._goaway_sent and self.negotiated_draft >= 18:
            return self._refusal(msg, rid, RequestErrorCode.GOING_AWAY,
                                 "the session is going away")
        ns = tuple(getattr(msg, 'track_namespace', None)
                   or getattr(msg, 'namespace', None)
                   or getattr(msg, 'namespace_prefix', None) or ())
        kind = type(msg)
        key = None
        code = None
        if self.negotiated_draft >= 18 and ns and ns[0] in (b".", b".session"):
            # No session-level tracks are defined, so none is recognized.
            code = RequestErrorCode.DOES_NOT_EXIST
            reason = f"reserved namespace {ns[0].decode()}"
        elif kind is Subscribe:
            key = (ns, bytes(msg.track_name or b""))
            # d20 allows several subscriptions to a track and drops
            # DUPLICATE_SUBSCRIPTION; a track here serves one per session.
            duplicate = (RequestErrorCode.NOT_SUPPORTED if d20
                         else RequestErrorCode.DUPLICATE_SUBSCRIPTION)
            if (kind, key) in self._peer_requests.values():
                code = duplicate
                reason = "track already subscribed in this session"
            elif self._publishing(key):
                code = duplicate
                reason = "our PUBLISH of the track holds its subscription"
        elif kind in (SubscribeNamespace, SubscribeTracks):
            key = ns
            for k, prefix in self._peer_requests.values():
                if k is kind and (prefix[:len(ns)] == ns
                                  or ns[:len(prefix)] == prefix):
                    code = RequestErrorCode.PREFIX_OVERLAP
                    reason = "prefix overlaps an active subscription"
                    break
        if code is None and d20:
            code, reason = self._d20_refusal(msg)
        if code is None:
            if key is not None:
                self._peer_requests[rid] = (kind, key)
            if d20 and kind is SubscribeTracks:
                params = msg.parameters or {}
                self._subscribe_tracks_params[rid] = {
                    k: v for k, v in params.items()
                    if k in self._SUBSCRIBE_TRACKS_CARRIED}
            return None
        return self._refusal(msg, rid, code, reason)

    # SUBSCRIBE_TRACKS parameters a resulting PUBLISH carries: the
    # subscription parameters PUBLISH may hold, never AUTHORIZATION TOKEN
    # (d20 §10.2.2, §10.20.1).
    _SUBSCRIBE_TRACKS_CARRIED = frozenset({
        ParamType.DELIVERY_TIMEOUT,
        0x06,                               # SUBGROUP_DELIVERY_TIMEOUT
        ParamType.FORWARD, ParamType.SUBSCRIBER_PRIORITY,
        ParamType.LOCATION_FILTER, ParamType.GROUP_ORDER,
    })

    def _note_request_kind(self, request_id: int, kind: type,
                           ours: bool) -> None:
        kinds = self._request_kinds
        kinds[request_id] = (kind, ours)
        if len(kinds) > self.REQUEST_KINDS_MAX:
            kinds.popitem(last=False)

    REQUEST_KINDS_MAX = 4096

    def _d20_refusal(self, msg: MOQTMessage) -> Tuple[Optional[int], str]:
        """(REQUEST_ERROR code, reason) for a d20+ request this session
        cannot accept, else (None, "")."""
        params = msg.parameters or {}
        # We send no MAX_FILTER_RANGES, so no Range may arrive (§5.1.4).
        why = range_filter_error(params, max_ranges=0)
        fill = params.get(D20ParamType.FILL_PARAMETERS)
        if why is None and isinstance(fill, dict):
            why = range_filter_error(fill, max_ranges=0)
        if why is not None:
            return D20RequestErrorCode.INVALID_FILTER, why
        return None, ""

    _UPDATE_SCOPES = {
        Subscribe: "Subscription", Publish: "Subscription", Fetch: "Fetch",
        PublishNamespace: "PublishNamespace",
        SubscribeNamespace: "SubscribeNamespace",
        SubscribeTracks: "SubscribeTracks",
    }

    def _check_d20_update(self, msg: RequestUpdate) -> Optional[Callable]:
        """d20 §10.9: only the sender of a request, or the subscriber of a
        PUBLISH, may update it, with the parameters its type allows.
        Raises on a violation; returns the handler that refuses a filter
        the session cannot take."""
        existing = msg.existing_request_id
        kind = self._request_kinds.get(existing)
        if kind is None or msg.request_id is None:
            return None
        cls, ours = kind
        if (ours and cls is not Publish) or (not ours and cls is TrackStatus):
            raise MOQTProtocolViolation(
                f"REQUEST_UPDATE for a {cls.__name__} the peer may not "
                f"update")
        params = msg.parameters or {}
        check_param_scope(params, f"RequestUpdate:{self._UPDATE_SCOPES[cls]}",
                          prof=self._profile)
        if any(p in RANGE_FILTERS for p in params):
            if cls is Publish and not ours:
                raise MOQTProtocolViolation(
                    "Range Filters in a REQUEST_UPDATE from the publisher")
            why = range_filter_error(params, max_ranges=0)
            if why is not None:
                return self._refusal(msg, int(msg.request_id),
                                     D20RequestErrorCode.INVALID_FILTER, why)
        carried = self._subscribe_tracks_params.get(existing)
        if (carried is not None and not ours
                and ParamType.FORWARD in params):
            # Later PUBLISHes for the prefix take the new Forward State.
            carried[ParamType.FORWARD] = params[ParamType.FORWARD]
        return None

    def _check_publish_state_notify(self, msg: PublishStateNotify) -> None:
        """d20 §10.10: only a subscription's publisher sends it."""
        kind = self._request_kinds.get(msg.request_id)
        if kind is None:
            return
        cls, ours = kind
        if not ((ours and cls is Subscribe) or (not ours and cls is Publish)):
            raise MOQTProtocolViolation(
                f"PUBLISH_STATE_NOTIFY on a {cls.__name__} stream "
                f"{'we' if ours else 'the peer'} opened")

    def _publishing(self, key) -> bool:
        """True while our PUBLISH of this Full Track Name is bound: its
        subscription is Pending (publisher) or Established, §5.1."""
        track = self._published_tracks.get(key)
        if track is None:
            return False
        own = 0 if self._is_client else 1
        return any(entry.track is track and entry.role == "publisher"
                   and rid & 1 == own
                   for rid, entry in self._requests.items())

    def _peer_subscribed(self, track) -> bool:
        """True while a SUBSCRIBE from the peer for the track is admitted,
        including one its handler has not reached yet."""
        key = self._track_key(track.namespace, track.trackname)
        return (Subscribe, key) in self._peer_requests.values()

    def _refuse_prefix_update(self, msg: RequestUpdate) -> Optional[Callable]:
        """§10.9.2: a REQUEST_UPDATE that moves a SUBSCRIBE_NAMESPACE or
        SUBSCRIBE_TRACKS prefix onto another of its type is refused with
        PREFIX_OVERLAP; an accepted one moves the recorded prefix."""
        prefix = (msg.parameters or {}).get(ParamType.TRACK_NAMESPACE_PREFIX)
        existing = msg.existing_request_id
        held = self._peer_requests.get(existing)
        if (prefix is None or held is None or msg.request_id is None
                or held[0] not in (SubscribeNamespace, SubscribeTracks)):
            return None
        kind, prefix = held[0], tuple(prefix)
        for rid, (k, other) in self._peer_requests.items():
            if rid != existing and k is kind and (
                    other[:len(prefix)] == prefix
                    or prefix[:len(other)] == other):
                return self._refusal(msg, int(msg.request_id),
                                     RequestErrorCode.PREFIX_OVERLAP,
                                     "prefix overlaps an active subscription")
        self._peer_requests[existing] = (kind, prefix)
        return None

    # Request IDs the peer cancelled, oldest first; replies to them are dropped.
    _gone_requests: Optional['OrderedDict[int, None]'] = None
    GONE_REQUESTS_MAX = 1024

    def _note_request_gone(self, request_id: int) -> None:
        gone = self._gone_requests
        if gone is None:
            gone = self._gone_requests = OrderedDict()
        gone[request_id] = None
        if len(gone) > self.GONE_REQUESTS_MAX:
            gone.popitem(last=False)

    def _reply_to_gone_request(self, request_id: int,
                               msg: MOQTMessage) -> bool:
        """True, and the reply is dropped, when the peer has cancelled
        the request (STOP_SENDING, reset, FIN, UNSUBSCRIBE, FETCH_CANCEL):
        a handler answering after an await races the cancel."""
        if not self._gone_requests or request_id not in self._gone_requests:
            return False
        logger.debug(f"MOQT: request {request_id} cancelled by peer; "
                     f"dropping {type(msg).__name__}")
        return True

    def _send_reply(self, request_id: int, msg: MOQTMessage,
                    fin: bool = False) -> None:
        """Send a response to a request. In d18 responses travel on the
        request's own bidi stream (demuxed by stream, no in-band Request
        ID); pre-d18 they go on the single control stream. `fin` closes
        our half of the request stream after a terminal reply
        (REQUEST_ERROR, PUBLISH_DONE, TRACK_STATUS_OK: §3.3.2, §10.11,
        §10.14); it has no meaning on the control stream."""
        if self._reply_to_gone_request(request_id, msg):
            return
        if fin:
            self._peer_requests.pop(request_id, None)
        if self._profile.control_uni_pair:
            self._send_on_request_stream(request_id, msg, fin=fin)
        else:
            self.send_control_message(msg)

    def _send_on_request_stream(self, request_id: int, msg: MOQTMessage,
                                fin: bool = False) -> None:
        """Send on the bidi stream a request owns: every request in d18,
        SUBSCRIBE_NAMESPACE from d16. Pre-d18 an unbound request falls
        back to the control stream; d18 has no such path."""
        if self._reply_to_gone_request(request_id, msg):
            return
        sid = (self._bidi_streams.get(request_id)
               if is_draft16_or_later(self.negotiated_draft) else None)
        if sid is not None:
            self.send_stream_message(sid, msg)
            if fin:
                self.stream_write(sid, b"", end_stream=True)
        elif self._profile.control_uni_pair:
            # Both directions bind request streams (incoming at
            # _on_control_data, outgoing at _send_request); a missing
            # binding is a bug, and falling back to the control stream
            # would emit an uncorrelatable reply.
            raise MOQTException(
                SessionCloseCode.INTERNAL_ERROR,
                f"no request stream bound for request_id="
                f"{request_id}: cannot send {type(msg).__name__}")
        else:
            self.send_control_message(msg)

    def _send_request(self, request_id: int, msg: MOQTMessage) -> None:
        """Send a request-stream opener. In d18 every request (SUBSCRIBE,
        PUBLISH, FETCH, TRACK_STATUS, PUBLISH_NAMESPACE, ...) opens its own
        bidi stream and the response returns on it (no in-band Request ID);
        pre-d18 requests go on the single control stream. Raw-QUIC stream-id
        allocation is synchronous, so callers stay sync."""
        if self._profile.control_uni_pair:
            self._note_request_kind(request_id, type(msg), ours=True)
            self._tx_updates[request_id] = deque([None])
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

    def send_dgram_message(self, buf: Buffer) -> int:
        """Send a MoQT message via QUIC datagram, fire-and-forget.

        Returns bytes accepted; 0 means the per-connection datagram
        ring is full (backpressure). Paced producers should use
        dgram_write_drain instead, which parks until the ring drains.
        """
        if self._quic is None:
            raise MOQTException(SessionCloseCode.INTERNAL_ERROR,
                                "QUIC not initialized")
        logger.debug(f"QUIC send: datagram message: {buf.capacity} bytes")
        return self._quic.send_datagram_frame(data=buf.data)

    @property
    def handshake_info(self) -> Dict[str, Any]:
        """Structured handshake result (probe ask #6): negotiated
        draft/version/ALPN, transport, time-to-established, current
        path RTT (µs), peer transport parameters, connection IDs.
        Fields are None until the corresponding state exists."""
        draft = getattr(self, 'negotiated_draft', None)
        if not self._is_wt:
            q = self._quic
            alpn = q._negotiated_alpn(0) if q is not None else None
            pq = q.path_quality() if q is not None else {}
        else:
            alpn = "h3"
            tr = getattr(self, '_transport', None)
            ptr = getattr(self, 'cnx_ptr', 0)
            pq = tr.path_quality(ptr) if tr is not None and ptr else {}
        return {
            'transport': 'webtransport' if self._is_wt else 'quic',
            'draft': draft,
            'wire_version': (f"0x{moqt_version_from_draft(draft):08x}"
                             if draft else None),
            'alpn': alpn,
            'wt_protocol': (f"moqt-{draft}"
                            if self._is_wt and draft and draft >= 15
                            else None),
            'time_to_established_ms': self._established_ms,
            'rtt_us': (pq or {}).get('rtt'),
            'peer_transport_parameters': self.peer_transport_parameters,
            'connection_ids': self.connection_ids,
        }

    @property
    def peer_address(self) -> Optional[Tuple[str, int]]:
        """The peer's (host, port); None while unknown, after close, or
        with an aiopquic that cannot report it. An IPv4-mapped IPv6
        address comes back as IPv4."""
        if self._is_wt:
            tr = getattr(self, '_transport', None)
            ptr = getattr(self, 'cnx_ptr', 0)
            fn = getattr(tr, 'peer_address', None)
            return fn(ptr) if fn is not None and ptr else None
        fn = getattr(self._quic, 'peer_address', None)
        return fn() if fn is not None else None

    @property
    def qlog_paths(self) -> List[str]:
        """qlog file(s) for this connection (probe ask #7): matches the
        session's connection-ID hex names under the effective qlog_dir
        (or AIOPQUIC_QLOG_DIR). Empty list when qlog is off or nothing
        has been written yet."""
        import os as _os
        from pathlib import Path as _Path
        cfg = getattr(getattr(self, '_session', None),
                      'effective_configuration', None)
        qdir = (getattr(cfg, 'qlog_dir', None)
                or _os.environ.get('AIOPQUIC_QLOG_DIR'))
        if not qdir:
            return []
        cids = self.connection_ids or {}
        hexes = {v.hex() for v in cids.values()
                 if isinstance(v, bytes) and v}
        out = []
        for f in _Path(qdir).glob('*.*qlog'):
            stem = f.stem.lower()
            if any(h and h in stem for h in hexes):
                out.append(str(f))
        return sorted(out)

    @property
    def peer_transport_parameters(self) -> Optional[Dict[str, Any]]:
        """The peer's negotiated QUIC transport parameters as a dict
        (None before the handshake). Probe/fingerprinting surface —
        the values live in picoquic; no qlog round trip. Unknown/GREASE
        ids and wire order are not preserved (struct parse)."""
        if not self._is_wt:
            q = self._quic
            return (q.peer_transport_parameters
                    if q is not None else None)
        tr = getattr(self, '_transport', None)
        ptr = getattr(self, 'cnx_ptr', 0)
        if tr is not None and ptr:
            return tr.transport_parameters(ptr)
        return None

    @property
    def connection_ids(self) -> Optional[Dict[str, bytes]]:
        """{'local', 'remote', 'initial'} connection IDs as bytes, or
        None. QUIC-LB / routable-CID detection."""
        if not self._is_wt:
            q = self._quic
            return q.connection_ids if q is not None else None
        tr = getattr(self, '_transport', None)
        ptr = getattr(self, 'cnx_ptr', 0)
        if tr is not None and ptr:
            return tr.connection_ids(ptr)
        return None

    def datagram_max_payload(self) -> int:
        """Usable per-datagram payload ceiling on this session's
        transport: min(local TP, peer TP, guaranteed MTU floor). 0 =
        datagrams unavailable (peer didn't negotiate, handshake not
        complete, or WT transport — WT datagram TX isn't wired yet)."""
        if getattr(self, '_is_wt', False):
            return 0
        q = self._quic
        if q is None or not hasattr(q, 'max_datagram_payload'):
            return 0
        return q.max_datagram_payload()

    async def send_padding(self, size: int, *,
                           datagram: bool = False) -> Optional[int]:
        """Send `size` bytes of PADDING (§11.5, d18+): a uni stream ended
        with FIN at the lowest send priority, returning its id, or one
        datagram, returning None."""
        wire = encode_padding(size, prof=self._profile, datagram=datagram)
        if datagram:
            if len(wire) > self.datagram_max_payload():
                raise ValueError(
                    f"padding datagram of {len(wire)}B does not fit "
                    f"(ceiling {self.datagram_max_payload()}B)")
            await self.dgram_write_drain(wire)
            return None
        sid = await self.open_uni_stream()
        self.set_stream_priority(sid, STREAM_BAND_PADDING)
        await self.stream_write_drain(sid, wire, end_stream=True)
        return sid

    async def dgram_write_drain(self, buf: Buffer) -> None:
        """Send a MoQT datagram, parking on transport backpressure.

        The per-connection record ring bounds queued datagrams (ring
        size / throughput = queuing latency); when it is full, wait for
        the worker's drain edge instead of dropping. Raises
        CancelledError once the session is closing, mirroring
        stream_write_drain.
        """
        data = buf if isinstance(buf, (bytes, bytearray)) else buf.data
        while True:
            if self._close_err is not None or self._quic is None:
                raise asyncio.CancelledError
            try:
                if self._quic.send_datagram_frame(data=data):
                    return
            except ConnectionError:
                raise asyncio.CancelledError
            await self._quic.get_datagram_tx_drain_event().wait()

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
        logger.info(f"MOQT send: {self._msgstr(message)}")
        self.send_control_message(message)

        return message

    def server_setup(
        self,
        selected_version: int = MOQT_VERSION_DRAFT14,
        parameters: Optional[Dict[int, bytes]] = None
    ) -> ServerSetup:
        """Send SERVER_SETUP message in response to CLIENT_SETUP."""
        self._check_setup_location(parameters)
        parameters = {**self._implementation_option(), **(parameters or {})}
        if not self._profile.control_uni_pair:
            self._local_request_max = self.REQUEST_ID_WINDOW
            parameters.setdefault(SetupParamType.MAX_REQUEST_ID,
                                  self._local_request_max)
        self._auth_token_cache_max = int(
            parameters.get(SetupParamType.MAX_AUTH_TOKEN_CACHE_SIZE, 0))

        message = ServerSetup(
            selected_version=selected_version,
            parameters=parameters
        )
        logger.info(f"MOQT send: {self._msgstr(message)}")
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
        location_filter: Optional[LocationFilter] = None,
        on_object: Optional[Callable] = None,
        on_done: Optional[Callable] = None,
    ) -> Optional[MOQTMessage]:
        """Subscribe to a track with configurable options.

        From d20 the filter travels as LOCATION_FILTER: filter_type and
        its Locations are converted, or `location_filter` is sent as is
        for the forms they cannot express.

        `on_object` receives this track's objects (the
        register_object_handler signature), bound to its alias as
        SUBSCRIBE_OK is parsed; `on_done` is its PUBLISH_DONE handler
        (register_publish_done_handler). Both are registered before
        SUBSCRIBE is sent and dropped if it is refused. A d20
        FILL_PARAMETERS fill stream comes under this Request ID:
        await_fetch_done() waits for it, and its objects reach
        on_fetch_object or register_fetch_object_handler()."""
        if parameters is None:
            parameters = {}
        if location_filter is not None and not uses_location_filter(
                self._profile):
            raise ValueError(
                f"location_filter needs draft-20 or later, not "
                f"draft-{self.negotiated_draft}")
        self._check_filter_ranges(parameters)
        request_id = self._allocate_request_id()
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
            parameters=parameters,
            location_filter=location_filter,
        )
        message.libquicr_compat = self._session.libquicr_compat
        self._subscriptions[request_id] = [message]
        self._register_fill(request_id, parameters, self._sent_filter(message),
                            group_order, forward)
        if on_object is not None:
            self._pending_object_handlers[request_id] = on_object
        if on_done is not None:
            self.register_publish_done_handler(request_id, on_done)
        logger.info(f"MOQT send: {self._msgstr(message)}")
        self._send_request(request_id, message)

        if not wait_response:
            return message
        if on_object is not None or on_done is not None:
            return self._await_routed(request_id)
        return self._await_response(request_id)

    def track_status(
        self,
        namespace: Union[str, Tuple[bytes, ...]],
        track_name: Union[str, bytes],
        priority: int = 128,
        group_order: int = GroupOrder.ASCENDING,
        forward: int = 1,
        filter_type: int = FilterType.LATEST_OBJECT,
        parameters: Optional[Dict[int, Any]] = None,
        wait_response: bool = False,
    ):
        """TRACK_STATUS (§10.14): ask about a track without subscribing.
        Answered by TRACK_STATUS_OK/ERROR (REQUEST_OK/ERROR at d16+).
        priority, group_order, forward and filter_type are d14 fields;
        d16+ sends none of them."""
        request_id = self._allocate_request_id()
        self._track_status_requests.add(request_id)
        message = TrackStatus(
            request_id=request_id,
            track_namespace=self._make_namespace_tuple(namespace),
            track_name=(track_name.encode() if isinstance(track_name, str)
                        else track_name),
            priority=priority,
            group_order=group_order,
            forward=forward,
            filter_type=filter_type,
            parameters=parameters or {},
        )
        logger.info(f"MOQT send: {self._msgstr(message)}")
        self._send_request(request_id, message)
        if not wait_response:
            return message
        return self._await_response(request_id)

    def request_update(
        self,
        existing_request_id: int,
        forward: Optional[int] = None,
        parameters: Optional[Dict[int, Any]] = None,
        wait_response: bool = False,
        *,
        start: Optional[Tuple[int, int]] = None,
        end_group: Optional[int] = None,
        priority: Optional[int] = None,
    ):
        """REQUEST_UPDATE (§10.9; SUBSCRIBE_UPDATE 0x02 at d16): change
        the parameters of an outstanding request. d18 sends it on that
        request's own stream and its REQUEST_OK/ERROR comes back there;
        d16 uses the control stream with the Existing Request ID.

        d14 sends SUBSCRIBE_UPDATE, which has no reply. Its Start Location,
        End Group and Subscriber Priority are `start`, `end_group` and
        `priority`, else taken from our SUBSCRIBE for that request; with
        neither, ValueError."""
        if self._profile.update_has_location:
            return self._subscribe_update(existing_request_id, forward,
                                          parameters, start, end_group,
                                          priority)
        params = dict(parameters or {})
        if forward is not None:
            params[ParamType.FORWARD] = int(forward)
        if self._profile.param_table >= 20:
            self._check_filter_ranges(params)
            pending = sum(u is not None for u in
                          self._tx_updates.get(existing_request_id, ()))
            limit = self._peer_max_request_updates
            if limit and pending >= limit:
                # d20 §10.3.1.7: past its limit the peer closes the session.
                raise ValueError(
                    f"{pending} REQUEST_UPDATEs already outstanding on "
                    f"request {existing_request_id} (peer "
                    f"MAX_REQUEST_UPDATES {limit})")
        request_id = self._allocate_request_id(opener=False)
        message = RequestUpdate(request_id=request_id,
                                existing_request_id=existing_request_id,
                                parameters=params)
        sub = next((m for m in self._subscriptions.get(existing_request_id, ())
                    if isinstance(m, Subscribe)), None)
        if sub is not None:
            lf = params.get(ParamType.LOCATION_FILTER, self._sent_filter(sub))
            self._register_fill(request_id, params, lf, sub.group_order,
                                params.get(ParamType.FORWARD))
        logger.info(f"MOQT send: {self._msgstr(message)}")
        if self._profile.control_uni_pair:
            sid = self._bidi_streams.get(existing_request_id)
            if sid is None:
                raise MOQTException(
                    SessionCloseCode.INTERNAL_ERROR,
                    f"no request stream bound for request_id="
                    f"{existing_request_id}: cannot send REQUEST_UPDATE")
            self._tx_updates.setdefault(existing_request_id,
                                        deque()).append(request_id)
            self.send_stream_message(sid, message)
        else:
            self.send_control_message(message)
        if not wait_response:
            return message
        return self._await_response(request_id)

    def _subscribe_update(self, existing_request_id: int,
                          forward: Optional[int],
                          parameters: Optional[Dict[int, Any]],
                          start: Optional[Tuple[int, int]],
                          end_group: Optional[int],
                          priority: Optional[int]) -> SubscribeUpdate:
        """d14 SUBSCRIBE_UPDATE (§9.10). Omitted fields keep what our
        SUBSCRIBE for the request set: a Start Location never decreases."""
        msgs = self._subscriptions.get(existing_request_id, ())
        sub = next((m for m in msgs if isinstance(m, Subscribe)), None)
        if sub is None and start is None:
            raise ValueError(
                f"SUBSCRIBE_UPDATE for request {existing_request_id} needs "
                f"start=: this session sent no SUBSCRIBE for it")
        if start is None:
            ok = next((m for m in msgs if isinstance(m, SubscribeOk)), None)
            start = self._filter_start(sub, ok)
        if end_group is None:
            end_group = (sub.end_group + 1 if sub is not None
                         and sub.filter_type == FilterType.ABSOLUTE_RANGE
                         else 0)
        if priority is None:
            priority = (sub.priority if sub is not None
                        and sub.priority is not None else 128)
        if forward is None:
            forward = 1 if sub is None or sub.forward is None else sub.forward
        params = {k: v for k, v in (parameters or {}).items()
                  if k != ParamType.FORWARD}
        message = SubscribeUpdate(
            request_id=self._allocate_request_id(opener=False),
            subscription_request_id=existing_request_id,
            start_group=start[0], start_object=start[1],
            end_group=end_group, priority=priority, forward=int(forward),
            parameters=params)
        logger.info(f"MOQT send: {self._msgstr(message)}")
        self.send_control_message(message)
        return message

    @staticmethod
    def _filter_start(sub: Subscribe,
                      ok: Optional[SubscribeOk]) -> Tuple[int, int]:
        """The Start Location a d14 SUBSCRIBE's filter resolved to
        (§5.1.2): Largest Object comes from SUBSCRIBE_OK."""
        if sub.filter_type in (FilterType.ABSOLUTE_START,
                               FilterType.ABSOLUTE_RANGE):
            return (sub.start_group or 0, sub.start_object or 0)
        if ok is None or ok.largest_group_id is None:
            return (0, 0)
        if sub.filter_type == FilterType.NEXT_GROUP_START:
            return (ok.largest_group_id + 1, 0)
        return (ok.largest_group_id, (ok.largest_object_id or 0) + 1)

    def publish_ok(
        self,
        request_msg: 'Publish',
        forward: int = 1,
        priority: int = 128,
        group_order: int = GroupOrder.ASCENDING,
        filter_type: int = FilterType.LATEST_OBJECT,
        start_group: Optional[int] = None,
        start_object: Optional[int] = None,
        end_group: Optional[int] = None,
        parameters: Optional[Dict[int, Any]] = None,
        location_filter: Optional[LocationFilter] = None,
    ) -> Optional[MOQTMessage]:
        """Accept a PUBLISH: PUBLISH_OK (0x1E) through d16, REQUEST_OK
        carrying the same fields as parameters at d18 (§10.5).

        From d20 PUBLISH_OK carries only EXPIRES (`parameters`); the
        subscription parameters that differ from the PUBLISH's follow in
        a REQUEST_UPDATE on its stream (d20 §10.9). GROUP_ORDER cannot
        change there and is not sent."""
        alias = getattr(request_msg, 'track_alias', None)
        if alias is not None:
            self._establish_alias(alias, self._msg_track(request_msg),
                                  request_msg.request_id)
        if self._profile.publish_ok_expires_only:
            message = PublishOk(request_id=request_msg.request_id,
                                parameters=dict(parameters or {}))
            logger.info(f"MOQT send: {self._msgstr(message)}")
            self._send_reply(request_msg.request_id, message)
            update = self._publish_ok_update(
                request_msg, forward, priority, filter_type, start_group,
                start_object, end_group, location_filter)
            if update:
                self.request_update(request_msg.request_id,
                                    parameters=update)
            return message
        message = PublishOk(
            request_id=request_msg.request_id,
            forward=forward,
            priority=priority,
            group_order=group_order,
            filter_type=filter_type,
            start_group=start_group,
            start_object=start_object,
            end_group=end_group,
            parameters=parameters or {},
        )
        logger.info(f"MOQT send: {self._msgstr(message)}")
        self._send_reply(request_msg.request_id, message)
        return message

    @staticmethod
    def _publish_ok_update(request_msg: 'Publish', forward, priority,
                           filter_type, start_group, start_object,
                           end_group, location_filter) -> Dict[int, Any]:
        """The REQUEST_UPDATE parameters that turn a PUBLISH's initial
        subscription settings into the ones a d18 PUBLISH_OK would have
        set. Next Object is where a PUBLISH starts, so that filter needs
        no update."""
        initial = request_msg.parameters or {}
        params: Dict[int, Any] = {}
        sent_forward = 1 if request_msg.forward is None else request_msg.forward
        if forward is not None and int(forward) != int(sent_forward):
            params[ParamType.FORWARD] = int(forward)
        if (priority is not None and priority != initial.get(
                ParamType.SUBSCRIBER_PRIORITY, 128)):
            params[ParamType.SUBSCRIBER_PRIORITY] = int(priority)
        if location_filter is None and filter_type not in (
                None, FilterType.LATEST_OBJECT):
            location_filter = LocationFilter.from_subscription_filter(
                filter_type, start_group, start_object, end_group)
        if location_filter is not None:
            params[ParamType.LOCATION_FILTER] = location_filter
        return params

    def _check_filter_ranges(self, params: Dict[int, Any]) -> None:
        """Refuse to send more Range Filter ranges than the peer's
        MAX_FILTER_RANGES allows (d20 §10.3.1.6): it would answer
        INVALID_FILTER."""
        if self._profile.param_table < 20:
            return
        fill = params.get(D20ParamType.FILL_PARAMETERS)
        count = range_filter_count(params) + (
            range_filter_count(fill) if isinstance(fill, dict) else 0)
        if count > self._peer_max_filter_ranges:
            raise ValueError(
                f"{count} filter ranges exceed the peer's "
                f"MAX_FILTER_RANGES {self._peer_max_filter_ranges}")

    @staticmethod
    def _sent_filter(msg: Subscribe) -> Optional[LocationFilter]:
        """The Location Filter a SUBSCRIBE of ours carries from d20."""
        if msg.location_filter is not None or msg.filter_type is None:
            return msg.location_filter
        return LocationFilter.from_subscription_filter(
            msg.filter_type, msg.start_group, msg.start_object, msg.end_group)

    def _register_fill(self, request_id: int, params: Dict[int, Any],
                       subscription_filter: Optional[LocationFilter],
                       group_order: Optional[int],
                       forward: Optional[int]) -> None:
        """Expect the fill fetch stream a SUBSCRIBE or REQUEST_UPDATE
        carrying FILL_PARAMETERS asks for (d20 §5.1.3), under its Request
        ID; await_fetch_done() waits for it. A paused one opens none."""
        fill = (params or {}).get(D20ParamType.FILL_PARAMETERS)
        if (fill is None or self._profile.param_table < 20
                or (forward is not None and not int(forward))):
            return
        order = (fill.get(ParamType.GROUP_ORDER) or group_order
                 or GroupOrder.ASCENDING)
        self._fill_requests[request_id] = (fill, subscription_filter,
                                           int(order))
        self._fetch_done_futures[request_id] = self._loop.create_future()

    def subscribe_ok(
        self,
        request_msg: Subscribe,
        expires: int = 0,  # 0 means no expiry
        group_order: int = GroupOrder.ASCENDING,
        content_exists: int = 0,
        largest_group_id: Optional[int] = None,
        largest_object_id: Optional[int] = None,
        parameters: Optional[Dict[int, bytes]] = None,
        track_properties: Optional[Dict[int, Any]] = None,
    ) -> Optional[MOQTMessage]:
        """Create and send a SUBSCRIBE_OK response. track_properties are
        sent from d16 and dropped at d14."""
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
            parameters=parameters or {},
            track_extensions=track_properties,
        )
        logger.info(f"MOQT send: {self._msgstr(message)}")
        self._send_reply(request_msg.request_id, message)
        return message

    def subscribe_done(
        self,
        request_id: int,
        status_code: Optional[int] = None,
        stream_count: int = 0,
        reason: str = "",
    ) -> Optional[MOQTMessage]:
        """Create and send a PUBLISH_DONE (SUBSCRIBE_DONE) response on the
        subscription's request stream (d18) or the control stream.
        The default status is SUBSCRIPTION_ENDED, or TRACK_ENDED from d20,
        which removed it."""
        if status_code is None:
            status_code = (SubscribeDoneCode.SUBSCRIPTION_ENDED
                           if self._profile.publish_done_subscription_ended
                           else SubscribeDoneCode.TRACK_ENDED)
        message = SubscribeDone(
            request_id=request_id,
            status_code=status_code,
            stream_count=stream_count,
            reason=reason,
        )
        logger.info(f"MOQT send: {self._msgstr(message)}")
        self._retire_published_alias(request_id)
        self._send_reply(request_id, message, fin=True)
        return message

    def subscribe_error(
        self,
        request_id: int,
        error_code: int = SubscribeErrorCode.INTERNAL_ERROR,
        reason: str = "Internal error",
    ) -> Optional[MOQTMessage]:
        """Create and send a negative response to a SUBSCRIBE.

        d14: SUBSCRIBE_ERROR on the control stream. d16+: code point
        0x05 is REQUEST_ERROR (Error Code, Retry Interval, Reason) —
        a d14 body there desyncs the peer's parse. A named code is
        encoded for the negotiated draft (request_error_code())."""
        error_code = request_error_code(error_code, self._profile)
        if is_draft16_or_later(self.negotiated_draft):
            message = RequestError(
                request_id=request_id,
                error_code=error_code,
                retry_interval=0,
                reason=reason,
            )
            logger.info(f"MOQT send: {self._msgstr(message)}")
            self._send_reply(request_id, message, fin=True)
        else:
            message = SubscribeError(
                request_id=request_id,
                error_code=error_code,
                reason=reason,
            )
            logger.info(f"MOQT send: {self._msgstr(message)}")
            self._send_reply(request_id, message)
        return message
    
    def unsubscribe(
        self,
        request_id: int,
    ) -> Optional[MOQTMessage]:
        """Unsubscribe from a track.

        d14/d16 send UNSUBSCRIBE (0x0A). d18 removed it: a request is
        cancelled by terminating its own bidi stream (§3.3.2) — reset
        our write half, STOP_SENDING the reply half. Returns None then."""
        self._retire_alias(request_id)
        if self.negotiated_draft >= 18:
            stream_id = self._bidi_streams.get(request_id)
            if stream_id is None:
                logger.debug(
                    f"d18 unsubscribe: no request stream for "
                    f"request_id={request_id}; nothing to reset")
                return None
            logger.info(f"MOQT: d18 unsubscribe: cancel request stream "
                        f"{stream_id} (request_id={request_id})")
            self.stream_reset(stream_id, StreamResetCode.CANCELLED)
            self.stream_stop_sending(stream_id, StreamResetCode.CANCELLED)
            self._bidi_streams.pop(request_id, None)
            self._fail_waiting_updates(request_id, "cancelled")
            return None
        message = Unsubscribe(request_id=request_id)
        logger.info(f"MOQT send: {self._msgstr(message)}")
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
        fetch_parameters: Optional[Dict[int, Any]] = None,
        on_object: Optional[Callable] = None,
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
            parameters: the SUBSCRIBE's. The FETCH takes only its
                AUTHORIZATION_TOKEN: a SUBSCRIBE-only parameter in a FETCH
                is a protocol violation (d18 §10.2.1).
            fetch_parameters: the FETCH's, over that token.
            on_object: the subscription's object callback, bound to its
                alias as SUBSCRIBE_OK arrives; objects that came first are
                replayed to it.
            wait_response: if True, awaits and returns
                (subscribe_ok, fetch_ok). If False, returns
                (subscribe_msg, fetch_msg) sent messages.

        Returns:
            (subscribe_response, fetch_response) when wait_response=True,
            (subscribe_msg, fetch_msg) when wait_response=False.

        From d20 there is no Joining FETCH: one SUBSCRIBE at the Next
        Object carries FILL_PARAMETERS, and the fill fetch stream under
        its Request ID delivers the range (d20 §5.1.6). Both halves of the
        pair are then the SUBSCRIBE's.
        """
        if fetch_type not in (FetchType.RELATIVE_JOINING,
                               FetchType.ABSOLUTE_JOINING):
            raise ValueError(
                f"join() requires a joining fetch_type, got {fetch_type}")
        if self._profile.fetch_location_filter:
            params = dict(parameters or {})
            params[D20ParamType.FILL_PARAMETERS] = {
                ParamType.LOCATION_FILTER: self._joining_filter(
                    fetch_type, joining_start)}
            sub_msg = self.subscribe(
                namespace, track_name, priority=subscriber_priority,
                group_order=group_order,
                filter_type=FilterType.LATEST_OBJECT, parameters=params,
                on_object=on_object)
            if not wait_response:
                return (sub_msg, sub_msg)
            sub_ok = await self._await_response(sub_msg.request_id)
            return (sub_ok, sub_ok)

        parameters = {} if parameters is None else parameters
        sub_request_id = self._allocate_request_id()
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
        if on_object is not None:
            self._pending_object_handlers[sub_request_id] = on_object
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
            parameters=self._joining_fetch_params(parameters,
                                                  fetch_parameters),
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

    @staticmethod
    def _joining_fetch_params(parameters: Dict[int, Any],
                              fetch_parameters: Optional[Dict[int, Any]]
                              ) -> Dict[int, Any]:
        params = {k: v for k, v in parameters.items()
                  if k == ParamType.AUTH_TOKEN}
        params.update(fetch_parameters or {})
        return params

    def joining_fetch(
        self,
        subscription_request_id: int,
        joining_start: int = 0,
        fetch_type: FetchType = FetchType.RELATIVE_JOINING,
        subscriber_priority: Optional[int] = None,
        group_order: Optional[GroupOrder] = None,
        parameters: Optional[Dict[int, bytes]] = None,
        wait_response: Optional[bool] = False,
    ) -> Optional[MOQTMessage]:
        """Joining FETCH (§10.12.2) against a subscription already made:
        the objects up to its Joining Location, from `joining_start`
        groups back (relative) or from that group (absolute)."""
        if fetch_type not in (FetchType.RELATIVE_JOINING,
                              FetchType.ABSOLUTE_JOINING):
            raise ValueError(
                f"joining_fetch() requires a joining fetch_type, "
                f"got {fetch_type}")
        if self._profile.fetch_location_filter:
            # d20: a REQUEST_UPDATE with FILL_PARAMETERS; its fill fetch
            # stream carries the update's Request ID.
            fill = {ParamType.LOCATION_FILTER: self._joining_filter(
                fetch_type, joining_start)}
            if subscriber_priority is not None:
                fill[ParamType.SUBSCRIBER_PRIORITY] = subscriber_priority
            if group_order not in (None, GroupOrder.PUBLISHER_DEFAULT):
                fill[ParamType.GROUP_ORDER] = group_order
            params = dict(parameters or {})
            params[D20ParamType.FILL_PARAMETERS] = fill
            return self.request_update(subscription_request_id,
                                       parameters=params,
                                       wait_response=bool(wait_response))
        request_id = self._allocate_request_id()
        message = Fetch(
            request_id=request_id,
            fetch_type=fetch_type,
            subscriber_priority=subscriber_priority,
            group_order=group_order,
            joining_request_id=subscription_request_id,
            joining_start=joining_start,
            parameters=dict(parameters or {}),
        )
        self._subscriptions[request_id] = [message]
        self._fetch_done_futures[request_id] = self._loop.create_future()
        logger.info(f"MOQT send: {self._msgstr(message)}")
        self._send_request(request_id, message)
        if not wait_response:
            return message
        return self._await_response(request_id)

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
        on_object: Optional[Callable] = None,
        location_filter: Optional[LocationFilter] = None,
    ) -> Optional[MOQTMessage]:
        """Standalone FETCH of a range of objects from a track.

        Per spec §9.16.1: End Location.Object of 0 means the entire end
        group is requested. From d20 the range travels as LOCATION_FILTER
        with an inclusive End: the arguments are converted, or
        `location_filter` is sent as is. `on_object` receives this
        FETCH's objects (register_fetch_object_handler), registered
        before it is sent.
        """
        if location_filter is not None and \
                not self._profile.fetch_location_filter:
            raise ValueError(
                f"location_filter needs draft-20 or later, not "
                f"draft-{self.negotiated_draft}")
        parameters = {} if parameters is None else parameters
        self._check_filter_ranges(parameters)
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
            location_filter=location_filter,
        )
        self._subscriptions[request_id] = [message]
        self._fetch_done_futures[request_id] = \
            self._loop.create_future()
        if on_object is not None:
            self.register_fetch_object_handler(request_id, on_object)
        logger.info(f"MOQT send: {self._msgstr(message)}")
        # FETCH is a request opener: at d18 it must open its own bidi
        # request stream (like SUBSCRIBE/PUBLISH); pre-d18 _send_request
        # falls back to the single control stream.
        self._send_request(request_id, message)

        if not wait_response:
            return message
        if on_object is not None:
            return self._await_routed(request_id)
        return self._await_response(request_id)

    @staticmethod
    def _joining_filter(fetch_type: int, joining_start: int) -> LocationFilter:
        """A joining range as a d20 fill Location Filter: relative counts
        groups back from the current one, absolute names the first."""
        if fetch_type == FetchType.RELATIVE_JOINING:
            return LocationFilter.relative((joining_start or 0) + 1)
        return LocationFilter.absolute(joining_start or 0, 0)

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
        logger.info(f"MOQT send: {self._msgstr(message)}")
        # FETCH_OK is a response: at d18 it returns on the request's own
        # bidi stream; pre-d18 _send_reply falls back to the control stream.
        self._send_reply(request_id, message)
        return message

    async def serve_fetch(self, request_id: int, objects, *,
                          group_order: int = GroupOrder.ASCENDING,
                          fin: bool = True) -> int:
        """Serve a FETCH's objects on its own uni stream (§10.13).

        Opens the fetch data stream, writes FETCH_HEADER, then each
        FetchObject in ascending order as a delta-coded object, then
        FINs unless `fin=False` (the caller then owns the FIN).
        `objects` is any iterable of FetchObject; where FETCH carries no
        Object Status (d20+), objects with one are skipped. Call
        fetch_ok() first (its End Location must be known). Returns the
        stream id. Cancelled mid-way, it resets the stream (§5.2); a
        failure resets it too."""
        stream_id = await self.open_uni_stream()
        header = FetchHeader(request_id=request_id)
        prior = None
        with_status = self._profile.fetch_object_status
        if self._served_fetch_streams is None:
            self._served_fetch_streams = {}
        self._served_fetch_streams[request_id] = stream_id
        try:
            await self.stream_write_drain(
                stream_id, header.serialize(prof=self._profile).data)
            for obj in objects:
                if (not with_status and obj.end_of_range is None
                        and obj.status not in (None, ObjectStatus.NORMAL)):
                    continue
                buf = obj.serialize(prof=self._profile, prior=prior,
                                    group_order=int(group_order))
                await self.stream_write_drain(stream_id, buf.data)
                if self._served_fetch_streams.get(request_id) != stream_id:
                    return stream_id  # FETCH_CANCEL reset it
                if obj.end_of_range is None:
                    prior = obj
                else:
                    # §11.4.4.2: the marker is the prior for Group/Object
                    # deltas; Subgroup/Priority carry over only from a real
                    # object (poisoned otherwise so later refs stay
                    # explicit).
                    prior = FetchObject(
                        group_id=obj.group_id, object_id=obj.object_id,
                        subgroup_id=(prior.subgroup_id if prior else -1),
                        publisher_priority=(prior.publisher_priority
                                            if prior else -1))
        except asyncio.CancelledError:
            self.stream_reset(stream_id, StreamResetCode.CANCELLED)
            raise
        except BaseException:
            self._served_fetch_streams.pop(request_id, None)
            self.stream_reset(stream_id, StreamResetCode.INTERNAL_ERROR)
            raise
        if fin:
            self.stream_fin(stream_id)
        return stream_id

    async def serve_fill(self, msg, history, largest, *,
                         subscription_filter: Optional[LocationFilter] = None,
                         group_order: Optional[int] = None,
                         ) -> Optional[int]:
        """Serve the fill fetch stream a SUBSCRIBE or REQUEST_UPDATE
        carrying FILL_PARAMETERS asks for (d20 §5.1.3), from `history`
        (an ObjectHistory) given the Largest Object `largest`. The stream
        carries `msg`'s Request ID. `subscription_filter` is the
        subscription's Location Filter, the fill range when
        FILL_PARAMETERS has none; `group_order` its group order. Under
        FILL_TIMEOUT, Objects the history no longer holds are reported
        as End of Timed-Out Range, else as End of Unknown Range. Returns
        the stream id, None when no fill stream opens (no
        FILL_PARAMETERS, or an empty range). Call it only while the
        subscription is not paused."""
        params = getattr(msg, 'parameters', None) or {}
        fill = params.get(D20ParamType.FILL_PARAMETERS)
        if fill is None:
            return None
        if subscription_filter is None:
            subscription_filter = getattr(msg, 'location_filter', None)
        rng = fill_range(fill, subscription_filter, largest)
        if rng is None:
            return None
        order = (fill.get(ParamType.GROUP_ORDER) or group_order
                 or GroupOrder.ASCENDING)
        timed_out = D20ParamType.FILL_TIMEOUT in fill
        objects = history.fetch(
            *rng, self, descending=order == GroupOrder.DESCENDING,
            lost_marker=(FETCH_FLAGS_END_TIMED_OUT if timed_out
                         else FETCH_FLAGS_END_UNKNOWN))
        return await self.serve_fetch(msg.request_id, objects,
                                      group_order=int(order))

    def fetch_error(
        self,
        request_id: int,
        error_code: int = 0,
        reason: str = "Internal error",
    ) -> Optional[MOQTMessage]:
        """Create and send a negative response to a FETCH.

        d14 sends FETCH_ERROR (0x19); d16+ dropped it — the reject is
        the universal REQUEST_ERROR, on the request's stream at d18. A
        named code is encoded for the negotiated draft."""
        error_code = request_error_code(error_code, self._profile,
                                        fetch=True)
        if is_draft16_or_later(self.negotiated_draft):
            message = RequestError(
                request_id=request_id,
                error_code=error_code,
                retry_interval=0,
                reason=reason,
            )
            logger.info(f"MOQT send: {self._msgstr(message)}")
            self._send_reply(request_id, message, fin=True)
        else:
            message = FetchError(
                request_id=request_id,
                error_code=error_code,
                reason=reason,
            )
            logger.info(f"MOQT send: {self._msgstr(message)}")
            self._send_reply(request_id, message)
        return message

    def publish_namespace(
        self,
        namespace: Union[str, Tuple[str, ...]],
        parameters: Optional[Dict[int, bytes]] = None,
        wait_response: Optional[bool] = False
    ) -> Optional[MOQTMessage]:
        """PublishNamespace track namespace availability."""
        namespace_tuple = self._make_namespace_tuple(namespace)
        self._check_sendable_namespace(namespace_tuple)
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
        logger.info(f"MOQT send: {self._msgstr(message)}")
        self._send_request(request_id, message)
        self._announced[request_id] = namespace_tuple
        self._report_namespace(namespace_tuple, withdrawn=False)

        if not wait_response:
            return message

        return self._await_response(request_id)

    def _check_sendable_namespace(self, namespace: Tuple[bytes, ...]) -> None:
        """Raise ValueError for a reserved namespace (first field starting
        with "."; d18 §2.4.1, d22 §2.4.3 and §6.5)."""
        prof = getattr(self, '_profile', None)
        if (prof is not None and prof.reserved_period_namespaces
                and namespace and namespace[0].startswith(b".")):
            raise ValueError(
                f"namespace {namespace[0].decode(errors='replace')!r}/...: "
                f"a first field starting with '.' is reserved")

    def is_announced(self, namespace: Union[str, Tuple[str, ...]]) -> bool:
        """True while a PUBLISH_NAMESPACE we sent for `namespace` is
        neither withdrawn nor refused."""
        return self._make_namespace_tuple(namespace) in self._announced.values()

    def _our_namespaces(self) -> set:
        """Namespaces this session announces or serves tracks under."""
        return (set(self._announced.values())
                | {key[0] for key in self._published_tracks})

    def _report_namespace(self, namespace: tuple, withdrawn: bool) -> None:
        """NAMESPACE (or NAMESPACE_DONE) to each discovery subscriber whose
        prefix covers `namespace`."""
        for rid, prefix in list(self._discovery_subs.items()):
            if namespace[:len(prefix)] != prefix:
                continue
            suffix = namespace[len(prefix):]
            try:
                if withdrawn:
                    self.namespace_done(suffix, request_id=rid)
                else:
                    self.namespace(suffix, request_id=rid)
            except Exception:
                logger.debug("namespace report failed", exc_info=True)

    def publish(
        self,
        namespace: Union[str, Tuple[str, ...]],
        track_name: str,
        forward: int = 0,
        content_exists: int = 0,
        parameters: Optional[Dict[int, Any]] = None,
        wait_response: Optional[bool] = False,
        largest: Optional[Tuple[int, int]] = None,
        track_properties: Optional[Dict[int, Any]] = None,
    ) -> Optional[MOQTMessage]:
        """PUBLISH — announce a specific track to the relay/subscriber.

        Args:
            forward: 0 = announce availability only, 1 = start data.
                     Subscriber responds with PUBLISH_OK(forward=1)
                     to request data flow.
            largest: the track's Largest Location (group, object), sent
                     as LARGEST_OBJECT; required once objects have been
                     published (§10.2.11).
            track_properties: Track Properties (d16+; dropped at d14).

        From d20 a PUBLISH under the prefix of a SUBSCRIBE_TRACKS the
        peer sent carries that request's subscription parameters, its
        FORWARD (1 when absent) overriding `forward` (§10.20.1).
        """
        namespace_tuple = self._make_namespace_tuple(namespace)
        self._check_sendable_namespace(namespace_tuple)
        carried = (self._subscribe_tracks_carry(namespace_tuple)
                   if self._subscribe_tracks_params else None)
        if carried is not None:
            st_forward = int(carried.pop(ParamType.FORWARD, 1))
            if st_forward != forward:
                logger.info(f"MOQT: PUBLISH forward={st_forward} from "
                            f"SUBSCRIBE_TRACKS, not {forward}")
            forward = st_forward
            parameters = {**(parameters or {}), **carried}
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
            content_exists=1 if largest is not None else content_exists,
            largest_group_id=None if largest is None else largest[0],
            largest_object_id=None if largest is None else largest[1],
            parameters=parameters or {},
            track_extensions=track_properties,
        )
        logger.info(f"MOQT send: {self._msgstr(message)}")
        # Pre-register the response future before send (like subscribe /
        # fetch / publish_namespace) so a fast peer ack can't resolve
        # before the awaiter registers — a race on loopback.
        if wait_response:
            self._pending_requests[request_id] = self._loop.create_future()
        self._send_request(request_id, message)

        if not wait_response:
            return message

        return self._await_response(request_id)

    def _subscribe_tracks_carry(self, namespace: tuple
                                ) -> Optional[Dict[int, Any]]:
        """The parameters of the peer's active SUBSCRIBE_TRACKS whose
        prefix covers `namespace`, else None."""
        for rid, params in list(self._subscribe_tracks_params.items()):
            held = self._peer_requests.get(rid)
            if held is None or held[0] is not SubscribeTracks:
                del self._subscribe_tracks_params[rid]
                continue
            prefix = held[1]
            if namespace[:len(prefix)] == prefix:
                return dict(params)
        return None

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
        logger.info(f"MOQT send: {self._msgstr(message)} request_id: {msg.request_id} namespace: {msg.namespace}")
        # PUBLISH_NAMESPACE is "Request, First" at d18: it opens its own
        # bidi stream and the reply returns on that stream, carrying no
        # Request ID. Sending it on the control stream instead left the
        # peer unable to correlate it, so the announce never resolved.
        self._send_reply(msg.request_id, message)
        return message

    def publish_namespace_done(
        self,
        namespace: Optional[Union[str, Tuple[str, ...]]] = None,
        request_id: int = None,
    ) -> Optional[MOQTMessage]:
        """Withdraw a track namespace announcement.

        Draft-14 takes the namespace, draft-16 the request_id of
        the original PUBLISH_NAMESPACE, and both send
        PUBLISH_NAMESPACE_DONE. Draft-18 has no such message: a request
        owns a bidirectional stream and is cancelled by abruptly
        terminating it (§3.3.2), so the withdrawal is a RESET_STREAM on
        the announce's own stream and no message is sent. Returns None
        in that case.
        """
        if request_id is None and namespace is not None:
            wanted = self._make_namespace_tuple(namespace)
            request_id = next((rid for rid, ns in self._announced.items()
                               if ns == wanted), None)
        withdrawn = self._announced.pop(request_id, None)
        if withdrawn is not None:
            self._report_namespace(withdrawn, withdrawn=True)
        if self.negotiated_draft >= 18:
            stream_id = self._bidi_streams.get(request_id)
            if stream_id is None:
                logger.debug(
                    f"d18 namespace withdraw: no request stream for "
                    f"request_id={request_id}; nothing to reset")
                return None
            logger.info(f"MOQT: d18 namespace withdraw: reset request "
                        f"stream {stream_id} (request_id={request_id})")
            self.stream_reset(stream_id, StreamResetCode.CANCELLED)
            self._bidi_streams.pop(request_id, None)
            self._fail_waiting_updates(request_id, "withdrawn")
            return None
        namespace = (withdrawn if namespace is None
                     else self._make_namespace_tuple(namespace))
        message = PublishNamespaceDone(namespace=namespace, request_id=request_id)
        logger.info(f"MOQT send: {self._msgstr(message)}")
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
        logger.info(f"MOQT send: {self._msgstr(message)}")

        if self._profile.control_uni_pair:
            self._note_request_kind(request_id, SubscribeNamespace, ours=True)
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
        """Send a positive response to SUBSCRIBE_NAMESPACE.

        Draft-14 sends SubscribeNamespaceOk on code point 0x12; d16
        removed that type, so d16+ acks with REQUEST_OK and replies on
        the bidi stream the request arrived on.

        The ack alone reports no namespaces: in d18 the application
        follows it with namespace() per namespace it serves.
        """
        if not is_draft16_or_later(self.negotiated_draft):
            message = SubscribeNamespaceOk(request_id=msg.request_id)
            logger.info(f"MOQT send: {self._msgstr(message)}")
            self.send_control_message(message)
            return message
        message = RequestOk(request_id=msg.request_id)
        logger.info(f"MOQT send: {self._msgstr(message)}")
        if stream_id is not None:
            self.send_stream_message(stream_id, message)
        else:
            self._send_on_request_stream(msg.request_id, message)
        return message

    def subscribe_tracks_ok(
        self,
        msg: 'SubscribeTracks',
        stream_id: int = None,
    ) -> Optional[MOQTMessage]:
        """Send a positive response to SUBSCRIBE_TRACKS (d18).

        The ack reports no tracks: the application follows it with a
        PUBLISH per track in the requested namespace.
        """
        message = RequestOk(request_id=msg.request_id)
        logger.info(f"MOQT send: {self._msgstr(message)}")
        if stream_id is not None:
            self.send_stream_message(stream_id, message)
        else:
            self._send_on_request_stream(msg.request_id, message)
        return message

    def redirect(
        self,
        msg: MOQTMessage,
        namespace: Optional[Tuple[bytes, ...]] = None,
        track_name: bytes = b"",
        connect_uri: bytes = b"",
        reason: str = "redirect",
    ) -> RequestError:
        """Refuse a peer's request with REDIRECT (d18+, §10.6.1). Empty
        namespace and track name retry the same name; an empty
        connect_uri retries on this session. A namespace-scoped request
        takes no track name, and only a server sends a connect_uri."""
        if not self._profile.vi64:
            raise ValueError(
                f"REDIRECT needs draft-18 or later, not "
                f"draft-{self._profile.draft}")
        if (type(msg).__name__ in self._profile.namespace_scoped
                and track_name):
            raise ValueError("a namespace-scoped REDIRECT has no track name")
        if connect_uri and self._is_client:
            raise ValueError("a client REDIRECT has no Connect URI")
        message = RequestError(
            request_id=msg.request_id,
            error_code=RequestErrorCode.REDIRECT,
            retry_interval=0,
            reason=reason,
            redirect=(connect_uri, tuple(namespace or ()), track_name),
        )
        logger.info(f"MOQT send: {self._msgstr(message)}")
        self._send_reply(msg.request_id, message, fin=True)
        return message

    def namespace(
        self,
        namespace_suffix: Union[str, Tuple[bytes, ...]] = (),
        stream_id: int = None,
        request_id: int = None,
    ) -> Optional[MOQTMessage]:
        """Report one namespace under a prefix a peer subscribed to, as
        a suffix relative to that prefix — an empty suffix means the
        prefix itself is the namespace.

        d18 discovery is two-level: this answers SUBSCRIBE_NAMESPACE
        (which namespaces exist), and the peer then asks each one for
        its tracks with SUBSCRIBE_TRACKS. It rides the SUBSCRIBE_NAMESPACE
        request stream, after subscribe_namespace_ok(): pass that
        request's id (or its stream id directly).
        """
        if isinstance(namespace_suffix, str):
            suffix = (self._make_namespace_tuple(namespace_suffix)
                      if namespace_suffix else ())
        else:
            suffix = tuple(namespace_suffix)
        if not is_draft16_or_later(self.negotiated_draft):
            raise MOQTException(
                SessionCloseCode.INTERNAL_ERROR,
                f"NAMESPACE is not defined by draft-{self.negotiated_draft}")
        message = Namespace(namespace_suffix=suffix)
        logger.info(f"MOQT send: {self._msgstr(message)}")
        if stream_id is not None:
            self.send_stream_message(stream_id, message)
        elif request_id is not None:
            self._send_on_request_stream(request_id, message)
        else:
            raise ValueError(
                "namespace() needs the SUBSCRIBE_NAMESPACE request_id "
                "or stream_id: NAMESPACE is not a control-stream message")
        return message

    def namespace_done(
        self,
        namespace_suffix: Union[str, Tuple[bytes, ...]] = (),
        stream_id: int = None,
        request_id: int = None,
    ) -> Optional[MOQTMessage]:
        """Withdraw a namespace previously reported with namespace().

        The counterpart to NAMESPACE, on the same SUBSCRIBE_NAMESPACE
        request stream and carrying the same suffix. Without it a peer
        that learned of a namespace keeps believing in it after the
        publisher withdraws or its session ends.
        """
        if isinstance(namespace_suffix, str):
            suffix = (self._make_namespace_tuple(namespace_suffix)
                      if namespace_suffix else ())
        else:
            suffix = tuple(namespace_suffix)
        if not is_draft16_or_later(self.negotiated_draft):
            raise MOQTException(
                SessionCloseCode.INTERNAL_ERROR,
                f"NAMESPACE_DONE is not defined by "
                f"draft-{self.negotiated_draft}")
        message = NamespaceDone(namespace_suffix=suffix)
        logger.info(f"MOQT send: {self._msgstr(message)}")
        if stream_id is not None:
            self.send_stream_message(stream_id, message)
        elif request_id is not None:
            self._send_on_request_stream(request_id, message)
        else:
            raise ValueError(
                "namespace_done() needs the SUBSCRIBE_NAMESPACE "
                "request_id or stream_id")
        return message

    async def subscribe_tracks(
        self,
        namespace: Union[str, Tuple[bytes, ...]],
        parameters: Optional[Dict[int, bytes]] = None,
        wait_response: Optional[bool] = False,
    ) -> Optional[MOQTMessage]:
        """d18 SUBSCRIBE_TRACKS (0x51) — enumerate the tracks in ONE
        namespace.

        d18 splits discovery in two: SUBSCRIBE_NAMESPACE reports which
        namespaces exist under a prefix (NAMESPACE messages), and this
        asks a specific namespace for its tracks, answered with PUBLISH.
        Pre-d18 the first request did both, which obliged a relay to
        push a PUBLISH for every track under a broad prefix.
        """
        if not self._profile.two_level_discovery:
            raise MOQTException(
                SessionCloseCode.INTERNAL_ERROR,
                f"SUBSCRIBE_TRACKS is not defined by "
                f"draft-{self.negotiated_draft}")
        if parameters is None:
            parameters = {}
        ns = self._make_namespace_tuple(namespace)
        request_id = self._allocate_request_id()
        message = SubscribeTracks(
            request_id=request_id,
            namespace_prefix=ns,
            parameters=parameters,
        )
        logger.info(f"MOQT send: {self._msgstr(message)}")
        if self._profile.control_uni_pair:
            self._note_request_kind(request_id, SubscribeTracks, ours=True)
        stream_id = await self.open_bidi_stream()
        self.send_stream_message(stream_id, message)
        self._bidi_streams[request_id] = stream_id
        self._bidi_stream_requests[stream_id] = request_id
        if not wait_response:
            return message
        return await self._await_response(request_id)

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

    def goaway(self, new_session_uri: str = "", timeout: int = 0,
               request_id: Optional[int] = None) -> GoAway:
        """Announce imminent session close / migration (§10.4).

        Servers MAY carry a New Session URI; clients MUST send an empty
        one. At d18 the control-stream form names the smallest peer
        request id that might not have been processed. With `request_id`
        (d18+) the GOAWAY rides that request's stream and migrates only
        that request."""
        if self._is_client and new_session_uri:
            raise ValueError("clients MUST send an empty New Session URI")
        msg = GoAway(new_session_uri=new_session_uri, timeout=timeout)
        if request_id is not None:
            if not self._profile.control_uni_pair:
                raise ValueError(
                    f"a request GOAWAY needs draft-18 or later, not "
                    f"draft-{self.negotiated_draft}")
            logger.info(f"MOQT send: {self._msgstr(msg)} on request {request_id}")
            self._send_on_request_stream(request_id, msg)
            return msg
        if self.negotiated_draft >= 18 and self._profile.goaway_request_id:
            if self._peer_request_max < 0:
                msg.request_id = 0 if not self._is_client else 1
            else:
                msg.request_id = self._peer_request_max + 2
        logger.info(f"MOQT send: {self._msgstr(msg)}")
        self.send_control_message(msg)
        self._goaway_sent = True
        return msg

    def unsubscribe_namespace(
        self,
        namespace_prefix: str = None,
        request_id: int = None,
    ) -> Optional[MOQTMessage]:
        """Withdraw a namespace-prefix subscription.

        d14 sends UNSUBSCRIBE_NAMESPACE (0x14) with the prefix; d16+
        removed it — the subscription is cancelled by terminating the
        SUBSCRIBE_NAMESPACE request's own stream (§3.3.2). Returns None
        then."""
        if is_draft16_or_later(self.negotiated_draft):
            stream_id = self._bidi_streams.get(request_id)
            if stream_id is None:
                logger.debug(
                    f"unsubscribe_namespace: no request stream for "
                    f"request_id={request_id}; nothing to reset")
                return None
            logger.info(f"MOQT: unsubscribe namespace: reset request "
                        f"stream {stream_id} (request_id={request_id})")
            self.stream_reset(stream_id, StreamResetCode.CANCELLED)
            self.stream_stop_sending(stream_id, StreamResetCode.CANCELLED)
            self._bidi_streams.pop(request_id, None)
            self._fail_waiting_updates(request_id, "cancelled")
            return None
        prefix = self._make_namespace_tuple(namespace_prefix)
        message = UnsubscribeNamespace(namespace_prefix=prefix)
        logger.info(f"MOQT send: {self._msgstr(message)}")
        self.send_control_message(message)
        return message


    ###############################################################################################
    #  Inbound MoQT message handlers                                                              #
    ###############################################################################################
    
    def register_handler(self, msg_type: int, handler: Callable) -> None:
        """Register a custom handler, overriding the default handler for
        this message type on this session. A message addressed to a
        track the session publishes goes to that track instead.

        Registration is expanded over HANDLER_ALIASES so a type that is
        renumbered between drafts stays registered under every number.
        """
        self._control_msg_overrides[msg_type] = handler
        for alias in HANDLER_ALIASES.get(msg_type, ()):
            self._control_msg_overrides[alias] = handler
    
    async def _handle_server_setup(self, msg: ServerSetup) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")

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
                logger.debug(f"MOQT event: d16+ ServerSetup (version from {_src}: draft-{selected_draft})")
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
                self._settle_draft(selected_draft)
            if self._refuse_setup_location(msg.parameters):
                return
            self._ingest_request_limit(msg.parameters)

            # indicate moqt session setup is complete
            self._moqt_session_setup.set_result(True)

    async def _handle_client_setup(self, msg: ClientSetup) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
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
            if self._refuse_setup_location(msg.parameters):
                return
            if version_ok:
                self._ingest_request_limit(msg.parameters)
                self.server_setup()
                self._moqt_session_setup.set_result(True)
                self._log_setup_complete()
            else:
                self._close_session(
                    SessionCloseCode.VERSION_NEGOTIATION_FAILED,
                    "no mutually supported MOQT version")

    async def _handle_subscribe(self, msg: Subscribe) -> None:
        """Refuse a SUBSCRIBE no published track or app handler took."""
        logger.debug(f"MOQT receive: {self._msgstr(msg)}")
        self.subscribe_error(msg.request_id,
                             error_code=RequestErrorCode.DOES_NOT_EXIST,
                             reason="track does not exist")

    async def _handle_publish_namepace(self, msg: PublishNamespace) -> None:
        logger.debug(f"MOQT receive: {self._msgstr(msg)}")
        self.publish_namepace_ok(msg)

    async def _handle_subscribe_update(self, msg: SubscribeUpdate) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        # Handle subscription update

    async def _handle_subscribe_ok(self, msg: SubscribeOk) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        if msg.request_id in self._subscriptions:
            self._subscriptions[msg.request_id].append(msg)
        # The publisher's alias assignment is authoritative — SUBSCRIBE
        # carries no Track Alias; the registry is keyed from the reply.
        if msg.track_alias is not None:
            self._track_aliases[msg.track_alias] = msg.request_id
            self._resolve_unbound_alias(msg.track_alias)
            self._latch_track_priority(msg.track_alias,
                                       msg.track_extensions)
        self._resolve_request(msg.request_id, msg)

    async def _handle_subscribe_error(self, msg: SubscribeError) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        if msg.request_id in self._subscriptions:
            self._subscriptions[msg.request_id].append(msg)
        self._resolve_request(msg.request_id, msg)

    async def _handle_publish_namepace_ok(self, msg: PublishNamespaceOk) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        self._resolve_request(msg.request_id, msg)

    async def _handle_publish_namepace_error(self, msg: PublishNamespaceError) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        self._resolve_request(msg.request_id, msg)

    async def _handle_publish_namepace_done(self, msg: PublishNamespaceDone) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        # Publisher is releasing the namespace — for subscribers whose
        # only interest is this namespace, that's a clean end-of-track
        # signal. Close the session so bench tools exit promptly
        # instead of hanging for the QUIC idle timeout.
        self._close_session(SessionCloseCode.NO_ERROR,
                            "publisher namespace done")

    async def _handle_publish_namepace_cancel(self, msg: PublishNamespaceCancel) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        # Handle announcement cancellation

    async def _handle_unsubscribe(self, msg: Unsubscribe) -> None:
        """Publisher-side: subscriber wants out. Retire the alias we
        issued and notify the owner, which owns the data streams.
        Receive-side state is keyed by the peer's aliases, a different
        number space, and is left alone."""
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        self._subscriptions.pop(msg.request_id, None)
        # d18 has no UNSUBSCRIBE; there the same news arrives as a
        # terminated request stream. Both reach the owner the same way.
        self._notify_request_cancelled(msg.request_id, "UNSUBSCRIBE")

    # §10.11: objects and late-opening streams can follow PUBLISH_DONE.
    # How long a finished subscription's track keeps receiving them.
    PUBLISH_DONE_GRACE_S = 5.0

    async def _handle_subscribe_done(self, msg: SubscribeDone) -> None:
        """Subscriber-side: publisher signals end-of-subscription. The
        track keeps receiving for PUBLISH_DONE_GRACE_S, then whatever is
        still open is released."""
        logger.debug(f"MOQT event: handle SubscribeDone "
                     f"request_id={msg.request_id} "
                     f"status={msg.status_code} "
                     f"streams={msg.stream_count}")
        self._retire_alias(msg.request_id)
        track_alias = next(
            (ta for ta, rid in self._track_aliases.items()
             if rid == msg.request_id),
            None)
        if track_alias is not None:
            self._loop.call_later(self.PUBLISH_DONE_GRACE_S,
                                  self._release_finished_track,
                                  track_alias, msg.request_id)
        future = self._pending_requests.get(msg.request_id)
        if future and not future.done():
            future.set_result(msg)
        cb = self._publish_done_handlers.pop(msg.request_id, None)
        if cb is not None:
            try:
                cb(msg)
            except Exception:
                logger.debug("publish-done handler raised", exc_info=True)
        # Terminal for that subscription only: the session stays up.
        # Owners learn of it through register_publish_done_handler().
        self._subscriptions.pop(msg.request_id, None)

    def _release_finished_track(self, track_alias: int,
                                request_id: int) -> None:
        """Release a finished subscription's receive state: STOP_SENDING
        any subgroup stream still open so the publisher's write side can
        release cleanly. Skipped once the alias belongs to a newer
        subscription."""
        if self._track_aliases.get(track_alias) != request_id:
            return
        for key, sid in list(self._subgroup_stream_by_key.items()):
            if key[0] == track_alias:
                self.stream_stop_sending(sid, StreamResetCode.CANCELLED)
                # Tombstone: bytes already in flight ahead of the
                # publisher's RESET would otherwise recreate the stream's
                # state with no parser and fail to parse.
                self._mark_stream_torn_down(sid)
        self._track_aliases.pop(track_alias, None)
        self._object_handlers.pop(track_alias, None)
        self._early_stream_ends.pop(track_alias, None)
        self._discard_early_objects(track_alias)
        self._forget_track_bounds(track_alias)

    def _extend_request_credit(self, rid: int) -> None:
        """Raise our MAX_REQUEST_ID before the peer reaches it (§9.5)."""
        limit = self._local_request_max
        if limit is None or rid < limit - self.REQUEST_ID_WINDOW // 2:
            return
        self._local_request_max = limit + self.REQUEST_ID_WINDOW
        self.send_control_message(
            MaxSubscribeId(request_id=self._local_request_max))

    def _ingest_request_limit(self, params) -> None:
        """Peer's MAX_REQUEST_ID Setup parameter (§9.3.1.3); absent is
        0, so nothing is sent until a MAX_REQUEST_ID raises it."""
        if self._profile.control_uni_pair:
            return
        limit = (params or {}).get(SetupParamType.MAX_REQUEST_ID)
        self._peer_request_limit = 0 if limit is None else int(limit)

    async def _handle_max_request_id(self, msg: MaxSubscribeId) -> None:
        """§9.5: the peer raised the ceiling on our request ids; it
        MUST only increase."""
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        new = int(msg.request_id)
        if (self._peer_request_limit is not None
                and new <= self._peer_request_limit):
            self._close_session(SessionCloseCode.PROTOCOL_VIOLATION,
                                f"MAX_REQUEST_ID {new} did not increase")
            return
        self._peer_request_limit = new
        self._requests_blocked_sent = False

    async def _handle_subscribes_blocked(self, msg: SubscribesBlocked) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        # Handle subscribes blocked notification

    async def _handle_track_status(self, msg: TrackStatus) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        if is_draft16_or_later(self.negotiated_draft):
            # §10.14: the receiver MUST answer with exactly one
            # TRACK_STATUS_OK or REQUEST_ERROR. A session with no track
            # knowledge answers honestly rather than hanging the peer;
            # apps override via register_handler(TRACK_STATUS, ...).
            err = RequestError(
                request_id=msg.request_id,
                error_code=int(RequestErrorCode.NOT_SUPPORTED),
                retry_interval=0,
                reason="track status not supported",
            )
            logger.info(f"MOQT send: {err}")
            self._send_reply(msg.request_id, err, fin=True)

    async def _handle_track_status_ok(self, msg: TrackStatusOk) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        self._resolve_request(msg.request_id, msg)

    async def _handle_track_status_error(self, msg: TrackStatusError) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        self._resolve_request(msg.request_id, msg)

    async def _handle_goaway(self, msg: GoAway) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        # §10.4: only a server may carry a New Session URI. One GOAWAY per
        # stream is enforced in _on_control_data.
        if not self._is_client and msg.new_session_uri:
            self._close_session(
                SessionCloseCode.PROTOCOL_VIOLATION,
                "client sent a non-empty New Session URI")
            return

    def _cancelled_unhandled(self, msg: MOQTMessage) -> bool:
        """True when the peer cancelled this d16+ request before its
        handler ran."""
        return (is_draft16_or_later(self.negotiated_draft)
                and msg.request_id not in self._peer_requests)

    async def _handle_subscribe_namespace(self, msg: SubscribeNamespace) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        if self._cancelled_unhandled(msg):
            return
        stream_id = self._bidi_streams.get(msg.request_id)
        logger.debug(f"MOQT event: subscribe_namespace bidi_stream={stream_id} request_id={msg.request_id} bidi_streams={self._bidi_streams}")
        self.subscribe_namespace_ok(msg, stream_id=stream_id)
        if not self._profile.two_level_discovery:
            return
        # §6.1: the namespaces we publish under the prefix, then any we
        # announce or withdraw while it stays open.
        prefix = tuple(msg.namespace_prefix or ())
        rid = msg.request_id
        self._discovery_subs[rid] = prefix
        self.register_request_cancel_handler(
            rid, lambda r: self._discovery_subs.pop(r, None))
        for ns in sorted(self._our_namespaces()):
            if ns[:len(prefix)] == prefix:
                self.namespace(ns[len(prefix):], request_id=rid)

    async def _handle_subscribe_tracks(self, msg: SubscribeTracks) -> None:
        """d18 SUBSCRIBE_TRACKS (0x51) — a subscriber asking one
        namespace for its tracks. Ack it; the application answers with
        PUBLISH per track (the same announcement d14/d16 relays pushed
        unprompted from SUBSCRIBE_NAMESPACE). An app that registers its
        own handler for this type overrides the default ack."""
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        if self._cancelled_unhandled(msg):
            return
        self.subscribe_tracks_ok(msg)

    async def _handle_publish_blocked(self, msg: PublishBlocked) -> None:
        # d18 PUBLISH_BLOCKED (0x0F) — subscriber-side notice that a track
        # cannot be published. Log-only for now.
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")

    async def _handle_subscribe_namespace_ok(self, msg: SubscribeNamespaceOk) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        self._resolve_request(msg.request_id, msg)

    async def _handle_subscribe_namespace_error(self, msg: SubscribeNamespaceError) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        self._resolve_request(msg.request_id, msg)

    async def _handle_unsubscribe_namespace(self, msg: UnsubscribeNamespace) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        # No response required per draft-14
        prefix = tuple(msg.namespace_prefix or ())
        for rid, (kind, key) in list(self._peer_requests.items()):
            if kind is SubscribeNamespace and key == prefix:
                del self._peer_requests[rid]

    async def _handle_publish(self, msg: Publish) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        self._publish_announcements.put_nowait(msg)

    async def _handle_publish_ok(self, msg: PublishOk) -> None:
        """Subscriber accepted our PUBLISH.

        Pre-d18 this arrives as its own control message rather than a
        REQUEST_OK on the request's stream, so nothing else resolves
        the sender's future — without this an awaited publish() waits
        out its whole timeout on an offer that was accepted."""
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        self._resolve_request(msg.request_id, msg)

    async def _handle_publish_error(self, msg: PublishError) -> None:
        """Subscriber rejected our PUBLISH; the awaiter raises."""
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        self._unbind_request(msg.request_id)
        self._resolve_request(msg.request_id, msg)

    async def _handle_fetch(self, msg: Fetch) -> None:
        """Default handler for a FETCH no published track serves.

        A joining FETCH names no subscription of ours
        (INVALID_JOINING_REQUEST_ID, §10.12.2); a standalone one is
        refused NOT_SUPPORTED. Override via register_handler(FETCH, ...)
        to serve others (see serve_fetch())."""
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        if _is_joining(msg.fetch_type):
            code, reason = (RequestErrorCode.INVALID_JOINING_REQUEST_ID,
                            "no such subscription")
        else:
            code, reason = (RequestErrorCode.NOT_SUPPORTED,
                            "fetch not supported")
        self.fetch_error(request_id=msg.request_id, error_code=code,
                         reason=reason)

    async def _handle_fetch_cancel(self, msg: FetchCancel) -> None:
        """d14/d16 FETCH_CANCEL for a fetch we serve: reset its stream
        and tell the request's owner."""
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        stream_id = (self._served_fetch_streams or {}).pop(msg.request_id,
                                                           None)
        if stream_id is not None:
            self.stream_reset(stream_id, StreamResetCode.CANCELLED)
            logger.debug(f"MOQT stream({stream_id}): reset on FETCH_CANCEL "
                         f"for request_id={msg.request_id}")
        self._notify_request_cancelled(msg.request_id, "FETCH_CANCEL")

    async def _handle_fetch_ok(self, msg: FetchOk) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        self._resolve_request(msg.request_id, msg)

    async def _handle_fetch_error(self, msg: FetchError) -> None:
        """Subscriber-side: FETCH_ERROR received for a fetch we issued.

        If the publisher already opened a fetch uni stream (legitimate
        per spec §9.16.3 — FETCH_OK/ERROR can arrive at any time relative
        to object delivery), send STOP_SENDING to release the peer's
        write side. Then resolve the pending request future with the
        error.
        """
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        stream_id = self._fetch_stream_by_request.pop(msg.request_id, None)
        if stream_id is not None:
            state = self._data_streams.get(stream_id)
            if state is not None:
                state.key = None
            self.stream_stop_sending(stream_id, StreamResetCode.CANCELLED)
            logger.debug(f"MOQT stream({stream_id}): stop_sending on "
                         f"FETCH_ERROR request_id={msg.request_id}")
        self._resolve_request(msg.request_id, msg)

    # Data handlers need full update - stream reader in progress
    async def _handle_subgroup_header(self, msg: SubgroupHeader, buf: Buffer) -> None:
        """Handle subgroup header message."""
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
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
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        return

    async def _handle_request_ok(self, msg: RequestOk) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        status = msg.request_id in self._track_status_requests
        self._track_status_requests.discard(msg.request_id)
        if msg.track_properties and not status:
            # §10.5: Track Properties belong to TRACK_STATUS_OK only.
            self._close_session(
                SessionCloseCode.PROTOCOL_VIOLATION,
                "Track Properties in a REQUEST_OK that is not TRACK_STATUS_OK")
            return
        self._resolve_request(msg.request_id, msg)

    async def _handle_request_error(self, msg: RequestError) -> None:
        logger.debug(f"MOQT event: handle {self._msgstr(msg)}")
        self._track_status_requests.discard(msg.request_id)
        self._resolve_request(msg.request_id, msg)
        kind = self._request_kinds.get(msg.request_id)
        if (msg.redirect is not None and msg.redirect[2] and kind is not None
                and kind[1] and kind[0].__name__ in self._profile.namespace_scoped):
            # §10.6.1: no Track Name in a namespace-scoped Redirect.
            self._close_session(
                SessionCloseCode.PROTOCOL_VIOLATION,
                f"Redirect names a track for a {kind[0].__name__}")

    async def _handle_namespace(self, msg) -> None:
        logger.debug(f"MOQT event: handle Namespace: {self._msgstr(msg)}")
        self._namespace_announcements.put_nowait(msg)

    async def _handle_namespace_done(self, msg) -> None:
        logger.debug(f"MOQT event: handle NamespaceDone: {self._msgstr(msg)}")

    async def _handle_request_update(self, msg: RequestUpdate) -> None:
        logger.debug(f"MOQT event: handle RequestUpdate: {self._msgstr(msg)}")
        d20 = self._profile.param_table >= 20
        existing = msg.existing_request_id
        held = self._peer_requests.get(existing)
        if d20 and held is not None and held[0] in (SubscribeNamespace,
                                                    SubscribeTracks):
            # A new prefix and SUBSCRIBE_TRACKS FORWARD were applied at
            # admission (§10.9.2, §10.2.18).
            if existing in self._discovery_subs:
                self._discovery_subs[existing] = held[1]
            self._send_reply(msg.request_id,
                             RequestOk(request_id=msg.request_id))
            return
        # §10.9: MUST answer with exactly one REQUEST_OK or
        # REQUEST_ERROR — a session with no update semantics answers
        # honestly instead of hanging the peer; apps override.
        err = RequestError(
            request_id=msg.request_id,
            error_code=int(RequestErrorCode.NOT_SUPPORTED),
            retry_interval=0,
            reason="request update not supported",
        )
        self._send_reply(msg.request_id, err)
        if d20:
            # d20 §10.9.1: a failed update ends the request it updated.
            self._update_failed(msg)

    async def _handle_d18_setup(self, msg) -> None:
        """draft-18 SETUP receive (§10.3). SETUP is symmetric: each peer
        sends its own SETUP on its control write-uni (the client at
        session init, the server on binding the control read-uni). On
        receipt of the peer's SETUP the session is established."""
        logger.debug(f"MOQT event: handle d18 Setup: {self._msgstr(msg)}")
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
        opts = getattr(msg, 'options', None) or {}
        if self._refuse_setup_location(opts):
            return
        # Server side has no separate session-init step: bring up our own
        # control write-uni and send SETUP on first receipt of the peer's
        # SETUP. The client brings its write-uni up eagerly in
        # client_session_init.
        if not self._is_client and self._d18_control_write_sid is None:
            # Transport-aware: raw QUIC allocates a uni id synchronously,
            # WT round-trips via create_stream. open_uni_stream handles both.
            self._d18_control_write_sid = await self.open_uni_stream()
            self._prioritise_control_stream(self._d18_control_write_sid)
            logger.debug(
                f"MOQT: d18 server control write-uni: "
                f"{self._d18_control_write_sid}")
            self.send_control_message(self._setup_message(
                self._implementation_option()))
            # Replies that raced the write-uni bring-up were deferred;
            # SETUP is on the wire, so release them now.
            self._flush_pending_control()
        if not self._moqt_session_setup.done():
            self._moqt_session_setup.set_result(True)
            if not self._is_client:
                self._log_setup_complete()

    async def _handle_d20_setup(self, msg) -> None:
        """d20+ SETUP: the d18 exchange, after recording the peer's d20
        limits (absent = 0)."""
        if not self._d18_setup_seen:
            opts = msg.options or {}
            self._peer_max_request_updates = int(
                opts.get(SetupParamType.MAX_REQUEST_UPDATES, 0))
            self._peer_max_filter_ranges = int(
                opts.get(SetupParamType.MAX_FILTER_RANGES, 0))
            if msg.ignored:
                logger.debug(
                    f"MOQT: ignored unknown Setup Options "
                    f"{[hex(t) for t in msg.ignored]}")
        await self._handle_d18_setup(msg)

    async def _handle_publish_state_notify(self, msg) -> None:
        """PUBLISH_STATE_NOTIFY (d20 §10.10) is informative and never
        answered; register_handler() takes it to act on it."""
        logger.debug(f"MOQT receive: {self._msgstr(msg)}")

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
    # d16 = d14 base + repurposed code points; the table is pruned to
    # CONTROL_MESSAGE_TYPES below, so d14-only points are rejected.
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

    # d20 and d22 share one table: d18 with the d20 SETUP codec and
    # PUBLISH_STATE_NOTIFY. PUBLISH_OK 0x1E is reserved from d20 and is
    # pruned below.
    _D20_REGISTRY = dict(_D18_REGISTRY)
    _D20_REGISTRY[MOQTMessageType.SETUP] = (D20Setup, _handle_d20_setup)
    _D20_REGISTRY[D20MessageType.PUBLISH_STATE_NOTIFY] = (
        PublishStateNotify, _handle_publish_state_notify)

    CONTROL_REGISTRY: Dict[int, Dict[int, Tuple[Type[MOQTMessage], Callable]]] = {
        MOQTDraft.DRAFT_14: MOQT_CONTROL_MESSAGE_REGISTRY,
        MOQTDraft.DRAFT_16: _D16_REGISTRY,
        MOQTDraft.DRAFT_18: _D18_REGISTRY,
        MOQTDraft.DRAFT_20: _D20_REGISTRY,
        MOQTDraft.DRAFT_22: _D20_REGISTRY,
    }
    # One source of truth with the TX guard: dispatch exactly the types
    # each draft defines (a type outside the table closes the session in
    # _get_control_entry) and fail at import if the tables drift.
    # Explicit loops: class-body comprehensions cannot see class names.
    for _draft in list(CONTROL_REGISTRY):
        _legal = CONTROL_MESSAGE_TYPES[int(_draft)]
        _pruned = {}
        for _k in CONTROL_REGISTRY[_draft]:
            if int(_k) in _legal:
                _pruned[_k] = CONTROL_REGISTRY[_draft][_k]
        _missing = set(_legal)
        for _k in _pruned:
            _missing.discard(int(_k))
        assert not _missing, (
            f"draft-{int(_draft)}: no dispatch entry for "
            f"{sorted(hex(m) for m in _missing)}")
        CONTROL_REGISTRY[_draft] = _pruned
    del _draft, _legal, _pruned, _missing, _k

    # Stream data message types (dispatch by range check, not registry lookup)
    MOQT_STREAM_DATA_REGISTRY: Dict[int, Tuple[Type[MOQTMessage], Callable]] = {
        DataStreamType.FETCH_HEADER: (FetchHeader, _handle_fetch_header),
        # SubgroupHeader: types 0x10-0x1D dispatched by range check
    }


from aiopquic.asyncio.protocol import (
    QuicConnectionProtocol as _AioPQuicConnectionProtocol,
)
from aiopquic.asyncio.webtransport import (
    WebTransportClient as _AioPWTClient,
    WebTransportServerSession as _AioPWTServerSession,
    _EVT_WT_STREAM_DATA, _EVT_WT_STREAM_FIN,
    _EVT_WT_STREAM_RESET, _EVT_WT_STOP_SENDING,
    _EVT_WT_DATAGRAM, _EVT_WT_SESSION_CLOSED,
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
        elif evt_type == _EVT_WT_SESSION_CLOSED:
            super()._on_event(ev_tuple)
            reason = bytes(data).decode(errors="replace") if data else ""
            self._close_session(error_code, reason or "WebTransport session closed",
                                transmit=False)
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
            self._settle_draft(get_major_version(draft))
        else:
            # Learn the client's draft from the WT-Protocol this server
            # selected (in-band WT-Available-Protocols -> WT-Protocol
            # negotiation, surfaced by aiopquic as negotiated_protocol).
            # If no subprotocol was negotiated we do NOT guess a draft;
            # negotiated_draft keeps its conservative __init__ default.
            negotiated = self.negotiated_protocol
            if negotiated:
                self._settle_draft(get_major_version(
                    moqt_version_from_alpn(negotiated)))
        self._moqt_wt_finalize()
