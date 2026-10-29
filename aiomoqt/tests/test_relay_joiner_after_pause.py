"""A joiner after any pause gets live media or nothing, never stale: a
track paused since its PUBLISH is resumed against the publisher's live
Largest; an upstream FETCH that succeeds with no objects reaches the
subscriber as an empty FETCH, not an error; a range in a pause's gap goes
upstream instead of being answered short from the cache. Two LOC tracks
on one publisher session, over raw QUIC and WebTransport."""
import asyncio
import contextlib
import logging

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.media import LocTrackPublisher
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.types import FetchType, MOQTRequestError

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._relay_wait import until_published

_PORT = 26800
_NS = "afterpause/ns"
_FPS = 100
# Video opens a group every _GOP frames; every audio frame is a group.
_GOP = {"video": 10, "audio": 1}
_HISTORY = 1 << 20


def _key(name):
    return (tuple(p.encode() for p in _NS.split("/")), name.encode())


def _reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
    relay._catalogs.clear()
    relay._resuming.clear()


async def _until(cond, timeout=6.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond() and loop.time() < deadline:
        await asyncio.sleep(0.02)
    return cond()


async def _source(track, gop, delay=0.0):
    """Frames at _FPS with time-based group ids, as an encoder feeds them,
    from `delay` seconds on."""
    await asyncio.sleep(delay)
    n = 0
    while True:
        await track.send_frame(f"{track.trackname}-{n}".encode(),
                               key_frame=n % gop == 0,
                               group_id=n // gop)
        n += 1
        await asyncio.sleep(1 / _FPS)


def _reply_largest(ok):
    if not getattr(ok, "content_exists", 0):
        return None
    return (ok.largest_group_id, ok.largest_object_id)


def _track_of(msg):
    payload = bytes(msg.payload or b"")
    return next((n for n in _GOP if n.encode() in payload), None)


class _Warm(LocTrackPublisher):
    """Keeps numbering and keeping objects while paused, as a warm start
    does; only the relay's Forward State gates sending."""

    @property
    def producing(self):
        return True


@contextlib.asynccontextmanager
async def _relay(port, use_quic, draft, make, pause_after=None):
    """Yields (connect, tracks): a relay fed by one publisher session
    with a track per _GOP entry, `make(pub, name, gop)` building each and
    returning it with its source coroutine or None; no publisher when
    `make` is None."""
    _reset()
    if pause_after is not None:
        relay._pause_after = pause_after
    handle = await relay._build_server("localhost", port, CERT, KEY,
                                       use_quic=use_quic,
                                       draft=draft).serve()
    sources = []
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s

            tracks = {}
            pub = None if make is None else await connect()
            for name, gop in (_GOP.items() if make else ()):
                track, source = make(pub, name, gop)
                await track.publish(announce_namespace=False,
                                    publish_track=True)
                tracks[name] = track
                if source is not None:
                    sources.append(asyncio.create_task(source))
            if make is not None:
                await until_published(_NS, tuple(_GOP))
            yield connect, tracks
    finally:
        for task in sources:
            task.cancel()
        relay._pause_after = relay.PAUSE_AFTER_S
        handle.close()
        _reset()


def _live(pub, name, gop):
    """make(): a LOC track keeping FETCH history, fed by a source."""
    track = LocTrackPublisher(pub, _NS, name, fetch_history_bytes=_HISTORY,
                              media_kind="audio" if gop == 1 else "video")
    return track, _source(track, gop)


def _collect(session):
    """(live, fetched): objects by track, and fetched Locations by
    request id."""
    live = {name: [] for name in _GOP}
    fetched = {}
    session.on_object_received = (
        lambda msg, _n, _ts, g, _sg: live.get(_track_of(msg), []).append(
            (g, msg.object_id)))
    session.on_fetch_object = (
        lambda obj, _n, _ts, rid: fetched.setdefault(rid, []).append(
            (obj.group_id, obj.object_id)))
    return live, fetched


async def _join(rx, name, draft, joining_start=0, absolute=False):
    """SUBSCRIBE plus a joining FETCH (d16/d18) or fill (d20+). Returns
    (SUBSCRIBE_OK, fetch request id, FETCH_OK or the refusal)."""
    ftype = (FetchType.ABSOLUTE_JOINING if absolute
             else FetchType.RELATIVE_JOINING)
    if draft >= 20:
        ok, _ = await rx.join(_NS, name, fetch_type=ftype,
                              joining_start=joining_start,
                              wait_response=True)
        return ok, ok.request_id, None
    ok = await rx.subscribe(_NS, name, forward=1, wait_response=True)
    msg = rx.joining_fetch(ok.request_id, joining_start=joining_start,
                           fetch_type=ftype)
    try:
        reply = await rx._await_response(msg.request_id)
    except MOQTRequestError as e:
        reply = e
    return ok, msg.request_id, reply


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [16, 18, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_track_paused_since_publish_joins_at_live(draft, use_quic):
    """A publisher with FETCH history from a forwarding period re-PUBLISHes
    to a restarted relay nobody watches (forward=0); its source runs on. A
    joiner gets the live Largest and nothing from before the restart."""
    port = _PORT + 4 * [16, 18, 22].index(draft) + 2 * (not use_quic)
    async with _relay(port, use_quic, draft, _live) as (connect, tracks):
        rx = await connect()
        live, _ = _collect(rx)
        for name in _GOP:
            await rx.subscribe(_NS, name, forward=1, wait_response=True)
        assert await _until(lambda: all(len(v) > 5 for v in live.values()))

        # The restart: a fresh relay, the same tracks re-PUBLISHed.
        _reset()
        async with _relay(port + 1, use_quic, draft, None) as (connect2, _):
            pub = await connect2()
            for track in tracks.values():
                old = track.session
                track.add_session(pub)
                track.drop_session(old)
                track.session = pub
                await track.publish(announce_namespace=False,
                                    publish_track=True, session=pub)
            await until_published(_NS, tuple(_GOP))
            relayed = {name: relay._tracks[_key(name)] for name in _GOP}
            assert all(t.paused and not t.lowered for t in relayed.values())
            published = {name: t.largest for name, t in relayed.items()}
            await asyncio.sleep(0.5)

            rx2 = await connect2()
            live2, fetched = _collect(rx2)
            fetch_of = {}
            for name in _GOP:
                ok, rid, reply = await _join(rx2, name, draft)
                fetch_of[rid] = name
                largest = _reply_largest(ok)
                assert largest is not None and largest > published[name], (
                    name, largest, published[name])
                assert not isinstance(reply, Exception), (name, reply)
                assert await rx2.await_fetch_done(rid, timeout=3.0), name
            assert await _until(lambda: all(live2.values())), live2
            await asyncio.sleep(0.2)
            stale = {rid: [loc for loc in locs
                           if loc <= published[fetch_of[rid]]]
                     for rid, locs in fetched.items()}
            assert not any(stale.values()), (published, stale)
            assert rx2._close_err is None


def _idle(history):
    """make(): a track with Largest (9, 4) and no source, holding groups
    0..2 when `history`, else no FETCH history."""
    def make(pub, name, gop):
        track = LocTrackPublisher(pub, _NS, name,
                                  fetch_history_bytes=_HISTORY if history
                                  else 0,
                                  media_kind="audio" if gop == 1 else "video")
        if history:
            track.prefill(3)
        track._note_largest(9, 4)
        return track, None
    return make


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("upstream", ["empty", "refused"])
async def test_an_empty_upstream_fetch_is_passed_through(upstream, draft,
                                                         use_quic):
    """The publisher's FETCH for the current group succeeds with no
    objects: the joining FETCH gets FETCH_OK and an empty stream, the
    fill a clean empty stream. A publisher refusing it still fails them."""
    port = (_PORT + 20 + 8 * (upstream == "refused")
            + 2 * [18, 22].index(draft) + (not use_quic))
    async with _relay(port, use_quic, draft,
                      _idle(upstream == "empty")) as (connect, _):
        rx = await connect()
        _, fetched = _collect(rx)
        for name in _GOP:
            ok, rid, reply = await _join(rx, name, draft)
            assert _reply_largest(ok) == (9, 4), (name, ok)
            if draft < 20:
                if upstream == "refused":
                    assert isinstance(reply, MOQTRequestError), reply
                    continue
                assert not isinstance(reply, Exception), (name, reply)
                assert reply.largest_group_id == 9, reply
            done = await rx.await_fetch_done(rid, timeout=3.0)
            assert done is (upstream == "empty"), (name, done)
            assert not fetched.get(rid), (name, fetched.get(rid))
        assert rx._close_err is None


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_range_in_the_pause_gap_is_fetched_upstream(
        draft, use_quic, caplog):
    """After a --pause-after pause and resume, a joining FETCH or fill for
    the current group, and one reaching back into the gap, are answered
    from the publisher, which kept what it published while paused."""
    port = _PORT + 40 + 2 * [18, 22].index(draft) + (not use_quic)

    def make(pub, name, gop):
        track = _Warm(pub, _NS, name, fetch_history_bytes=_HISTORY,
                      media_kind="audio" if gop == 1 else "video")
        return track, _source(track, gop)

    caplog.set_level(logging.INFO, logger=relay.logger.name)
    async with _relay(port, use_quic, draft, make,
                      pause_after=0.3) as (connect, _):
        rx = await connect()
        live, _ = _collect(rx)
        oks = [await rx.subscribe(_NS, name, forward=1, wait_response=True)
               for name in _GOP]
        assert await _until(lambda: all(live.values())), live
        for ok in oks:
            rx.unsubscribe(ok.request_id)
        relayed = {name: relay._tracks[_key(name)] for name in _GOP}
        assert await _until(lambda: all(t.lowered
                                        for t in relayed.values()))
        paused_at = {name: t.largest for name, t in relayed.items()}
        await asyncio.sleep(0.5)

        # The resuming joiner: the current group was published while
        # paused.
        rx2 = await connect()
        live2, fetched2 = _collect(rx2)
        current, gapped = {}, 0
        for name in _GOP:
            caplog.clear()
            ok, rid, _ = await _join(rx2, name, draft)
            largest = _reply_largest(ok)
            assert largest > paused_at[name], (name, largest)
            current[name] = largest[0]
            assert await rx2.await_fetch_done(rid, timeout=3.0), name
            want = [(largest[0], o) for o in range(largest[1] + 1)]
            assert fetched2.get(rid) == want, (name, fetched2.get(rid))
            if relayed[name].crosses_gap((largest[0], 0), largest):
                gapped += 1
                assert "upstream FETCH" in caplog.text, name
        assert gapped, "no joining range fell in a pause gap"
        assert await _until(lambda: all(live2.values())), live2
        await asyncio.sleep(0.3)

        # A later joiner reaching back from live groups into the gap.
        rx3 = await connect()
        _, fetched3 = _collect(rx3)
        for name, gop in _GOP.items():
            ok, rid, _ = await _join(rx3, name, draft,
                                     joining_start=current[name],
                                     absolute=True)
            assert await rx3.await_fetch_done(rid, timeout=3.0), name
            got = fetched3.get(rid, [])
            last = _reply_largest(ok)[0]
            assert last > current[name] + 1, (name, current[name], last)
            want = [(g, o) for g in range(current[name], last)
                    for o in range(gop)]
            assert got[:len(want)] == want, (name, want, got)
        for s in (rx, rx2, rx3):
            assert s._close_err is None


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_the_fence_spares_the_live_group(draft, use_quic):
    """A pause shorter than a GOP: the publisher keeps producing the
    group open at the pause, so a joiner gets that group whole, from its
    key frame, out of the cache and from upstream."""
    port = _PORT + 48 + 2 * [18, 22].index(draft) + (not use_quic)
    gops = {"video": 200, "audio": 1}

    def make(pub, name, gop):
        track = _Warm(pub, _NS, name, fetch_history_bytes=_HISTORY,
                      media_kind="audio" if gop == 1 else "video")
        return track, _source(track, gops[name], delay=0.3)

    async with _relay(port, use_quic, draft, make,
                      pause_after=0.3) as (connect, _):
        rx = await connect()
        live, _ = _collect(rx)
        oks = [await rx.subscribe(_NS, name, forward=1, wait_response=True)
               for name in _GOP]
        assert await _until(lambda: all(live.values())), live
        assert live["video"][0] == (0, 0), live["video"][:3]
        for ok in oks:
            rx.unsubscribe(ok.request_id)
        relayed = {name: relay._tracks[_key(name)] for name in _GOP}
        assert await _until(lambda: all(t.lowered
                                        for t in relayed.values()))
        await asyncio.sleep(0.2)

        rx2 = await connect()
        _, fetched = _collect(rx2)
        for name in _GOP:
            ok, rid, reply = await _join(rx2, name, draft)
            assert not isinstance(reply, Exception), (name, reply)
            largest = _reply_largest(ok)
            if name == "video":
                assert largest[0] == 0, largest
            assert await rx2.await_fetch_done(rid, timeout=3.0), name
            want = [(largest[0], o) for o in range(largest[1] + 1)]
            assert fetched.get(rid) == want, (name, fetched.get(rid))
        assert rx2._close_err is None
