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


# -- d20/d22 SETUP (d20 §10.3, d22 §9.1) -----------------------------
#
# Type 0x2F00 (vi64), Length (16), then Setup Options as delta-coded
# Key-Value-Pairs (§1.4.3) up to the Length: odd Type = Length + bytes,
# even Type = one vi64. Unknown options MUST be ignored.

from aiomoqt.messages.d20 import PublishStateNotify, Setup as D20Setup  # noqa: E402

_D20_DRAFTS = [20, 22]


def _ref_kvps(items, vint):
    """Reference §1.4.3 encoder: (type, value) pairs in wire order."""
    out, prev = b"", 0
    for key, value in items:
        out += vint(key - prev)
        prev = key
        out += vint(len(value)) + value if key % 2 else vint(value)
    return out


def _ref_setup(options_wire: bytes, length=None) -> bytes:
    n = len(options_wire) if length is None else length
    return ref_vi64(0x2F00) + n.to_bytes(2, "big") + options_wire


def _parse_setup(raw: bytes, draft: int):
    buf = Buffer(data=raw, vi64=True)
    assert buf.pull_uint_vi64() == 0x2F00
    end = buf.tell() + buf.pull_uint16() + 2
    return D20Setup.deserialize(buf, prof=profile_for(draft), buf_end=end)


@pytest.mark.parametrize("draft", _D20_DRAFTS)
def test_d20_setup_exact_bytes_and_round_trip(draft):
    opts = {0x07: b"aiomoqt/x", 0x01: b"/moq", 0x05: b"relay:443",
            0x08: 4, 0x06: 200, 0x04: 1 << 20}
    expect = _ref_setup(_ref_kvps(sorted(opts.items()), ref_vi64))
    raw = bytes(D20Setup(options=opts).serialize(prof=profile_for(draft)).data)
    assert raw == expect
    out = _parse_setup(raw, draft)
    assert out.options == opts and out.ignored == []


def test_d20_setup_bytes_match_d18_for_shared_options():
    from aiomoqt.messages.d18 import Setup as D18Setup
    opts = {0x01: b"/", 0x05: b"h:1", 0x07: b"x"}
    assert bytes(D20Setup(options=opts).serialize(prof=profile_for(20)).data) \
        == bytes(D18Setup(options=opts).serialize(prof=profile_for(18)).data)


@pytest.mark.parametrize("draft", _D20_DRAFTS)
def test_d20_setup_ignores_unknown_options(draft):
    # 0x00 and 0x02 (d16 MAX_REQUEST_ID) even, 0x09 odd, 0x9D grease twice
    # (duplicates of unknown options MUST be allowed); the known options
    # around them still parse.
    wire = _ref_kvps([(0x00, 7), (0x02, 100), (0x07, b"peer/1"),
                      (0x08, 3), (0x09, b"\xff" * 300), (0x9D, b""),
                      (0x9D, b"grease")], ref_vi64)
    out = _parse_setup(_ref_setup(wire), draft)
    assert out.options == {0x07: b"peer/1", 0x08: 3}
    assert out.ignored == [0x00, 0x02, 0x09, 0x9D, 0x9D]


def test_d20_setup_empty_is_valid():
    out = _parse_setup(_ref_setup(b""), 20)
    assert out.options == {} and out.ignored == []


@pytest.mark.parametrize("draft", _D20_DRAFTS)
def test_d20_setup_truncated_is_a_protocol_violation(draft):
    # Length claims the whole option; the bytes stop inside its value.
    wire = _ref_kvps([(0x07, b"aiomoqt/x")], ref_vi64)
    raw = _ref_setup(wire)[:-3]
    with pytest.raises(MOQTProtocolViolation, match="truncated"):
        _parse_setup(raw, draft)


@pytest.mark.parametrize("cut", [1, 4])
def test_d20_setup_length_short_of_its_options_is_a_protocol_violation(cut):
    # The Length ends inside an option: it would read the next message.
    wire = _ref_kvps([(0x01, b"/"), (0x07, b"aiomoqt/x")], ref_vi64)
    raw = _ref_setup(wire, length=len(wire) - cut) + b"\x00" * 8
    with pytest.raises(MOQTProtocolViolation, match="past"):
        _parse_setup(raw, 20)


def test_d20_setup_length_past_its_options_is_a_protocol_violation():
    # Length covers a dangling option Type with no Value.
    wire = _ref_kvps([(0x01, b"/")], ref_vi64) + ref_vi64(0x06)
    with pytest.raises(MOQTProtocolViolation):
        _parse_setup(_ref_setup(wire), 20)


def test_d20_setup_option_value_over_65535_is_a_protocol_violation():
    # §1.4.3: a Length above 2^16-1 MUST close the session.
    wire = ref_vi64(0x07) + ref_vi64(0x10000)
    with pytest.raises(MOQTProtocolViolation, match="exceeds"):
        _parse_setup(_ref_setup(wire), 20)


