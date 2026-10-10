"""Wire conformance against independently-derived bytes.

Round-trip tests cannot catch codec bugs that are symmetric between our
encoder and decoder (RFC9000-vs-vi64 varints, absolute-vs-delta KVP
types — both shipped that way and passed loopback). Everything here is
checked against a reference codec implemented in-test from the spec
text, golden bytes captured from real peers, or exact hand-derived wire
images.

Golden capture: moq-dev relay SUBSCRIBE_OK (d18), 2026-08-13 — Track
Properties TIMESCALE(0x08)=1000 in minimal vi64. 0.10.6 failed to parse
it (RFC9000 pull on a vi64 field).
"""
import pytest

from aiomoqt.context import profile_for
from aiomoqt.messages import MOQTMessage
from aiomoqt.messages.subscribe import SubscribeOk
from aiomoqt.utils.buffer import Buffer
from aiopquic.streamchain import StreamChain


# -- reference vi64 (transport-18 §1.4.1), written from the spec ------

def ref_vi64(v: int) -> bytes:
    n = 1
    while n < 9 and v >= (1 << (7 * n)):
        n += 1
    if n == 9:
        return bytes([0xFF]) + v.to_bytes(8, 'big')
    first = ((0xFF << (9 - n)) & 0xFF) | (v >> (8 * (n - 1)))
    rest = v & ((1 << (8 * (n - 1))) - 1)
    return bytes([first]) + rest.to_bytes(n - 1, 'big')


def ref_rfc9000(v: int) -> bytes:
    for bits, prefix in ((6, 0x00), (14, 0x40), (30, 0x80), (62, 0xC0)):
        if v < (1 << bits):
            n = (bits + 2) // 8
            out = v.to_bytes(n, 'big')
            return bytes([out[0] | prefix]) + out[1:]
    raise ValueError(v)


_BOUNDARIES = [0, 1, 63, 64, 127, 128, 16383, 16384,
               (1 << 30) - 1, 1 << 30, (1 << 56) - 1, 1 << 56,
               (1 << 62) - 1]


@pytest.mark.parametrize("v", _BOUNDARIES)
def test_vi64_matches_reference(v):
    buf = Buffer(capacity=16)
    buf.push_uint_vi64(v)
    assert bytes(buf.data_slice(0, buf.tell())) == ref_vi64(v), hex(v)
    rb = Buffer(data=ref_vi64(v))
    assert rb.pull_uint_vi64() == v


@pytest.mark.parametrize("v", _BOUNDARIES)
def test_rfc9000_matches_reference(v):
    buf = Buffer(capacity=16)
    buf.push_uint_var(v)
    assert bytes(buf.data_slice(0, buf.tell())) == ref_rfc9000(v), hex(v)


# -- KVP extension blocks: hand-derived wire images -------------------
#
# d14 §1.4.2: absolute Type. d16/d18 §1.4.2/§1.4.3: Type is a DELTA
# from the previous Type (unsigned → ascending emission); even/odd
# (value form) follows the ABSOLUTE type. d18 varints are vi64.

_EXTS = {6: 1000, 2: 7, 13: b"ab"}  # deliberately unsorted insertion


def _encode(exts, *, vi64, delta):
    buf = Buffer(capacity=64, vi64=vi64)
    MOQTMessage._extensions_encode(buf, exts, delta=delta)
    return bytes(buf.data_slice(0, buf.tell()))


def _decode(raw, *, vi64, delta):
    buf = Buffer(data=raw, vi64=vi64)
    return MOQTMessage._extensions_decode(buf, delta=delta)


def test_d16_delta_block_exact_bytes():
    # sorted [2, 6, 13] → wire deltas [2, 4, 7]; values RFC9000
    # (1000 → 0x43E8).
    expect = bytes([0x02, 0x07,
                    0x04, 0x43, 0xE8,
                    0x07, 0x02]) + b"ab"
    assert _encode(_EXTS, vi64=False, delta=True) == (
        bytes([len(expect)]) + expect)
    assert _decode(bytes([len(expect)]) + expect,
                   vi64=False, delta=True) == _EXTS


def test_d18_delta_block_exact_bytes():
    # Same deltas, vi64 values: 1000 → 0x83E8 (matches the moq-dev
    # capture's value bytes).
    expect = bytes([0x02, 0x07,
                    0x04, 0x83, 0xE8,
                    0x07, 0x02]) + b"ab"
    assert _encode(_EXTS, vi64=True, delta=True) == (
        bytes([len(expect)]) + expect)
    assert _decode(bytes([len(expect)]) + expect,
                   vi64=True, delta=True) == _EXTS


def test_d14_absolute_block_round_trip():
    raw = _encode(_EXTS, vi64=False, delta=False)
    # absolute ids appear literally on the wire, insertion order
    assert raw[1] == 6 and raw[0] == len(raw) - 1
    assert _decode(raw, vi64=False, delta=False) == _EXTS


