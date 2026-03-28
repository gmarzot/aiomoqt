from ..types import *
from typing import Tuple, Dict, Optional, Any
from dataclasses import dataclass

from . import MOQTMessage, BUF_SIZE
from ..context import is_draft16_or_later
from ..utils.buffer import Buffer, BufferReadError
from ..utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Publish(MOQTMessage):
    """PUBLISH (0x1D) — Publisher announces a track to subscriber.

    Wire format (Section 9.13):
        Request ID (i), Track Namespace (tuple), Track Name Len (i) + Track Name (..),
        Track Alias (i), Group Order (8), Content Exists (8),
        [Largest Location (Location)], Forward (8),
        Num Parameters (i), Parameters (..) ...

    Largest Location present if content_exists == 1.
    """
    request_id: int = 0
    track_namespace: Tuple[bytes, ...] = None
    track_name: bytes = None
    track_alias: int = 0
    group_order: int = None
    content_exists: int = 0
    largest_group_id: Optional[int] = None
    largest_object_id: Optional[int] = None
    forward: int = None
    parameters: Optional[Dict[int, Any]] = None
    track_extensions: Optional[Dict[int, Any]] = None  # d16 only

    def __post_init__(self):
        self.type = MOQTMessageType.PUBLISH

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
        payload.push_uint_var(self.track_alias)

        if is_draft16_or_later():
            # d16: group_order, content_exists, forward all in params/extensions
            params = dict(self.parameters or {})
            if self.forward is not None:
                params[ParamType.FORWARD] = self.forward
            if self.largest_group_id is not None:
                lbuf = Buffer(capacity=16)
                lbuf.push_uint_var(self.largest_group_id)
                lbuf.push_uint_var(self.largest_object_id or 0)
                params[ParamType.LARGEST_OBJECT] = lbuf.data_slice(0, lbuf.tell())
            MOQTMessage._serialize_params(payload, params)
            # Track Extensions
            exts = dict(self.track_extensions or {})
            if self.group_order is not None:
                exts[0x22] = self.group_order
            MOQTMessage._extensions_encode(payload, exts)
        else:
            # d14: fixed fields
            payload.push_uint8(self.group_order)
            payload.push_uint8(self.content_exists)
            if self.content_exists == ContentExistsCode.EXISTS:
                payload.push_uint_var(self.largest_group_id)
                payload.push_uint_var(self.largest_object_id)
            payload.push_uint8(self.forward)
            MOQTMessage._serialize_params(payload, self.parameters or {})

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'Publish':
        request_id = buf.pull_uint_var()

        tuple_len = buf.pull_uint_var()
        namespace = tuple(buf.pull_bytes(buf.pull_uint_var()) for _ in range(tuple_len))

        track_name_len = buf.pull_uint_var()
        track_name = buf.pull_bytes(track_name_len)
        track_alias = buf.pull_uint_var()

        group_order = None
        content_exists = None
        largest_group_id = None
        largest_object_id = None
        forward = None
        track_extensions = None

        if is_draft16_or_later():
            params = MOQTMessage._deserialize_params(buf)
            forward = params.pop(ParamType.FORWARD, None)
            largest_raw = params.pop(ParamType.LARGEST_OBJECT, None)
            if largest_raw is not None:
                lbuf = Buffer(data=largest_raw)
                largest_group_id = lbuf.pull_uint_var()
                largest_object_id = lbuf.pull_uint_var()
                content_exists = ContentExistsCode.EXISTS
            else:
                content_exists = ContentExistsCode.NO_CONTENT
            track_extensions = MOQTMessage._extensions_decode(buf)
            go_val = track_extensions.pop(0x22, None)
            if go_val is not None:
                group_order = go_val
        else:
            group_order = buf.pull_uint8()
            content_exists = buf.pull_uint8()
            if content_exists == ContentExistsCode.EXISTS:
                largest_group_id = buf.pull_uint_var()
                largest_object_id = buf.pull_uint_var()
            forward = buf.pull_uint8()
            params = MOQTMessage._deserialize_params(buf)

        return cls(
            request_id=request_id,
            track_namespace=namespace,
            track_name=track_name,
            track_alias=track_alias,
            group_order=group_order,
            content_exists=content_exists,
            largest_group_id=largest_group_id,
            largest_object_id=largest_object_id,
            forward=forward,
            parameters=params,
            track_extensions=track_extensions,
        )


