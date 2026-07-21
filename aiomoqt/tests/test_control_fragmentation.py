"""Control-stream reassembly across StreamDataReceived events.

QUIC delivers stream bytes in arbitrary chunks, so a control message — even
just its type-vint + 2-byte length header — can arrive split across events.
The control path must accumulate and parse only whole messages, retaining any
partial trailing message for the next chunk (the same save/rollback/commit
reassembly the data plane already uses). Regression for the d18
`BufferReadError: read out of bounds` at `_moqt_handle_control_message`'s
`pull_uint16()` when a fragmenting peer (moq-dev-js) split a control message.
"""
import asyncio
from collections import deque

import pytest

from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.messages.request import RequestOk
from aiomoqt.context import profile_for


def _control_session(draft):
    """Minimal session wired for _on_control_data: request/response state
    plus the control-chain map, bidi-binding maps, and a recording
    _close_session (bypasses __init__ / QUIC)."""
    s = object.__new__(_MOQTSessionMixin)
    s._next_request_id = 0
    s._sent_requests = deque(maxlen=1024)
    s._pending_requests = {}
    s._next_track_alias = 0
    s._track_aliases = {}
    s._loop = asyncio.get_running_loop()
    s._sent = []
    s._send_request = lambda rid, msg: s._sent.append((rid, msg))
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s._control_msg_overrides = {}
    s._tasks = set()
    s._control_chains = {}
    s._bidi_stream_requests = {}
    s._bidi_streams = {}
    s._d18_control_read_sid = None
    s._control_stream_id = None
    s._closed = []
    s._close_session = lambda code, reason: s._closed.append((code, reason))
    return s


def _spy_parses(s):
    """Record every whole control message _on_control_data parses."""
    parsed = []
    real = s._moqt_handle_control_message

    def spy(buf, request_id=None):
        msg = real(buf, request_id=request_id)
        if msg is not None and msg is not s._MSG_SKIPPED:
            parsed.append(msg)
        return msg

    s._moqt_handle_control_message = spy
    return parsed


@pytest.mark.parametrize("draft", [14, 16, 18])
@pytest.mark.parametrize("cut", [1, 2, 3])
async def test_header_split_reassembles(draft, cut):
    # Split inside the type+length header — the exact d18 crash shape.
    s = _control_session(draft)
    parsed = _spy_parses(s)
    wire = bytes(RequestOk(request_id=7, parameters={}).serialize(
        prof=s._profile).data)
    assert len(wire) > cut

    s._on_control_data(0, wire[:cut], False)
    assert parsed == []            # partial header — nothing parsed yet
    assert s._closed == []         # and NOT treated as a protocol error

    s._on_control_data(0, wire[cut:], False)
    assert len(parsed) == 1        # reassembled into exactly one message
    assert s._closed == []


@pytest.mark.parametrize("draft", [14, 16, 18])
async def test_body_split_reassembles(draft):
    s = _control_session(draft)
    parsed = _spy_parses(s)
    wire = bytes(RequestOk(request_id=7, parameters={}).serialize(
        prof=s._profile).data)
    cut = len(wire) - 1            # header complete, body one byte short

    s._on_control_data(0, wire[:cut], False)
    assert parsed == []
    assert s._closed == []

    s._on_control_data(0, wire[cut:], False)
    assert len(parsed) == 1
    assert s._closed == []


@pytest.mark.parametrize("draft", [14, 16, 18])
async def test_whole_message_single_event(draft):
    s = _control_session(draft)
    parsed = _spy_parses(s)
    wire = bytes(RequestOk(request_id=7, parameters={}).serialize(
        prof=s._profile).data)
    s._on_control_data(0, wire, False)
    assert len(parsed) == 1
    assert s._closed == []


@pytest.mark.parametrize("draft", [14, 16, 18])
async def test_two_messages_one_event(draft):
    s = _control_session(draft)
    parsed = _spy_parses(s)
    one = bytes(RequestOk(request_id=7, parameters={}).serialize(
        prof=s._profile).data)
    s._on_control_data(0, one + one, False)
    assert len(parsed) == 2        # both whole messages drained
    assert s._closed == []


