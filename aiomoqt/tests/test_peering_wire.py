"""moq-peering wire codec, byte-exact both ways, plus malformed input.

Vectors are the output of moq-peering d0bfdc9's own serializers (its unit-test inputs), compiled with g++ 11.4 -std=c++20.
"""
import struct

import pytest
from aiopquic.buffer import Buffer

from aiomoqt.peering.session import PeerSession
from aiomoqt.peering.wire import (
    AnnounceInfo, Connect, ConnectCode, ConnectResponse, ControlReader,
    DataHeader, DataType, MsgType, NodeInfo, NodeSet, NodeType, PathItem,
    SubscribeInfo, WireError, decode_body, decode_varint, encode_message,
    encode_varint, parse_data_header, rewrite_sns)

# value -> moq-peering UintVar bytes
VARINTS = [
    (0, "00"), (63, "3f"), (64, "40"), (127, "7f"), (128, "8080"),
    (300, "812c"), (16383, "bfff"), (16384, "c04000"),
    (2097151, "dfffff"), (2097152, "e0200000"),
    ((1 << 56) - 1, "feffffffffffffff"), (1 << 56, "ff0100000000000000"),
    (0xf9deacf2c69f506d, "fff9deacf2c69f506d"),
    ((1 << 64) - 1, "ffffffffffffffffff"),
]

ID_12_34 = 51539607586
ID_1_1 = (1 << 32) | 1
ID_2_2 = (2 << 32) | 2

NODE = NodeInfo(ID_12_34, NodeType.EDGE, "hello:1234", -122.5327124,
                47.6482974, 250, (PathItem(ID_1_1, 54321, 100),
                                  PathItem(ID_2_2, 12345, 400)))
NODE_BODY = (
    "0000000c00000022010a68656c6c6f3a31323334c05ea217f5c00b1d4047d2fb68c18a79"
    "00000000000000fa010000000100000031d4000000000000640000000000000002000000"
    "0200000039300000000000009001000000000000")

CONNECT = Connect(NodeInfo(ID_12_34, NodeType.EDGE, "localhost:1234",
                           -122.5327124, 47.6482974, 100,
                           (PathItem(ID_1_1, 54321), PathItem(ID_2_2, 12345))))
CONNECT_HEX = (
    "01000100000061020000000c00000022010e6c6f63616c686f73743a31323334c05ea217"
    "f5c00b1d4047d2fb68c18a790000000000000064010000000100000031d4000000000000"
    "0000000000000000020000000200000039300000000000000000000000000000")

CONNECT_OK = ConnectResponse(ConnectCode.OK, NodeInfo(
    (50 << 32) | 60, NodeType.EDGE, "relay.m10x.org:33435", -122.53271,
    47.64829, 100, (PathItem((1 << 32) | 99, 54321),)))
CONNECT_OK_HEX = (
    "010002000000500000000000320000003c011472656c61792e6d3130782e6f72673a3333"
    "343335c05ea217ebaf10234047d2fb2aae29740000000000000064630000000100000031"
    "d40000000000000000000000000000")

# A leaf's own CONNECT: id 9.9:1, empty path, metric 2^32.
LEAF_ID = (9 << 48) | (9 << 32) | 1
CONNECT_LEAF = Connect(NodeInfo(LEAF_ID, NodeType.EDGE, "aiomoqt-peer",
                                metric=1 << 32))
CONNECT_LEAF_HEX = (
    "0100010000002f020009000900000001010c61696f6d6f71742d70656572000000000000"
    "000000000000000000000000000100000000")

SNS = NodeSet(0x1234, 127, (ID_1_1, (200 << 32) | 300))
SNS_ADV_HEX = "01000a00000017000012347f00020000000100000001000000c80000012c"
SNS_WD_HEX = "01000b0000000400001234"

ANNOUNCE = AnnounceInfo(0xff00aabbcc, 0x9876543210,
                        (b"abc", b"12345", b"third tuple",
                         b"now the final tuple"), bytes(range(9)), 0)
ANNOUNCE_BODY = (
    "000000ff00aabbcc000000987654321004000361626300053132333435000b7468697264"
    "207475706c6500136e6f77207468652066696e616c207475706c6500090001020304050607"
    "0800")
ANNOUNCE_NS = AnnounceInfo(LEAF_ID, 0xf7260bfde8a2eee9, (b"demo",), b"", 1)
ANNOUNCE_NS_HEX = ("0100080000001a0009000900000001f7260bfde8a2eee901000464656d"
                   "6f000001")


