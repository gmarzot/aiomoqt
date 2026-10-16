"""Stream resets and STOP_SENDING carry stream reset codes (d18 §3.3.3),
never session-close codes: a cancel is CANCELLED (0x1), not 0x0."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, StreamResetCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 16700


@pytest.fixture
def sent(monkeypatch):
    """(session, stream_id, kind, code) for every reset and STOP_SENDING."""
    ops = []
    orig = _MOQTSessionMixin._tx_send

    def record(self, stream_id, kind, arg):
        if kind in ("reset", "stop"):
            ops.append((self, stream_id, kind, arg))
        return orig(self, stream_id, kind, arg)
    monkeypatch.setattr(_MOQTSessionMixin, "_tx_send", record)
    return ops


async def _loopback(port, use_quic, sessions):
    async def on_subscribe(session, msg):
        sessions.append(session)
        session.subscribe_error(msg.request_id, error_code=0x10, reason="no")
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)
    server.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
    return await server.serve()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_padding_stream_cancel_is_cancelled(use_quic, sent):
    port = _BASE_PORT + (0 if use_quic else 1)
    sessions = []
    server = await _loopback(port, use_quic, sessions)
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            with pytest.raises(Exception):
                await asyncio.wait_for(session.subscribe(
                    "codes", "t", wait_response=True), 5.0)
            sid = await session.send_padding(16)
            for _ in range(100):
                if any(s is sessions[0] for s, *_ in sent):
                    break
                await asyncio.sleep(0.02)
    finally:
        server.close()
    stops = [(k, code) for s, i, k, code in sent if s is sessions[0]]
    assert stops == [("stop", StreamResetCode.CANCELLED)]
    assert int(stops[0][1]) == 0x1
    assert sid is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_finished_subscription_stop_sending_is_cancelled(use_quic,
                                                               sent):
    port = _BASE_PORT + 2 + (0 if use_quic else 1)
    sessions = []
    server = await _loopback(port, use_quic, sessions)
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            # Two subscriptions' open streams; only the finished one's goes.
            sid_a = await session.open_uni_stream()
            sid_b = await session.open_uni_stream()
            session._track_aliases.update({5: 7, 6: 9})
            session._subgroup_stream_by_key[(5, 0, 0)] = sid_a
            session._subgroup_stream_by_key[(6, 0, 0)] = sid_b
            session._release_finished_track(5, 7)
            got = [(i, k, int(code)) for s, i, k, code in sent
                   if s is session]
    finally:
        server.close()
    assert got == [(sid_a, "stop", 0x1)]
