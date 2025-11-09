from enum import IntEnum

MOQT_VERSIONS = [
    0xff00000e
]
MOQT_CUR_VERSION = 0xff00000e

MOQT_ALPN = "moq-00"

MOQT_DEFAULT_PRIORITY = 128

MOQT_TIMESTAMP_EXT = 0x20

class MOQTMessageType(IntEnum):
    """MOQT message type constants."""
    CLIENT_SETUP = 0x20 # changed from 0x40 in draft-14
    SERVER_SETUP = 0x21 # changed from 0x41 in draft-14
    GOAWAY = 0x10
    MAX_REQUEST_ID = 0x15
    REQUESTS_BLOCKED = 0x1A
    SUBSCRIBE = 0x03
    SUBSCRIBE_OK = 0x04
    SUBSCRIBE_ERROR = 0x05
    SUBSCRIBE_UPDATE = 0x02
    UNSUBSCRIBE = 0x0A
    PUBLISH_NAMESPACE = 0x06
    PUBLISH_NAMESPACE_OK = 0x07
    PUBLISH_NAMESPACE_ERROR = 0x08
    PUBLISH_NAMESPACE_DONE = 0x09
    PUBLISH_DONE = 0x0B
    PUBLISH_NAMESPACE_CANCEL = 0x0C
    TRACK_STATUS_REQUEST = 0x0D
    TRACK_STATUS = 0x0E
    SUBSCRIBE_NAMESPACE = 0x11
    SUBSCRIBE_NAMESPACE_OK = 0x12
    SUBSCRIBE_NAMESPACE_ERROR = 0x13
    UNSUBSCRIBE_NAMESPACE = 0x14
    FETCH = 0x16
    FETCH_CANCEL = 0x17
    FETCH_OK = 0x18
    FETCH_ERROR = 0x19
    PUBLISH = 0x1B  # New in draft-14
    PUBLISH_OK = 0x1C
    PUBLISH_ERROR = 0x1D


class ParamType(IntEnum):
    """Parameter types for MOQT messages."""
    DELIVERY_TIMEOUT = 0x02
    AUTH_TOKEN = 0x03
    MAX_CACHE_DURATION = 0x04
    EXPIRES = 0x08
    LARGEST_OBJECT = 0x09
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
    DUPLICATE_TRACK_ALIAS = 0x04
    PARAMETER_LENGTH_MISMATCH = 0x05
    TOO_MANY_SUBSCRIBES = 0x06
    GOAWAY_TIMEOUT = 0x10
    CONTROL_MESSAGE_TIMEOUT = 0x11
    DATA_STREAM_TIMEOUT = 0x12

class ContentExistsCode(IntEnum):
    """Content Exists Code"""
    NO_CONTENT = 0x0
    EXISTS = 0x01
    
class SubscribeErrorCode(IntEnum):
    """SUBSCRIBE_ERROR error codes."""
    INTERNAL_ERROR = 0x0
    INVALID_RANGE = 0x01
    RETRY_TRACK_ALIAS = 0x02
    TRACK_DOES_NOT_EXIST = 0x03
    UNAUTHORIZED = 0x04
    TIMEOUT = 0x05


class SubscribeDoneCode(IntEnum):
    """SUBSCRIBE_DONE status codes."""
    UNSUBSCRIBED = 0x0
    INTERNAL_ERROR = 0x01
    UNAUTHORIZED = 0x02
    TRACK_ENDED = 0x03
    SUBSCRIPTION_ENDED = 0x04
    GOING_AWAY = 0x05
    EXPIRED = 0x06
    TOO_FAR_BEHIND = 0x07


class TrackStatusCode(IntEnum):
    """TRACK_STATUS status codes."""
    IN_PROGRESS = 0x00
    DOES_NOT_EXIST = 0x01
    NOT_STARTED = 0x02
    FINISHED = 0x03
    RELAY_NO_INFO = 0x04


class FilterType(IntEnum):
    """Subscription filter types."""
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
    DOES_NOT_EXIST = 0x01
    END_OF_GROUP = 0x03
    END_OF_TRACK_AND_GROUP = 0x04
    END_OF_TRACK = 0x05


class ForwardingPreference(IntEnum):
    """Object forwarding preferences."""
    TRACK = 0x0
    SUBGROUP = 0x01
    DATAGRAM = 0x02


class FetchType(IntEnum):
    FETCH = 0x01
    JOINING_FETCH = 0x02


class DataStreamType(IntEnum):
    """Stream type identifiers."""
    SUBGROUP_HEADER = 0x04
    FETCH_HEADER = 0x05


class DatagramType(IntEnum):
    """Datagram type identifiers."""
    OBJECT_DATAGRAM = 0x01
    OBJECT_DATAGRAM_STATUS = 0x02


class MOQTException(Exception):
    def __init__(self, error_code: SessionCloseCode, reason_phrase: str):
        self.error_code = error_code
        self.reason_phrase = reason_phrase
        super().__init__(f"{reason_phrase} ({error_code})")
        
