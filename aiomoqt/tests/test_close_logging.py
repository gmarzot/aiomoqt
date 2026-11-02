"""The session close line names the side that closed, the code, the peer
and whether MoQT setup completed; its level follows who closed and why.
A session logs one close line; the peer's reason is logged escaped."""
import asyncio
import contextlib
import dataclasses
import logging
import os
import re
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from aiopquic.quic.configuration import QuicConfiguration
from aiopquic.quic.events import ConnectionTerminated, WebTransportSessionClosed

import aiomoqt.server

from aiomoqt.client import MOQTClient
from aiomoqt.messages.base import MOQTMessage
from aiomoqt.messages.subscribe import TrackStatus
from aiomoqt.protocol import _MOQTSessionMixin, _close_kind, _local_cause
from aiomoqt.server import MOQTServer
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._procs import free_udp_port
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


_SIDES = ("MOQT: closing:", "MOQT: peer closed:", "MOQT: connection closed:")


def _close_line(caplog):
    lines = [r for r in caplog.records if r.getMessage().startswith(_SIDES)]
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


@pytest.mark.parametrize("is_wt", [False, True], ids=["quic", "wt"])
async def test_our_goaway_timeout_close_logs_info(records, is_wt):
    s = _stub(is_wt=is_wt, setup_done=True)
    s._close_session(SessionCloseCode.GOAWAY_TIMEOUT, "relay restarting")
    rec = _close_line(records)
    assert rec.levelno == logging.INFO
    assert rec.getMessage() == ("MOQT: closing: GOAWAY_TIMEOUT (0x10) to "
                                "158.23.190.70:6983; reason: relay restarting")


async def test_peer_goaway_timeout_close_logs_warning(records):
    s = _stub(is_wt=False, setup_done=True)
    s._close_session(SessionCloseCode.GOAWAY_TIMEOUT, "", transmit=False)
    assert _close_line(records).levelno == logging.WARNING


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
    # Code 0 with no local cause: the side is unknown.
    s = _stub(is_wt=False, setup_done=True)
    s._quic_event(ConnectionTerminated(error_code=0))
    rec = _close_line(records)
    assert rec.levelno == logging.INFO
    assert rec.getMessage().startswith("MOQT: connection closed: NO_ERROR (0x0)")
    assert "peer closed" not in records.text


@pytest.mark.parametrize("is_wt", [False, True], ids=["quic", "wt"])
@pytest.mark.parametrize("local, level, label", [
    (0x433, logging.INFO, "idle timeout"),
    (0x41e, logging.WARNING, "local error 0x41e"),
])
async def test_a_local_close_names_its_cause(records, is_wt, local, level,
                                             label):
    s = _stub(is_wt=is_wt, setup_done=True)
    s._quic_event(SimpleNamespace(error_code=0, frame_type=0,
                                  local_error=local))
    rec = _close_line(records)
    assert rec.levelno == level
    assert rec.getMessage() == (f"MOQT: connection closed: {label} "
                                "peer 158.23.190.70:6983")


async def test_a_peer_code_wins_over_a_local_cause(records):
    s = _stub(is_wt=False, setup_done=True)
    s._quic_event(SimpleNamespace(error_code=0x3, local_error=0x433))
    assert _close_line(records).getMessage().startswith(
        "MOQT: peer closed: PROTOCOL_VIOLATION (0x3) from")


@pytest.mark.parametrize("event, cause", [
    (SimpleNamespace(local_error=0x433), "idle timeout"),
    (SimpleNamespace(local_error=0x41e), "local error 0x41e"),
    (SimpleNamespace(local_error=0x41e, local_reason="disconnected"),
     "disconnected"),
    (SimpleNamespace(local_error=0), None),
    (SimpleNamespace(error_code=0), None),
    (None, None),
])
def test_local_cause(event, cause):
    assert _local_cause(event) == cause


async def test_raw_quic_transport_close_names_the_transport_code(records):
    s = _stub(is_wt=False)
    s._quic_event(ConnectionTerminated(error_code=0x2, frame_type=0))
    rec = _close_line(records)
    assert rec.levelno == logging.WARNING
    assert rec.getMessage().startswith(
        "MOQT: peer closed: CONNECTION_REFUSED (0x2) from")
    assert s._close_err == (0x2, "")
    assert "; reason:" not in _close_line(records).getMessage()


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


