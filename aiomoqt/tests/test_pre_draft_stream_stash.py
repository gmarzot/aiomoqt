"""A raw-QUIC session that offered several drafts must not classify stream
bytes under the default profile: a d18 peer's control uni can arrive
before ProtocolNegotiated. Bytes wait and replay under the settled draft."""
from aiomoqt.protocol import _MOQTSessionMixin

D18_CONTROL_HEAD = bytes.fromhex("af00")   # vi64 stream type 0x2F00
SETUP_BODY = bytes.fromhex("00020264")


def _session(settled: bool):
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = 14                 # unpinned __init__ default
    s._draft_settled = settled
    s._pre_draft_stream_data = {}
    s._uni_peek_stash = {}
    s._d18_control_read_sid = None
    s._data_streams = {}
    s._stream_torn_down = {}
    s._control_stream_id = None
    s.routed = []
    s._on_control_data = lambda sid, data, end, is_request_bidi=False: \
        s.routed.append(("control", sid, bytes(data), end))
    s._on_stream_data = lambda sid, data, end: \
        s.routed.append(("data", sid, bytes(data), end))
    return s


def test_bytes_before_alpn_are_held_then_replayed_under_the_settled_draft():
    s = _session(settled=False)
    s._ingest_stream_data(3, D18_CONTROL_HEAD + SETUP_BODY[:2], False)
    s._ingest_stream_data(3, SETUP_BODY[2:], False)
    assert s.routed == []
    assert bytes(s._pre_draft_stream_data[3][0]) == D18_CONTROL_HEAD + SETUP_BODY

    s._settle_draft(18)

    assert s._draft_settled and s.negotiated_draft == 18
    assert s._pre_draft_stream_data == {}
    assert s._d18_control_read_sid == 3
    assert s.routed == [("control", 3, D18_CONTROL_HEAD + SETUP_BODY, False)]


def test_settled_session_routes_immediately():
    s = _session(settled=True)
    s.negotiated_draft = 18
    s._ingest_stream_data(3, D18_CONTROL_HEAD + SETUP_BODY, False)
    s._ingest_stream_data(0, b"\x03\x00\x01", False)     # d18 request bidi
    assert [r[:2] for r in s.routed] == [("control", 3), ("control", 0)]
    assert s._pre_draft_stream_data == {}


def test_webtransport_session_never_holds():
    s = _session(settled=False)
    s._is_wt = True
    s._ingest_stream_data(3, b"\x10\x00", False)          # d14 subgroup stream
    assert s.routed == [("data", 3, b"\x10\x00", False)]
    assert s._pre_draft_stream_data == {}