@dataclass
class PublishOk(MOQTMessage):
    """PUBLISH_OK (0x1E) — Subscriber accepts a PUBLISH.

    Draft-14: Request ID, Forward (8), Priority (8), Group Order (8),
              Filter Type (i), [Location], [End Group], Params
    Draft-16: Request ID, Params
              (all fixed fields moved to parameters)
    """
    request_id: int = 0
    forward: int = None
    priority: int = None
    group_order: int = None
    filter_type: int = None
    start_group: Optional[int] = None
    start_object: Optional[int] = None
    end_group: Optional[int] = None
    parameters: Optional[Dict[int, Any]] = None

    def __post_init__(self):
        self.type = MOQTMessageType.PUBLISH_OK

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)

        if is_draft16_or_later():
            # d16: everything in params
            params = dict(self.parameters or {})
            if self.forward is not None:
                params[ParamType.FORWARD] = self.forward
            if self.priority is not None:
                params[ParamType.SUBSCRIBER_PRIORITY] = self.priority
            if self.group_order is not None:
                params[ParamType.GROUP_ORDER] = self.group_order
            if self.filter_type is not None:
                fbuf = Buffer(capacity=64)
                fbuf.push_uint_var(self.filter_type)
                if self.filter_type in (3, 4):
                    fbuf.push_uint_var(self.start_group or 0)
                    fbuf.push_uint_var(self.start_object or 0)
                if self.filter_type == 4:
                    fbuf.push_uint_var(self.end_group or 0)
                params[ParamType.SUBSCRIPTION_FILTER] = fbuf.data_slice(0, fbuf.tell())
            MOQTMessage._serialize_params(payload, params)
        else:
            # d14: fixed fields
            payload.push_uint8(self.forward)
            payload.push_uint8(self.priority)
            payload.push_uint8(self.group_order)
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
    def deserialize(cls, buf: Buffer) -> 'PublishOk':
        request_id = buf.pull_uint_var()

        forward = None
        priority = None
        group_order = None
        filter_type = None
        start_group = None
        start_object = None
        end_group = None

        if is_draft16_or_later():
            params = MOQTMessage._deserialize_params(buf)
            forward = params.pop(ParamType.FORWARD, None)
            priority = params.pop(ParamType.SUBSCRIBER_PRIORITY, None)
            group_order = params.pop(ParamType.GROUP_ORDER, None)
            filter_raw = params.pop(ParamType.SUBSCRIPTION_FILTER, None)
            if filter_raw is not None:
                fbuf = Buffer(data=filter_raw)
                filter_type = fbuf.pull_uint_var()
                if filter_type in (3, 4):
                    start_group = fbuf.pull_uint_var()
                    start_object = fbuf.pull_uint_var()
                if filter_type == 4:
                    end_group = fbuf.pull_uint_var()
        else:
            forward = buf.pull_uint8()
            priority = buf.pull_uint8()
            group_order = buf.pull_uint8()
            filter_type = buf.pull_uint_var()
            if filter_type in (3, 4):
                start_group = buf.pull_uint_var()
                start_object = buf.pull_uint_var()
            if filter_type == 4:
                end_group = buf.pull_uint_var()
            params = MOQTMessage._deserialize_params(buf)

        return cls(
            request_id=request_id,
            forward=forward,
            priority=priority,
            group_order=group_order,
            filter_type=filter_type,
            start_group=start_group,
            start_object=start_object,
            end_group=end_group,
            parameters=params
        )


@dataclass
class PublishError(MOQTMessage):
    """PUBLISH_ERROR (0x1F) — Subscriber rejects a PUBLISH.

    Wire format (Section 9.15) — same as SUBSCRIBE_ERROR:
        Request ID (i), Error Code (i), Error Reason (Reason Phrase)
    """
    request_id: int = None
    error_code: int = None
    reason: str = None

    def __post_init__(self):
        self.type = MOQTMessageType.PUBLISH_ERROR

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)
        payload.push_uint_var(self.error_code.value if isinstance(self.error_code, PublishErrorCode) else self.error_code)

        reason_bytes = self.reason.encode()
        payload.push_uint_var(len(reason_bytes))
        payload.push_bytes(reason_bytes)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'PublishError':
        request_id = buf.pull_uint_var()
        error_code = buf.pull_uint_var()
        reason_len = buf.pull_uint_var()
        reason = buf.pull_bytes(reason_len).decode()

        return cls(
            request_id=request_id,
            error_code=error_code,
            reason=reason,
        )