def test_d20_setup_option_longer_than_the_message_is_a_protocol_violation():
    wire = ref_vi64(0x07) + ref_vi64(40) + b"short"
    with pytest.raises(MOQTProtocolViolation, match="past the SETUP"):
        _parse_setup(_ref_setup(wire), 20)


def test_d20_setup_option_type_overflow_is_a_protocol_violation():
    wire = ref_vi64(0x08) + ref_vi64(1) + ref_vi64((1 << 64) - 1) \
        + ref_vi64(0)
    with pytest.raises(MOQTProtocolViolation, match="overflows"):
        _parse_setup(_ref_setup(wire), 20)


def test_d20_setup_repeated_known_option_is_a_protocol_violation():
    wire = _ref_kvps([(0x08, 1), (0x08, 2)], ref_vi64)
    with pytest.raises(MOQTProtocolViolation, match="repeated"):
        _parse_setup(_ref_setup(wire), 20)


def test_d20_setup_may_repeat_the_authorization_token():
    # §10.3.1.4: one or more tokens. USE_VALUE (0x3), type 0, value.
    tok = lambda v: ref_vi64(3) + ref_vi64(0) + v  # noqa: E731
    wire = _ref_kvps([(0x03, tok(b"a")), (0x03, tok(b"b"))], ref_vi64)
    out = _parse_setup(_ref_setup(wire), 20)
    assert out.options[0x03] == b"a"


def test_d20_setup_refuses_to_serialize_what_the_wire_cannot_carry():
    prof = profile_for(20)
    with pytest.raises(ValueError):
        D20Setup(options={0x07: b"x" * 0x10000}).serialize(prof=prof)
    with pytest.raises(ValueError):
        D20Setup(options={0x09: b"x" * 40000, 0x07: b"y" * 40000}) \
            .serialize(prof=prof)
    with pytest.raises(TypeError):
        D20Setup(options={0x08: b"4"}).serialize(prof=prof)
    with pytest.raises(TypeError):
        D20Setup(options={0x07: 4}).serialize(prof=prof)


def test_d20_setup_length_mismatch_closes_the_session():
    """Through the control dispatch: a SETUP whose Length ends inside an
    option closes the session with PROTOCOL_VIOLATION."""
    from aiomoqt.protocol import MOQTSessionQuic
    from aiomoqt.types import SessionCloseCode
    assert MOQTSessionQuic.CONTROL_REGISTRY[20][0x2F00][0] is D20Setup
    s = object.__new__(MOQTSessionQuic)
    s.negotiated_draft = 20
    s._control_msg_overrides = {}
    s._control_chains = {}
    s._bidi_stream_requests = {}
    s._d18_control_read_sid = 3
    s._closed = []
    s._close_session = lambda code, reason: s._closed.append((code, reason))
    wire = _ref_kvps([(0x07, b"aiomoqt/x")], ref_vi64)
    s._on_control_data(3, _ref_setup(wire, length=len(wire) - 2)
                       + b"\x00" * 8, False)
    assert s._closed and s._closed[0][0] == SessionCloseCode.PROTOCOL_VIOLATION


@pytest.mark.parametrize("draft", _D20_DRAFTS)
def test_d20_publish_state_notify_exact_bytes(draft):
    # Type 0x22, Length, Number of Parameters, LARGEST_OBJECT {5, 300}.
    prof = profile_for(draft)
    raw = bytes(PublishStateNotify(largest_group_id=5, largest_object_id=300)
                .serialize(prof=prof).data)
    body = ref_vi64(1) + ref_vi64(0x09) + ref_vi64(5) + ref_vi64(300)
    assert raw == ref_vi64(0x22) + len(body).to_bytes(2, "big") + body
    buf = Buffer(data=raw, vi64=True)
    buf.pull_vint()
    end = buf.tell() + buf.pull_uint16() + 2
    out = PublishStateNotify.deserialize(buf, prof=prof, buf_end=end)
    assert (out.largest_group_id, out.largest_object_id) == (5, 300)
    assert out.parameters == {}


# -- d14/d16/d18 bytes are unchanged by d20 --------------------------
#
# Golden images of the setup messages and of the two types d18 renumbers
# on the wire, as the tree before d20 support encoded them.

def test_d14_d16_d18_setup_bytes_unchanged():
    from aiomoqt.messages.d18 import Setup as D18Setup
    from aiomoqt.messages.session_setup import ClientSetup, ServerSetup

    def hx(msg, draft):
        return bytes(msg.serialize(prof=profile_for(draft)).data).hex()

    assert hx(ClientSetup(versions=[0xff00000e],
                          parameters={1: b"/", 2: 100}), 14) \
        == "20001001c0000000ff00000e0201012f024064"
    assert hx(ServerSetup(selected_version=0xff00000e,
                          parameters={2: 100}), 14) \
        == "21000cc0000000ff00000e01024064"
    assert hx(ClientSetup(versions=[],
                          parameters={7: b"x", 1: b"/", 2: 100}), 16) \
        == "20000a0301012f014064050178"
    assert hx(ServerSetup(parameters={2: 100}), 16) == "21000401024064"
    assert hx(D18Setup(options={7: b"x", 1: b"/", 5: b"h:1"}), 18) \
        == "af00000b01012f0403683a31020178"