def test_delta_parity_follows_absolute_type():
    # {2: v, 5: bytes}: wire deltas [2, 3] — the second delta is ODD
    # while the absolute type 5 is also odd here; use {2, 8, 13} where
    # delta parity differs from absolute parity: deltas [2, 6, 5] —
    # 5 is odd but leads to absolute 13 (odd, bytes) via accumulation,
    # while absolute-8 (even, varint) came from delta 6.
    exts = {2: 1, 8: 2, 13: b"z"}
    raw = _encode(exts, vi64=False, delta=True)
    assert _decode(raw, vi64=False, delta=True) == exts


# -- Cython hot-path twins agree with the python codec ----------------

from aiopquic._binding._streamchain import (          # noqa: E402
    encode_object_subgroup, encode_object_subgroup_vi64)


@pytest.mark.parametrize("vi64", [False, True], ids=["d16", "d18"])
def test_cython_subgroup_object_kvp_delta(vi64):
    encode = encode_object_subgroup_vi64 if vi64 else encode_object_subgroup
    body = encode(0, _EXTS, 0, b"pay", True, True)
    chain = StreamChain()
    chain.extend(body)
    fused = (chain.parse_object_subgroup_vi64 if vi64
             else chain.parse_object_subgroup)
    delta, exts, status, payload = fused(True, 16 * 1024, True)
    assert (delta, exts, status, payload) == (0, _EXTS, 0, b"pay")
    # And the Cython encoder's ext block matches the python encoder's.
    pybuf = Buffer(capacity=64, vi64=vi64)
    pybuf.push_vint(0)
    MOQTMessage._extensions_encode(pybuf, _EXTS, delta=True)
    pyhead = bytes(pybuf.data_slice(0, pybuf.tell()))
    assert body.startswith(pyhead)


# -- the two codecs agree on MALFORMED input too ----------------------
#
# §1.4.3: "Key-Value-Pairs are always parsed with a known byte length,
# which bounds the sequence." A KVP declaring a value longer than its
# block must be refused, not read into the payload behind it. Agreement
# on well-formed blocks (above) does not imply agreement on malformed
# ones, and only the malformed case can desync a stream.


def _vint(vi64):
    return ref_vi64 if vi64 else ref_rfc9000


def _kvp_block(vi64, declared_len, value):
    """Type 1 (odd → length-prefixed) carrying `value`, in a block
    whose declared length is `declared_len`."""
    v = _vint(vi64)
    return v(declared_len) + v(1) + v(len(value)) + value


@pytest.mark.parametrize("vi64", [False, True], ids=["d16", "d18"])
def test_kvp_overrunning_its_block_is_refused_by_both_codecs(vi64):
    v = _vint(vi64)
    # Declared block length 4; the single KVP actually spans 12 bytes.
    block = _kvp_block(vi64, 4, b"\xaa" * 10)

    buf = Buffer(data=block + v(3) + b"pay", vi64=vi64)
    with pytest.raises(RuntimeError, match="overrun"):
        MOQTMessage._extensions_decode(buf, delta=True)

    # The fused parser refuses the declared length before pulling it.
    chain = StreamChain()
    chain.extend(v(0) + block + v(3) + b"pay")
    fused = (chain.parse_object_subgroup_vi64 if vi64
             else chain.parse_object_subgroup)
    with pytest.raises(OverflowError, match="exceeds its block"):
        fused(True, 16 * 1024, True)


@pytest.mark.parametrize("vi64", [False, True], ids=["d16", "d18"])
def test_kvp_exactly_filling_its_block_is_accepted(vi64):
    """Off-by-one guard: a KVP ending exactly at the block end is
    legal, so the overrun check must use > and not >=."""
    v = _vint(vi64)
    # type(1) + len(1) + 2 value bytes == 4 == the declared length.
    block = _kvp_block(vi64, 4, b"ab")
    assert len(block) - len(v(4)) == 4

    buf = Buffer(data=block, vi64=vi64)
    assert MOQTMessage._extensions_decode(buf, delta=True) == {1: b"ab"}

    chain = StreamChain()
    chain.extend(v(0) + block + v(3) + b"pay")
    fused = (chain.parse_object_subgroup_vi64 if vi64
             else chain.parse_object_subgroup)
    assert fused(True, 16 * 1024, True) == (0, {1: b"ab"}, 0, b"pay")


