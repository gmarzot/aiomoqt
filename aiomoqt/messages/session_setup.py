from dataclasses import dataclass
from typing import Dict, Any, Optional

from . import MOQTMessageType, MOQTMessage, BUF_SIZE
from ..context import DraftProfile
from ..types import MOQTProtocolViolation
from ..utils.buffer import Buffer, BufferReadError
from ..utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class ServerSetup(MOQTMessage):
    """SERVER_SETUP message for accepting MOQT session.

    Num Parameters (i), Parameters (..); the version is negotiated via
    ALPN or WT-Protocol.
    """
    parameters: Dict[int, Any] = None

    def __post_init__(self):
        self.type = MOQTMessageType.SERVER_SETUP

    def serialize(self, *, prof: DraftProfile) -> Buffer:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        MOQTMessage._serialize_params(payload, self.parameters, prof=prof)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile,
                    buf_end: Optional[int] = None) -> 'ServerSetup':
        """Handle SERVER_SETUP message."""
        params = MOQTMessage._deserialize_params(buf, prof=prof,
                                                 buf_end=buf_end)
        return cls(parameters=params)


@dataclass(slots=True)
class ClientSetup(MOQTMessage):
    """CLIENT_SETUP message for initializing MOQT session.

    Num Params (i), Params (..); the version is negotiated via ALPN or
    WT-Protocol.
    """
    parameters: Dict[int, Any] = None

    def __post_init__(self):
        self.type = MOQTMessageType.CLIENT_SETUP

    def serialize(self, *, prof: DraftProfile) -> Buffer:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        MOQTMessage._serialize_params(payload, self.parameters, prof=prof)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile,
                    buf_end: Optional[int] = None) -> 'ClientSetup':
        """Handle CLIENT_SETUP message."""
        params = MOQTMessage._deserialize_params(buf, prof=prof,
                                                 buf_end=buf_end)
        return cls(parameters=params)


@dataclass(slots=True)
class GoAway(MOQTMessage):
    new_session_uri: str = None
    # d18 §10.4 (Figure 7): Timeout always; Request ID only in the
    # control-stream form (smallest unprocessed peer request id).
    timeout: int = 0
    request_id: Optional[int] = None

    # New Session URI maximum length: 8 KiB.
    MAX_URI_LENGTH = 8192

    def __post_init__(self):
        self.type = MOQTMessageType.GOAWAY

    def serialize(self, *, prof: DraftProfile) -> Buffer:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)

        uri_bytes = self.new_session_uri.encode()
        if len(uri_bytes) > self.MAX_URI_LENGTH:
            raise ValueError(
                "New Session URI exceeds maximum length (8 KiB)")

        payload.push_vint(len(uri_bytes))  # uri length
        payload.push_bytes(uri_bytes)
        if prof.draft >= 18:
            payload.push_vint(self.timeout or 0)
            if prof.goaway_request_id and self.request_id is not None:
                payload.push_vint(self.request_id)

        buf.vi64 = prof.vi64
        buf.push_vint(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)

        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile,
                    buf_end: Optional[int] = None) -> 'GoAway':
        """Handle GOAWAY message."""
        buf.vi64 = prof.vi64
        uri_len = buf.pull_vint()
        if uri_len > cls.MAX_URI_LENGTH:
            raise BufferReadError(
                "New Session URI exceeds maximum length (8 KiB)")

        try:
            uri = buf.pull_bytes(uri_len).decode()
        except UnicodeDecodeError:
            raise MOQTProtocolViolation("New Session URI is not UTF-8") from None
        timeout = 0
        request_id = None
        if prof.draft >= 18:
            timeout = buf.pull_vint()
            # Request ID: only in the d18 control-stream form; the message
            # Length covers it, so remaining bytes decide presence.
            if (prof.goaway_request_id and buf_end is not None
                    and buf.tell() < buf_end):
                request_id = buf.pull_vint()

        return cls(new_session_uri=uri, timeout=timeout,
                   request_id=request_id)
