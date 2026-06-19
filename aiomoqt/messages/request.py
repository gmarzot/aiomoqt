"""Draft-16 unified request messages.

REQUEST_OK (0x07) — universal positive response for REQUEST_UPDATE,
    TRACK_STATUS, SUBSCRIBE_NAMESPACE, PUBLISH_NAMESPACE.
REQUEST_ERROR (0x05) — universal error response for all request types.
    Adds Retry Interval field not present in draft-14 error messages.
NAMESPACE (0x08) — sent on SUBSCRIBE_NAMESPACE response stream.
NAMESPACE_DONE (0x0E) — indicates namespace no longer published.
"""
from typing import Dict, Tuple, Optional, Any
from dataclasses import dataclass

from .base import MOQTMessage, BUF_SIZE
from ..types import D16MessageType
from ..context import profile_for
from ..utils.buffer import Buffer, BufferReadError
from ..utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class RequestOk(MOQTMessage):
    """REQUEST_OK (0x07) — draft-16 universal OK response.

    Wire format: Request ID (i), Num Parameters (i), Parameters (..) ...
    """
    request_id: int = 0
    parameters: Optional[Dict[int, Any]] = None

    def __post_init__(self):
        self.type = D16MessageType.REQUEST_OK

    def serialize(self, *, draft: int) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=profile_for(draft).vi64)

        # d18 replies omit the Request ID (demuxed by request stream, §10.1).
        if profile_for(draft).reply_has_request_id:
            payload.push_vint(self.request_id)
        MOQTMessage._serialize_params(payload, self.parameters or {}, draft=draft)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int, buf_end: Optional[int] = None) -> 'RequestOk':
        request_id = (buf.pull_vint()
                      if profile_for(draft).reply_has_request_id else None)
        params = MOQTMessage._deserialize_params(buf, draft=draft, buf_end=buf_end)
        return cls(request_id=request_id, parameters=params)


@dataclass(slots=True)
class RequestError(MOQTMessage):
    """REQUEST_ERROR (0x05) — draft-16 universal error response.

    Wire format: Request ID (i), Error Code (i), Retry Interval (i),
                 Error Reason Length (i), Error Reason (..)

    Retry Interval: minimum ms before retrying + 1.
        0 = don't retry. 1 = retry immediately.
    """
    request_id: int = None
    error_code: int = None
    retry_interval: int = 0  # 0 = don't retry
    reason: str = None

    def __post_init__(self):
        self.type = D16MessageType.REQUEST_ERROR

    def serialize(self, *, draft: int) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=profile_for(draft).vi64)

        # d18 replies omit the Request ID (demuxed by request stream, §10.1).
        if profile_for(draft).reply_has_request_id:
            payload.push_vint(self.request_id)
        payload.push_vint(self.error_code)
        payload.push_vint(self.retry_interval)

        reason_bytes = (self.reason or "").encode()
        payload.push_vint(len(reason_bytes))
        payload.push_bytes(reason_bytes)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int, buf_end: Optional[int] = None) -> 'RequestError':
        request_id = (buf.pull_vint()
                      if profile_for(draft).reply_has_request_id else None)
        error_code = buf.pull_vint()
        retry_interval = buf.pull_vint()
        reason_len = buf.pull_vint()
        reason = buf.pull_bytes(reason_len).decode()

        return cls(
            request_id=request_id,
            error_code=error_code,
            retry_interval=retry_interval,
            reason=reason,
        )


@dataclass(slots=True)
class RequestUpdate(MOQTMessage):
    """REQUEST_UPDATE (0x02) — draft-16 universal update.

    Replaces SUBSCRIBE_UPDATE. Now applies to all request types and
    gets an acknowledgment (REQUEST_OK/REQUEST_ERROR).

    Wire format: Request ID (i), Existing Request ID (i),
                 Num Parameters (i), Parameters (..) ...
    """
    request_id: int = None
    existing_request_id: int = None
    parameters: Optional[Dict[int, Any]] = None

    def __post_init__(self):
        self.type = D16MessageType.REQUEST_UPDATE

    def serialize(self, *, draft: int) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=profile_for(draft).vi64)

        # REQUEST_UPDATE carries both a (new) Request ID and the Existing
        # Request ID it updates, in d16 and d18 alike — confirmed against
        # the mvfst/moxygen d18 relay, which sends both. d18 uses vi64.
        payload.push_vint(self.request_id)
        payload.push_vint(self.existing_request_id)
        MOQTMessage._serialize_params(payload, self.parameters or {}, draft=draft)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int, buf_end: Optional[int] = None) -> 'RequestUpdate':
        request_id = buf.pull_vint()
        existing_request_id = buf.pull_vint()
        params = MOQTMessage._deserialize_params(buf, draft=draft, buf_end=buf_end)

        return cls(
            request_id=request_id,
            existing_request_id=existing_request_id,
            parameters=params,
        )


@dataclass(slots=True)
class Namespace(MOQTMessage):
    """NAMESPACE (0x08) — draft-16 namespace report.

    Sent on the response half of a SUBSCRIBE_NAMESPACE bidirectional stream
    to report track namespace suffixes matching the prefix.

    Wire format: Track Namespace Suffix (..)
    """
    namespace_suffix: Tuple[bytes, ...] = None

    def __post_init__(self):
        self.type = D16MessageType.NAMESPACE

    def serialize(self, *, draft: int) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=profile_for(draft).vi64)

        payload.push_vint(len(self.namespace_suffix))
        for part in self.namespace_suffix:
            payload.push_vint(len(part))
            payload.push_bytes(part)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int, buf_end: Optional[int] = None) -> 'Namespace':
        tuple_len = buf.pull_vint()
        namespace_suffix = tuple(
            buf.pull_bytes(buf.pull_vint()) for _ in range(tuple_len)
        )
        return cls(namespace_suffix=namespace_suffix)


@dataclass(slots=True)
class NamespaceDone(MOQTMessage):
    """NAMESPACE_DONE (0x0E) — draft-16 namespace withdrawal.

    Sent on SUBSCRIBE_NAMESPACE response stream to indicate
    a namespace is no longer published.

    Wire format: Track Namespace Suffix (..)
    """
    namespace_suffix: Tuple[bytes, ...] = None

    def __post_init__(self):
        self.type = D16MessageType.NAMESPACE_DONE

    def serialize(self, *, draft: int) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=profile_for(draft).vi64)

        payload.push_vint(len(self.namespace_suffix))
        for part in self.namespace_suffix:
            payload.push_vint(len(part))
            payload.push_bytes(part)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int, buf_end: Optional[int] = None) -> 'NamespaceDone':
        tuple_len = buf.pull_vint()
        namespace_suffix = tuple(
            buf.pull_bytes(buf.pull_vint()) for _ in range(tuple_len)
        )
        return cls(namespace_suffix=namespace_suffix)