def test_kvp_delta_type_overflow_is_a_protocol_violation():
    """§1.4.3: the previous Type plus the Delta Type MUST NOT exceed
    2^64-1, and a receiver MUST close the session with a
    PROTOCOL_VIOLATION. Python ints do not overflow, so without an
    explicit guard the key simply grows and a nonsense Type is
    accepted. vi64 reaches 2^64-1 in one delta, so two KVPs suffice."""
    from aiomoqt.types import MOQTProtocolViolation
    v = ref_vi64
    # type 1 (odd, empty value), then a delta that lands past the space
    kvps = v(1) + v(0) + v((1 << 64) - 1) + v(0)
    block = v(len(kvps)) + kvps

    buf = Buffer(data=block, vi64=True)
    with pytest.raises(MOQTProtocolViolation, match="Delta Type"):
        MOQTMessage._extensions_decode(buf, delta=True)

    from aiomoqt.messages.data import ObjectHeader
    body = v(0) + block + v(3) + b"pay"
    chain = StreamChain()
    chain.extend(body)
    with pytest.raises(MOQTProtocolViolation, match="overflow"):
        ObjectHeader(object_id=0).deserialize_into(
            chain, buf_len=len(body), extensions_present=True,
            vi64=True, kvp_delta=True)


# -- d18 FETCH data plane (§11.4.4): vi64 + group/object deltas -------

from aiomoqt.messages.data import FetchHeader, FetchObject  # noqa: E402
from aiomoqt.types import ObjectStatus  # noqa: E402


def test_d18_fetch_header_exact_bytes():
    raw = bytes(FetchHeader(request_id=5).serialize(
        profile_for(18)).data)
    assert raw == bytes([0x05, 0x05])
    rb = Buffer(data=raw, vi64=True)
    assert rb.pull_vint() == 0x05
    assert FetchHeader.deserialize(rb).request_id == 5


def _fetch_chain_roundtrip(objs, group_order=0x1):
    prof = profile_for(18)
    prior = None
    out = []
    for o in objs:
        raw = bytes(o.serialize(prof=prof, prior=prior,
                                group_order=group_order).data)
        rb = Buffer(data=raw, vi64=True)
        got = FetchObject.deserialize(rb, prior=prior, prof=prof,
                                      group_order=group_order)
        out.append((raw, got))
        prior = got
    return out


def test_d18_fetch_object_exact_bytes_and_chain():
    objs = [
        FetchObject(group_id=2, subgroup_id=0, object_id=0,
                    publisher_priority=128,
                    extensions={8: 1000}, payload=b"hi"),
        FetchObject(group_id=2, subgroup_id=0, object_id=1,
                    publisher_priority=128, payload=b"x"),
        FetchObject(group_id=3, subgroup_id=0, object_id=0,
                    publisher_priority=128, payload=b"y"),
    ]
    chain = _fetch_chain_roundtrip(objs)
    # First object: flags GD|OD|PRI|PROPS = 0x3C, absolute ids,
    # delta-typed vi64 properties, then len-prefixed payload.
    assert chain[0][0] == bytes.fromhex("3c 02 00 80 03 08 83 e8 02 6869"
                                        .replace(" ", ""))
    # Second: everything inherited from prior — flags 0.
    assert chain[1][0] == bytes.fromhex("000178")
    # Third: next group (delta 0), object absolute again.
    assert chain[2][0] == bytes.fromhex("0c00000179")
    for want, (_, got) in zip(objs, chain):
        assert (got.group_id, got.object_id) == (want.group_id,
                                                 want.object_id)
        assert got.payload == want.payload
    assert chain[0][1].extensions == {8: 1000}


def test_d18_fetch_descending_group_delta():
    objs = [
        FetchObject(group_id=5, object_id=0, publisher_priority=1,
                    payload=b"a"),
        FetchObject(group_id=3, object_id=0, publisher_priority=1,
                    payload=b"b"),
    ]
    chain = _fetch_chain_roundtrip(objs, group_order=0x2)
    # 5 -> 3 descending: wire delta = 5 - 3 - 1 = 1.
    assert chain[1][0][1] == 1
    assert chain[1][1].group_id == 3


def test_d18_fetch_end_of_range():
    prof = profile_for(18)
    marker = FetchObject(group_id=7, object_id=9, end_of_range=0x8C,
                         payload=b"")
    raw = bytes(marker.serialize(prof=prof).data)
    # 0x8C needs the 2-byte vi64 form.
    assert raw == bytes([0x80, 0x8C, 0x07, 0x09])
    rb = Buffer(data=raw, vi64=True)
    got = FetchObject.deserialize(rb, prof=prof)
    assert (got.end_of_range, got.group_id, got.object_id) == (0x8C, 7, 9)


def test_d18_fetch_first_object_must_be_absolute():
    prof = profile_for(18)
    rb = Buffer(data=bytes([0x00, 0x01, 0x78]), vi64=True)
    with pytest.raises(ValueError, match="first object"):
        FetchObject.deserialize(rb, prior=None, prof=prof)


