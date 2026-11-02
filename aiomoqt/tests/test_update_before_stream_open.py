"""A REQUEST_UPDATE sent right after its request, before the request's
stream is open (WebTransport opens it asynchronously), follows the
request on that stream, in order."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import RequestOk
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, ParamType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 23000
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


@requires_certs
@_TRANSPORTS
@pytest.mark.parametrize("draft", [18, 22])
async def test_updates_sent_before_the_stream_opens_follow_in_order(
        use_quic, draft):
    port = _BASE_PORT + 2 * (draft == 22) + (not use_quic)
    seen = []

    async def _on_subscribe(session, msg):
        seen.append((msg.request_id, "subscribe", msg.track_name))
        session.subscribe_ok(request_msg=msg)

    async def _on_update(session, msg):
        seen.append((msg.existing_request_id, "update",
                     msg.parameters.get(ParamType.FORWARD)))
        session._send_reply(msg.request_id,
                            RequestOk(request_id=msg.request_id))

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    server.register_handler(MOQTMessageType.SUBSCRIBE_UPDATE, _on_update)
    quic_server = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            await session.client_session_init()
            ids, replies = [], []
            for name in ("video", "audio"):
                sub = session.subscribe("update/early", name,
                                        wait_response=False)
                ids.append(sub.request_id)
                for forward in (0, 1):
                    replies.append(session.request_update(
                        sub.request_id, forward=forward, wait_response=True))
            assert all(isinstance(r, RequestOk) for r in
                       await asyncio.wait_for(asyncio.gather(*replies), 5.0))
            assert not session._unbound_updates
            assert session._close_err is None
    finally:
        quic_server.close()
    for rid, name in zip(ids, ("video", "audio")):
        assert [s[1:] for s in seen if s[0] == rid] == [
            ("subscribe", name.encode()), ("update", 0), ("update", 1)]
