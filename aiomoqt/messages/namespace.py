from dataclasses import dataclass
from typing import Dict, Tuple, Any, Optional

from . import MOQTMessageType, D18MessageType, MOQTMessage, BUF_SIZE
from ..context import is_draft16_or_later, profile_for
from ..utils.buffer import Buffer, BufferReadError
from ..utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class PublishNamespace(MOQTMessage):
    """PUBLISH_NAMESPACE message for advertising a track namespace."""
    request_id: int = 0  # Request ID field from spec
    namespace: Tuple[bytes, ...] = None
    parameters: Dict[int, Any] = None

    def __post_init__(self):
        self.type = MOQTMessageType.PUBLISH_NAMESPACE

    def serialize(self, *, draft: int) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=profile_for(draft).vi64)

        payload.push_vint(self.request_id)
        
        # Serialize namespace tuple
        payload.push_vint(len(self.namespace))
        for part in self.namespace:
            payload.push_vint(len(part))
            payload.push_bytes(part)

        # Serialize parameters
        MOQTMessage._serialize_params(payload, self.parameters, draft=draft)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int, buf_end: Optional[int] = None) -> 'PublishNamespace':
        request_id = buf.pull_vint()
        
        tuple_len = buf.pull_vint()
        namespace = tuple(buf.pull_bytes(buf.pull_vint()) for _ in range(tuple_len))
        
        params = MOQTMessage._deserialize_params(buf, draft=draft, buf_end=buf_end)
        return cls(request_id=request_id, namespace=namespace, parameters=params)


@dataclass(slots=True)
class PublishNamespaceOk(MOQTMessage):
    """PUBLISH_NAMESPACE_OK response message."""
    request_id: int = 0  # Spec says Request ID, not namespace

    def __post_init__(self):
        self.type = MOQTMessageType.PUBLISH_NAMESPACE_OK

    def serialize(self, *, draft: int) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=profile_for(draft).vi64)

        payload.push_vint(self.request_id)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int, buf_end: Optional[int] = None) -> 'PublishNamespaceOk':
        request_id = buf.pull_vint()
        return cls(request_id=request_id)


@dataclass(slots=True)
class PublishNamespaceError(MOQTMessage):
    """PUBLISH_NAMESPACE_ERROR response message."""
    request_id: int = 0  # Spec says Request ID, not namespace
    error_code: int = None
    reason: str = None

    def __post_init__(self):
        self.type = MOQTMessageType.PUBLISH_NAMESPACE_ERROR

    def serialize(self, *, draft: int) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=profile_for(draft).vi64)

        payload.push_vint(self.request_id)
        payload.push_vint(self.error_code)
        
        reason_bytes = self.reason.encode()
        payload.push_vint(len(reason_bytes))
        payload.push_bytes(reason_bytes)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int, buf_end: Optional[int] = None) -> 'PublishNamespaceError':
        request_id = buf.pull_vint()
        error_code = buf.pull_vint()
        reason_len = buf.pull_vint()
        reason = buf.pull_bytes(reason_len).decode()
        return cls(request_id=request_id, error_code=error_code, reason=reason)


@dataclass(slots=True)
class PublishNamespaceDone(MOQTMessage):
    """PUBLISH_NAMESPACE_DONE message to withdraw track namespace.

    Draft-14: Track Namespace (tuple)
    Draft-16: Request ID (i)
    """
    namespace: Tuple[bytes, ...] = None
    request_id: int = None  # d16 only

    def __post_init__(self):
        self.type = MOQTMessageType.PUBLISH_NAMESPACE_DONE

    def serialize(self, *, draft: int) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=profile_for(draft).vi64)

        if is_draft16_or_later(draft):
            payload.push_vint(self.request_id)
        else:
            payload.push_vint(len(self.namespace))
            for part in self.namespace:
                payload.push_vint(len(part))
                payload.push_bytes(part)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int, buf_end: Optional[int] = None) -> 'PublishNamespaceDone':
        if is_draft16_or_later(draft):
            request_id = buf.pull_vint()
            return cls(request_id=request_id)
        else:
            tuple_len = buf.pull_vint()
            namespace = tuple(buf.pull_bytes(buf.pull_vint()) for _ in range(tuple_len))
            return cls(namespace=namespace)


