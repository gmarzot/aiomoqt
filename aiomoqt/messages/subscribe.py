from ..types import *
from typing import Tuple, Dict, Optional, Any
from dataclasses import dataclass

from . import MOQTMessage, BUF_SIZE
from ..utils.buffer import Buffer, BufferReadError
from ..utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TrackStatus(MOQTMessage):
    """TRACK_STATUS (0x0D) — identical format to SUBSCRIBE."""
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

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)

        payload.push_uint_var(len(self.track_namespace))
        for part in self.track_namespace:
            payload.push_uint_var(len(part))
            payload.push_bytes(part)

        payload.push_uint_var(len(self.track_name))
        payload.push_bytes(self.track_name)
        payload.push_uint8(self.priority)
        payload.push_uint8(self.group_order)
        payload.push_uint8(self.forward)
        payload.push_uint_var(self.filter_type)

        if self.filter_type in (3, 4):
            payload.push_uint_var(self.start_group or 0)
            payload.push_uint_var(self.start_object or 0)
        if self.filter_type == 4:
            payload.push_uint_var(self.end_group or 0)

        MOQTMessage._serialize_params(payload, self.parameters or {})

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'TrackStatus':

        request_id = buf.pull_uint_var()

        tuple_len = buf.pull_uint_var()
        namespace = tuple(buf.pull_bytes(buf.pull_uint_var()) for _ in range(tuple_len))

        track_name_len = buf.pull_uint_var()
        track_name = buf.pull_bytes(track_name_len)
        priority = buf.pull_uint8()
        group_order = buf.pull_uint8()
        forward = buf.pull_uint8()
        filter_type = buf.pull_uint_var()

        start_group = None
        start_object = None
        end_group = None
        if filter_type in (3, 4):
            start_group = buf.pull_uint_var()
            start_object = buf.pull_uint_var()
        if filter_type == 4:
            end_group = buf.pull_uint_var()

        params = MOQTMessage._deserialize_params(buf)

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


@dataclass
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

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)
        payload.push_uint_var(self.track_alias)
        payload.push_uint_var(self.expires)
        payload.push_uint8(self.group_order.value)
        payload.push_uint8(self.content_exists)

        if self.content_exists == ContentExistsCode.EXISTS:
            payload.push_uint_var(self.largest_group_id)
            payload.push_uint_var(self.largest_object_id)

        MOQTMessage._serialize_params(payload, self.parameters or {})

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'TrackStatusOk':
        request_id = buf.pull_uint_var()
        track_alias = buf.pull_uint_var()
        expires = buf.pull_uint_var()
        group_order = GroupOrder(buf.pull_uint8())
        content_exists = buf.pull_uint8()

        largest_group_id = None
        largest_object_id = None
        if content_exists == ContentExistsCode.EXISTS:
            largest_group_id = buf.pull_uint_var()
            largest_object_id = buf.pull_uint_var()

        params = MOQTMessage._deserialize_params(buf)

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


@dataclass
class TrackStatusError(MOQTMessage):
    """TRACK_STATUS_ERROR (0x0F) — identical format to SUBSCRIBE_ERROR."""
    request_id: int = None
    error_code: int = None
    reason: str = None

    def __post_init__(self):
        self.type = MOQTMessageType.TRACK_STATUS_ERROR

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)
        payload.push_uint_var(self.error_code.value if isinstance(self.error_code, SubscribeErrorCode) else self.error_code)

        reason_bytes = self.reason.encode()
        payload.push_uint_var(len(reason_bytes))
        payload.push_bytes(reason_bytes)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'TrackStatusError':

        request_id = buf.pull_uint_var()
        error_code = buf.pull_uint_var()
        reason_len = buf.pull_uint_var()
        reason = buf.pull_bytes(reason_len).decode()

        return cls(
            request_id=request_id,
            error_code=error_code,
            reason=reason,
        )


@dataclass
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

    def __post_init__(self):
        self.type = MOQTMessageType.SUBSCRIBE

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)

        # Namespace tuple
        payload.push_uint_var(len(self.track_namespace))
        for part in self.track_namespace:
            payload.push_uint_var(len(part))
            payload.push_bytes(part)

        payload.push_uint_var(len(self.track_name))
        payload.push_bytes(self.track_name)
        payload.push_uint8(self.priority)
        payload.push_uint8(self.group_order)
        payload.push_uint8(self.forward)
        payload.push_uint_var(self.filter_type)

        # Optional start/end based on filter type
        if self.filter_type in (3, 4):  # ABSOLUTE_START or ABSOLUTE_RANGE
            payload.push_uint_var(self.start_group or 0)
            payload.push_uint_var(self.start_object or 0)

        if self.filter_type == 4:  # ABSOLUTE_RANGE
            payload.push_uint_var(self.end_group or 0)

        # Parameters
        MOQTMessage._serialize_params(payload, self.parameters or {})

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'Subscribe':

        request_id = buf.pull_uint_var()

        tuple_len = buf.pull_uint_var()
        namespace = tuple(buf.pull_bytes(buf.pull_uint_var()) for _ in range(tuple_len))

        track_name_len = buf.pull_uint_var()
        track_name = buf.pull_bytes(track_name_len)
        priority = buf.pull_uint8()
        group_order = buf.pull_uint8()
        forward = buf.pull_uint8()
        filter_type = buf.pull_uint_var()

        start_group = None
        start_object = None
        end_group = None
        if filter_type in (3, 4):
            start_group = buf.pull_uint_var()
            start_object = buf.pull_uint_var()
        if filter_type == 4:
            end_group = buf.pull_uint_var()

        params = MOQTMessage._deserialize_params(buf)

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