def test_d14_d16_d18_renumbered_type_bytes_unchanged():
    from aiomoqt.messages.namespace import SubscribeNamespace
    from aiomoqt.messages.publish import PublishOk

    def hx(msg, draft):
        return bytes(msg.serialize(prof=profile_for(draft)).data).hex()

    def sub_ns():
        return SubscribeNamespace(request_id=2, namespace_prefix=(b"n",),
                                  parameters={})

    def pub_ok():
        return PublishOk(request_id=2, forward=1, priority=128,
                         group_order=1, filter_type=2)

    assert hx(sub_ns(), 14) == "1100050201016e00"
    assert hx(sub_ns(), 16) == "1100060201016e0000"
    assert hx(sub_ns(), 18) == "5000050201016e00"
    assert hx(pub_ok(), 16) == "1e000c020410011040800101020101"
    assert hx(pub_ok(), 18) == "07000a04100110800101020101"
    # d20 PUBLISH_OK is a REQUEST_OK with EXPIRES at most.
    assert hx(PublishOk(request_id=2), 20) == "07000100"


# -- d20/d22 LOCATION_FILTER (0x21) against the spec text --------------
#
# d20 §5.1.2: Length, then 0-4 vi64 fields whose count selects the
# meaning; two zeros are Next Object. d22 §9.20.9: a Location Filter
# Type 0x00-0x05 selects the fields; nothing is length-prefixed. For one
# byte per field, Length equals the field count equals the d22 type, so
# only Next Object, an absolute {0, 0} start and values >= 128 tell the
# drafts apart.

from aiomoqt.messages.params import (  # noqa: E402
    LocationFilter, LocationFilterType as LFT, RangeFilter)

_prof = profile_for


def ref_lf_d20(*fields):
    inner = b"".join(ref_vi64(f) for f in fields)
    return ref_vi64(len(inner)) + inner


def ref_lf_d22(ftype, *fields):
    return ref_vi64(ftype) + b"".join(ref_vi64(f) for f in fields)


def _lf_encode(lf, draft):
    buf = Buffer(capacity=64, vi64=True)
    lf.encode(buf, prof=_prof(draft))
    return bytes(buf.data_slice(0, buf.tell()))


def _lf_decode(raw, draft):
    buf = Buffer(data=raw, vi64=True)
    lf = LocationFilter.decode(buf, prof=_prof(draft), buf_end=len(raw))
    assert buf.tell() == len(raw)
    return lf


# (filter, d20 bytes, d22 bytes): every form, the separating cases first.
_LF_VECTORS = [
    (LocationFilter.next_object(), ref_lf_d20(0, 0), ref_lf_d22(5)),
    (LocationFilter.absolute(200, 0), ref_lf_d20(200, 0),
     ref_lf_d22(2, 200, 0)),
    (LocationFilter.relative(128), ref_lf_d20(128), ref_lf_d22(1, 128)),
    (LocationFilter.absolute(7, 300, 9, 1000),
     ref_lf_d20(7, 300, 2, 1000), ref_lf_d22(4, 7, 300, 2, 1000)),
    (LocationFilter.none(), ref_lf_d20(), ref_lf_d22(0)),
    (LocationFilter.relative(0), ref_lf_d20(0), ref_lf_d22(1, 0)),
    (LocationFilter.relative(1), ref_lf_d20(1), ref_lf_d22(1, 1)),
    (LocationFilter.absolute(7, 3), ref_lf_d20(7, 3), ref_lf_d22(2, 7, 3)),
    (LocationFilter.absolute(7, 3, 9), ref_lf_d20(7, 3, 2),
     ref_lf_d22(3, 7, 3, 2)),
    (LocationFilter.absolute(7, 3, 9, 4), ref_lf_d20(7, 3, 2, 4),
     ref_lf_d22(4, 7, 3, 2, 4)),
]


@pytest.mark.parametrize("lf, d20, d22", _LF_VECTORS,
                         ids=[v[0].type.name + str(i)
                              for i, v in enumerate(_LF_VECTORS)])
def test_location_filter_matches_reference(lf, d20, d22):
    assert _lf_encode(lf, 20) == d20
    assert _lf_decode(d20, 20) == lf
    assert _lf_encode(lf, 22) == d22
    assert _lf_decode(d22, 22) == lf


