"""SUBSCRIBER_PRIORITY orders a session's data streams before publisher
priority (§5.1.2). Each distinct subscriber priority on a session takes a
slice of the data bands; one, or none sent, keeps the publisher-priority
bands. Two tracks on one session with opposite subscriber and publisher
priorities, raw QUIC and WT, direct and through the relay."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import (
    MOQTMessageType, ParamType, SCHEDULING, STREAM_BAND_DATA_FLOOR,
    STREAM_BAND_PADDING, stream_band_table, to_stream_priority,
)
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tests.test_relay_forwarding import _reset_relay_state

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 35200
_DRAFTS = [18, 20]
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_NS = "prio/sub"
# name -> (publisher priority, subscriber priority): the subscriber puts
# first the track its publisher ranks last.
_TRACKS = {"hi": (200, 10), "lo": (10, 200)}
_MIN_OBJECTS = 6


def _port(case: int, draft: int, use_quic: bool) -> int:
    return (_BASE_PORT + 4 * case + 2 * _DRAFTS.index(draft)
            + (0 if use_quic else 1))


@pytest.mark.parametrize("discipline", SCHEDULING)
def test_one_rank_is_the_publisher_bands(discipline):
    table = stream_band_table(discipline=discipline)
    assert list(table) == [to_stream_priority(p, discipline=discipline)
                           for p in range(256)]


@pytest.mark.parametrize("ranks", [2, 3, 5, 16, 123])
def test_ranks_take_ordered_slices(ranks):
    tables = [stream_band_table(r, ranks, discipline="fifo")
              for r in range(ranks)]
    for r, table in enumerate(tables):
        assert list(table) == sorted(table)
        assert all(b & 1 for b in table)
        assert STREAM_BAND_DATA_FLOOR <= table[0]
        assert table[-1] & 0xFE < STREAM_BAND_PADDING
        if r:
            assert max(tables[r - 1]) < min(table)


def test_rank_out_of_range_raises():
    with pytest.raises(ValueError):
        stream_band_table(2, 2)


class _Session(_MOQTSessionMixin):
    stream_scheduling = "chain"


def test_session_bands_follow_its_subscriber_priorities():
    s = object.__new__(_Session)
    base = stream_band_table(discipline="chain")
    assert s.band_table() == base
    moved = []
    a, b = object(), object()
    s.hold_subscriber_priority(a, 10, lambda: moved.append("a"))
    assert s.band_table(10) == base and moved == []
    s.hold_subscriber_priority(b, 128, lambda: moved.append("b"))
    assert sorted(moved) == ["a", "b"]
    assert max(s.band_table(10)) < min(s.band_table(128))
    assert s.data_stream_band(255, 10) < s.data_stream_band(0, 128)
    moved.clear()
    s.hold_subscriber_priority(b, 128, lambda: moved.append("b"))
    assert moved == []
    s.hold_subscriber_priority(b, None)
    assert moved == ["a"]
    assert s.band_table(10) == base
    with pytest.raises(ValueError):
        s.data_stream_band(256)


class _Produced(PublishedTrack):
    async def produce(self, out):
        group = 0
        while True:
            for oid in range(5):
                await out.write(group, oid, b"x" * 100,
                                group_start=oid == 0)
                await asyncio.sleep(0.005)
            group += 1


def _spy_bands(monkeypatch, sessions=None):
    """Bands of the data streams opened or moved, as (session, chain or
    stream id, band)."""
    seen = []
    orig_open = _MOQTSessionMixin.open_uni_stream
    orig_set = _MOQTSessionMixin.set_stream_priority

    async def spy_open(self, initial=b"", priority=None, end_stream=False,
                       chain=None, order=0):
        sid = await orig_open(self, initial=initial, priority=priority,
                              end_stream=end_stream, chain=chain,
                              order=order)
        seen.append(("open", self, chain, sid, priority))
        return sid

    def spy_set(self, stream_id, priority):
        seen.append(("set", self, None, stream_id, priority))
        return orig_set(self, stream_id, priority)
    monkeypatch.setattr(_MOQTSessionMixin, "open_uni_stream", spy_open)
    monkeypatch.setattr(_MOQTSessionMixin, "set_stream_priority", spy_set)
    return seen


def _opened(seen, session, alias):
    return {band for kind, s, chain, _sid, band in seen
            if kind == "open" and s is session and chain == alias + 1}


class _Receiver:
    def __init__(self):
        self.session = None
        self.counts = {}

    def watch(self, alias):
        def on_object(obj, *_):
            if obj.payload:
                self.counts[alias] = self.counts.get(alias, 0) + 1
        self.session.register_object_handler(alias, on_object)

    async def on_publish_namespace(self, session, msg):
        self.session = session
        session.publish_namespace_ok(msg)


@contextlib.asynccontextmanager
async def _direct(port, draft, use_quic):
    """A publisher client and a server that subscribes to it."""
    rx = _Receiver()
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE,
                            rx.on_publish_namespace)
    quic_server = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            tracks = {n: _Produced(session, namespace=_NS, trackname=n,
                                   priority=pub)
                      for n, (pub, _sp) in _TRACKS.items()}
            for t in tracks.values():
                t.attach()
            await session.publish_namespace(namespace=_NS,
                                            wait_response=True)
            yield session, rx, tracks
    finally:
        quic_server.close()


async def _until(cond, timeout=5.0) -> bool:
    try:
        async with asyncio.timeout(timeout):
            while not cond():
                await asyncio.sleep(0.02)
    except TimeoutError:
        return False
    return True


async def _subscribe_both(rx, with_priority, seen):
    """Subscribe to both tracks; `seen` then holds only what was opened
    with both subscriptions in place."""
    oks = {}
    for name, (_pub, sp) in _TRACKS.items():
        ok = await rx.session.subscribe(
            _NS, name, forward=1, wait_response=True,
            priority=sp if with_priority else None)
        rx.watch(ok.track_alias)
        oks[name] = ok
    del seen[:]
    base = {ok.track_alias: rx.counts.get(ok.track_alias, 0)
            for ok in oks.values()}
    flowing = await _until(lambda: all(
        rx.counts.get(a, 0) >= n + _MIN_OBJECTS for a, n in base.items()))
    assert flowing, rx.counts
    return oks


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_publisher_orders_by_subscriber_priority(draft, use_quic,
                                                       monkeypatch):
    seen = _spy_bands(monkeypatch)
    async with _direct(_port(0, draft, use_quic), draft,
                       use_quic) as (session, rx, tracks):
        oks = await _subscribe_both(rx, True, seen)
        bands = {n: _opened(seen, session, ok.track_alias)
                 for n, ok in oks.items()}
    assert all(bands.values()), bands
    assert max(bands["hi"]) < min(bands["lo"]), bands


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_unset_keeps_publisher_bands(draft, use_quic, monkeypatch):
    seen = _spy_bands(monkeypatch)
    async with _direct(_port(1, draft, use_quic), draft,
                       use_quic) as (session, rx, tracks):
        oks = await _subscribe_both(rx, False, seen)
        sched = session.stream_scheduling
        for name, ok in oks.items():
            pub = _TRACKS[name][0]
            assert _opened(seen, session, ok.track_alias) == {
                to_stream_priority(pub, discipline=sched)}


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_update_moves_bands(draft, use_quic, monkeypatch):
    seen = _spy_bands(monkeypatch)
    async with _direct(_port(2, draft, use_quic), draft,
                       use_quic) as (session, rx, tracks):
        oks = await _subscribe_both(rx, False, seen)
        subs = {n: tracks[n]._sub_for(session) for n in _TRACKS}
        assert (subs["hi"].delivery.stream_priority
                > subs["lo"].delivery.stream_priority)
        for name, (_pub, sp) in _TRACKS.items():
            await rx.session.request_update(
                oks[name].request_id,
                parameters={ParamType.SUBSCRIBER_PRIORITY: sp},
                wait_response=True)
        assert await _until(lambda: subs["hi"].delivery.stream_priority
                            < subs["lo"].delivery.stream_priority)
        moved = {(sid, band) for kind, s, _c, sid, band in seen
                 if kind == "set" and s is session}
        del seen[:]
        alias = oks["hi"].track_alias
        count = rx.counts[alias]
        assert await _until(lambda: rx.counts[alias] >= count + 10)
        band = subs["hi"].delivery.stream_priority
        opened = _opened(seen, session, alias)
    assert band in {b for _sid, b in moved} | opened, (moved, opened)
    assert opened <= {band}, opened


async def _through_relay(port, draft, use_quic, monkeypatch, with_priority,
                         update=False):
    """The relay's bands toward one subscriber session for both tracks;
    with `update`, the priorities come in a REQUEST_UPDATE."""
    _reset_relay_state()
    seen = _spy_bands(monkeypatch)
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    got = {n: 0 for n in _TRACKS}
    try:
        pub_client = MOQTClient("localhost", port, path="/",
                                use_quic=use_quic, verify_tls=False,
                                supported_drafts=draft)
        async with pub_client.connect() as pub:
            await pub.client_session_init()
            for n, (prio, _sp) in _TRACKS.items():
                _Produced(pub, namespace=_NS, trackname=n,
                          priority=prio).attach()
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            sub_client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
            async with sub_client.connect() as session:
                await session.client_session_init()
                aliases, subs = {}, {}
                for name, (_pub, sp) in _TRACKS.items():
                    def on_object(*_, name=name):
                        got[name] += 1
                    t = SubscribedTrack(session, _NS, name,
                                        on_object=on_object)
                    await t.subscribe(
                        timeout=8.0,
                        priority=sp if with_priority and not update
                        else None)
                    aliases[name] = t.track_alias
                    subs[name] = t
                if update:
                    for name, (_pub, sp) in _TRACKS.items():
                        await session.request_update(
                            subs[name].request_id,
                            parameters={ParamType.SUBSCRIBER_PRIORITY: sp},
                            wait_response=True)
                # Only what opens with both subscriptions in place.
                del seen[:]
                base = dict(got)
                assert await _until(lambda: all(
                    got[n] >= base[n] + _MIN_OBJECTS for n in got)), got
    finally:
        handle.close()
    # The relay's opens toward the subscriber: every open not on `pub`.
    bands = {}
    for name, alias in aliases.items():
        bands[name] = {band for kind, s, chain, _sid, band in seen
                       if kind == "open" and s is not pub
                       and chain == alias + 1}
    return bands


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_relay_orders_by_subscriber_priority(draft, use_quic,
                                                   monkeypatch):
    bands = await _through_relay(_port(3, draft, use_quic), draft, use_quic,
                                 monkeypatch, with_priority=True)
    assert all(bands.values()), bands
    assert max(bands["hi"]) < min(bands["lo"]), bands


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_relay_update_moves_bands(draft, use_quic, monkeypatch):
    bands = await _through_relay(_port(5, draft, use_quic), draft, use_quic,
                                 monkeypatch, with_priority=True,
                                 update=True)
    assert all(bands.values()), bands
    assert max(bands["hi"]) < min(bands["lo"]), bands


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_relay_unset_keeps_publisher_bands(draft, use_quic,
                                                 monkeypatch):
    bands = await _through_relay(_port(4, draft, use_quic), draft, use_quic,
                                 monkeypatch, with_priority=False)
    for name, pub_band in (("hi", 200), ("lo", 10)):
        assert bands[name] == {to_stream_priority(
            pub_band, discipline=relay.STREAM_SCHEDULING)}, bands
