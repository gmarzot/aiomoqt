"""Receive-side MUST-close rules: an unknown uni stream type, an object
status the draft does not define, properties on a status object, a
datagram PROPERTIES bit with no properties, an over-long reason phrase,
and a namespace with more than 32 fields all close the session with
PROTOCOL_VIOLATION."""
import time
from types import SimpleNamespace

import pytest

from aiomoqt.context import profile_for
from aiomoqt.messages.base import MOQTMessage
from aiomoqt.messages.data import ObjectDatagram, SubgroupHeader
from aiomoqt.messages.request import RequestError
from aiomoqt.messages.subscribe import Subscribe
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.types import MOQTProtocolViolation, ObjectStatus
from aiopquic.buffer import Buffer

EOG = ObjectStatus.END_OF_GROUP


def _stub(draft):
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = draft
    s._data_streams = {}
    s._control_chains = {}
    s._uni_peek_stash = {}
    s._stream_torn_down = {}
    s._stream_torn_down_last_sweep = time.monotonic()
    s._stream_torn_down_evict_after = 30.0
    s._stream_end_handlers = {}
    s._fetch_done_futures = {}
    s._subgroup_stream_by_key = {}
    s._fetch_stream_by_request = {}
    s._track_aliases = {7: 1}
    s._unbound_aliases = {}
    s._unbound_escalated = set()
    s._malformed_aliases = set()
    s._group_bound = {}
    s._track_bound = {}
    s._object_handlers = {}
    s._track_default_priority = {}
    s._loop = SimpleNamespace(call_later=lambda delay, cb: SimpleNamespace(
        cancel=lambda: None))
    s.delivered = []
    s.closed = []
    s.stopped = []
    s.on_object_received = lambda msg, size, ts, gid, sgid: \
        s.delivered.append((msg.object_id, msg.status))
    s._close_session = lambda code, reason: s.closed.append((int(code), reason))
    s.stream_stop_sending = lambda sid, code: s.stopped.append((sid, code))
    return s


def _vint(draft, *values):
    buf = Buffer(capacity=64, vi64=profile_for(draft).vi64)
    for v in values:
        buf.push_vint(v)
    return bytes(buf.data)


def _header(draft, **kw):
    return SubgroupHeader(track_alias=7, group_id=1, subgroup_id=0,
                          prof=profile_for(draft), **kw)


@pytest.mark.parametrize("draft, stream_type", [
    (18, 0x110), (18, 0x90), (16, 0x50), (14, 0x30), (18, 0x16),
])
def test_unknown_uni_stream_type_closes_the_session(draft, stream_type):
    s = _stub(draft)
    s._on_stream_data(3, _vint(draft, stream_type, 7, 1, 0), False)
    assert s.closed and s.closed[0][0] == 0x3          # PROTOCOL_VIOLATION
    assert "stream type" in s.closed[0][1]


@pytest.mark.parametrize("draft, stream_type", [(18, 0x50), (18, 0x30), (16, 0x30)])
def test_draft_defined_subgroup_types_are_admitted(draft, stream_type):
    s = _stub(draft)
    s._on_stream_data(3, _vint(draft, stream_type, 7, 1, 0), False)
    assert not s.closed
    assert s._data_streams[3].parser is not None


def test_undefined_object_status_closes_the_session():
    s = _stub(18)
    hdr = _header(18)
    s._on_stream_data(3, bytes(hdr.serialize().data), False)
    s._on_stream_data(3, _vint(18, 0, 0, 0x7), False)     # delta, len 0, status 7
    assert s.closed == [(0x3, "unknown object status 0x7")]


def test_d14_only_status_is_a_violation_at_d18_but_fine_at_d14():
    for draft, expect_close in ((18, True), (14, False)):
        s = _stub(draft)
        hdr = _header(draft)
        s._on_stream_data(3, bytes(hdr.serialize().data), False)
        s._on_stream_data(3, bytes(hdr.next_object(
            status=ObjectStatus.DOES_NOT_EXIST).data), False)
        assert bool(s.closed) is expect_close, draft
        if not expect_close:
            assert s.delivered == [(0, ObjectStatus.DOES_NOT_EXIST)]