def test_d20_and_d22_disagree_where_the_spec_says():
    # Next Object: two zero fields at d20, its own type at d22.
    assert _lf_encode(LocationFilter.next_object(), 20) == bytes.fromhex("020000")
    assert _lf_encode(LocationFilter.next_object(), 22) == bytes.fromhex("05")
    # The same three bytes are Next Object at d20 and {0, 0} at d22.
    raw = bytes.fromhex("020000")
    assert _lf_decode(raw, 20).type == LFT.NEXT_OBJECT
    assert _lf_decode(raw, 22) == LocationFilter.absolute(0, 0)
    # A two-byte field: d20 counts bytes (3), d22 names the form (2).
    assert _lf_encode(LocationFilter.absolute(200, 1), 20) == \
        bytes.fromhex("0380c801")
    assert _lf_encode(LocationFilter.absolute(200, 1), 22) == \
        bytes.fromhex("0280c801")
    # Small values encode the same in both.
    for lf in (LocationFilter.relative(1), LocationFilter.absolute(7, 3),
               LocationFilter.absolute(7, 3, 9),
               LocationFilter.absolute(7, 3, 9, 4)):
        assert _lf_encode(lf, 20) == _lf_encode(lf, 22)


def test_d20_absolute_origin_is_the_unfiltered_form():
    # §5.1.2: two zero fields would be Next Object; an open filter from
    # {0, 0} equals no filter and travels as one.
    assert _lf_encode(LocationFilter.absolute(0, 0), 20) == b"\x00"
    assert _lf_decode(b"\x00", 20).type == LFT.NONE


@pytest.mark.parametrize("draft, raw, match", [
    (20, ref_lf_d20(1, 2, 3, 4, 5), "more than four"),
    (20, bytes.fromhex("0180c8"), "crosses its length"),   # 2-byte vi64, length 1
    (20, bytes.fromhex("0201"), "overruns"),
    (20, ref_lf_d20((1 << 64) - 1, 0, 1), "exceeds 2\\^64-1"),
    (22, ref_lf_d22(6), "Location Filter Type 0x6"),
    (22, ref_lf_d22(0x80), "Location Filter Type 0x80"),
    (22, ref_lf_d22(3, 7, 3), "truncated"),
    (22, ref_lf_d22(4, (1 << 64) - 1, 0, 1, 0), "exceeds 2\\^64-1"),
], ids=["d20-five-fields", "d20-field-past-length", "d20-length-past-msg",
        "d20-end-overflow", "d22-type-6", "d22-type-128", "d22-short",
        "d22-end-overflow"])
def test_malformed_location_filter_is_a_protocol_violation(draft, raw, match):
    with pytest.raises(MOQTProtocolViolation, match=match):
        _lf_decode(raw, draft)


def test_d20_filter_read_as_d22_desyncs():
    # A d20 Absolute Start {200, 0} (Length 3) read at d22 is Type 3 and
    # takes the next parameter's bytes as End Group Delta.
    raw = ref_lf_d20(200, 0) + b"\x01"
    got = _lf_decode(raw, 22)
    assert got == LocationFilter.absolute(200, 0, 201)


# -- d20 Range Filters (§5.1.4) and FILL_PARAMETERS (§10.2.15) ----------

def _rf_encode(rf, ptype, draft=20):
    buf = Buffer(capacity=64, vi64=True)
    rf.encode(buf, ptype, prof=_prof(draft))
    return bytes(buf.data_slice(0, buf.tell()))


def test_range_filter_matches_spec_example():
    # §5.1.4: ranges 3-5 and 10-15 are Start=3, End=2, Start=5, End=5.
    rf = RangeFilter(set_id=4, ranges=((3, 5), (10, 15)))
    raw = bytes.fromhex("05" "04" "03020505")
    assert _rf_encode(rf, 0x25) == raw
    assert RangeFilter.decode(Buffer(data=raw, vi64=True), 0x25,
                              prof=_prof(20)) == rf


def test_range_filter_open_last_range_and_property_type():
    rf = RangeFilter(set_id=0, ranges=((2, 2), (300, None)),
                     property_type=0x10)
    raw = (ref_vi64(1 + 1 + 2 + 2) + b"\x00" + ref_vi64(0x10)
           + ref_vi64(2) + ref_vi64(0) + ref_vi64(298))
    assert _rf_encode(rf, 0x28) == raw
    assert RangeFilter.decode(Buffer(data=raw, vi64=True), 0x28,
                              prof=_prof(20)) == rf


def test_zero_length_range_filter_removes_the_filter():
    assert _rf_encode(RangeFilter(set_id=None), 0x26) == b"\x00"
    assert RangeFilter.decode(Buffer(data=b"\x00", vi64=True), 0x26,
                              prof=_prof(20)).set_id is None


@pytest.mark.parametrize("raw, match", [
    (b"\x02\x00\x80", "truncated"),          # vi64 cut by the buffer end
    (b"\x02\x00\x80\xc8", "crosses its length"),
    (b"\x09\x00\x01", "overruns"),
], ids=["truncated", "crosses-length", "overruns"])
def test_malformed_range_filter_is_a_protocol_violation(raw, match):
    with pytest.raises(MOQTProtocolViolation, match=match):
        RangeFilter.decode(Buffer(data=raw, vi64=True), 0x25,
                           prof=_prof(20), buf_end=len(raw))


