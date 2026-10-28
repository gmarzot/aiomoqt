"""A track whose publisher ends it while the relay's head FETCH is
outstanding: the subscriber joining after the PUBLISH_DONE still gets the
cached objects, then a PUBLISH_DONE counting the streams it was sent."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import SubgroupHeader
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.types import FilterType, SubscribeDoneCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 19010
_NS = "relay/done-before-join"
_NAMES = ("a", "b")
# Groups sent after the prefilled group 0, one subgroup stream each.
_GROUPS = (1, 2)
_OBJECTS = 3


class _EndingTrack(PublishedTrack):
    """Group 0 already published; on subscription sends `_GROUPS`, then
    PUBLISH_DONE TRACK_ENDED counting those streams."""

    async def generate(self, session, track_alias):
        self._task = asyncio.ensure_future(self._send(session, track_alias))

    async def _send(self, session, track_alias):
        # SUBSCRIBE_OK goes out first.
        await asyncio.sleep(0.05)
        sub = self._sub_for(session)
        for gid in _GROUPS:
            sid = await session.open_uni_stream()
            hdr = SubgroupHeader(track_alias=track_alias, group_id=gid,
                                 subgroup_id=0, publisher_priority=128,
                                 extensions_present=True,
                                 prof=session._profile)
            session.stream_write(sid, hdr.serialize().data)
            for oid in range(_OBJECTS):
                await session.stream_write_drain(sid, hdr.next_object(
                    payload=f"{self.trackname}-{gid}-{oid}".encode(),
                    extensions=None, object_id=oid).data)
            session.stream_fin(sid)
            sub.stream_count += 1
        self._send_publish_done(session, SubscribeDoneCode.TRACK_ENDED)


def _reset():
    relay._announced.clear()
    relay._tracks.clear()


@contextlib.asynccontextmanager
async def _relay(port, use_quic, draft):
    """A relay and a publisher announcing `_NAMES`; yields a subscriber
    session."""
    _reset()
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
            for name in _NAMES:
                track = _EndingTrack(pub, namespace=_NS, trackname=name,
                                          object_size=16,
                                          group_size=_OBJECTS)
                track.prefill(1)
                track.attach()
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            yield sub
    finally:
        handle.close()
        _reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_subscriber_joining_as_the_track_ends_gets_its_objects(
        draft, use_quic, monkeypatch):
    # The relay's head FETCH goes upstream only once the PUBLISH_DONE has
    # queued the track's terminal behind its undrained objects.
    terminal = {name.encode(): asyncio.Event() for name in _NAMES}
    fetched = []
    enqueue, fetch = (relay._RelayedTrack._enqueue_terminal,
                      relay._fetch_upstream)

    def _enqueue_terminal(self, *args):
        enqueue(self, *args)
        terminal[self.key[1]].set()

    async def _held_fetch(ns, track_name, msg, **kw):
        fetched.append(track_name)
        await asyncio.wait_for(terminal[track_name].wait(), 5.0)
        return await fetch(ns, track_name, msg, **kw)

    monkeypatch.setattr(relay._RelayedTrack, "_enqueue_terminal",
                        _enqueue_terminal)
    monkeypatch.setattr(relay, "_fetch_upstream", _held_fetch)
    port = _PORT + 2 * (draft == 22) + (not use_quic)
    got = {name: [] for name in _NAMES}
    tracks = {}
    async with _relay(port, use_quic, draft) as rx:
        for name in _NAMES:
            tracks[name] = SubscribedTrack(
                rx, _NS, name,
                on_object=lambda m, _s, _t, g, sg, n=name: got[n].append(
                    (g, sg, m.object_id, bytes(m.payload))))
        await asyncio.gather(*(
            t.subscribe(timeout=10.0, filter_type=FilterType.LATEST_OBJECT)
            for t in tracks.values()))
        for _ in range(250):
            if all(t.publish_done is not None for t in tracks.values()):
                break
            await asyncio.sleep(0.02)
    assert sorted(fetched) == sorted(n.encode() for n in _NAMES), fetched
    for name, t in tracks.items():
        # Group 0 from the head FETCH, the rest from the subscription.
        want = [(g, 0, o) for g in (0, *_GROUPS) for o in range(_OBJECTS)]
        assert sorted(x[:3] for x in got[name]) == want, (name, got[name])
        assert all(p == f"{name}-{g}-{o}".encode()
                   for g, _sg, o, p in got[name] if g), got[name]
        assert t.publish_done is not None, f"{name}: no PUBLISH_DONE"
        streams = len({(g, sg) for g, sg, _o, _p in got[name]})
        assert t.publish_done.stream_count == streams == 1 + len(_GROUPS), (
            name, t.publish_done.stream_count, streams)
