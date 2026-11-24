"""moq-fabric wire codec, as Quicr/moq-fabric f10e8bc5 sends and accepts it.

Sans-I/O. Fixed-width integers are big-endian, except NodeInfo path items:
the library memcpy's those in host order, little-endian on every platform
it builds for. Strings are raw bytes. The one varint (NodeInfo contact
length) is the MoQT leading-ones varint, not the QUIC one.
"""
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import (List, Literal, NamedTuple, Optional, Sequence, Tuple,
                    Union, overload)

ALPN = "moq-fabric"
DEFAULT_PORT = 33434
PROTOCOL_VERSION = 1
HEADER_SIZE = 7
PATH_ITEM_SIZE = 24
DATAGRAM_HEADER_LEN = 14
NEW_STREAM_HEADER_LEN = 19
DEFAULT_NODE_METRIC = 100
# Ours; the library has none. Longer messages are skipped (strict: rejected).
MAX_MESSAGE_SIZE = 1 << 20

ANNOUNCE_NAMESPACE = 0x01


class MsgType(IntEnum):
    CONNECT = 1
    CONNECT_RESPONSE = 2
    DATA_OBJECT = 3
    NODE_INFO_ADV = 4
    NODE_INFO_WD = 5
    SUBSCRIBE_INFO_ADV = 6
    SUBSCRIBE_INFO_WD = 7
    ANNOUNCE_INFO_ADV = 8
    ANNOUNCE_INFO_WD = 9
    SNS_ADV = 10
    SNS_WD = 11


class NodeType(IntEnum):
    VIA = 0
    EDGE = 1
    STUB = 2


class PeerMode(IntEnum):
    IBP = 0
    DATA = 1
    BOTH = 2


class ConnectCode(IntEnum):
    OK = 0
    CONNECT_ERROR = 1
    NOT_AUTHORIZED = 2


class DataType(IntEnum):
    DATAGRAM = 0
    EXISTING_STREAM = 1
    NEW_STREAM = 2


class WireError(ValueError):
    """Malformed moq-fabric bytes."""


_U64 = (1 << 64) - 1
_HEADER = struct.Struct(">BHI")
_NODE_HEAD = struct.Struct(">QB")
_NODE_TAIL = struct.Struct(">ddQ")
_PATH = struct.Struct("<QQQ")
_SUB_HEAD = struct.Struct(">HHQQQQBQQQB")
_U8 = struct.Struct(">B")
_U16 = struct.Struct(">H")
_U64S = struct.Struct(">Q")
_ANN_HEAD = struct.Struct(">QQ")
_SNS_HEAD = struct.Struct(">IBH")
_SNS_ID = struct.Struct(">I")
_DGRAM_HDR = struct.Struct(">BBIQ")
_STREAM_HDR = struct.Struct(">BBIQBI")

Bytes = Union[bytes, bytearray, memoryview]


# -- varint ----------------------------------------------------------