def _params(params, draft):
    buf = Buffer(capacity=256, vi64=True)
    MOQTMessage._serialize_params(buf, params, prof=_prof(draft))
    return bytes(buf.data_slice(0, buf.tell()))


def _unparams(raw, draft, scope=None):
    return MOQTMessage._deserialize_params(
        Buffer(data=raw, vi64=True), prof=_prof(draft), buf_end=len(raw),
        scope=scope)


def test_fill_parameters_is_a_counted_nested_block():
    # moq-dev's current-group join: count 1, LOCATION_FILTER StartGroup=1.
    raw = _params({0x23: {0x21: LocationFilter.relative(1)}}, 20)
    assert raw == bytes.fromhex("01" "23" "04" "01210101")
    assert _unparams(raw, 20) == {0x23: {0x21: LocationFilter.relative(1)}}
    # d22 nests the typed filter.
    raw22 = _params({0x23: {0x21: LocationFilter.relative(1)}}, 22)
    assert raw22 == bytes.fromhex("01" "23" "04" "01210101")
    raw22 = _params({0x23: {0x21: LocationFilter.next_object()}}, 22)
    assert raw22 == bytes.fromhex("01" "23" "03" "012105")


@pytest.mark.parametrize("raw, match", [
    (bytes.fromhex("012305" "01210101"), "overruns|exceeds remaining"),
    (bytes.fromhex("012305" "0121010100"), "does not fill"),
    (bytes.fromhex("012303" "011001"), "not allowed in FillParameters"),
    (bytes.fromhex("012300"), "no Number of Parameters"),
], ids=["past-message", "short-of-length", "forward-inside", "empty"])
def test_malformed_fill_parameters_is_a_protocol_violation(raw, match):
    with pytest.raises(MOQTProtocolViolation, match=match):
        _unparams(raw, 20)


def test_namespace_prefix_parameter_d20_prefixed_d22_bare():
    # d20 keeps the d18 length prefix; d22 §9.20 makes Track
    # Namespace an encoding of its own, with no Length.
    assert _params({0x34: (b"a", b"bc")}, 20) == \
        bytes.fromhex("0134 06 02 0161 026263")
    bare = bytes.fromhex("0134 02 0161 026263")
    assert _params({0x34: (b"a", b"bc")}, 22) == bare
    assert _unparams(bare, 22) == {0x34: (b"a", b"bc")}


# -- d20 FETCH / FETCH_OK / fetch objects -------------------------------

def test_d20_fetch_exact_bytes():
    # §10.13: Request ID, Track Namespace, Track Name, Parameters; the
    # range {1, 0} through all of group 2 is LOCATION_FILTER [1, 0, 1].
    m = Fetch(fetch_type=1, request_id=4, namespace=(b"n",), track_name=b"t",
              subscriber_priority=None, group_order=None,
              start_group=1, start_object=0, end_group=2, end_object=0)
    body = b"\x04\x01\x01n\x01t" + b"\x01" + b"\x21" + ref_lf_d20(1, 0, 1)
    raw = bytes(m.serialize(prof=_prof(20)).data)
    assert raw == b"\x16" + len(body).to_bytes(2, "big") + body
    body22 = b"\x04\x01\x01n\x01t" + b"\x01\x21" + ref_lf_d22(3, 1, 0, 1)
    raw = bytes(m.serialize(prof=_prof(22)).data)
    assert raw == b"\x16" + len(body22).to_bytes(2, "big") + body22


def test_d20_fetch_ok_end_location_is_inclusive():
    # The field keeps the d14-d18 form (last Object + 1); the wire is
    # the last Object.
    ok = FetchOk(request_id=None, end_of_track=1, largest_group_id=5,
                 largest_object_id=4)
    raw = bytes(ok.serialize(prof=_prof(20)).data)
    assert raw == bytes.fromhex("18" "0004" "01" "05" "03" "00")
    raw18 = bytes(ok.serialize(prof=profile_for(18)).data)
    assert raw18 == bytes.fromhex("18" "0004" "01" "05" "04" "00")
    got = FetchOk.deserialize(Buffer(data=raw[3:], vi64=True),
                              prof=_prof(20), buf_end=len(raw) - 3)
    assert (got.largest_group_id, got.largest_object_id) == (5, 4)


def test_d20_fetch_ok_parameter_is_a_protocol_violation():
    # FETCH_OK defines no parameter from d20; d18 tolerated GROUP_ORDER.
    body = bytes.fromhex("00" "05" "03" "01" "22" "01")
    with pytest.raises(MOQTProtocolViolation, match="not allowed in FetchOk"):
        FetchOk.deserialize(Buffer(data=body, vi64=True), prof=_prof(20),
                            buf_end=len(body))
    FetchOk.deserialize(Buffer(data=body, vi64=True), prof=profile_for(18),
                        buf_end=len(body))


