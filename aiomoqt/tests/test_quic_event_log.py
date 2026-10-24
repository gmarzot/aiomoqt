"""Events a session handles are not logged as unhandled."""
import logging
from types import SimpleNamespace

import aiomoqt.protocol as protocol_mod
from aiomoqt.protocol import _MOQTSessionMixin


class _Lines(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def test_ingested_stream_data_is_not_logged_unhandled():
    s = object.__new__(_MOQTSessionMixin)
    s._wt_session_setup = SimpleNamespace(done=lambda: True)
    s._close_err = None
    s._session_id = 0
    got = []
    s._ingest_stream_data = lambda sid, data, end: got.append((sid, data, end))
    log = protocol_mod.logger
    lines = _Lines()
    level = log.level
    log.setLevel(logging.DEBUG)
    log.addHandler(lines)
    try:
        s._quic_event(protocol_mod.StreamDataReceived(
            stream_id=2, data=b"x", end_stream=False))
    finally:
        log.removeHandler(lines)
        log.setLevel(level)
    assert got == [(2, b"x", False)]
    assert not any("event not handled" in m for m in lines.lines), lines.lines


def test_handshake_completed_is_not_logged_unhandled():
    s = object.__new__(_MOQTSessionMixin)
    log = protocol_mod.logger
    lines = _Lines()
    level = log.level
    log.setLevel(logging.DEBUG)
    log.addHandler(lines)
    try:
        s._quic_event(protocol_mod.HandshakeCompleted(
            alpn_protocol="moqt-18", early_data_accepted=False,
            session_resumed=False))
    finally:
        log.removeHandler(lines)
        log.setLevel(level)
    assert lines.lines == ["QUIC: handshake complete"], lines.lines
