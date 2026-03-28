from enum import IntEnum

MOQT_VERSION_DRAFT14 = 0xff00000e
MOQT_VERSION_DRAFT16 = 0xff000010

MOQT_VERSIONS = [
    MOQT_VERSION_DRAFT14,
    MOQT_VERSION_DRAFT16,
]
MOQT_CUR_VERSION = MOQT_VERSION_DRAFT14  # default; set per-session

# ALPN: draft-14 uses legacy "moq-00"; draft-16+ uses "moqt-NN"
MOQT_ALPN_LEGACY = "moq-00"
MOQT_ALPN_DRAFT14 = "moq-00"
MOQT_ALPN_DRAFT16 = "moqt-16"
MOQT_ALPN = MOQT_ALPN_LEGACY  # default for backward compat

def moqt_alpn_for_version(version: int) -> str:
    """Return the ALPN string for a given MoQT draft version."""
    from .context import get_major_version
    major = get_major_version(version)
    if major <= 14:
        return MOQT_ALPN_LEGACY
    return f"moqt-{major}"

def moqt_version_from_alpn(alpn: str) -> int:
    """Return the MoQT version code from an ALPN string."""
    if alpn == MOQT_ALPN_LEGACY:
        return MOQT_VERSION_DRAFT14
    if alpn.startswith("moqt-"):
        draft_num = int(alpn[5:])
        return 0xff000000 + draft_num
    raise ValueError(f"Unknown MoQT ALPN: {alpn}")

MOQT_DEFAULT_PRIORITY = 128

MOQT_TIMESTAMP_EXT = 0x20

class MOQTMessageType(IntEnum):
    """MOQT message type constants.

    Code points shared across drafts are listed once.
    Draft-14-only and draft-16-only types are annotated.
    Some code points are reused between drafts (e.g. 0x05, 0x07, 0x08, 0x0E).
    The correct interpretation depends on the negotiated version.
    """
    # -- Common (same semantics in both drafts) --
    CLIENT_SETUP = 0x20
    SERVER_SETUP = 0x21
    SUBSCRIBE_UPDATE = 0x02  # d14: SUBSCRIBE_UPDATE; d16: REQUEST_UPDATE (same code point)
    SUBSCRIBE = 0x03
    SUBSCRIBE_OK = 0x04
    PUBLISH_NAMESPACE = 0x06
    PUBLISH_NAMESPACE_DONE = 0x09
    UNSUBSCRIBE = 0x0A
    PUBLISH_DONE = 0x0B
    PUBLISH_NAMESPACE_CANCEL = 0x0C
    TRACK_STATUS = 0x0D
    GOAWAY = 0x10
    SUBSCRIBE_NAMESPACE = 0x11
    MAX_REQUEST_ID = 0x15
    FETCH = 0x16
    FETCH_CANCEL = 0x17
    FETCH_OK = 0x18
    REQUESTS_BLOCKED = 0x1A
    PUBLISH = 0x1D
    PUBLISH_OK = 0x1E

    # -- Draft-14 only (removed or repurposed in draft-16) --
    SUBSCRIBE_ERROR = 0x05         # d16: repurposed as REQUEST_ERROR
    PUBLISH_NAMESPACE_OK = 0x07    # d16: repurposed as REQUEST_OK
    PUBLISH_NAMESPACE_ERROR = 0x08 # d16: repurposed as NAMESPACE
    TRACK_STATUS_OK = 0x0E         # d16: repurposed as NAMESPACE_DONE
    TRACK_STATUS_ERROR = 0x0F      # d16: removed
    SUBSCRIBE_NAMESPACE_OK = 0x12  # d16: removed
    SUBSCRIBE_NAMESPACE_ERROR = 0x13  # d16: removed
    UNSUBSCRIBE_NAMESPACE = 0x14   # d16: removed
    FETCH_ERROR = 0x19             # d16: removed
    PUBLISH_ERROR = 0x1F           # d16: removed (use REQUEST_ERROR)

    # -- Draft-16 aliases (same code points, different semantics) --
    # These are aliases for the code points above, for clarity in d16 code paths.
    # Python IntEnum doesn't allow duplicate values, so we use class-level constants.