@pytest.mark.parametrize("draft", [16, 18])
async def test_request_bidi_split_reassembles(draft):
    # The request bidi streams take the same reassembly path: a reply
    # split across events on a bound request stream reassembles.
    s = _control_session(draft)
    parsed = _spy_parses(s)
    s._bidi_stream_requests[5] = 7   # stream bound to request 7
    wire = bytes(RequestOk(request_id=7, parameters={}).serialize(
        prof=s._profile).data)
    s._on_control_data(5, wire[:2], False, is_request_bidi=True)
    assert parsed == []
    assert s._closed == []
    s._on_control_data(5, wire[2:], False, is_request_bidi=True)
    assert len(parsed) == 1
    assert s._closed == []


# --- lifecycle: classification, FIN handling, containment ------------------

async def test_stash_survives_control_binding_on_other_stream():
    # Stream A stashes a partial type vint; stream B then binds as the
    # control read-uni. A's continuation must still merge the stashed
    # prefix and reach the data path with ALL its bytes.
    s = _control_session(18)
    s._uni_peek_stash = {}
    s._data_streams = {}
    s._stream_torn_down = set()
    s._stream_torn_down = {}
    nine = b"\xff" + (12345).to_bytes(8, "big")   # 9-byte vi64, not SETUP
    assert s._classify_d18_uni(2, nine[:1], False) is None   # stashed
    assert 2 in s._uni_peek_stash
    setup_wire = b"\xaf\x00"                       # vi64 0x2F00
    assert s._classify_d18_uni(6, setup_wire, False) == setup_wire
    assert s._d18_control_read_sid == 6            # control bound on B
    got = s._classify_d18_uni(2, nine[1:], False)  # A's continuation
    assert got == nine                             # prefix restored
    assert 2 not in s._uni_peek_stash


async def test_stash_dropped_on_fin_and_cleanup():
    s = _control_session(18)
    s._uni_peek_stash = {}
    s._data_streams = {}
    s._stream_torn_down = set()
    # FIN mid-type-vint: undecodable, dropped without stashing.
    assert s._classify_d18_uni(2, b"\xff", True) is None
    assert 2 not in s._uni_peek_stash
    # Reset/teardown pops a pending stash entry.
    s._uni_peek_stash[10] = b"\xff"
    s._control_chains = {}
    s._mark_stream_torn_down = lambda sid: None
    s._unbind_key = lambda key: None
    s._fetch_done_futures = {}
    s._cleanup_stream(10)
    assert 10 not in s._uni_peek_stash


async def test_fin_with_truncated_message_pops_chain():
    # FIN'd mid-message: nothing more can arrive — the chain must be
    # released (not pinned until session close), and the control stream
    # case is a protocol violation.
    s = _control_session(18)
    wire = bytes(RequestOk(request_id=7, parameters={}).serialize(
        prof=s._profile).data)
    s._on_control_data(5, wire[:2], True)          # header only + FIN
    assert 5 not in s._control_chains              # released, not pinned


async def test_parse_exception_contained_not_escaped():
    # A non-underflow deserialize exception must not escape into the
    # event loop (WT would drop the rest of the batch) — it closes the
    # session with forensics instead.
    s = _control_session(18)

    def boom(buf, request_id=None):
        raise ValueError("boom")
    s._moqt_handle_control_message = boom
    s._on_control_data(0, b"\x01\x02\x03", False)  # must not raise
    assert s._closed


async def test_unknown_type_skipped_session_survives():
    # d16 request-bidi: an unknown-but-length-delimited control type is
    # skipped per the lenient surface; a following message still parses
    # and the session stays up.
    from aiomoqt.types import MOQTMessageType
    s = _control_session(16)
    parsed = _spy_parses(s)
    unknown_t = next(t for t in range(0x21, 0x3f)
                     if t not in MOQTMessageType._value2member_map_)
    frame = bytes([unknown_t]) + (3).to_bytes(2, "big") + b"abc"
    good = bytes(RequestOk(request_id=7, parameters={}).serialize(
        prof=s._profile).data)
    s._bidi_stream_requests[5] = 7
    s._on_control_data(5, frame + good, False, is_request_bidi=True)
    assert len(parsed) == 1                        # the good one
    assert s._closed == []                         # session survived


