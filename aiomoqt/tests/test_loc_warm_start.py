"""A LOC track with a FETCH history keeps the group in progress while
paused and resumes it mid-group: a joining FETCH (d16/d18) or a
current-group fill (d20+) serves it from its key frame, and live
delivery continues at the next object, with no gap and no duplicate.
Without a history, resume waits for the next key frame. Video and audio
on one publisher session, over raw QUIC and WebTransport, direct and
through aiomoqt-relay."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.media import LocTrackPublisher
from aiomoqt.messages.params import D20ParamType, ParamType
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.types import FetchType, MOQTRequestError

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._relay_wait import until_published
from aiomoqt.tests.test_multi_track_publish import _Receiver, _loopback

_PORT = 27700
_RELAY_PORT = 27760
_NS = "warm/ns"
_HISTORY = 1 << 20
# Video opens a group every _GOP frames; every audio frame is a group.
_GOP = {"video": 8, "audio": 1}
_DRAFTS = [16, 18, 20, 22]
_PRIVATE = b"\x0a\x00"   # empty Private Properties block ahead of a payload


async def _until(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond() and loop.time() < deadline:
        await asyncio.sleep(0.01)
    return cond()


def _frame(name, n):
    return f"{name}-{n}".encode()


async def _feed(tracks, first, last, gap=0.0):
    """Frames first..last on every track, then wait until produced."""
    for n in range(first, last + 1):
        for name, track in tracks.items():
            gop = _GOP[name]
            await track.send_frame(_frame(name, n), key_frame=n % gop == 0,
                                   group_id=n // gop)
        if gap:
            await asyncio.sleep(gap)
    assert await _until(lambda: all(t._frames.empty()
                                    for t in tracks.values()))
    await asyncio.sleep(0.05)


def _expected(name, first, last):
    gop = _GOP[name]
    return [(n // gop, n % gop) for n in range(first, last + 1)]


# -- produce() against a recording delivery ----------------------------

class _Gated(LocTrackPublisher):
    """`producing` set by the test instead of by subscribers."""
    live = True

    @property
    def producing(self):
        return self.live


class _Out:
    def __init__(self):
        self.written = []

    def reset_group(self):
        pass

    async def write(self, group_id, object_id, payload, *, extensions=None,
                    group_start=False):
        self.written.append((group_id, object_id, group_start))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(_GOP))
@pytest.mark.parametrize("history", [_HISTORY, 0], ids=["history", "none"])
async def test_paused_history_holds_one_open_group(name, history):
    gop = _GOP[name]
    track = _Gated(None, _NS, name, fetch_history_bytes=history,
                   media_kind="audio" if gop == 1 else "video")
    out = _Out()
    produce = asyncio.create_task(track.produce(out))
    tracks = {name: track}
    live_end = 2 * gop + 1 if gop > 1 else 2
    await _feed(tracks, 0, live_end)
    live_groups = {g for g, _ in _expected(name, 0, live_end)}
    track.live = False
    paused_end = 5 * gop + 3 if gop > 1 else 9
    for n in range(live_end + 1, paused_end + 1):
        await _feed(tracks, n, n)
        if not history:
            assert track._history is None
            continue
        paused = set(track._history._groups) - live_groups
        assert len(paused) <= 1, (n, sorted(paused))
    head = paused_end // gop
    if history:
        held = track._history.fetch((head, 0), (head, 1 << 62))
        assert [(o.group_id, o.object_id) for o in held] == [
            loc for loc in _expected(name, 0, paused_end) if loc[0] == head]
        assert held[0].payload == _PRIVATE + _frame(name, head * gop)
    track.live = True
    await _feed(tracks, paused_end + 1, paused_end + 2 * gop)
    await track.finish()
    await produce
    resumed = out.written[len(_expected(name, 0, live_end)):]
    if history and gop > 1:
        # Continues the open group at its next object.
        expect = _expected(name, paused_end + 1, paused_end + 2 * gop)
    else:
        first_key = -(-(paused_end + 1) // gop) * gop
        expect = _expected(name, first_key, paused_end + 2 * gop)
    assert [(g, o) for g, o, _ in resumed] == expect
    assert all(start == (o == 0) for _, o, start in resumed)


# -- a direct publisher ------------------------------------------------

def _resume(rx, draft, rid):
    """Raise the subscription's Forward State; from d20 the update also
    asks for a current-group fill. Returns the fill's request id, if any."""
    if draft < 20:
        return rx.request_update(rid, forward=1), None
    fill = {D20ParamType.FILL_PARAMETERS: {
        ParamType.LOCATION_FILTER: rx._joining_filter(
            FetchType.RELATIVE_JOINING, 0)}}
    msg = rx.request_update(rid, forward=1, parameters=fill)
    return msg, msg.request_id


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("draft", _DRAFTS)
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("history", [_HISTORY, 0], ids=["history", "none"])
async def test_direct_resume_mid_group(history, use_quic, draft):
    port = (_PORT + 2 * _DRAFTS.index(draft) + (not use_quic)
            + 10 * (not history))
    rx = _Receiver()
    async with _loopback(port, draft, rx, use_quic=use_quic) as session:
        tracks = {}
        for name, gop in _GOP.items():
            tracks[name] = LocTrackPublisher(
                session, _NS, name, fetch_history_bytes=history,
                media_kind="audio" if gop == 1 else "video")
            tracks[name].attach()
        await session.publish_namespace(namespace=_NS, wait_response=True)
        sub = rx.session
        live = {name: [] for name in _GOP}
        flags = {}
        fetched = {name: [] for name in _GOP}
        oks = {}
        for name in _GOP:
            ok = await sub.subscribe(_NS, name, forward=1,
                                     wait_response=True)
            oks[name] = ok

            def on_object(msg, _n, _ts, g, sg, name=name):
                if not msg.payload:
                    return   # END_OF_GROUP marker
                live[name].append((g, msg.object_id))
                flags[(name, g, msg.object_id)] = (sg, msg.stream_flags[0])
            sub.register_object_handler(ok.track_alias, on_object)
        by_alias = {}
        sub.on_fetch_object = lambda obj, _n, _ts, rid: by_alias.setdefault(
            rid, []).append(obj)

        await _feed(tracks, 0, 17, gap=0.002)
        assert await _until(lambda: len(live["video"]) == 18), live
        for name in _GOP:
            sub.request_update(oks[name].request_id, forward=0)
        assert await _until(lambda: not any(t.producing
                                            for t in tracks.values()))
        await asyncio.sleep(0.1)
        # Video: G2's tail, G3, G4, and G5 objects 0..3.
        await _feed(tracks, 18, 43)
        if history:
            assert set(tracks["video"]._history._groups) == {0, 1, 2, 5}
        for name in _GOP:
            live[name].clear()

        fetch_rids = {}
        for name in _GOP:
            rid = oks[name].request_id
            _, fill_rid = _resume(sub, draft, rid)
            await _until(lambda name=name: tracks[name].producing)
            if fill_rid is not None:
                fetch_rids[name] = fill_rid
                continue
            try:
                f = await sub.joining_fetch(rid, joining_start=0,
                                            wait_response=True)
            except MOQTRequestError:
                assert not history
                continue
            fetch_rids[name] = f.request_id
        if history:
            for name, rid in fetch_rids.items():
                assert await sub.await_fetch_done(rid, timeout=3.0), name
        await _feed(tracks, 44, 58, gap=0.002)
        assert await _until(lambda: len(live["audio"]) >= 15), live
        await asyncio.sleep(0.2)
        for name, rid in fetch_rids.items():
            fetched[name] = [o for o in by_alias.get(rid, ())
                             if o.end_of_range is None]

        if history:
            assert [(o.group_id, o.object_id) for o in fetched["video"]] == [
                (5, i) for i in range(4)]
            assert fetched["video"][0].payload == _PRIVATE + b"video-40"
            assert [(o.group_id, o.object_id) for o in fetched["audio"]] == [
                (43, 0)]
            assert live["video"] == _expected("video", 44, 58)
            assert live["audio"] == _expected("audio", 44, 58)
            if draft >= 18:
                # Same subgroup, reopened: FIRST_OBJECT clear (§2.2).
                assert flags[("video", 5, 4)] == (0, False)
                assert flags[("video", 6, 0)] == (0, True)
        else:
            assert not any(fetched.values()), fetched
            assert live["video"] == _expected("video", 48, 58)
            assert live["audio"] == _expected("audio", 44, 58)
        assert sub._close_err is None


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_direct_resume_in_the_paused_group(use_quic):
    """A pause that ends inside the group it began in: live resumes that
    group at its next object, on a new stream, after the joining FETCH."""
    draft = 18
    port = _PORT + 30 + (not use_quic)
    rx = _Receiver()
    async with _loopback(port, draft, rx, use_quic=use_quic) as session:
        tracks = {}
        for name, gop in _GOP.items():
            tracks[name] = LocTrackPublisher(
                session, _NS, name, fetch_history_bytes=_HISTORY,
                media_kind="audio" if gop == 1 else "video")
            tracks[name].attach()
        await session.publish_namespace(namespace=_NS, wait_response=True)
        sub = rx.session
        live = {name: [] for name in _GOP}
        first = {}
        oks = {}
        for name in _GOP:
            ok = oks[name] = await sub.subscribe(_NS, name, forward=1,
                                                 wait_response=True)

            def on_object(msg, _n, _ts, g, _sg, name=name):
                if msg.payload:
                    live[name].append((g, msg.object_id))
                    first[(name, g, msg.object_id)] = msg.stream_flags[0]
            sub.register_object_handler(ok.track_alias, on_object)
        fetched = []
        sub.on_fetch_object = lambda obj, *_: fetched.append(
            (obj.group_id, obj.object_id))

        await _feed(tracks, 0, 11, gap=0.002)
        assert await _until(lambda: len(live["video"]) == 12), live
        for name in _GOP:
            sub.request_update(oks[name].request_id, forward=0)
        assert await _until(lambda: not any(t.producing
                                            for t in tracks.values()))
        await _feed(tracks, 12, 13)          # video 1.4, 1.5
        for name in _GOP:
            live[name].clear()
        for name in _GOP:
            sub.request_update(oks[name].request_id, forward=1)
        assert await _until(lambda: all(t.producing
                                        for t in tracks.values()))
        f = await sub.joining_fetch(oks["video"].request_id, joining_start=0,
                                    wait_response=True)
        assert await sub.await_fetch_done(f.request_id, timeout=3.0)
        await _feed(tracks, 14, 23, gap=0.002)
        assert await _until(lambda: len(live["video"]) >= 10), live
        await asyncio.sleep(0.2)

        assert fetched == [(1, i) for i in range(6)]
        assert live["video"] == _expected("video", 14, 23)
        assert live["audio"] == _expected("audio", 14, 23)
        assert first[("video", 1, 6)] is False
        assert first[("video", 2, 0)] is True
        assert sub._close_err is None


