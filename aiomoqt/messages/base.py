import os
from typing import Any, Optional, Union, Dict
from dataclasses import dataclass, field, fields

from . import ParamType, SetupParamType, AuthTokenAliasType
from ..context import profile_for
from ..types import MOQTProtocolViolation
from ..utils.buffer import Buffer, BufferReadError
from ..utils.logger import *

logger = get_logger(__name__)

# Same env gate as the ObjectHeader.serialize strict-check, extended to
# cover the extensions block. Catches publisher-side declared-vs-actual
# mismatches in the temp-buffer extensions encode path.
_STRICT_SERIALIZE = bool(int(os.environ.get('AIOMOQT_STRICT_SERIALIZE', '0')))

BUF_SIZE = 4 * 1024  # 4KB buffer size for messages


# Aliased from aiopquic so transport-layer pull_*/parse_* raises are
# catchable as MOQTUnderflow in aiomoqt without introducing an upward
# dependency (aiopquic must not import aiomoqt).
from aiopquic.exceptions import StreamUnderflow as MOQTUnderflow  # noqa: E402


@dataclass(slots=True)
class MOQTMessage:
    """Base class for all MOQT messages.

    Declared `type` slot so subclasses can assign their MOQTMessageType
    in __post_init__ (and consumers can read it generically) under
    slots=True. Default None; concrete subclasses override.
    """
    type: Optional[int] = field(default=None, kw_only=True)

    @staticmethod
    def _extensions_encode(buf: Buffer, exts: Dict,
                           with_length: bool = True) -> None:
        # The extensions/properties wire form (length-prefixed KVP
        # block) is identical across every supported draft, so this
        # codec is version-agnostic: no draft argument and no per-object
        # version lookup on the hot path.
        if exts is None or len(exts) == 0:
            if with_length:
                buf.push_uint_var(0)
            return

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
        if _STRICT_SERIALIZE and len(payload.data) != exts_len:
            raise AssertionError(
                f"_extensions_encode payload mismatch: "
                f"declared exts_len={exts_len} "
                f"actual payload.data bytes={len(payload.data)}"
            )
        if with_length:
            buf.push_uint_var(exts_len)
        payload_bytes_before = buf.tell()
        buf.push_bytes(payload.data)
        actual_pushed = buf.tell() - payload_bytes_before
        if _STRICT_SERIALIZE and actual_pushed != exts_len:
            raise AssertionError(
                f"_extensions_encode push_bytes mismatch: "
                f"declared exts_len={exts_len} "
                f"actually pushed={actual_pushed}"
            )

    exts_err_count = 0
    EXTENSIONS_LEN_LIMIT = 1024 * 16

    # Wire-noncompliance tolerance. Strict by default: a truncated
    # trailing extensions block (peer's wire-level msg_len declares
    # more bytes than form a valid KVP) is a spec violation and is
    # raised through the normal control-message error path. Interop
    # clients can opt-in to tolerance by setting this flag; truncation
    # then becomes a counted, ERROR-logged event and the partially
    # parsed dict is returned so the rest of the message dispatches.
    _tolerate_trailing_extensions = False
    _trailing_extensions_truncation_count = 0

    @staticmethod
    def _extensions_decode(buf: Buffer, with_length: bool = True,
                           buf_end: Optional[int] = None) -> Optional[Dict[int, Union[int, bytes]]]:
        # Two framing modes:
        #   with_length=True  — Object Extensions (§10.2.1.2): the
        #     extensions block is `{ Length, Headers }`. Bound is
        #     read from the wire here.
        #   with_length=False — d16 Track Extensions on control
        #     messages (§9.13 etc.): no length on the wire; sequence
        #     runs to end of the containing message. Caller MUST
        #     supply buf_end derived from the outer frame length.
        #     No buf.capacity fallback: capacity is the allocated
        #     buffer size (e.g. 4 KB), not data length — reading past
        #     data reads uninitialized heap.
        if with_length:
            pos_before = buf.tell()
            exts_len = buf.pull_uint_var()
            if exts_len > MOQTMessage.EXTENSIONS_LEN_LIMIT:
                # Framer has lost alignment: this varint is being read
                # from a position that doesn't actually point at an
                # extensions block. Returning silently here ratchets
                # the desync — caller advances past garbage and reads
                # more garbage. Raise so the outer loop terminates the
                # stream cleanly via STOP_SENDING.
                MOQTMessage.exts_err_count += 1
                if MOQTMessage.exts_err_count == 1:
                    # First-corruption diagnostic — dump anchor bytes
                    # so the misalignment can be reverse-engineered.
                    try:
                        cap = buf.capacity
                        head = buf.data_slice(0, min(64, cap)).hex()
                    except Exception:
                        head = "<unavailable>"
                    logger.error(
                        "MOQT framer desync: ext_len=%d at pos=%d "
                        "(cap=%d) head_hex=%s",
                        exts_len, pos_before, cap, head,
                    )
                raise RuntimeError(
                    f"framer desync: ext_len={exts_len} exceeds "
                    f"limit {MOQTMessage.EXTENSIONS_LEN_LIMIT} "
                    f"at pos={pos_before}"
                )
            if exts_len == 0:
                return None
            exts_end = buf.tell() + exts_len
        else:
            if buf_end is None:
                raise ValueError(
                    "_extensions_decode(with_length=False) requires "
                    "buf_end from the caller (outer message frame "
                    "length). buf.capacity is allocated size, not "
                    "data length — using it reads uninitialized memory."
                )
            exts_end = buf_end
            if buf.tell() == exts_end:
                return None

        exts = {}
        while buf.tell() < exts_end:
            kvp_start = buf.tell()
            # BufferReadError (aliased as MOQTUnderflow) means the
            # underlying buffer ran out mid-pull. For data streams
            # (with_length=True) the caller's re-buffering path uses
            # this to wait for more data, so let it propagate.
            # For control messages (with_length=False) the buffer is
            # single-shot: a truncation here means the peer sent a
            # message whose wire-level length declares more extension
            # bytes than were actually transmitted (we have seen this
            # from relays during teardown). Returning the partial dict
            # lets the dispatcher process the message body (which we
            # already finished parsing) without a noisy traceback.
            try:
                ext_id = buf.pull_uint_var()
                if ext_id % 2 == 0:
                    # even type → varint value (self-bounded by varint prefix)
                    ext_value = buf.pull_uint_var()
                else:
                    # odd type → length-prefixed bytes value
                    value_len = buf.pull_uint_var()
                    ext_value = buf.pull_bytes(value_len)
            except BufferReadError:
                if with_length:
                    raise
                # Trailing extensions block on a control message is
                # truncated: the peer's wire-level msg_len declares
                # more bytes than form a valid KVP. This is a spec
                # violation (d16 §9.13). Count it regardless of
                # policy so the event is always recorded.
                MOQTMessage._trailing_extensions_truncation_count += 1
                if not MOQTMessage._tolerate_trailing_extensions:
                    raise RuntimeError(
                        f"non-compliant peer: truncated trailing "
                        f"extensions block (kvp_start={kvp_start} "
                        f"tell={buf.tell()} exts_end={exts_end} "
                        f"parsed_kvps={len(exts)})"
                    )
                logger.error(
                    "MOQT wire non-compliance tolerated: truncated "
                    "trailing extensions block "
                    "(kvp_start=%d tell=%d exts_end=%d) — "
                    "returned %d KVPs; enabled by "
                    "--compat lenient-extensions",
                    kvp_start, buf.tell(), exts_end, len(exts),
                )
                buf.seek(exts_end)
                return exts if exts else None
            if buf.tell() > exts_end:
                # KVP read succeeded against buf.capacity but crossed
                # our logical end-of-message — malformed framing.
                try:
                    head = buf.data_slice(
                        kvp_start, min(buf.tell(), kvp_start + 32)
                    ).hex()
                except Exception:
                    head = "<unavailable>"
                logger.warning(
                    "MOQT extensions decode overrun: pos=%d exts_end=%d "
                    "kvp_start=%d ext_id=0x%x head_hex=%s",
                    buf.tell(), exts_end, kvp_start, ext_id, head,
                )
                raise RuntimeError(
                    f"extensions decode overrun: pos={buf.tell()} "
                    f"exts_end={exts_end} kvp_start={kvp_start} "
                    f"ext_id=0x{ext_id:x}"
                )
            exts[ext_id] = ext_value

        return exts if exts else None
          
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
    def deserialize(cls, buf: Buffer,
                    buf_end: Optional[int] = None) -> 'MOQTMessage':
        """Create message from buf containing payload.

        buf_end is the absolute end-of-message position; required by
        deserialize implementations that parse trailing fields with no
        wire-level length prefix (d16 Track Extensions in PUBLISH /
        SUBSCRIBE_OK / FETCH_OK). Other message types accept and
        ignore it for signature uniformity.
        """
        raise NotImplementedError()

    def serialize(self) -> bytes:
        """Convert message to complete wire format."""
        raise NotImplementedError()

    def __str__(self) -> str:
        """Generic string representation showing all fields."""
        parts = []
        
        for field in fields(self.__class__):
            value = getattr(self, field.name)
            
            # Skip redundant type field
            if field.name == 'type':
                continue
                
            if value is None:
                parts.append(f"{field.name}=None")
                continue
            
            # Format based on value type
            if "version" in field.name.lower():
                # Version fields - show in hex
                if isinstance(value, (list, tuple)):
                    str_val = "[" + ", ".join(f"0x{x:x}" for x in value) + "]"
                else:
                    str_val = f"0x{value:x}"
                    
            elif field.name == "parameters":
                # Parameters dict - special formatting
                str_val = self._format_parameters(value, class_name(self))
                
            elif isinstance(value, (list, tuple)) and value and isinstance(value[0], bytes):
                # Namespace tuple - decode each part
                items = [self._format_bytes(item, prefer_text=True) for item in value]
                str_val = "[" + ", ".join(items) + "]"
                
            elif isinstance(value, bytes):
                # Bytes field - try UTF-8 decode
                str_val = self._format_bytes(value, prefer_text=True)
                
            else:
                # Everything else - use default string representation
                str_val = str(value)
                
            parts.append(f"{field.name}={str_val}")

        return f"{class_name(self)}({', '.join(parts)})"

    @staticmethod
    def _format_parameters(params: dict, message_class_name: str) -> str:
        """Format parameters dict for display."""
        if not params:
            return "{}"
        
        # Determine which enum to use based on message type
        is_setup = message_class_name.endswith('Setup')
        
        items = []
        for k, v in params.items():
            # Try to get enum name
            try:
                if is_setup:
                    param_name = SetupParamType(k).name
                else:
                    param_name = ParamType(k).name
            except ValueError:
                param_name = f"0x{k:02x}"
            
            # Format value - even types are ints, odd types are bytes
            if k % 2 == 0:
                items.append(f"{param_name}={v}")
            else:
                if isinstance(v, bytes):
                    items.append(f"{param_name}={MOQTMessage._format_bytes(v, prefer_text=True)}")
                else:
                    items.append(f"{param_name}=\"{v}\"")
        
        return "{" + ", ".join(items) + "}"


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
    def _serialize_params(payload: Buffer, parameters: Dict[int, Any],
                          *, draft: int, delta_keys: bool = None) -> None:
        """
        Serialize parameters using Key-Value-Pair structure. Payload is modified in place.

        Key-Value-Pair structure:
        - Even type: Type (varint) + Value (varint)
        - Odd type: Type (varint) + Length (varint) + Value (bytes)

        delta_keys defaults from the draft's profile (d16+ sorts keys
        and writes deltas: key_on_wire = key - previous_key).
        """
        if delta_keys is None:
            delta_keys = profile_for(draft).params_delta_coded
        if delta_keys:
            # Sort by key for delta encoding
            parameters = dict(sorted(parameters.items()))
        payload.push_uint_var(len(parameters))
        prev_key = 0
        for param_type, param_value in parameters.items():
            if delta_keys:
                payload.push_uint_var(param_type - prev_key)
                prev_key = param_type
            else:
                payload.push_uint_var(param_type)  # Type
            
            if param_type % 2 == 1:  # Odd type - includes Length field
                # Value is bytes or string
                if isinstance(param_value, str):
                    param_value = param_value.encode()
                if not isinstance(param_value, bytes):
                    raise TypeError(f"Param {param_type} expects bytes, got {type(param_value)}")

                # AUTH_TOKEN requires Token structure wrapping (Section 9.2.1.1)
                if param_type in (ParamType.AUTH_TOKEN, SetupParamType.AUTH_TOKEN):
                    token_buf = Buffer(capacity=BUF_SIZE)
                    token_buf.push_uint_var(AuthTokenAliasType.USE_VALUE)  # Alias Type
                    token_buf.push_uint_var(0)  # Token Type (0 = out-of-band)
                    token_buf.push_bytes(param_value)  # Token Value (rest of param)
                    param_value = token_buf.data_slice(0, token_buf.tell())
                    logger.info(f"Serializing AUTH_TOKEN param as Token(USE_VALUE): {len(param_value)} bytes")

                logger.info(f"Serializing param {param_type} length {len(param_value)}")
                payload.push_uint_var(len(param_value))  # Length
                payload.push_bytes(param_value)  # Value
            else:  # Even type - Value is varint (no Length field)
                if not isinstance(param_value, int):
                    raise TypeError(f"Param {param_type} expects uint, got {type(param_value)}")
                logger.info(f"Serializing param {param_type} value {param_value}")
                payload.push_uint_var(param_value)  # Value as varint
                
        logger.info(f"Serialized {len(parameters)} parameters: {payload.data_slice(0,12)}")


    @staticmethod
    def _deserialize_params(buf: Buffer, *, draft: int,
                            buf_end: Optional[int] = None,
                            delta_keys: bool = None) -> Dict[int, Any]:
        """
        Deserialize parameters using Key-Value-Pair structure.

        delta_keys defaults from the draft's profile (d16+ keys are
        delta-decoded). When buf_end is supplied the parameter block is
        bounded to it: a declared count or length that would read past
        buf_end raises MOQTProtocolViolation (spec §9 — the Length
        field is the authoritative message extent).

        Returns:
            Dict mapping parameter type to value
        """
        if delta_keys is None:
            delta_keys = profile_for(draft).params_delta_coded
        params = {}
        param_count = buf.pull_uint_var()
        prev_key = 0

        for _ in range(param_count):
            if buf_end is not None and buf.tell() >= buf_end:
                raise MOQTProtocolViolation(
                    f"parameters declared {param_count} but buffer "
                    f"exhausted at {buf.tell()}/{buf_end}")
            raw_key = buf.pull_uint_var()
            if delta_keys:
                param_type = prev_key + raw_key
                prev_key = param_type
            else:
                param_type = raw_key
            
            if param_type % 2 == 1:  # Odd type - includes Length field
                param_len = buf.pull_uint_var()
                if param_len > 65535:  # 2^16-1 maximum
                    raise MOQTProtocolViolation(
                        "parameter length exceeds maximum of 65535 bytes")
                if buf_end is not None and buf.tell() + param_len > buf_end:
                    raise MOQTProtocolViolation(
                        f"parameter length {param_len} exceeds remaining "
                        f"{buf_end - buf.tell()}")
                logger.info(f"deserializing param {param_type} length {param_len}")
                param_value = buf.pull_bytes(param_len)

                # AUTH_TOKEN: unwrap Token structure (Section 9.2.1.1)
                if param_type in (ParamType.AUTH_TOKEN, SetupParamType.AUTH_TOKEN) and param_len > 0:
                    token_buf = Buffer(data=param_value)
                    alias_type = token_buf.pull_uint_var()
                    if alias_type == AuthTokenAliasType.USE_VALUE:
                        token_type = token_buf.pull_uint_var()
                        param_value = token_buf.pull_bytes(param_len - token_buf.tell())
                    elif alias_type == AuthTokenAliasType.USE_ALIAS:
                        token_alias = token_buf.pull_uint_var()
                        param_value = param_value  # keep raw for now
                    elif alias_type == AuthTokenAliasType.REGISTER:
                        token_alias = token_buf.pull_uint_var()
                        token_type = token_buf.pull_uint_var()
                        param_value = token_buf.pull_bytes(param_len - token_buf.tell())
                    logger.info(f"AUTH_TOKEN: alias_type={alias_type} value={len(param_value)} bytes")
            else:  # Even type - Value is varint
                param_value = buf.pull_uint_var()
            # Even-type varints are self-describing, so the frame bound
            # is checked after the read: an over-read past buf_end is a
            # wire violation, not a silent consume into the next message.
            if buf_end is not None and buf.tell() > buf_end:
                raise MOQTProtocolViolation(
                    f"parameter overran frame extent: "
                    f"{buf.tell()}/{buf_end}")
            params[param_type] = param_value
        
        return params