def test_d18_fetch_zero_length_encodes_status():
    # Golden bytes per moxygen's writer (writeStreamObject): a zero
    # Payload Length is followed by an explicit Status varint.
    objs = [
        FetchObject(group_id=1, subgroup_id=0, object_id=0,
                    publisher_priority=200, payload=b"tt"),
        FetchObject(group_id=1, subgroup_id=0, object_id=1,
                    publisher_priority=200, payload=b""),
        FetchObject(group_id=1, subgroup_id=0, object_id=2,
                    publisher_priority=200,
                    status=ObjectStatus.END_OF_GROUP, payload=b""),
    ]
    chain = _fetch_chain_roundtrip(objs)
    # First: flags GD|OD|PRI = 0x1C, absolute ids, len 2 + payload.
    assert chain[0][0] == bytes.fromhex("1c0100c8027474")
    # Zero-length Normal: flags 0, len 0, explicit status 0.
    assert chain[1][0] == bytes.fromhex("000000")
    # End of Group: flags 0, len 0, status 3.
    assert chain[2][0] == bytes.fromhex("000003")
    assert [g.status for _, g in chain] == [
        ObjectStatus.NORMAL, ObjectStatus.NORMAL, ObjectStatus.END_OF_GROUP]
    assert [g.payload for _, g in chain] == [b"tt", b"", b""]


def test_d18_fetch_non_normal_status_guards():
    prof = profile_for(18)
    with pytest.raises(ValueError, match="empty payload"):
        FetchObject(group_id=1, object_id=0,
                    status=ObjectStatus.END_OF_GROUP,
                    payload=b"x").serialize(prof=prof)
    with pytest.raises(ValueError, match="non-Normal"):
        FetchObject(group_id=1, object_id=0,
                    status=ObjectStatus.END_OF_GROUP,
                    extensions={8: 1}, payload=b"").serialize(prof=prof)
    # RX: properties on a non-Normal object close the parse.
    raw = bytes.fromhex("3c0100c802080500 03".replace(" ", ""))
    rb = Buffer(data=raw, vi64=True)
    with pytest.raises(ValueError, match="non-Normal"):
        FetchObject.deserialize(rb, prior=None, prof=prof)


def test_d18_fetch_first_object_sg_prior_rejected():
    prof = profile_for(18)
    # flags GD|OD|PRI|SG=prior (0x1D): prior-Subgroup ref on first object.
    raw = bytes.fromhex("1d0100c80000")
    rb = Buffer(data=raw, vi64=True)
    with pytest.raises(ValueError, match="prior Subgroup"):
        FetchObject.deserialize(rb, prior=None, prof=prof)


def test_d18_fetch_datagram_object_flag():
    # §11.4.4.1: a Datagram object sets 0x40 and has no Subgroup ID; the
    # next object's Subgroup counts on from 0.
    objs = [
        FetchObject(group_id=7, object_id=9, publisher_priority=128,
                    payload=b"d", datagram=True),
        FetchObject(group_id=7, subgroup_id=1, object_id=10,
                    publisher_priority=128, payload=b"s"),
    ]
    chain = _fetch_chain_roundtrip(objs)
    # DGRAM|PRI|GD|OD = 0x5C, group, object, priority, payload.
    assert chain[0][0] == bytes.fromhex("5c0709800164")
    # Subgroup prior + 1 (0x02); object and priority inherited.
    assert chain[1][0] == bytes.fromhex("020173")
    assert chain[0][1].datagram and not chain[1][1].datagram
    assert chain[1][1].subgroup_id == 1


def test_d18_fetch_datagram_flag_ignores_subgroup_bits():
    # 0x40 with Subgroup bits 0b11: the bits are ignored, no field is read.
    rb = Buffer(data=bytes.fromhex("5f0709800164"), vi64=True)
    got = FetchObject.deserialize(rb, prof=profile_for(18))
    assert got.datagram and got.subgroup_id == 0
    assert (got.group_id, got.object_id, got.payload) == (7, 9, b"d")


def test_d16_fetch_datagram_object_flag():
    prof = profile_for(16)
    raw = bytes(FetchObject(group_id=7, object_id=9, publisher_priority=128,
                            payload=b"d", datagram=True)
                .serialize(prof=prof).data)
    # Flags 0x5C as a 2-byte RFC 9000 varint; no Subgroup ID field.
    assert raw == bytes.fromhex("405c0709800164")
    got = FetchObject.deserialize(Buffer(data=raw), prof=prof)
    assert got.datagram and (got.group_id, got.object_id) == (7, 9)