def test_properties_on_a_status_object_close_the_session():
    s = _stub(18)
    hdr = _header(18, extensions_present=True)
    s._on_stream_data(3, bytes(hdr.serialize().data), False)
    s._on_stream_data(3, bytes(hdr.next_object(
        payload=b"x", extensions={0x02: 5}).data), False)   # normal: fine
    assert s.delivered == [(0, ObjectStatus.NORMAL)] and not s.closed
    # Our encoder refuses properties on a status object; craft the wire.
    buf = Buffer(capacity=64, vi64=True)
    buf.push_vint(0)                                  # delta -> object 1
    MOQTMessage._extensions_encode(buf, {0x02: 5}, delta=True)
    buf.push_vint(0)                                  # payload length
    buf.push_vint(int(EOG))
    s._on_stream_data(3, bytes(buf.data), False)
    assert s.closed == [(0x3, "properties on a status object")]


def _dgram(draft, raw):
    s = _stub(draft)
    s._moqt_handle_data_dgram(Buffer(data=raw))
    return s


def test_datagram_properties_bit_with_no_properties_closes_the_session():
    raw = _vint(18, 0x01, 7, 1, 0) + bytes([128]) + _vint(18, 0) + b"pay"
    s = _dgram(18, raw)
    assert s.closed == [(0x3, "datagram PROPERTIES bit with no properties")]


def test_datagram_status_with_properties_closes_the_session():
    dg = ObjectDatagram(track_alias=7, group_id=1, object_id=2,
                        publisher_priority=128, status=EOG,
                        extensions={0x02: 5})
    s = _dgram(18, bytes(dg.serialize(prof=profile_for(18)).data))
    assert s.closed == [(0x3, "properties on a status object")]
    assert not s.delivered


def test_datagram_undefined_status_closes_the_session():
    raw = _vint(18, 0x20, 7, 1, 2) + bytes([128]) + _vint(18, 0x7)
    s = _dgram(18, raw)
    assert s.closed and "unknown object status 0x7" in s.closed[0][1]


@pytest.mark.parametrize("draft", [16, 18])
def test_reason_phrase_over_1024_bytes_is_a_violation(draft):
    prof = profile_for(draft)
    wire = bytes(RequestError(request_id=1, error_code=1, retry_interval=0,
                              reason="x" * 1025).serialize(prof=prof).data)
    payload = wire[3:]
    with pytest.raises(MOQTProtocolViolation, match="reason phrase 1025"):
        RequestError.deserialize(Buffer(data=payload, vi64=prof.vi64), prof=prof,
                                 buf_end=len(payload))


@pytest.mark.parametrize("draft", [16, 18])
def test_namespace_over_32_fields_is_a_violation(draft):
    prof = profile_for(draft)
    msg = Subscribe(request_id=1, track_namespace=tuple([b"a"] * 33),
                    track_name=b"t", priority=128, group_order=1, forward=1,
                    filter_type=2)
    wire = bytes(msg.serialize(prof=prof).data)
    payload = wire[3:]
    with pytest.raises(MOQTProtocolViolation, match="33 fields"):
        Subscribe.deserialize(Buffer(data=payload, vi64=prof.vi64), prof=prof,
                              buf_end=len(payload))


def test_fin_mid_object_closes_the_session():
    s = _stub(18)
    hdr = _header(18)
    s._on_stream_data(3, bytes(hdr.serialize().data), False)
    obj = bytes(hdr.next_object(payload=b"abcdefgh").data)
    s._on_stream_data(3, obj[:-3], True)               # FIN inside the payload
    assert s.closed and s.closed[0][0] == 0x3
    assert "FIN mid-object" in s.closed[0][1]


def test_fin_after_a_whole_object_is_clean():
    s = _stub(18)
    hdr = _header(18)
    s._on_stream_data(3, bytes(hdr.serialize().data), False)
    s._on_stream_data(3, bytes(hdr.next_object(payload=b"abcdefgh").data), True)
    assert not s.closed and s.delivered == [(0, ObjectStatus.NORMAL)]


def test_fin_inside_the_stream_header_rejects_without_closing():
    s = _stub(18)
    s._on_stream_data(3, _vint(18, 0x50, 7)[:2], True)   # truncated header
    assert not s.closed
