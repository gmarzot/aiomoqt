from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..base import BUF_SIZE, MOQTMessage, MOQTUnderflow
from ..d18 import Setup as _D18Setup
from ...context import DraftProfile
from ...types import MOQTProtocolViolation, SetupParamType
from ...utils.buffer import Buffer, BufferReadError
from ...utils.logger import get_logger

logger = get_logger(__name__)

# Setup Options registry, d20 §15.4 (d22 §16.4 lists the same rows).
# The Value form follows the Type's parity (§1.4.3): odd = Length + bytes,
# even = one varint.
SETUP_OPTIONS: Dict[int, str] = {
    SetupParamType.PATH: "PATH",
    SetupParamType.AUTH_TOKEN: "AUTHORIZATION_TOKEN",
    SetupParamType.MAX_AUTH_TOKEN_CACHE_SIZE: "MAX_AUTH_TOKEN_CACHE_SIZE",
    SetupParamType.AUTHORITY: "AUTHORITY",
    SetupParamType.MAX_FILTER_RANGES: "MAX_FILTER_RANGES",
    SetupParamType.IMPLEMENTATION: "MOQT_IMPLEMENTATION",
    SetupParamType.MAX_REQUEST_UPDATES: "MAX_REQUEST_UPDATES",
}

# §10.3.1.4: SETUP may carry one or more AUTHORIZATION TOKENs.
REPEATABLE_OPTIONS = frozenset({SetupParamType.AUTH_TOKEN})

SETUP_LENGTH_MAX = 0xFFFF       # Length (16)
OPTION_VALUE_MAX = 0xFFFF       # §1.4.3: a Value is at most 2^16-1 bytes
OPTION_TYPE_MAX = (1 << 64) - 1


@dataclass(slots=True)
class Setup(_D18Setup):
    """SETUP for d20 and d22 (d20 §10.3, d22 §9.1): type 0x2F00, a
    16-bit Length, then delta-coded Setup Options up to the Length.

    `options` holds the known options. Unknown ones are skipped, as the
    spec requires; their types land in `ignored`. A repeated
    AUTHORIZATION TOKEN keeps its first instance; another repeated known
    option is a PROTOCOL_VIOLATION.
    """
    ignored: List[int] = field(default_factory=list)

    def serialize(self, *, prof: DraftProfile) -> Buffer:
        payload = Buffer(capacity=BUF_SIZE, vi64=prof.vi64)
        prev = 0
        for key, value in sorted((self.options or {}).items()):
            key = int(key)
            if not 0 <= key <= OPTION_TYPE_MAX:
                raise ValueError(f"Setup Option type {key} out of range")
            payload.push_vint(key - prev)
            prev = key
            if key % 2:
                if key == SetupParamType.AUTH_TOKEN:
                    value = MOQTMessage._auth_token_wrap(value, prof)
                elif isinstance(value, str):
                    value = value.encode()
                if not isinstance(value, (bytes, bytearray)):
                    raise TypeError(
                        f"Setup Option 0x{key:x} takes bytes, got "
                        f"{type(value).__name__}")
                if len(value) > OPTION_VALUE_MAX:
                    raise ValueError(
                        f"Setup Option 0x{key:x} is {len(value)} bytes "
                        f"(max {OPTION_VALUE_MAX})")
                payload.push_vint(len(value))
                payload.push_bytes(bytes(value))
            else:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise TypeError(
                        f"Setup Option 0x{key:x} takes an integer, got "
                        f"{type(value).__name__}")
                if not 0 <= value <= OPTION_TYPE_MAX:
                    raise ValueError(
                        f"Setup Option 0x{key:x} value {value} out of range")
                payload.push_vint(value)
        length = payload.tell()
        if length > SETUP_LENGTH_MAX:
            raise ValueError(
                f"SETUP is {length} bytes; Length is 16 bits")
        buf = Buffer(capacity=length + 16, vi64=prof.vi64)
        buf.push_vint(self.type)
        buf.push_uint16(length)
        buf.push_bytes(payload.data_slice(0, length))
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, *, prof: DraftProfile,
                    buf_end: Optional[int] = None) -> 'Setup':
        """Parse the Setup Options; the dispatch loop has consumed Type and
        Length, and buf_end is where the Length ends."""
        if buf_end is None:
            buf_end = buf.capacity
        buf.vi64 = prof.vi64
        options: Dict[int, object] = {}
        ignored: List[int] = []
        prev = 0

        def check_extent(what: str) -> None:
            if buf.tell() > buf_end:
                raise MOQTProtocolViolation(
                    f"SETUP {what} runs {buf.tell() - buf_end} bytes past "
                    f"its Length")

        try:
            while buf.tell() < buf_end:
                key = prev + buf.pull_vint()
                check_extent("option type")
                if key > OPTION_TYPE_MAX:
                    raise MOQTProtocolViolation(
                        f"Setup Option type overflows 2^64-1: {key}")
                prev = key
                if key % 2:
                    vlen = buf.pull_vint()
                    check_extent(f"option 0x{key:x} length")
                    if vlen > OPTION_VALUE_MAX:
                        raise MOQTProtocolViolation(
                            f"Setup Option 0x{key:x} Length {vlen} exceeds "
                            f"{OPTION_VALUE_MAX}")
                    if buf.tell() + vlen > buf_end:
                        raise MOQTProtocolViolation(
                            f"Setup Option 0x{key:x} Length {vlen} runs "
                            f"past the SETUP Length")
                    value = buf.pull_bytes(vlen)
                else:
                    value = buf.pull_vint()
                    check_extent(f"option 0x{key:x} value")
                if key not in SETUP_OPTIONS:
                    ignored.append(key)
                    continue
                if key in options:
                    if key in REPEATABLE_OPTIONS:
                        logger.debug(
                            f"MOQT: repeated {SETUP_OPTIONS[key]} in SETUP: "
                            f"only the first is used")
                        continue
                    raise MOQTProtocolViolation(
                        f"Setup Option {SETUP_OPTIONS[key]} repeated")
                if key == SetupParamType.AUTH_TOKEN:
                    value = MOQTMessage._auth_token_unwrap(value, prof)
                options[key] = value
        except (BufferReadError, MOQTUnderflow):
            raise MOQTProtocolViolation(
                "SETUP truncated: Setup Options end before its Length") \
                from None
        return cls(options=options, ignored=ignored)
