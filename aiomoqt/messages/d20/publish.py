from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..base import BUF_SIZE, MOQTMessage
from ..params import LocationFilter
from ...context import DraftProfile
from ...types import D20MessageType, ParamType
from ...utils.buffer import Buffer


@dataclass(slots=True)
class PublishStateNotify(MOQTMessage):
    """PUBLISH_STATE_NOTIFY (0x22, d20 §10.10): subscription state the
    publisher changed for a reason other than the subscriber's
    REQUEST_UPDATE, sent on the subscription's request stream and never
    answered. It carries only the changed parameters: LARGEST_OBJECT,
    FORWARD and LOCATION_FILTER.

    No Request ID on the wire; the session fills in the stream's.
    """
    forward: Optional[int] = None
    largest_group_id: Optional[int] = None
    largest_object_id: Optional[int] = None
    location_filter: Optional[LocationFilter] = None
    parameters: Optional[Dict[int, Any]] = None
    request_id: Optional[int] = None

    def __post_init__(self):
        self.type = D20MessageType.PUBLISH_STATE_NOTIFY

    def serialize(self, *, prof: DraftProfile) -> Buffer:
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)
        params = dict(self.parameters or {})
        if self.largest_group_id is not None:
            params[ParamType.LARGEST_OBJECT] = (
                self.largest_group_id, self.largest_object_id or 0)
        if self.forward is not None:
            params[ParamType.FORWARD] = int(self.forward)
        if self.location_filter is not None:
            params[ParamType.LOCATION_FILTER] = self.location_filter
        MOQTMessage._serialize_params(payload, params, prof=prof)
        buf = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)
        buf.push_vint(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile,
                    buf_end: Optional[int] = None) -> 'PublishStateNotify':
        params = MOQTMessage._deserialize_params(
            buf, prof=prof, buf_end=buf_end, scope=cls.__name__)
        largest = params.pop(ParamType.LARGEST_OBJECT, None)
        return cls(
            forward=params.pop(ParamType.FORWARD, None),
            largest_group_id=None if largest is None else largest[0],
            largest_object_id=None if largest is None else largest[1],
            location_filter=params.pop(ParamType.LOCATION_FILTER, None),
            parameters=params,
        )
