"""Data stream bands. Every session, client and server, raw QUIC and WT,
starts undeclared data streams in MoQT's neutral band; a published track's
streams, and the relay's downstream copies of them, take the band of the
track's publisher priority. per_track by default: tracks share a band
and each track's groups stay in order (test_stream_order.py);
stream_scheduling selects round_robin or fifo."""
import asyncio

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.protocol import _RAW_WRITE_PRIORITY, _MOQTSessionMixin
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import STREAM_BAND_CONTROL, to_stream_priority
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tests.test_relay_forwarding import _reset_relay_state

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 15490
_NS = "prio/bands"
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_SCHEDULINGS = ["per_track", "round_robin", "fifo"]
_SCHEDULING = pytest.mark.parametrize("scheduling", _SCHEDULINGS)
# Track name -> publisher priority.
_TRACKS = {"audio": 64, "video": 200}
_GROUPS = 2
_PER_GROUP = 3


class _Prod(PublishedTrack):
    async def produce(self, out):
        for g in range(_GROUPS):
            for o in range(_PER_GROUP):
                await out.write(g, o, b"x" * 50, group_start=o == 0)


def _spy(monkeypatch, name):
    calls = []
    orig = getattr(_MOQTSessionMixin, name)

    def spy(self, *args):
        ok = orig(self, *args)
        calls.append((self, *args, ok))
        return ok
    monkeypatch.setattr(_MOQTSessionMixin, name, spy)
    return calls


def _spy_open(monkeypatch):
    """Priorities that ride a stream's first write, which calls no setter."""
    calls = []
    orig = _MOQTSessionMixin.open_uni_stream

    async def spy(self, initial=b"", priority=None, end_stream=False):
        sid = await orig(self, initial=initial, priority=priority,
                         end_stream=end_stream)
        if priority is not None and (
                self._is_wt or (_RAW_WRITE_PRIORITY
                                and (initial or end_stream))):
            calls.append((self, sid, priority, True))
        return sid
    monkeypatch.setattr(_MOQTSessionMixin, "open_uni_stream", spy)
    return calls


async def _run(port, use_quic, scheduling, monkeypatch):
    """Two tracks through the relay; objects received per track."""
    _reset_relay_state()
    monkeypatch.setattr(relay, "STREAM_SCHEDULING", scheduling)
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=18)
    handle = await server.serve()
    got = {name: 0 for name in _TRACKS}
    want = _GROUPS * _PER_GROUP
    try:
        pub_client = MOQTClient("localhost", port, path="/",
                                use_quic=use_quic, verify_tls=False,
                                supported_drafts=18)
        pub_client.stream_scheduling = scheduling
        async with pub_client.connect() as pub:
            await pub.client_session_init()
            for name, prio in _TRACKS.items():
                _Prod(pub, _NS, name, priority=prio).attach()
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            sub_client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=18)
            sub_client.stream_scheduling = scheduling
            async with sub_client.connect() as session:
                await session.client_session_init()
                for name in _TRACKS:
                    def on_object(m, s, t, g, sg, name=name):
                        got[name] += 1
                    await SubscribedTrack(session, _NS, name,
                                          on_object=on_object).subscribe(
                                              timeout=8.0)
                async with asyncio.timeout(5.0):
                    while min(got.values()) < want:
                        await asyncio.sleep(0.02)
    finally:
        handle.close()
    assert all(n >= want for n in got.values()), got
    return pub, server


@_SCHEDULING
@_TRANSPORTS
async def test_sessions_default_to_the_neutral_band(
        use_quic, scheduling, monkeypatch):
    defaults = _spy(monkeypatch, "set_default_stream_priority")
    bands = _spy(monkeypatch, "set_stream_priority")
    port = _BASE_PORT + 3 * (not use_quic) + _SCHEDULINGS.index(scheduling)
    await _run(port, use_quic, scheduling, monkeypatch)

    # per_track keeps undeclared streams oldest first, as fifo does.
    neutral = to_stream_priority(
        128, discipline="fifo" if scheduling == "per_track" else scheduling)
    assert neutral == (128 if scheduling == "round_robin" else 129)
    # Publisher, subscriber and the relay's two sessions: one call each.
    assert [(p, ok) for _s, p, ok in defaults] == [(neutral, True)] * 4
    # Control streams stay above every data band.
    control = [p for _s, _sid, p, _ok in bands if p == STREAM_BAND_CONTROL]
    assert control and min(p for _s, _sid, p, _ok in bands) == control[0]


