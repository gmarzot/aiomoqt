"""Error and reset codes are named once and encoded per draft inside
subscribe_error / fetch_error / stream_reset."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import (canonical_request_error, canonical_stream_reset,
                             profile_for, request_error_code,
                             stream_reset_code)
from aiomoqt.server import MOQTServer
from aiomoqt.types import (MOQTMessageType, MOQTRequestError,
                           RequestErrorCode, StreamResetCode,
                           SubscribeErrorCode)

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_DRAFTS = [14, 16, 18, 20, 22]


@pytest.mark.parametrize("draft, name, fetch, wire", [
    (14, RequestErrorCode.DOES_NOT_EXIST, False, 0x04),
    (14, RequestErrorCode.INVALID_RANGE, True, 0x05),
    (14, RequestErrorCode.MALFORMED_AUTH_TOKEN, False, 0x10),
    (14, RequestErrorCode.INVALID_JOINING_REQUEST_ID, True, 0x07),
    (14, RequestErrorCode.MALFORMED_TRACK, True, 0x09),
    (14, RequestErrorCode.MALFORMED_TRACK, False, 0x00),
    (16, SubscribeErrorCode.TRACK_DOES_NOT_EXIST, False, 0x10),
    (16, SubscribeErrorCode.INVALID_RANGE, True, 0x11),
    (16, RequestErrorCode.GOING_AWAY, False, 0x00),
    (18, RequestErrorCode.GOING_AWAY, False, 0x06),
    (18, RequestErrorCode.INVALID_JOINING_REQUEST_ID, True, 0x32),
    (20, RequestErrorCode.INVALID_JOINING_REQUEST_ID, True, 0x00),
    (20, RequestErrorCode.INVALID_FILTER, False, 0x36),
    (22, RequestErrorCode.DOES_NOT_EXIST, False, 0x10),
])
def test_request_error_code_per_draft(draft, name, fetch, wire):
    assert request_error_code(name, profile_for(draft), fetch=fetch) == wire


def test_a_plain_int_is_sent_as_given():
    for draft in _DRAFTS:
        assert request_error_code(0x04, profile_for(draft)) == 0x04
        assert stream_reset_code(0x42, profile_for(draft)) == 0x42


@pytest.mark.parametrize("draft, wire, fetch, name", [
    (14, 0x05, True, RequestErrorCode.INVALID_RANGE),
    (14, 0x04, False, RequestErrorCode.DOES_NOT_EXIST),
    (16, 0x04, False, RequestErrorCode.MALFORMED_AUTH_TOKEN),
    (18, 0x11, True, RequestErrorCode.INVALID_RANGE),
])
def test_canonical_request_error(draft, wire, fetch, name):
    got = canonical_request_error(wire, profile_for(draft), fetch=fetch)
    assert got is name
    assert request_error_code(got, profile_for(18), fetch=fetch) == {
        RequestErrorCode.INVALID_RANGE: 0x11,
        RequestErrorCode.DOES_NOT_EXIST: 0x10,
        RequestErrorCode.MALFORMED_AUTH_TOKEN: 0x04}[name]


def test_an_unknown_wire_code_stays_as_received():
    assert canonical_request_error(0x7E, profile_for(18)) == 0x7E


@pytest.mark.parametrize("draft, code, wire", [
    (14, StreamResetCode.DELIVERY_TIMEOUT, 0x2),
    (14, StreamResetCode.MALFORMED_TRACK, 0x0),
    (16, StreamResetCode.UNKNOWN_OBJECT_STATUS, 0x4),
    (16, StreamResetCode.GOING_AWAY, 0x0),
    (18, StreamResetCode.UNKNOWN_OBJECT_STATUS, 0x6),
    (18, StreamResetCode.GOING_AWAY, 0x4),
    (22, StreamResetCode.EXCESSIVE_LOAD, 0x9),
])
def test_stream_reset_code_per_draft(draft, code, wire):
    assert stream_reset_code(code, profile_for(draft)) == wire


@pytest.mark.parametrize("draft, wire, code", [
    (14, 0x2, StreamResetCode.DELIVERY_TIMEOUT),
    (16, 0x4, StreamResetCode.UNKNOWN_OBJECT_STATUS),
    (16, 0x12, StreamResetCode.MALFORMED_TRACK),
    (18, 0x4, StreamResetCode.GOING_AWAY),
    (18, 0x6, StreamResetCode.UNKNOWN_OBJECT_STATUS),
    (22, 0x9, StreamResetCode.EXCESSIVE_LOAD),
])
def test_canonical_stream_reset(draft, wire, code):
    got = canonical_stream_reset(wire, profile_for(draft))
    assert got is code
    assert stream_reset_code(got, profile_for(draft)) == wire


def test_an_unknown_reset_code_stays_as_received():
    assert canonical_stream_reset(0x4, profile_for(14)) == 0x4
    assert canonical_stream_reset(0x7E, profile_for(18)) == 0x7E


@contextlib.asynccontextmanager
async def _loopback(port, draft, use_quic):
    async def on_subscribe(session, msg):
        session.subscribe_error(msg.request_id,
                                error_code=RequestErrorCode.DOES_NOT_EXIST,
                                reason="no")

    async def on_fetch(session, msg):
        session.fetch_error(msg.request_id,
                            error_code=RequestErrorCode.INVALID_RANGE,
                            reason="no")

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
    server.register_handler(MOQTMessageType.FETCH, on_fetch)
    quic_server = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            yield session
    finally:
        quic_server.close()


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_error_replies_carry_the_draft_code(draft, use_quic):
    port = 16520 + _DRAFTS.index(draft) + (0 if use_quic else 8)
    async with _loopback(port, draft, use_quic) as session:
        codes = {}
        for name in ("a", "b"):
            with pytest.raises(MOQTRequestError) as err:
                await asyncio.wait_for(session.subscribe(
                    "codes/ns", name, wait_response=True), 5.0)
            codes[("sub", name)] = err.value.error_code
            with pytest.raises(MOQTRequestError) as err:
                await asyncio.wait_for(session.fetch(
                    "codes/ns", name, start_group=0, start_object=0,
                    end_group=1, end_object=0, wait_response=True), 5.0)
            codes[("fetch", name)] = err.value.error_code
    dne, rng = (0x04, 0x05) if draft == 14 else (0x10, 0x11)
    assert codes == {("sub", "a"): dne, ("fetch", "a"): rng,
                     ("sub", "b"): dne, ("fetch", "b"): rng}
