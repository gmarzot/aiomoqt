"""Concurrent sessions on different drafts in one event loop must not
perturb each other.

This is the regression the process-global version context made
impossible: a d16 session's handshake would clobber a d14 session's
version (and vice versa). With per-session ``_draft`` and the read-only
per-draft CONTROL_REGISTRY, four interleaved sessions — alternating
d14/d16 — each settle and operate on their own draft. Raw QUIC, pinned
clients against matching single-draft servers (no multi-offer, so this
is independent of the picky-d14 ALPN-selector fast-follow).
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
        await session.client_session_init()
        assert session._moqt_session_setup.result() is True
        return session.negotiated_draft


@pytest.mark.asyncio
async def test_concurrent_d14_d16_no_leakage():
    """Four interleaved d14/d16 handshakes run concurrently in one loop;
    each settles its own draft (would clobber under a process global)."""
    p14 = _BASE_PORT
    p16 = _BASE_PORT + 1
    s14 = await _start_server(p14, 14)
    s16 = await _start_server(p16, 16)
    try:
        async with asyncio.timeout(30):
            results = await asyncio.gather(
                _handshake_draft(p14, 14),
                _handshake_draft(p16, 16),
                _handshake_draft(p14, 14),
                _handshake_draft(p16, 16),
            )
        assert results == [14, 16, 14, 16], \
            f"cross-draft leakage: {results}"
    finally:
        s14.close()
        s16.close()
