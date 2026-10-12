from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..base import BUF_SIZE, MOQTMessage
from ...context import DraftProfile
from ...types import D20MessageType
from ...utils.buffer import Buffer


@dataclass(slots=True)
class PublishStateNotify(MOQTMessage):
    """PUBLISH_STATE_NOTIFY (0x22, d20 §10.10): the subscription
    parameters a publisher changed, sent on the subscription's request
    stream. No Request ID on the wire; the session fills in the
    stream's."""
    request_id: Optional[int] = None
    parameters: Optional[Dict[int, Any]] = None

    def __post_init__(self):
        self.type = D20MessageType.PUBLISH_STATE_NOTIFY

    def serialize(self, *, prof: DraftProfile) -> Buffer:
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)
        MOQTMessage._serialize_params(payload, self.parameters or {},
                                      prof=prof)
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
        return cls(parameters=params)
