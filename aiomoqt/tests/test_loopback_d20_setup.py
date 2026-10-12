"""Loopback SETUP at drafts 20, 21 and 22 over raw QUIC and WebTransport.

The server accepts every draft, so each end must learn the draft from
the negotiation (ALPN moqt-NN, or WT-Available-Protocols -> WT-Protocol)
rather than from a pin. The server's SETUP handler is wrapped to capture
its session and the client's SETUP.
"""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages.d20 import Setup as D20Setup
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, SessionCloseCode, SetupParamType
from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 16600
_ALL_DRAFTS = [22, 21, 20, 18, 16, 14]


async def _start_server(port, use_quic, supported_drafts=_ALL_DRAFTS):
    """Start a server; returns (handle, captured [(session, setup msg)])."""
    captured = []

    async def on_setup(session, msg):
        captured.append((session, msg))
        _, default = session.CONTROL_REGISTRY[session.negotiated_draft][
            MOQTMessageType.SETUP]
        await default(session, msg)

    server = MOQTServer(
        host="localhost", port=port, certificate=CERT, private_key=KEY,
        path="/", use_quic=use_quic, supported_drafts=supported_drafts)
    server.register_handler(MOQTMessageType.SETUP, on_setup)
    return await server.serve(), captured


def _client(port, use_quic, drafts):
    return MOQTClient("localhost", port, path="/", use_quic=use_quic,
                      verify_tls=False, supported_drafts=drafts)


async def _server_session(captured, timeout=3.0):
    async with asyncio.timeout(timeout):
        while not captured:
            await asyncio.sleep(0.01)
        session, msg = captured[0]
        assert await session._moqt_session_setup is True
        return session, msg


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", [20, 21, 22])
async def test_d20_setup_connects_and_closes(draft, use_quic):
    port = _BASE_PORT + (draft - 20) * 2 + (0 if use_quic else 1)
    handle, captured = await _start_server(port, use_quic)
    try:
        async with _client(port, use_quic, draft).connect() as session:
            await session.client_session_init()
            assert session._moqt_session_setup.result() is True
            assert session.negotiated_draft == draft
            assert session._d18_control_write_sid is not None
            assert session._d18_control_read_sid is not None
            assert session._control_stream_id is None
            if not use_quic:
                assert session.negotiated_protocol == f"moqt-{draft}"

            srv, setup = await _server_session(captured)
            assert srv.negotiated_draft == draft
            assert type(setup) is D20Setup
            opts = setup.options
            assert opts[SetupParamType.IMPLEMENTATION].startswith(b"aiomoqt/")
            # PATH and AUTHORITY travel only over raw QUIC.
            assert (SetupParamType.PATH in opts) is use_quic
            assert (SetupParamType.AUTHORITY in opts) is use_quic
            assert srv._close_err is None and session._close_err is None
        async with asyncio.timeout(3):
            code, _ = await srv._moqt_session_closed
        assert code == SessionCloseCode.NO_ERROR
        assert len(captured) == 1
    finally:
        handle.close()


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_d20_unknown_setup_options_are_ignored_end_to_end(use_quic):
    port = _BASE_PORT + 10 + (0 if use_quic else 1)
    handle, captured = await _start_server(port, use_quic)
    try:
        async with _client(port, use_quic, 22).connect() as session:
            await session.client_session_init(
                parameters={0x0A: 7, 0x9D: b"grease",
                            SetupParamType.MAX_REQUEST_UPDATES: 4})
            srv, setup = await _server_session(captured)
            assert setup.ignored == [0x0A, 0x9D]
            assert srv._peer_max_request_updates == 4
            assert session._peer_max_request_updates == 0
    finally:
        handle.close()


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_d20_offer_falls_back_to_a_default_server(use_quic):
    """A client offering 22..18 settles on 18 with a server left at the
    default drafts, which do not include 20-22."""
    port = _BASE_PORT + 12 + (0 if use_quic else 1)
    handle, captured = await _start_server(port, use_quic,
                                           supported_drafts=None)
    try:
        client = _client(port, use_quic, [22, 21, 20, 18])
        async with client.connect() as session:
            await session.client_session_init()
            assert session.negotiated_draft == 18
            srv, setup = await _server_session(captured)
            assert srv.negotiated_draft == 18
    finally:
        handle.close()


async def test_d20_is_refused_by_a_server_not_configured_for_it():
    port = _BASE_PORT + 14
    handle, _ = await _start_server(port, True, supported_drafts=None)
    try:
        with pytest.raises(Exception):
            async with asyncio.timeout(8):
                async with _client(port, True, 22).connect() as session:
                    await session.client_session_init(timeout=3)
    finally:
        handle.close()


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_d20_multi_offer_settles_the_servers_draft(use_quic):
    port = _BASE_PORT + 16 + (0 if use_quic else 1)
    handle, captured = await _start_server(port, use_quic,
                                           supported_drafts=[21])
    try:
        async with _client(port, use_quic, [22, 21, 20]).connect() as session:
            await session.client_session_init()
            assert session.negotiated_draft == 21
            srv, _ = await _server_session(captured)
            assert srv.negotiated_draft == 21
    finally:
        handle.close()