def test_d18_fetch_unknown_type_is_a_protocol_violation():
    # §10.12: Request ID 1, Fetch Type 0, no parameters.
    from aiomoqt.messages.fetch import Fetch
    from aiomoqt.types import MOQTProtocolViolation
    body = bytes([0x01, 0x00, 0x00])
    with pytest.raises(MOQTProtocolViolation):
        Fetch.deserialize(Buffer(data=body, vi64=True), prof=profile_for(18),
                          buf_end=len(body))


# -- d18 SUBSCRIPTION_FILTER internals (§5.1.2) -----------------------
#
# Filter values follow the negotiated varint codec (vi64 on d18 —
# cross-checked against moxygen MoQFramer.writeSubscriptionFilter,
# whose version-aware writeVarint uses the MoQ varint on d17+), and
# d18 carries End Group as end - start (moxygen: error when negative;
# parse reconstructs the absolute).

from aiomoqt.messages.subscribe import Subscribe  # noqa: E402


def test_d18_filter_absolute_start_uses_vi64():
    # AbsoluteStart (3) with group/object >= 64: vi64 encodes 100 as
    # one byte (0x64), RFC9000 as two (0x4064) — the wire images differ.
    p18 = profile_for(18)
    m = Subscribe(request_id=0, track_namespace=(b"n",), track_name=b"t",
                  filter_type=3, start_group=100, start_object=200)
    raw18 = bytes(m.serialize(prof=p18).data)
    assert bytes([0x03, 0x64, 0x80, 0xC8]) in raw18  # vi64 100, 200
    assert bytes([0x03, 0x40, 0x64]) not in raw18    # RFC9000 100
    got = Subscribe.deserialize(
        Buffer(data=raw18[_subscribe_body_off(raw18)::], vi64=True),
        prof=p18, buf_end=len(raw18) - _subscribe_body_off(raw18))
    assert (got.filter_type, got.start_group, got.start_object) == (
        3, 100, 200)


def test_d18_filter_absolute_range_end_group_delta():
    p18 = profile_for(18)
    m = Subscribe(request_id=0, track_namespace=(b"n",), track_name=b"t",
                  filter_type=4, start_group=100, start_object=0,
                  end_group=110)
    raw18 = bytes(m.serialize(prof=p18).data)
    # type 4, start 100/0, then END GROUP AS DELTA: 110-100 = 10 (0x0A)
    assert bytes([0x04, 0x64, 0x00, 0x0A]) in raw18
    got = Subscribe.deserialize(
        Buffer(data=raw18[_subscribe_body_off(raw18):], vi64=True),
        prof=p18, buf_end=len(raw18) - _subscribe_body_off(raw18))
    assert got.end_group == 110  # absolute reconstructed
    with pytest.raises(ValueError, match="end_group"):
        Subscribe(request_id=0, track_namespace=(b"n",), track_name=b"t",
                  filter_type=4, start_group=100,
                  end_group=90).serialize(prof=p18)


def test_d16_filter_unchanged_rfc9000_absolute():
    p16 = profile_for(16)
    m = Subscribe(request_id=0, track_namespace=(b"n",), track_name=b"t",
                  filter_type=4, start_group=100, start_object=200,
                  end_group=110)
    raw16 = bytes(m.serialize(prof=p16).data)
    # RFC9000 two-byte ints, end group ABSOLUTE: 0x406E = 110.
    assert bytes([0x04, 0x40, 0x64, 0x40, 0xC8, 0x40, 0x6E]) in raw16
    got = Subscribe.deserialize(
        Buffer(data=raw16[_subscribe_body_off(raw16):]),
        prof=p16, buf_end=len(raw16) - _subscribe_body_off(raw16))
    assert (got.start_group, got.end_group) == (100, 110)


def _subscribe_body_off(raw: bytes) -> int:
    # Control frame: type varint (1B here) + u16 length prefix.
    return 3


# -- golden capture: moq-dev SUBSCRIBE_OK (d18) -----------------------

# Full control message: type=0x04, len=0x0007, body 00 01 22 02 08 83 e8.
# Track Properties carry TIMESCALE(0x08) = 1000.
_MOQ_DEV_SUBSCRIBE_OK_BODY = bytes.fromhex("000122020883e8")


def test_moq_dev_subscribe_ok_golden():
    buf = Buffer(data=_MOQ_DEV_SUBSCRIBE_OK_BODY, vi64=True)
    msg = SubscribeOk.deserialize(
        buf, prof=profile_for(18),
        buf_end=len(_MOQ_DEV_SUBSCRIBE_OK_BODY))
    assert msg.track_alias == 0
    assert msg.track_extensions == {8: 1000}


# -- subgroup header FIRST_OBJECT (d18 §2.2, §11.4.2) -----------------

from aiomoqt.messages.data import SubgroupHeader  # noqa: E402


