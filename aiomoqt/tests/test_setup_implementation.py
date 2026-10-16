"""Both ends send MOQT_IMPLEMENTATION in their SETUP at d14, d16 and d18 over
raw QUIC and WebTransport; implementation= sets the value and None omits it.
"""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.protocol import USER_AGENT
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, SetupParamType
from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 17400
_SETUP_TYPES = (MOQTMessageType.CLIENT_SETUP, MOQTMessageType.SERVER_SETUP,
                MOQTMessageType.SETUP)


def _capture(peer, seen):
    """Record each SETUP-family message's options, then run the default."""
    def wrap(msg_type):
        async def handler(session, msg):
            opts = getattr(msg, 'options', None)
            seen.append(opts if opts is not None else msg.parameters)
            _, default = session.CONTROL_REGISTRY[session.negotiated_draft][
                msg_type]
            await default(session, msg)
        return handler
    for t in _SETUP_TYPES:
        peer.register_handler(t, wrap(t))


async def _exchange(port, draft, use_quic, client_impl, server_impl):
    """Run one SETUP exchange; returns (options the server got, options the
    client got)."""
    at_server, at_client = [], []
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=[draft], implementation=server_impl)
    _capture(server, at_server)
    handle = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=[draft],
                            implementation=client_impl)
        _capture(client, at_client)
        async with client.connect() as session:
            await session.client_session_init(timeout=5)
            assert session.negotiated_draft == draft
            async with asyncio.timeout(3):
                while not at_server:
                    await asyncio.sleep(0.01)
    finally:
        handle.close()
    assert len(at_server) == 1 and len(at_client) == 1
    return at_server[0], at_client[0]


def _port(draft, use_quic, case):
    return (_BASE_PORT + {14: 0, 16: 4, 18: 8}[draft] + case * 2
            + (0 if use_quic else 1))


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", [14, 16, 18])
async def test_both_ends_send_implementation(draft, use_quic):
    to_server, to_client = await _exchange(
        _port(draft, use_quic, 0), draft, use_quic, USER_AGENT, "test-server/1")
    assert to_server[SetupParamType.IMPLEMENTATION] == USER_AGENT.encode()
    assert to_client[SetupParamType.IMPLEMENTATION] == b"test-server/1"


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", [14, 16, 18])
async def test_implementation_none_omits_the_option(draft, use_quic):
    to_server, to_client = await _exchange(
        _port(draft, use_quic, 1), draft, use_quic, None, None)
    assert SetupParamType.IMPLEMENTATION not in to_server
    assert SetupParamType.IMPLEMENTATION not in to_client
