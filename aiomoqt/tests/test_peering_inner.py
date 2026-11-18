"""MoQT d18 inner framing of moq-fabric data: moqx's bytes both ways,
aiomoqt's own d18 decoder on what we write, moqx's quirks, bad input.

Vectors are moqx 7b67019's PeeringObject encoding (wire-spec §10.1).
"""
import pytest

from aiomoqt.messages import ObjectStatus
from aiomoqt.messages.data import ObjectDatagram, ObjectHeader, SubgroupHeader
from aiomoqt.peering.inner import (DATAGRAM_MAX_INNER, MAX_OBJECT, P18,
                                   DatagramObject, StreamObject,
                                   SubgroupReader, SubgroupWriter,
                                   decode_datagram, encode_datagram)
from aiomoqt.peering.wire import (DataHeader, DataType, WireError,
                                  parse_data_header, rewrite_sns)
from aiomoqt.types import LOC_TIMESTAMP
from aiomoqt.utils.buffer import Buffer

H = 0xf9deacf2c69f506d   # (moq-test, live) / video

NEW_STREAM = "130200000001f9deacf2c69f506d8000001388"
SG0_HEADER = "51fff9deacf2c69f506d0580"
SG7_HEADER = "55fff9deacf2c69f506d050780"
OBJ_HELLO = "00000568656c6c6f"
OBJ_WORLD = "000005776f726c64"
OBJ_EOG = "00000003"
DGRAM_OBJ0 = "04fff9deacf2c69f506d07806869"
DGRAM_OBJ3 = "00fff9deacf2c69f506d0703806869"


def _h(text):
    return bytes.fromhex(text)


def _obj(oid, payload=b"", status=ObjectStatus.NORMAL, sg=0, group=5,
         ext=None):
    return StreamObject(group, sg, oid, status, ext, payload, 128)


# -- moqx vectors ----------------------------------------------------

def test_subgroup_writer_matches_moqx():
    w = SubgroupWriter(H, 5, 0)
    assert w.header.hex() == SG0_HEADER
    assert w.object(0, b"hello").hex() == OBJ_HELLO
    assert w.object(1, b"world").hex() == OBJ_WORLD
    assert w.status(2, ObjectStatus.END_OF_GROUP).hex() == OBJ_EOG
    assert SubgroupWriter(H, 5, 7).header.hex() == SG7_HEADER


def test_new_stream_chunks_match_moqx():
    """chunk 1 = NEW_STREAM header + subgroup header + object 0."""
    hdr = DataHeader(DataType.NEW_STREAM, 1, H, 128, 5000).encode()
    w = SubgroupWriter(H, 5, 0)
    assert (hdr + w.header + w.object(0, b"hello")).hex() == (
        NEW_STREAM + SG0_HEADER + OBJ_HELLO)
    assert rewrite_sns(hdr, 18).hex() == (
        "130200000012f9deacf2c69f506d8000001388")


@pytest.mark.parametrize("oid,hexed", [(0, DGRAM_OBJ0), (3, DGRAM_OBJ3)])
def test_datagram_matches_moqx(oid, hexed):
    assert encode_datagram(H, 7, oid, b"hi").hex() == hexed
    assert decode_datagram(_h(hexed)) == DatagramObject(
        7, oid, ObjectStatus.NORMAL, None, b"hi", 128, False)
    frame = _h("0e0000000001f9deacf2c69f506d" + hexed)
    hdr, off = parse_data_header(frame, datagram=True)
    assert (hdr.sns_id, hdr.fullname_hash) == (1, H)
    assert decode_datagram(frame[off:]).object_id == oid


def test_reader_decodes_moqx_stream_byte_by_byte():
    wire = _h(SG0_HEADER + OBJ_HELLO + OBJ_WORLD + OBJ_EOG)
    r = SubgroupReader()
    got = []
    for i in range(len(wire)):
        got += r.feed(wire[i:i + 1])
    assert got == [_obj(0, b"hello"), _obj(1, b"world"),
                   _obj(2, status=ObjectStatus.END_OF_GROUP)]
    r.finish()


def test_reader_decodes_subgroup_seven():
    got = SubgroupReader().feed(_h(SG7_HEADER + OBJ_HELLO))
    assert got == [_obj(0, b"hello", sg=7)]


# -- our bytes through aiomoqt's d18 decoder --------------------------

