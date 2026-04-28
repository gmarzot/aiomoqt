import asyncio
import time
from asyncio import Future
from collections import defaultdict
from functools import partial
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
from .types import *
from .utils.buffer import Buffer, BufferReadError
from .utils.logger import *
from aiopquic.streamchain import StreamChain

USER_AGENT = f"aiomoqt/{version('aiomoqt')}"

MOQT_IDLE_STREAM_TIMEOUT = 5

# WebTransport stream header wire constants (draft-ietf-webtrans-http3).
# Every WT stream is prefixed with <type, session_id> as two QUIC varints.
logger = get_logger(__name__)


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
    def __init__(self, allow_optional_dgram: bool = False, libquicr_compat: bool = False):
        #  message handlers
        self._control_msg_handlers: Dict[int, Tuple[Type, Callable]] = {}
        self.allow_optional_dgram = allow_optional_dgram
        self.libquicr_compat = libquicr_compat

    def register_handler(self, msg_type: int, handler: Callable) -> None:
        """Register a custom message handler."""
        (msg_class, _) = _MOQTSessionMixin.MOQT_CONTROL_MESSAGE_REGISTRY[msg_type]
        self._control_msg_handlers[msg_type] = (msg_class, handler)


class _MOQTSessionMixin:
    """MoQT session methods. Mix with an aiopquic transport base —
    QuicConnectionProtocol for raw QUIC, WebTransportSession (via
    _WTSessionMixin) for WebTransport. Concrete classes are
    MOQTSessionQuic / MOQTSessionWTClient / MOQTSessionWTServer at
    the end of this file.
    """

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

    def __init__(self, *args, session: 'MOQTPeer', **kwargs):
        super().__init__(*args, **kwargs)
        self._session: MOQTPeer = session  # backref to session object with config
        self._session_id: Optional[int] = None
        self._control_stream_id: Optional[int] = None
        self._loop = asyncio.get_running_loop()
        self._wt_session_setup: Future[bool] = self._loop.create_future()
        # Raw-QUIC mode has no WT setup phase. Pre-resolve so
        # StreamDataReceived processing isn't gated on a never-resolved
        # future. WT mode resolves it in _moqt_wt_finalize().
        if getattr(session, 'use_quic', False):
            self._wt_session_setup.set_result(True)
        self._moqt_version: int = MOQT_CUR_VERSION
        self._moqt_session_setup: Future[bool] = self._loop.create_future()
        self._moqt_session_closed: Future[Tuple[int,str]] = self._loop.create_future()
        self._next_request_id = 0 if self._is_client else 1
        self._next_track_alias = 0
        # Per-stream queues feeding _process_data_stream. Each entry is
        # an incoming chunk (bytes or memoryview) or None as FIN sentinel.
        self._stream_queues: DefaultDict[
            int, asyncio.Queue[Optional[Union[bytes, memoryview]]]
        ] = defaultdict(asyncio.Queue)
        self._stream_tasks: Dict[int, asyncio.Task] = {}
        self._tasks: Set[asyncio.Task] = set()
        self._close_err = None  # tuple holding latest (error_code, Reason_phrase)
        
        # Active uni data streams. Value is None when the stream has been
        # registered but no header has been parsed yet; after header
        # parsing it holds a FetchHeader or SubgroupHeader instance
        # (carrying per-stream runtime state like _prior_obj /
        # _last_object_id) until the stream closes.
        self._data_streams: Dict[int, Optional[MOQTMessage]] = {}
        self._bidi_streams: Dict[int, int] = {}  # map request_id to bidi stream_id (d16)
        self._bidi_stream_requests: Dict[int, int] = {}  # map bidi stream_id to request_id (d16)
        self._track_aliases: Dict[int, int] = {}  # map alias to subscription_id
        self._subscriptions: Dict[int, List] = {}  # map subscription_id to request
        self._pending_requests: Dict[int, Future[MOQTMessage]] = {}  # unified response futures

        # MoQT-level request ↔ uni stream binding (subscriber side).
        # Populated when a FETCH_HEADER or SubgroupHeader is admitted.
        # Consulted on FETCH_ERROR / FETCH_CANCEL / UNSUBSCRIBE to find the
        # data stream that belongs to a request, so we can terminate it.
        # Keyed as follows:
        #   _fetch_stream_by_request[request_id]                  -> stream_id
        #   _subgroup_stream_by_key[(track_alias, group, subgrp)] -> stream_id
        # The reverse lookup (_data_stream_key[stream_id]) stores whichever
        # key is appropriate for the stream so stream-close cleanup can
        # remove both sides of the binding.
        self._fetch_stream_by_request: Dict[int, int] = {}
        self._subgroup_stream_by_key: Dict[Tuple[int, int, Optional[int]], int] = {}
        self._data_stream_key: Dict[int, Tuple] = {}

        self._control_msg_registry = dict(_MOQTSessionMixin.MOQT_CONTROL_MESSAGE_REGISTRY)
        self._control_msg_registry.update(session._control_msg_handlers)

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
        """Resolve a pending request future by request_id."""
        future = self._pending_requests.get(request_id)
        if future and not future.done():
            future.set_result(msg)
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
        """Get the (message_class, handler) for a control message type.

        Handles version-aware dispatch for code points that are reused
        between draft-14 and draft-16 (0x05, 0x07, 0x08, 0x0E).
        """
        if is_draft16_or_later() and msg_type in self.MOQT_D16_OVERRIDE_REGISTRY:
            return self.MOQT_D16_OVERRIDE_REGISTRY[msg_type]
        return self._control_msg_registry[msg_type]

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

    def _moqt_handle_control_message(self, buf: Buffer) -> Optional[MOQTMessage]:
        """Process an incoming message."""
        buf_len = buf.capacity
        if buf_len == 0:
            logger.warning("MOQT event: handle control message: no data")
            return None

        logger.debug(f"MOQT event: handle control message: ({buf_len} bytes) 0x{buf.data_slice(0, buf_len).hex()}")
        try:
            start_pos = buf.tell()
            msg_type = buf.pull_uint_var()
            msg_len = buf.pull_uint16()
            hdr_len = buf.tell() - start_pos
            end_pos = start_pos + hdr_len + msg_len
            assert buf.tell() + msg_len <= buf_len
            # Check that msg_type exists
            try:
                msg_type = MOQTMessageType(msg_type)
            except ValueError:
                logger.error(f"MOQT error: unknown control message: type: {hex(msg_type)} start: {start_pos} len: {msg_len}")
                # Skip the rest of this message if possible
                buf.seek(end_pos)
                return
            # Look up message class (version-aware for shared code points)
            message_class, handler = self._get_control_entry(msg_type)
            logger.debug(f"MOQT event: control message: {message_class.__name__} ({msg_len} bytes)")
            # Deserialize message
            msg = message_class.deserialize(buf)
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

        except Exception as e:
            logger.error(f"handle_control_message: error handling control message: {e}")
            raise

    def _stream_task_done(self, stream_id: int, task: asyncio.Task) -> None:
        logger.debug(f"MOQT stream({stream_id}): stream task done: num stream tasks: {len(self._stream_tasks)}")

        if self._stream_tasks.pop(stream_id, None) is None:
            logger.warning(f"MOQT stream({stream_id}): stream task already removed")

        if stream_id not in self._data_streams:
            logger.warning(f"MOQT stream({stream_id}): stream already removed")

        error_code = QuicErrorCode.NO_ERROR
        if task.cancelled():
            error_code = QuicErrorCode.APPLICATION_ERROR
            logger.warning(f"MOQT stream({stream_id}): task cancelled")
        else:
            e = task.exception()
            if e:
                error_code = QuicErrorCode.APPLICATION_ERROR
                if isinstance(e, TimeoutError):
                    logger.debug(f"MOQT stream({stream_id}): stream idle timeout")
                else:
                    import traceback
                    logger.error(f"MOQT stream({stream_id}): task failed with exception: {type(e).__name__}\n{''.join(traceback.format_exception(e))}")
            else:
                logger.debug(f"MOQT stream({stream_id}): task completed")

        # Resolve fetch completion future if this was a fetch stream
        key = self._data_stream_key.get(stream_id)
        if key and len(key) == 2 and key[0] == 'fetch':
            request_id = key[1]
            fut = self._fetch_done_futures.pop(request_id, None)
            if fut and not fut.done():
                fut.set_result(error_code == QuicErrorCode.NO_ERROR)
                logger.debug(f"MOQT stream({stream_id}): fetch "
                             f"request_id={request_id} complete")
        self._unbind_stream(stream_id)

    def _on_stream_data(self, stream_id: int, data: bytes,
                        end_stream: bool) -> None:
        """Enqueue a data-stream chunk and spawn its processing task on
        first arrival. `data` must be pure MoQT payload — any transport
        framing (WT prefix, etc.) has already been stripped by the caller.
        `end_stream` pushes the FIN sentinel for the processing task.
        """
        if stream_id not in self._data_streams:
            logger.debug(f"MOQT event: new data stream: id: {stream_id} {len(data)} bytes")
            self._data_streams[stream_id] = None
            if stream_id in self._stream_tasks:
                logger.warning(f"MOQT stream({stream_id}): replacing existing task")
                self._stream_tasks[stream_id].cancel()
            task = asyncio.create_task(self._process_data_stream(stream_id))
            self._stream_tasks[stream_id] = task
            task.add_done_callback(partial(self._stream_task_done, stream_id))
            logger.debug(f"MOQT event: creating _process_data_stream task: {stream_id} num streams: {len(self._data_streams)}")

        if len(data) > 0:
            # Push the chunk by reference; StreamChain accepts bytes or
            # memoryview, no Buffer wrap needed. Avoids one copy when the
            # underlying transport already delivers a memoryview (aiopquic).
            logger.debug(f"MOQT event: pushing data on stream: {stream_id} len: {len(data)}")
            self._stream_queues[stream_id].put_nowait(data)

        if end_stream:
            logger.debug(f"MOQT event: stream FIN: {stream_id}")
            self._stream_queues[stream_id].put_nowait(None)

    # task for processing incoming data streams
    async def _process_data_stream(self, stream_id: int) -> None:
        """Subgroup / fetch stream data processing task.

        Per-stream byte accumulator (StreamChain) holds incoming chunks
        by reference and walks across chunk boundaries during parse.
        On successful parse: commit (drop the consumed prefix, reset
        tell to 0). On MOQTUnderflow / BufferReadError: rollback to
        the start of the failed parse attempt and await more bytes.
        Bounded by un-parsed bytes only; no fixed-size accumulator.
        """
        chain = StreamChain()
        consumed: int = 0
        group_id = None
        subgroup_id = None
        object_id = None

        while True:
            try:
                async with asyncio.timeout(MOQT_IDLE_STREAM_TIMEOUT):
                    msg_buf = await self._stream_queues[stream_id].get()
            except asyncio.TimeoutError:
                logger.debug(
                    f"MOQT stream({stream_id}): idle timeout: "
                    f"{group_id}.{subgroup_id}.{object_id}"
                )
                raise

            if msg_buf is None:  # FIN sentinel
                logger.debug(
                    f"MOQT stream({stream_id}): queue closed: task shutdown"
                )
                return

            chain.extend(msg_buf)
            logger.debug(
                f"MOQT stream({stream_id}): chunk received: "
                f"chunk_len={len(msg_buf)} chain_total={chain.capacity}"
            )

            # Drain as many complete messages as the chain currently holds.
            while chain.capacity > 0:
                chain.save()
                msg_obj = None
                try:
                    # StreamChain is duck-compatible with Buffer for the
                    # parser's needs (pull_uint_var, pull_bytes, tell,
                    # capacity). Proper Protocol-based typing is a
                    # separate cleanup.
                    msg_obj = self._moqt_handle_data_stream(
                        stream_id, chain, chain.capacity  # type: ignore[arg-type]
                    )
                except MOQTStreamReject as e:
                    # Phase 1c admission failure — STOP_SENDING the
                    # stream, do not close the session.
                    self._reject_stream(stream_id, e.error_code, e.reason)
                    return
                except MOQTUnderflow as e:
                    # Need more bytes; rewind to start of attempt and
                    # await next chunk.
                    chain.rollback()
                    logger.debug(
                        f"MOQT MOQTUnderflow({stream_id}): at pos: "
                        f"{e.pos} abs: {e.needed} chain_total: "
                        f"{chain.capacity} need: "
                        f"{e.needed - chain.capacity}"
                    )
                    break
                except BufferReadError:
                    # Same posture as underflow.
                    chain.rollback()
                    logger.debug(
                        f"MOQT BufferReadError({stream_id}): "
                        f"at pos: {chain.tell()}"
                    )
                    break
                except Exception as e:
                    # Data-plane parse failure — deframer lost
                    # alignment. Abandon this stream via STOP_SENDING;
                    # keep the session alive (other concurrent streams
                    # are unaffected). Pre-hardening this raised and
                    # killed the whole session.
                    tell = chain.tell()
                    hex_at = chain.data_slice(
                        0, min(40, chain.capacity)
                    ).hex()
                    logger.error(
                        f"MOQT stream({stream_id}): PARSE EXCEPTION "
                        f"at tell={tell} chain_total={chain.capacity} "
                        f"hex@anchor={hex_at} "
                        f"object_id={object_id} group_id={group_id} "
                        f"exc={type(e).__name__}: {e}"
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
                    raise asyncio.CancelledError(
                        SessionCloseCode.PROTOCOL_VIOLATION, error
                    )

                consumed = chain.tell()  # bytes parsed since save() (= 0)
                # Diagnostic: remember last successful parse before
                # commit, so a downstream framer-desync can dump it.
                self._last_parse_consumed = consumed
                self._last_parse_obj_id = (
                    msg_obj.object_id if hasattr(msg_obj, 'object_id') else None
                )
                chain.commit()  # drop consumed prefix; tell() reset to 0

                if isinstance(msg_obj, ObjectHeader):
                    assert object_id is None or msg_obj.object_id > object_id
                    object_id = msg_obj.object_id
                    status = ObjectStatus(msg_obj.status).name
                    id = f"{group_id}.{subgroup_id}.{object_id}"
                    now = int(time.time() * 1000)
                    msg_ts = (
                        msg_obj.extensions.get(MOQT_TIMESTAMP_EXT)
                        if msg_obj.extensions else None
                    )
                    delay = f"delay: {now - msg_ts} ms" if msg_ts else ""
                    logstr = (
                        f"{id} status: {status} size: {consumed} bytes "
                        f"{delay}"
                    )
                    if status != ObjectStatus.NORMAL:
                        if msg_obj.status in (
                            ObjectStatus.END_OF_GROUP,
                            ObjectStatus.END_OF_TRACK,
                        ):
                            logger.debug(
                                f"MOQT stream({stream_id}): {logstr}"
                            )
                            return
                    logger.debug(f"MOQT stream({stream_id}): {logstr}")
                    if self.on_object_received:
                        self.on_object_received(
                            msg_obj, consumed, now, group_id, subgroup_id
                        )
                elif isinstance(msg_obj, SubgroupHeader):
                    logger.debug(
                        f"MOQT stream({stream_id}): {msg_obj} "
                        f"{consumed} bytes"
                    )
                    assert group_id is None or msg_obj.group_id > group_id
                    group_id = msg_obj.group_id
                    subgroup_id = msg_obj.subgroup_id
                elif isinstance(msg_obj, FetchHeader):
                    logger.debug(
                        f"MOQT stream({stream_id}): {msg_obj} "
                        f"{consumed} bytes"
                    )
                elif isinstance(msg_obj, FetchObject):
                    now = int(time.time() * 1000)
                    stream_state = self._data_streams.get(stream_id)
                    request_id = (
                        stream_state.request_id
                        if isinstance(stream_state, FetchHeader)
                        else None
                    )
                    if msg_obj.end_of_range is not None:
                        logger.debug(
                            f"MOQT stream({stream_id}): FetchObject "
                            f"end-of-range 0x{msg_obj.end_of_range:x} "
                            f"at {msg_obj.group_id}.{msg_obj.object_id}"
                        )
                    else:
                        id = (
                            f"{msg_obj.group_id}.{msg_obj.subgroup_id}."
                            f"{msg_obj.object_id}"
                        )
                        msg_ts = (
                            msg_obj.extensions.get(MOQT_TIMESTAMP_EXT)
                            if msg_obj.extensions else None
                        )
                        delay = (
                            f"delay: {now - msg_ts} ms"
                            if isinstance(msg_ts, int) else ""
                        )
                        logger.debug(
                            f"MOQT stream({stream_id}): FetchObject "
                            f"{id} size: {consumed} bytes {delay}"
                        )
                        if self.on_fetch_object:
                            self.on_fetch_object(
                                msg_obj, consumed, now, request_id
                            )
                else:
                    logger.error(
                        f"MOQT stream({stream_id}): {msg_obj} "
                        f"size: {consumed} bytes"
                    )

    def _reject_stream(self, stream_id: int, error_code: int, reason: str) -> None:
        """Terminate a uni stream at the MoQT layer via STOP_SENDING.

        Used for admission failures (unknown request_id/track_alias, stream
        reuse, budget exceeded). Stream-level only — never closes the
        session. Removes any binding table entries for the stream.
        """
        logger.warning(f"MOQT stream({stream_id}): rejecting: "
                       f"{reason} (code=0x{error_code:x})")
        try:
            self._quic.stop_stream(stream_id, error_code)
        except Exception as e:
            logger.debug(f"MOQT stream({stream_id}): stop_stream failed: {e}")
        self._unbind_stream(stream_id)
        # Drop any parsing state and signal the task to exit
        self._data_streams.pop(stream_id, None)
        if stream_id in self._stream_queues:
            self._stream_queues[stream_id].put_nowait(None)

    def _unbind_stream(self, stream_id: int) -> None:
        """Remove a stream from the request↔stream binding table."""
        key = self._data_stream_key.pop(stream_id, None)
        if key is None:
            return
        if len(key) == 2 and key[0] == 'fetch':
            self._fetch_stream_by_request.pop(key[1], None)
        elif len(key) == 2 and key[0] == 'subgroup':
            self._subgroup_stream_by_key.pop(key[1], None)

    def _admit_fetch_stream(self, stream_id: int, header: 'FetchHeader') -> None:
        """Admit a FETCH_HEADER stream or raise MOQTStreamReject.

        Admission rules (Phase 1c):
        - request_id MUST match an outstanding FETCH we sent
          (present in _subscriptions with a Fetch message)
        - at most one uni stream per FETCH request (no reuse)
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
        self._data_stream_key[stream_id] = ('fetch', request_id)

    def _admit_subgroup_stream(self, stream_id: int,
                                header: 'SubgroupHeader') -> None:
        """Admit a SubgroupHeader stream or raise MOQTStreamReject.

        Admission rules (Phase 1c):
        - track_alias SHOULD map to a live subscription, but per spec
          §10.4.2 data may arrive before the control message that
          establishes the alias — tolerate with a warning.
        - at most one stream per (track_alias, group_id, subgroup_id)
          tuple. For FIRST_OBJ mode, subgroup_id is None at header time
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
            self._data_stream_key[stream_id] = ('subgroup', key)

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
        self._data_stream_key[stream_id] = ('subgroup', key)

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
            # new data streams will not yet have an entry
            if self._data_streams.get(stream_id) is None:
                # Get stream type from first byte
                stream_type = buf.pull_uint_var()
                # SubgroupHeader type ranges:
                #   d14: 0x10-0x1D (bit 4 set, bits 0-3 = flags)
                #   d16: also 0x30-0x3D (bit 5 = DEFAULT_PRIORITY)
                # Reserved: subgroup_id_mode 0b11 (bits 1-2)
                is_subgroup = (
                    (0x10 <= stream_type <= 0x1D or
                     0x30 <= stream_type <= 0x3D)
                    and ((stream_type >> 1) & 0x03) != 3
                )
                if is_subgroup:
                    msg_header = SubgroupHeader.deserialize(buf, type_val=stream_type)
                    data_type = "SUBGROUP_HEADER"
                    # Phase 1c admission check
                    self._admit_subgroup_stream(stream_id, msg_header)
                elif stream_type == DataStreamType.FETCH_HEADER:
                    msg_header = FetchHeader.deserialize(buf)
                    data_type = "FETCH_HEADER"
                    # Phase 1c admission check
                    self._admit_fetch_stream(stream_id, msg_header)
                else:
                    data_type = f"0x{stream_type:x}"
                    logger.warning(f"MOQT stream({stream_id}): unexpected data stream type: {data_type}")

                if msg_header is None:
                    error = f"data stream {stream_id}: {data_type} parse failed at: {buf.tell()}"
                    logger.error(f"MOQT error: " + error)
                    self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
                    return None

                # record that the data stream header has been processed
                consumed = buf.tell() - pos
                logger.debug(f"MOQT stream({stream_id}): {msg_header} consumed: {consumed} bytes")
                self._data_streams[stream_id] = msg_header
            else:
                stream_state = self._data_streams[stream_id]
                if isinstance(stream_state, SubgroupHeader):
                    sg_header: SubgroupHeader = stream_state
                    msg_header = ObjectHeader.deserialize(
                        buf, len,
                        extensions_present=sg_header.extensions_present,
                        prev_object_id=sg_header._last_object_id
                    )
                    # Update delta tracking state
                    sg_header._last_object_id = msg_header.object_id
                    # Resolve subgroup_id for FIRST_OBJ mode and complete
                    # the deferred subgroup binding
                    if (sg_header.subgroup_id_mode == SUBGROUP_ID_FIRST_OBJ
                            and sg_header.subgroup_id is None):
                        sg_header.subgroup_id = msg_header.object_id
                        self._bind_subgroup_first_obj(stream_id, sg_header)

                elif isinstance(stream_state, FetchHeader):
                    fh: FetchHeader = stream_state
                    msg_header = FetchObject.deserialize(buf, prior=fh._prior_obj)
                    # Track prior object for d16 delta-encoded references
                    if msg_header.end_of_range is None:
                        fh._prior_obj = msg_header

                if msg_header is None:
                    error = f"MOQT stream({stream_id}): ObjectHeader parse failed at: {buf.tell()}"
                    logger.error(f"MOQT error: " + error)
                    self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
                    return None
                consumed = buf.tell() - pos
                logger.debug(f"MOQT stream({stream_id}): {class_name(msg_header)} consumed: {consumed} bytes")


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
        # Get datagram type from first byte
        pos = buf.tell()
        dgram_type = buf.pull_uint_var()
        # Draft-14: ObjectDatagram types 0x00-0x07 (payload datagrams)
        if 0x00 <= dgram_type <= 0x07:
            msg = ObjectDatagram.deserialize(buf, buf.capacity, type_val=dgram_type)
            if msg is None:
                error = f"datagram parsing failed at: {buf.tell()}"
                logger.error(f"MOQT error: " + error)
                self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
                return msg

            consumed = buf.tell() - pos
            group_id = msg.group_id
            object_id = msg.object_id
            id = f"{group_id}.{object_id}"
            now = int(time.time()*1000)
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
            now = int(time.time()*1000)
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
    
    # primary event handling for all QUIC messaging
    def quic_event_received(self, event: QuicEvent) -> None:
        """Handle incoming QUIC events."""

        event_class = class_name(event)

        # CONNECTION_CLOSE events terminate the session.
        # StopSendingReceived and StreamReset also have error_code but
        # are stream-level events handled below — do NOT catch them here.
        if (hasattr(event, 'error_code')
                and not isinstance(event, (StopSendingReceived, StreamReset))):
            error = getattr(event, 'error_code', QuicErrorCode.INTERNAL_ERROR)
            reason = getattr(event, 'reason_phrase', event_class)
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
                    version = moqt_version_from_alpn(alpn)
                    set_moqt_ctx_version(version)
                    self._moqt_version = version
                    logger.info(f"MOQT: version set from ALPN: {alpn} -> 0x{version:x}")
                except ValueError:
                    pass
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
                logger.warning(f"QUIC event: stream data after close: stream {stream_id}")
                return

            data = event.data if event.data is not None else b""

            # Abrupt close of a critical stream
            if (event.end_stream and len(data) == 0 and
                    stream_id in (self._control_stream_id, self._session_id)):
                self._close_session(
                    SessionCloseCode.INTERNAL_ERROR,
                    f"critical stream closed by remote peer: {stream_id}"
                )
                return

            # Uni MoQT data streams: hot path through StreamChain
            # (memoryview-native; no Buffer construction).
            if stream_is_unidirectional(stream_id):
                self._on_stream_data(
                    stream_id, data, event.end_stream)
                return

            # Bidi/control: small messages. aiopquic delivers memoryview;
            # Buffer wants bytes for now.
            if not isinstance(data, (bytes, bytearray)):
                data = bytes(data)
            msg_buf = Buffer(data=data)
            msg_len = msg_buf.capacity
            logger.debug(f"MOQT event: StreamDataReceived: stream: {stream_id} len: {msg_len}")

            if self._control_stream_id is None:
                self._control_stream_id = stream_id
                logger.debug(f"QUIC event: detecting control stream: {stream_id}")
            elif stream_id != self._control_stream_id:
                if is_draft16_or_later():
                    self._handle_bidi_stream(stream_id, msg_buf, msg_len)
                    return
                logger.warning(f"MOQT event: unrecognized bidirectional stream({stream_id})")
                return

            if stream_id == self._control_stream_id:
                while msg_buf.tell() < msg_len:
                    msg = self._moqt_handle_control_message(msg_buf)
                    if msg is None:
                        error = f"control stream: parsing failed at position: {msg_buf.tell()} of {msg_len} bytes"
                        logger.error(f"MOQT error: " + error)
                        self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
                        break
                return

        elif isinstance(event, DatagramFrameReceived) and self._wt_session_setup.done():
            msg_buf = Buffer(data=event.data)
            logger.debug(f"MOQT event: DatagramFrameReceived: 0x{msg_buf.data_slice(0,min(msg_buf.capacity,16)).hex()}")
            self._moqt_handle_data_dgram(msg_buf)
            return
        elif isinstance(event, StopSendingReceived):
            logger.debug(f"MOQT event: StopSendingReceived: stream {event.stream_id}")
            self._quic.reset_stream(stream_id=event.stream_id, error_code=event.error_code)
            self._unbind_stream(event.stream_id)
            if event.stream_id in self._data_streams:
                del self._data_streams[event.stream_id]
            if event.stream_id in self._stream_tasks:
                self._stream_tasks[event.stream_id].cancel()
                del self._stream_tasks[event.stream_id]
            return
        elif isinstance(event, StreamReset):
            logger.debug(f"MOQT event: StreamReset: stream {event.stream_id}")
            self._unbind_stream(event.stream_id)
            if event.stream_id in self._data_streams:
                del self._data_streams[event.stream_id]
            if event.stream_id in self._stream_tasks:
                self._stream_tasks[event.stream_id].cancel()
                del self._stream_tasks[event.stream_id]
            return

        logger.debug(f"QUIC event: event not handled({event_class})")

    def _close_session(self, 
              error_code: SessionCloseCode = SessionCloseCode.NO_ERROR, 
              reason_phrase: str = "no error") -> None:
        """Close the MoQT session."""
        if error_code == SessionCloseCode.NO_ERROR:
            logger.info(f"MOQT: closing: {reason_phrase} ({error_code})")
        else:
            logger.error(f"MOQT error: closing: {reason_phrase} ({error_code})")
        self._close_err = (error_code, reason_phrase)

        # Signal all stream tasks to shut down gracefully with sentinel value
        for stream_id in list(self._stream_tasks.keys()):
            if stream_id in self._stream_queues:
                self._stream_queues[stream_id].put_nowait(None)

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
            if self._control_stream_id is not None:
                self._quic.send_stream_data(
                    self._control_stream_id, b"", end_stream=True)
                self._control_stream_id = None
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


    async def client_session_init(self, timeout: int = 10) -> bool:
        """Initialize WebTransport and MoQT client session."""

        use_quic = self._session.use_quic
        params = {}
        if use_quic:
            # Raw QUIC flow. _wt_session_setup is pre-resolved in __init__.
            logger.info(f"MOQT: Using raw QUIC transport")
            self._control_stream_id = self._quic.get_next_available_stream_id(
                is_unidirectional=False)
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
            # _wt_session_setup is resolved in _moqt_wt_finalize. Open
            # the MoQT control bidi stream and pin the version from
            # draft (in-band wt-available-protocols negotiation isn't
            # wired for v0.9.0; picowt supports it).
            draft = getattr(self._session, 'draft_version', None)
            if draft is not None:
                set_moqt_ctx_version(draft)
                self._moqt_version = draft
            self._control_stream_id = await self.open_bidi_stream()
            logger.info(
                f"MOQT: WT control stream created stream id: "
                f"{self._control_stream_id}")

        # Send CLIENT_SETUP
        params[SetupParamType.MAX_REQUEST_ID] = 10000
        params[SetupParamType.IMPLEMENTATION] = USER_AGENT.encode()
        # For raw QUIC with explicit draft: single version
        # For H3/WT: version list depends on whether WT protocol was negotiated
        #   - negotiated: is_draft16_or_later() is set, no version array needed
        #   - not negotiated: d14 format, include version array
        draft = getattr(self._session, 'draft_version', None)
        if use_quic and draft is not None:
            versions = [moqt_version_from_alpn(moqt_alpn_for_version(draft))]
        else:
            versions = MOQT_VERSIONS
        client_setup = self.client_setup(
            versions=versions,
            parameters=params
        )

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
        if getattr(self._session, 'use_quic', False):
            return self._quic.get_next_available_stream_id(is_unidirectional=True)
        # aiopquic-WT: round-trip to picoquic for stream-id allocation
        return await self.create_stream(bidir=False)

    async def open_bidi_stream(self) -> int:
        """Open a bidirectional stream. Returns the stream ID."""
        if getattr(self._session, 'use_quic', False):
            return self._quic.get_next_available_stream_id(is_unidirectional=False)
        # aiopquic-WT
        return await self.create_stream(bidir=True)

    def stream_write(self, stream_id: int, data: bytes, end_stream: bool = False) -> None:
        """Write data to a stream. No-op if stream is reset or FIN'd."""
        if not self._stream_is_writable(stream_id) and not end_stream:
            return
        try:
            self._quic.send_stream_data(stream_id, data, end_stream=end_stream)
        except AssertionError:
            pass  # stream already FIN'd — race with close()

    def _stream_is_writable(self, stream_id: int) -> bool:
        """Check if a stream is still writable (not reset or FIN'd).

        aiopquic owns stream state in picoquic and doesn't expose it,
        so return True optimistically; send_stream_data raises on a
        bad stream and the caller swallows it.
        """
        return True

    async def stream_write_drain(self, stream_id: int, data: bytes,
                                 end_stream: bool = False) -> None:
        """Write data to a stream, yielding when the SPSC TX ring is full.

        aiopquic owns congestion control on its picoquic pthread; we
        only need to backpressure on the SPSC TX ring (BufferError)
        when Python pushes faster than picoquic drains.
        """
        while True:
            try:
                self._quic.send_stream_data(
                    stream_id, data, end_stream=end_stream)
                return
            except BufferError:
                await asyncio.sleep(0.0001)

    def send_control_message(self, buf: Buffer) -> None:
        """Send a MoQT message on the control stream."""
        if self._quic is None or self._control_stream_id is None:
            raise MOQTException(SessionCloseCode.INTERNAL_ERROR, "control stream not intialized")
        logger.debug(f"QUIC send: control message: {buf.tell()} bytes")

        self._quic.send_stream_data(
            stream_id=self._control_stream_id,
            data=buf.data,
            end_stream=False
        )

    def _handle_bidi_stream(self, stream_id: int, buf: Buffer, buf_len: int) -> None:
        """Handle d16 bidirectional stream messages (SUBSCRIBE_NAMESPACE,
        responses). aiopquic delivers clean MoQT payload either way —
        WT prefix is stripped in C; raw QUIC has no prefix.
        """
        request_id = self._bidi_stream_requests.get(stream_id)
        if request_id is not None:
            while buf.tell() < buf_len:
                msg = self._moqt_handle_control_message(buf)
                if msg is None:
                    break
            return

        msg = self._moqt_handle_control_message(buf)
        if msg is not None and isinstance(msg, SubscribeNamespace):
            self._bidi_stream_requests[stream_id] = msg.request_id
            self._bidi_streams[msg.request_id] = stream_id

    def send_dgram_message(self, buf: Buffer) -> None:
        """Send a MoQT message on the control stream."""
        if self._quic is None:
            raise MOQTException(SessionCloseCode.INTERNAL_ERROR, "QUIC not intialized")

        logger.debug(f"QUIC send: datagram message: {buf.capacity} bytes")

        self._quic.send_datagram_frame(
            data=buf.data
        )
        self.transmit()

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
        self.send_control_message(message.serialize())

        return message

    def server_setup(
        self,
        selected_version: int = MOQT_CUR_VERSION,
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
        self.send_control_message(message.serialize())
        return message

    def subscribe(
        self,
        namespace: str,
        track_name: str,
        priority: int = 128,
        group_order: GroupOrder = GroupOrder.ASCENDING,
        forward: int = 1,
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
        self.send_control_message(message.serialize())

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
        self.send_control_message(message.serialize())
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
        self.send_control_message(message.serialize())
        return message
    
    def unsubscribe(
        self,
        request_id: int,
    ) -> Optional[MOQTMessage]:
        """Unsubscribe from a track."""
        message = Unsubscribe(request_id=request_id)
        logger.info(f"MOQT send: {message}")
        self.send_control_message(message.serialize())
 
        return message       

    async def join(
        self,
        namespace: Union[Tuple[bytes, ...], List[Union[bytes, str]], str],
        track_name: Union[bytes, str],
        subscriber_priority: int = 128,
        group_order: GroupOrder = GroupOrder.DESCENDING,
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
            forward=1,
            filter_type=FilterType.LATEST_OBJECT,  # spec §9.16.2
            parameters=parameters,
        )
        self._subscriptions[sub_request_id] = [sub_msg]
        # Pre-register response futures before send so loopback / low-RTT
        # peers can't resolve them before our awaiter registers.
        self._pending_requests[sub_request_id] = self._loop.create_future()
        logger.info(f"MOQT send: {sub_msg}")
        self.send_control_message(sub_msg.serialize())

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
        self.send_control_message(fetch_msg.serialize())

        if not wait_response:
            return (sub_msg, fetch_msg)

        sub_response = await self._await_response(sub_request_id)
        fetch_response = await self._await_response(fetch_request_id)
        return (sub_response, fetch_response)

    def fetch(
        self,
        namespace: Union[Tuple[bytes, ...], List[Union[bytes, str]], str],
        track_name: Union[bytes, str],
        subscriber_priority: int = 128,
        group_order: GroupOrder = GroupOrder.ASCENDING,
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
        self.send_control_message(message.serialize())

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
        self.send_control_message(message.serialize())
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
        self.send_control_message(message.serialize())
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
        logger.info(f"MOQT send: {message}")
        self.send_control_message(message.serialize())

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
        self.send_control_message(message.serialize())

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
        if is_draft16_or_later():
            message = RequestOk(request_id=msg.request_id)
        else:
            message = PublishNamespaceOk(request_id=msg.request_id)
        logger.info(f"MOQT send: {message} request_id: {msg.request_id} namespace: {msg.namespace}")
        self.send_control_message(message.serialize())
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
        self.send_control_message(message.serialize())
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

        if is_draft16_or_later():
            stream_id = await self.open_bidi_stream()
            buf = message.serialize()
            self.stream_write(stream_id, buf.data)
            self.transmit()
            # Track bidi stream ↔ request mapping for response routing
            self._bidi_streams[request_id] = stream_id
            self._bidi_stream_requests[stream_id] = request_id
        else:
            self.send_control_message(message.serialize())

        if not wait_response:
            return message

        return self._await_response(request_id)

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
        if stream_id is not None and is_draft16_or_later():
            buf = message.serialize()
            self.stream_write(stream_id, buf.data)
            self.transmit()
        else:
            self.send_control_message(message.serialize())
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
        self.send_control_message(message.serialize())
        return message


    ###############################################################################################
    #  Inbound MoQT message handlers                                                              #
    ###############################################################################################
    
    def default_message_handler(self, type: int,  msg: MOQTMessage) -> None:
        """Call the standard message handler"""
        _, handler = self.MOQT_CONTROL_MESSAGE_REGISTRY[type]
        # Schedule handler if one exists
        logger.info(f"MOQT event: calling default handler: {handler.__qualname__}")
        if handler is not None:
            task = asyncio.create_task(handler(self, msg))
            task.add_done_callback(lambda t: self._tasks.discard(t))
            self._tasks.add(task)       

    def register_handler(self, msg_type: int, handler: Callable) -> None:
        """Register a custom message handler."""
        (msg_class, _) = self._control_msg_registry[msg_type]
        self._control_msg_registry[msg_type] = (msg_class, handler)
    
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
                # Draft-16+: version already negotiated via ALPN
                selected_version = self._moqt_version
                logger.info(f"MOQT event: d16+ ServerSetup (version from ALPN: 0x{selected_version:x})")
            if selected_version not in MOQT_VERSIONS:
                error = f"MOQT event: unsupported version in ServerSetup {hex(selected_version)}"
                logger.debug(error)
                self._close_session(
                    error_code=SessionCloseCode.PROTOCOL_VIOLATION,
                    reason_phrase=error
                )
            else:
                self._moqt_version = selected_version
                set_moqt_ctx_version(self._moqt_version)

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
            # already set in self._moqt_version on ProtocolNegotiated.
            version_ok = (
                is_draft16_or_later() or MOQT_CUR_VERSION in msg.versions
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
        if is_draft16_or_later():
            # d16: respond with REQUEST_OK instead of PublishNamespaceOk
            message = RequestOk(request_id=msg.request_id)
            logger.info(f"MOQT send: {message} request_id: {msg.request_id} namespace: {msg.namespace}")
            self.send_control_message(message.serialize())
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
        logger.info(f"MOQT event: handle {msg}")
        # Handle unsubscribe request

    async def _handle_subscribe_done(self, msg: SubscribeDone) -> None:
        logger.info(f"MOQT event: handle SubscribeDone "
                     f"request_id={msg.request_id} "
                     f"status={msg.status_code} "
                     f"streams={msg.stream_count}")
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

        Phase 1b publisher side: only operative when the application
        registered the fetch's uni stream with bind_fetch_tx_stream().
        """
        logger.info(f"MOQT event: handle {msg}")
        stream_id = self._fetch_stream_by_request.pop(msg.request_id, None)
        if stream_id is not None:
            self._data_stream_key.pop(stream_id, None)
            try:
                self._quic.reset_stream(
                    stream_id, SessionCloseCode.NO_ERROR)
                logger.debug(f"MOQT stream({stream_id}): reset on FETCH_CANCEL "
                             f"for request_id={msg.request_id}")
            except Exception as e:
                logger.debug(f"MOQT stream({stream_id}): reset_stream failed: {e}")
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
            self._data_stream_key.pop(stream_id, None)
            try:
                self._quic.stop_stream(stream_id, msg.error_code)
                logger.debug(f"MOQT stream({stream_id}): stop_sending on "
                             f"FETCH_ERROR request_id={msg.request_id}")
            except Exception as e:
                logger.debug(f"MOQT stream({stream_id}): stop_stream failed: {e}")
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
        """Handle fetch header message.

        NOTE: This registry handler is not currently dispatched by the
        data-stream path. Admission checks and binding-table registration
        happen inline in _moqt_handle_data_stream (Phase 1c), where
        unknown request_ids are rejected at the stream level via
        STOP_SENDING — not at the session level. This method is retained
        for registry completeness / user override.
        """
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


    # Draft-16 override registry for repurposed code points.
    # When is_draft16_or_later(), these take precedence over the main registry.
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

    MOQT_D16_OVERRIDE_REGISTRY: Dict[int, Tuple[Type[MOQTMessage], Callable]] = {
        # Code point 0x05: d14=SUBSCRIBE_ERROR, d16=REQUEST_ERROR
        0x05: (RequestError, _handle_request_error),
        # Code point 0x07: d14=PUBLISH_NAMESPACE_OK, d16=REQUEST_OK
        0x07: (RequestOk, _handle_request_ok),
        # Code point 0x08: d14=PUBLISH_NAMESPACE_ERROR, d16=NAMESPACE
        0x08: (Namespace, _handle_namespace),
        # Code point 0x0E: d14=TRACK_STATUS_OK, d16=NAMESPACE_DONE
        0x0E: (NamespaceDone, _handle_namespace_done),
        # Code point 0x02: d14=SUBSCRIBE_UPDATE, d16=REQUEST_UPDATE
        0x02: (RequestUpdate, _handle_request_update),
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

    def _moqt_wt_finalize(self) -> None:
        self._quic = self
        if not self._wt_session_setup.done():
            self._wt_session_setup.set_result(True)

    def _on_event(self, ev_tuple) -> None:
        """Translate WT-specific events into the QuicEvent classes
        the mixin's quic_event_received already handles, then
        dispatch through that path. Falls back to WebTransportSession
        base handling for session-level signals (ready/closed)."""
        super()._on_event(ev_tuple)
        evt_type, sid, data, _is_fin, error_code, _cnx, _ = ev_tuple
        from aiopquic.quic.events import (
            StreamDataReceived as _SD, StreamReset as _SR,
            StopSendingReceived as _SS, DatagramFrameReceived as _DG,
        )
        from aiopquic.asyncio.webtransport import (
            _EVT_WT_STREAM_DATA, _EVT_WT_STREAM_FIN,
            _EVT_WT_STREAM_RESET, _EVT_WT_STOP_SENDING,
            _EVT_WT_DATAGRAM,
        )
        if evt_type == _EVT_WT_STREAM_DATA:
            self.quic_event_received(_SD(stream_id=sid, data=data,
                                          end_stream=False))
        elif evt_type == _EVT_WT_STREAM_FIN:
            self.quic_event_received(_SD(stream_id=sid, data=data,
                                          end_stream=True))
        elif evt_type == _EVT_WT_STREAM_RESET:
            self.quic_event_received(_SR(stream_id=sid,
                                          error_code=error_code))
        elif evt_type == _EVT_WT_STOP_SENDING:
            self.quic_event_received(_SS(stream_id=sid,
                                          error_code=error_code))
        elif evt_type == _EVT_WT_DATAGRAM:
            self.quic_event_received(_DG(data=data))


class MOQTSessionWTClient(
        _WTSessionMixin, _MOQTSessionMixin, _AioPWTClient):
    """aiopquic-backed MoQT session — initiator-side WT (client)."""

    def __init__(self, transport, host: str, port: int, path: str,
                 sni: Optional[str] = None, *,
                 session: 'MOQTPeer'):
        super().__init__(transport, host, port, path,
                         sni=sni, session=session)
        self._moqt_wt_finalize()


class MOQTSessionWTServer(
        _WTSessionMixin, _MOQTSessionMixin, _AioPWTServerSession):
    """aiopquic-backed MoQT session — acceptor-side WT (server)."""

    def __init__(self, transport, state, *, session: 'MOQTPeer'):
        super().__init__(transport, state, session=session)
        self._moqt_wt_finalize()
