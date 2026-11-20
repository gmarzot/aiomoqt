from ..types import *
from typing import Tuple, Dict, Optional, Any
from dataclasses import dataclass, field

from . import MOQTMessage, BUF_SIZE
from .params import LocationFilter, uses_location_filter
from ..types import ParamType, SubscribeDoneCode
from ..context import DraftProfile
from ..utils.buffer import Buffer
from ..utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class TrackStatus(MOQTMessage):
    """TRACK_STATUS (0x0D) — SUBSCRIBE's format, sent with no delivery
    parameters (§10.14). The response is REQUEST_OK/REQUEST_ERROR (no
    Track Alias).
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

        # §10.14: delivery parameters (priority, group order, forward,
        # filter) are not included; only the caller's, e.g. AUTH_TOKEN.
        MOQTMessage._serialize_params(payload, self.parameters or {},
                                      prof=prof)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'TrackStatus':

        request_id = buf.pull_vint()

        namespace, track_name = MOQTMessage._pull_full_track_name(
            buf, prof=prof)

        filter_type = None
        start_group = None
        start_object = None
        end_group = None

        params = MOQTMessage._deserialize_params(buf, prof=prof, buf_end=buf_end,
                                             scope=cls.__name__)
        priority = params.pop(ParamType.SUBSCRIBER_PRIORITY, None)
        group_order = params.pop(ParamType.GROUP_ORDER, None)
        forward = params.pop(ParamType.FORWARD, None)
        filter_raw = params.pop(ParamType.SUBSCRIPTION_FILTER, None)
        if filter_raw is not None:
            (filter_type, start_group, start_object,
             end_group) = MOQTMessage._decode_filter(filter_raw, prof=prof)

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
    # d20+ LOCATION_FILTER. Sent in place of filter_type when set; on
    # receive it is the filter, and filter_type and the Locations hold
    # its d16-d18 form when one exists.
    location_filter: Optional[LocationFilter] = None
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

        # priority, group_order, forward, filter all go into params
        params = dict(self.parameters or {})
        if self.priority is not None:
            params[ParamType.SUBSCRIBER_PRIORITY] = self.priority
        # No 0 value; omission means the publisher's order.
        if self.group_order not in (None, GroupOrder.PUBLISHER_DEFAULT):
            params[ParamType.GROUP_ORDER] = self.group_order
        if self.forward is not None:
            params[ParamType.FORWARD] = self.forward
        if uses_location_filter(prof):
            lf = self.location_filter
            if lf is None and self.filter_type is not None:
                lf = LocationFilter.from_subscription_filter(
                    self.filter_type, self.start_group,
                    self.start_object, self.end_group)
            if lf is not None:
                params[ParamType.LOCATION_FILTER] = lf
        elif self.filter_type is not None:
            # SUBSCRIPTION_FILTER param (0x21) is odd → bytes value
            # Encode: filter_type varint [+ start_group + start_obj [+ end_group]]
            # Filter internals follow the negotiated varint codec
            # (vi64 on d18); d18 carries End Group as a delta from
            # Start Group (moxygen MoQFramer: end - start, >= 0).
            fbuf = Buffer(capacity=64, vi64=prof.vi64)
            fbuf.push_vint(self.filter_type)
            if self.filter_type in (3, 4):
                fbuf.push_vint(self.start_group or 0)
                fbuf.push_vint(self.start_object or 0)
            if self.filter_type == 4:
                end = self.end_group or 0
                if prof.vi64:
                    start = self.start_group or 0
                    if end < start:
                        raise ValueError(
                            f"end_group {end} < start_group {start}")
                    end -= start
                fbuf.push_vint(end)
            params[ParamType.SUBSCRIPTION_FILTER] = fbuf.data_slice(0, fbuf.tell())
        MOQTMessage._serialize_params(payload, params, prof=prof)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'Subscribe':

        request_id = buf.pull_vint()

        namespace, track_name = MOQTMessage._pull_full_track_name(
            buf, prof=prof)

        priority = None
        group_order = None
        forward = None
        filter_type = None
        start_group = None
        start_object = None
        end_group = None
        location_filter = None

        # All fields are in parameters
        params = MOQTMessage._deserialize_params(buf, prof=prof, buf_end=buf_end,
                                             scope=cls.__name__)
        priority = params.pop(ParamType.SUBSCRIBER_PRIORITY, None)
        group_order = params.pop(ParamType.GROUP_ORDER, None)
        forward = params.pop(ParamType.FORWARD, None)
        filter_raw = params.pop(ParamType.SUBSCRIPTION_FILTER, None)
        if isinstance(filter_raw, LocationFilter):
            location_filter = filter_raw
            legacy = filter_raw.subscription_filter()
            if legacy is not None:
                filter_type, start_group, start_object, end_group = legacy
        elif filter_raw is not None:
            (filter_type, start_group, start_object,
             end_group) = MOQTMessage._decode_filter(filter_raw, prof=prof)

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
            parameters=params,
            location_filter=location_filter,
        )


@dataclass(slots=True)
class SubscribeOk(MOQTMessage):
    """SUBSCRIBE_OK (0x04).

    Request ID, Track Alias, Params, Track Extensions
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
    track_extensions: Optional[Dict[int, Any]] = None

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

        # expires and largest_object go into params
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
        # Track Extensions; DEFAULT_PUBLISHER_GROUP_ORDER (0x22)
        # omitted means Ascending.
        exts = dict(self.track_extensions or {})
        if self.group_order == GroupOrder.DESCENDING:
            exts[0x22] = self.group_order
        MOQTMessage._extensions_encode(payload, exts, with_length=False)

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

        params = MOQTMessage._deserialize_params(buf, prof=prof, buf_end=buf_end,
                                             scope=cls.__name__)
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
        MOQTMessage._check_track_properties(track_extensions,
                                            prof=prof)
        group_order = GroupOrder.ASCENDING
        if track_extensions is not None:
            group_order_val = track_extensions.pop(0x22, None)
            if group_order_val is not None:
                MOQTMessage._check_range(
                    "default publisher group order",
                    group_order_val, 1, 2)
                group_order = GroupOrder(group_order_val)

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

    # d18 §15.10.3 swapped two codes relative to d16: on the d18 wire
    # TOO_FAR_BEHIND=0x5 and EXPIRED=0x6. Canonical enum keeps the d16
    # values; the swap (its own inverse) is applied at the wire.
    _D18_WIRE_SWAP = {0x05: 0x06, 0x06: 0x05}

    def __post_init__(self):
        self.type = MOQTMessageType.PUBLISH_DONE

    def serialize(self, *, prof: DraftProfile) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)

        # d18 replies omit the Request ID (demuxed by request stream, §10.1).
        if prof.reply_has_request_id:
            payload.push_vint(self.request_id)
        code = int(self.status_code)
        if (code == SubscribeDoneCode.SUBSCRIPTION_ENDED
                and not prof.publish_done_subscription_ended):
            raise ValueError(
                f"PUBLISH_DONE SUBSCRIPTION_ENDED does not exist at "
                f"d{prof.draft}: a subscription does not end at its "
                f"filter's end")
        if prof.draft >= 18:
            code = self._D18_WIRE_SWAP.get(code, code)
        payload.push_vint(code)
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
        if prof.draft >= 18:
            status_code = cls._D18_WIRE_SWAP.get(status_code, status_code)
        stream_count = buf.pull_vint()
        reason = MOQTMessage._pull_reason(buf)

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
