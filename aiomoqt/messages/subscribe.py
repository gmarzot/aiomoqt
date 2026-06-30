from ..types import *
from typing import Tuple, Dict, Optional, Any
from dataclasses import dataclass, field

from . import MOQTMessage, BUF_SIZE
from ..context import is_draft16_or_later, DraftProfile
from ..utils.buffer import Buffer, BufferReadError
from ..utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class TrackStatus(MOQTMessage):
    """TRACK_STATUS (0x0D) — identical format to SUBSCRIBE.

    Version branching same as Subscribe.
    Draft-16 response is REQUEST_OK/REQUEST_ERROR (no Track Alias).
    """
    request_id: int = 0
    track_namespace: Tuple[bytes, ...] = None
    track_name: bytes = None
    priority: int = None
    group_order: int = None
    forward: int = None
    filter_type: int = None
    start_group: Optional[int] = None
    start_object: Optional[int] = None
    end_group: Optional[int] = None
    parameters: Optional[Dict[int, Any]] = None

    def __post_init__(self):
        self.type = MOQTMessageType.TRACK_STATUS

    def serialize(self, *, prof: DraftProfile) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)

        payload.push_vint(self.request_id)

        payload.push_vint(len(self.track_namespace))
        for part in self.track_namespace:
            payload.push_vint(len(part))
            payload.push_bytes(part)

        payload.push_vint(len(self.track_name))
        payload.push_bytes(self.track_name)

        if is_draft16_or_later(prof.draft):
            params = dict(self.parameters or {})
            if self.priority is not None:
                params[ParamType.SUBSCRIBER_PRIORITY] = self.priority
            if self.group_order is not None:
                params[ParamType.GROUP_ORDER] = self.group_order
            if self.forward is not None:
                params[ParamType.FORWARD] = self.forward
            if self.filter_type is not None:
                fbuf = Buffer(capacity=64)
                fbuf.push_uint_var(self.filter_type)
                if self.filter_type in (3, 4):
                    fbuf.push_uint_var(self.start_group or 0)
                    fbuf.push_uint_var(self.start_object or 0)
                if self.filter_type == 4:
                    fbuf.push_uint_var(self.end_group or 0)
                params[ParamType.SUBSCRIPTION_FILTER] = fbuf.data_slice(0, fbuf.tell())
            MOQTMessage._serialize_params(payload, params, prof=prof)
        else:
            payload.push_uint8(self.priority)
            payload.push_uint8(self.group_order)
            payload.push_uint8(self.forward)
            payload.push_vint(self.filter_type)
            if self.filter_type in (3, 4):
                payload.push_vint(self.start_group or 0)
                payload.push_vint(self.start_object or 0)
            if self.filter_type == 4:
                payload.push_vint(self.end_group or 0)
            MOQTMessage._serialize_params(payload, self.parameters or {}, prof=prof)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'TrackStatus':

        request_id = buf.pull_vint()

        tuple_len = buf.pull_vint()
        namespace = tuple(buf.pull_bytes(buf.pull_vint()) for _ in range(tuple_len))

        track_name_len = buf.pull_vint()
        track_name = buf.pull_bytes(track_name_len)

        priority = None
        group_order = None
        forward = None
        filter_type = None
        start_group = None
        start_object = None
        end_group = None

        if is_draft16_or_later(prof.draft):
            params = MOQTMessage._deserialize_params(buf, prof=prof, buf_end=buf_end)
            priority = params.pop(ParamType.SUBSCRIBER_PRIORITY, None)
            group_order = params.pop(ParamType.GROUP_ORDER, None)
            forward = params.pop(ParamType.FORWARD, None)
            filter_raw = params.pop(ParamType.SUBSCRIPTION_FILTER, None)
            if filter_raw is not None:
                fbuf = Buffer(data=filter_raw)
                filter_type = fbuf.pull_vint()
                if filter_type in (3, 4):
                    start_group = fbuf.pull_vint()
                    start_object = fbuf.pull_vint()
                if filter_type == 4:
                    end_group = fbuf.pull_vint()
        else:
            priority = buf.pull_uint8()
            group_order = buf.pull_uint8()
            forward = buf.pull_uint8()
            filter_type = buf.pull_vint()
            if filter_type in (3, 4):
                start_group = buf.pull_vint()
                start_object = buf.pull_vint()
            if filter_type == 4:
                end_group = buf.pull_vint()
            params = MOQTMessage._deserialize_params(buf, prof=prof, buf_end=buf_end)

        return cls(
            request_id=request_id,
            track_namespace=namespace,
            track_name=track_name,
            priority=priority,
            group_order=group_order,
            forward=forward,
            filter_type=filter_type,
            start_group=start_group,
            start_object=start_object,
            end_group=end_group,
            parameters=params
        )