def test_d20_end_of_timed_out_range():
    marker = FetchObject(group_id=7, object_id=9, end_of_range=0x20C,
                         payload=b"")
    raw = bytes(marker.serialize(prof=_prof(20)).data)
    assert raw == ref_vi64(0x20C) + b"\x07\x09"
    got = FetchObject.deserialize(Buffer(data=raw, vi64=True),
                                  prof=_prof(20))
    assert (got.end_of_range, got.group_id, got.object_id) == (0x20C, 7, 9)
    # Not defined before d20: both directions refuse it.
    with pytest.raises(ValueError, match="flags 0x20c"):
        FetchObject.deserialize(Buffer(data=raw, vi64=True),
                                prof=profile_for(18))
    with pytest.raises(ValueError, match="not defined at d18"):
        marker.serialize(prof=profile_for(18))


def test_d20_fetch_object_carries_no_status():
    # §11.2.1.1: no Object Status in a FETCH; a zero length is an empty
    # Normal object (moq-dev and the contribution runner read it so).
    objs = [FetchObject(group_id=1, object_id=0, publisher_priority=200,
                        payload=b"tt"),
            FetchObject(group_id=1, object_id=1, publisher_priority=200,
                        payload=b"")]
    prior = None
    wire = []
    for o in objs:
        wire.append(bytes(o.serialize(prof=_prof(20), prior=prior).data))
        prior = o
    assert wire == [bytes.fromhex("1c0100c8027474"), bytes.fromhex("0000")]
    rb = Buffer(data=b"".join(wire), vi64=True)
    first = FetchObject.deserialize(rb, prof=_prof(20))
    second = FetchObject.deserialize(rb, prior=first, prof=_prof(20))
    assert rb.tell() == sum(map(len, wire))
    assert (second.object_id, second.payload, second.status) == (
        1, b"", ObjectStatus.NORMAL)
    with pytest.raises(ValueError, match="no Object Status"):
        FetchObject(group_id=1, object_id=2, payload=b"",
                    status=ObjectStatus.END_OF_GROUP).serialize(prof=_prof(20))


def test_d22_fetch_every_parameter_exact_bytes():
    # d22 §9.11: FILL_TIMEOUT 0, SUBSCRIBER_PRIORITY 7, LOCATION_FILTER
    # {1, 0}..{2, 3}, GROUP_ORDER Descending, INCLUDE_PROPERTIES 0; keys
    # delta-coded in ascending order.
    m = Fetch(fetch_type=1, request_id=4, namespace=(b"n",), track_name=b"t",
              subscriber_priority=7, group_order=2,
              location_filter=LocationFilter.absolute(1, 0, 2, 3),
              parameters={0x0A: 0, 0x35: 0})
    body = (b"\x04\x01\x01n\x01t" + b"\x05"
            + b"\x0a\x00" + b"\x16\x07" + b"\x01" + ref_lf_d22(4, 1, 0, 1, 3)
            + b"\x01\x02" + b"\x13\x00")
    raw = bytes(m.serialize(prof=_prof(22)).data)
    assert raw == b"\x16" + len(body).to_bytes(2, "big") + body
    got = Fetch.deserialize(Buffer(data=body, vi64=True), prof=_prof(22),
                            buf_end=len(body))
    assert got.location_filter == LocationFilter.absolute(1, 0, 2, 3)
    assert (got.subscriber_priority, got.group_order) == (7, 2)
    assert got.parameters == {0x0A: 0, 0x35: 0}
    # The d14-d18 view of the inclusive End {2, 3} is {2, 4}.
    assert (got.start_group, got.start_object, got.end_group,
            got.end_object) == (1, 0, 2, 4)


@pytest.mark.parametrize("draft, params, match", [
    (22, b"\x01\x21\x06", "Location Filter Type 0x6"),
    (20, b"\x01\x21\x05\x01\x02\x03\x04\x05", "more than four"),
    (22, b"\x01\x23\x01\x00", "not allowed in Fetch"),   # FILL_PARAMETERS
    (22, b"\x01\x22\x03", "0x22"),                       # GROUP_ORDER 3
    (20, b"\x01\x35\x02", "0x35"),                       # INCLUDE_PROPERTIES 2
    (22, b"\x02\x21\x00\x00\x00", "duplicate"),          # LOCATION_FILTER twice
    (20, b"\x01\x21\x04\x01", "overruns"),
], ids=["d22-filter-type-6", "d20-five-fields", "fill-parameters",
        "group-order-3", "include-properties-2", "filter-twice",
        "filter-past-message"])
def test_d20_malformed_fetch_is_a_protocol_violation(draft, params, match):
    body = b"\x04\x01\x01n\x01t" + params
    with pytest.raises(MOQTProtocolViolation, match=match):
        Fetch.deserialize(Buffer(data=body, vi64=True), prof=_prof(draft),
                          buf_end=len(body))


