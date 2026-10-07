"""A session closed for a detected violation puts its error code on the
wire: the CONNECTION_CLOSE application error (raw QUIC) or the
WebTransport session close code, so the peer sees why."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages.subscribe import Subscribe
from aiomoqt.server import MOQTServer
from aiomoqt.types import SessionCloseCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_PORT = 14850


@requires_certs
@pytest.mark.parametrize("use_quic, port", [
    pytest.param(True, _PORT, id="quic"),
    pytest.param(False, _PORT + 1, id="wt", marks=pytest.mark.xfail(
        strict=True, reason="aiopquic reports a CLOSE_WEBTRANSPORT_SESSION "
                            "capsule that arrives with its FIN as code 0")),
])
async def test_violation_close_reaches_the_peer_with_its_code(use_quic, port):
    server = await MOQTServer(
        host="localhost", port=port, certificate=CERT, private_key=KEY,
        path="/", use_quic=use_quic, supported_drafts=18).serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            bad = Subscribe(request_id=session._allocate_request_id(),
                            track_namespace=(b"n",), track_name=b"x",
                            forward=2)  # FORWARD is 0 or 1 (§10.2.12)
            sid = await session.open_bidi_stream()
            session.stream_write(
                sid, bytes(bad.serialize(prof=session._profile).data))
            code, _ = await asyncio.wait_for(session._moqt_session_closed, 5)
            assert code == SessionCloseCode.PROTOCOL_VIOLATION
    finally:
        server.close()
