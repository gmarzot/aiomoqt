"""d14/d16 MAX_REQUEST_ID (§9.5): a peer request at or beyond our
advertised ceiling closes the session with TOO_MANY_REQUESTS; we raise
the ceiling before a well-behaved peer reaches it; we never allocate
past the peer's ceiling (REQUESTS_BLOCKED once, then refuse) until a
MAX_REQUEST_ID raises it, and a non-increasing MAX_REQUEST_ID is a
protocol violation. The ceiling is raised only while the peer holds
fewer than MAX_OPEN_PEER_REQUESTS open. d18 has no ceiling."""
import asyncio
from collections import deque

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.messages.subscribe import MaxSubscribeId, TrackStatus
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.server import MOQTServer
from aiomoqt.types import (MOQTMessageType, MOQTRequestError, RequestErrorCode,
                           SessionCloseCode, SetupParamType)
from aiopquic.buffer import Buffer

from aiomoqt.tests._certs import CERT, KEY, requires_certs


def _stub(draft):
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s.is_client = True
    s._next_request_id = 0
    s._sent_requests = deque(maxlen=64)
    s._track_status_requests = set()
    s._announced = {}
    s._discovery_subs = {}
    s._pending_requests = {}
    s._peer_requests = {}
    s._requests = {}
    s._peer_request_max = -1
    s._peer_request_low = 0
    s._peer_request_seen = set()
    s._track_aliases = {}
    s._published_tracks = {}
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
async def test_request_at_ceiling_on_control_stream_closes_once(caplog):
    s = _stub(16)
    s.is_client = False                           # peer requests are even
    s._local_request_max = 8
    s._close_err = None
    s._control_chains = {}
    s._control_stream_id = 0

    def close(code, reason):
        s.closed.append((code, reason))
        s._close_err = s._close_err or (code, reason)
    s._close_session = close
    msg = TrackStatus(request_id=8, track_namespace=(b"n",), track_name=b"t")
    s._on_control_data(0, bytes(msg.serialize(prof=s._profile).data), False)
    assert [c for c, _ in s.closed] == [SessionCloseCode.TOO_MANY_REQUESTS]
    assert "parse failed" not in caplog.text


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
    assert s._peer_request_limit == 0                    # absent: 0


@pytest.mark.asyncio
async def test_d18_has_no_ceiling():
    s = _stub(18)
    assert s._local_request_max is None
    s._extend_request_credit(10 ** 6)
    assert s.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [None, {}, {SetupParamType.IMPLEMENTATION: b"x"}],
                         ids=["none", "empty", "other"])
@pytest.mark.parametrize("draft", [14, 16])
async def test_no_max_request_id_means_no_requests(draft, params):
    # §9.3.1.3: the default is 0; until MAX_REQUEST_ID nothing may be sent.
    s = _stub(draft)
    s._ingest_request_limit(params)
    with pytest.raises(MOQTRequestError):
        s.track_status("n", "t")
    assert not [m for m in s.sent if isinstance(m, TrackStatus)]
    await s._handle_max_request_id(MaxSubscribeId(request_id=2))
    s.track_status("n", "t")
    assert [m for m in s.sent if isinstance(m, TrackStatus)]


@pytest.fixture
def missing_credit(monkeypatch):
    monkeypatch.setattr(_MOQTSessionMixin, "_tolerate_missing_request_credit",
                        True)


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [14, 16])
async def test_compat_lets_requests_out_without_max_request_id(
        draft, missing_credit, caplog):
    # --compat missing-request-credit: an absent MAX_REQUEST_ID leaves our
    # requests uncapped, with one WARNING, and no REQUESTS_BLOCKED.
    s = _stub(draft)
    with caplog.at_level("WARNING", logger="aiomoqt.protocol"):
        s._ingest_request_limit({})
    assert s._peer_request_limit is None
    assert [r.message for r in caplog.records].count(
        "MOQT: peer SETUP has no MAX_REQUEST_ID; sending requests without "
        "credit (--compat missing-request-credit)") == 1
    for _ in range(3):
        s.track_status("n", "t")
    assert len([m for m in s.sent if isinstance(m, TrackStatus)]) == 3
    assert not [m for m in s.sent
                if type(m).__name__ == "SubscribesBlocked"]


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [14, 16])
async def test_compat_still_honours_a_max_request_id(draft, missing_credit):
    s = _stub(draft)
    s._ingest_request_limit({SetupParamType.MAX_REQUEST_ID: 2})
    s.track_status("n", "t")
    with pytest.raises(MOQTRequestError):
        s.track_status("n", "t")
    # One arriving later caps an uncapped session too.
    u = _stub(draft)
    u._ingest_request_limit({})
    await u._handle_max_request_id(MaxSubscribeId(request_id=2))
    u.track_status("n", "t")
    with pytest.raises(MOQTRequestError):
        u.track_status("n", "t")


