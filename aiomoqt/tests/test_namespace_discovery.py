"""A publisher answers SUBSCRIBE_NAMESPACE with NAMESPACE for each namespace
it publishes under the prefix, reports later announcements, and sends
NAMESPACE_DONE when it withdraws one (d18 §6.1)."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import D16MessageType, MOQTMessageType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 16380


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_namespaces_are_reported_and_withdrawn(use_quic):
    port = _BASE_PORT + (not use_quic)
    peer = asyncio.get_running_loop().create_future()
    seen: asyncio.Queue = asyncio.Queue()

    async def _on_publish_namespace(session, msg):
        session.publish_namepace_ok(msg)
        if not peer.done():
            peer.set_result(session)

    async def _on_namespace(session, msg):
        await seen.put(("NAMESPACE", tuple(msg.namespace_suffix)))

    async def _on_namespace_done(session, msg):
        await seen.put(("DONE", tuple(msg.namespace_suffix)))

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            _on_publish_namespace)
    server.register_handler(D16MessageType.NAMESPACE, _on_namespace)
    server.register_handler(D16MessageType.NAMESPACE_DONE, _on_namespace_done)
    quic_server = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as pub:
            await pub.client_session_init()
            await pub.publish_namespace(namespace="disc/a", wait_response=True)
            PublishedTrack(pub, namespace="disc/b", trackname="t").attach()
            PublishedTrack(pub, namespace="other", trackname="t").attach()
            rx = await asyncio.wait_for(peer, 5.0)
            await rx.subscribe_namespace("disc", wait_response=True)
            got = {await asyncio.wait_for(seen.get(), 5.0) for _ in range(2)}
            assert got == {("NAMESPACE", (b"a",)), ("NAMESPACE", (b"b",))}
            await pub.publish_namespace(namespace="disc/c", wait_response=True)
            assert await asyncio.wait_for(seen.get(), 5.0) == (
                "NAMESPACE", (b"c",))
            pub.publish_namespace_done(namespace="disc/a")
            assert await asyncio.wait_for(seen.get(), 5.0) == ("DONE", (b"a",))
            await asyncio.sleep(0.05)
            assert seen.empty()
            assert pub._close_err is None and rx._close_err is None
    finally:
        quic_server.close()
