"""Concurrent sessions on different drafts in one event loop must not
perturb each other.

With per-session ``_draft`` and the read-only per-draft CONTROL_REGISTRY,
four interleaved sessions — alternating d16/d18 — each settle and operate on
their own draft. Raw QUIC, pinned clients against matching single-draft
servers.
"""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 14530


async def _start_server(port, draft):
    server = MOQTServer(
        host="localhost", port=port,
        certificate=CERT, private_key=KEY, path="/",
        use_quic=True, supported_drafts=draft,
    )
    return await server.serve()


async def _handshake_draft(port, draft):
    """Connect, complete SETUP, and report the session's settled draft."""
    client = MOQTClient(
        "localhost", port, path="/", use_quic=True,
        verify_tls=False, supported_drafts=draft,
    )
    async with client.connect() as session:
        # Four TLS handshakes at once: the 10 s library default is not
        # enough headroom on a loaded CI runner.
        await session.client_session_init(timeout=45)
        assert session._moqt_session_setup.result() is True
        return session.negotiated_draft


@pytest.mark.asyncio
async def test_concurrent_d16_d18_no_leakage():
    """Four interleaved d16/d18 handshakes run concurrently in one loop;
    each settles its own draft (would clobber under a process global)."""
    p16 = _BASE_PORT
    p18 = _BASE_PORT + 1
    s16 = await _start_server(p16, 16)
    s18 = await _start_server(p18, 18)
    try:
        async with asyncio.timeout(60):
            results = await asyncio.gather(
                _handshake_draft(p16, 16),
                _handshake_draft(p18, 18),
                _handshake_draft(p16, 16),
                _handshake_draft(p18, 18),
            )
        assert results == [16, 18, 16, 18], \
            f"cross-draft leakage: {results}"
    finally:
        s16.close()
        s18.close()
