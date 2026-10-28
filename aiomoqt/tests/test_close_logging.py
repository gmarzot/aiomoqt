"""The session close line names the side that closed, the code, the peer
and whether MoQT setup completed; its level follows who closed and why."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from aiomoqt.protocol import _MOQTSessionMixin
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
    lines = [r for r in caplog.records if r.getMessage().startswith("MOQT: ")
             and ("closing:" in r.getMessage() or "peer closed:" in r.getMessage())]
    assert len(lines) == 1, [r.getMessage() for r in caplog.records]
    return lines[0]


@pytest.mark.parametrize("code, level, label", [
    pytest.param(0, logging.INFO, "NO_ERROR (0x0)", id="peer-0"),
    pytest.param(0x100, logging.INFO, "H3_NO_ERROR (0x100)", id="peer-h3-no-error"),
    pytest.param(0x106, logging.WARNING, "H3_FRAME_ERROR (0x106)", id="peer-h3-frame"),
])
async def test_peer_close_on_webtransport(records, code, level, label):
    s = _stub()
    s._close_session(code, "WebTransport session closed", transmit=False)
    rec = _close_line(records)
    assert rec.levelno == level
    msg = rec.getMessage()
    assert msg.startswith(f"MOQT: peer closed: {label} from 158.23.190.70:6983")
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
