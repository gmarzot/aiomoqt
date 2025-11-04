from typing import Any, Union, Dict
from dataclasses import dataclass, fields

from . import ParamType, SetupParamType
from ..context import get_moqt_ctx_version, get_major_version
from ..utils.buffer import Buffer, BufferReadError
from ..utils.logger import *

logger = get_logger(__name__)

BUF_SIZE = 64


class MOQTUnderflow(Exception):
    def __init__(self, pos: int, needed: int):
        self.pos = pos
        self.needed = needed


@dataclass
class MOQTMessage:
    """Base class for all MOQT messages."""
    # type: Optional[int] = None - let subclass set it - annoying warnings

    @staticmethod
    def _extensions_encode(buf: Buffer, exts: Dict) -> None:
        vers = get_moqt_ctx_version()
        major_version = get_major_version(vers)
        # logger.debug(f"MOQTMessage._extensions_encode(): {vers} maj: {major_version}")
        if exts is None or len(exts) == 0:
            buf.push_uint_var(0)
            return
        
        if major_version > 8:
            pos = buf.tell()
            payload = Buffer(capacity=BUF_SIZE)
            for ext_id, ext_value in exts.items():
                payload.push_uint_var(ext_id)
                if ext_id % 2 == 0:  # even extension types are simple var int
                    payload.push_uint_var(ext_value)
                else:
                    if isinstance(ext_value, str):
                        ext_value = ext_value.encode()
                    assert isinstance(ext_value, bytes)
                    payload.push_uint_var(len(ext_value))
                    payload.push_bytes(ext_value)

            exts_len = payload.tell()
            buf.push_uint_var(exts_len)
            buf.push_bytes(payload.data)
        else:
            buf.push_uint_var(len(exts))
            for ext_id, ext_value in exts.items():
                buf.push_uint_var(ext_id)
                if ext_id % 2 == 0:
                    buf.push_uint_var(ext_value)
                else:
                    if isinstance(ext_value, str):
                        ext_value = ext_value.encode()
                    assert isinstance(ext_value, bytes)
                    buf.push_uint_var(len(ext_value))
                    buf.push_bytes(ext_value)

    exts_err_count = 0
    @staticmethod
    def _extensions_decode(buf: Buffer) -> Dict[int, Union[int, bytes]]:
        exts = {}
        exts_len = buf.pull_uint_var()
        if exts_len > (1024*16):
            global exts_err_count
            exts_err_count += 1
            logger.warning(f"MOQTMessage._extensions_decode(): corrupted buffer : ext_len: {exts_len} count: {exts_err_count}")
            return exts

        if exts_len > 0:
            pos = buf.tell()
            exts_end = pos + exts_len
            while buf.tell() < exts_end:
                ext_id = buf.pull_uint_var()
                if ext_id % 2 == 0:  # even extension types are simple var int
                    ext_value = buf.pull_uint_var()
                else:
                    value_len = buf.pull_uint_var()
                    ext_value = buf.pull_bytes(value_len)
                exts[ext_id] = ext_value
        # assert buf.tell() == exts_end, f"Payload length mismatch: {exts_len} {buf.tell()-pos}"

        return exts
          
    @staticmethod
    def _bytes_encode(value: Any) -> bytes:
        if isinstance(value, int):
            return MOQTMessage._varint_encode(value)
        if isinstance(value, str):
            return value.encode()
        return value

    @staticmethod
    def _varint_encode(value: int) -> bytes:
        buf = Buffer(capacity=8)
        buf.push_uint_var(value)
        return buf.data
    
    @staticmethod
    def _varint_decode(data: bytes) -> int:
        buf = Buffer(data=data)
        return buf.pull_uint_var()

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'MOQTMessage':
        """Create message from buf containing payload."""
        raise NotImplementedError()

    def serialize(self) -> bytes:
        """Convert message to complete wire format."""
        raise NotImplementedError()

    def __str__(self) -> str:
        """Generic string representation showing all fields."""
        parts = []
        class_fields = fields(self.__class__)
        
        # Fields that are typically UTF-8 text even though defined as bytes
        TEXT_LIKE_FIELDS = {
            'track_namespace', 'track_name', 'namespace', 'name',
            'new_session_uri', 'uri', 'path', 'authority', 
            'reason_phrase', 'reason'
        }

        for field in class_fields:
            value = getattr(self, field.name)
            
            # Skip 'type' field as it's redundant with class name
            if field.name == 'type':
                continue
                
            if value is None:
                parts.append(f"{field.name}=None")
                continue
                
            str_val = ''
            
            if "version" in field.name.lower():
                if isinstance(value, (list, tuple)):
                    str_val = "[" + ", ".join(f"0x{x:x}" for x in value) + "]"
                else:
                    str_val = f"0x{value:x}"
                    
            elif field.name == "parameters":
                # Decode parameter types and values
                if not value:
                    str_val = "{}"
                else:
                    items = []
                    enum = SetupParamType if class_name(self).endswith('Setup') else ParamType
                    for k, v in value.items():
                        try:
                            param_name = enum(k).name
                        except ValueError:
                            param_name = f"0x{k:02x}"
                        
                        # Display based on Key-Value-Pair structure
                        if k % 2 == 0:  # Even type - varint
                            items.append(f"{param_name}={v}")
                        else:  # Odd type - bytes (often text like PATH, AUTHORITY)
                            if isinstance(v, bytes):
                                items.append(f"{param_name}={MOQTMessage._format_bytes(v, prefer_text=True)}")
                            else:
                                items.append(f"{param_name}={v}")
                    
                    str_val = "{" + ", ".join(items) + "}"
                    
            elif isinstance(value, bytes):
                # Check if this field is typically text
                prefer_text = field.name in TEXT_LIKE_FIELDS
                str_val = MOQTMessage._format_bytes(value, prefer_text=prefer_text)
                
            elif isinstance(value, (list, tuple)) and value and isinstance(value[0], bytes):
                # Handle tuples of bytes (like namespace tuples)
                decoded_items = []
                for item in value:
                    decoded_items.append(MOQTMessage._format_bytes(item, prefer_text=True))
                str_val = "[" + ", ".join(decoded_items) + "]"
                
            elif isinstance(value, dict):
                if not value:
                    str_val = "{}"
                else:
                    str_val = "{" + ", ".join(f"{k}: {v}" for k, v in value.items()) + "}"
                    
            else:
                str_val = str(value)
                
            parts.append(f"{field.name}={str_val}")

        return f"{class_name(self)}({', '.join(parts)})"

    @staticmethod
    def _format_bytes(data: bytes, prefer_text: bool = False, max_len: int = 32) -> str:
        """
        Format bytes for display, trying UTF-8 decode when appropriate.
        
        Args:
            data: Bytes to format
            prefer_text: If True, strongly prefer text representation
            max_len: Maximum display length before truncation
        """
        if not data:
            return '""' if prefer_text else "0x"
        
        try:
            decoded = data.decode('utf-8')
            # Check if it's reasonable text (all printable or common whitespace)
            if all(c.isprintable() or c in '\n\r\t' for c in decoded):
                # It's valid UTF-8 and printable
                truncated = decoded[:max_len]
                suffix = "..." if len(decoded) > max_len else ""
                return f'"{truncated}{suffix}"'
            elif prefer_text:
                # Force text display even if not all printable (user knows it's text)
                truncated = decoded[:max_len]
                suffix = "..." if len(decoded) > max_len else ""
                return f'"{truncated}{suffix}"'
            else:
                # Valid UTF-8 but not printable, show as hex
                hex_output = data.hex()
                truncated = hex_output[:max_len]
                suffix = "..." if len(hex_output) > max_len else ""
                return f"0x{truncated}{suffix}"
        except UnicodeDecodeError:
            # Not UTF-8, show as hex
            hex_output = data.hex()
            truncated = hex_output[:max_len]
            suffix = "..." if len(hex_output) > max_len else ""
            return f"0x{truncated}{suffix}"
    
    @staticmethod
    def _serialize_params(payload: Buffer, parameters: Dict[int, Any]) -> None:
        """
        Serialize parameters using Key-Value-Pair structure. Payload is modified in place.
        
        Key-Value-Pair structure:
        - Even type: Type (varint) + Value (varint)
        - Odd type: Type (varint) + Length (varint) + Value (bytes)
        """
        payload.push_uint_var(len(parameters))
        for param_type, param_value in parameters.items():
            payload.push_uint_var(param_type)  # Type
            
            if param_type % 2 == 1:  # Odd type - includes Length field
                # Value is bytes
                if isinstance(param_value, int):
                    param_value = MOQTMessage._varint_encode(param_value)
                if isinstance(param_value, str):
                    param_value = param_value.encode()
                payload.push_uint_var(len(param_value))  # Length
                payload.push_bytes(param_value)  # Value
            else:  # Even type - Value is varint (no Length field)
                # Convert bytes to int if needed
                if isinstance(param_value, bytes):
                    param_value = Buffer(data=param_value).pull_uint_var()
                payload.push_uint_var(param_value)  # Value as varint


    def _deserialize_params(buf: Buffer) -> Dict[int, Any]:
        """
        Deserialize parameters using Key-Value-Pair structure.

        Returns:
            Dict mapping parameter type to value
        """
        params = {}
        param_count = buf.pull_uint_var()
        
        for _ in range(param_count):
            param_type = buf.pull_uint_var()
            
            if param_type % 2 == 1:  # Odd type - includes Length field
                param_len = buf.pull_uint_var()
                if param_len > 65535:  # 2^16-1 maximum
                    raise BufferReadError("Parameter length exceeds maximum of 65535 bytes")
                param_value = buf.pull_bytes(param_len)
            else:  # Even type - Value is varint
                param_value = buf.pull_uint_var()
            
            params[param_type] = param_value
        
        return params
