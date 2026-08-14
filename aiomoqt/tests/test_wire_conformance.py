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
