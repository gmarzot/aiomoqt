"""A WebTransport client cancelled while it closes still detaches its
dispatcher and stops its transport, then re-raises the cancellation."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.protocol import MOQTSessionWTClient
from aiomoqt.server import MOQTServer

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 17540


async def test_cancel_during_close_still_releases_the_transport(monkeypatch):
    server = MOQTServer(host="localhost", port=_PORT, certificate=CERT,
                        private_key=KEY, path="/", use_quic=False,
                        supported_drafts=18)
    handle = await server.serve()
    closing = asyncio.Event()
    seen = {}

    async def close_never_acked(self, *args, **kwargs):
        closing.set()
        await asyncio.Event().wait()

    async def run():
        client = MOQTClient("localhost", _PORT, path="/", use_quic=False,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            seen["session"] = session
            monkeypatch.setattr(MOQTSessionWTClient, "aclose",
                                close_never_acked)

    try:
        task = asyncio.create_task(run())
        await asyncio.wait_for(closing.wait(), 5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5.0)
        session = seen["session"]
        assert not session._dispatcher_attached
        assert not session._transport.started
    finally:
        handle.close()