# -- through aiomoqt-relay ---------------------------------------------

def _relay_reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
    relay._catalogs.clear()
    relay._resuming.clear()


def _track_of(msg):
    payload = bytes(msg.payload or b"")
    return next((n for n in _GOP if n.encode() + b"-" in payload), None)


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_relay_joining_fetch_after_resume(use_quic):
    """d18: a joining FETCH after --pause-after resumes the track goes
    upstream and returns the open group from its key frame."""
    draft = 18
    port = _RELAY_PORT + (not use_quic)
    _relay_reset()
    relay._pause_after = 0.3
    handle = await relay._build_server("localhost", port, CERT, KEY,
                                       use_quic=use_quic,
                                       draft=draft).serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s

            pub = await connect()
            tracks = {}
            for name, gop in _GOP.items():
                tracks[name] = LocTrackPublisher(
                    pub, _NS, name, fetch_history_bytes=_HISTORY,
                    media_kind="audio" if gop == 1 else "video")
                await tracks[name].publish(announce_namespace=False,
                                           publish_track=True)
            await until_published(_NS, tuple(_GOP))

            rx = await connect()
            live = {name: [] for name in _GOP}
            rx.on_object_received = (
                lambda msg, _n, _ts, g, _sg: live.get(
                    _track_of(msg), []).append((g, msg.object_id)))
            oks = [await rx.subscribe(_NS, name, forward=1,
                                      wait_response=True) for name in _GOP]
            await _feed(tracks, 0, 10, gap=0.005)
            assert await _until(lambda: len(live["video"]) == 11), live
            for ok in oks:
                rx.unsubscribe(ok.request_id)
            assert await _until(lambda: not any(
                t.producing for t in tracks.values()), 3.0)
            # Video: G1's tail, G2, G3 objects 0..4.
            await _feed(tracks, 11, 28)

            rx2 = await connect()
            live = {name: [] for name in _GOP}
            ends = {name: [] for name in _GOP}
            first = {}
            fetched = {name: [] for name in _GOP}
            rx2.on_fetch_object = (
                lambda obj, _n, _ts, _rid: fetched.get(
                    _track_of(obj), []).append(
                        (obj.group_id, obj.object_id, bytes(obj.payload))))
            for name in _GOP:
                def on_object(msg, _n, _ts, g, _sg, name=name):
                    if msg.payload:
                        live[name].append((g, msg.object_id))
                        first[(name, g, msg.object_id)] = msg.stream_flags[0]
                    else:
                        ends[name].append(g)
                ok = await rx2.subscribe(_NS, name, forward=1,
                                         wait_response=True)
                rx2.register_object_handler(ok.track_alias, on_object)
                assert (ok.largest_group_id, ok.largest_object_id) == (
                    _expected(name, 28, 28)[0]), name
                f = await rx2.joining_fetch(ok.request_id, joining_start=0,
                                            wait_response=True)
                assert await rx2.await_fetch_done(f.request_id, timeout=3.0)
            await _feed(tracks, 29, 44, gap=0.005)
            assert await _until(lambda: len(live["audio"]) >= 16), live
            await asyncio.sleep(0.2)

            video = [(g, o) for g, o, _ in fetched["video"]]
            assert video == [(3, i) for i in range(5)], fetched["video"]
            assert fetched["video"][0][2] == _PRIVATE + b"video-24"
            assert [(g, o) for g, o, _ in fetched["audio"]] == [(28, 0)]
            # Live continues the open group: no gap, no duplicate.
            assert live["video"] == _expected("video", 29, 44), live
            assert live["audio"] == _expected("audio", 29, 44), live
            assert first[("video", 3, 5)] is False
            assert first[("video", 4, 0)] is True
            # The relay ends the resumed group with its END_OF_GROUP.
            assert ends["video"][:2] == [3, 4], ends
            assert rx2._close_err is None
    finally:
        relay._pause_after = relay.PAUSE_AFTER_S
        handle.close()
        _relay_reset()