@dataclass(slots=True)
class TrackStatusOk(MOQTMessage):
    """TRACK_STATUS_OK (0x0E) — identical format to SUBSCRIBE_OK."""
    request_id: int = 0
    track_alias: int = 0
    expires: int = None
    group_order: GroupOrder = None
    content_exists: ContentExistsCode = None
    largest_group_id: Optional[int] = None
    largest_object_id: Optional[int] = None
    parameters: Optional[Dict[int, Any]] = None

    def __post_init__(self):
        self.type = MOQTMessageType.TRACK_STATUS_OK

    def serialize(self, *, prof: DraftProfile) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)

        payload.push_vint(self.request_id)
        payload.push_vint(self.track_alias)
        payload.push_vint(self.expires)
        payload.push_uint8(self.group_order.value)
        payload.push_uint8(self.content_exists)

        if self.content_exists == ContentExistsCode.EXISTS:
            payload.push_vint(self.largest_group_id)
            payload.push_vint(self.largest_object_id)

        MOQTMessage._serialize_params(payload, self.parameters or {}, prof=prof)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'TrackStatusOk':
        request_id = buf.pull_vint()
        track_alias = buf.pull_vint()
        expires = buf.pull_vint()
        group_order = GroupOrder(buf.pull_uint8())
        content_exists = buf.pull_uint8()

        largest_group_id = None
        largest_object_id = None
        if content_exists == ContentExistsCode.EXISTS:
            largest_group_id = buf.pull_vint()
            largest_object_id = buf.pull_vint()

        params = MOQTMessage._deserialize_params(buf, prof=prof, buf_end=buf_end)

        return cls(
            request_id=request_id,
            track_alias=track_alias,
            expires=expires,
            group_order=group_order,
            content_exists=content_exists,
            largest_group_id=largest_group_id,
            largest_object_id=largest_object_id,
            parameters=params
        )


@dataclass(slots=True)
class TrackStatusError(MOQTMessage):
    """TRACK_STATUS_ERROR (0x0F) — identical format to SUBSCRIBE_ERROR."""
    request_id: int = None
    error_code: int = None
    reason: str = None

    def __post_init__(self):
        self.type = MOQTMessageType.TRACK_STATUS_ERROR

    def serialize(self, *, prof: DraftProfile) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)

        payload.push_vint(self.request_id)
        payload.push_vint(self.error_code.value if isinstance(self.error_code, SubscribeErrorCode) else self.error_code)

        reason_bytes = self.reason.encode()
        payload.push_vint(len(reason_bytes))
        payload.push_bytes(reason_bytes)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'TrackStatusError':

        request_id = buf.pull_vint()
        error_code = buf.pull_vint()
        reason_len = buf.pull_vint()
        reason = buf.pull_bytes(reason_len).decode()

        return cls(
            request_id=request_id,
            error_code=error_code,
            reason=reason,
        )


