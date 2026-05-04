from dataclasses import dataclass
from typing import Dict, Tuple, Any

from . import MOQTMessageType, MOQTMessage, BUF_SIZE
from ..context import is_draft16_or_later
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

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)
        
        # Serialize namespace tuple
        payload.push_uint_var(len(self.namespace))
        for part in self.namespace:
            payload.push_uint_var(len(part))
            payload.push_bytes(part)

        # Serialize parameters
        MOQTMessage._serialize_params(payload, self.parameters)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'PublishNamespace':
        request_id = buf.pull_uint_var()
        
        tuple_len = buf.pull_uint_var()
        namespace = tuple(buf.pull_bytes(buf.pull_uint_var()) for _ in range(tuple_len))
        
        params = MOQTMessage._deserialize_params(buf)
        return cls(request_id=request_id, namespace=namespace, parameters=params)


@dataclass(slots=True)
class PublishNamespaceOk(MOQTMessage):
    """PUBLISH_NAMESPACE_OK response message."""
    request_id: int = 0  # Spec says Request ID, not namespace

    def __post_init__(self):
        self.type = MOQTMessageType.PUBLISH_NAMESPACE_OK

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'PublishNamespaceOk':
        request_id = buf.pull_uint_var()
        return cls(request_id=request_id)


@dataclass(slots=True)
class PublishNamespaceError(MOQTMessage):
    """PUBLISH_NAMESPACE_ERROR response message."""
    request_id: int = 0  # Spec says Request ID, not namespace
    error_code: int = None
    reason: str = None

    def __post_init__(self):
        self.type = MOQTMessageType.PUBLISH_NAMESPACE_ERROR

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
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'PublishNamespaceError':
        request_id = buf.pull_uint_var()
        error_code = buf.pull_uint_var()
        reason_len = buf.pull_uint_var()
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

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        if is_draft16_or_later():
            payload.push_uint_var(self.request_id)
        else:
            payload.push_uint_var(len(self.namespace))
            for part in self.namespace:
                payload.push_uint_var(len(part))
                payload.push_bytes(part)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'PublishNamespaceDone':
        if is_draft16_or_later():
            request_id = buf.pull_uint_var()
            return cls(request_id=request_id)
        else:
            tuple_len = buf.pull_uint_var()
            namespace = tuple(buf.pull_bytes(buf.pull_uint_var()) for _ in range(tuple_len))
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

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        if is_draft16_or_later():
            payload.push_uint_var(self.request_id)
        else:
            payload.push_uint_var(len(self.namespace))
            for part in self.namespace:
                payload.push_uint_var(len(part))
                payload.push_bytes(part)

        payload.push_uint_var(self.error_code)

        reason_bytes = (self.reason or "").encode()
        payload.push_uint_var(len(reason_bytes))
        payload.push_bytes(reason_bytes)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'PublishNamespaceCancel':
        namespace = None
        request_id = None
        if is_draft16_or_later():
            request_id = buf.pull_uint_var()
        else:
            tuple_len = buf.pull_uint_var()
            namespace = tuple(buf.pull_bytes(buf.pull_uint_var()) for _ in range(tuple_len))
        error_code = buf.pull_uint_var()
        reason_len = buf.pull_uint_var()
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

    def serialize(self) -> bytes:
        from ..context import is_draft16_or_later
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)

        payload.push_uint_var(len(self.namespace_prefix))
        for part in self.namespace_prefix:
            payload.push_uint_var(len(part))
            payload.push_bytes(part)

        if is_draft16_or_later():
            payload.push_uint_var(self.subscribe_options)

        MOQTMessage._serialize_params(payload, self.parameters)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'SubscribeNamespace':
        from ..context import is_draft16_or_later
        request_id = buf.pull_uint_var()
        tuple_len = buf.pull_uint_var()
        namespace_prefix = tuple(buf.pull_bytes(buf.pull_uint_var()) for _ in range(tuple_len))
        subscribe_options = 0
        if is_draft16_or_later():
            subscribe_options = buf.pull_uint_var()
        params = MOQTMessage._deserialize_params(buf)
        return cls(request_id=request_id, namespace_prefix=namespace_prefix,
                   subscribe_options=subscribe_options, parameters=params)


@dataclass(slots=True)
class SubscribeNamespaceOk(MOQTMessage):
    """SUBSCRIBE_NAMESPACE_OK response message."""
    request_id: int = 0  # Spec says Request ID, not namespace_prefix

    def __post_init__(self):
        self.type = MOQTMessageType.SUBSCRIBE_NAMESPACE_OK

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(self.request_id)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'SubscribeNamespaceOk':
        request_id = buf.pull_uint_var()
        return cls(request_id=request_id)


@dataclass(slots=True)
class SubscribeNamespaceError(MOQTMessage):
    """SUBSCRIBE_NAMESPACE_ERROR response message."""
    request_id: int = 0  # Spec says Request ID, not namespace_prefix
    error_code: int = None
    reason: str = None

    def __post_init__(self):
        self.type = MOQTMessageType.SUBSCRIBE_NAMESPACE_ERROR

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
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'SubscribeNamespaceError':
        request_id = buf.pull_uint_var()
        error_code = buf.pull_uint_var()
        reason_len = buf.pull_uint_var()
        reason = buf.pull_bytes(reason_len).decode()
        return cls(request_id=request_id, error_code=error_code, reason=reason)


@dataclass 
class UnsubscribeNamespace(MOQTMessage):
    """UNSUBSCRIBE_NAMESPACE message."""
    namespace_prefix: Tuple[bytes, ...] = None

    def __post_init__(self):
        self.type = MOQTMessageType.UNSUBSCRIBE_NAMESPACE

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        payload.push_uint_var(len(self.namespace_prefix))
        for part in self.namespace_prefix:
            payload.push_uint_var(len(part))
            payload.push_bytes(part)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data_slice(0, payload.tell()))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'UnsubscribeNamespace':
        tuple_len = buf.pull_uint_var()
        namespace_prefix = tuple(buf.pull_bytes(buf.pull_uint_var()) for _ in range(tuple_len))
        return cls(namespace_prefix=namespace_prefix)