# Draft-16 message type aliases (same numeric values, different semantics)
# Use these in d16 code paths for clarity.
class D16MessageType:
    """Draft-16 message type aliases for repurposed code points."""
    REQUEST_UPDATE = 0x02       # was SUBSCRIBE_UPDATE
    REQUEST_ERROR = 0x05        # was SUBSCRIBE_ERROR
    REQUEST_OK = 0x07           # was PUBLISH_NAMESPACE_OK
    NAMESPACE = 0x08            # was PUBLISH_NAMESPACE_ERROR
    NAMESPACE_DONE = 0x0E       # was TRACK_STATUS_OK


class ParamType(IntEnum):
    """Parameter types for MOQT messages."""
    DELIVERY_TIMEOUT = 0x02
    AUTH_TOKEN = 0x03
    MAX_CACHE_DURATION = 0x04
    EXPIRES = 0x08
    LARGEST_OBJECT = 0x09   # d16: replaces Content Exists + Largest Location fields
    PUBLISHER_PRIORITY = 0x0E
    FORWARD = 0x10
    SUBSCRIBER_PRIORITY = 0x20
    SUBSCRIPTION_FILTER = 0x21
    GROUP_ORDER = 0x22
    DYNAMIC_GROUPS = 0x30
    NEW_GROUP_REQUEST = 0x32
    GREASE_1_PARAM = 0x55
    GREASE_2_PARAM = 0x8A


class SetupParamType(IntEnum):
    """Setup Parameter type constants"""
    PATH = 0x01  # only relevant to raw QUIC connection
    MAX_REQUEST_ID = 0x02
    AUTH_TOKEN = 0x03 
    MAX_AUTH_TOKEN_CACHE_SIZE = 0x04
    AUTHORITY = 0x05
    IMPLEMENTATION = 0x07  # Wrong in draft 14, draft-15 fixed it to this value
    GREASE_1_PARAM = 0x77
    GREASE_2_PARAM = 0x92

class SessionCloseCode(IntEnum):
    """Session close error codes."""
    NO_ERROR = 0x0
    INTERNAL_ERROR = 0x01
    UNAUTHORIZED = 0x02
    PROTOCOL_VIOLATION = 0x03
    INVALID_REQUEST_ID = 0x04
    DUPLICATE_TRACK_ALIAS = 0x05
    KEY_VALUE_FORMATTING_ERROR = 0x06
    TOO_MANY_REQUESTS = 0x07
    INVALID_PATH = 0x08
    MALFORMED_PATH = 0x09
    GOAWAY_TIMEOUT = 0x10
    CONTROL_MESSAGE_TIMEOUT = 0x11
    DATA_STREAM_TIMEOUT = 0x12
    AUTH_TOKEN_CACHE_OVERFLOW = 0x13
    DUPLICATE_AUTH_TOKEN_ALIAS = 0x14
    VERSION_NEGOTIATION_FAILED = 0x15
    MALFORMED_AUTH_TOKEN = 0x16
    UNKNOWN_AUTH_TOKEN_ALIAS = 0x17
    EXPIRED_AUTH_TOKEN = 0x18
    INVALID_AUTHORITY = 0x19
    MALFORMED_AUTHORITY = 0x1A

class ContentExistsCode(IntEnum):
    """Content Exists Code"""
    NO_CONTENT = 0x0
    EXISTS = 0x01
    
class AuthTokenAliasType(IntEnum):
    """Authorization Token Alias Types (Section 9.2.1.1)."""
    DELETE = 0x0      # Alias only — retire the alias and its token
    REGISTER = 0x1    # Alias + Type + Value — register alias for reuse
    USE_ALIAS = 0x2   # Alias only — use previously registered token
    USE_VALUE = 0x3   # Type + Value only — one-shot, no alias


class SubscribeErrorCode(IntEnum):
    """SUBSCRIBE_ERROR error codes (draft-14)."""
    INTERNAL_ERROR = 0x0
    UNAUTHORIZED = 0x01
    TIMEOUT = 0x02
    NOT_SUPPORTED = 0x03
    TRACK_DOES_NOT_EXIST = 0x04
    INVALID_RANGE = 0x05
    MALFORMED_AUTH_TOKEN = 0x10
    EXPIRED_AUTH_TOKEN = 0x12


