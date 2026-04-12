import asyncio
import time
from asyncio import Future
from collections import defaultdict
from functools import partial
from typing import (Callable, DefaultDict, Dict, List, Optional, Set, Tuple,
                    Type, Union)

from qh3.asyncio.protocol import QuicConnectionProtocol
from qh3.h3.connection import (H3_ALPN, ErrorCode, H3Connection, Setting,
                               StreamType)
from qh3.h3.events import HeadersReceived
from qh3.quic.connection import (QuicConnection, QuicErrorCode,
                                 stream_is_unidirectional)
from qh3.quic.events import (DatagramFrameReceived, ProtocolNegotiated,
                             QuicEvent, StopSendingReceived,
                             StreamDataReceived, StreamReset)

# Monkey-patch qh3 Setting enum: H3_DATAGRAM should be 0x33 (RFC 9297),
# not 0xFFD277 (old experimental value). Remove when qh3 is fixed or
# when we move to aiopquic native H3.
if Setting.H3_DATAGRAM.value != 0x33:
    _old = Setting.H3_DATAGRAM.value
    Setting._value2member_map_.pop(_old, None)
    Setting._value2member_map_.pop(0x33, None)  # remove DUMMY mapping
    Setting.H3_DATAGRAM._value_ = 0x33
    Setting._value2member_map_[0x33] = Setting.H3_DATAGRAM

from importlib.metadata import version

from .context import *
from .messages import *
from .types import *
from .utils.buffer import Buffer, BufferReadError
from .utils.logger import *

USER_AGENT = f"aiomoqt/{version('aiomoqt')}"

MOQT_IDLE_STREAM_TIMEOUT = 5

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
    

class H3CustomConnection(H3Connection):
    """Custom H3Connection wrapper to support alternate SETTINGS"""

    def __init__(self, quic: QuicConnection, table_capacity: int = 0,
                 allow_optional_dgram: bool = False, **kwargs) -> None:
        self._max_table_capacity = table_capacity
        self._max_table_capacity_cfg = table_capacity
        self._allow_optional_dgram = allow_optional_dgram
        super().__init__(quic, **kwargs)
        # report sent settings
        settings = self.sent_settings
        if settings is not None:
            logger.debug("H3 SETTINGS sent:")
            for setting_id, value in settings.items():
                logger.debug(f"  Setting 0x{setting_id:x} = {value}")

    def _validate_settings(self, settings: dict) -> None:
        """Validate received H3 SETTINGS with qh3 enum fixup.

        qh3 uses wrong value for H3_DATAGRAM (0xFFD277 instead of 0x33).
        We patch the enum at import time, but also need to remap the raw
        settings dict keys since qh3 parses them before our patch takes
        effect on the wire format.
        """
        patched = dict(settings)
        # Remap raw 0x33 to the (now-patched) Setting.H3_DATAGRAM enum key
        if 0x33 in patched and Setting.H3_DATAGRAM not in patched:
            patched[Setting.H3_DATAGRAM] = patched.pop(0x33)
        logger.debug(f"H3 SETTINGS received: { {(f'0x{k:x}' if isinstance(k, int) else k.name): v for k, v in patched.items()} }")
        if self._allow_optional_dgram:
            if (patched.get(Setting.ENABLE_WEBTRANSPORT) == 1
                    and patched.get(Setting.H3_DATAGRAM) != 1):
                logger.warning("H3: peer sent ENABLE_WEBTRANSPORT without "
                               "H3_DATAGRAM — accepting (allow_optional_dgram=True)")
                patched[Setting.H3_DATAGRAM] = 1
        super()._validate_settings(patched)

    @property
    def _max_table_capacity(self):
        return self._max_table_capacity_cfg

    @_max_table_capacity.setter
    def _max_table_capacity(self, value):
        # Ignore the parent class attempt to set it
        pass
    
    
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
        (msg_class, _) = MOQTSession.MOQT_CONTROL_MESSAGE_REGISTRY[msg_type]
        self._control_msg_handlers[msg_type] = (msg_class, handler)
        



