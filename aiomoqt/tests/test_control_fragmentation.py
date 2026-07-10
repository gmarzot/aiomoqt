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

from aiomoqt.protocol import _MOQTSessionMixin, SessionCloseCode
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
    s._closed = []
    s._close_session = lambda code, reason: s._closed.append((code, reason))
    return s


def _spy_parses(s):
    """Record every whole control message _on_control_data parses."""
    parsed = []
    real = s._moqt_handle_control_message

    def spy(buf, request_id=None):
        msg = real(buf, request_id=request_id)
        if msg is not None:
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