@dataclass(slots=True)
class PublishNamespaceCancel(MOQTMessage):
    """PUBLISH_NAMESPACE_CANCEL message to withdraw announcement acceptance.

    Draft-14: Track Namespace (tuple), Error Code (i), Error Reason
    Draft-16: Request ID (i), Error Code (i), Error Reason
    """
    namespace: Tuple[bytes, ...] = None
    error_code: int = None
    reason: str = None
    request_id: int = None  # d16 only

    def __post_init__(self):
        self.type = MOQTMessageType.PUBLISH_NAMESPACE_CANCEL

    def serialize(self, *, draft: int) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=profile_for(draft).vi64)

        if is_draft16_or_later(draft):
            payload.push_vint(self.request_id)
        else:
            payload.push_vint(len(self.namespace))
            for part in self.namespace:
                payload.push_vint(len(part))
                payload.push_bytes(part)

        payload.push_vint(self.error_code)

        reason_bytes = (self.reason or "").encode()
        payload.push_vint(len(reason_bytes))
        payload.push_bytes(reason_bytes)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int, buf_end: Optional[int] = None) -> 'PublishNamespaceCancel':
        namespace = None
        request_id = None
        if is_draft16_or_later(draft):
            request_id = buf.pull_vint()
        else:
            tuple_len = buf.pull_vint()
            namespace = tuple(buf.pull_bytes(buf.pull_vint()) for _ in range(tuple_len))
        error_code = buf.pull_vint()
        reason_len = buf.pull_vint()
        reason = buf.pull_bytes(reason_len).decode()
        return cls(namespace=namespace, error_code=error_code, reason=reason, request_id=request_id)


@dataclass(slots=True)
class SubscribeNamespace(MOQTMessage):
    """SUBSCRIBE_NAMESPACE message to subscribe to announcements."""
    request_id: int = 0  # Request ID field from spec
    namespace_prefix: Tuple[bytes, ...] = None
    subscribe_options: int = 0  # d16: Subscribe Options field
    parameters: Dict[int, Any] = None

    def __post_init__(self):
        self.type = MOQTMessageType.SUBSCRIBE_NAMESPACE

    def serialize(self, *, draft: int) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=profile_for(draft).vi64)

        payload.push_vint(self.request_id)

        payload.push_vint(len(self.namespace_prefix))
        for part in self.namespace_prefix:
            payload.push_vint(len(part))
            payload.push_bytes(part)

        # subscribe_options: d16 only — d18 (SUBSCRIBE_NAMESPACE, 10.18)
        # removed it.
        if is_draft16_or_later(draft) and not profile_for(draft).vi64:
            payload.push_vint(self.subscribe_options)

        MOQTMessage._serialize_params(payload, self.parameters, draft=draft)

        # d18 renumbers this 0x11 -> 0x50 and writes the type as vi64.
        buf.vi64 = profile_for(draft).vi64
        buf.push_vint(
            D18MessageType.SUBSCRIBE_NAMESPACE if buf.vi64 else self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int, buf_end: Optional[int] = None) -> 'SubscribeNamespace':
        request_id = buf.pull_vint()
        tuple_len = buf.pull_vint()
        namespace_prefix = tuple(buf.pull_bytes(buf.pull_vint()) for _ in range(tuple_len))
        subscribe_options = 0
        if is_draft16_or_later(draft) and not profile_for(draft).vi64:
            subscribe_options = buf.pull_vint()
        params = MOQTMessage._deserialize_params(buf, draft=draft, buf_end=buf_end)
        return cls(request_id=request_id, namespace_prefix=namespace_prefix,
                   subscribe_options=subscribe_options, parameters=params)


@dataclass(slots=True)
class SubscribeTracks(MOQTMessage):
    """SUBSCRIBE_TRACKS (0x51, draft-18) — request PUBLISH messages for all
    tracks within matching namespaces. Body is identical to
    SUBSCRIBE_NAMESPACE: Request ID, Track Namespace Prefix, Parameters."""
    request_id: int = 0
    namespace_prefix: Tuple[bytes, ...] = None
    parameters: Dict[int, Any] = None

    def __post_init__(self):
        self.type = D18MessageType.SUBSCRIBE_TRACKS

    def serialize(self, *, draft: int) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=profile_for(draft).vi64)

        payload.push_vint(self.request_id)
        payload.push_vint(len(self.namespace_prefix))
        for part in self.namespace_prefix:
            payload.push_vint(len(part))
            payload.push_bytes(part)
        MOQTMessage._serialize_params(
            payload, self.parameters or {}, draft=draft)

        buf.vi64 = profile_for(draft).vi64
        buf.push_vint(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int, buf_end: Optional[int] = None) -> 'SubscribeTracks':
        request_id = buf.pull_vint()
        tuple_len = buf.pull_vint()
        namespace_prefix = tuple(
            buf.pull_bytes(buf.pull_vint()) for _ in range(tuple_len))
        params = MOQTMessage._deserialize_params(buf, draft=draft, buf_end=buf_end)
        return cls(request_id=request_id, namespace_prefix=namespace_prefix,
                   parameters=params)