def _sub(seq, new_group):
    return SubscribeInfo(0xff00aabbcc, (b"namespace",), b"track", 0xaabbcc,
                         0x9000, 0x1234567, epoch=3, seq=seq, priority=10,
                         delivery_timeout_ms=5000, expires_ms=1000, forward=1,
                         new_group_request_id=new_group)


SUB_NG_HEX = (
    "0100060000005900030001000000ff00aabbcc0000000000aabbcc000000000000900000"
    "000000012345670a000000000000138800000000000003e8000000000000000101000000"
    "000000002a0100096e616d6573706163650005747261636b")
SUB_HEX = (
    "0100060000005100030001000000ff00aabbcc0000000000aabbcc000000000000900000"
    "000000012345670a000000000000138800000000000003e8000000000000000100010009"
    "6e616d6573706163650005747261636b")
SUB_WD_HEX = (
    "0100070000005100030002000000ff00aabbcc0000000000aabbcc000000000000900000"
    "000000012345670a000000000000138800000000000003e8000000000000000100010009"
    "6e616d6573706163650005747261636b")


def _h(text):
    return bytes.fromhex(text)


# -- varint ----------------------------------------------------------

@pytest.mark.parametrize("value,hexed", VARINTS)
def test_varint_both_ways(value, hexed):
    assert encode_varint(value).hex() == hexed
    assert decode_varint(_h(hexed)) == (value, len(hexed) // 2)


@pytest.mark.parametrize("value,hexed", VARINTS)
def test_varint_matches_aiopquic_vi64(value, hexed):
    buf = Buffer(capacity=16)
    buf.push_uint_vi64(value)
    assert buf.data.hex() == hexed
    assert Buffer(data=_h(hexed)).pull_uint_vi64() == value


def test_varint_accepts_non_minimal_and_rejects_truncation():
    assert decode_varint(_h("8005")) == (5, 2)
    assert decode_varint(_h("ff0000000000000005")) == (5, 9)
    for bad in ("", "80", "c040", "ff01"):
        with pytest.raises(WireError):
            decode_varint(_h(bad))
    with pytest.raises(WireError):
        encode_varint(1 << 64)


# -- control messages ------------------------------------------------

def test_node_info_body():
    assert NODE.encode().hex() == NODE_BODY
    assert NodeInfo.decode(_h(NODE_BODY)) == NODE
    assert NODE.sum_metric() == 750
    assert NodeInfo.decode(_h(NODE_BODY)).path[0].node_id == ID_1_1


@pytest.mark.parametrize("msg_type,head", [
    (MsgType.NODE_INFO_ADV, "0100040000005c"),
    (MsgType.NODE_INFO_WD, "0100050000005c"),
])
def test_node_info_messages(msg_type, head):
    wire = _h(head + NODE_BODY)
    assert encode_message(msg_type, NODE) == wire
    assert ControlReader(strict=True).feed(wire) == [(msg_type, NODE, 1)]


@pytest.mark.parametrize("msg_type,body,hexed", [
    (MsgType.CONNECT, CONNECT, CONNECT_HEX),
    (MsgType.CONNECT, CONNECT_LEAF, CONNECT_LEAF_HEX),
    (MsgType.CONNECT_RESPONSE, CONNECT_OK, CONNECT_OK_HEX),
    (MsgType.CONNECT_RESPONSE, ConnectResponse(ConnectCode.CONNECT_ERROR),
     "010002000000020001"),
    (MsgType.SNS_ADV, SNS, SNS_ADV_HEX),
    (MsgType.SNS_WD, NodeSet(0x1234), SNS_WD_HEX),
    (MsgType.ANNOUNCE_INFO_ADV, ANNOUNCE, "0100080000004b" + ANNOUNCE_BODY),
    (MsgType.ANNOUNCE_INFO_WD, ANNOUNCE, "0100090000004b" + ANNOUNCE_BODY),
    (MsgType.ANNOUNCE_INFO_ADV, ANNOUNCE_NS, ANNOUNCE_NS_HEX),
    (MsgType.SUBSCRIBE_INFO_ADV, _sub(1, 42), SUB_NG_HEX),
    (MsgType.SUBSCRIBE_INFO_ADV, _sub(1, None), SUB_HEX),
    (MsgType.SUBSCRIBE_INFO_WD, _sub(2, None), SUB_WD_HEX),
], ids=lambda v: v.name if isinstance(v, MsgType) else None)
def test_message_both_ways(msg_type, body, hexed):
    wire = _h(hexed)
    assert encode_message(msg_type, body) == wire
    msgs = ControlReader(strict=True).feed(wire)
    assert msgs == [(msg_type, body, 1)]
    assert encode_message(msg_type, msgs[0].body) == wire


def test_sns_nodes_go_out_ascending():
    unsorted = NodeSet(0x1234, 127, ((200 << 32) | 300, ID_1_1))
    assert encode_message(MsgType.SNS_ADV, unsorted).hex() == SNS_ADV_HEX


def test_reader_splits_and_coalesces():
    wire = _h(CONNECT_OK_HEX + SNS_ADV_HEX + ANNOUNCE_NS_HEX)
    reader = ControlReader(strict=True)
    got = []
    for i in range(len(wire)):
        got += reader.feed(wire[i:i + 1])
    assert [m.type for m in got] == [MsgType.CONNECT_RESPONSE, MsgType.SNS_ADV,
                                     MsgType.ANNOUNCE_INFO_ADV]
    assert reader.buffered == 0
    assert [m.type for m in ControlReader().feed(wire)] == [m.type for m in got]


# -- data headers ----------------------------------------------------

@pytest.mark.parametrize("hdr,hexed", [
    (DataHeader(DataType.DATAGRAM, 0x1234, 0xabcdef),
     "0e00000012340000000000abcdef"),
    (DataHeader(DataType.NEW_STREAM, 0x1234, 0xabcdef, 100, 5000),
     "1302000012340000000000abcdef6400001388"),
    (DataHeader(DataType.NEW_STREAM, 1, 0xf9deacf2c69f506d, 128, 5000),
     "130200000001f9deacf2c69f506d8000001388"),
])
def test_data_header_both_ways(hdr, hexed):
    assert hdr.encode().hex() == hexed
    datagram = hdr.type == DataType.DATAGRAM
    inner = b"\x51\xff"
    assert parse_data_header(_h(hexed) + inner, datagram=datagram) == (
        hdr if not datagram else DataHeader(hdr.type, hdr.sns_id,
                                            hdr.fullname_hash),
        len(hexed) // 2)


def test_existing_stream_has_no_header():
    assert DataHeader(DataType.EXISTING_STREAM, 1, 2).encode() == b""


def test_stream_header_waits_for_its_bytes():
    wire = _h("1302000012340000000000abcdef6400001388")
    for n in range(len(wire)):
        assert parse_data_header(wire[:n]) is None
    assert parse_data_header(wire)[1] == 19


def test_via_rewrites_sns():
    wire = _h("130200000001f9deacf2c69f506d8000001388")
    assert rewrite_sns(wire, 18).hex() == "130200000012f9deacf2c69f506d8000001388"


@pytest.mark.parametrize("hexed,datagram", [
    ("0202", False),                                    # header_len 2, type 2
    ("1300000012340000000000abcdef6400001388", False),  # stream of type 0
    ("1301000012340000000000abcdef6400001388", False),  # stream of type 1
    ("0e02000012340000000000abcdef", True),             # datagram of type 2
    ("0e000000", True),                                 # datagram truncated
])
def test_data_header_rejects(hexed, datagram):
    with pytest.raises(WireError):
        parse_data_header(_h(hexed), datagram=datagram)


# -- malformed control input -----------------------------------------

def _header(msg_type, body, version=1):
    return struct.pack(">BHI", version, msg_type, len(body)) + body


MALFORMED = {
    # path remainder not a multiple of 24 (the library reads out of bounds)
    "path_partial": _header(MsgType.NODE_INFO_ADV, _h(NODE_BODY)[:-1]),
    # contact_len varint past the end of the body
    "contact_len": _header(MsgType.NODE_INFO_ADV,
                           _h("0000000c0000002201c0")),
    "contact_bytes": _header(MsgType.NODE_INFO_ADV,
                             _h("0000000c00000022010a68656c")),
    "node_no_metric": _header(MsgType.NODE_INFO_ADV, _h(NODE_BODY)[:40]),
    # ANNOUNCE truncated in a tuple, in the name, before the flags
    "announce_tuple": _header(MsgType.ANNOUNCE_INFO_ADV,
                              _h(ANNOUNCE_BODY)[:20]),
    "announce_name": _header(MsgType.ANNOUNCE_INFO_ADV,
                             _h(ANNOUNCE_BODY)[:-3]),
    "announce_flags": _header(MsgType.ANNOUNCE_INFO_ADV,
                              _h(ANNOUNCE_BODY)[:-1]),
    "subscribe": _header(MsgType.SUBSCRIBE_INFO_ADV, _h(SUB_HEX)[7:-1]),
    "subscribe_head": _header(MsgType.SUBSCRIBE_INFO_ADV, _h(SUB_HEX)[7:40]),
    "connect_empty": _header(MsgType.CONNECT, b""),
    "connect_ok_no_node": _header(MsgType.CONNECT_RESPONSE, b"\x00\x00"),
    "sns_count": _header(MsgType.SNS_ADV, _h("000012347f0003" + "00" * 16)),
    "sns_wd_short": _header(MsgType.SNS_WD, b"\x00\x12"),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_malformed_strict_rejects(name):
    with pytest.raises(WireError):
        ControlReader(strict=True).feed(MALFORMED[name])


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_malformed_lenient_skips_and_counts(name):
    reader = ControlReader()
    good = _h(SNS_WD_HEX)
    msgs = reader.feed(good + MALFORMED[name] + good)
    assert [m.type for m in msgs] == [MsgType.SNS_WD, MsgType.SNS_WD]
    assert reader.malformed == 1 and reader.last_error


def test_unknown_type_skipped_and_counted():
    unknown = _header(42, b"future") + _header(MsgType.DATA_OBJECT, b"x")
    with pytest.raises(WireError):
        ControlReader(strict=True).feed(unknown)
    reader = ControlReader()
    msgs = reader.feed(unknown + _h(SNS_WD_HEX))
    assert [m.type for m in msgs] == [MsgType.SNS_WD]
    assert reader.unknown == 2


def test_version_other_than_1_counted_and_decoded():
    wire = _h(CONNECT_HEX)
    bad = b"\x02" + wire[1:]
    with pytest.raises(WireError):
        ControlReader(strict=True).feed(bad)
    reader = ControlReader()
    assert reader.feed(bad) == [(MsgType.CONNECT, CONNECT, 2)]
    assert reader.bad_version == 1


def test_oversize_length_discarded_without_buffering():
    reader = ControlReader(max_message_size=64)
    big = _header(MsgType.ANNOUNCE_INFO_ADV, b"\xaa" * 1000)
    good = _h(SNS_WD_HEX)
    got = []
    for i in range(0, len(big), 100):
        got += reader.feed(big[i:i + 100])
        assert reader.buffered <= 100
    got += reader.feed(good)
    assert [m.type for m in got] == [MsgType.SNS_WD]
    assert reader.oversize == 1
    with pytest.raises(WireError):
        ControlReader(strict=True, max_message_size=64).feed(big[:7])


def test_declared_length_past_the_data_waits():
    reader = ControlReader()
    assert reader.feed(_h(SNS_WD_HEX)[:-1]) == []
    assert reader.feed(_h(SNS_WD_HEX)[-1:]) == [(MsgType.SNS_WD,
                                                  NodeSet(0x1234), 1)]


def test_encoders_refuse_what_cannot_be_framed():
    with pytest.raises(WireError):
        ConnectResponse(ConnectCode.OK).encode()
    with pytest.raises(WireError):
        AnnounceInfo(1, 2, tuple(b"x" for _ in range(256))).encode()
    with pytest.raises(WireError):
        AnnounceInfo(1, 2, (b"x" * 65536,)).encode()
    with pytest.raises(WireError):
        encode_message(MsgType.SNS_ADV, NODE)


def test_trailing_bytes_after_known_fields_ignored():
    """Later library versions may append fields; the known ones still
    decode, as in the library."""
    body = _h(ANNOUNCE_BODY) + b"\x07\x07"
    assert decode_body(MsgType.ANNOUNCE_INFO_ADV, body) == ANNOUNCE


# -- session (sans-I/O) ----------------------------------------------

LOCAL = NodeInfo(LEAF_ID, NodeType.STUB, "aiomoqt-peer", metric=1 << 32)


def _dialer():
    s = PeerSession(LOCAL, dialer=True)
    s.start(0)
    s.take_output()
    return s


def test_dialer_ignores_bytes_off_its_control_stream():
    s = _dialer()
    assert s.control_data(4, _h(ANNOUNCE_NS_HEX)) == [] and s.stray == 1


def test_dialer_opens_with_connect():
    s = PeerSession(LOCAL, dialer=True)
    s.start(0)
    assert s.take_output() == [encode_message(MsgType.CONNECT, Connect(LOCAL))]
    assert s.state == "connecting"


def test_acceptor_answers_connect_and_tolerates_early_sns():
    s = PeerSession(LOCAL, dialer=False)
    events = s.control_data(0, _h(CONNECT_HEX)[:10])
    assert events == [] and s.control_stream == 0
    events = s.control_data(0, _h(CONNECT_HEX)[10:] + _h(SNS_ADV_HEX))
    assert [e.kind for e in events] == ["connected", "sns"]
    assert s.take_output() == [encode_message(
        MsgType.CONNECT_RESPONSE, ConnectResponse(ConnectCode.OK, LOCAL))]
    assert s.remote == CONNECT.node and s.sns_in[0x1234].body == SNS


def test_dialer_takes_sns_before_connect_response():
    s = PeerSession(LOCAL, dialer=True)
    s.start(0)
    events = s.control_data(0, _h(SNS_ADV_HEX + CONNECT_OK_HEX))
    assert [e.kind for e in events] == ["sns", "connected"]
    assert s.remote == CONNECT_OK.node


def test_session_skips_bad_message_and_keeps_going():
    s = PeerSession(LOCAL, dialer=True)
    s.start(0)
    events = s.control_data(0, _h(CONNECT_OK_HEX) + MALFORMED["announce_name"]
                            + _h(ANNOUNCE_NS_HEX))
    assert [e.kind for e in events] == ["connected", "announce"]
    assert s.reader.malformed == 1 and s.state == "connected"


def test_session_checks_hashes():
    s = _dialer()
    forged = encode_message(MsgType.ANNOUNCE_INFO_ADV, ANNOUNCE)
    events = s.control_data(0, forged + _h(ANNOUNCE_NS_HEX))
    assert [(e.kind, e.hash_ok) for e in events] == [("announce", False),
                                                     ("announce", True)]
    assert s.hash_mismatches == 1


def test_subscribe_seq_rules():
    from aiomoqt.peering.hashing import track_hashes
    th = track_hashes((b"ns",), b"t")

    def sub(seq, epoch=0, forward=1):
        return encode_message(MsgType.SUBSCRIBE_INFO_ADV, SubscribeInfo(
            7, (b"ns",), b"t", *th, epoch=epoch, seq=seq, forward=forward))
    s = _dialer()
    kinds = [e.kind for e in s.control_data(0, sub(5))]
    assert kinds == ["subscribe"]
    assert s.control_data(0, sub(5)) == [] and s.duplicates == 1
    assert s.control_data(0, sub(4)) == [] and s.stale == 1
    assert s.control_data(0, sub(6, epoch=1)) == [] and s.stale == 2
    events = s.control_data(0, sub(6, forward=0))
    assert [(e.kind, e.note) for e in events] == [("subscribe", "update")]
    assert s.control_data(0, sub(32775 + 6)) == [] and s.stale == 3
    wrap = _dialer()
    wrap.control_data(0, sub(65535))
    assert wrap.control_data(0, sub(0))[0].body.seq == 0


def test_self_echo_and_withdraws():
    s = _dialer()
    echo = encode_message(MsgType.NODE_INFO_ADV, LOCAL)
    assert s.control_data(0, echo) == [] and s.self_echoes == 1
    adv = encode_message(MsgType.NODE_INFO_ADV, NODE)
    wd = encode_message(MsgType.NODE_INFO_WD, NODE)
    assert [e.kind for e in s.control_data(0, adv + adv + wd + wd)] == [
        "node", "node", "node_withdrawn"]
    assert NODE.node_id not in s.nodes


def test_data_headers_counted_not_decoded():
    s = _dialer()
    head = _h("130200000001f9deacf2c69f506d8000001388")
    inner = _h("51fff9deacf2c69f506d0580000005") + b"hello"
    events = s.stream_data(3, head[:5], False)
    events += s.stream_data(3, head[5:] + inner, False)
    events += s.stream_data(3, b"world", True)
    events += s.stream_data(7, head + b"x", True)
    assert [e.kind for e in events] == ["data_first"]
    flow = s.flows[0xf9deacf2c69f506d]
    assert (flow.streams, flow.bytes, flow.sns) == (2, len(inner) + 6, 1)
    assert s.stream_data(11, _h("1300"), True) == [] and s.bad_data == 1
    s.datagram(_h("0e00000000010000000000abcdef") + b"hi")
    assert s.flows[0xabcdef].datagrams == 1