@dataclass(slots=True)
class Subscribe(MOQTMessage):
    request_id: int = 0  # This is the Request ID
    track_namespace: Tuple[bytes, ...] = None
    track_name: bytes = None
    priority: int = None
    group_order: int = None
    forward: int = None
    filter_type: int = None
    start_group: Optional[int] = None
    start_object: Optional[int] = None
    end_group: Optional[int] = None
    parameters: Optional[Dict[int, Any]] = None
    # Server-side runtime: set by allocate_track_alias when responding
    # with SubscribeOk. Not on the wire (Subscribe doesn't carry alias);
    # declared as a slot field so server handlers can assign it.
    track_alias: Optional[int] = field(default=None, init=False)
    # Runtime: client-side libquicr filter encoding flag, set by the
    # protocol layer just before serialize so the LAPS variant of
    # filter encoding is emitted. Not on the wire as such.
    libquicr_compat: bool = field(default=False, init=False)

    def __post_init__(self):
        self.type = MOQTMessageType.SUBSCRIBE

    def serialize(self, *, prof: DraftProfile) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)

        payload.push_vint(self.request_id)

        # Namespace tuple
        payload.push_vint(len(self.track_namespace))
        for part in self.track_namespace:
            payload.push_vint(len(part))
            payload.push_bytes(part)

        payload.push_vint(len(self.track_name))
        payload.push_bytes(self.track_name)

        if is_draft16_or_later(prof.draft):
            # d16: priority, group_order, forward, filter all go into params
            params = dict(self.parameters or {})
            if self.priority is not None:
                params[ParamType.SUBSCRIBER_PRIORITY] = self.priority
            if self.group_order is not None:
                params[ParamType.GROUP_ORDER] = self.group_order
            if self.forward is not None:
                params[ParamType.FORWARD] = self.forward
            if self.filter_type is not None:
                # SUBSCRIPTION_FILTER param (0x21) is odd → bytes value
                # Encode: filter_type varint [+ start_group + start_obj [+ end_group]]
                fbuf = Buffer(capacity=64)
                fbuf.push_uint_var(self.filter_type)
                if self.filter_type in (3, 4):
                    fbuf.push_uint_var(self.start_group or 0)
                    fbuf.push_uint_var(self.start_object or 0)
                if self.filter_type == 4:
                    fbuf.push_uint_var(self.end_group or 0)
                params[ParamType.SUBSCRIPTION_FILTER] = fbuf.data_slice(0, fbuf.tell())
            MOQTMessage._serialize_params(payload, params, prof=prof)
        else:
            # d14: priority / group_order / forward are mandatory fixed
            # fields on the wire (no optional form like d16's params), so
            # substitute defaults when the caller left them unset (None).
            payload.push_uint8(self.priority
                               if self.priority is not None else 128)
            payload.push_uint8(self.group_order
                               if self.group_order is not None
                               else GroupOrder.ASCENDING)
            payload.push_uint8(self.forward
                               if self.forward is not None else 1)
            payload.push_vint(self.filter_type)

            if self.filter_type in (3, 4):
                payload.push_vint(self.start_group or 0)
                payload.push_vint(self.start_object or 0)

            if self.filter_type == 4:
                payload.push_vint(self.end_group or 0)

            MOQTMessage._serialize_params(payload, self.parameters or {}, prof=prof)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'Subscribe':

        request_id = buf.pull_vint()

        tuple_len = buf.pull_vint()
        namespace = tuple(buf.pull_bytes(buf.pull_vint()) for _ in range(tuple_len))

        track_name_len = buf.pull_vint()
        track_name = buf.pull_bytes(track_name_len)

        priority = None
        group_order = None
        forward = None
        filter_type = None
        start_group = None
        start_object = None
        end_group = None

        if is_draft16_or_later(prof.draft):
            # d16: all fields are in parameters
            params = MOQTMessage._deserialize_params(buf, prof=prof, buf_end=buf_end)
            priority = params.pop(ParamType.SUBSCRIBER_PRIORITY, None)
            group_order = params.pop(ParamType.GROUP_ORDER, None)
            forward = params.pop(ParamType.FORWARD, None)
            filter_raw = params.pop(ParamType.SUBSCRIPTION_FILTER, None)
            if filter_raw is not None:
                fbuf = Buffer(data=filter_raw)
                filter_type = fbuf.pull_vint()
                if filter_type in (3, 4):
                    start_group = fbuf.pull_vint()
                    start_object = fbuf.pull_vint()
                if filter_type == 4:
                    end_group = fbuf.pull_vint()
        else:
            # d14: fixed fields on wire
            priority = buf.pull_uint8()
            group_order = buf.pull_uint8()
            forward = buf.pull_uint8()
            filter_type = buf.pull_vint()

            if filter_type in (3, 4):
                start_group = buf.pull_vint()
                start_object = buf.pull_vint()
            if filter_type == 4:
                end_group = buf.pull_vint()

            params = MOQTMessage._deserialize_params(buf, prof=prof, buf_end=buf_end)

        return cls(
            request_id=request_id,
            track_namespace=namespace,
            track_name=track_name,
            priority=priority,
            group_order=group_order,
            forward=forward,
            filter_type=filter_type,
            start_group=start_group,
            start_object=start_object,
            end_group=end_group,
            parameters=params
        )


