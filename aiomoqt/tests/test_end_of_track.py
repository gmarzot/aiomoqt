"""A produce() track that ends marks it with End of Track before
PUBLISH_DONE TRACK_ENDED: one past the last object on the open group
stream, else at the start of the next group on a new stream, or in a
datagram. end() stops production at an object boundary. Two tracks on one
session, over raw QUIC and WebTransport."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.delivery import StreamMapping, SubgroupDelivery
from aiomoqt.messages.data import ObjectHeader, SubgroupHeader
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack
from aiomoqt.types import (FilterType, MOQTProtocolViolation, ObjectStatus,
                           SubscribeDoneCode)
from aiomoqt.utils.buffer import Buffer

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._procs import free_udp_port
from aiomoqt.tests.test_loopback_d20_pubsub import _loopback, _until
from aiomoqt.tests.test_status_datagram_delivery import _feed, _session

_NS = "eot/test"
_DRAFTS = pytest.mark.parametrize("draft", [18, 22])
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
EOT = ObjectStatus.END_OF_TRACK


class _Groups(PublishedTrack):
    """`size` objects per group once `go` is set: `groups` groups, or
    until end() with None. `closed` ends each group's stream after its
    last object; `hold` then waits before the next group."""

    def __init__(self, *args, groups=None, size=3, closed=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.groups, self.size, self.closed = groups, size, closed
        self.go = asyncio.Event()
        self.hold = asyncio.Event()
        self.hold.set()

    async def produce(self, out):
        await self.go.wait()
        group_id = 0
        while self.groups is None or group_id < self.groups:
            await self.hold.wait()
            for object_id in range(self.size):
                await out.write(group_id, object_id, b"x" * 64,
                                group_start=object_id == 0)
                await asyncio.sleep(0.005)
            if self.closed:
                out.end_group()
            group_id += 1


class _Peer:
    """What one subscriber session sees per track, in arrival order."""

    def __init__(self, srv):
        self.srv = srv
        self.seen = {}
        self.done = {}

    async def subscribe(self, name, forward=1):
        ok = await self.srv.subscribe(_NS, name, forward=forward,
                                      filter_type=FilterType.LATEST_OBJECT,
                                      wait_response=True)
        seen = self.seen[name] = []
        done = self.done[name] = asyncio.get_running_loop().create_future()

        def on_object(obj, _size, _ts, group_id, _subgroup_id):
            seen.append((group_id, obj.object_id, obj.status))
        self.srv.register_object_handler(ok.track_alias, on_object)
        self.srv.register_publish_done_handler(
            ok.request_id, lambda msg: done.done() or done.set_result(msg))
        return ok


def _track(session, name, **kw):
    track = _Groups(session, namespace=_NS, trackname=name, object_size=64,
                    group_size=3, rate=0, **kw)
    track.attach()
    return track


def _order(monkeypatch):
    """('eot'|'done', track name) as the publisher sends them."""
    log = []
    end_track = SubgroupDelivery.end_track

    async def spy_end_track(self, largest):
        await end_track(self, largest)
        log.append(("eot", self.track_alias))
    monkeypatch.setattr(SubgroupDelivery, "end_track", spy_end_track)
    done = PublishedTrack._send_publish_done

    def spy_done(self, session, status_code=0x2, final=True, reason=None):
        log.append(("done", self._sub_for(session).track_alias))
        return done(self, session, status_code, final, reason=reason)
    monkeypatch.setattr(PublishedTrack, "_send_publish_done", spy_done)
    return log


def _eot_before_done(log, alias):
    events = [kind for kind, a in log if a == alias]
    assert events == ["eot", "done"], log


@requires_certs
@_TRANSPORTS
@_DRAFTS
async def test_a_finished_track_ends_with_end_of_track(monkeypatch, draft,
                                                       use_quic):
    log = _order(monkeypatch)
    async with _loopback(free_udp_port(), draft, use_quic) as (pub, srv):
        video = _track(pub, "video", groups=2)
        audio = _track(pub, "audio", groups=2, closed=True)
        rx = _Peer(srv)
        oks = {"video": await rx.subscribe("video"),
               "audio": await rx.subscribe("audio")}
        video.go.set()
        audio.go.set()
        for name in ("video", "audio"):
            msg = await asyncio.wait_for(rx.done[name], 5)
            assert msg.status_code == SubscribeDoneCode.TRACK_ENDED
        assert await _until(lambda: all(
            rx.seen[n] and rx.seen[n][-1][2] == EOT for n in rx.seen))
        # Open stream: one past the last object. Closed: the next group.
        assert rx.seen["video"][-1] == (1, 3, EOT)
        assert rx.seen["audio"][-1] == (2, 0, EOT)
        assert [s for *_, s in rx.seen["video"]].count(EOT) == 1
        for name, ok in oks.items():
            _eot_before_done(log, ok.track_alias)


@requires_certs
@_TRANSPORTS
async def test_end_stops_at_an_object_and_leaves_the_other_track(
        monkeypatch, use_quic):
    log = _order(monkeypatch)
    async with _loopback(free_udp_port(), 22, use_quic) as (pub, srv):
        video = _track(pub, "video")
        audio = _track(pub, "audio")
        rx = _Peer(srv)
        ok = await rx.subscribe("video")
        await rx.subscribe("audio")
        video.go.set()
        audio.go.set()
        assert await _until(lambda: len(rx.seen["video"]) >= 7)
        video.end()
        msg = await asyncio.wait_for(rx.done["video"], 5)
        assert msg.status_code == SubscribeDoneCode.TRACK_ENDED
        assert await _until(lambda: rx.seen["video"][-1][2] == EOT)
        *objects, last = rx.seen["video"]
        group, object_id, status = objects[-1]
        assert status == ObjectStatus.NORMAL
        assert last == (group, object_id + 1, EOT)
        assert EOT not in {s for *_, s in objects}
        _eot_before_done(log, ok.track_alias)
        heard = len(rx.seen["audio"])
        assert await _until(lambda: len(rx.seen["audio"]) > heard + 3)
        assert not rx.done["audio"].done()
        assert video._production.done() and not audio._production.done()


@requires_certs
@_TRANSPORTS
async def test_end_between_groups_opens_the_next_group(use_quic):
    async with _loopback(free_udp_port(), 22, use_quic) as (pub, srv):
        video = _track(pub, "video", closed=True)
        audio = _track(pub, "audio")
        rx = _Peer(srv)
        await rx.subscribe("video")
        await rx.subscribe("audio")
        audio.go.set()
        video.go.set()
        assert await _until(lambda: len(rx.seen["video"]) >= 3)
        video.hold.clear()
        await asyncio.sleep(0.1)
        video.end()
        await asyncio.wait_for(rx.done["video"], 5)
        assert await _until(lambda: rx.seen["video"][-1][2] == EOT)
        last_group = max(g for g, _o, s in rx.seen["video"][:-1])
        assert rx.seen["video"][-1] == (last_group + 1, 0, EOT)


@requires_certs
@_TRANSPORTS
async def test_no_end_of_track_at_forward_state_0(use_quic):
    async with _loopback(free_udp_port(), 22, use_quic) as (pub, srv):
        video = _track(pub, "video")
        audio = _track(pub, "audio")
        rx = _Peer(srv)
        await rx.subscribe("video", forward=0)
        await rx.subscribe("audio")
        video.go.set()
        audio.go.set()
        assert await _until(lambda: len(rx.seen["audio"]) >= 3)
        video.end()
        msg = await asyncio.wait_for(rx.done["video"], 5)
        assert msg.status_code == SubscribeDoneCode.TRACK_ENDED
        await asyncio.sleep(0.2)
        assert rx.seen["video"] == []


# -- through the relay ------------------------------------------------------

@contextlib.asynccontextmanager
async def _through_relay(use_quic, draft=22):
    """A relay, a publisher announcing _NS and a subscriber session."""
    relay._announced.clear()
    relay._tracks.clear()
    port = free_udp_port()
    handle = await relay._build_server("localhost", port, CERT, KEY,
                                       use_quic=use_quic,
                                       draft=draft).serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            sessions = []
            for _ in range(2):
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                sessions.append(s)
            pub, sub = sessions
            yield pub, sub
    finally:
        handle.close()
        relay._announced.clear()
        relay._tracks.clear()


@requires_certs
@_TRANSPORTS
async def test_the_relay_forwards_and_caches_end_of_track(use_quic):
    async with _through_relay(use_quic) as (pub, sub):
        tracks = {"video": _track(pub, "video", groups=2,
                                  fetch_history_bytes=1 << 20),
                  "audio": _track(pub, "audio", groups=2, closed=True,
                                  fetch_history_bytes=1 << 20)}
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        rx = _Peer(sub)
        for name in tracks:
            await rx.subscribe(name)
        for track in tracks.values():
            track.go.set()
        for name in tracks:
            await asyncio.wait_for(rx.done[name], 5)
        assert await _until(lambda: all(
            rx.seen[n] and rx.seen[n][-1][2] == EOT for n in tracks))
        assert rx.seen["video"][-1] == (1, 3, EOT)
        assert rx.seen["audio"][-1] == (2, 0, EOT)
        # An ended track's FETCH is answered by its publisher.
        for name in tracks:
            end_group = rx.seen[name][-1][0]
            ok = await sub.fetch(_NS, name, start_group=0,
                                 end_group=end_group, wait_response=True)
            assert ok.end_of_track == 1, (name, ok)


# -- one peer's delivery, against the receive path ----------------------

class _Out:
    def __init__(self, draft):
        self._profile = profile_for(draft)
        self.datagrams = []

    async def dgram_write_drain(self, data):
        self.datagrams.append(bytes(data))


@pytest.mark.parametrize("draft", [14, 16, 18, 22])
async def test_a_datagram_track_ends_in_a_status_datagram(draft):
    out = _Out(draft)
    d = SubgroupDelivery(out, 7, priority=9, mapping=StreamMapping.DATAGRAM)
    await d.end_track((4, 2))
    rx = _session(draft)
    for raw in out.datagrams:
        _feed(rx, raw)
    assert rx.delivered == [(7, 5, 0, EOT)]


async def test_no_end_of_track_where_the_filter_ends_first():
    out = _Out(22)
    d = SubgroupDelivery(out, 7, mapping=StreamMapping.DATAGRAM)
    d.passes = lambda group_id, object_id: group_id <= 4
    await d.end_track((4, 2))
    assert out.datagrams == []


# -- wire ------------------------------------------------------------------

def _end_of_track_object(prof, status=EOT):
    hdr = SubgroupHeader(track_alias=1, group_id=5, subgroup_id=0,
                         publisher_priority=1, extensions_present=True,
                         prof=prof, first_object=True)
    hdr.serialize()
    return bytes(hdr.next_object(payload=b"", object_id=3,
                                 status=status).data)


def _decode(raw, prof):
    obj = ObjectHeader(object_id=0)
    obj.deserialize_into(Buffer(data=raw, vi64=prof.vi64), len(raw), True,
                         None, vi64=prof.vi64,
                         kvp_delta=prof.params_delta_coded)
    return obj


@pytest.mark.parametrize("draft", [14, 16, 18, 20, 22])
def test_end_of_track_object_round_trips(draft):
    prof = profile_for(draft)
    obj = _decode(_end_of_track_object(prof), prof)
    assert (obj.object_id, obj.status, obj.payload) == (3, EOT, b"")


@pytest.mark.parametrize("draft", [14, 16, 18, 20, 22])
def test_an_unknown_object_status_is_a_protocol_violation(draft):
    prof = profile_for(draft)
    raw = _end_of_track_object(prof, status=0x07)
    with pytest.raises(MOQTProtocolViolation, match="unknown object status"):
        _decode(raw, prof)
