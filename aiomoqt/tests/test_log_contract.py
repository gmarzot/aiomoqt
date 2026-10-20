"""What a session logs at INFO: each control message once, where it
crosses the wire, plus setup and close; transport plumbing and repeated
views of a message are DEBUG. A MOQTServer quiet_peers session logs
nothing below WARNING. Raw QUIC and WebTransport."""
import contextlib
import logging

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTRequestError
from aiomoqt.utils.logger import _loggers, get_log_level, set_log_level

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 18400
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_DEBUG_ONLY = ("MOQT event: handle", "MOQT receive:", "control write-uni",
               "control stream created", "version set from",
               "ignored unknown Setup Options")


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def at(self, level):
        return [r.getMessage() for r in self.records if r.levelno == level]


@contextlib.contextmanager
def _captured(level):
    """Every aiomoqt record at `level` and above (they do not propagate
    to the root logger, so caplog never sees them)."""
    records, saved = _Records(), get_log_level()
    set_log_level(level)
    loggers = list(_loggers.values())
    for lg in loggers:
        lg.addHandler(records)
    try:
        yield records
    finally:
        for lg in loggers:
            lg.removeHandler(records)
        set_log_level(logging.INFO if saved is None else saved)


async def _session(port, use_quic, implementation=None):
    """SETUP, a SUBSCRIBE the server refuses, close."""
    kw = {} if implementation is None else {"implementation": implementation}
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=18, **kw)
    async with client.connect() as session:
        await session.client_session_init()
        with pytest.raises(MOQTRequestError):
            await session.subscribe("log/contract", "t", wait_response=True)


def _server(port, use_quic, **kw):
    return MOQTServer(host="localhost", port=port, certificate=CERT,
                      private_key=KEY, path="/", use_quic=use_quic,
                      supported_drafts=18, **kw)


def _count(messages, text):
    return sum(text in m for m in messages)


@_TRANSPORTS
@pytest.mark.asyncio
async def test_info_logs_each_control_message_once_where_it_crosses(
        use_quic):
    port = _BASE_PORT + (not use_quic)
    server = await _server(port, use_quic).serve()
    try:
        with _captured(logging.INFO) as records:
            await _session(port, use_quic)
    finally:
        server.close()
    info = records.at(logging.INFO)
    kind = "raw QUIC" if use_quic else "WT"
    assert _count(info, f"setup complete: draft-18 ({kind})") == 2
    assert _count(info, "MOQT send: Subscribe(") == 1
    assert _count(info, "control message parsed: Subscribe(") == 1
    assert _count(info, "control message parsed: Setup(") == 2
    assert not [m for m in info if any(s in m for s in _DEBUG_ONLY)]


@_TRANSPORTS
@pytest.mark.asyncio
async def test_a_quiet_peer_session_logs_nothing_below_warning(use_quic):
    port = _BASE_PORT + 2 + (not use_quic)
    server = await _server(port, use_quic, quiet_peers=("probe/",)).serve()
    try:
        with _captured(logging.INFO) as records:
            await _session(port, use_quic, implementation="probe/1")
            await _session(port, use_quic)
        with _captured(logging.DEBUG) as debug:
            await _session(port, use_quic, implementation="probe/1")
    finally:
        server.close()
    info = records.at(logging.INFO)
    # The server's view of the probe: its SETUP names probe/1.
    assert not [m for m in info if "probe/1" in m]
    # Client side of both sessions, server side of the ordinary one only.
    assert _count(info, "setup complete: draft-18") == 3
    assert _count(info, "control message parsed: Subscribe(") == 1
    assert [m for m in debug.at(logging.DEBUG) if "probe/1" in m]
