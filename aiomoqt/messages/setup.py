from dataclasses import dataclass
from typing import Dict, List, Any

from . import MOQTMessageType, MOQTMessage, SetupParamType, BUF_SIZE
from ..utils.buffer import Buffer, BufferReadError
from ..utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ServerSetup(MOQTMessage):
    """SERVER_SETUP message for accepting MOQT session."""
    selected_version: int = None
    parameters: Dict[int, Any] = None

    def __post_init__(self):
        self.type = MOQTMessageType.SERVER_SETUP

    def serialize(self) -> Buffer:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        # Add selected version
        payload.push_uint_var(self.selected_version)

        # Add parameters
        MOQTMessage._serialize_params(payload, self.parameters)

        # Build final message
        buf.push_uint_var(self.type)  # SERVER_SETUP type
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'ServerSetup':
        """Handle SERVER_SETUP message."""
        version = buf.pull_uint_var()
        params = MOQTMessage._deserialize_params(buf)
        return cls(selected_version=version, parameters=params)


@dataclass
class ClientSetup(MOQTMessage):
    """CLIENT_SETUP message for initializing MOQT session."""
    versions: List[int] = None
    parameters: Dict[int, Any] = None

    def __post_init__(self):
        self.type = MOQTMessageType.CLIENT_SETUP

    def serialize(self) -> Buffer:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)

        # Add versions
        payload.push_uint_var(len(self.versions))
        for version in self.versions:
            payload.push_uint_var(version)

        # Add parameters
        MOQTMessage._serialize_params(payload, self.parameters)

        # Build final message
        buf.push_uint_var(self.type)  # CLIENT_SETUP type
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)
        logger.debug(f"ClientSetup.serialize: payload size: {payload.tell()} payload data len: {len(payload.data)} buf size: {buf.tell()}")
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'ClientSetup':
        """Handle CLIENT_SETUP message."""
        versions = []
        version_count = buf.pull_uint_var()
        for _ in range(version_count):
            versions.append(buf.pull_uint_var())
        params = MOQTMessage._deserialize_params(buf)
        return cls(versions=versions, parameters=params)
        

@dataclass
class GoAway(MOQTMessage):
    new_session_uri: str = None

    def __post_init__(self):
        self.type = MOQTMessageType.GOAWAY

    def serialize(self) -> Buffer:
        buf = Buffer(capacity=BUF_SIZE)
        payload = Buffer(capacity=BUF_SIZE)
        
        uri_bytes = self.new_session_uri.encode()
        
        # Enforce maximum URI length of 8,192 bytes
        if len(uri_bytes) > 8192:
            raise ValueError("New Session URI exceeds maximum length of 8,192 bytes")
        
        payload.push_uint_var(len(uri_bytes))  # uri length
        payload.push_bytes(uri_bytes)
        
        # Write message
        buf.push_uint_var(self.type)
        buf.push_uint16(payload.tell())
        buf.push_bytes(payload.data)

        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'GoAway':
        """Handle GOAWAY message."""        
        uri_len = buf.pull_uint_var()
        
        # Enforce maximum URI length of 8,192 bytes
        if uri_len > 8192:
            raise BufferReadError("New Session URI exceeds maximum length of 8,192 bytes")
        
        uri = buf.pull_bytes(uri_len).decode()

        return cls(new_session_uri=uri)