def test_d22_fetch_ok_exact_bytes():
    # d22 §9.12: End Of Track, inclusive End Location, no parameters, then
    # Track Properties to the end of the message.
    ok = FetchOk(request_id=None, end_of_track=0, largest_group_id=200,
                 largest_object_id=1, track_extensions={0x08: 1000})
    body = b"\x00" + ref_vi64(200) + b"\x00" + b"\x00" + b"\x08" + ref_vi64(1000)
    raw = bytes(ok.serialize(prof=_prof(22)).data)
    assert raw == b"\x18" + len(body).to_bytes(2, "big") + body
    got = FetchOk.deserialize(Buffer(data=body, vi64=True), prof=_prof(22),
                              buf_end=len(body))
    assert (got.largest_group_id, got.largest_object_id) == (200, 1)
    assert got.track_extensions == {0x08: 1000}


def _fetch_stream(raw: bytes, draft: int, group_order=0x1):
    rb = Buffer(data=raw, vi64=True)
    prior, out = None, []
    while rb.tell() < len(raw):
        prior = FetchObject.deserialize(rb, prior=prior, prof=_prof(draft),
                                        group_order=group_order)
        out.append(prior)
    return out


@pytest.mark.parametrize("draft", _D20_DRAFTS)
def test_d20_object_after_a_marker_takes_its_location(draft):
    # d22 §11.4.1.2: after End of Timed-Out Range {3, 4}, an Object with
    # flags 0 is {3, 5}, its Subgroup and Priority the last actual one's.
    first = bytes.fromhex("1f" "02" "03" "01" "07" "0161")  # {2,1} sg 3 pri 7
    marker = ref_vi64(0x20C) + b"\x03\x04"
    nxt = bytes.fromhex("01" "0162")                     # sg prior, all else prior
    got = _fetch_stream(first + marker + nxt, draft)
    assert [(o.group_id, o.object_id, o.end_of_range) for o in got] == [
        (2, 1, None), (3, 4, 0x20C), (3, 5, None)]
    assert (got[2].subgroup_id, got[2].publisher_priority) == (3, 7)


@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("nxt, match", [
    (bytes.fromhex("01" "0162"), "prior Subgroup"),      # sg = prior
    (bytes.fromhex("00" "0162"), "prior Priority"),      # priority = prior
], ids=["subgroup", "priority"])
def test_prior_reference_after_a_leading_marker_is_refused(draft, nxt, match):
    # A marker with no actual Object before it carries no Subgroup or
    # Priority to inherit.
    marker = ref_vi64(0x10C) + b"\x03\x04"
    with pytest.raises(ValueError, match=match):
        _fetch_stream(marker + nxt, draft)


@pytest.mark.parametrize("draft", _D20_DRAFTS)
@pytest.mark.parametrize("flags", [0x80, 0x30C, 0x40C])
def test_d20_undefined_range_indicator_is_refused(draft, flags):
    with pytest.raises(ValueError, match="invalid serialization flags"):
        _fetch_stream(ref_vi64(flags) + b"\x03\x04", draft)


# -- PADDING streams and datagrams (§11.5) -----------------------------

@pytest.mark.parametrize("draft", [18, 20, 22])
@pytest.mark.parametrize("datagram, ptype", [(False, 0x132B3E28),
                                             (True, 0x132B3E29)])
@pytest.mark.parametrize("size", [0, 1, 37])
def test_padding_exact_bytes(draft, datagram, ptype, size):
    from aiomoqt.messages.data import encode_padding
    wire = encode_padding(size, prof=profile_for(draft), datagram=datagram)
    assert wire == ref_vi64(ptype) + bytes(size)


@pytest.mark.parametrize("draft", [14, 16])
def test_padding_does_not_exist_before_d18(draft):
    from aiomoqt.messages.data import encode_padding
    with pytest.raises(ValueError):
        encode_padding(4, prof=profile_for(draft))


def test_padding_size_is_not_negative():
    from aiomoqt.messages.data import encode_padding
    with pytest.raises(ValueError):
        encode_padding(-1, prof=profile_for(18))


# -- Immutable Properties (0x0B) in received Track Properties ---------
#
# d16 §11.2 / d18 §12.7: the value is a Key-Value-Pair sequence; a
# nested 0x0B or a KVP that cannot be parsed is malformed. Decoders
# close with PROTOCOL_VIOLATION, also under --compat lenient-extensions.

from aiomoqt.messages.publish import Publish  # noqa: E402

_IMM_DRAFTS = [16, 18]
_IMM_INNER = {0x04: 2000, 0x3B: b"m"}


def _imm_inner(draft):
    v = lambda x: _ref_vint(draft, x)  # noqa: E731
    return v(0x04) + v(2000) + v(0x3B - 0x04) + v(1) + b"m"


