from dataclasses import dataclass, field
from typing import Optional, Dict, Tuple

from .base import MOQTMessage, BUF_SIZE
from ..types import *
from ..context import is_draft16_or_later
from ..utils.buffer import Buffer, BufferReadError
from ..utils.logger import get_logger

logger = get_logger(__name__)

@dataclass
class Fetch(MOQTMessage):
    """FETCH message to request a range of objects."""
    fetch_type: int
    request_id: int
    subscriber_priority: int = 128
    group_order: int = GroupOrder.DESCENDING
    namespace: Optional[Tuple[bytes, ...]] = None
    track_name: Optional[bytes] = None
    start_group: Optional[int] = None
    start_object: Optional[int] = None
    end_group: Optional[int] = None
    end_object: Optional[int] = None
    joining_sub_id: Optional[int] = None
    pre_group_offset: Optional[int] = None
    parameters: Dict[int, bytes] = field(default_factory=dict)

    def __post_init__(self):
        self.type = MOQTMessageType.FETCH

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)

        if is_draft16_or_later():
            # d16: priority and group_order moved to params
            payload.push_uint_var(self.fetch_type)
        else:
            # d14: priority and group_order as fixed fields before fetch_type
            payload.push_uint8(self.subscriber_priority)
            payload.push_uint8(self.group_order)
            payload.push_uint_var(self.fetch_type)

        if self.fetch_type == FetchType.FETCH:
            payload.push_uint_var(len(self.namespace))
            for part in self.namespace:
                payload.push_uint_var(len(part))
                payload.push_bytes(part)

            payload.push_uint_var(len(self.track_name))
            payload.push_bytes(self.track_name)

            payload.push_uint_var(self.start_group)
            payload.push_uint_var(self.start_object)
            payload.push_uint_var(self.end_group)
            payload.push_uint_var(self.end_object)
        elif self.fetch_type == FetchType.JOINING_FETCH:
            payload.push_uint_var(self.joining_sub_id)
            payload.push_uint_var(self.pre_group_offset)
        else:
            raise RuntimeError

        if is_draft16_or_later():
            params = dict(self.parameters)
            if self.subscriber_priority is not None:
                params[ParamType.SUBSCRIBER_PRIORITY] = self.subscriber_priority
            if self.group_order is not None:
                params[ParamType.GROUP_ORDER] = self.group_order
            MOQTMessage._serialize_params(payload, params)
        else:
            MOQTMessage._serialize_params(payload, self.parameters)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'Fetch':

        namespace = None
        track_name = None
        start_group = None
        start_object = None
        end_group = None
        end_object = None
        joining_sub_id = None
        pre_group_offset = None
        subscriber_priority = 128
        group_order = GroupOrder.DESCENDING

        request_id = buf.pull_uint_var()

        if is_draft16_or_later():
            fetch_type = buf.pull_uint_var()
        else:
            subscriber_priority = buf.pull_uint8()
            group_order = buf.pull_uint8()
            fetch_type = buf.pull_uint_var()

        if fetch_type == FetchType.FETCH:
            namespace = []
            ns_len = buf.pull_uint_var()
            for _ in range(ns_len):
                part_len = buf.pull_uint_var()
                namespace.append(buf.pull_bytes(part_len))
            namespace = tuple(namespace)
            track_name_len = buf.pull_uint_var()
            track_name = buf.pull_bytes(track_name_len)
            start_group = buf.pull_uint_var()
            start_object = buf.pull_uint_var()
            end_group = buf.pull_uint_var()
            end_object = buf.pull_uint_var()
        elif fetch_type == FetchType.JOINING_FETCH:
            joining_sub_id = buf.pull_uint_var()
            pre_group_offset = buf.pull_uint_var()
        else:
            raise RuntimeError

        params = MOQTMessage._deserialize_params(buf)

        if is_draft16_or_later():
            subscriber_priority = params.pop(ParamType.SUBSCRIBER_PRIORITY, 128)
            group_order = params.pop(ParamType.GROUP_ORDER, GroupOrder.DESCENDING)

        return cls(
            fetch_type=fetch_type,
            request_id=request_id,
            namespace=namespace,
            subscriber_priority=subscriber_priority,
            group_order=group_order,
            track_name=track_name,
            start_group=start_group,
            start_object=start_object,
            end_group=end_group,
            end_object=end_object,
            joining_sub_id=joining_sub_id,
            pre_group_offset=pre_group_offset,
            parameters=params
        )

