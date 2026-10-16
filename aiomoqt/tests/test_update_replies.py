"""d18 REQUEST_OK / REQUEST_ERROR carry no Request ID: on a request's
stream they answer, in order, the request (until its own reply) and then
each REQUEST_UPDATE sent on it. The stream ending fails the updates
still waiting."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import RequestError, RequestOk
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, MOQTRequestError, ParamType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 17220
_NAMESPACES = ("ns/a", "ns/b")


def _error(rid, code):
    return RequestError(request_id=rid, error_code=code, retry_interval=0,
                        reason="no")


@contextlib.asynccontextmanager
async def _loopback(port, use_quic, rx):
    async def on_publish_namespace(session, msg):
        rx["session"] = session
        rx.setdefault("requests", []).append(msg.request_id)

    async def on_update(session, msg):
        rx.setdefault("updates", {}).setdefault(
            msg.existing_request_id, []).append(msg.request_id)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            on_publish_namespace)
    server.register_handler(MOQTMessageType.SUBSCRIBE_UPDATE, on_update)
    handle = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=18)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            yield session
    finally:
        handle.close()


async def _until(pred, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_replies_answer_the_request_then_each_update(use_quic):
    rx = {}
    port = _BASE_PORT + (not use_quic)
    async with _loopback(port, use_quic, rx) as session:
        reqs = [session.publish_namespace(ns) for ns in _NAMESPACES]
        await _until(lambda: len(rx.get("requests", ())) == 2)
        req_futs, upd_futs = [], []
        for req in reqs:
            req_futs.append(asyncio.ensure_future(
                session._await_response(req.request_id)))
            upd_futs.append([
                asyncio.ensure_future(session.request_update(
                    req.request_id, wait_response=True,
                    parameters={ParamType.AUTH_TOKEN: f"t{i}".encode()}))
                for i in range(2)])
        await _until(lambda: sum(map(len, rx.get("updates", {}).values()))
                     == 4)
        srv = rx["session"]
        # Request A: refused, its updates OK then refused. Request B:
        # accepted, its updates refused then OK.
        for i, rid in enumerate(sorted(rx["requests"])):
            ups = rx["updates"][rid]
            if i == 0:
                srv._send_reply(rid, _error(rid, 0x10))
                srv._send_reply(ups[0], RequestOk(request_id=ups[0]))
                srv._send_reply(ups[1], _error(ups[1], 0x11))
            else:
                srv._send_reply(rid, RequestOk(request_id=rid))
                srv._send_reply(ups[0], _error(ups[0], 0x12))
                srv._send_reply(ups[1], RequestOk(request_id=ups[1]))
        done = await asyncio.gather(
            *req_futs, *(f for fs in upd_futs for f in fs),
            return_exceptions=True)
        req_a, req_b, a0, a1, b0, b1 = done
        assert isinstance(req_a, MOQTRequestError) and req_a.error_code == 0x10
        assert isinstance(req_b, RequestOk)
        assert isinstance(a0, RequestOk)
        assert isinstance(a1, MOQTRequestError) and a1.error_code == 0x11
        assert isinstance(b0, MOQTRequestError) and b0.error_code == 0x12
        assert isinstance(b1, RequestOk)
        assert session._close_err is None


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_stream_end_fails_the_waiting_updates(use_quic):
    rx = {}
    port = _BASE_PORT + 2 + (not use_quic)
    async with _loopback(port, use_quic, rx) as session:
        reqs = [session.publish_namespace(ns) for ns in _NAMESPACES]
        await _until(lambda: len(rx.get("requests", ())) == 2)
        futs = [asyncio.ensure_future(session.request_update(
            req.request_id, wait_response=True,
            parameters={ParamType.AUTH_TOKEN: b"t"})) for req in reqs]
        await _until(lambda: len(rx.get("updates", {})) == 2)
        srv = rx["session"]
        a, b = sorted(rx["requests"])
        srv._send_reply(a, RequestOk(request_id=a))
        # Request a answered: only its update still waits.
        await _until(lambda: list(session._tx_updates.get(a, ()))
                     == rx["updates"][a])
        srv.stream_fin(srv._bidi_streams[a])
        srv._send_reply(b, RequestOk(request_id=b))
        srv._send_reply(rx["updates"][b][0],
                        RequestOk(request_id=rx["updates"][b][0]))
        async with asyncio.timeout(2.0):
            got_a, got_b = await asyncio.gather(*futs,
                                                return_exceptions=True)
        assert isinstance(got_a, MOQTRequestError)
        assert isinstance(got_b, RequestOk)
