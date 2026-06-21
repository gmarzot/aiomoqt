from dataclasses import dataclass
from typing import Dict, List, Any, Optional

from . import MOQTMessageType, MOQTMessage, SetupParamType, BUF_SIZE
from ..context import DraftProfile
from ..utils.buffer import Buffer, BufferReadError
from ..utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class ServerSetup(MOQTMessage):
    """SERVER_SETUP message for accepting MOQT session.

    Draft-14: Selected Version (i), Num Parameters (i), Parameters (..)
    Draft-16: Num Parameters (i), Parameters (..)
              (version negotiated via ALPN, not in-band)
    """
    selected_version: int = None
    parameters: Dict[int, Any] = None

    def __post_init__(self):
        self.type = MOQTMessageType.SERVER_SETUP

    def serialize(self, *, prof: DraftProfile) -> Buffer:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        # Draft-14: version on wire; Draft-16: version via ALPN
        if prof.setup_carries_versions:
            payload.push_uint_var(self.selected_version)

        MOQTMessage._serialize_params(payload, self.parameters, prof=prof)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile,
                    buf_end: Optional[int] = None) -> 'ServerSetup':
        """Handle SERVER_SETUP message."""
        version = None
        if prof.setup_carries_versions:
            version = buf.pull_uint_var()
        params = MOQTMessage._deserialize_params(buf, prof=prof,
                                                 buf_end=buf_end)
        return cls(selected_version=version, parameters=params)


@dataclass(slots=True)
class ClientSetup(MOQTMessage):
    """CLIENT_SETUP message for initializing MOQT session.

    Draft-14: Num Versions (i), Versions (i)..., Num Params (i), Params (..)
    Draft-16: Num Params (i), Params (..)
              (version negotiated via ALPN, not in-band)
    """
    versions: List[int] = None
    parameters: Dict[int, Any] = None

    def __post_init__(self):
        self.type = MOQTMessageType.CLIENT_SETUP

    def serialize(self, *, prof: DraftProfile) -> Buffer:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        # Draft-14: versions on wire; Draft-16: version via ALPN
        if prof.setup_carries_versions:
            payload.push_uint_var(len(self.versions))
            for version in self.versions:
                payload.push_uint_var(version)

        MOQTMessage._serialize_params(payload, self.parameters, prof=prof)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile,
                    buf_end: Optional[int] = None) -> 'ClientSetup':
        """Handle CLIENT_SETUP message."""
        versions = []
        if prof.setup_carries_versions:
            version_count = buf.pull_uint_var()
            for _ in range(version_count):
                versions.append(buf.pull_uint_var())
        params = MOQTMessage._deserialize_params(buf, prof=prof,
                                                 buf_end=buf_end)
        return cls(versions=versions, parameters=params)
        

@dataclass(slots=True)
class GoAway(MOQTMessage):
    new_session_uri: str = None

    # New Session URI maximum length: 8 KiB.
    MAX_URI_LENGTH = 8192

    def __post_init__(self):
        self.type = MOQTMessageType.GOAWAY

    def serialize(self, *, prof: DraftProfile) -> Buffer:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        uri_bytes = self.new_session_uri.encode()
        if len(uri_bytes) > self.MAX_URI_LENGTH:
            raise ValueError(
                "New Session URI exceeds maximum length (8 KiB)")

        payload.push_uint_var(len(uri_bytes))  # uri length
        payload.push_bytes(uri_bytes)

        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)

        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile,
                    buf_end: Optional[int] = None) -> 'GoAway':
        """Handle GOAWAY message."""
        uri_len = buf.pull_uint_var()
        if uri_len > cls.MAX_URI_LENGTH:
            raise BufferReadError(
                "New Session URI exceeds maximum length (8 KiB)")

        uri = buf.pull_bytes(uri_len).decode()

        return cls(new_session_uri=uri)