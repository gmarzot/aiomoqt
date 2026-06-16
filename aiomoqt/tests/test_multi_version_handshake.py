"""Multi-version negotiation matrix (raw QUIC).

A client offering several drafts settles on the server's draft via ALPN:
the client offers every supported ALPN newest-first, the single-draft
server confirms its one ALPN, and the client locks its draft from the
TLS-negotiated value (version-from-ALPN). The session never touches the
process — each session's draft is its own.

Raw-QUIC only: WT in-band multi-version selection needs the aiopquic
server-side picowt_select_wt_protocol (fast-follow). WT pinned-draft
handshakes are covered by test_loopback_setup.
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


async def _start_server(port, draft_version):
    server = MOQTServer(
        host="localhost", port=port,
        certificate=CERT, private_key=KEY, path="/",
        use_quic=True, draft_version=draft_version,
    )
    return await server.serve()


@pytest.mark.asyncio
@pytest.mark.parametrize("server_draft", [
    16,
    pytest.param(14, marks=pytest.mark.skip(
        reason="multi-offer settling on the non-preferred (lower) draft "
               "needs the aiopquic server-side ALPN selector — the "
               "fast-follow tracked as the picky-d14 limitation. A "
               "single-draft d16 server (highest mutual) works today.")),
])
async def test_multi_offer_settles_server_draft(server_draft):
    """Client offers [16, 14]; settles on whichever draft the
    single-draft server speaks. d16 (highest mutual) is the Phase-0
    working case; settling on a lower offered draft is the fast-follow."""
    port = _BASE_PORT + server_draft
    server = await _start_server(port, server_draft)
    try:
        client = MOQTClient(
            "localhost", port, path="/", use_quic=True,
            verify_tls=False, supported_drafts=[16, 14],
        )
        assert client.draft_version is None        # multi-offer
        assert client.supported_drafts == (16, 14)
        async with client.connect() as session:
            await session.client_session_init()
            assert session._moqt_session_setup.result() is True
            assert session._draft == server_draft
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
            verify_tls=False, draft_version=16,
        )
        assert client.supported_drafts == (16,)
        async with client.connect() as session:
            await session.client_session_init()
            assert session._moqt_session_setup.result() is True
            assert session._draft == 16
    finally:
        server.close()


# --- Negotiation matrix cells blocked on the aiopquic fast-follow ---
# (server-side alpn_select_fn, clean no-ALPN failure, and WT
#  picowt_select_wt_protocol + WT-Protocol read-back). Encoded here so
#  the matrix is complete; flip the skips when the fast-follow lands.

@pytest.mark.asyncio
@pytest.mark.skip(reason="aiopquic 0.3.8 does not surface a no-common-ALPN "
                         "handshake as a clean failure (it hangs); the "
                         "negative case needs the negotiation fast-follow.")
async def test_non_intersecting_alpn_fails():
    """Negative (raw QUIC): client offers only d16, server speaks only
    d14 → no common ALPN → connect MUST fail cleanly, not hang."""
    port = _BASE_PORT + 40
    server = await _start_server(port, 14)
    try:
        client = MOQTClient(
            "localhost", port, path="/", use_quic=True,
            verify_tls=False, draft_version=16,
        )
        with pytest.raises(Exception):
            async with asyncio.timeout(8):
                async with client.connect() as session:
                    await session.client_session_init()
    finally:
        server.close()


@pytest.mark.asyncio
async def test_wt_default_server_and_client_settle_highest():
    """Unpinned WT server + unpinned WT client both default their draft
    to the highest supported (16). The default WT client offers only
    moqt-{newest}; the server, lacking in-band WT selection, defaults to
    max(supported_drafts) — they agree on d16 and the handshake
    completes. Locks the WT-server-default-draft fix.
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
            assert session._draft == 16
    finally:
        handle.close()


@pytest.mark.skip(reason="WT in-band version selection (WT-Available-Protocols "
                         "→ WT-Protocol) is not surfaced by aiopquic 0.3.8 — "
                         "the WT client stamps the highest offered draft. The "
                         "full WT matrix (intersecting→highest, single→single, "
                         "non-intersecting→fail) lands with the aiopquic "
                         "picowt_select_wt_protocol + WT-Protocol read-back.")
def test_wt_version_negotiation_matrix():
    """Placeholder documenting the WT negotiation matrix pending the
    aiopquic fast-follow."""

