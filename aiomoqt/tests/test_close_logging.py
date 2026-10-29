"""The session close line names the side that closed, the code, the peer
and whether MoQT setup completed; its level follows who closed and why."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from aiopquic.quic.events import ConnectionTerminated, WebTransportSessionClosed

from aiomoqt.protocol import _MOQTSessionMixin, _close_kind
from aiomoqt.types import SessionCloseCode

_PEER = ("158.23.190.70", 6983)


@pytest.fixture
def records(caplog):
    """aiomoqt loggers do not propagate: attach caplog to this one."""
    lg = logging.getLogger("aiomoqt.protocol")
    lg.addHandler(caplog.handler)
    with caplog.at_level(logging.DEBUG, logger="aiomoqt.protocol"):
        yield caplog
    lg.removeHandler(caplog.handler)


def _stub(is_wt=True, setup_done=False):
    s = object.__new__(_MOQTSessionMixin)
    loop = asyncio.get_running_loop()
    s._is_wt = is_wt
    s._transport = SimpleNamespace(peer_address=lambda ptr: _PEER)
    s.cnx_ptr = 1
    s._quic = SimpleNamespace(peer_address=lambda: _PEER)
    s._close_err = None
    s._pending_requests = {}
    s._fetch_done_futures = {}
    s._reaper_handle = None
    s._data_streams = {}
    s._control_chains = {}
    s._pending_control_msgs = []
    s._uni_peek_stash = {}
    s._published_tracks = {}
    s._requests = {}
    s._early_stream_ends = {}
    s._wt_session_setup = loop.create_future()
    s._moqt_session_setup = loop.create_future()
    if setup_done:
        s._moqt_session_setup.set_result(True)
    s._moqt_session_closed = loop.create_future()
    s.sent = []
    s.close = lambda *a, **kw: s.sent.append(a)
    return s


def _close_line(caplog):
    sides = ("MOQT: closing:", "MOQT: peer closed:", "MOQT: connection closed:")
    lines = [r for r in caplog.records if r.getMessage().startswith(sides)]
    assert len(lines) == 1, [r.getMessage() for r in caplog.records]
    return lines[0]


@pytest.mark.parametrize("code, level, head", [
    pytest.param(0, logging.INFO, "connection closed: NO_ERROR (0x0) peer",
                 id="peer-0"),
    pytest.param(0x100, logging.INFO, "peer closed: H3_NO_ERROR (0x100) from",
                 id="peer-h3-no-error"),
    pytest.param(0x106, logging.WARNING, "peer closed: H3_FRAME_ERROR (0x106) from",
                 id="peer-h3-frame"),
])
async def test_peer_close_on_webtransport(records, code, level, head):
    s = _stub()
    s._close_session(code, "WebTransport session closed", transmit=False)
    rec = _close_line(records)
    assert rec.levelno == level
    msg = rec.getMessage()
    assert msg.startswith(f"MOQT: {head} 158.23.190.70:6983")
    assert ", before MoQT setup" in msg
    assert s.sent == []
    assert s._close_err == (code, "WebTransport session closed")


async def test_local_error_close_logs_error(records):
    s = _stub(is_wt=False, setup_done=True)
    s._close_session(SessionCloseCode.PROTOCOL_VIOLATION, "bad message")
    rec = _close_line(records)
    assert rec.levelno == logging.ERROR
    assert rec.getMessage() == ("MOQT: closing: PROTOCOL_VIOLATION (0x3) "
                                "to 158.23.190.70:6983; reason: bad message")
    assert s.sent == [()]


async def test_h3_codes_are_not_named_on_raw_quic(records):
    s = _stub(is_wt=False, setup_done=True)
    s._close_session(0x100, "x", transmit=False)
    rec = _close_line(records)
    assert rec.levelno == logging.WARNING
    assert "peer closed: 0x100 from" in rec.getMessage()


async def test_first_close_wins():
    s = _stub()
    s._close_session(0x106, "first", transmit=False)
    s._close_session(SessionCloseCode.INTERNAL_ERROR, "second")
    assert s._close_err == (0x106, "first")
    assert s.sent == []


async def test_raw_quic_peer_error_close_logs_one_warning(records):
    s = _stub(is_wt=False, setup_done=True)
    s._quic_event(ConnectionTerminated(error_code=0x3, reason_phrase="bad"))
    errors = [r for r in records.records if r.levelno >= logging.WARNING]
    assert [r.getMessage() for r in errors] == [
        "MOQT: peer closed: PROTOCOL_VIOLATION (0x3) from "
        "158.23.190.70:6983; reason: bad"]
    assert errors[0].levelno == logging.WARNING


async def test_transport_close_without_a_code_is_not_called_a_peer_close(records):
    # A local idle timeout arrives as code 0: the side is unknown.
    s = _stub(is_wt=False, setup_done=True)
    s._quic_event(ConnectionTerminated(error_code=0))
    rec = _close_line(records)
    assert rec.levelno == logging.INFO
    assert rec.getMessage().startswith("MOQT: connection closed: NO_ERROR (0x0)")
    assert "peer closed" not in records.text


async def test_raw_quic_transport_close_names_the_transport_code(records):
    s = _stub(is_wt=False)
    s._quic_event(ConnectionTerminated(error_code=0x2, frame_type=0))
    rec = _close_line(records)
    assert rec.levelno == logging.WARNING
    assert rec.getMessage().startswith(
        "MOQT: peer closed: CONNECTION_REFUSED (0x2) from")
    assert s._close_err == (0x2, "ConnectionTerminated")


async def test_raw_quic_application_close_keeps_the_moqt_name(records):
    s = _stub(is_wt=False, setup_done=True)
    s._quic_event(ConnectionTerminated(error_code=0x10, frame_type=None,
                                       reason_phrase="drain"))
    assert _close_line(records).getMessage().startswith(
        "MOQT: peer closed: GOAWAY_TIMEOUT (0x10) from")
    assert s._close_err == (0x10, "drain")


@pytest.mark.parametrize("is_wt", [False, True], ids=["quic", "wt"])
async def test_event_without_close_kind_keeps_moqt_naming(records, is_wt):
    s = _stub(is_wt=is_wt)
    s._quic_event(SimpleNamespace(error_code=0x2))
    assert "peer closed: UNAUTHORIZED (0x2) from" in \
        _close_line(records).getMessage()


@pytest.mark.parametrize("event, kind", [
    (SimpleNamespace(error_code=0x2), None),
    (ConnectionTerminated(error_code=0x2), None),
    (ConnectionTerminated(error_code=0x2, frame_type=0), "transport"),
    (WebTransportSessionClosed(error_code=0x10), None),
    (SimpleNamespace(error_code=0x10, connection_closed=False,
                     frame_type=None), None),
    (SimpleNamespace(error_code=0x2, connection_closed=True, frame_type=0),
     "transport"),
    (SimpleNamespace(error_code=0x106, connection_closed=True,
                     frame_type=None), "connection"),
    (None, None),
])
def test_close_kind(event, kind):
    assert _close_kind(event) == kind


@pytest.mark.parametrize("kind, code, head", [
    ("transport", 0x2, "CONNECTION_REFUSED (0x2)"),
    ("transport", 0x12a, "CRYPTO_ERROR (0x12a)"),
    ("connection", 0x106, "H3_FRAME_ERROR (0x106)"),
    ("connection", 0x2, "0x2"),
    (None, 0x2, "UNAUTHORIZED (0x2)"),
])
async def test_webtransport_close_names_by_kind(records, kind, code, head):
    s = _stub()
    s._close_session(code, "x", transmit=False, kind=kind)
    assert _close_line(records).getMessage().startswith(
        f"MOQT: peer closed: {head} from")