# --- exception taxonomy: malformation is not fragmentation -----------------
# The declared-length guard is the ONLY legitimate "wait for more bytes"
# signal. Once a message's full declared body is present, a short read
# inside deserialize means the peer sent a malformed body — waiting for
# more bytes would stall the control stream forever.

@pytest.mark.parametrize("draft", [14, 16, 18])
async def test_malformed_body_closes_not_stalls(draft):
    # Draft-portable malformation: declare a zero-length body — the frame
    # is complete per the header, but every draft's decoder must pull at
    # least the request_id, which reads past the declared end.
    from aiomoqt.utils.buffer import Buffer
    s = _control_session(draft)
    parsed = _spy_parses(s)
    wire = bytes(RequestOk(request_id=7, parameters={}).serialize(
        prof=s._profile).data)
    hdr = Buffer(data=wire)
    hdr.vi64 = s._profile.vi64
    hdr.pull_vint()
    hdr.pull_uint16()
    wire = wire[:hdr.tell() - 2] + b"\x00\x00"   # type + len=0, no body
    s._on_control_data(0, wire, False)
    assert parsed == []
    assert s._closed                # protocol violation, not a silent wait


async def test_peek_nine_byte_vi64():
    # vi64 encodes in up to NINE bytes (0xFF prefix + 8); d18 decoders
    # must accept non-minimal encodings. An 8-byte prefix of a 9-byte
    # SETUP type is undecidable (None), never False.
    from aiomoqt.types import MOQTMessageType
    nine = b"\xff" + int(MOQTMessageType.SETUP).to_bytes(8, "big")
    peek = _MOQTSessionMixin._d18_peek_is_setup
    assert peek(nine[:8]) is None
    assert peek(nine) is True
    assert peek(b"\xff" + (12345).to_bytes(8, "big")) is False


def _exts_chain(data):
    from aiopquic.streamchain import StreamChain
    chain = StreamChain()
    chain.extend(data)
    return chain


@pytest.mark.parametrize("make_buf", [bytes, _exts_chain],
                         ids=["buffer", "chain"])
def test_truncated_extensions_tolerated_both_buffers(make_buf):
    # The lenient-extensions tolerance (d16 §9.13 truncated trailing KVP,
    # seen live from moq-rs-d16) must fire whether the message parses
    # from a Buffer (BufferReadError) or a StreamChain (StreamUnderflow).
    from aiomoqt.messages.base import MOQTMessage
    from aiomoqt.utils.buffer import Buffer
    # one whole KVP (id=1 odd, len=2, b"ab") + one truncated (id=3, len=5)
    data = bytes([1, 2]) + b"ab" + bytes([3, 5]) + b"xy"
    src = make_buf(data)
    buf = Buffer(data=src) if isinstance(src, bytes) else src
    prev = MOQTMessage._tolerate_trailing_extensions
    MOQTMessage._tolerate_trailing_extensions = True
    try:
        exts = MOQTMessage._extensions_decode(
            buf, with_length=False, buf_end=len(data))
    finally:
        MOQTMessage._tolerate_trailing_extensions = prev
    assert exts == {1: b"ab"}       # partial dict returned, no exception


@pytest.mark.parametrize("draft", [14, 16, 18])
async def test_message_and_a_half(draft):
    # One whole message plus a partial: parse the first, retain the rest.
    s = _control_session(draft)
    parsed = _spy_parses(s)
    one = bytes(RequestOk(request_id=7, parameters={}).serialize(
        prof=s._profile).data)
    s._on_control_data(0, one + one[:2], False)
    assert len(parsed) == 1        # only the whole one
    assert s._closed == []
    s._on_control_data(0, one[2:], False)
    assert len(parsed) == 2        # remainder completed
    assert s._closed == []