@_SCHEDULING
@_TRANSPORTS
async def test_data_streams_take_their_track_band(
        use_quic, scheduling, monkeypatch):
    bands = _spy(monkeypatch, "set_stream_priority")
    opens = _spy_open(monkeypatch)
    port = (_BASE_PORT + 10 + 3 * (not use_quic)
            + _SCHEDULINGS.index(scheduling))
    pub, server = await _run(port, use_quic, scheduling, monkeypatch)

    want = {to_stream_priority(p, discipline=scheduling)
            for p in _TRACKS.values()}
    assert {p % 2 for p in want} == {1 if scheduling == "fifo" else 0}
    opened = {(id(s), sid) for s, sid, _p, _ok in opens}
    if use_quic and _RAW_WRITE_PRIORITY:
        # Bands ride the first write; per_track's moves up come later.
        assert all(p == STREAM_BAND_CONTROL for s, sid, p, _ok in bands
                   if (id(s), sid) not in opened)
    # Each data stream's band is the first it was given.
    first = {}
    for s, sid, p, ok in opens + bands:
        if p != STREAM_BAND_CONTROL:
            first.setdefault((id(s), sid), (s, p, ok))
    data = list(first.values())
    assert all(ok for _s, _p, ok in data), data
    pub_bands = [p for s, p, _ok in data if s is pub]
    relay_bands = [p for s, p, _ok in data if s._session is server]
    # One stream per group per track, upstream and downstream; per_track
    # opens a track's later stream one step down while an older one still
    # holds transport bytes.
    for got in (pub_bands, relay_bands):
        if scheduling == "per_track":
            got = [p & ~1 for p in got]
        assert sorted(got) == sorted(list(want) * _GROUPS), got


def test_a_broadcast_declares_audio_above_video():
    from types import SimpleNamespace
    from aiomoqt.media.broadcast import (
        AUDIO_PUBLISHER_PRIORITY, CATALOG_PUBLISHER_PRIORITY,
        VIDEO_PUBLISHER_PRIORITY, MediaPublisher)
    from aiomoqt.media.catalog import Catalog
    from aiomoqt.media.loc import LocTrackPublisher

    session = SimpleNamespace()
    pub = MediaPublisher(session, _NS, Catalog(tracks=[]))
    video = pub.add_track(LocTrackPublisher(session, _NS, "video"))
    audio = pub.add_track(LocTrackPublisher(session, _NS, "audio",
                                            media_kind="audio"))
    own = pub.add_track(LocTrackPublisher(session, _NS, "a2",
                                          media_kind="audio", priority=90))
    assert (AUDIO_PUBLISHER_PRIORITY, VIDEO_PUBLISHER_PRIORITY,
            CATALOG_PUBLISHER_PRIORITY) == (64, 128, 64)
    assert pub.catalog_track.priority == CATALOG_PUBLISHER_PRIORITY
    assert (audio.priority, video.priority, own.priority) == (64, 128, 90)
    assert audio._stream_band(session) < video._stream_band(session)


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_datagrams_share_the_neutral_band(use_quic, monkeypatch):
    calls = []
    orig = _MOQTSessionMixin.set_datagram_priority

    def spy(self, priority):
        ok = orig(self, priority)
        calls.append((priority, ok))
        return ok
    monkeypatch.setattr(_MOQTSessionMixin, "set_datagram_priority", spy)
    _reset_relay_state()
    port = _BASE_PORT + 40 + (not use_quic)
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=18)
    handle = await server.serve()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            await asyncio.sleep(0.1)
    finally:
        handle.close()
    band = to_stream_priority(128, discipline="per_track")
    assert len(calls) == 2, calls                       # client and relay
    assert all(c == (band, True) for c in calls), calls