class RequestErrorCode(IntEnum):
    """REQUEST_ERROR error codes (draft-16 unified)."""
    INTERNAL_ERROR = 0x0
    UNAUTHORIZED = 0x01
    TIMEOUT = 0x02
    NOT_SUPPORTED = 0x03
    MALFORMED_AUTH_TOKEN = 0x04   # was 0x10 in d14
    EXPIRED_AUTH_TOKEN = 0x05     # was 0x12 in d14
    DOES_NOT_EXIST = 0x10         # was 0x04 (TRACK_DOES_NOT_EXIST) in d14
    INVALID_RANGE = 0x11          # was 0x05 in d14
    MALFORMED_TRACK = 0x12
    DUPLICATE_SUBSCRIPTION = 0x19
    UNINTERESTED = 0x20           # was 0x04 in PublishErrorCode d14
    PREFIX_OVERLAP = 0x30
    INVALID_JOINING_REQUEST_ID = 0x32


class SubscribeDoneCode(IntEnum):
    """SUBSCRIBE_DONE / PUBLISH_DONE status codes."""
    INTERNAL_ERROR = 0x0
    UNAUTHORIZED = 0x01
    TRACK_ENDED = 0x02
    SUBSCRIPTION_ENDED = 0x03
    GOING_AWAY = 0x04
    EXPIRED = 0x05
    TOO_FAR_BEHIND = 0x06
    MALFORMED_TRACK = 0x07        # d14 value; d16 moves to 0x12
    UPDATE_FAILED = 0x08          # d16 only


class TrackStatusCode(IntEnum):
    """TRACK_STATUS status codes."""
    IN_PROGRESS = 0x00
    DOES_NOT_EXIST = 0x01
    NOT_STARTED = 0x02
    FINISHED = 0x03
    RELAY_NO_INFO = 0x04


class FilterType(IntEnum):
    """Subscription filter types."""
    NEXT_GROUP_START = 0x01
    LATEST_OBJECT = 0x02
    ABSOLUTE_START = 0x03
    ABSOLUTE_RANGE = 0x04


class GroupOrder(IntEnum):
    """Group ordering preferences."""
    PUBLISHER_DEFAULT = 0x0
    ASCENDING = 0x01
    DESCENDING = 0x02


class ObjectStatus(IntEnum):
    """Object status codes."""
    NORMAL = 0x0
    DOES_NOT_EXIST = 0x01  # draft-14 only; removed in draft-16
    END_OF_GROUP = 0x03
    END_OF_TRACK = 0x04


class ForwardingPreference(IntEnum):
    """Object forwarding preferences."""
    SUBGROUP = 0x01
    DATAGRAM = 0x02


class FetchType(IntEnum):
    FETCH = 0x01
    JOINING_FETCH = 0x02
    ABSOLUTE_JOINING = 0x03


class PublishErrorCode(IntEnum):
    """PUBLISH_ERROR error codes."""
    INTERNAL_ERROR = 0x0
    UNAUTHORIZED = 0x01
    TIMEOUT = 0x02
    NOT_SUPPORTED = 0x03
    UNINTERESTED = 0x04


class DataStreamType(IntEnum):
    """Stream type identifiers."""
    FETCH_HEADER = 0x05


# Draft-14 SubgroupHeader type range: 0x10-0x1D (12 valid types, 0x16-0x17 reserved)
# Bits: 0=extensions_present, 1-2=subgroup_id_mode, 3=end_of_group
SUBGROUP_HEADER_BASE = 0x10
SUBGROUP_ID_ZERO = 0        # subgroup_id = 0 (no field on wire)
SUBGROUP_ID_FIRST_OBJ = 1   # subgroup_id = first object's ID (no field on wire)
SUBGROUP_ID_EXPLICIT = 2    # subgroup_id present on wire

# Draft-14 ObjectDatagram type range: 0x00-0x07 (payload), 0x20-0x21 (status)
# Payload types bits: 0=extensions, 1=end_of_group, 2=no_object_id
# Status types bits: 0=extensions
OBJECT_DATAGRAM_BASE = 0x00
OBJECT_DATAGRAM_STATUS_BASE = 0x20

# Draft-16 adds DEFAULT_PRIORITY bit (0x20) to subgroup and datagram types.
# When set, Publisher Priority field is omitted (inherited from Track Extension).
SUBGROUP_DEFAULT_PRIORITY_BIT = 0x20   # d16 only
DATAGRAM_DEFAULT_PRIORITY_BIT = 0x08   # d16 only (bit 3 for datagrams)


class MOQTException(Exception):
    def __init__(self, error_code: SessionCloseCode, reason_phrase: str):
        self.error_code = error_code
        self.reason_phrase = reason_phrase
        super().__init__(f"{reason_phrase} ({error_code})")
        
