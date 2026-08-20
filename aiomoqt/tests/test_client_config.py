"""MOQTClient configuration surface: effective_configuration is always
populated after connect, qlog_dir= is a first-class kwarg on both
transports, and a partial configuration= merges instead of silently
dropping the ALPN/draft wiring. Regression source:
notes (openmoq) aiopquic-aiomoqt-probe-api-asks.md.
"""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiopquic.quic.configuration import QuicConfiguration

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 14830


def _server(port, use_quic=True):
    return MOQTServer(host="localhost", port=port, certificate=CERT,
                      private_key=KEY, path="/", use_quic=use_quic,
                      supported_drafts=18)


@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_effective_configuration_populated(use_quic):
    port = _BASE_PORT + (1 if use_quic else 2)
    server = await _server(port, use_quic=use_quic).serve()
    try:
        client = MOQTClient("localhost", port, path="/",
                            use_quic=use_quic, verify_tls=False,
                            supported_drafts=18)
        assert client.effective_configuration is None
        async with client.connect() as session:
            await session.client_session_init()
            eff = client.effective_configuration
            assert eff is not None
            if use_quic:
                # The relay_probe regression: alpn must be readable.
                assert eff.alpn_protocols[0] == "moqt-18"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_qlog_dir_kwarg(tmp_path):
    port = _BASE_PORT + 3
    server = await _server(port).serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=18,
                            qlog_dir=str(tmp_path))
        async with client.connect() as session:
            await session.client_session_init()
            assert client.effective_configuration.qlog_dir == str(tmp_path)
            await asyncio.sleep(0.1)
    finally:
        server.close()
    assert any(tmp_path.iterdir()), "no qlog written to qlog_dir"


@pytest.mark.asyncio
async def test_partial_configuration_merges_alpn():
    # A config carrying only logging fields used to lose the ALPN
    # wiring and fail the handshake with an opaque error code 0.
    port = _BASE_PORT + 4
    server = await _server(port).serve()
    try:
        client = MOQTClient(
            "localhost", port, path="/", use_quic=True, verify_tls=False,
            supported_drafts=18,
            configuration=QuicConfiguration(is_client=True))
        async with client.connect() as session:
            await session.client_session_init()
            assert session.negotiated_draft == 18
            assert client.effective_configuration.alpn_protocols == [
                "moqt-18"]
    finally:
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_peer_transport_parameters_and_cids(use_quic):
    # Probe-API surface (openmoq asks #1/#2): the peer's negotiated
    # transport parameters and the connection IDs, in-memory.
    port = _BASE_PORT + (5 if use_quic else 6)
    server = await _server(port, use_quic=use_quic).serve()
    try:
        client = MOQTClient("localhost", port, path="/",
                            use_quic=use_quic, verify_tls=False,
                            supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            tp = session.peer_transport_parameters
            assert tp is not None
            assert tp['max_datagram_frame_size'] == 64 * 1024
            assert tp['initial_max_data'] > 0
            assert tp['max_idle_timeout'] > 0
            local = (session._quic.local_transport_parameters
                     if use_quic else None)
            if local is not None:
                assert local['max_datagram_frame_size'] == 64 * 1024
            cids = session.connection_ids
            assert cids is not None
            assert isinstance(cids['remote'], bytes) and cids['remote']
            assert isinstance(cids['local'], bytes)
    finally:
        server.close()
