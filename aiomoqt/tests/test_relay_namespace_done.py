"""A prefix subscriber hears exactly one NAMESPACE_DONE per withdrawn
namespace (§6.1: a second is for a suffix no longer announced), whether
the publisher withdraws it or its session closes. Two namespaces under one
prefix; raw QUIC and WebTransport, d22 and d18."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.types import D16MessageType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 20950
_DRAFTS = pytest.mark.parametrize("draft", [22, 18], ids=["d22", "d18"])
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
    relay._ns_subs.clear()
    relay._announce_subs.clear()


def _client(port, use_quic, draft):
    return MOQTClient("localhost", port, path="/", use_quic=use_quic,
                      verify_tls=False, supported_drafts=(draft,))


async def _next(seen):
    return await asyncio.wait_for(seen.get(), 5.0)


@_TRANSPORTS
@_DRAFTS
async def test_one_namespace_done_per_withdrawn_namespace(use_quic, draft):
    port = _PORT + 2 * (draft == 18) + (not use_quic)
    _reset()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=(22, 18))
    handle = await server.serve()
    seen: asyncio.Queue = asyncio.Queue()

    async def _ns(session, msg):
        await seen.put(("NAMESPACE", tuple(msg.namespace_suffix)))

    async def _done(session, msg):
        await seen.put(("DONE", tuple(msg.namespace_suffix)))

    try:
        async with contextlib.AsyncExitStack() as stack:
            sub = await stack.enter_async_context(
                _client(port, use_quic, draft).connect())
            await sub.client_session_init()
            sub.register_handler(D16MessageType.NAMESPACE, _ns)
            sub.register_handler(D16MessageType.NAMESPACE_DONE, _done)
            async with _client(port, use_quic, draft).connect() as pub:
                await pub.client_session_init()
                for ns in ("nd/a", "nd/b"):
                    await pub.publish_namespace(namespace=ns,
                                                wait_response=True)
                await sub.subscribe_namespace("nd", wait_response=True)
                got = {await _next(seen) for _ in range(2)}
                assert got == {("NAMESPACE", (b"a",)),
                               ("NAMESPACE", (b"b",))}
                # Withdrawn by the publisher.
                pub.publish_namespace_done(namespace="nd/a")
                assert await _next(seen) == ("DONE", (b"a",))
            # Withdrawn by the publisher's session closing.
            assert await _next(seen) == ("DONE", (b"b",))
            await asyncio.sleep(0.3)
            assert seen.empty(), seen.get_nowait()
            assert sub._close_err is None
    finally:
        handle.close()
        _reset()
