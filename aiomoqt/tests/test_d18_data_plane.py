"""draft-18 data-plane codec round-trips (Phase 4).

Exercises the vi64 wiring of the subgroup header + objects and the
OBJECT_DATAGRAM relayout. Values >= 64 are used so vi64 and RFC9000
genuinely diverge (vi64 1-byte vs RFC9000 2-byte), proving the d18 codec
is actually taken. d14/d16 paths are covered by the existing suite.
"""
import pytest
from aiomoqt.utils.buffer import Buffer
from aiomoqt.messages.data import (
    SubgroupHeader, ObjectHeader, ObjectDatagram)
from aiomoqt.messages import ObjectStatus
from aiomoqt.context import profile_for
from aiomoqt.types import (
    SUBGROUP_ID_EXPLICIT)


def test_d18_subgroup_header_roundtrip():
    h = SubgroupHeader(
        track_alias=100, group_id=200, subgroup_id=300,
        publisher_priority=7, extensions_present=False,
        subgroup_id_mode=SUBGROUP_ID_EXPLICIT, prof=profile_for(18))
    assert h._vi64 is True
    raw = bytes(h.serialize().data)
    rbuf = Buffer(data=raw)
    type_val = rbuf.pull_uint_vi64()
    assert (type_val & 0x10) and not (type_val & 0x80)
    out = SubgroupHeader.deserialize(rbuf, type_val, prof=profile_for(18))
    assert out.track_alias == 100
    assert out.group_id == 200
    assert out.subgroup_id == 300
    assert out.publisher_priority == 7


def test_d18_subgroup_header_vi64_diverges_from_rfc9000():
    kw = dict(track_alias=100, group_id=200, subgroup_id=300,
              publisher_priority=7, subgroup_id_mode=SUBGROUP_ID_EXPLICIT)
    d16 = bytes(SubgroupHeader(**kw, prof=profile_for(16)).serialize().data)
    d18 = bytes(SubgroupHeader(**kw, prof=profile_for(18)).serialize().data)
    # Header fields 100/200/300 are all >= 64, so RFC9000 spends 2 bytes
    # each while vi64 spends 1 -> the d18 header is strictly shorter.
    assert len(d18) < len(d16)


def test_d18_object_roundtrip_slowpath():
    # Buffer slow path (no fused parse_object_subgroup on a plain Buffer):
    # exercises the field-by-field vi64 decode.
    obj = ObjectHeader(object_id=0, status=ObjectStatus.NORMAL,
                       payload=b"x" * 130)
    raw = bytes(obj.serialize(extensions_present=False,
                              prev_object_id=None, vi64=True).data)
    into = ObjectHeader.__new__(ObjectHeader)
    rbuf = Buffer(data=raw)
    into.deserialize_into(rbuf, len(raw), extensions_present=False,
                          prev_object_id=None, vi64=True)
    assert into.object_id == 0
    assert into.payload == b"x" * 130
    assert into.status == ObjectStatus.NORMAL


def test_d18_datagram_payload_roundtrip():
    dg = ObjectDatagram(track_alias=100, group_id=200, object_id=300,
                        publisher_priority=5, payload=b"hello")
    raw = bytes(dg.serialize(prof=profile_for(18)).data)
    rbuf = Buffer(data=raw)
    type_val = rbuf.pull_uint_vi64()
    # form 0b00X0XXXX, payload datagram (no STATUS bit)
    assert type_val & 0x20 == 0
    out = ObjectDatagram.deserialize(rbuf, len(raw), type_val, prof=profile_for(18))
    assert out.track_alias == 100
    assert out.group_id == 200
    assert out.object_id == 300
    assert out.payload == b"hello"
    assert out.status == ObjectStatus.NORMAL


def test_d18_datagram_status_roundtrip():
    dg = ObjectDatagram(track_alias=100, group_id=200, object_id=300,
                        publisher_priority=5,
                        status=ObjectStatus.END_OF_GROUP)
    raw = bytes(dg.serialize(prof=profile_for(18)).data)
    rbuf = Buffer(data=raw)
    type_val = rbuf.pull_uint_vi64()
    assert type_val & 0x20  # STATUS bit set
    out = ObjectDatagram.deserialize(rbuf, len(raw), type_val, prof=profile_for(18))
    assert out.status == ObjectStatus.END_OF_GROUP
    assert out.payload == b""
    assert out.object_id == 300


# -- d18 extension varint codec --------------------------------------
#
# Regression: the extension KVP block was encoded with the STANDARD
# varint encoder while the surrounding d18 header fields used vi64.
# Self-testing could not see it — our encoder and decoder were both
# standard, so loopback round-tripped fine — but moxygen parses the
# whole frame as vi64, reads garbage, and closes the session with
# PROTOCOL_VIOLATION. Caught only by publishing to a real d18 relay.

class _Prof:
    def __init__(self, vi64):
        self.vi64 = vi64
        self.draft = 18 if vi64 else 16
        self.params_delta_coded = True  # KVP delta types, d16+ §1.4.2


def test_d18_extensions_use_vi64_not_standard_varint():
    from aiomoqt.messages.data import ObjectDatagram
    big = 1_700_000_000_000_000        # a value whose encodings differ
    d16 = ObjectDatagram(track_alias=1, group_id=2, object_id=3,
                         extensions={0x20: big}, payload=b"xy")
    d18 = ObjectDatagram(track_alias=1, group_id=2, object_id=3,
                         extensions={0x20: big}, payload=b"xy")
    b16 = d16.serialize(prof=_Prof(False))
    b18 = d18.serialize(prof=_Prof(True))
    raw16 = bytes(b16.data_slice(0, b16.tell()))
    raw18 = bytes(b18.data_slice(0, b18.tell()))
    assert raw16 != raw18, (
        "d18 datagram encodes identically to d16 — the extension block "
        "is not following the vi64 codec")
    # standard varint tags a 8-byte value 0xc0..., vi64 uses 0xfe...
    assert raw16.hex().find("c0060a24181e4000") > 0, raw16.hex()
    assert raw18.hex().find("fe060a24181e4000") > 0, raw18.hex()


@pytest.mark.parametrize("vi64", [False, True], ids=["d16", "d18"])
def test_datagram_extension_round_trip(vi64):
    from aiomoqt.messages.data import ObjectDatagram
    from aiomoqt.utils.buffer import Buffer
    prof = _Prof(vi64)
    big = 1_700_000_000_000_000
    o = ObjectDatagram(track_alias=7, group_id=9, object_id=11,
                       extensions={0x20: big}, payload=b"payload")
    b = o.serialize(prof=prof)
    raw = bytes(b.data_slice(0, b.tell()))
    rb = Buffer(data=raw, vi64=vi64)
    type_val = rb.pull_vint()
    got = ObjectDatagram.deserialize(rb, len(raw), type_val=type_val,
                                     prof=prof)
    assert got.extensions == {0x20: big}
    assert got.payload == b"payload"
    assert (got.track_alias, got.group_id, got.object_id) == (7, 9, 11)
