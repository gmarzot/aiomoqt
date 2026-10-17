"""PADDING streams and datagrams (§11.5) are sent as zeros and discarded
by the receiver without disturbing the session."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, MOQTRequestError

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 16560
_DRAFTS = [18, 20, 22]


async def _serve(port, draft, use_quic, sessions):
    async def on_subscribe(session, msg):
        sessions.append(session)
        session.subscribe_error(msg.request_id, error_code=0x10,
                                reason="no track")
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
    return await server.serve()


async def _still_serving(session, name):
    with pytest.raises(MOQTRequestError):
        await asyncio.wait_for(
            session.subscribe("pad/ns", name, wait_response=True), 5.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_padding_streams_both_ways(draft, use_quic):
    port = _BASE_PORT + _DRAFTS.index(draft) + (0 if use_quic else 4)
    sessions = []
    server = await _serve(port, draft, use_quic, sessions)
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            await session.client_session_init()
            await _still_serving(session, "a")
            for size in (0, 1500):
                sid = await session.send_padding(size)
                assert sid is not None
                await sessions[0].send_padding(size)
            await _still_serving(session, "b")
            assert session._close_err is None
            assert sessions[0]._close_err is None
            assert session._padding_rx == 2
            assert sessions[0]._padding_rx == 2
    finally:
        server.close()


# WT datagram TX is not wired, so datagrams run over raw QUIC only.
@pytest.mark.asyncio
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_padding_datagram(draft):
    port = _BASE_PORT + 8 + _DRAFTS.index(draft)
    sessions = []
    server = await _serve(port, draft, True, sessions)
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            await session.client_session_init()
            await _still_serving(session, "a")
            assert await sessions[0].send_padding(64, datagram=True) is None
            await _still_serving(session, "b")
            assert session._close_err is None
            assert session._padding_rx == 1
    finally:
        server.close()


@pytest.mark.asyncio
async def test_padding_is_refused_before_d18():
    port = _BASE_PORT + 12
    sessions = []
    server = await _serve(port, 16, True, sessions)
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=16)
        async with client.connect() as session:
            await session.client_session_init()
            with pytest.raises(ValueError):
                await session.send_padding(8)
    finally:
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_pub_bench_sends_padding(use_quic, monkeypatch):
    from aiomoqt.tools import pub_bench
    port = _BASE_PORT + 14 + (0 if use_quic else 1)
    seen = []

    async def on_publish(session, msg):
        seen.append(session)
        session.publish_ok(msg, forward=0)
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)
    server.register_handler(MOQTMessageType.PUBLISH, on_publish)
    quic_server = await server.serve()
    url = (f"moqt://localhost:{port}" if use_quic
           else f"https://localhost:{port}/")
    monkeypatch.setattr("sys.argv", [
        "pub_bench", url, "-k", "--draft", "18", "-N", "pad", "-T", "t",
        "--padding", "64", "-t", "1", "--no-stats"])
    try:
        await pub_bench.run(pub_bench.parse_args())
    finally:
        quic_server.close()
    # The stream always; the datagram only where datagrams are negotiated.
    assert seen and seen[0]._padding_rx >= 1