def encode_varint(value: int) -> bytes:
    """MoQT leading-ones varint, minimal length."""
    if not 0 <= value <= _U64:
        raise WireError(f"varint out of range: {value}")
    bits = value.bit_length()
    length = 1 if bits == 0 else min(9, (bits + 6) // 7)
    if length == 9:
        return b"\xff" + value.to_bytes(8, "big")
    out = bytearray(value.to_bytes(length, "big"))
    if length > 1:
        out[0] |= (0xFF << (9 - length)) & 0xFF
    return bytes(out)


def decode_varint(buf: Bytes, offset: int = 0) -> Tuple[int, int]:
    """(value, next offset). Accepts non-minimal encodings."""
    if offset >= len(buf):
        raise WireError("varint: no bytes")
    first = buf[offset]
    ones = 8 - (~first & 0xFF).bit_length()
    length = 9 if ones == 8 else ones + 1
    end = offset + length
    if end > len(buf):
        raise WireError(f"varint: needs {length} bytes, has {len(buf) - offset}")
    if length == 9:
        return int.from_bytes(buf[offset + 1:end], "big"), end
    head = first & ((1 << (8 - length)) - 1)
    return int.from_bytes(bytes([head]) + bytes(buf[offset + 1:end]), "big"), end


# -- helpers ---------------------------------------------------------

def _need(buf: Bytes, offset: int, size: int, what: str) -> None:
    if len(buf) - offset < size:
        raise WireError(f"{what}: truncated at {offset} "
                        f"(needs {size}, has {len(buf) - offset})")


def _str(raw: Bytes) -> str:
    return bytes(raw).decode("utf-8", "surrogateescape")


def _raw(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def _encode_name(namespace: Sequence[bytes], name: bytes) -> bytes:
    if len(namespace) > 0xFF:
        raise WireError(f"namespace has {len(namespace)} tuples, max 255")
    out = bytearray(_U8.pack(len(namespace)))
    for entry in namespace:
        if len(entry) > 0xFFFF:
            raise WireError("namespace tuple longer than 65535 bytes")
        out += _U16.pack(len(entry)) + bytes(entry)
    if len(name) > 0xFFFF:
        raise WireError("name longer than 65535 bytes")
    out += _U16.pack(len(name)) + bytes(name)
    return bytes(out)


def _decode_name(buf: Bytes, off: int,
                 what: str) -> Tuple[Tuple[bytes, ...], bytes, int]:
    _need(buf, off, 1, what)
    count = buf[off]
    off += 1
    entries = []
    for _ in range(count):
        _need(buf, off, 2, what)
        (size,) = _U16.unpack_from(buf, off)
        off += 2
        _need(buf, off, size, what)
        entries.append(bytes(buf[off:off + size]))
        off += size
    _need(buf, off, 2, what)
    (size,) = _U16.unpack_from(buf, off)
    off += 2
    _need(buf, off, size, what)
    return tuple(entries), bytes(buf[off:off + size]), off + size


# -- message bodies --------------------------------------------------

@dataclass(frozen=True)
class PathItem:
    node_id: int
    srtt_us: int = 0
    metric: int = 0


@dataclass(frozen=True)
class NodeInfo:
    """A node advertisement; also the tail of CONNECT and CONNECT_RESPONSE."""
    node_id: int
    node_type: int = NodeType.EDGE
    contact: str = ""
    longitude: float = 0.0
    latitude: float = 0.0
    metric: int = DEFAULT_NODE_METRIC
    path: Tuple[PathItem, ...] = ()

    def sum_metric(self) -> int:
        return self.metric + sum(p.metric for p in self.path)

    def sum_srtt(self) -> int:
        return sum(p.srtt_us for p in self.path)

    def encode(self) -> bytes:
        contact = _raw(self.contact)
        out = bytearray(_NODE_HEAD.pack(self.node_id, self.node_type))
        out += encode_varint(len(contact)) + contact
        out += _NODE_TAIL.pack(self.longitude, self.latitude, self.metric)
        for p in self.path:
            out += _PATH.pack(p.node_id, p.srtt_us, p.metric)
        return bytes(out)

    @classmethod
    def decode(cls, buf: Bytes) -> "NodeInfo":
        _need(buf, 0, _NODE_HEAD.size, "NodeInfo")
        node_id, node_type = _NODE_HEAD.unpack_from(buf, 0)
        size, off = decode_varint(buf, _NODE_HEAD.size)
        _need(buf, off, size, "NodeInfo contact")
        contact = _str(buf[off:off + size])
        off += size
        _need(buf, off, _NODE_TAIL.size, "NodeInfo")
        lon, lat, metric = _NODE_TAIL.unpack_from(buf, off)
        off += _NODE_TAIL.size
        rest = len(buf) - off
        if rest % PATH_ITEM_SIZE:
            raise WireError(f"NodeInfo: {rest} path bytes, not a multiple "
                            f"of {PATH_ITEM_SIZE}")
        path = tuple(PathItem(*_PATH.unpack_from(buf, off + i))
                     for i in range(0, rest, PATH_ITEM_SIZE))
        return cls(node_id, node_type, contact, lon, lat, metric, path)


@dataclass(frozen=True)
class Connect:
    node: NodeInfo
    mode: int = PeerMode.BOTH

    def encode(self) -> bytes:
        return _U8.pack(self.mode) + self.node.encode()

    @classmethod
    def decode(cls, buf: Bytes) -> "Connect":
        _need(buf, 0, 1, "CONNECT")
        return cls(NodeInfo.decode(buf[1:]), buf[0])


@dataclass(frozen=True)
class ConnectResponse:
    code: int = ConnectCode.OK
    node: Optional[NodeInfo] = None

    def encode(self) -> bytes:
        if self.code == ConnectCode.OK:
            if self.node is None:
                raise WireError("CONNECT_RESPONSE ok needs a NodeInfo")
            return _U16.pack(self.code) + self.node.encode()
        return _U16.pack(self.code)

    @classmethod
    def decode(cls, buf: Bytes) -> "ConnectResponse":
        _need(buf, 0, 2, "CONNECT_RESPONSE")
        (code,) = _U16.unpack_from(buf, 0)
        if code != ConnectCode.OK:
            return cls(code)
        return cls(code, NodeInfo.decode(buf[2:]))


@dataclass(frozen=True)
class SubscribeInfo:
    """SUBSCRIBE_INFO_ADV / _WD body."""
    source: int
    namespace: Tuple[bytes, ...]
    name: bytes
    namespace_hash: int
    name_hash: int
    fullname_hash: int
    epoch: int = 0
    seq: int = 0
    priority: int = 128
    delivery_timeout_ms: int = 5000
    expires_ms: int = 5000
    forward: int = 1
    new_group_request_id: Optional[int] = None

    def encode(self) -> bytes:
        ng = self.new_group_request_id
        out = bytearray(_SUB_HEAD.pack(
            self.epoch, self.seq, self.source, self.namespace_hash,
            self.name_hash, self.fullname_hash, self.priority,
            self.delivery_timeout_ms, self.expires_ms, self.forward,
            0 if ng is None else 1))
        if ng is not None:
            out += _U64S.pack(ng)
        return bytes(out + _encode_name(self.namespace, self.name))

    @classmethod
    def decode(cls, buf: Bytes) -> "SubscribeInfo":
        _need(buf, 0, _SUB_HEAD.size, "SUBSCRIBE_INFO")
        (epoch, seq, source, ns_hash, name_hash, full_hash, priority,
         timeout, expires, forward, ng_present) = _SUB_HEAD.unpack_from(buf, 0)
        off = _SUB_HEAD.size
        ng = None
        if ng_present:
            _need(buf, off, 8, "SUBSCRIBE_INFO new group")
            (ng,) = _U64S.unpack_from(buf, off)
            off += 8
        namespace, name, _ = _decode_name(buf, off, "SUBSCRIBE_INFO name")
        return cls(source, namespace, name, ns_hash, name_hash, full_hash,
                   epoch, seq, priority, timeout, expires, forward, ng)


@dataclass(frozen=True)
class AnnounceInfo:
    """ANNOUNCE_INFO_ADV / _WD body. flags bit 0: namespace, else track."""
    source: int
    fullname_hash: int
    namespace: Tuple[bytes, ...]
    name: bytes = b""
    flags: int = 0

    @property
    def is_namespace(self) -> bool:
        return bool(self.flags & ANNOUNCE_NAMESPACE)

    def encode(self) -> bytes:
        return (_ANN_HEAD.pack(self.source, self.fullname_hash)
                + _encode_name(self.namespace, self.name)
                + _U8.pack(self.flags))

    @classmethod
    def decode(cls, buf: Bytes) -> "AnnounceInfo":
        _need(buf, 0, _ANN_HEAD.size, "ANNOUNCE_INFO")
        source, full_hash = _ANN_HEAD.unpack_from(buf, 0)
        namespace, name, off = _decode_name(buf, _ANN_HEAD.size,
                                            "ANNOUNCE_INFO name")
        _need(buf, off, 1, "ANNOUNCE_INFO flags")
        return cls(source, full_hash, namespace, name, buf[off])


@dataclass(frozen=True)
class NodeSet:
    """SUBSCRIBE_NODE_SET (SNS). A withdraw carries only sns_id."""
    sns_id: int
    priority: int = 2
    nodes: Tuple[int, ...] = ()

    def encode(self, withdraw: bool = False) -> bytes:
        if withdraw:
            return _SNS_ID.pack(self.sns_id)
        nodes = sorted(set(self.nodes))
        return (_SNS_HEAD.pack(self.sns_id, self.priority, len(nodes))
                + b"".join(_U64S.pack(n) for n in nodes))

    @classmethod
    def decode(cls, buf: Bytes, withdraw: bool = False) -> "NodeSet":
        if withdraw:
            _need(buf, 0, 4, "SNS_WD")
            return cls(_SNS_ID.unpack_from(buf, 0)[0])
        _need(buf, 0, _SNS_HEAD.size, "SNS_ADV")
        sns_id, priority, count = _SNS_HEAD.unpack_from(buf, 0)
        _need(buf, _SNS_HEAD.size, 8 * count, "SNS_ADV nodes")
        nodes = tuple(_U64S.unpack_from(buf, _SNS_HEAD.size + 8 * i)[0]
                      for i in range(count))
        return cls(sns_id, priority, nodes)


Body = Union[Connect, ConnectResponse, NodeInfo, SubscribeInfo,
             AnnounceInfo, NodeSet]

_BODY = {
    MsgType.CONNECT: Connect,
    MsgType.CONNECT_RESPONSE: ConnectResponse,
    MsgType.NODE_INFO_ADV: NodeInfo,
    MsgType.NODE_INFO_WD: NodeInfo,
    MsgType.SUBSCRIBE_INFO_ADV: SubscribeInfo,
    MsgType.SUBSCRIBE_INFO_WD: SubscribeInfo,
    MsgType.ANNOUNCE_INFO_ADV: AnnounceInfo,
    MsgType.ANNOUNCE_INFO_WD: AnnounceInfo,
    MsgType.SNS_ADV: NodeSet,
    MsgType.SNS_WD: NodeSet,
}


class Message(NamedTuple):
    type: MsgType
    body: Body
    version: int = PROTOCOL_VERSION


def encode_body(msg_type: MsgType, body: Body) -> bytes:
    if not isinstance(body, _BODY[msg_type]):
        raise WireError(f"{msg_type.name} takes {_BODY[msg_type].__name__}, "
                        f"not {type(body).__name__}")
    if isinstance(body, NodeSet):
        return body.encode(withdraw=msg_type == MsgType.SNS_WD)
    return body.encode()


def encode_message(msg_type: MsgType, body: Body,
                   version: int = PROTOCOL_VERSION) -> bytes:
    """Common 7-byte header plus body."""
    payload = encode_body(msg_type, body)
    return _HEADER.pack(version, msg_type, len(payload)) + payload


def decode_body(msg_type: MsgType, payload: Bytes) -> Body:
    if msg_type == MsgType.SNS_WD:
        return NodeSet.decode(payload, withdraw=True)
    cls = _BODY.get(msg_type)
    if cls is None:
        raise WireError(f"no body codec for message type {int(msg_type)}")
    return cls.decode(payload)


class ControlReader:
    """Reassembles control messages from the control stream's bytes.

    Strict mode raises WireError on anything malformed. Lenient mode skips
    the bad message by its header length and counts it: malformed bodies,
    unknown types and oversize lengths (discarded as they arrive, never
    buffered). A version byte other than 1 is counted and still decoded,
    as the library does.
    """

    def __init__(self, strict: bool = False,
                 max_message_size: int = MAX_MESSAGE_SIZE):
        self.strict = strict
        self.max_message_size = max_message_size
        self.malformed = 0
        self.unknown = 0
        self.bad_version = 0
        self.oversize = 0
        self.last_error: Optional[str] = None
        self._buf = bytearray()
        self._skip = 0

    @property
    def buffered(self) -> int:
        return len(self._buf)

    @property
    def rejected(self) -> int:
        return self.malformed + self.unknown + self.bad_version + self.oversize

    def _reject(self, counter: str, error: str) -> None:
        if self.strict:
            raise WireError(error)
        setattr(self, counter, getattr(self, counter) + 1)
        self.last_error = error

    def feed(self, data: Bytes) -> List[Message]:
        if self._skip:
            n = min(self._skip, len(data))
            self._skip -= n
            data = data[n:]
        self._buf += data
        buf = self._buf
        out = []
        while len(buf) >= HEADER_SIZE:
            version, mtype, length = _HEADER.unpack_from(buf, 0)
            total = HEADER_SIZE + length
            if length > self.max_message_size:
                self._reject("oversize", f"message type {mtype} length "
                             f"{length} over {self.max_message_size}")
                if len(buf) >= total:
                    del buf[:total]
                    continue
                self._skip = total - len(buf)
                buf.clear()
                break
            if len(buf) < total:
                break
            payload = bytes(buf[HEADER_SIZE:total])
            del buf[:total]
            if version != PROTOCOL_VERSION:
                self._reject("bad_version", f"protocol version {version}")
            try:
                msg_type = MsgType(mtype)
            except ValueError:
                msg_type = None
            if msg_type is None or msg_type == MsgType.DATA_OBJECT:
                self._reject("unknown", f"unknown message type {mtype}")
                continue
            try:
                body = decode_body(msg_type, payload)
            except WireError as e:
                self._reject("malformed", f"{msg_type.name}: {e}")
                continue
            out.append(Message(msg_type, body, version))
        return out


# -- data plane headers ----------------------------------------------

@dataclass(frozen=True)
class DataHeader:
    """DATAGRAM (14 bytes) or NEW_STREAM (19 bytes) header."""
    type: int
    sns_id: int
    fullname_hash: int
    priority: int = 1
    ttl_ms: int = 2000

    def encode(self) -> bytes:
        if self.type == DataType.DATAGRAM:
            return _DGRAM_HDR.pack(DATAGRAM_HEADER_LEN, self.type,
                                   self.sns_id, self.fullname_hash)
        if self.type == DataType.NEW_STREAM:
            return _STREAM_HDR.pack(NEW_STREAM_HEADER_LEN, self.type,
                                    self.sns_id, self.fullname_hash,
                                    self.priority, self.ttl_ms)
        if self.type == DataType.EXISTING_STREAM:
            return b""
        raise WireError(f"data type {self.type}")


@overload
def parse_data_header(buf: Bytes, datagram: Literal[True]
                      ) -> Tuple[DataHeader, int]: ...


@overload
def parse_data_header(buf: Bytes, datagram: bool = False
                      ) -> Optional[Tuple[DataHeader, int]]: ...


def parse_data_header(buf: Bytes, datagram: bool = False
                      ) -> Optional[Tuple[DataHeader, int]]:
    """(header, offset of the inner bytes), or None until enough bytes.

    A uni stream must open with NEW_STREAM and a datagram with DATAGRAM.
    header_len may exceed the known size (later fields are skipped), never
    fall short of it.
    """
    if len(buf) < 2:
        if datagram:
            raise WireError("datagram shorter than its header")
        return None
    header_len, dtype = buf[0], buf[1]
    want, layout = ((DataType.DATAGRAM, _DGRAM_HDR) if datagram
                    else (DataType.NEW_STREAM, _STREAM_HDR))
    if dtype != want:
        raise WireError(f"data type {dtype}, expected {int(want)}")
    if header_len < layout.size:
        raise WireError(f"header_len {header_len} below {layout.size}")
    if len(buf) < header_len:
        if datagram:
            raise WireError("datagram shorter than its header_len")
        return None
    fields = layout.unpack_from(buf, 0)
    if datagram:
        return DataHeader(dtype, fields[2], fields[3]), header_len
    return DataHeader(dtype, fields[2], fields[3], fields[4],
                      fields[5]), header_len


def rewrite_sns(buf: Bytes, sns_id: int) -> bytes:
    """The data header with its SNS id (offset 2..5) replaced, as a via does."""
    if len(buf) < 6:
        raise WireError("data header shorter than its SNS id")
    return bytes(buf[:2]) + _SNS_ID.pack(sns_id) + bytes(buf[6:])
