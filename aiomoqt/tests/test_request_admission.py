"""Session-level request admission (d16+): reserved namespaces (d18
§3.2.1-2), a duplicate subscription (§5.1) and an overlapping namespace
prefix (§10.18-19) are refused with REQUEST_ERROR before the application
sees the request."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.messages.namespace import (
    PublishNamespace, SubscribeNamespace, SubscribeTracks,
)
from aiomoqt.messages.request import RequestError
from aiomoqt.messages.subscribe import Subscribe
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import MOQTMessageType, MOQTRequestError, RequestErrorCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs


def _session(draft):
    s = object.__new__(_MOQTSessionMixin)
    s._peer_requests = {}
    s._requests = {}
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s._request_cancel_handlers = {}
    s._publish_done_handlers = {}
    s.sent = []
    s._send_on_request_stream = (
        lambda rid, msg, fin=False: s.sent.append((rid, msg, fin)))
    s.send_control_message = lambda msg: s.sent.append((None, msg, False))
    return s


def _sub(rid, ns=(b"a",), name=b"t"):
    return Subscribe(request_id=rid, track_namespace=ns, track_name=name)


async def _refused(s, msg):
    """The error code a refused request is answered with, else None."""
    refuse = s._refuse_request(msg)
    if refuse is None:
        return None
    assert s.sent == [], "refusal sent before the request stream is bound"
    await refuse(s, msg)
    rid, reply, fin = s.sent.pop()
    assert isinstance(reply, RequestError) and rid == msg.request_id and fin
    return reply.error_code


@pytest.mark.parametrize("msg", [
    _sub(1, ns=(b".",)),
    _sub(1, ns=(b".session",), name=b""),
    _sub(1, ns=(b".session",), name=b"anything"),
    PublishNamespace(request_id=1, namespace=(b".", b"x")),
    SubscribeNamespace(request_id=1, namespace_prefix=(b".session",)),
], ids=["dot", "session-empty-name", "session-unknown", "publish-ns-dot",
        "subscribe-ns-session"])
async def test_d18_reserved_namespaces_do_not_exist(msg):
    assert await _refused(_session(18), msg) == RequestErrorCode.DOES_NOT_EXIST


async def test_other_reserved_namespaces_reach_the_application():
    assert await _refused(_session(18), _sub(1, ns=(b".future",))) is None


async def test_reserved_namespace_rule_is_d18_only():
    assert await _refused(_session(16), _sub(1, ns=(b".",))) is None


@pytest.mark.parametrize("draft", [16, 18])
async def test_duplicate_subscription_until_the_first_ends(draft):
    s = _session(draft)
    assert await _refused(s, _sub(1)) is None
    assert await _refused(s, _sub(3)) == RequestErrorCode.DUPLICATE_SUBSCRIPTION
    assert await _refused(s, _sub(5, name=b"other")) is None
    s._notify_request_cancelled(1, "test")             # peer cancelled
    assert await _refused(s, _sub(7)) is None
    s._send_reply(7, RequestError(request_id=7), fin=True)  # we ended it
    assert await _refused(s, _sub(9)) is None


@pytest.mark.parametrize("draft", [16, 18])
@pytest.mark.parametrize("first, second, overlap", [
    ((b"a",), (b"a", b"b"), True),
    ((b"a", b"b"), (b"a",), True),
    ((b"a",), (b"a",), True),
    ((), (b"a",), True),
    ((b"a",), (b"b",), False),
    ((b"a", b"b"), (b"a", b"c"), False),
])
async def test_subscribe_namespace_prefix_overlap(draft, first, second,
                                                  overlap):
    s = _session(draft)
    assert await _refused(s, SubscribeNamespace(
        request_id=1, namespace_prefix=first)) is None
    got = await _refused(s, SubscribeNamespace(
        request_id=3, namespace_prefix=second))
    assert got == (RequestErrorCode.PREFIX_OVERLAP if overlap else None)


async def test_d18_subscribe_tracks_overlap_space_is_independent():
    s = _session(18)
    assert await _refused(s, SubscribeNamespace(
        request_id=1, namespace_prefix=(b"a",))) is None
    assert await _refused(s, SubscribeTracks(
        request_id=3, namespace_prefix=(b"a",))) is None
    assert await _refused(s, SubscribeTracks(
        request_id=5, namespace_prefix=(b"a", b"b"))) \
        == RequestErrorCode.PREFIX_OVERLAP


async def test_d14_requests_are_not_checked():
    s = _session(14)
    assert await _refused(s, _sub(1)) is None
    assert await _refused(s, _sub(3)) is None


# -- over real sessions: the refusal reaches the peer ------------------

_BASE_PORT = 16000
_NS = "admit/ns"


def _port(case: int, draft: int) -> int:
    return _BASE_PORT + 2 * case + (draft == 18)


@contextlib.asynccontextmanager
async def _loopback(port, draft, subscribe_handler=None):
    """The client publishes `_NS`/video; yields (client session, server
    session), the server being the one that sends requests."""
    peer = asyncio.get_running_loop().create_future()

    async def _on_publish_namespace(session, msg):
        session.publish_namepace_ok(msg)
        if not peer.done():
            peer.set_result(session)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=True,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            _on_publish_namespace)
    quic_server = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=True,
                        verify_tls=False, supported_drafts=draft)
    if subscribe_handler is not None:
        client.register_handler(MOQTMessageType.SUBSCRIBE, subscribe_handler)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            track = PublishedTrack(session, namespace=_NS, trackname="video",
                                   object_size=64, group_size=10, rate=50)
            await track.publish(announce_namespace=True, publish_track=False)
            yield session, await asyncio.wait_for(peer, 5.0)
    finally:
        quic_server.close()


async def _error_code(request) -> int:
    with pytest.raises(MOQTRequestError) as err:
        await request
    assert err.value.response is not None, "timed out, no reply"
    return err.value.error_code


@requires_certs
@pytest.mark.parametrize("draft", [16, 18])
async def test_duplicate_subscribe_is_refused_on_the_wire(draft):
    async with _loopback(_port(0, draft), draft) as (pub, rx):
        await rx.subscribe(_NS, "video", forward=1, wait_response=True)
        code = await _error_code(
            rx.subscribe(_NS, "video", forward=1, wait_response=True))
        assert code == RequestErrorCode.DUPLICATE_SUBSCRIPTION
        assert pub._close_err is None and rx._close_err is None


@requires_certs
@pytest.mark.parametrize("draft", [16, 18])
async def test_overlapping_subscribe_namespace_is_refused_on_the_wire(draft):
    async with _loopback(_port(1, draft), draft) as (pub, rx):
        await rx.subscribe_namespace("admit", wait_response=True)
        code = await _error_code(
            rx.subscribe_namespace("admit/ns", wait_response=True))
        assert code == RequestErrorCode.PREFIX_OVERLAP
        assert pub._close_err is None and rx._close_err is None


@requires_certs
async def test_session_namespace_never_reaches_an_app_handler():
    seen = []

    async def _app_subscribe(session, msg):
        seen.append(msg.track_namespace)
        session.subscribe_ok(request_msg=msg)

    async with _loopback(_port(2, 18), 18,
                         subscribe_handler=_app_subscribe) as (pub, rx):
        code = await _error_code(
            rx.subscribe(".session/x", "y", forward=1, wait_response=True))
        assert code == RequestErrorCode.DOES_NOT_EXIST
        assert seen == []
        assert pub._close_err is None