@pytest.mark.parametrize("is_wt", [False, True], ids=["quic", "wt"])
async def test_peer_close_without_a_reason_logs_none(records, is_wt):
    s = _stub(is_wt=is_wt, setup_done=True)
    s._quic_event(SimpleNamespace(error_code=0x10))
    msg = _close_line(records).getMessage()
    assert "; reason:" not in msg and "SimpleNamespace" not in msg, msg
    assert s._close_err == (0x10, "")


async def test_local_close_without_a_reason_logs_none(records):
    s = _stub(is_wt=False, setup_done=True)
    s._close_session()
    assert _close_line(records).getMessage() == (
        "MOQT: closing: NO_ERROR (0x0) to 158.23.190.70:6983")


async def test_close_line_keeps_the_recorded_peer_address(records):
    s = _stub(is_wt=False, setup_done=True)
    s._note_peer_address()
    s._quic = SimpleNamespace(peer_address=lambda: None)
    s._close_session(0x10, "", transmit=False)
    assert "from 158.23.190.70:6983" in _close_line(records).getMessage()


async def test_a_second_close_logs_nothing(records):
    s = _stub(is_wt=False, setup_done=True)
    s._close_session(SessionCloseCode.PROTOCOL_VIOLATION, "bad")
    s._quic_event(ConnectionTerminated(error_code=0x3, reason_phrase="bad"))
    _close_line(records)


@pytest.mark.parametrize("reason, logged", [
    ("\x1b[31mred\x1b[0m", "\\x1b[31mred\\x1b[0m"),
    ("one\r\nMOQT: fake line\x00", "one\\x0d\\x0aMOQT: fake line\\x00"),
    ("caf\u00e9 \u202e", "caf\\xe9 \\u202e"),
    (b"\xff\x7f", "\\xff\\x7f"),
])
async def test_peer_reason_is_logged_escaped(records, reason, logged):
    s = _stub(is_wt=False, setup_done=True)
    s._close_session(0x10, reason, transmit=False)
    msg = _close_line(records).getMessage()
    assert msg.endswith(f"; reason: {logged}"), msg
    assert s._close_err == (0x10, reason)


async def test_peer_reason_is_cut_to_the_reason_phrase_max(records):
    s = _stub(is_wt=False, setup_done=True)
    s._close_session(0x10, "x" * 5000, transmit=False)
    msg = _close_line(records).getMessage()
    assert msg.endswith("; reason: " + "x" * MOQTMessage.REASON_PHRASE_MAX)


# Real sessions over raw QUIC and WebTransport.

