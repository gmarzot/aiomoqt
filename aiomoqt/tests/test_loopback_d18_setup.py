"""Loopback draft-18 SETUP self-test (Phase 3).

draft-18 runs control over a PAIR of unidirectional streams (SETUP type
0x2F00, which is both the control uni-stream type and the SETUP message
type). This proves the raw-QUIC uni-pair handshake end to end:

  * the client opens a write-control uni and sends SETUP on it,
  * the server binds its read-control uni by the 0x2F00 demux, brings up
    its own write-control uni, and replies with SETUP,
  * the client completes setup on receipt of the server's SETUP.

A returning client_session_init() therefore proves the full exchange.
This self-test covers raw QUIC; the WebTransport d18 control path is
covered by test_loopback_d18_objects.
"""
import asyncio
import os

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, SubscribeDoneCode
from aiomoqt.messages.subscribe import SubscribeOk, SubscribeDone


_CERT_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'certs')
CERT = os.path.realpath(os.path.join(_CERT_DIR, 'cert.pem'))
KEY = os.path.realpath(os.path.join(_CERT_DIR, 'key.pem'))

pytestmark = pytest.mark.skipif(
    not os.path.exists(CERT) or not os.path.exists(KEY),
    reason="TLS certs not found in certs/",
)

_BASE_PORT = 14480


async def _start_server(port, on_subscribe=None):
    server = MOQTServer(
        host="localhost", port=port,
        certificate=CERT, private_key=KEY,
        path="/",
        use_quic=True,
        supported_drafts=18,
    )
    if on_subscribe is not None:
        server.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
    return await server.serve()


@pytest.mark.asyncio
async def test_d18_setup_handshake_quic():
    """Client + server complete the d18 raw-QUIC handshake over the
    control uni-stream pair."""
    port = _BASE_PORT + 1
    server = await _start_server(port)
    try:
        client = MOQTClient(
            "localhost", port, path="/",
            use_quic=True, verify_tls=False, supported_drafts=18,
        )
        async with client.connect() as session:
            await session.client_session_init()
            assert session._moqt_session_setup.done()
            assert session._moqt_session_setup.result() is True
            assert session.negotiated_draft == 18
            # Control ran over a uni pair, not a single bidi stream.
            assert session._control_stream_id is None
            assert session._d18_control_write_sid is not None
            assert session._d18_control_read_sid is not None
    finally:
        server.close()


@pytest.mark.asyncio
async def test_d18_subscribe_roundtrip_quic():
    """d18 SUBSCRIBE opens a bidi request stream; SUBSCRIBE_OK and
    PUBLISH_DONE come back on that same stream with NO in-band Request ID
    and are correlated positionally."""
    port = _BASE_PORT + 2

    async def _on_subscribe(session, msg):
        # Reply on the request's own bidi stream: SUBSCRIBE_OK then
        # PUBLISH_DONE — both demuxed by the stream, no Request ID.
        session.subscribe_ok(request_msg=msg, content_exists=0)
        session.subscribe_done(
            msg.request_id, status_code=SubscribeDoneCode.SUBSCRIPTION_ENDED)

    done_seen = []

    async def _on_done(session, msg):
        done_seen.append(msg)

    server = await _start_server(port, on_subscribe=_on_subscribe)
    try:
        client = MOQTClient(
            "localhost", port, path="/",
            use_quic=True, verify_tls=False, supported_drafts=18,
        )
        client.register_handler(MOQTMessageType.PUBLISH_DONE, _on_done)
        async with client.connect() as session:
            await session.client_session_init()
            resp = await session.subscribe(
                "test/ns", "track", wait_response=True)
            # Reply correlated by stream (request_id injected, not on wire).
            assert isinstance(resp, SubscribeOk)
            assert resp.request_id == 0  # client's first request id
            # The SUBSCRIBE opened a bidi request stream.
            assert 0 in session._bidi_streams
            # PUBLISH_DONE arrived on the same stream and was correlated.
            for _ in range(50):
                if done_seen:
                    break
                await asyncio.sleep(0.02)
            assert len(done_seen) == 1
            assert isinstance(done_seen[0], SubscribeDone)
            assert done_seen[0].request_id == 0
    finally:
        server.close()