def test_apply_compat_sets_missing_request_credit(monkeypatch):
    from aiomoqt.messages.base import MOQTMessage
    from aiomoqt.utils.workers import apply_compat
    # "all" also turns on lenient-extensions; restore it after the test.
    monkeypatch.setattr(MOQTMessage, "_tolerate_trailing_extensions", False)
    monkeypatch.setattr(_MOQTSessionMixin, "_tolerate_missing_request_credit",
                        False)
    apply_compat("missing-request-credit")
    assert _MOQTSessionMixin._tolerate_missing_request_credit is True
    monkeypatch.setattr(_MOQTSessionMixin, "_tolerate_missing_request_credit",
                        False)
    apply_compat("all")
    assert _MOQTSessionMixin._tolerate_missing_request_credit is True


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", [14, 16])
async def test_credit_waits_while_the_peer_holds_too_many_requests_open(
        draft, use_quic, monkeypatch, caplog):
    # Window 20: ids 0..18, a raise due from id 10 on. With 4 SUBSCRIBEs
    # held unanswered the server keeps its MAX_REQUEST_ID, the client
    # runs dry, and answering them lets credit resume.
    monkeypatch.setattr(_MOQTSessionMixin, "REQUEST_ID_WINDOW", 20)
    monkeypatch.setattr(_MOQTSessionMixin, "MAX_OPEN_PEER_REQUESTS", 4)
    monkeypatch.setattr(_MOQTSessionMixin, "CREDIT_RECHECK_S", 0.05)
    held = []

    async def hold(session, msg):
        held.append((session, msg.request_id))

    port = 26200 + 2 * (draft == 16) + (not use_quic)
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.SUBSCRIBE, hold)
    handle = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            await session.client_session_init()
            with caplog.at_level("WARNING", logger="aiomoqt.protocol"):
                for i in range(10):
                    session.subscribe("credit", f"t{i}")
                with pytest.raises(MOQTRequestError):
                    session.subscribe("credit", "dry")
                for _ in range(100):
                    if len(held) == 10:
                        break
                    await asyncio.sleep(0.02)
                assert len(held) == 10
                await asyncio.sleep(0.2)
                assert session._peer_request_limit == 20
            assert [r.message for r in caplog.records].count(
                "MOQT: peer holds 6 open requests (cap 4): MAX_REQUEST_ID "
                "held at 20") == 1
            for srv, rid in held:
                srv.subscribe_error(rid, error_code=RequestErrorCode.TIMEOUT,
                                    reason="released")
            for _ in range(100):
                if session._peer_request_limit == 40:
                    break
                await asyncio.sleep(0.02)
            assert session._peer_request_limit == 40
            session.subscribe("credit", "again")
    finally:
        handle.close()


def _reorder_stub(draft):
    s = _stub(draft)
    s._get_control_entry = lambda _: (TrackStatus, None)
    s._route_reply = lambda _: None
    s._resolve_auth_token = lambda _: None
    s._refuse_request = lambda _: None
    s._track_handler = lambda _: None
    return s


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [18, 20, 22])
async def test_request_delayed_behind_many_later_ones_is_accepted(draft):
    s = _reorder_stub(draft)
    for rid in range(3, 134, 2):
        _feed(s, rid)
    _feed(s, 1)
    assert not s.closed
    assert s._peer_request_low == 135 and not s._peer_request_seen
    _feed(s, 77)                                  # a true duplicate
    assert s.closed and s.closed[0][0] == SessionCloseCode.INVALID_REQUEST_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [18, 20, 22])
async def test_request_with_wrong_parity_closes(draft):
    s = _reorder_stub(draft)
    _feed(s, 4)
    assert s.closed and s.closed[0][0] == SessionCloseCode.INVALID_REQUEST_ID