@dataclass(slots=True)
class SubscribeOk(MOQTMessage):
    """SUBSCRIBE_OK (0x04).

    Draft-14: Request ID, Track Alias, Expires, Group Order (8),
              Content Exists (8), [Location], Params
    Draft-16: Request ID, Track Alias, Params, Track Extensions
              (expires, group_order, content/location all in params/extensions)
    """
    request_id: int = 0
    track_alias: int = 0
    expires: int = None
    group_order: GroupOrder = None
    content_exists: ContentExistsCode = None
    largest_group_id: Optional[int] = None
    largest_object_id: Optional[int] = None
    parameters: Optional[Dict[int, Any]] = None
    track_extensions: Optional[Dict[int, Any]] = None  # d16 only

    def __post_init__(self):
        self.type = MOQTMessageType.SUBSCRIBE_OK

    def serialize(self, *, prof: DraftProfile) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)

        # d18 replies omit the Request ID — the response is demuxed by the
        # bidi request stream it arrives on (§10.1).
        if prof.reply_has_request_id:
            payload.push_vint(self.request_id)
        payload.push_vint(self.track_alias)

        if is_draft16_or_later(prof.draft):
            # d16: expires and largest_object go into params
            params = dict(self.parameters or {})
            if self.expires is not None:
                params[ParamType.EXPIRES] = self.expires
            if self.largest_group_id is not None:
                if ParamType.LARGEST_OBJECT in prof.location_params:
                    # d18: LARGEST_OBJECT is an inline Location value —
                    # (group, object) tuple, encoded as two bare varints.
                    params[ParamType.LARGEST_OBJECT] = (
                        self.largest_group_id, self.largest_object_id or 0)
                else:
                    # d16: LARGEST_OBJECT (0x09, odd) = length-prefixed
                    # bytes(group_id varint + object_id varint)
                    lbuf = Buffer(capacity=16)
                    lbuf.push_uint_var(self.largest_group_id)
                    lbuf.push_uint_var(self.largest_object_id or 0)
                    params[ParamType.LARGEST_OBJECT] = lbuf.data_slice(0, lbuf.tell())
            MOQTMessage._serialize_params(payload, params, prof=prof)
            # Track Extensions (group_order goes here as extension 0x22)
            exts = dict(self.track_extensions or {})
            if self.group_order is not None:
                exts[0x22] = self.group_order  # DEFAULT_PUBLISHER_GROUP_ORDER
            MOQTMessage._extensions_encode(payload, exts, with_length=False)
        else:
            # d14: fixed fields
            payload.push_vint(self.expires)
            payload.push_uint8(self.group_order.value)
            payload.push_uint8(self.content_exists)
            if self.content_exists == ContentExistsCode.EXISTS:
                payload.push_vint(self.largest_group_id)
                payload.push_vint(self.largest_object_id)
            MOQTMessage._serialize_params(payload, self.parameters or {}, prof=prof)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'SubscribeOk':
        # buf_end is the absolute end-of-message position derived from
        # the outer frame length. Required for d16 (Track Extensions
        # have no length prefix; sequence runs to end of message).
        # d18 replies omit the Request ID (demuxed by request stream).
        request_id = (buf.pull_vint()
                      if prof.reply_has_request_id else None)
        track_alias = buf.pull_vint()

        expires = None
        group_order = None
        content_exists = None
        largest_group_id = None
        largest_object_id = None
        track_extensions = None

        if is_draft16_or_later(prof.draft):
            params = MOQTMessage._deserialize_params(buf, prof=prof, buf_end=buf_end)
            expires = params.pop(ParamType.EXPIRES, None)
            largest = params.pop(ParamType.LARGEST_OBJECT, None)
            if largest is not None:
                if isinstance(largest, tuple):
                    # d18: inline Location value (group, object)
                    largest_group_id, largest_object_id = largest
                else:
                    # d16: length-prefixed bytes(group varint + object varint)
                    lbuf = Buffer(data=largest)
                    largest_group_id = lbuf.pull_vint()
                    largest_object_id = lbuf.pull_vint()
                content_exists = ContentExistsCode.EXISTS
            else:
                content_exists = ContentExistsCode.NO_CONTENT
            track_extensions = MOQTMessage._extensions_decode(
                buf, with_length=False, buf_end=buf_end)
            if track_extensions is not None:
                group_order_val = track_extensions.pop(0x22, None)
                if group_order_val is not None:
                    group_order = GroupOrder(group_order_val)
        else:
            expires = buf.pull_vint()
            group_order = GroupOrder(buf.pull_uint8())
            content_exists = buf.pull_uint8()
            if content_exists == ContentExistsCode.EXISTS:
                largest_group_id = buf.pull_vint()
                largest_object_id = buf.pull_vint()
            params = MOQTMessage._deserialize_params(buf, prof=prof, buf_end=buf_end)

        return cls(
            request_id=request_id,
            track_alias=track_alias,
            expires=expires,
            group_order=group_order,
            content_exists=content_exists,
            largest_group_id=largest_group_id,
            largest_object_id=largest_object_id,
            parameters=params,
            track_extensions=track_extensions,
        )