class MOQTSession(QuicConnectionProtocol):
    """MOQT session protocol implementation."""

    def __init__(self, *args, session: 'MOQTPeer', **kwargs):
        super().__init__(*args, **kwargs)
        self._session: MOQTPeer = session  # backref to session object with config
        self._h3: Optional[H3Connection] = None
        self._session_id: Optional[int] = None
        self._control_stream_id: Optional[int] = None
        self._loop = asyncio.get_running_loop()
        self._wt_session_setup: Future[bool] = self._loop.create_future()
        self._wt_selected_protocol: Optional[str] = None
        self._moqt_version: int = MOQT_CUR_VERSION
        self._moqt_session_setup: Future[bool] = self._loop.create_future()
        self._moqt_session_closed: Future[Tuple[int,str]] = self._loop.create_future()
        self._next_request_id = 0 if self._quic.configuration.is_client else 1
        self._next_track_alias = 0
        self._stream_queues: DefaultDict[int, asyncio.Queue[Buffer]] = defaultdict(asyncio.Queue)
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

        self._control_msg_registry = dict(MOQTSession.MOQT_CONTROL_MESSAGE_REGISTRY)
        self._control_msg_registry.update(session._control_msg_handlers)

        self._stream_data_registry = dict(MOQTSession.MOQT_STREAM_DATA_REGISTRY)
        self._dgram_data_registry = dict(MOQTSession.MOQT_DGRAM_DATA_REGISTRY)

        # Partial WT header buffer for streams where the 2-varint
        # WebTransport stream header was fragmented across QUIC packets.
        # Keyed by stream_id → bytes of the partial header.
        self._wt_header_pending: Dict[int, bytes] = {}

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
        return isinstance(msg, MOQTSession._ERROR_TYPES)

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
        """
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
        """Convert string or tuple into bytes tuple."""
        if isinstance(namespace, str):
            return tuple(part.encode() for part in namespace.split('/'))
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

    def _endpoint_match(self, path: Union[bytes,str]):
        endpoint = getattr(self._session, 'endpoint')
        if endpoint is None:
            return False
        # Convert bytes to str if needed
        if isinstance(endpoint, bytes):
            endpoint = endpoint.decode('utf-8')
        if isinstance(path, bytes):
            path = path.decode('utf-8')
            
        # Strip trailing slashes
        endpoint = endpoint.strip('/')
        path = path.strip('/')
        logger.debug(f"H3 event: endpoint: {endpoint} path: {path}")
        return endpoint == path
            
    def _moqt_handle_control_message(self, buf: Buffer) -> Optional[MOQTMessage]:
        """Process an incoming message."""
        buf_len = buf.capacity
        if buf_len == 0:
            logger.warning("MOQT event: handle control message: no data")
            return None
        pos = buf.tell()
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
            #assert start_pos + msg_len == (buf.tell())
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
 
    # task for processing incoming data streams
    async def _process_data_stream(self, stream_id: int) -> None:
        ''' Subgroup stream data processing task '''
        re_buf = Buffer(capacity=(1024*1024*32))  # pre-allocate large buffer accumulator
        cur_pos: int = 0
        consumed: int = 0
        needed: int = 0
        group_id = None
        subgroup_id = None
        object_id = None
        while True:
            try:
                while True:
                    async with asyncio.timeout(MOQT_IDLE_STREAM_TIMEOUT):
                        msg_buf = await self._stream_queues[stream_id].get()

                    if msg_buf is None:  # Sentinel done value - return
                        logger.debug(f"MOQT stream({stream_id}): queue closed: task shutdown")
                        return
                    
                    cur_pos = msg_buf.tell()
                    msg_len = msg_buf.capacity

                    if msg_len < needed:
                        needed -= msg_len
                        re_buf.push_bytes(msg_buf.data_slice(cur_pos, msg_len))
                        have = re_buf.tell()
                        logger.debug(f"MOQT stream({stream_id}): data added: len: {msg_len} have: {have} still need: {needed}")
                    elif cur_pos == msg_len:
                        continue  # special case where the stream id is all we got - next
                    else:
                        logger.debug(f"MOQT stream({stream_id}): data received: pos: {cur_pos} len: {msg_len} needed: {needed}")
                        break
                        
            except asyncio.TimeoutError:
                logger.debug(f"MOQT stream({stream_id}): idle timeout: {group_id}.{subgroup_id}.{object_id}")
                raise
            
            # if more data was needed, add it to re_buf accumulator and reprocess
            if needed > 0:
                re_buf.push_bytes(msg_buf.data_slice(cur_pos,msg_buf.capacity))
                msg_len = re_buf.tell()
                msg_buf = re_buf  # process accumulator, GC msg_buf
                msg_buf.seek(0)
                cur_pos = 0
                needed = 0
                
            while cur_pos < msg_len:
                logger.debug(f"MOQT stream({stream_id}): process message: pos: {cur_pos} len: {msg_len}")
                msg_obj = None
                try:
                    msg_obj = self._moqt_handle_data_stream(stream_id, msg_buf, msg_len)
                except MOQTStreamReject as e:
                    # Phase 1c: admission failure — reject at the stream
                    # level only (STOP_SENDING), NOT a session close.
                    self._reject_stream(stream_id, e.error_code, e.reason)
                    return
                except MOQTUnderflow as e:
                    # e.needed is absolute buffer position; convert to bytes beyond msg_len
                    needed = e.needed - msg_len
                    if needed <= 0:
                        needed = 1  # at least request one more byte
                    logger.debug(f"MOQT MOQTUnderflow({stream_id}): at pos: {e.pos} abs: {e.needed} msg_len: {msg_len} need: {needed}")
                    break
                except BufferReadError as e:
                    logger.debug(f"MOQT BufferReadError({stream_id}): cur_pos: {cur_pos} tell: {msg_buf.tell()}")
                    needed = 1  # just get the next msg_buf - we dont know amount needed
                    break
                except Exception as e:
                    # Dump buffer state for debugging parse failures
                    tell = msg_buf.tell()
                    hex_at = msg_buf.data_slice(max(0, cur_pos), min(msg_len, cur_pos + 40)).hex()
                    logger.error(f"MOQT stream({stream_id}): PARSE EXCEPTION at cur_pos={cur_pos} tell={tell} msg_len={msg_len} "
                                 f"needed_was={needed} hex@cur_pos={hex_at} "
                                 f"object_id={object_id} group_id={group_id}")
                    raise
                    
                if msg_obj is None:
                    error = f"MOQT error: data stream({stream_id}):: parsing failed at position: "
                    logger.error(error + f"{msg_buf.tell()} of {msg_len} bytes")
                    self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
                    raise asyncio.CancelledError(SessionCloseCode.PROTOCOL_VIOLATION, error)
                
                consumed = msg_buf.tell() - cur_pos
                cur_pos = msg_buf.tell()
                if isinstance(msg_obj, ObjectHeader):
                    assert object_id is None or msg_obj.object_id > object_id
                    object_id = msg_obj.object_id
                    status = ObjectStatus(msg_obj.status).name
                    id = f"{group_id}.{subgroup_id}.{object_id}"
                    now = int(time.time()*1000)
                    msg_ts = msg_obj.extensions.get(MOQT_TIMESTAMP_EXT) if msg_obj.extensions else None
                    delay = f"delay: {now - msg_ts} ms" if msg_ts else ""
                    logstr = f"{id} status: {status} size: {consumed} bytes {delay}"
                    if status != ObjectStatus.NORMAL:
                        if msg_obj.status in (ObjectStatus.END_OF_GROUP, ObjectStatus.END_OF_TRACK):
                                logger.debug(f"MOQT stream({stream_id}): {logstr}")
                                self._stream_queues[stream_id].closed = True
                                return
                    logger.debug(f"MOQT stream({stream_id}): {logstr}")
                    if self.on_object_received:
                        self.on_object_received(msg_obj, consumed, now, group_id, subgroup_id)
                elif isinstance(msg_obj, SubgroupHeader):
                    logger.debug(f"MOQT stream({stream_id}): {msg_obj} {consumed} bytes")
                    assert group_id is None or msg_obj.group_id > group_id
                    group_id = msg_obj.group_id
                    subgroup_id = msg_obj.subgroup_id
                elif isinstance(msg_obj, FetchHeader):
                    logger.debug(f"MOQT stream({stream_id}): {msg_obj} {consumed} bytes")
                    # request_id stays on the header (also in the binding
                    # table) — nothing to dispatch until FetchObjects arrive
                elif isinstance(msg_obj, FetchObject):
                    now = int(time.time()*1000)
                    # Look up the fetch request_id from the stream's
                    # FetchHeader state so the callback can correlate.
                    stream_state = self._data_streams.get(stream_id)
                    request_id = (stream_state.request_id
                                   if isinstance(stream_state, FetchHeader)
                                   else None)
                    if msg_obj.end_of_range is not None:
                        # d16 end-of-range marker (0x8C/0x10C) — logs and
                        # waits for the stream FIN that must follow.
                        logger.debug(
                            f"MOQT stream({stream_id}): FetchObject "
                            f"end-of-range 0x{msg_obj.end_of_range:x} "
                            f"at {msg_obj.group_id}.{msg_obj.object_id}")
                    else:
                        id = f"{msg_obj.group_id}.{msg_obj.subgroup_id}.{msg_obj.object_id}"
                        msg_ts = (msg_obj.extensions.get(MOQT_TIMESTAMP_EXT)
                                  if msg_obj.extensions else None)
                        delay = (f"delay: {now - msg_ts} ms"
                                 if isinstance(msg_ts, int) else "")
                        logger.debug(
                            f"MOQT stream({stream_id}): FetchObject "
                            f"{id} size: {consumed} bytes {delay}")
                        if self.on_fetch_object:
                            self.on_fetch_object(msg_obj, consumed, now, request_id)
                else:
                    logger.error(f"MOQT stream({stream_id}): {msg_obj} size: {consumed} bytes")
                    # raise RuntimeError


            if needed > 0:
                have = msg_len - cur_pos
                # yuck - python memove - custom stream reader in progress
                saved_bytes = msg_buf.data_slice(cur_pos, msg_len)
                if have < needed:  # we might not know how much we need
                    needed -= have
                re_buf.seek(0)
                re_buf.push_bytes(saved_bytes)
                logger.debug(f"MOQT stream({stream_id}): saved {have} bytes still need: {needed}")
                cur_pos = 0

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
        - track_alias MUST map to a live subscription
        - at most one stream per (track_alias, group_id, subgroup_id)
          tuple. For FIRST_OBJ mode, subgroup_id is None at header time
          and the uniqueness check is deferred until the first object.
        """
        if header.track_alias not in self._track_aliases:
            raise MOQTStreamReject(
                SessionCloseCode.PROTOCOL_VIOLATION,
                f"subgroup stream for unknown track_alias="
                f"{header.track_alias}")
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
                # Draft-14: SubgroupHeader types 0x10-0x1D (12 valid, 0x16-0x17 reserved)
                if 0x10 <= stream_type <= 0x1D and ((stream_type >> 1) & 0x03) != 3:
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
    
    # def transmit(self) -> None:
    #     """Transmit pending data."""
    #     logger.debug("Transmitting data")
    #     super().transmit()

    def connection_made(self, transport):
        """Called when QUIC connection is established."""
        super().connection_made(transport)
        use_quic = getattr(self._session, 'use_quic', False)
        logger.info(f"MOQT: session connection initialized: {use_quic}")
        if not use_quic:
            allow_dgram = getattr(self._session, 'allow_optional_dgram', False)
            self._h3 = H3CustomConnection(
                self._quic, enable_webtransport=True,
                allow_optional_dgram=allow_dgram)
            logger.info("H3 connection initialized")

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
        
        data_len = len(event.data) if hasattr(event, 'data') else 0
        data = "<none>"
        if data_len > 0:
            data = event.data.hex()
            
        logger.debug(f"QUIC event: {event_class}: len: {data_len} bytes data: {data}")
        
        if isinstance(event, ProtocolNegotiated):
            # Enforce supported ALPN
            alpn = event.alpn_protocol
            if alpn in H3_ALPN:
                logger.debug(f"QUIC event: ALPN ProtocolNegotiated: {alpn}")
            elif alpn == MOQT_ALPN or (alpn and alpn.startswith("moqt-")):
                logger.debug(f"QUIC event: ALPN ProtocolNegotiated alpn: {alpn}")
                # Set version from ALPN (draft-16+: version is ALPN-negotiated)
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

            # WT session stream (CONNECT) — pass through to H3 for capsule processing
            if stream_id == self._session_id:
                logger.debug(f"MOQT event: WT session stream data({stream_id}): {len(event.data)} bytes 0x{event.data[:16].hex()}")
                # fall through to H3 handler below

            elif self._closed.is_set() or self._close_err is not None:
                close_condition = f"MOQT: {self._close_err} QUIC: {self._closed.is_set()}"
                logger.warning(f"QUIC event: stream data after close: " + close_condition)
                return

            # Detect abrupt closure of critical streams
            elif (event.end_stream and len(event.data) == 0 and
                stream_id in [self._control_stream_id, self._session_id]):
                self._close_session(
                    SessionCloseCode.INTERNAL_ERROR,
                    f"critical stream closed by remote peer: {stream_id}"
                )
                return

            else:
                msg_buf = Buffer(data=event.data)
                msg_len = msg_buf.capacity
                logger.debug(f"MOQT event: StreamDataReceived: stream: {stream_id} len: {msg_len}")

                # Handle possible MoQT control stream
                if not stream_is_unidirectional(stream_id):
                    # Assume first bidi stream is MoQT control stream
                    if self._control_stream_id is None:
                        self._control_stream_id = stream_id
                        logger.debug(f"QUIC event: detecting control stream: {stream_id}")
                        # Strip WT stream header (WebTransport only)
                        if self._h3 is not None:
                            msg_buf.pull_uint_var()
                            msg_buf.pull_uint_var()
                    elif stream_id != self._control_stream_id:
                        # d16: bidi streams carry SUBSCRIBE_NAMESPACE and responses
                        if is_draft16_or_later():
                            self._handle_bidi_stream(stream_id, msg_buf, msg_len)
                            return
                        logger.warning(f"MOQT event: unrecognized bidirectional stream({stream_id}):")
                        return

                # Handle MoQT control messages
                if stream_id == self._control_stream_id:
                    # XXX handle underflow in control stream as well
                    while msg_buf.tell() < msg_len:
                        msg = self._moqt_handle_control_message(msg_buf)
                        if msg is None:
                            error = f"control stream: parsing failed at position: {msg_buf.tell()} of {msg_len} bytes"
                            logger.error(f"MOQT error: " + error)
                            self._close_session(SessionCloseCode.PROTOCOL_VIOLATION, error)
                            break
                    return

                # Handle MoQT data streams (unidirectional only)
                if stream_is_unidirectional(stream_id):
                    # Identify H3 internal uni streams (SETTINGS, QPACK
                    # encoder/decoder) so they pass through to the H3
                    # handler below. All other uni streams are MoQT data.
                    # Raw QUIC has no H3 framing — skip this check.
                    if self._h3 is not None:
                        h3_peer_streams = {
                            self._h3._peer_control_stream_id,
                            self._h3._peer_encoder_stream_id,
                            self._h3._peer_decoder_stream_id,
                        }
                        is_h3_internal = stream_id in h3_peer_streams
                    else:
                        is_h3_internal = False
                    if not is_h3_internal:
                        # Handle WT header continuation for fragmented streams
                        if stream_id in self._wt_header_pending:
                            combined = self._wt_header_pending.pop(stream_id) + event.data
                            msg_buf = Buffer(data=combined)
                            msg_len = msg_buf.capacity
                            try:
                                msg_buf.pull_uint_var()
                                msg_buf.pull_uint_var()
                            except BufferReadError:
                                # Still not enough data — re-save and wait
                                self._wt_header_pending[stream_id] = combined
                                logger.debug(f"MOQT stream({stream_id}): WT header still incomplete ({len(combined)} bytes)")
                                return
                            logger.debug(f"MOQT stream({stream_id}): WT header completed from {len(combined)} bytes")
                            self._data_streams[stream_id] = None
                            assert stream_id not in self._stream_tasks
                            task = asyncio.create_task(self._process_data_stream(stream_id))
                            self._stream_tasks[stream_id] = task
                            task.add_done_callback(partial(self._stream_task_done, stream_id))
                            if msg_buf.tell() < msg_len:
                                self._stream_queues[stream_id].put_nowait(msg_buf)
                            if event.end_stream:
                                self._stream_queues[stream_id].put_nowait(None)
                            return

                        if stream_id not in self._data_streams:
                            logger.debug(f"MOQT event: new data stream: id: {stream_id} {msg_len} bytes")
                            # Strip WT 2-varint stream header (WebTransport only)
                            if self._h3 is not None:
                                try:
                                    msg_buf.pull_uint_var()
                                    msg_buf.pull_uint_var()
                                except BufferReadError:
                                    # WT header fragmented across QUIC packets —
                                    # save partial bytes and wait for continuation
                                    self._wt_header_pending[stream_id] = event.data
                                    logger.debug(f"MOQT stream({stream_id}): WT header fragmented, buffering {msg_len} bytes")
                                    return
                            self._data_streams[stream_id] = None
                            assert stream_id not in self._stream_tasks
                            task = asyncio.create_task(self._process_data_stream(stream_id))
                            self._stream_tasks[stream_id] = task
                            task.add_done_callback(partial(self._stream_task_done, stream_id))
                            logger.debug(f"MOQT event: creating _process_data_stream task: {stream_id} num streams: {len(self._data_streams)}")

                        # Queue the event data buffer for processing
                        if msg_buf.tell() < msg_len:
                            logger.debug(f"MOQT event: pushing data on stream: {stream_id} pos: {msg_buf.tell()} len: {msg_len}")
                            self._stream_queues[stream_id].put_nowait(msg_buf)
                        else:
                            logger.debug(f"MOQT event: skipping empty data: {stream_id} pos: {msg_buf.tell()} len: {msg_len}")
                        # Signal stream task that stream is done (FIN received)
                        if event.end_stream:
                            logger.debug(f"MOQT event: stream FIN: {stream_id}")
                            self._stream_queues[stream_id].put_nowait(None)
                        return
                    else:
                        logger.debug(f"MOQT event: H3 internal uni stream {stream_id} type=0x{event.data[0]:02x}")
                        # fall through to H3 handler below

        elif isinstance(event, DatagramFrameReceived) and self._wt_session_setup.done():
            msg_buf = Buffer(data=event.data)
            msg_len = msg_buf.capacity
            logger.debug(f"MOQT event: DatagramFrameReceived: 0x{msg_buf.data_slice(0,min(msg_len,16)).hex()}")
            # Strip WT Quarter Stream ID / Context ID (WebTransport only)
            if self._h3 is not None:
                msg_buf.pull_uint_var()
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

        # Pass remaining events to H3
        if self._h3 is not None:
            settings = self._h3.received_settings
            try:
                logger.debug(f"MOQT event: h3 processing: {event_class} len: {data_len}")
                if hasattr(event, "stream_id"):
                    logger.debug(f"MOQT event: H3 stream: {event.stream_id} {stream_is_unidirectional(event.stream_id)}")
                for h3_event in self._h3.handle_event(event):
                    logger.debug(f"MOQT event: h3 processing: {h3_event.__class__.__name__}")
                    self._h3_handle_event(h3_event)
                # Check if settings just received
                if self._h3.received_settings != settings:
                    settings = self._h3.received_settings
                    logger.debug(f"H3 event: SETTINGS received:")
                    if settings is not None:
                        for setting_id, value in settings.items():
                            logger.debug(f"  Setting 0x{setting_id:x} = {value}")
            except Exception as e:
                logger.error(f"H3 error: error handling event: {e}")
                raise
        else:
            logger.debug(f"QUIC event: event not handled({event_class})")
  
    def _h3_handle_event(self, event: QuicEvent) -> None:
        """Handle H3-specific events."""
        logger.debug(f"H3 event: _h3_handle_event {event}")
        if isinstance(event, HeadersReceived):
            return self._h3_handle_headers_received(event)
        msg_class = class_name(event)
        data = getattr(event, 'data', None)
        hex_data = f"0x{data.hex()}" if data is not None else "<no data>"
        logger.debug(f"H3 event: stream {event.stream_id}: {msg_class}: {hex_data}")
        # pass to parent H3 to handle - XX not required?
        # self._h3.handle_event(event)

    def _h3_handle_headers_received(self, event: HeadersReceived) -> None:
        """Process incoming H3 headers."""
        method = None
        protocol = None
        path = None
        authority = None
        status = None
        is_client = self._quic.configuration.is_client
        stream_id = event.stream_id
        logger.info(f"H3 event: HeadersReceived: session id: {stream_id} is_client: {is_client} ")
        for name, value in event.headers:
            logger.debug(f"  {name.decode()}: {value.decode()}")
            if name == b":method":
                method = value
            elif name == b":protocol":
                protocol = value
            elif name == b":path":
                path = value
            elif name == b":authority":
                authority = value
            elif name == b':status':
                status = value
                
        if is_client:
            if status == b"200":
                # Capture WT protocol negotiation result
                # Server may use wt-protocol or wt-selected-protocol
                for name, value in event.headers:
                    if name in (b"wt-protocol", b"wt-selected-protocol"):
                        self._wt_selected_protocol = value.decode().strip('"')
                        logger.info(f"H3 event: {name.decode()}: {self._wt_selected_protocol}")
                logger.debug(f"H3 event: WebTransport client session setup: session id: {stream_id}")
                self._wt_session_setup.set_result(True)
            else:
                error = f"WebTransport session setup failed ({status})"
                logger.error(f"H3 error: stream {stream_id}: " + error)
                self._close_session(ErrorCode.H3_CONNECT_ERROR, error)
        else:
            # Server: Handle incoming WebTransport CONNECT request
            if method == b"CONNECT" and protocol == b"webtransport":
                if self._endpoint_match(path):
                    self._session_id = stream_id
                    # Send 200 response with WebTransport headers
                    response_headers = [
                        (b":status", b"200"),
                        (b"server", USER_AGENT.encode()),
                        (b"sec-webtransport-http3-draft", b"draft02"),
                    ]
                    self._h3.send_headers(
                        stream_id=stream_id,
                        headers=response_headers,
                        end_stream=False
                    )
                    self.transmit()
                    logger.debug(f"H3 event: WebTransport server session setup: session id: {stream_id}")
                    self._wt_session_setup.set_result(True)
                else:
                    # Endpoint doesn't match, return 404
                    logger.warning(f"H3 event: path not found: {path}")
                    error_headers = [
                        (b":status", b"404"),
                        (b"server", USER_AGENT.encode()),
                    ]
                    self._h3.send_headers(
                        stream_id=stream_id,
                        headers=error_headers,
                        end_stream=True
                    )
                    self.transmit()
            else:
                # Unsupported HTTP transaction
                logger.warning(f"H3 event: path not found: {path}")
                error_headers = [
                    (b":status", b"500"),
                    (b"server", USER_AGENT.encode()),
                ]
                self._h3.send_headers(
                    stream_id=stream_id,
                    headers=error_headers,
                    end_stream=True
                )
                self.transmit()
            
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
        is_client = self._quic.configuration.is_client
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
            self.transmit()  # flush FINs before CONNECTION_CLOSE
        except Exception:
            pass  # best-effort during teardown

        if self._h3 is not None and self._session_id is not None:
            logger.debug(f"H3 session: closing: {class_name(self._h3)} "
                         f"({self._session_id})")
            self._session_id = None
        self._h3 = None

        # set the async exit condition for session
        if not self._moqt_session_closed.done():
            self._moqt_session_closed.set_result((error_code, reason_phrase))
        # close QUIC connection
        super().close()
        self.transmit()
        
    async def async_closed(self) -> bool:
        if not self._moqt_session_closed.done():
            self._close_err = await self._moqt_session_closed
        return True


    async def client_session_init(self, timeout: int = 10) -> bool:
        """Initialize WebTransport and MoQT client session."""

        use_quic = self._session.use_quic
        params = {}
        if use_quic:
            # Raw QUIC flow
            logger.info(f"MOQT: Using raw QUIC transport")
            
            # For raw QUIC, immediately mark WebTransport as "done" since we skip it
            self._wt_session_setup.set_result(True)
            
            # Create MoQT control stream (raw QUIC bidirectional stream)
            self._control_stream_id = self._quic.get_next_available_stream_id(is_unidirectional=False)
            logger.info(f"MOQT: QUIC control stream created stream id: {self._control_stream_id}")
            
            # CLIENT_SETUP parameters for raw QUIC (include PATH/AUTHORITY)
            endpoint = self._session.endpoint or ""
            params[SetupParamType.PATH] = f"/{endpoint}"
            params[SetupParamType.AUTHORITY] = f"{self._session.host}:{self._session.port}"
        else:
            # WebTransport over H3 flow
            self._session_id = self._h3._quic.get_next_available_stream_id(is_unidirectional=False)
            # Create WebTransport session
            host = self._session.host
            port = self._session.port
            # Build WT CONNECT headers
            draft = getattr(self._session, 'draft_version', None)
            headers = [
                (b":method", b"CONNECT"),
                (b":scheme", b"https"),
                (b":authority", f"{host}:{port}".encode()),
                (b":path", f"/{self._session.endpoint}".encode()),
                (b":protocol", b"webtransport"),
                (b"sec-webtransport-http3-draft", b"draft02"),
                (b"user-agent", USER_AGENT.encode()),
            ]
            # For draft-15+, include wt-available-protocols to negotiate
            # the MoQT version over WT. RFC 8941 quoted string.
            # Draft-14 predates this header — version negotiation is
            # entirely in-band via CLIENT_SETUP version array.
            if draft is not None and get_major_version(draft) >= 15:
                wt_proto = moqt_alpn_for_version(draft)
                headers.append(
                    (b"wt-available-protocols", f'"{wt_proto}"'.encode()),
                )

            logger.info(f"H3 send: WebTransport CONNECT: session id: {self._session_id}")
            for name, value in headers:
                logger.debug(f"  {name.decode()}: {value.decode()}")

            self._h3.send_headers(stream_id=self._session_id, headers=headers, end_stream=False)
            self.transmit()

            # Wait for WebTransport session establishment
            try:
                async with asyncio.timeout(timeout):
                    result = await self._wt_session_setup
                result = "SUCCESS" if result else "FAILED"
                logger.info(f"H3 event: WebTransport setup: {result}")
            except asyncio.TimeoutError:
                error = f"WebTransport session establishment timeout: {timeout} sec"
                logger.error("H3 error: " + error)
                self._close_session(SessionCloseCode.CONTROL_MESSAGE_TIMEOUT, error)
                raise MOQTException(*self._close_err)

            # Check for H3 connection close
            if self._close_err is not None:
                raise MOQTException(*self._close_err)

            # Set version context based on WT protocol negotiation
            if self._wt_selected_protocol:
                # Server negotiated a specific MoQT version via WT
                version = moqt_version_from_alpn(self._wt_selected_protocol)
                set_moqt_ctx_version(version)
                self._moqt_version = version
                logger.info(f"MOQT: version set from WT protocol: "
                            f"{self._wt_selected_protocol} -> 0x{version:x}")
            elif draft is not None:
                # Server didn't echo protocol — fall back to d14 in-band
                logger.info(f"MOQT: WT protocol not negotiated, "
                            f"falling back to in-band version negotiation")
                set_moqt_ctx_version(MOQT_VERSION_DRAFT14)
                self._moqt_version = MOQT_VERSION_DRAFT14

            # Create MoQT control stream
            self._control_stream_id = self._h3.create_webtransport_stream(session_id=self._session_id)
            logger.info(f"MOQT: WT control stream created stream id: {self._control_stream_id}")

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



    def open_uni_stream(self) -> int:
        """Open a unidirectional data stream. Returns the stream ID."""
        if self._h3 is not None:
            return self._h3.create_webtransport_stream(
                session_id=self._session_id, is_unidirectional=True)
        return self._quic.get_next_available_stream_id(is_unidirectional=True)

    def open_bidi_stream(self) -> int:
        """Open a bidirectional stream. Returns the stream ID."""
        if self._h3 is not None:
            return self._h3.create_webtransport_stream(
                session_id=self._session_id, is_unidirectional=False)
        return self._quic.get_next_available_stream_id(is_unidirectional=False)

    def stream_write(self, stream_id: int, data: bytes, end_stream: bool = False) -> None:
        """Write data to a stream. No-op if stream is reset or FIN'd."""
        if not self._stream_is_writable(stream_id) and not end_stream:
            return
        try:
            self._quic.send_stream_data(stream_id, data, end_stream=end_stream)
        except AssertionError:
            pass  # stream already FIN'd — race with close()

    def _stream_is_writable(self, stream_id: int) -> bool:
        """Check if a stream is still writable (not reset or FIN'd)."""
        stream = self._quic._streams.get(stream_id)
        if stream is None:
            return True  # stream not yet registered — allow write
        if stream.sender._reset_error_code is not None:
            return False
        if stream.sender._buffer_fin is not None:
            return False  # FIN already queued (close() sent it)
        return True

    async def stream_write_drain(self, stream_id: int, data: bytes,
                                 end_stream: bool = False) -> None:
        """Write data to a stream, respecting QUIC congestion control.

        Waits for the congestion window to have capacity before writing,
        preventing buffer bloat and relay overload. Returns silently if
        the stream has been reset or FIN'd (e.g. by close()).
        """
        if not self._stream_is_writable(stream_id):
            return

        # Wait for congestion window to have space
        loss = self._quic._loss
        while loss.bytes_in_flight >= loss.congestion_window:
            self.transmit()
            await asyncio.sleep(0.001)
            if not self._stream_is_writable(stream_id):
                return

        self._quic.send_stream_data(stream_id, data, end_stream=end_stream)

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
        self.transmit()

    def _handle_bidi_stream(self, stream_id: int, buf: Buffer, buf_len: int) -> None:
        """Handle d16 bidirectional stream messages (SUBSCRIBE_NAMESPACE, responses)."""
        # Check if this is a response to our outbound request
        request_id = self._bidi_stream_requests.get(stream_id)
        if request_id is not None:
            # Response on a bidi stream we opened — parse as control message
            while buf.tell() < buf_len:
                msg = self._moqt_handle_control_message(buf)
                if msg is None:
                    break
            return

        # New incoming bidi stream from peer — strip WT stream header if present
        if self._h3 is not None:
            buf.pull_uint_var()  # WT session/stream identifier
            buf.pull_uint_var()

        # Parse the message
        msg = self._moqt_handle_control_message(buf)
        if msg is not None and isinstance(msg, SubscribeNamespace):
            # Track the stream so we can respond on it
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
        content_exists: int = 0,
        parameters: Optional[Dict[int, Any]] = None,
        wait_response: Optional[bool] = False,
    ) -> Optional[MOQTMessage]:
        """PUBLISH — announce a specific track to the relay/subscriber."""
        namespace_tuple = self._make_namespace_tuple(namespace)
        request_id = self._allocate_request_id()
        track_alias = self._allocate_track_alias(request_id)
        track_name_bytes = track_name.encode() if isinstance(track_name, str) else track_name
        message = Publish(
            request_id=request_id,
            track_namespace=namespace_tuple,
            track_name=track_name_bytes,
            track_alias=track_alias,
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
        """Create and send a ANNOUNCE_OK response."""
        message = PublishNamespaceOk(
            request_id=msg.request_id,
        )
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

    def subscribe_namespace(
        self,
        namespace_prefix: str,
        parameters: Optional[Dict[int, bytes]] = None,
        wait_response: Optional[bool] = False
    ) -> Optional[MOQTMessage]:
        """Subscribe to announcements for a namespace prefix.

        In d16+, sends on a new bidirectional stream per spec Section 9.25.
        In d14, sends on the control stream.
        """
        if parameters is None:
            parameters = {}

        prefix = self._make_namespace_tuple(namespace_prefix)
        request_id = self._allocate_request_id()
        message = SubscribeNamespace(
            request_id=request_id,
            namespace_prefix=prefix,
            parameters=parameters
        )
        logger.info(f"MOQT send: {message}")

        if is_draft16_or_later():
            stream_id = self.open_bidi_stream()
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

    async def await_publish(self, timeout: float = 10.0):
        """Wait for a PUBLISH (track announcement) from the relay.

        Returns the Publish message with track_namespace and track_name,
        or raises TimeoutError if no announcement arrives within timeout.
        """
        return await asyncio.wait_for(
            self._publish_announcements.get(), timeout=timeout)

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

        if not self._quic.configuration.is_client:
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
        if self._quic.configuration.is_client:
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
            # indicate moqt session setup is complete
            if MOQT_CUR_VERSION in msg.versions:
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
        # PublishNamespaceDone is a notification, no response required

    async def _handle_publish_namepace_cancel(self, msg: PublishNamespaceCancel) -> None:
        logger.info(f"MOQT event: handle {msg}")
        # Handle announcement cancellation

    async def _handle_unsubscribe(self, msg: Unsubscribe) -> None:
        logger.info(f"MOQT event: handle {msg}")
        # Handle unsubscribe request

    async def _handle_subscribe_done(self, msg: SubscribeDone) -> None:
        logger.info(f"MOQT event: handle {msg}")
        self._resolve_request(msg.request_id, msg)
        # Publisher is done — close session gracefully
        self._close_session(SessionCloseCode.NO_ERROR,
                            "publisher done")

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
    
    MOQT_DGRAM_DATA_REGISTRY: Dict[int, Tuple[Type[MOQTMessage], Callable]] = {
        # ObjectDatagram: types 0x00-0x07 dispatched by range check
        # ObjectDatagramStatus: types 0x20-0x21 dispatched by range check
    }
    
    MOQT_DGRAM_DATA_REGISTRY: Dict[int, Tuple[Type[MOQTMessage], Callable]] = {
        # ObjectDatagram: types 0x00-0x07 dispatched by range check
        # ObjectDatagramStatus: types 0x20-0x21 dispatched by range check
    }