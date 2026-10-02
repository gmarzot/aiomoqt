"""Loopback SETUP self-tests.

Confirms that the aiomoqt client + server agree on the MoQT session
handshake at each supported draft version, and that optional SETUP-time
parameters (AUTH_TOKEN on PUBLISH_NAMESPACE) round-trip.

Runs publisher + subscriber in a single process via aiopquic on localhost.
Parameterized over use_quic=True (raw QUIC) and use_quic=False (WT).
"""
import asyncio

import pytest

from aiomoqt.types import (
    MOQTMessageType, ParamType,
    MOQT_VERSION_DRAFT14, MOQT_VERSION_DRAFT16,
)
from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer


from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs


async def _start_server(port: int, supported_drafts, use_quic,
                         on_publish_namespace=None):
    server = MOQTServer(
        host="localhost", port=port,
        certificate=CERT, private_key=KEY,
        path="/",
        use_quic=use_quic,
        supported_drafts=supported_drafts,
    )
    if on_publish_namespace is not None:
        server.register_handler(
            MOQTMessageType.PUBLISH_NAMESPACE, on_publish_namespace)
    return await server.serve()


_BASE_PORT = 14450


@pytest.fixture(params=[True, False], ids=["use_quic", "wt"])
def use_quic(request):
    return request.param


@pytest.mark.asyncio
async def test_setup_draft14(use_quic):
    """Client + server complete the d14 handshake on both transports."""
    port = _BASE_PORT + (1 if use_quic else 4)
    server = await _start_server(
        port, supported_drafts=14, use_quic=use_quic)
    try:
        client = MOQTClient(
            "localhost", port, path="/",
            use_quic=use_quic,
            verify_tls=False, supported_drafts=14,
        )
        async with client.connect() as session:
            await session.client_session_init()
            assert session._moqt_session_setup.done()
            assert session._moqt_session_setup.result() is True
    finally:
        server.close()


@pytest.mark.asyncio
async def test_setup_draft16(use_quic):
    """Client + server complete the d16 handshake on both transports."""
    port = _BASE_PORT + (2 if use_quic else 5)
    server = await _start_server(
        port, supported_drafts=16, use_quic=use_quic)
    try:
        client = MOQTClient(
            "localhost", port, path="/",
            use_quic=use_quic,
            verify_tls=False, supported_drafts=16,
        )
        async with client.connect() as session:
            await session.client_session_init()
            assert session._moqt_session_setup.done()
            assert session._moqt_session_setup.result() is True
    finally:
        server.close()


@pytest.mark.asyncio
async def test_setup_auth_token_roundtrip(use_quic):
    """AUTH_TOKEN parameter on PUBLISH_NAMESPACE reaches the server."""
    port = _BASE_PORT + (3 if use_quic else 6)
    received_tokens: list[bytes] = []

    async def _handle_pub_ns(session, msg):
        token = msg.parameters.get(ParamType.AUTH_TOKEN)
        if token is not None:
            received_tokens.append(token)
        session.publish_namepace_ok(msg)

    server = await _start_server(
        port, supported_drafts=16, use_quic=use_quic,
        on_publish_namespace=_handle_pub_ns)
    try:
        client = MOQTClient(
            "localhost", port, path="/",
            use_quic=use_quic,
            verify_tls=False, supported_drafts=16,
        )
        async with client.connect() as session:
            await session.client_session_init()
            await session.publish_namespace(
                namespace="setup-test",
                parameters={ParamType.AUTH_TOKEN: b"tok-xyz"},
                wait_response=True,
            )
            await asyncio.sleep(0.1)
        assert received_tokens == [b"tok-xyz"], \
            f"expected one tok-xyz; got {received_tokens}"
    finally:
        server.close()


def _applied(session) -> int:
    """aiopquic's count of priorities picoquic accepted, or -1."""
    tp = getattr(session, "_transport", None)
    if tp is None:
        return -1
    try:
        return tp.counters["set_priority_applied"]
    except Exception:
        return -1


@pytest.mark.asyncio
async def test_stream_priority_reaches_the_scheduler(use_quic):
    """A declared priority reaches picoquic on BOTH transports.

    On WebTransport `self._quic` is the session itself, so looking
    set_stream_priority up there used to find the mixin's own method and
    recurse. RecursionError was caught only in the deepest frame, so every
    frame above returned True and the caller was told a priority had been
    applied that never left the process.
    """
    port = _BASE_PORT + (31 if use_quic else 34)
    server = await _start_server(
        port, supported_drafts=18, use_quic=use_quic)
    try:
        client = MOQTClient(
            "localhost", port, path="/",
            use_quic=use_quic,
            verify_tls=False, supported_drafts=18,
        )
        async with client.connect() as session:
            await session.client_session_init()
            before = _applied(session)
            if before < 0:
                pytest.skip("aiopquic without priority counters")

            sid = await session.open_uni_stream()
            assert session.set_stream_priority(sid, 2) is True

            for _ in range(200):
                if _applied(session) > before:
                    break
                await asyncio.sleep(0.01)
            assert _applied(session) == before + 1, (
                f"picoquic never applied the priority on "
                f"{'raw QUIC' if use_quic else 'WebTransport'}")
    finally:
        server.close()


@pytest.mark.parametrize("draft", (16, 18))
@pytest.mark.asyncio
async def test_control_stream_gets_its_own_band(use_quic, draft):
    """The control stream is prioritised on every transport and draft.

    Scheduling is strict, so a greedy data track sharing or undercutting
    the control band starves it — no SUBSCRIBE_OK, no PUBLISH_DONE, the
    session wedges under load. There are five places a control stream id is
    established (raw QUIC bidi, raw QUIC d18 write-uni, WT bidi, WT d18
    write-uni, and the server accepting inbound); the raw-QUIC d18 one was
    missed on the first pass and only the counter caught it.
    """
    port = _BASE_PORT + (41 if use_quic else 44) + draft
    server = await _start_server(
        port, supported_drafts=draft, use_quic=use_quic)
    try:
        client = MOQTClient(
            "localhost", port, path="/",
            use_quic=use_quic,
            verify_tls=False, supported_drafts=draft,
        )
        async with client.connect() as session:
            await session.client_session_init()
            if _applied(session) < 0:
                pytest.skip("aiopquic without priority counters")
            counters = session._transport.counters
            assert counters["set_priority_applied"] >= 1, (
                "control stream was never prioritised")
            assert counters["set_priority_rejected"] == 0, (
                f"picoquic refused it, last_err="
                f"{counters['set_priority_last_err']}")
    finally:
        server.close()