@dataclass
class FetchOk(MOQTMessage):
    """FETCH_OK response message.

    Draft-14: Request ID, Group Order (8), End of Track (8),
              Largest Group ID, Largest Object ID, Params
    Draft-16: Request ID, End of Track (8), End Location, Params, Track Extensions
              (group_order removed; now a parameter 0x22)
    """
    request_id: int
    group_order: int = GroupOrder.DESCENDING
    end_of_track: int = 0
    largest_group_id: int = 0
    largest_object_id: int = 0
    parameters: Dict[int, bytes] = None
    track_extensions: Optional[Dict[int, Any]] = None  # d16 only

    def __post_init__(self):
        self.type = MOQTMessageType.FETCH_OK
        if self.parameters is None:
            self.parameters = {}

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)

        if is_draft16_or_later():
            # d16: no group_order fixed field
            payload.push_uint8(self.end_of_track)
            payload.push_uint_var(self.largest_group_id)
            payload.push_uint_var(self.largest_object_id)
            params = dict(self.parameters)
            if self.group_order is not None:
                params[ParamType.GROUP_ORDER] = self.group_order
            MOQTMessage._serialize_params(payload, params)
            MOQTMessage._extensions_encode(payload, self.track_extensions or {}, with_length=False)
        else:
            # d14: group_order as fixed field
            payload.push_uint8(self.group_order)
            payload.push_uint8(self.end_of_track)
            payload.push_uint_var(self.largest_group_id)
            payload.push_uint_var(self.largest_object_id)
            MOQTMessage._serialize_params(payload, self.parameters)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'FetchOk':
        request_id = buf.pull_uint_var()
        track_extensions = None

        if is_draft16_or_later():
            end_of_track = buf.pull_uint8()
            largest_group_id = buf.pull_uint_var()
            largest_object_id = buf.pull_uint_var()
            params = MOQTMessage._deserialize_params(buf)
            group_order = params.pop(ParamType.GROUP_ORDER, GroupOrder.DESCENDING)
            track_extensions = MOQTMessage._extensions_decode(buf, with_length=False)
        else:
            group_order = buf.pull_uint8()
            end_of_track = buf.pull_uint8()
            largest_group_id = buf.pull_uint_var()
            largest_object_id = buf.pull_uint_var()
            params = MOQTMessage._deserialize_params(buf)

        return cls(
            request_id=request_id,
            group_order=group_order,
            end_of_track=end_of_track,
            largest_group_id=largest_group_id,
            largest_object_id=largest_object_id,
            parameters=params,
            track_extensions=track_extensions,
        )

@dataclass
class FetchError(MOQTMessage):
    """FETCH_ERROR response message."""
    request_id: int
    error_code: int
    reason: str

    def __post_init__(self):
        self.type = MOQTMessageType.FETCH_ERROR

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)
        payload.push_uint_var(self.error_code)
        
        reason_bytes = self.reason.encode()
        payload.push_uint_var(len(reason_bytes))
        payload.push_bytes(reason_bytes)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'FetchError':

        request_id = buf.pull_uint_var()
        error_code = buf.pull_uint_var()
        reason_len = buf.pull_uint_var()
        reason = buf.pull_bytes(reason_len).decode()

        return cls(
            request_id=request_id,
            error_code=error_code,
            reason=reason
        )
    
@dataclass
class FetchCancel(MOQTMessage):
    """FETCH_CANCEL message to cancel an ongoing fetch."""
    request_id: int

    def __post_init__(self):
        self.type = MOQTMessageType.FETCH_CANCEL

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'FetchCancel':

        request_id = buf.pull_uint_var()

        return cls(request_id=request_id)
