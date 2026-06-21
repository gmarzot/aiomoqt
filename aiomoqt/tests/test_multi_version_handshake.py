"""Multi-version negotiation matrix (raw QUIC).

A client offering several drafts settles on the server's draft via ALPN:
the client offers every supported ALPN newest-first, the single-draft
server confirms its one ALPN, and the client locks its draft from the
TLS-negotiated value (version-from-ALPN). The session never touches the
process — each session's draft is its own.

Both transports negotiate in-band: raw QUIC via the ALPN list
(version-from-ALPN) and WebTransport via WT-Available-Protocols ->
WT-Protocol (version-from-negotiated-protocol). WT pinned-draft
handshakes are also covered by test_loopback_setup.
"""
import asyncio
import os

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer

_CERT_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'certs')
CERT = os.path.realpath(os.path.join(_CERT_DIR, 'cert.pem'))
KEY = os.path.realpath(os.path.join(_CERT_DIR, 'key.pem'))

pytestmark = pytest.mark.skipif(
    not os.path.exists(CERT) or not os.path.exists(KEY),
    reason="TLS certs not found in certs/",
)

_BASE_PORT = 14480


async def _start_server(port, supported_drafts):
    server = MOQTServer(
        host="localhost", port=port,
        certificate=CERT, private_key=KEY, path="/",
        use_quic=True, supported_drafts=supported_drafts,
    )
    return await server.serve()


@pytest.mark.asyncio
@pytest.mark.parametrize("server_draft", [16, 14])
async def test_multi_offer_settles_server_draft(server_draft):
    """Client offers [16, 14]; settles on whichever draft the single-draft
    server speaks. Settling on the lower offered draft (d14) works now that
    the client offers the full ALPN list (request_alpn_list) and the server
    confirms its one ALPN — the former picky-d14 limitation is fixed."""
    port = _BASE_PORT + server_draft
    server = await _start_server(port, server_draft)
    try:
        client = MOQTClient(
            "localhost", port, path="/", use_quic=True,
            verify_tls=False, supported_drafts=[16, 14],
        )
        assert client.supported_drafts == [16, 14]
        async with client.connect() as session:
            await session.client_session_init()
            assert session._moqt_session_setup.result() is True
            assert session.negotiated_draft == server_draft
    finally:
        server.close()


@pytest.mark.asyncio
async def test_pinned_client_against_matching_server():
    """A pinned client offers exactly one ALPN and settles there."""
    port = _BASE_PORT + 20
    server = await _start_server(port, 16)
    try:
        client = MOQTClient(
            "localhost", port, path="/", use_quic=True,
            verify_tls=False, supported_drafts=16,
        )
        assert client.supported_drafts == [16]
        async with client.connect() as session:
            await session.client_session_init()
            assert session._moqt_session_setup.result() is True
            assert session.negotiated_draft == 16
    finally:
        server.close()


@pytest.mark.asyncio
async def test_non_intersecting_alpn_fails():
    """Negative (raw QUIC): client offers only d16, server speaks only
    d14 → no common ALPN → connect MUST fail cleanly, not hang (the
    aiopquic clean-fail surfaces WRONG_ALPN as a prompt ConnectionError)."""
    port = _BASE_PORT + 40
    server = await _start_server(port, 14)
    try:
        client = MOQTClient(
            "localhost", port, path="/", use_quic=True,
            verify_tls=False, supported_drafts=16,
        )
        with pytest.raises(Exception):
            async with asyncio.timeout(8):
                async with client.connect() as session:
                    await session.client_session_init()
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_default_server_and_client_settle_d16():
    """Unpinned WT server + unpinned WT client both default to the STABLE
    set (16, 14) — d18 is beta and opt-in, so an auto session settles on
    d16, NOT d18. The default WT client offers moqt-16; the server, lacking
    in-band WT selection, defaults to max(supported_drafts) = 16. Guards
    that a no-args session never negotiates onto the beta d18 wire (d18
    requires an explicit supported_drafts=18 / supported_drafts opt-in).
    """
    port = _BASE_PORT + 60
    server = MOQTServer(
        host="localhost", port=port,
        certificate=CERT, private_key=KEY, path="/",
        use_quic=False,  # WebTransport, no draft pin
    )
    handle = await server.serve()
    try:
        client = MOQTClient(
            "localhost", port, path="/", use_quic=False,
            verify_tls=False,  # no draft pin
        )
        async with client.connect() as session:
            await session.client_session_init()
            assert session._moqt_session_setup.result() is True
            assert session.negotiated_draft == 16
    finally:
        handle.close()


@pytest.mark.asyncio
async def test_wt_negotiates_protocol_from_wt_protocol():
    """WT in-band negotiation (positive): an unpinned WT client offers its
    supported WT subprotocols; the server selects one and echoes
    WT-Protocol; both ends derive their draft from the negotiated value.

    Both ends pin supported_drafts=[16] so the in-band selection is
    deterministic: negotiated_protocol == "moqt-16" proves the draft came
    from the in-band WT-Protocol selection, not the max-supported fallback.
    The 'highest-of-several' selection mechanics are covered at the
    aiopquic layer (tests/test_negotiation.py) with moqt-18/16/14.
    """
    port = _BASE_PORT + 80
    server = MOQTServer(
        host="localhost", port=port,
        certificate=CERT, private_key=KEY, path="/",
        use_quic=False, supported_drafts=[16],
    )
    handle = await server.serve()
    try:
        client = MOQTClient(
            "localhost", port, path="/", use_quic=False,
            verify_tls=False, supported_drafts=[16],
        )
        async with client.connect() as session:
            await session.client_session_init()
            assert session._moqt_session_setup.result() is True
            assert session.negotiated_protocol == "moqt-16"
            assert session.negotiated_draft == 16
    finally:
        handle.close()