@dataclass(slots=True)
class PublishBlocked(MOQTMessage):
    """PUBLISH_BLOCKED (0x0F, draft-18) — the publisher cannot send a PUBLISH
    to start a new subscription for a track in a SUBSCRIBE_TRACKS namespace.
    Body: Track Namespace Suffix, Track Name (10.20)."""
    namespace_suffix: Tuple[bytes, ...] = None
    track_name: bytes = b""

    def __post_init__(self):
        self.type = D18MessageType.PUBLISH_BLOCKED

    def serialize(self, *, draft: int) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=profile_for(draft).vi64)

        payload.push_vint(len(self.namespace_suffix))
        for part in self.namespace_suffix:
            payload.push_vint(len(part))
            payload.push_bytes(part)
        tn = (self.track_name.encode()
              if isinstance(self.track_name, str) else self.track_name)
        payload.push_vint(len(tn))
        payload.push_bytes(tn)

        buf.vi64 = profile_for(draft).vi64
        buf.push_vint(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int, buf_end: Optional[int] = None) -> 'PublishBlocked':
        tuple_len = buf.pull_vint()
        namespace_suffix = tuple(
            buf.pull_bytes(buf.pull_vint()) for _ in range(tuple_len))
        track_name = buf.pull_bytes(buf.pull_vint())
        return cls(namespace_suffix=namespace_suffix, track_name=track_name)


@dataclass(slots=True)
class SubscribeNamespaceOk(MOQTMessage):
    """SUBSCRIBE_NAMESPACE_OK response message."""
    request_id: int = 0  # Spec says Request ID, not namespace_prefix

    def __post_init__(self):
        self.type = MOQTMessageType.SUBSCRIBE_NAMESPACE_OK

    def serialize(self, *, draft: int) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=profile_for(draft).vi64)

        payload.push_vint(self.request_id)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int, buf_end: Optional[int] = None) -> 'SubscribeNamespaceOk':
        request_id = buf.pull_vint()
        return cls(request_id=request_id)


@dataclass(slots=True)
class SubscribeNamespaceError(MOQTMessage):
    """SUBSCRIBE_NAMESPACE_ERROR response message."""
    request_id: int = 0  # Spec says Request ID, not namespace_prefix
    error_code: int = None
    reason: str = None

    def __post_init__(self):
        self.type = MOQTMessageType.SUBSCRIBE_NAMESPACE_ERROR

    def serialize(self, *, draft: int) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=profile_for(draft).vi64)

        payload.push_vint(self.request_id)
        payload.push_vint(self.error_code)
        
        reason_bytes = self.reason.encode()
        payload.push_vint(len(reason_bytes))
        payload.push_bytes(reason_bytes)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int, buf_end: Optional[int] = None) -> 'SubscribeNamespaceError':
        request_id = buf.pull_vint()
        error_code = buf.pull_vint()
        reason_len = buf.pull_vint()
        reason = buf.pull_bytes(reason_len).decode()
        return cls(request_id=request_id, error_code=error_code, reason=reason)


@dataclass 
class UnsubscribeNamespace(MOQTMessage):
    """UNSUBSCRIBE_NAMESPACE message."""
    namespace_prefix: Tuple[bytes, ...] = None

    def __post_init__(self):
        self.type = MOQTMessageType.UNSUBSCRIBE_NAMESPACE

    def serialize(self, *, draft: int) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=profile_for(draft).vi64)

        payload.push_vint(len(self.namespace_prefix))
        for part in self.namespace_prefix:
            payload.push_vint(len(part))
            payload.push_bytes(part)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, draft: int, buf_end: Optional[int] = None) -> 'UnsubscribeNamespace':
        tuple_len = buf.pull_vint()
        namespace_prefix = tuple(buf.pull_bytes(buf.pull_vint()) for _ in range(tuple_len))
        return cls(namespace_prefix=namespace_prefix)