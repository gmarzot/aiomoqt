from dataclasses import dataclass, field
from typing import Any, Optional, Dict, Tuple

from .base import MOQTMessage, BUF_SIZE
from ..types import (
    MOQTMessageType, FetchType, GroupOrder, ParamType,
    SessionCloseCode,
)
from ..context import is_draft16_or_later, DraftProfile
from ..utils.buffer import Buffer
from ..utils.logger import get_logger

logger = get_logger(__name__)


def _is_joining(fetch_type: int) -> bool:
    return fetch_type in (FetchType.RELATIVE_JOINING,
                          FetchType.ABSOLUTE_JOINING)


@dataclass(slots=True)
class Fetch(MOQTMessage):
    """FETCH message to request a range of objects.

    Three variants (MoQT §9.16):
      STANDALONE (0x1):       namespace + track_name + [start_loc, end_loc]
      RELATIVE_JOINING (0x2): joining_request_id + joining_start (delta)
      ABSOLUTE_JOINING (0x3): joining_request_id + joining_start (abs group)

    The wire format of Relative and Absolute Joining Fetch is identical;
    only the publisher's range computation differs.
    """
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
    # Joining fetch fields (spec: Joining Request ID, Joining Start)
    joining_request_id: Optional[int] = None
    joining_start: Optional[int] = None
    parameters: Dict[int, bytes] = field(default_factory=dict)

    def __post_init__(self):
        self.type = MOQTMessageType.FETCH

    def serialize(self, *, prof: DraftProfile) -> Buffer:
        buf = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)

        payload.push_vint(self.request_id)

        if is_draft16_or_later(prof.draft):
            # d16: priority and group_order live in params
            payload.push_vint(self.fetch_type)
        else:
            # d14: priority and group_order are mandatory fixed fields (no
            # optional form like d16's params) — substitute defaults when
            # the caller left them unset (None).
            payload.push_uint8(self.subscriber_priority
                               if self.subscriber_priority is not None else 128)
            payload.push_uint8(self.group_order
                               if self.group_order is not None
                               else GroupOrder.ASCENDING)
            payload.push_vint(self.fetch_type)

        if self.fetch_type == FetchType.STANDALONE:
            payload.push_vint(len(self.namespace))
            for part in self.namespace:
                payload.push_vint(len(part))
                payload.push_bytes(part)
            payload.push_vint(len(self.track_name))
            payload.push_bytes(self.track_name)
            payload.push_vint(self.start_group)
            payload.push_vint(self.start_object)
            payload.push_vint(self.end_group)
            payload.push_vint(self.end_object)
        elif _is_joining(self.fetch_type):
            payload.push_vint(self.joining_request_id)
            payload.push_vint(self.joining_start)
        else:
            raise ValueError(
                f"Invalid fetch_type: {self.fetch_type} "
                f"(must be 0x1, 0x2, or 0x3)")

        if is_draft16_or_later(prof.draft):
            params = dict(self.parameters)
            if self.subscriber_priority is not None:
                params[ParamType.SUBSCRIBER_PRIORITY] = self.subscriber_priority
            if self.group_order is not None:
                params[ParamType.GROUP_ORDER] = self.group_order
            MOQTMessage._serialize_params(payload, params, prof=prof)
        else:
            MOQTMessage._serialize_params(payload, self.parameters, prof=prof)

        buf.push_vint(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'Fetch':
        namespace = None
        track_name = None
        start_group = None
        start_object = None
        end_group = None
        end_object = None
        joining_request_id = None
        joining_start = None
        subscriber_priority = 128
        group_order = GroupOrder.DESCENDING

        request_id = buf.pull_vint()

        if is_draft16_or_later(prof.draft):
            fetch_type = buf.pull_vint()
        else:
            subscriber_priority = buf.pull_uint8()
            group_order = buf.pull_uint8()
            fetch_type = buf.pull_vint()

        if fetch_type == FetchType.STANDALONE:
            ns = []
            ns_len = buf.pull_vint()
            for _ in range(ns_len):
                part_len = buf.pull_vint()
                ns.append(buf.pull_bytes(part_len))
            namespace = tuple(ns)
            track_name_len = buf.pull_vint()
            track_name = buf.pull_bytes(track_name_len)
            start_group = buf.pull_vint()
            start_object = buf.pull_vint()
            end_group = buf.pull_vint()
            end_object = buf.pull_vint()
        elif _is_joining(fetch_type):
            joining_request_id = buf.pull_vint()
            joining_start = buf.pull_vint()
        else:
            raise ValueError(
                f"Invalid fetch_type: {fetch_type} "
                f"(spec §9.16: must be 0x1, 0x2, or 0x3)")

        params = MOQTMessage._deserialize_params(buf, prof=prof, buf_end=buf_end)

        if is_draft16_or_later(prof.draft):
            subscriber_priority = params.pop(
                ParamType.SUBSCRIBER_PRIORITY, 128)
            group_order = params.pop(
                ParamType.GROUP_ORDER, GroupOrder.DESCENDING)

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
            joining_request_id=joining_request_id,
            joining_start=joining_start,
            parameters=params
        )