def _subgroup_type(draft, **kw):
    hdr = SubgroupHeader(track_alias=1, group_id=0, subgroup_id=0,
                         prof=profile_for(draft), **kw)
    buf = Buffer(data=hdr.serialize().data, vi64=profile_for(draft).vi64)
    return buf.pull_uint_vi64() if profile_for(draft).vi64 \
        else buf.pull_uint_var()


def test_d18_new_subgroup_sets_first_object():
    # An original publisher's new subgroup MUST set FIRST_OBJECT.
    assert _subgroup_type(18) & 0x40


@pytest.mark.parametrize("draft", [14, 16])
def test_first_object_is_never_written_before_d18(draft):
    # 0x40 is not a subgroup type bit before d18.
    assert not _subgroup_type(draft) & 0x40


def test_relay_forwarding_mid_subgroup_clears_first_object():
    assert not _subgroup_type(18, first_object=False) & 0x40


def test_d18_first_object_round_trips():
    prof = profile_for(18)
    hdr = SubgroupHeader(track_alias=1, group_id=0, subgroup_id=0,
                         prof=prof)
    buf = Buffer(data=hdr.serialize().data, vi64=True)
    type_val = buf.pull_uint_vi64()
    assert SubgroupHeader.deserialize(buf, type_val, prof=prof).first_object


# -- malformed requests the session MUST close on (§2.4.1, §5.1.2, §10.2) --

from aiomoqt.types import GroupOrder, MOQTProtocolViolation  # noqa: E402


def _ref_vint(draft, v):
    return ref_vi64(v) if draft >= 18 else ref_rfc9000(v)


def _subscribe_body(draft, *, ns=(b"n",), name=b"x", params=b"\x00"):
    # Request ID 1, Track Namespace, Track Name, then the parameter block.
    out = _ref_vint(draft, 1) + _ref_vint(draft, len(ns))
    for f in ns:
        out += _ref_vint(draft, len(f)) + f
    return out + _ref_vint(draft, len(name)) + name + params


def _filter_param(draft, *fields):
    # One SUBSCRIPTION_FILTER (0x21) parameter, length-prefixed.
    inner = b"".join(_ref_vint(draft, f) for f in fields)
    return (_ref_vint(draft, 1) + _ref_vint(draft, 0x21)
            + _ref_vint(draft, len(inner)) + inner)


def _decode_subscribe(draft, body):
    prof = profile_for(draft)
    return Subscribe.deserialize(Buffer(data=body, vi64=prof.vi64),
                                 prof=prof, buf_end=len(body))


@pytest.mark.parametrize("draft", [16, 18])
@pytest.mark.parametrize("params", [
    b"\x01\x10\x02",   # FORWARD 2
    b"\x01\x22\x00",   # GROUP_ORDER 0
    b"\x01\x22\x03",   # GROUP_ORDER 3
], ids=["forward-2", "group-order-0", "group-order-3"])
def test_out_of_range_parameter_is_a_protocol_violation(draft, params):
    with pytest.raises(MOQTProtocolViolation, match="outside"):
        _decode_subscribe(draft, _subscribe_body(draft, params=params))


@pytest.mark.parametrize("draft", [16, 18])
def test_in_range_parameters_are_accepted(draft):
    params = b"\x03\x10\x01\x10\xff\x02\x02"  # FORWARD 1, PRIORITY 255, ORDER 2
    if draft < 18:
        params = b"\x03\x10\x01\x10\x40\xff\x02\x02"  # 255 as a varint
    got = _decode_subscribe(draft, _subscribe_body(draft, params=params))
    assert (got.forward, got.priority, got.group_order) == (1, 255, 2)


def test_d16_subscriber_priority_over_255_is_a_protocol_violation():
    params = b"\x01\x20\x41\x00"  # SUBSCRIBER_PRIORITY 256
    with pytest.raises(MOQTProtocolViolation, match="outside"):
        _decode_subscribe(16, _subscribe_body(16, params=params))


@pytest.mark.parametrize("draft", [16, 18])
def test_empty_namespace_field_is_a_protocol_violation(draft):
    with pytest.raises(MOQTProtocolViolation, match="empty namespace field"):
        _decode_subscribe(draft, _subscribe_body(draft, ns=(b"n", b"")))


def test_empty_namespace_field_is_legal_at_d14():
    got = MOQTMessage._pull_tuple(Buffer(data=b"\x01\x00"),
                                  prof=profile_for(14))
    assert got == (b"",)


@pytest.mark.parametrize("draft", [16, 18])
def test_full_track_name_is_at_most_4096_bytes(draft):
    at_limit = _subscribe_body(draft, ns=(b"n" * 4095,), name=b"x")
    assert _decode_subscribe(draft, at_limit).track_name == b"x"
    with pytest.raises(MOQTProtocolViolation, match="namespace is 4097"):
        _decode_subscribe(draft, _subscribe_body(draft, ns=(b"n" * 4097,)))
    with pytest.raises(MOQTProtocolViolation, match="full track name is 4097"):
        _decode_subscribe(draft, _subscribe_body(draft, name=b"x" * 4096))


