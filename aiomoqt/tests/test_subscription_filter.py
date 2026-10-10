"""A PublishedTrack sends only the objects a subscription's filter admits
(§5.1.2), ends an AbsoluteRange with PUBLISH_DONE SUBSCRIPTION_ENDED once
its streams have closed, and refuses one whose End Group is already
published."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.delivery import StreamMapping, SubgroupDelivery
from aiomoqt.server import MOQTServer
from aiomoqt.track import (
    PublishedTrack, _Subscription, _UNFILTERED, _filter_window,
)
from aiomoqt.types import (
    FilterType, MOQTMessageType, MOQTRequestError, RequestErrorCode,
    SubscribeDoneCode,
)

from aiomoqt.tests._certs import CERT, KEY, requires_certs


# -- the window -----------------------------------------------------------

@pytest.mark.parametrize("filter_type, fields, largest, window", [
    (None, (None, None, None), (3, 4), _UNFILTERED),
    (FilterType.LATEST_OBJECT, (None, None, None), (3, 4), ((3, 5), None)),
    (FilterType.LATEST_OBJECT, (None, None, None), None, ((0, 0), None)),
    (FilterType.NEXT_GROUP_START, (None, None, None), (3, 4), ((4, 0), None)),
    (FilterType.ABSOLUTE_START, (7, 2, None), (3, 4), ((7, 2), None)),
    (FilterType.ABSOLUTE_RANGE, (7, 2, 9), None, ((7, 2), 9)),
], ids=["none", "latest", "latest-empty", "next-group", "absolute-start",
        "absolute-range"])
def test_filter_window(filter_type, fields, largest, window):
    assert _filter_window(filter_type, *fields, largest) == window


def test_a_window_admits_its_range_only():
    sub = _Subscription(None)
    sub.window = ((2, 3), 4)
    assert not sub.passes(2, 2) and sub.passes(2, 3) and sub.passes(4, 99)
    assert not sub.passes(5, 0)
    assert not sub.past_end(4) and sub.past_end(5)


async def test_a_delivery_admitted_mid_group_does_not_claim_its_first_object():
    class _Session:
        _profile = profile_for(18)
        streams = 0

        async def open_uni_stream(self):
            self.streams += 1
            return self.streams

        def stream_write(self, sid, data, end_stream=False):
            pass

        async def stream_write_drain(self, sid, data, end_stream=False):
            pass

    delivery = SubgroupDelivery(_Session(), 1,
                                mapping=StreamMapping.PER_GROUP)
    delivery.passes = lambda g, o: (g, o) >= (1, 1)
    for group in range(2):
        for obj in range(3):
            await delivery.write(group, obj, b"x", group_start=obj == 0)
    assert delivery.stream_count == 1 and delivery.objects_sent == 2
    assert delivery._header.group_id == 1
    assert delivery._header.first_object is False


# -- over a real session ---------------------------------------------------

_BASE_PORT = 16300
_NS = "filter/test"
_SIZES = {"video": 300, "audio": 100}
_GROUP = 5
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _port(case: int, use_quic: bool) -> int:
    return _BASE_PORT + 2 * case + (not use_quic)


@contextlib.asynccontextmanager
async def _loopback(port, use_quic):
    """The client publishes video and audio, two groups each already
    published (Largest {1, 4}); yields (tracks, server session, objects),
    `objects` mapping a track name to the (group, object) it received."""
    peer = asyncio.get_running_loop().create_future()
    objects: dict = {"video": [], "audio": []}
    by_size = {size: name for name, size in _SIZES.items()}

    def _on_object(msg, _size, _ts, group_id, _subgroup_id):
        name = by_size.get(len(msg.payload or b""))
        if name is not None:
            objects[name].append((group_id, msg.object_id))

    async def _on_publish_namespace(session, msg):
        session.on_object_received = _on_object
        session.publish_namepace_ok(msg)
        if not peer.done():
            peer.set_result(session)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            _on_publish_namespace)
    quic_server = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as pub:
            await pub.client_session_init()
            tracks = {}
            for name, size in _SIZES.items():
                track = PublishedTrack(pub, namespace=_NS, trackname=name,
                                       object_size=size, group_size=_GROUP,
                                       rate=500)
                track.prefill(2)
                track.attach()
                tracks[name] = track
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            yield tracks, await asyncio.wait_for(peer, 5.0), objects
    finally:
        quic_server.close()


@requires_certs
@_TRANSPORTS
async def test_an_absolute_range_sends_its_range_then_ends(use_quic):
    async with _loopback(_port(0, use_quic), use_quic) as (tracks, rx, objects):
        await rx.subscribe(_NS, "audio", forward=1, wait_response=True)
        ok = await rx.subscribe(
            _NS, "video", forward=1, filter_type=FilterType.ABSOLUTE_RANGE,
            start_group=2, start_object=1, end_group=3, wait_response=True)
        done = asyncio.get_running_loop().create_future()
        rx.register_publish_done_handler(ok.request_id, done.set_result)
        status = await asyncio.wait_for(done, 5.0)
        assert status.status_code == SubscribeDoneCode.SUBSCRIPTION_ENDED
        await asyncio.sleep(0.1)
        assert sorted(objects["video"]) == (
            [(2, o) for o in range(1, _GROUP)] + [(3, o) for o in range(_GROUP)])
        # The other track's subscription carries on.
        before = len(objects["audio"])
        await asyncio.sleep(0.1)
        assert len(objects["audio"]) > before
        assert rx._close_err is None


@requires_certs
@_TRANSPORTS
async def test_a_range_already_published_is_refused(use_quic):
    async with _loopback(_port(1, use_quic), use_quic) as (tracks, rx, _):
        with pytest.raises(MOQTRequestError) as err:
            await rx.subscribe(
                _NS, "video", forward=1,
                filter_type=FilterType.ABSOLUTE_RANGE, start_group=0,
                start_object=0, end_group=0, wait_response=True)
        assert err.value.error_code == RequestErrorCode.INVALID_RANGE
        assert rx._close_err is None


@requires_certs
@_TRANSPORTS
async def test_forward_zero_pauses_only_its_subscription(use_quic):
    # §5.1: no objects while the Forward State is 0; the other track goes on.
    async with _loopback(_port(2, use_quic), use_quic) as (tracks, rx, objects):
        await rx.subscribe(_NS, "audio", forward=1, wait_response=True)
        video = await rx.subscribe(_NS, "video", forward=1, wait_response=True)
        await asyncio.sleep(0.1)
        await rx.request_update(video.request_id, forward=0,
                                wait_response=True)
        await asyncio.sleep(0.05)
        assert tracks["video"]._subs[0].senders == set()
        video_seen, audio_seen = len(objects["video"]), len(objects["audio"])
        await asyncio.sleep(0.15)
        assert len(objects["video"]) == video_seen
        assert len(objects["audio"]) > audio_seen
        await rx.request_update(video.request_id, forward=1,
                                wait_response=True)
        await asyncio.sleep(0.15)
        assert len(objects["video"]) > video_seen
        assert rx._close_err is None