def test_aiomoqt_d18_decoder_reads_our_stream():
    w = SubgroupWriter(H, 9, 3)
    objs = [(1, b"a" * 10, {LOC_TIMESTAMP: 1794777763064534}),
            (4, b"b" * 300, None), (5, b"", None)]
    wire = w.header + b"".join(w.object(o, p, e) for o, p, e in objs[:2])
    wire += w.status(5, ObjectStatus.END_OF_TRACK)
    buf = Buffer(data=wire, vi64=True)
    stype = buf.pull_uint_vi64()
    h = SubgroupHeader.deserialize(buf, stype, prof=P18)
    assert (h.track_alias, h.group_id, h.subgroup_id, h.first_object,
            h.extensions_present) == (H, 9, 3, True, True)
    prev = None
    seen = []
    while buf.tell() < len(wire):
        o = ObjectHeader(object_id=0)
        o.deserialize_into(buf, len(wire), True, prev, True,
                           P18.params_delta_coded)
        prev = o.object_id
        seen.append((o.object_id, o.payload, o.extensions, o.status))
    assert seen == [(1, b"a" * 10, {LOC_TIMESTAMP: 1794777763064534},
                     ObjectStatus.NORMAL),
                    (4, b"b" * 300, None, ObjectStatus.NORMAL),
                    (5, b"", None, ObjectStatus.END_OF_TRACK)]


def test_aiomoqt_d18_decoder_reads_our_datagram():
    data = encode_datagram(H, 2, 6, b"xyz", {LOC_TIMESTAMP: 42},
                           end_of_group=True)
    buf = Buffer(data=data, vi64=True)
    dtype = buf.pull_uint_vi64()
    d = ObjectDatagram.deserialize(buf, len(data), dtype, prof=P18)
    assert (d.track_alias, d.group_id, d.object_id, d.payload,
            d.extensions, d.end_of_group) == (H, 2, 6, b"xyz",
                                              {LOC_TIMESTAMP: 42}, True)
    assert decode_datagram(data).extensions == {LOC_TIMESTAMP: 42}


# -- moqx quirks ------------------------------------------------------

def test_first_object_set_mid_subgroup():
    """moqx sets FIRST_OBJECT when it starts forwarding mid-subgroup; the
    first id is then absolute."""
    w = SubgroupWriter(H, 5, 0)
    wire = w.header + w.object(12, b"x") + w.object(13, b"y")
    assert [o.object_id for o in SubgroupReader().feed(wire)] == [12, 13]


@pytest.mark.parametrize("status", [ObjectStatus.NORMAL,
                                    ObjectStatus.END_OF_GROUP,
                                    ObjectStatus.END_OF_TRACK])
def test_statuses_moqx_sends(status):
    w = SubgroupWriter(H, 5, 0)
    tail = (w.object(0, b"p") if status == ObjectStatus.NORMAL
            else w.status(0, status))
    (obj,) = SubgroupReader().feed(w.header + tail)
    assert obj.status == status


def test_status_datagram():
    data = encode_datagram(H, 4, 9, status=ObjectStatus.END_OF_GROUP)
    assert decode_datagram(data).status == ObjectStatus.END_OF_GROUP


def test_extension_block_present_on_every_object():
    w = SubgroupWriter(H, 5, 0)
    assert w.object(0, b"z")[1] == 0x00          # empty block, length 0
    assert w.status(1, ObjectStatus.END_OF_GROUP)[1] == 0x00


# -- malformed --------------------------------------------------------

@pytest.mark.parametrize("hexed", [
    "05fff9deacf2c69f506d",      # FETCH header
    "16",                         # subgroup id mode 3 (reserved)
    "00",                         # datagram type on a stream
])
def test_reader_rejects_non_subgroup_streams(hexed):
    with pytest.raises(WireError):
        SubgroupReader().feed(_h(hexed) + b"\x00" * 16)


def test_reader_rejects_oversize_object():
    w = SubgroupWriter(H, 5, 0)
    r = SubgroupReader(max_object=100)
    with pytest.raises(WireError):
        r.feed(w.header + w.object(0, b"q" * 101))
    with pytest.raises(WireError):
        SubgroupWriter(H, 5, 0).object(0, b"q" * (MAX_OBJECT + 1))


@pytest.mark.parametrize("cut", [3, 11, 14, 18])
def test_reader_truncated_at_fin(cut):
    wire = _h(SG0_HEADER + OBJ_HELLO)[:cut]
    r = SubgroupReader()
    assert r.feed(wire) == []
    with pytest.raises(WireError):
        r.finish()


def test_reader_unknown_status_rejected():
    w = SubgroupWriter(H, 5, 0)
    with pytest.raises(WireError):
        SubgroupReader().feed(w.header + _h("00000002"))


def test_writer_needs_rising_ids():
    w = SubgroupWriter(H, 5, 0)
    w.object(3, b"a")
    with pytest.raises(ValueError):
        w.object(3, b"b")


@pytest.mark.parametrize("hexed", [
    "10fff9deacf2c69f506d0780",   # subgroup type as a datagram
    "04fff9deacf2c6",              # truncated alias
    "04",                          # type only
    "",
])
def test_datagram_rejects(hexed):
    with pytest.raises(WireError):
        decode_datagram(_h(hexed))


def test_datagram_cap():
    assert len(encode_datagram(H, 1, 1, b"d" * (DATAGRAM_MAX_INNER - 13))) \
        == DATAGRAM_MAX_INNER
    with pytest.raises(WireError):
        encode_datagram(H, 1, 1, b"d" * (DATAGRAM_MAX_INNER - 12))