@dataclass
class SubscribeOk(MOQTMessage):
    request_id: int = 0
    track_alias: int = 0
    expires: int = None
    group_order: GroupOrder = None
    content_exists: ContentExistsCode = None
    largest_group_id: Optional[int] = None
    largest_object_id: Optional[int] = None
    parameters: Optional[Dict[int, Any]] = None

    def __post_init__(self):
        self.type = MOQTMessageType.SUBSCRIBE_OK

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)
        payload.push_uint_var(self.track_alias)
        payload.push_uint_var(self.expires)
        payload.push_uint8(self.group_order.value)
        payload.push_uint8(self.content_exists)

        if self.content_exists == ContentExistsCode.EXISTS:
            payload.push_uint_var(self.largest_group_id)
            payload.push_uint_var(self.largest_object_id)

        MOQTMessage._serialize_params(payload, self.parameters or {})

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'SubscribeOk':
        request_id = buf.pull_uint_var()
        track_alias = buf.pull_uint_var()
        expires = buf.pull_uint_var()
        group_order = GroupOrder(buf.pull_uint8())
        content_exists = buf.pull_uint8()

        largest_group_id = None
        largest_object_id = None
        if content_exists == ContentExistsCode.EXISTS:
            largest_group_id = buf.pull_uint_var()
            largest_object_id = buf.pull_uint_var()

        params = MOQTMessage._deserialize_params(buf)

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


@dataclass
class SubscribeError(MOQTMessage):
    request_id: int = None
    error_code: int = None
    reason: str = None

    def __post_init__(self):
        self.type = MOQTMessageType.SUBSCRIBE_ERROR

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)
        payload.push_uint_var(self.error_code.value if isinstance(self.error_code, SubscribeErrorCode) else self.error_code)

        reason_bytes = self.reason.encode()
        payload.push_uint_var(len(reason_bytes))
        payload.push_bytes(reason_bytes)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'SubscribeError':

        request_id = buf.pull_uint_var()
        error_code = buf.pull_uint_var()
        reason_len = buf.pull_uint_var()
        reason = buf.pull_bytes(reason_len).decode()

        return cls(
            request_id=request_id,
            error_code=error_code,
            reason=reason,
        )


@dataclass
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

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)
        payload.push_uint_var(self.subscription_request_id)
        payload.push_uint_var(self.start_group)
        payload.push_uint_var(self.start_object)
        payload.push_uint_var(self.end_group)
        payload.push_uint8(self.priority)
        payload.push_uint8(self.forward)

        MOQTMessage._serialize_params(payload, self.parameters or {})

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'SubscribeUpdate':

        request_id = buf.pull_uint_var()
        subscription_request_id = buf.pull_uint_var()
        start_group = buf.pull_uint_var()
        start_object = buf.pull_uint_var()
        end_group = buf.pull_uint_var()
        priority = buf.pull_uint8()
        forward = buf.pull_uint8()
        params = MOQTMessage._deserialize_params(buf)

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


@dataclass
class Unsubscribe(MOQTMessage):
    request_id: int = None

    def __post_init__(self):
        self.type = MOQTMessageType.UNSUBSCRIBE

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'Unsubscribe':

        request_id = buf.pull_uint_var()
        return cls(request_id=request_id)


@dataclass
class SubscribeDone(MOQTMessage):
    request_id: int = None
    status_code: int = None
    stream_count: int = None
    reason: str = None

    def __post_init__(self):
        self.type = MOQTMessageType.PUBLISH_DONE

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)
        
        payload.push_uint_var(self.request_id)
        payload.push_uint_var(self.status_code.value if isinstance(self.status_code, SubscribeDoneCode) else self.status_code)
        payload.push_uint_var(self.stream_count)
        
        reason_bytes = self.reason.encode()
        payload.push_uint_var(len(reason_bytes))
        payload.push_bytes(reason_bytes)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'SubscribeDone':

        request_id = buf.pull_uint_var()
        status_code = buf.pull_uint_var()
        stream_count = buf.pull_uint_var()
        reason_len = buf.pull_uint_var()
        reason = buf.pull_bytes(reason_len).decode()

        return cls(
            request_id=request_id,
            status_code=status_code,
            stream_count=stream_count,
            reason=reason
        )


@dataclass
class MaxSubscribeId(MOQTMessage):
    request_id: int = None

    def __post_init__(self):
        self.type = MOQTMessageType.MAX_REQUEST_ID

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'MaxSubscribeId':

        request_id = buf.pull_uint_var()
        return cls(request_id=request_id)


@dataclass
class SubscribesBlocked(MOQTMessage):
    maximum_request_id: int = None

    def __post_init__(self):
        self.type = MOQTMessageType.REQUESTS_BLOCKED

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.maximum_request_id)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'SubscribesBlocked':

        maximum_request_id = buf.pull_uint_var()
        return cls(maximum_request_id=maximum_request_id)