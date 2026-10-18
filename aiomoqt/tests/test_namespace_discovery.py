"""A publisher answers SUBSCRIBE_NAMESPACE with NAMESPACE for each namespace
it publishes under the prefix, reports later announcements, and sends
NAMESPACE_DONE when it withdraws one (d18 §6.1). At d18 a FIN from the
requester cancels the subscription; from d20 it does not (§6.4.2)."""
import asyncio
import contextlib
import logging
from collections import deque

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
async def _discovery(port, use_quic, draft=18):
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
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            _on_publish_namespace)
    server.register_handler(D16MessageType.NAMESPACE, _on_namespace)
    server.register_handler(D16MessageType.NAMESPACE_DONE, _on_namespace_done)
    quic_server = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
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
        rx._tx_updates[sub.request_id] = deque([rid])
        rx.stream_write(rx._bidi_streams[sub.request_id],
                        update.serialize(prof=rx._profile).data,
                        end_stream=True)
        with pytest.raises(MOQTRequestError) as err:
            await asyncio.wait_for(rx._await_response(rid), 5.0)
        assert err.value.error_code == RequestErrorCode.PREFIX_OVERLAP
        await asyncio.sleep(0.05)
        assert sorted(pub._discovery_subs.values()) == [(b"other",)]
        assert pub._close_err is None and rx._close_err is None


@requires_certs
@_TRANSPORTS
@pytest.mark.parametrize("draft", [20, 22])
async def test_a_fin_with_the_request_does_not_cancel_from_d20(
        use_quic, draft):
    # Each request and its FIN in one frame, as the contribution runner
    # sends them: both are answered and both keep reporting.
    port = _BASE_PORT + 8 + 2 * (draft == 22) + (not use_quic)
    async with _discovery(port, use_quic, draft) as (pub, rx, seen):
        PublishedTrack(pub, namespace="other/x", trackname="t").attach()
        replies = []
        for prefix in ((b"disc",), (b"other",)):
            rid = rx._allocate_request_id()
            msg = SubscribeNamespace(request_id=rid, namespace_prefix=prefix,
                                     subscribe_options=0, parameters={})
            sid = await rx.open_bidi_stream()
            rx._bidi_streams[rid] = sid
            rx._bidi_stream_requests[sid] = rid
            rx._note_request_kind(rid, SubscribeNamespace, ours=True)
            replies.append(asyncio.ensure_future(rx._await_response(rid)))
            await asyncio.sleep(0)
            rx.stream_write(sid, msg.serialize(prof=rx._profile).data,
                            end_stream=True)
        await asyncio.wait_for(asyncio.gather(*replies), 5.0)
        got = {await _next(seen) for _ in range(2)}
        assert got == {("NAMESPACE", (b"a",)), ("NAMESPACE", (b"x",))}
        assert sorted(pub._discovery_subs.values()) == [
            (b"disc",), (b"other",)]
        await pub.publish_namespace(namespace="disc/c", wait_response=True)
        assert await _next(seen) == ("NAMESPACE", (b"c",))
        assert pub._close_err is None and rx._close_err is None


@requires_certs
@_TRANSPORTS
@pytest.mark.parametrize("draft", [18, 22])
async def test_reserved_namespaces_are_never_sent(use_quic, draft):
    port = _BASE_PORT + 12 + 2 * (draft == 22) + (not use_quic)
    async with _discovery(port, use_quic, draft) as (pub, rx, seen):
        await rx.subscribe_namespace("", wait_response=True)
        assert await _next(seen) == ("NAMESPACE", (b"disc", b"a"))
        for ns in (".", ".session/x", ".interop"):
            with pytest.raises(ValueError, match="reserved"):
                pub.publish_namespace(namespace=ns)
            with pytest.raises(ValueError, match="reserved"):
                pub.publish(namespace=ns, track_name="t")
            track = PublishedTrack(pub, namespace=ns, trackname="t")
            with pytest.raises(ValueError, match="reserved"):
                await track.publish(announce_namespace=True,
                                    publish_track=False)
        await pub.publish_namespace(namespace="disc/b", wait_response=True)
        assert await _next(seen) == ("NAMESPACE", (b"disc", b"b"))
        assert seen.empty()
        assert pub._close_err is None and rx._close_err is None


def test_reserved_namespace_check_is_d18_and_later():
    from aiomoqt.context import profile_for
    from aiomoqt.protocol import _MOQTSessionMixin
    s = object.__new__(_MOQTSessionMixin)
    s._profile = profile_for(16)
    s._check_sendable_namespace((b".x",))
    for draft in (18, 20, 22):
        s._profile = profile_for(draft)
        s._check_sendable_namespace((b"a", b".x"))
        with pytest.raises(ValueError):
            s._check_sendable_namespace((b".",))
