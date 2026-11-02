"""A publisher session answers SUBSCRIBE_TRACKS itself (d22 §3.6): a PUBLISH
for each attached track under the prefix, then for each track attached while
the request is open, once per track, filtered by TRACK_PROPERTY_FILTER
(§3.6.1). An application handler for the type answers instead."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages.params import D20ParamType, RangeFilter
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import D18MessageType, MOQTMessageType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 17000
_DRAFTS = (18, 22)
_TRACK_PROP = 0x3800


def _cases(drafts=_DRAFTS):
    return pytest.mark.parametrize(
        "draft, use_quic", [(d, q) for d in drafts for q in (True, False)],
        ids=[f"d{d}-{'quic' if q else 'wt'}" for d in drafts
             for q in (True, False)])


def _port(offset, draft, use_quic):
    return _BASE_PORT + offset + 2 * _DRAFTS.index(draft) + (not use_quic)


@contextlib.asynccontextmanager
async def _peers(port, use_quic, draft, on_tracks=None):
    """Yields (publisher client session, subscriber server session)."""
    peer = asyncio.get_running_loop().create_future()

    async def _on_publish_namespace(session, msg):
        session.publish_namespace_ok(msg)
        if not peer.done():
            peer.set_result(session)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            _on_publish_namespace)
    quic_server = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        if on_tracks is not None:
            client.register_handler(D18MessageType.SUBSCRIBE_TRACKS, on_tracks)
        async with client.connect() as pub:
            await pub.client_session_init()
            await pub.publish_namespace(namespace="disc/a", wait_response=True)
            yield pub, await asyncio.wait_for(peer, 5.0)
    finally:
        quic_server.close()


@contextlib.asynccontextmanager
async def _server_publishes(port, use_quic, draft):
    """Yields (publisher server session, subscriber client session); the
    server accepts Range Filters."""
    peer = asyncio.get_running_loop().create_future()

    async def _on_publish_namespace(session, msg):
        session.publish_namespace_ok(msg)
        if not peer.done():
            peer.set_result(session)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft, max_filter_ranges=4)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            _on_publish_namespace)
    quic_server = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as rx:
            await rx.client_session_init()
            await rx.publish_namespace(namespace="hello", wait_response=True)
            yield await asyncio.wait_for(peer, 5.0), rx
    finally:
        quic_server.close()


async def _offered(rx, count):
    return sorted([(await rx.await_publish(timeout=5.0)).track_name.decode()
                   for _ in range(count)])


async def _none_offered(rx):
    with pytest.raises(asyncio.TimeoutError):
        await rx.await_publish(timeout=0.5)


@requires_certs
@_cases()
async def test_tracks_under_the_prefix_are_published(draft, use_quic):
    async with _peers(_port(0, draft, use_quic), use_quic, draft) as (
            pub, rx):
        for ns, name in (("disc/a", "video"), ("disc/a", "audio"),
                         ("other", "meta")):
            PublishedTrack(pub, namespace=ns, trackname=name).attach()
        await rx.subscribe_tracks("disc/a", wait_response=True)
        assert await _offered(rx, 2) == ["audio", "video"]
        await _none_offered(rx)
        PublishedTrack(pub, namespace="disc/a", trackname="late").attach()
        assert await _offered(rx, 1) == ["late"]
        PublishedTrack(pub, namespace="other", trackname="late").attach()
        await _none_offered(rx)
        assert pub._close_err is None and rx._close_err is None


@requires_certs
@_cases()
async def test_one_publish_per_track_when_the_app_publishes_too(draft,
                                                               use_quic):
    async with _peers(_port(4, draft, use_quic), use_quic, draft) as (
            pub, rx):
        await rx.subscribe_tracks("disc/a", wait_response=True)
        for name in ("video", "audio"):
            await PublishedTrack(pub, namespace="disc/a",
                                 trackname=name).publish(publish_track=True)
        assert await _offered(rx, 2) == ["audio", "video"]
        await _none_offered(rx)
        assert pub._close_err is None and rx._close_err is None


@requires_certs
@_cases((22,))
async def test_track_property_filter_selects_tracks(draft, use_quic):
    async with _server_publishes(_port(8, draft, use_quic), use_quic,
                                 draft) as (pub, rx):
        for name, value in (("video", 1), ("audio", 1), ("data", 2)):
            PublishedTrack(pub, namespace="disc/a", trackname=name,
                           track_properties={_TRACK_PROP: value}).attach()
        await rx.subscribe_tracks("disc/a", wait_response=True, parameters={
            D20ParamType.TRACK_PROPERTY_FILTER: RangeFilter(
                set_id=0, ranges=((1, 1),), property_type=_TRACK_PROP)})
        assert await _offered(rx, 2) == ["audio", "video"]
        await _none_offered(rx)
        assert pub._close_err is None and rx._close_err is None


@requires_certs
@_cases()
async def test_an_app_handler_answers_instead(draft, use_quic):
    async def ack_only(session, msg):
        session.subscribe_tracks_ok(msg)

    async with _peers(_port(12, draft, use_quic), use_quic, draft,
                      on_tracks=ack_only) as (pub, rx):
        PublishedTrack(pub, namespace="disc/a", trackname="video").attach()
        await rx.subscribe_tracks("disc/a", wait_response=True)
        PublishedTrack(pub, namespace="disc/a", trackname="audio").attach()
        await _none_offered(rx)
        assert pub._close_err is None and rx._close_err is None


@requires_certs
@_cases()
async def test_discovery_against_a_plain_publisher(draft, use_quic):
    async with _peers(_port(16, draft, use_quic), use_quic, draft) as (
            pub, rx):
        PublishedTrack(pub, namespace="disc/a", trackname="video").attach()
        sub = SubscribedTrack(rx, "disc/a")
        sub._quiet = True
        await sub.subscribe(timeout=5)
        assert sub.trackname == "video"
        assert pub._close_err is None and rx._close_err is None