@dataclass(slots=True)
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

    def serialize(self, *, prof: DraftProfile) -> bytes:
        buf = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)

        # FETCH_OK is a response (§10.1): d18 omits the Request ID (demuxed
        # by the request stream; the dispatcher injects it on parse).
        if prof.reply_has_request_id:
            payload.push_vint(self.request_id)

        if is_draft16_or_later(prof.draft):
            # d16: no group_order fixed field
            payload.push_uint8(self.end_of_track)
            payload.push_vint(self.largest_group_id)
            payload.push_vint(self.largest_object_id)
            params = dict(self.parameters)
            if self.group_order is not None:
                params[ParamType.GROUP_ORDER] = self.group_order
            MOQTMessage._serialize_params(payload, params, prof=prof)
            MOQTMessage._extensions_encode(payload, self.track_extensions or {}, with_length=False)
        else:
            # d14: group_order as fixed field
            payload.push_uint8(self.group_order)
            payload.push_uint8(self.end_of_track)
            payload.push_vint(self.largest_group_id)
            payload.push_vint(self.largest_object_id)
            MOQTMessage._serialize_params(payload, self.parameters, prof=prof)

        buf.push_vint(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'FetchOk':
        # buf_end is the absolute end-of-message position derived from
        # the outer frame length. Required for d16 (Track Extensions
        # have no length prefix; sequence runs to end of message).
        request_id = (buf.pull_vint()
                      if prof.reply_has_request_id else None)
        track_extensions = None

        if is_draft16_or_later(prof.draft):
            end_of_track = buf.pull_uint8()
            largest_group_id = buf.pull_vint()
            largest_object_id = buf.pull_vint()
            params = MOQTMessage._deserialize_params(buf, prof=prof, buf_end=buf_end)
            group_order = params.pop(ParamType.GROUP_ORDER, GroupOrder.DESCENDING)
            track_extensions = MOQTMessage._extensions_decode(
                buf, with_length=False, buf_end=buf_end)
        else:
            group_order = buf.pull_uint8()
            end_of_track = buf.pull_uint8()
            largest_group_id = buf.pull_vint()
            largest_object_id = buf.pull_vint()
            params = MOQTMessage._deserialize_params(buf, prof=prof, buf_end=buf_end)

        return cls(
            request_id=request_id,
            group_order=group_order,
            end_of_track=end_of_track,
            largest_group_id=largest_group_id,
            largest_object_id=largest_object_id,
            parameters=params,
            track_extensions=track_extensions,
        )

@dataclass(slots=True)
class FetchError(MOQTMessage):
    """FETCH_ERROR response message."""
    request_id: int
    error_code: int
    reason: str

    def __post_init__(self):
        self.type = MOQTMessageType.FETCH_ERROR

    def serialize(self, *, prof: DraftProfile) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_vint(self.request_id)
        payload.push_vint(self.error_code)
        
        reason_bytes = self.reason.encode()
        payload.push_vint(len(reason_bytes))
        payload.push_bytes(reason_bytes)

        buf.push_vint(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'FetchError':

        request_id = buf.pull_vint()
        error_code = buf.pull_vint()
        reason_len = buf.pull_vint()
        reason = buf.pull_bytes(reason_len).decode()

        return cls(
            request_id=request_id,
            error_code=error_code,
            reason=reason
        )
    
@dataclass(slots=True)
class FetchCancel(MOQTMessage):
    """FETCH_CANCEL message to cancel an ongoing fetch."""
    request_id: int

    def __post_init__(self):
        self.type = MOQTMessageType.FETCH_CANCEL

    def serialize(self, *, prof: DraftProfile) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_vint(self.request_id)

        buf.push_vint(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile, buf_end: Optional[int] = None) -> 'FetchCancel':

        request_id = buf.pull_vint()

        return cls(request_id=request_id)