def test_d18_absolute_range_end_past_2_64_is_a_protocol_violation():
    top = (1 << 64) - 1
    at_limit = _subscribe_body(18, params=_filter_param(18, 4, top - 1, 0, 1))
    assert _decode_subscribe(18, at_limit).end_group == top
    past = _subscribe_body(18, params=_filter_param(18, 4, top, 0, 1))
    with pytest.raises(MOQTProtocolViolation, match="2\\^64-1"):
        _decode_subscribe(18, past)


@pytest.mark.parametrize("draft", [16, 18])
@pytest.mark.parametrize("value", [0, 3])
def test_default_publisher_group_order_out_of_range(draft, value):
    # SUBSCRIBE_OK: [Request ID], Track Alias, no parameters, then Track
    # Properties carrying DEFAULT_PUBLISHER_GROUP_ORDER (0x22).
    prof = profile_for(draft)
    head = b"" if draft >= 18 else _ref_vint(draft, 1)
    body = (head + _ref_vint(draft, 0) + _ref_vint(draft, 0)
            + _ref_vint(draft, 0x22) + _ref_vint(draft, value))
    with pytest.raises(MOQTProtocolViolation, match="outside"):
        SubscribeOk.deserialize(Buffer(data=body, vi64=prof.vi64),
                                prof=prof, buf_end=len(body))


@pytest.mark.parametrize("draft", [16, 18])
def test_publisher_default_group_order_is_omitted(draft):
    # d16+ GROUP_ORDER has no 0 value; omission means the publisher's.
    # Request ID 128 encodes differently per draft (40 80 / 80 80).
    prof = profile_for(draft)
    raw = bytes(Subscribe(request_id=128, track_namespace=(b"n",),
                          track_name=b"t", filter_type=2,
                          group_order=GroupOrder.PUBLISHER_DEFAULT,
                          ).serialize(prof=prof).data)
    # Request ID, namespace (n), name (t), then one parameter:
    # SUBSCRIPTION_FILTER (0x21) holding LatestObject (2). No 0x22.
    body = (_ref_vint(draft, 128) + b"\x01\x01n\x01t"
            + b"\x01\x21\x01\x02")
    assert raw == b"\x03" + len(body).to_bytes(2, "big") + body
    off = _subscribe_body_off(raw)
    got = Subscribe.deserialize(Buffer(data=raw[off:], vi64=prof.vi64),
                                prof=prof, buf_end=len(raw) - off)
    assert got.group_order is None


from aiomoqt.messages.fetch import Fetch, FetchOk  # noqa: E402
from aiomoqt.messages.namespace import PublishBlocked  # noqa: E402
from aiomoqt.messages.request import RequestError  # noqa: E402


@pytest.mark.parametrize("draft", [16, 18])
def test_fetch_without_group_order_is_ascending(draft):
    # §10.2.8: omitted from FETCH, the receiver uses Ascending.
    # Standalone FETCH of n/t, locations 0/0..0/0, no parameters.
    prof = profile_for(draft)
    body = (_ref_vint(draft, 2) + b"\x01" + b"\x01\x01n\x01t"
            + b"\x00\x00\x00\x00" + b"\x00")
    got = Fetch.deserialize(Buffer(data=body, vi64=prof.vi64), prof=prof,
                            buf_end=len(body))
    assert got.group_order == GroupOrder.ASCENDING


@pytest.mark.parametrize("draft", [16, 18])
def test_fetch_ok_without_group_order_leaves_the_requested_order(draft):
    # [Request ID], End Of Track 0, Largest Location 5/3, no parameters.
    prof = profile_for(draft)
    head = b"" if draft >= 18 else _ref_vint(draft, 7)
    body = head + b"\x00\x05\x03\x00"
    got = FetchOk.deserialize(Buffer(data=body, vi64=prof.vi64), prof=prof,
                              buf_end=len(body))
    assert got.group_order is None


def test_d18_publish_blocked_full_track_name_is_at_most_4096_bytes():
    prof = profile_for(18)
    ok = (b"\x01" + ref_vi64(4095) + b"n" * 4095 + b"\x01t")
    assert PublishBlocked.deserialize(
        Buffer(data=ok, vi64=True), prof=prof).track_name == b"t"
    past = b"\x01\x01n" + ref_vi64(4096) + b"t" * 4096
    with pytest.raises(MOQTProtocolViolation, match="full track name"):
        PublishBlocked.deserialize(Buffer(data=past, vi64=True), prof=prof)