def _imm_props(draft, inner, trailer=True):
    """Track Properties: Immutable Properties carrying `inner`, then a
    mutable 0x3D so bytes follow the immutable block."""
    v = lambda x: _ref_vint(draft, x)  # noqa: E731
    out = v(0x0B) + v(len(inner)) + inner
    if trailer:
        out += v(0x3D - 0x0B) + v(3) + b"xyz"
    return out


def _imm_subscribe_ok(draft, props):
    head = b"" if draft >= 18 else _ref_vint(draft, 1)
    return head + _ref_vint(draft, 0) + _ref_vint(draft, 0) + props


def _imm_publish(draft, props):
    v = lambda x: _ref_vint(draft, x)  # noqa: E731
    return (v(1) + v(1) + v(1) + b"n" + v(1) + b"x" + v(0) + v(0)
            + props)


_IMM_MESSAGES = {"subscribe_ok": (SubscribeOk, _imm_subscribe_ok),
                 "publish": (Publish, _imm_publish)}


def _imm_decode(draft, kind, props):
    cls, build = _IMM_MESSAGES[kind]
    body = build(draft, props)
    prof = profile_for(draft)
    return cls.deserialize(Buffer(data=body, vi64=prof.vi64), prof=prof,
                           buf_end=len(body))


@pytest.mark.parametrize("kind", list(_IMM_MESSAGES))
@pytest.mark.parametrize("draft", _IMM_DRAFTS)
def test_received_immutable_properties_decode(draft, kind):
    inner = _imm_inner(draft)
    msg = _imm_decode(draft, kind, _imm_props(draft, inner))
    assert msg.track_extensions == {0x0B: inner, 0x3D: b"xyz"}
    nested = MOQTMessage._extensions_decode(
        Buffer(data=inner, vi64=profile_for(draft).vi64),
        with_length=False, buf_end=len(inner), delta=True)
    assert nested == _IMM_INNER


def _imm_malformed(draft):
    v = lambda x: _ref_vint(draft, x)  # noqa: E731
    return {
        "nested": (v(0x0B) + v(0), "inside Immutable"),
        # A Type with no value.
        "truncated": (v(0x04), "do not parse"),
        # A value declared past the end of the Immutable Properties.
        "overrun": (v(0x3B) + v(5) + b"ab", "do not parse"),
    }


@pytest.mark.parametrize("lenient", [False, True], ids=["strict", "lenient"])
@pytest.mark.parametrize("case", ["nested", "truncated", "overrun"])
@pytest.mark.parametrize("kind", list(_IMM_MESSAGES))
@pytest.mark.parametrize("draft", _IMM_DRAFTS)
def test_malformed_immutable_properties_are_a_protocol_violation(
        draft, kind, case, lenient, monkeypatch):
    monkeypatch.setattr(MOQTMessage, "_tolerate_trailing_extensions", lenient)
    inner, match = _imm_malformed(draft)[case]
    with pytest.raises(MOQTProtocolViolation, match=match):
        _imm_decode(draft, kind, _imm_props(draft, inner))


@pytest.mark.parametrize("vi64", [False, True], ids=["d16", "d18"])
def test_object_immutable_properties_agree_across_codecs(vi64):
    """An Object's Immutable Properties reach the app as the same opaque
    bytes from the Cython subgroup parser and the python decoder."""
    draft = 18 if vi64 else 16
    exts = {0x0B: _imm_inner(draft)}
    body = (encode_object_subgroup_vi64 if vi64
            else encode_object_subgroup)(0, exts, 0, b"pay", True, True)
    chain = StreamChain()
    chain.extend(body)
    fused = (chain.parse_object_subgroup_vi64 if vi64
             else chain.parse_object_subgroup)
    assert fused(True, 16 * 1024, True) == (0, exts, 0, b"pay")
    buf = Buffer(data=body, vi64=vi64)
    buf.pull_vint()
    assert MOQTMessage._extensions_decode(buf, delta=True) == exts


# -- d16 DELIVERY_TIMEOUT 0 (§9.2.2.2, §11.1) ---------------------------

def test_d16_zero_delivery_timeout_parameter_is_a_protocol_violation():
    with pytest.raises(MOQTProtocolViolation, match="DELIVERY_TIMEOUT"):
        _decode_subscribe(16, _subscribe_body(16, params=b"\x01\x02\x00"))


def test_d16_nonzero_delivery_timeout_parameter_is_accepted():
    got = _decode_subscribe(16, _subscribe_body(16, params=b"\x01\x02\x40\x64"))
    assert got.parameters[0x02] == 100


def test_zero_object_delivery_timeout_is_legal_at_d18():
    got = _decode_subscribe(18, _subscribe_body(18, params=b"\x01\x02\x00"))
    assert got.parameters[0x02] == 0


def test_d16_zero_delivery_timeout_track_property_is_a_protocol_violation():
    props = _ref_vint(16, 0x02) + _ref_vint(16, 0)
    with pytest.raises(MOQTProtocolViolation, match="DELIVERY_TIMEOUT"):
        _imm_decode(16, "subscribe_ok", props)
