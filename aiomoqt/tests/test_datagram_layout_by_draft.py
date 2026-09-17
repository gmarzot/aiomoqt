"""OBJECT_DATAGRAM layout follows the draft profile: the merged form
(STATUS 0x20, DEFAULT_PRIORITY 0x08) is d16+ with the draft's varint
codec; d14 has no STATUS bit at all."""
from aiomoqt.context import profile_for
from aiomoqt.messages.data import ObjectDatagram
from aiomoqt.types import ObjectStatus
from aiopquic.buffer import Buffer


def _status_dgram():
    return ObjectDatagram(track_alias=3, group_id=7, object_id=2,
                          publisher_priority=5,
                          status=ObjectStatus.END_OF_GROUP)


def test_d16_status_datagram_round_trips_in_the_merged_layout():
    prof = profile_for(16)
    raw = bytes(_status_dgram().serialize(prof=prof).data)
    buf = Buffer(data=raw)
    type_val = buf.pull_uint_var()             # d16 codec is RFC 9000
    assert type_val & 0x20 and not type_val & 0x02
    out = ObjectDatagram.deserialize(buf, len(raw), type_val, prof=prof)
    assert out.status == ObjectStatus.END_OF_GROUP
    assert out.payload == b""
    assert (out.track_alias, out.group_id, out.object_id) == (3, 7, 2)


def test_d18_status_datagram_uses_vi64_with_the_same_bits():
    prof = profile_for(18)
    raw = bytes(_status_dgram().serialize(prof=prof).data)
    buf = Buffer(data=raw)
    type_val = buf.pull_uint_vi64()
    assert type_val & 0x20
    out = ObjectDatagram.deserialize(buf, len(raw), type_val, prof=prof)
    assert out.status == ObjectStatus.END_OF_GROUP


def test_d14_object_datagram_never_sets_the_status_bit():
    prof = profile_for(14)
    raw = bytes(_status_dgram().serialize(prof=prof).data)
    type_val = Buffer(data=raw).pull_uint_var()
    assert type_val <= 0x07