@dataclass(slots=True)
class SubscribeError(MOQTMessage):
    request_id: int = None
    error_code: int = None
    reason: str = None

    def __post_init__(self):
        self.type = MOQTMessageType.SUBSCRIBE_ERROR

    def serialize(self, *, prof: DraftProfile) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)

        payload.push_vint(self.request_id)
        payload.push_vint(self.error_code.value if isinstance(self.error_code, SubscribeErrorCode) else self.error_code)

        reason_bytes = self.reason.encode()
        payload.push_vint(len(reason_bytes))
        payload.push_bytes(reason_bytes)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'SubscribeError':

        request_id = buf.pull_vint()
        error_code = buf.pull_vint()
        reason_len = buf.pull_vint()
        reason = buf.pull_bytes(reason_len).decode()

        return cls(
            request_id=request_id,
            error_code=error_code,
            reason=reason,
        )


@dataclass(slots=True)
class SubscribeUpdate(MOQTMessage):
    request_id: int = None
    subscription_request_id: int = None
    start_group: int = None
    start_object: int = None
    end_group: int = None
    priority: int = None
    forward: int = None
    parameters: Optional[Dict[int, Any]] = None

    def __post_init__(self):
        self.type = MOQTMessageType.SUBSCRIBE_UPDATE

    def serialize(self, *, prof: DraftProfile) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)

        payload.push_vint(self.request_id)
        payload.push_vint(self.subscription_request_id)
        payload.push_vint(self.start_group)
        payload.push_vint(self.start_object)
        payload.push_vint(self.end_group)
        payload.push_uint8(self.priority)
        payload.push_uint8(self.forward)

        MOQTMessage._serialize_params(payload, self.parameters or {}, prof=prof)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'SubscribeUpdate':

        request_id = buf.pull_vint()
        subscription_request_id = buf.pull_vint()
        start_group = buf.pull_vint()
        start_object = buf.pull_vint()
        end_group = buf.pull_vint()
        priority = buf.pull_uint8()
        forward = buf.pull_uint8()
        params = MOQTMessage._deserialize_params(buf, prof=prof, buf_end=buf_end)

        return cls(
            request_id=request_id,
            subscription_request_id=subscription_request_id,
            start_group=start_group,
            start_object=start_object,
            end_group=end_group,
            priority=priority,
            forward=forward,
            parameters=params
        )


@dataclass(slots=True)
class Unsubscribe(MOQTMessage):
    request_id: int = None

    def __post_init__(self):
        self.type = MOQTMessageType.UNSUBSCRIBE

    def serialize(self, *, prof: DraftProfile) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)

        payload.push_vint(self.request_id)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'Unsubscribe':

        request_id = buf.pull_vint()
        return cls(request_id=request_id)


@dataclass(slots=True)
class SubscribeDone(MOQTMessage):
    request_id: int = None
    status_code: int = None
    stream_count: int = None
    reason: str = None

    def __post_init__(self):
        self.type = MOQTMessageType.PUBLISH_DONE

    def serialize(self, *, prof: DraftProfile) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)

        # d18 replies omit the Request ID (demuxed by request stream, §10.1).
        if prof.reply_has_request_id:
            payload.push_vint(self.request_id)
        payload.push_vint(self.status_code.value if isinstance(self.status_code, SubscribeDoneCode) else self.status_code)
        payload.push_vint(self.stream_count)
        
        reason_bytes = self.reason.encode()
        payload.push_vint(len(reason_bytes))
        payload.push_bytes(reason_bytes)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'SubscribeDone':

        # d18 replies omit the Request ID (demuxed by request stream).
        request_id = (buf.pull_vint()
                      if prof.reply_has_request_id else None)
        status_code = buf.pull_vint()
        stream_count = buf.pull_vint()
        reason_len = buf.pull_vint()
        reason = buf.pull_bytes(reason_len).decode()

        return cls(
            request_id=request_id,
            status_code=status_code,
            stream_count=stream_count,
            reason=reason
        )


@dataclass(slots=True)
class MaxSubscribeId(MOQTMessage):
    request_id: int = None

    def __post_init__(self):
        self.type = MOQTMessageType.MAX_REQUEST_ID

    def serialize(self, *, prof: DraftProfile) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)

        payload.push_vint(self.request_id)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'MaxSubscribeId':

        request_id = buf.pull_vint()
        return cls(request_id=request_id)


@dataclass(slots=True)
class SubscribesBlocked(MOQTMessage):
    maximum_request_id: int = None

    def __post_init__(self):
        self.type = MOQTMessageType.REQUESTS_BLOCKED

    def serialize(self, *, prof: DraftProfile) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)

        payload.push_vint(self.maximum_request_id)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'SubscribesBlocked':

        maximum_request_id = buf.pull_vint()
        return cls(maximum_request_id=maximum_request_id)