"""A session closed for a detected violation puts its error code on the
wire: the CONNECTION_CLOSE application error (raw QUIC) or the
WebTransport session close code, so the peer sees why."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages.subscribe import Subscribe
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, MOQTRequestError, SessionCloseCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_PORT = 14850


@requires_certs
@pytest.mark.parametrize("use_quic, port", [
    pytest.param(True, _PORT, id="quic"),
    pytest.param(False, _PORT + 1, id="wt"),
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


@requires_certs
async def test_a_request_pending_at_close_fails_promptly():
    # The server closes instead of answering: the client's SUBSCRIBE fails
    # with the session, not after its timeout, and so does a later one.
    port = _PORT + 2

    async def _close_instead(session, msg):
        session._close_session(SessionCloseCode.PROTOCOL_VIOLATION, "bye")

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=True,
                        supported_drafts=18)
    server.register_handler(MOQTMessageType.SUBSCRIBE, _close_instead)
    quic_server = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            with pytest.raises(MOQTRequestError, match="session closed"):
                await session.subscribe("n", "t", wait_response=True)
            assert loop.time() - t0 < 5.0
            t0 = loop.time()
            with pytest.raises(MOQTRequestError, match="session closed"):
                await session.subscribe("n", "u", wait_response=True)
            assert loop.time() - t0 < 1.0
    finally:
        quic_server.close()


@requires_certs
async def test_a_client_that_exits_on_the_failure_still_sends_its_close():
    # The client closes on a wrong-parity GOAWAY while its SUBSCRIBE is
    # pending; the app exits on the failed request at once, and the
    # server must still receive the close with its code.
    from aiomoqt.messages.session_setup import GoAway
    port = _PORT + 3
    server_session = asyncio.get_running_loop().create_future()

    async def _bad_goaway(session, msg):
        session.send_control_message(
            GoAway(new_session_uri="", timeout=0, request_id=1))
        server_session.set_result(session)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=True,
                        supported_drafts=18)
    server.register_handler(MOQTMessageType.SUBSCRIBE, _bad_goaway)
    quic_server = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            with pytest.raises(MOQTRequestError, match="session closed"):
                await session.subscribe("n", "t", wait_response=True)
        peer = await asyncio.wait_for(server_session, 5)
        code, _ = await asyncio.wait_for(peer._moqt_session_closed, 5)
        assert code == SessionCloseCode.INVALID_REQUEST_ID
    finally:
        quic_server.close()


async def _capture_and_refuse(holder, session, msg):
    session.subscribe_error(msg.request_id, 0x10, "no such track")
    if not holder.done():
        holder.set_result(session)


@requires_certs
async def test_a_graceful_close_reaches_the_peer_as_no_error():
    # §3.3: the control stream is never closed while the session lives,
    # so a graceful close is the CONNECTION_CLOSE alone.
    port = _PORT + 4
    server_session = asyncio.get_running_loop().create_future()
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=True,
                        supported_drafts=18)
    server.register_handler(
        MOQTMessageType.SUBSCRIBE,
        lambda s, m: _capture_and_refuse(server_session, s, m))
    quic_server = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            with pytest.raises(MOQTRequestError):
                await session.subscribe("n", "t", wait_response=True)
        peer = await asyncio.wait_for(server_session, 5)
        code, _ = await asyncio.wait_for(peer._moqt_session_closed, 5)
        assert code == SessionCloseCode.NO_ERROR
    finally:
        quic_server.close()


@requires_certs
async def test_a_peer_that_closes_its_control_stream_is_a_violation():
    port = _PORT + 5

    async def _close_control(session, msg):
        session._quic.send_stream_data(
            session._control_write_stream_id, b"", end_stream=True)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=True,
                        supported_drafts=18)
    server.register_handler(MOQTMessageType.SUBSCRIBE, _close_control)
    quic_server = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            session.subscribe("n", "t", wait_response=False)
            code, _ = await asyncio.wait_for(session._moqt_session_closed, 5)
            assert code == SessionCloseCode.PROTOCOL_VIOLATION
    finally:
        quic_server.close()
