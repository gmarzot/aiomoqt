"""d14/d16 MAX_REQUEST_ID (§9.5): a peer request at or beyond our
advertised ceiling closes the session with TOO_MANY_REQUESTS; we raise
the ceiling before a well-behaved peer reaches it; we never allocate
past the peer's ceiling (REQUESTS_BLOCKED once, then refuse) until a
MAX_REQUEST_ID raises it, and a non-increasing MAX_REQUEST_ID is a
protocol violation. d18 has no ceiling."""
import asyncio
from collections import deque

import pytest

from aiomoqt.context import profile_for
from aiomoqt.messages.subscribe import MaxSubscribeId, TrackStatus
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.types import MOQTRequestError, SessionCloseCode, SetupParamType
from aiopquic.buffer import Buffer


def _stub(draft):
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s.is_client = True
    s._next_request_id = 0
    s._sent_requests = deque(maxlen=64)
    s._pending_requests = {}
    s._peer_request_max = -1
    s._peer_request_seen = set()
    s._track_aliases = {}
    s._subscriptions = {}
    s._request_cancel_handlers = {}
    s._publish_done_handlers = {}
    s._control_msg_overrides = {}
    s._tasks = set()
    s._loop = asyncio.get_running_loop()
    s._bidi_streams = {}
    s._bidi_stream_requests = {}
    s._tx_updates = {}
    s.sent = []
    s.closed = []
    s.send_control_message = lambda m: s.sent.append(m)
    s._close_session = lambda code, reason: s.closed.append((code, reason))
    return s


def _feed(s, rid):
    msg = TrackStatus(request_id=rid, track_namespace=(b"n",), track_name=b"t")
    raw = bytes(msg.serialize(prof=s._profile).data)
    s._moqt_handle_control_message(Buffer(data=raw, vi64=s._profile.vi64),
                                   request_id=None)


@pytest.mark.asyncio
async def test_peer_request_at_our_ceiling_closes_with_too_many_requests():
    s = _stub(16)
    s._local_request_max = 8
    s.REQUEST_ID_WINDOW = 4
    _feed(s, 1)
    assert not s.closed and s._local_request_max == 8
    _feed(s, 9)
    assert s.closed and s.closed[0][0] == SessionCloseCode.TOO_MANY_REQUESTS


@pytest.mark.asyncio
async def test_ceiling_is_raised_before_the_peer_reaches_it():
    s = _stub(16)
    s._local_request_max = 8
    s.REQUEST_ID_WINDOW = 8
    _feed(s, 1)
    assert s._local_request_max == 8
    _feed(s, 5)                                   # within half a window
    assert s._local_request_max == 16
    raised = [m for m in s.sent if isinstance(m, MaxSubscribeId)]
    assert [m.request_id for m in raised] == [16]


@pytest.mark.asyncio
async def test_we_stop_at_the_peers_ceiling_until_it_is_raised():
    s = _stub(16)
    s._peer_request_limit = 2
    assert s._allocate_request_id() == 0
    with pytest.raises(MOQTRequestError):
        s._allocate_request_id()
    with pytest.raises(MOQTRequestError):
        s._allocate_request_id()
    blocked = [m for m in s.sent if type(m).__name__ == "SubscribesBlocked"]
    assert len(blocked) == 1 and blocked[0].maximum_request_id == 2
    await s._handle_max_request_id(MaxSubscribeId(request_id=6))
    assert s._allocate_request_id() == 2
    await s._handle_max_request_id(MaxSubscribeId(request_id=6))
    assert s.closed and s.closed[0][0] == SessionCloseCode.PROTOCOL_VIOLATION


@pytest.mark.asyncio
async def test_setup_parameter_sets_the_peer_ceiling_pre_d18_only():
    s = _stub(16)
    s._ingest_request_limit({SetupParamType.MAX_REQUEST_ID: 50})
    assert s._peer_request_limit == 50
    s = _stub(18)
    s._ingest_request_limit({SetupParamType.MAX_REQUEST_ID: 50})
    assert s._peer_request_limit is None
    s = _stub(16)
    s._ingest_request_limit({})
    assert s._peer_request_limit is None                 # absent: unlimited


@pytest.mark.asyncio
async def test_d18_has_no_ceiling():
    s = _stub(18)
    assert s._local_request_max is None
    s._extend_request_credit(10 ** 6)
    assert s.sent == []
