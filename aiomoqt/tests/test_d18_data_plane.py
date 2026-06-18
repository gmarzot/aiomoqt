"""draft-18 data-plane codec round-trips (Phase 4).

Exercises the vi64 wiring of the subgroup header + objects and the
OBJECT_DATAGRAM relayout. Values >= 64 are used so vi64 and RFC9000
genuinely diverge (vi64 1-byte vs RFC9000 2-byte), proving the d18 codec
is actually taken. d14/d16 paths are covered by the existing suite.
"""
from aiomoqt.utils.buffer import Buffer
from aiomoqt.messages.track import (
    SubgroupHeader, ObjectHeader, ObjectDatagram)
from aiomoqt.messages import ObjectStatus
from aiomoqt.types import (
    SUBGROUP_ID_EXPLICIT, OBJECT_DATAGRAM_BASE)


def test_d18_subgroup_header_roundtrip():
    h = SubgroupHeader(
        track_alias=100, group_id=200, subgroup_id=300,
        publisher_priority=7, extensions_present=False,
        subgroup_id_mode=SUBGROUP_ID_EXPLICIT, draft=18)
    assert h._vi64 is True
    raw = bytes(h.serialize().data)
    rbuf = Buffer(data=raw)
    type_val = rbuf.pull_uint_vi64()
    assert (type_val & 0x10) and not (type_val & 0x80)
    out = SubgroupHeader.deserialize(rbuf, type_val, draft=18)
    assert out.track_alias == 100
    assert out.group_id == 200
    assert out.subgroup_id == 300
    assert out.publisher_priority == 7


def test_d18_subgroup_header_vi64_diverges_from_rfc9000():
    kw = dict(track_alias=100, group_id=200, subgroup_id=300,
              publisher_priority=7, subgroup_id_mode=SUBGROUP_ID_EXPLICIT)
    d16 = bytes(SubgroupHeader(**kw, draft=16).serialize().data)
    d18 = bytes(SubgroupHeader(**kw, draft=18).serialize().data)
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
    raw = bytes(dg.serialize(draft=18).data)
    rbuf = Buffer(data=raw)
    type_val = rbuf.pull_uint_vi64()
    # form 0b00X0XXXX, payload datagram (no STATUS bit)
    assert type_val & 0x20 == 0
    out = ObjectDatagram.deserialize(rbuf, len(raw), type_val, draft=18)
    assert out.track_alias == 100
    assert out.group_id == 200
    assert out.object_id == 300
    assert out.payload == b"hello"
    assert out.status == ObjectStatus.NORMAL


def test_d18_datagram_status_roundtrip():
    dg = ObjectDatagram(track_alias=100, group_id=200, object_id=300,
                        publisher_priority=5,
                        status=ObjectStatus.END_OF_GROUP)
    raw = bytes(dg.serialize(draft=18).data)
    rbuf = Buffer(data=raw)
    type_val = rbuf.pull_uint_vi64()
    assert type_val & 0x20  # STATUS bit set
    out = ObjectDatagram.deserialize(rbuf, len(raw), type_val, draft=18)
    assert out.status == ObjectStatus.END_OF_GROUP
    assert out.payload == b""
    assert out.object_id == 300