def test_d18_redirect_full_track_name_is_at_most_4096_bytes():
    # REQUEST_ERROR REDIRECT (0x34): retry 0, empty reason, empty URI,
    # then namespace n and a 4096-byte name.
    prof = profile_for(18)
    body = (ref_vi64(0x34) + b"\x00\x00\x00" + b"\x01\x01n"
            + ref_vi64(4096) + b"t" * 4096)
    with pytest.raises(MOQTProtocolViolation, match="full track name"):
        RequestError.deserialize(Buffer(data=body, vi64=True), prof=prof,
                                 buf_end=len(body))


def test_d18_namespace_parameter_field_past_its_value_is_refused():
    # One TRACK_NAMESPACE_PREFIX (0x34) parameter, length 4, whose field
    # claims 5 bytes where the value holds 2.
    prof = profile_for(18)
    block = b"\x01" + ref_vi64(0x34) + b"\x04\x01\x05ab"
    with pytest.raises(MOQTProtocolViolation, match="overruns frame"):
        MOQTMessage._deserialize_params(
            Buffer(data=block + b"xyz", vi64=True), prof=prof,
            buf_end=len(block))


def test_d18_namespace_parameter_is_length_prefixed():
    # §10.2.14 / §10.2: the Track Namespace rides a length prefix (moxygen
    # and mondain's runner agree): length 6, 2 fields "a" and "bc".
    prof = profile_for(18)
    block = b"\x01" + ref_vi64(0x34) + b"\x06\x02\x01a\x02bc"
    buf = Buffer(capacity=64, vi64=True)
    MOQTMessage._serialize_params(buf, {0x34: (b"a", b"bc")}, prof=prof)
    assert bytes(buf.data_slice(0, buf.tell())) == block
    got = MOQTMessage._deserialize_params(
        Buffer(data=block, vi64=True), prof=prof, buf_end=len(block))
    assert got == {0x34: (b"a", b"bc")}


def test_d18_namespace_parameter_short_of_its_length_is_refused():
    prof = profile_for(18)
    block = b"\x01" + ref_vi64(0x34) + b"\x07\x02\x01a\x02bcz"
    with pytest.raises(MOQTProtocolViolation, match="fill its length"):
        MOQTMessage._deserialize_params(
            Buffer(data=block, vi64=True), prof=prof, buf_end=len(block))


@pytest.mark.parametrize("draft", [16, 18])
def test_track_status_carries_no_delivery_parameters(draft):
    # §10.14: priority, group order, forward and filter are not included.
    from aiomoqt.messages.subscribe import TrackStatus
    msg = TrackStatus(request_id=1, track_namespace=(b"n",), track_name=b"t",
                      priority=128, group_order=1, forward=1, filter_type=2)
    raw = bytes(msg.serialize(prof=profile_for(draft)).data)
    # Type 0x0D, Length 7, Request ID, namespace "n", name "t", no params.
    assert raw == bytes.fromhex("0d0007" "01" "01016e" "0174" "00")


def _params_body(params) -> bytes:
    buf = Buffer(capacity=64, vi64=True)
    MOQTMessage._serialize_params(buf, params, prof=profile_for(18))
    return bytes(buf.data_slice(0, buf.tell()))


@pytest.mark.parametrize("scope, params", [
    ("Subscribe", {0x08: 5}),              # EXPIRES
    ("TrackStatus", {0x20: 7}),            # SUBSCRIBER_PRIORITY
    ("Fetch", {0x10: 1}),                  # FORWARD
    ("SubscribeNamespace", {0x10: 1}),
    ("RequestUpdate", {0x09: (1, 2)}),     # LARGEST_OBJECT
], ids=["subscribe-expires", "track-status-priority", "fetch-forward",
        "subscribe-namespace-forward", "update-largest"])
def test_d18_a_parameter_outside_its_requests_is_refused(scope, params):
    # §10.2.1: a Message Parameter on a message it is not defined for.
    body = _params_body(params)
    with pytest.raises(MOQTProtocolViolation, match="not allowed"):
        MOQTMessage._deserialize_params(
            Buffer(data=body, vi64=True), prof=profile_for(18),
            buf_end=len(body), scope=scope)


def test_d18_parameters_in_scope_pass_and_d16_is_not_checked():
    body = _params_body({0x03: b"t", 0x10: 1, 0x20: 7, 0x21: b"\x02"})
    MOQTMessage._deserialize_params(Buffer(data=body, vi64=True),
                                    prof=profile_for(18), buf_end=len(body),
                                    scope="Subscribe")
    d16 = Buffer(capacity=64)
    MOQTMessage._serialize_params(d16, {0x08: 5}, prof=profile_for(16))
    raw = bytes(d16.data_slice(0, d16.tell()))
    MOQTMessage._deserialize_params(Buffer(data=raw), prof=profile_for(16),
                                    buf_end=len(raw), scope="Subscribe")