@contextlib.asynccontextmanager
async def _pair(use_quic, draft=22):
    """A set-up client session and the server's side of it."""
    port = free_udp_port()
    server = MOQTServer(host="127.0.0.1", port=port, certificate=CERT,
                        private_key=KEY, path="/", supported_drafts=draft)
    handle = await server.serve_dual()
    try:
        client = MOQTClient("127.0.0.1", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            await session.client_session_init()
            async with asyncio.timeout(5):
                while not server._live:
                    await asyncio.sleep(0.01)
            yield session, next(iter(server._live)), port
    finally:
        handle.close()


async def _closed(*sessions):
    async with asyncio.timeout(5):
        for s in sessions:
            await s.async_closed()
    await asyncio.sleep(0.3)  # a late transport event would log here


def _lines(caplog, port=None):
    """Close lines; with port, those naming that peer port."""
    out = [r.getMessage() for r in caplog.records
           if r.getMessage().startswith(_SIDES)]
    if port is not None:
        out = [m for m in out if re.search(rf"127\.0\.0\.1:{port}\b", m)]
    return out


_REAL = [
    pytest.param(True, id="quic"),
    pytest.param(False, id="wt"),
]


@requires_certs
@pytest.mark.parametrize("use_quic", _REAL)
async def test_session_close_without_a_reason(records, use_quic):
    async with _pair(use_quic) as (client, peer, port):
        peer.close(SessionCloseCode.GOAWAY_TIMEOUT)
        await _closed(client, peer)
    seen = _lines(records, port)
    assert seen == [f"MOQT: peer closed: GOAWAY_TIMEOUT (0x10) from "
                    f"127.0.0.1:{port}"], _lines(records)
    assert client._close_err == (SessionCloseCode.GOAWAY_TIMEOUT, "")


@requires_certs
@pytest.mark.parametrize("use_quic", _REAL)
@pytest.mark.parametrize("how", ["close", "_close_session"])
async def test_local_close_is_one_line(records, use_quic, how):
    async with _pair(use_quic) as (client, peer, port):
        getattr(client, how)(SessionCloseCode.NO_ERROR, "bye")
        await _closed(client, peer)
    assert _lines(records, port) == [
        f"MOQT: closing: NO_ERROR (0x0) to 127.0.0.1:{port}; reason: bye"], \
        _lines(records)
    assert not any("MOQT session: closing" in r.getMessage()
                   for r in records.records)
    peer_lines = [m for m in _lines(records) if m not in _lines(records, port)]
    assert len(peer_lines) == 1, _lines(records)
    assert peer_lines[0].endswith("; reason: bye"), peer_lines


@requires_certs
@pytest.mark.parametrize("use_quic", _REAL)
async def test_a_peer_violation_logs_one_error(records, use_quic):
    async with _pair(use_quic, draft=16) as (client, peer, port):
        rid = peer._local_request_max
        client.send_control_message(TrackStatus(
            request_id=rid, track_namespace=(b"n",), track_name=b"t"))
        await _closed(client, peer)
    errors = [r.getMessage() for r in records.records
              if r.levelno >= logging.ERROR]
    assert len(errors) == 1, errors
    assert re.fullmatch(
        rf"MOQT: closing: TOO_MANY_REQUESTS \(0x7\) to 127\.0\.0\.1:\d+; "
        rf"reason: peer request_id {rid} at or beyond our MAX_REQUEST_ID {rid}",
        errors[0]), errors


@requires_certs
@pytest.mark.parametrize("use_quic", _REAL)
async def test_peer_reason_with_escapes_over_the_wire(records, use_quic):
    async with _pair(use_quic) as (client, peer, port):
        peer.close(SessionCloseCode.GOAWAY_TIMEOUT, "\x1b[2Jgone\nfake")
        await _closed(client, peer)
    assert _lines(records, port) == [
        f"MOQT: peer closed: GOAWAY_TIMEOUT (0x10) from 127.0.0.1:{port}"
        "; reason: \\x1b[2Jgone\\x0afake"], _lines(records)


_SILENT_CLIENT = r"""
import asyncio, sys
from aiomoqt.client import MOQTClient

async def main(port, use_quic):
    client = MOQTClient("127.0.0.1", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=22)
    async with client.connect() as s:
        await s.client_session_init()
        print("ready", flush=True)
        await asyncio.sleep(3600)

asyncio.run(main(int(sys.argv[1]), sys.argv[2] == "quic"))
"""


@dataclasses.dataclass
class _ShortIdle(QuicConfiguration):
    idle_timeout: float = 1.0


@requires_certs
@pytest.mark.skipif(sys.platform == "win32", reason="SIGSTOP")
@pytest.mark.parametrize("use_quic", _REAL)
async def test_idle_timeout_close_line(records, monkeypatch, use_quic):
    monkeypatch.setattr(aiomoqt.server, "QuicConfiguration", _ShortIdle)
    port = free_udp_port()
    server = MOQTServer(host="127.0.0.1", port=port, certificate=CERT,
                        private_key=KEY, path="/", supported_drafts=22)
    handle = await server.serve_dual()
    proc = None
    try:
        # Not an asyncio subprocess: on macOS py3.14 the child watcher
        # reaps a SIGSTOP'd child with a blocking waitpid on the loop.
        proc = subprocess.Popen(
            [sys.executable, "-c", _SILENT_CLIENT, str(port),
             "quic" if use_quic else "wt"],
            stdout=subprocess.PIPE, cwd=os.getcwd())
        line = await asyncio.wait_for(
            asyncio.to_thread(proc.stdout.readline), 10)
        assert line.strip() == b"ready", line
        async with asyncio.timeout(5):
            while not server._live:
                await asyncio.sleep(0.01)
        peer = next(iter(server._live))
        os.kill(proc.pid, signal.SIGSTOP)
        async with asyncio.timeout(10):
            await peer.async_closed()
        await asyncio.sleep(0.3)
    finally:
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(proc.pid, signal.SIGCONT)
            proc.kill()
            await asyncio.to_thread(proc.wait, 10)
            proc.stdout.close()
        handle.close()
    (rec,) = [r for r in records.records if r.getMessage().startswith(_SIDES)]
    assert rec.levelno == logging.INFO
    assert re.fullmatch(r"MOQT: connection closed: idle timeout "
                        r"peer 127\.0\.0\.1:\d+", rec.getMessage()), \
        rec.getMessage()
