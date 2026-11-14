"""per_track scheduling: tracks of one priority share its band, and each
track's streams go oldest first. A stream opened while an older stream of
its track still holds transport bytes waits one step below the band and
moves up once the older one drains; another track's streams never wait on
it. Unit cases on StreamOrder, then two equal-priority tracks through the
relay, raw QUIC and WT, with one track's transport backlog faked."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt import delivery
from aiomoqt.delivery import StreamOrder
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import to_stream_priority, waiting_band
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tests.test_relay_forwarding import _reset_relay_state

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BAND = to_stream_priority(128, discipline="per_track")


class _Quic:
    def __init__(self):
        self.held = {}

    def tx_data_ring_used(self, sid):
        return self.held.get(sid, 0)


class _Session:
    def __init__(self):
        self._quic = _Quic()
        self.moved = {}

    def set_stream_priority(self, sid, band):
        self.moved[sid] = band
        return True


def _open(order, sid, held=0):
    band = order.open_band()
    order.opened(sid, band)
    order.session._quic.held[sid] = held
    return band


def test_bands_for_per_track():
    assert _BAND == 128
    assert waiting_band(_BAND) == 129
    assert waiting_band(_BAND) < to_stream_priority(130,
                                                    discipline="per_track")


async def test_a_later_stream_waits_until_the_older_one_drains():
    s = _Session()
    order = StreamOrder(s, _BAND)
    assert _open(order, 4, held=5000) == _BAND
    assert _open(order, 8, held=900) == waiting_band(_BAND)
    order.check()
    assert s.moved == {}
    s._quic.held[4] = 0
    order.check()
    assert s.moved == {8: _BAND}


async def test_waiting_streams_move_up_oldest_first():
    s = _Session()
    order = StreamOrder(s, _BAND)
    _open(order, 4, held=100)
    _open(order, 8, held=100)
    _open(order, 12, held=100)
    s._quic.held[4] = 0
    order.check()
    assert s.moved == {8: _BAND}
    s._quic.held[8] = 0
    order.check()
    assert s.moved == {8: _BAND, 12: _BAND}


async def test_an_empty_older_stream_holds_nothing_up():
    s = _Session()
    order = StreamOrder(s, _BAND)
    _open(order, 4, held=0)
    assert _open(order, 8) == _BAND


async def test_opening_moves_up_what_has_drained():
    s = _Session()
    order = StreamOrder(s, _BAND)
    _open(order, 4, held=100)
    _open(order, 8, held=100)
    s._quic.held[4] = 0
    s._quic.held[8] = 0
    assert _open(order, 12) == _BAND
    assert s.moved == {8: _BAND}


async def test_tracks_never_wait_on_each_other():
    s = _Session()
    video, audio = StreamOrder(s, _BAND), StreamOrder(s, _BAND)
    _open(video, 4, held=4_000_000)
    _open(video, 8, held=4_000_000)
    assert _open(audio, 12) == _BAND
    assert _open(audio, 16) == _BAND


async def test_a_waiting_stream_moves_up_on_the_timer():
    s = _Session()
    order = StreamOrder(s, _BAND)
    _open(order, 4, held=100)
    _open(order, 8, held=100)
    s._quic.held[4] = 0
    await asyncio.sleep(3 * delivery.ORDER_CHECK_S)
    assert s.moved == {8: _BAND}
    assert order not in delivery._waiting_orders


# Through the relay: two tracks at one priority, every video stream reported
# as holding transport bytes and every audio stream as drained, on the
# publisher's upstream and the relay's downstream.

_BASE_PORT = 15620
_NS = "prio/order"
_GROUPS = 3
_PER_GROUP = 3
_HELD = 10_000      # under any lane's bound, so nothing is shed


class _Prod(PublishedTrack):
    async def produce(self, out):
        for g in range(_GROUPS):
            for o in range(_PER_GROUP):
                await out.write(g, o, b"x" * 50, group_start=o == 0)
            await asyncio.sleep(0.05)


def _orders(name, tracks):
    """The StreamOrders of track `name`: relay downstream and publisher
    upstream."""
    out = []
    for t in relay._tracks.values():
        tname = t.key[1].decode() if isinstance(t.key[1], bytes) else t.key[1]
        if tname == name:
            for per in t._orders.values():
                out.extend(per.values())
    for sub in tracks[name]._subs:
        d = getattr(sub, "delivery", None)
        if d is not None and d.order is not None:
            out.append(d.order)
    return out


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_audio_keeps_its_band_while_video_is_backed_up(
        use_quic, monkeypatch):
    _reset_relay_state()
    monkeypatch.setattr(relay, "STREAM_SCHEDULING", "per_track")
    tracks = {}
    opened = []      # (StreamOrder, sid, band)
    orig_opened = StreamOrder.opened

    def spy_opened(self, sid, band):
        opened.append((self, sid, band))
        return orig_opened(self, sid, band)
    monkeypatch.setattr(StreamOrder, "opened", spy_opened)

    def held_by_video(session):
        def held(sid):
            video = _orders("video", tracks) if tracks else ()
            return _HELD if any(o.session is session and sid in o._streams
                                for o in video) else 0
        return held
    monkeypatch.setattr(relay, "_tx_used", held_by_video)
    monkeypatch.setattr(delivery, "_tx_used", held_by_video)

    port = _BASE_PORT + (not use_quic)
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=18)
    handle = await server.serve()
    got = {"video": [], "audio": []}
    want = _GROUPS * _PER_GROUP
    try:
        pub_client = MOQTClient("localhost", port, path="/",
                                use_quic=use_quic, verify_tls=False,
                                supported_drafts=18)
        async with pub_client.connect() as pub:
            await pub.client_session_init()
            for n in got:
                tracks[n] = _Prod(pub, _NS, n, priority=128)
                tracks[n].attach()
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            sub_client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=18)
            async with sub_client.connect() as session:
                await session.client_session_init()
                for name in got:
                    def on_object(m, s, t, g, sg, name=name):
                        got[name].append(g)
                    await SubscribedTrack(session, _NS, name,
                                          on_object=on_object).subscribe(
                                              timeout=8.0)
                async with asyncio.timeout(5.0):
                    while min(len(v) for v in got.values()) < want:
                        await asyncio.sleep(0.02)
                audio, video = _orders("audio", tracks), _orders("video",
                                                                 tracks)
                bands = {o: [b for oo, _, b in opened if oo is o]
                         for o in audio + video}
                waiting = {o: set(o._waiting) for o in video}
                for o in video:
                    o._used = lambda _sid: 0
                    o.check()
    finally:
        handle.close()
    assert len(audio) == len(video) == 2, (audio, video)
    for o in audio:
        assert bands[o] == [_BAND] * _GROUPS, bands[o]
    for o in video:
        assert bands[o] == [_BAND] + [waiting_band(_BAND)] * (_GROUPS - 1), \
            bands[o]
        assert len(waiting[o]) == _GROUPS - 1 and not o._waiting, waiting
    assert got["video"] == sorted(got["video"]), got
    assert got["audio"] == sorted(got["audio"]), got
