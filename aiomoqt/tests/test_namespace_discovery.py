"""A publisher answers SUBSCRIBE_NAMESPACE with NAMESPACE for each namespace
it publishes under the prefix, reports later announcements, and sends
NAMESPACE_DONE when it withdraws one (d18 §6.1). A FIN from the requester
cancels the subscription."""
import asyncio
import contextlib
import logging

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages.namespace import SubscribeNamespace
from aiomoqt.messages.request import RequestUpdate
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import (
    D16MessageType, MOQTMessageType, MOQTRequestError, ParamType,
    RequestErrorCode,
)

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 16380
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


@contextlib.asynccontextmanager
async def _discovery(port, use_quic):
    """Yields (publisher, server session, queue of (kind, suffix)) where
    kind is NAMESPACE or DONE as the server session receives them."""
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
            yield pub, await asyncio.wait_for(peer, 5.0), seen
    finally:
        quic_server.close()


async def _next(seen):
    return await asyncio.wait_for(seen.get(), 5.0)


@requires_certs
@_TRANSPORTS
async def test_namespaces_are_reported_and_withdrawn(use_quic):
    async with _discovery(_BASE_PORT + (not use_quic), use_quic) as (
            pub, rx, seen):
        PublishedTrack(pub, namespace="disc/b", trackname="t").attach()
        PublishedTrack(pub, namespace="other", trackname="t").attach()
        await rx.subscribe_namespace("disc", wait_response=True)
        got = {await _next(seen) for _ in range(2)}
        assert got == {("NAMESPACE", (b"a",)), ("NAMESPACE", (b"b",))}
        await pub.publish_namespace(namespace="disc/c", wait_response=True)
        assert await _next(seen) == ("NAMESPACE", (b"c",))
        pub.publish_namespace_done(namespace="disc/a")
        assert await _next(seen) == ("DONE", (b"a",))
        await asyncio.sleep(0.05)
        assert seen.empty()
        assert pub._close_err is None and rx._close_err is None


@requires_certs
@_TRANSPORTS
async def test_a_fin_cancels_only_its_namespace_subscription(use_quic):
    async with _discovery(_BASE_PORT + 2 + (not use_quic), use_quic) as (
            pub, rx, seen):
        await rx.subscribe_namespace("other", wait_response=True)
        exact = await rx.subscribe_namespace("disc/a")
        assert await _next(seen) == ("NAMESPACE", ())
        rx.stream_fin(rx._bidi_streams[exact.request_id])
        for _ in range(100):
            if exact.request_id not in pub._discovery_subs:
                break
            await asyncio.sleep(0.02)
        assert exact.request_id not in pub._peer_requests
        # Its prefix no longer overlaps.
        await rx.subscribe_namespace("disc", wait_response=True)
        assert await _next(seen) == ("NAMESPACE", (b"a",))
        await pub.publish_namespace(namespace="other/x", wait_response=True)
        assert await _next(seen) == ("NAMESPACE", (b"x",))
        pub.publish_namespace_done(namespace="disc/a")
        assert await _next(seen) == ("DONE", (b"a",))
        await asyncio.sleep(0.05)
        assert seen.empty()
        assert sorted(pub._discovery_subs.values()) == [
            (b"disc",), (b"other",)]
        assert pub._close_err is None and rx._close_err is None


@requires_certs
@_TRANSPORTS
async def test_a_request_cancelled_before_it_is_handled_is_dropped(
        use_quic, caplog):
    async with _discovery(_BASE_PORT + 4 + (not use_quic), use_quic) as (
            pub, rx, seen):
        # The request and its FIN in one frame: the cancel is parsed
        # before the request's handler runs.
        msg = SubscribeNamespace(request_id=rx._allocate_request_id(),
                                 namespace_prefix=(b"disc",),
                                 subscribe_options=0, parameters={})
        sid = await rx.open_bidi_stream()
        rx.stream_write(sid, msg.serialize(prof=rx._profile).data,
                        end_stream=True)
        await asyncio.sleep(0.2)
        assert seen.empty()
        assert pub._discovery_subs == {} and pub._peer_requests == {}
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert pub._close_err is None and rx._close_err is None


@requires_certs
@_TRANSPORTS
async def test_an_update_before_the_fin_is_still_answered(use_quic):
    # A failing REQUEST_UPDATE, then FIN, on the subscription's stream.
    async with _discovery(_BASE_PORT + 6 + (not use_quic), use_quic) as (
            pub, rx, seen):
        await rx.subscribe_namespace("other", wait_response=True)
        sub = await rx.subscribe_namespace("disc", wait_response=True)
        assert await _next(seen) == ("NAMESPACE", (b"a",))
        # In one frame, as request_update() would send it, then FIN.
        rid = rx._allocate_request_id()
        update = RequestUpdate(
            request_id=rid, existing_request_id=sub.request_id,
            parameters={ParamType.TRACK_NAMESPACE_PREFIX: (b"other", b"x")})
        rx._tx_updates[sub.request_id] = rid
        rx.stream_write(rx._bidi_streams[sub.request_id],
                        update.serialize(prof=rx._profile).data,
                        end_stream=True)
        with pytest.raises(MOQTRequestError) as err:
            await asyncio.wait_for(rx._await_response(rid), 5.0)
        assert err.value.error_code == RequestErrorCode.PREFIX_OVERLAP
        await asyncio.sleep(0.05)
        assert sorted(pub._discovery_subs.values()) == [(b"other",)]
        assert pub._close_err is None and rx._close_err is None
