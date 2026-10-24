"""The relay's upstream subscription covers its subscribers' widest need
in any arrival order (§7.4): Next Object (d22) or Latest Object (d18) at
a relay holding nothing starts at {0, 0} (§3.1.4); unfiltered passes
every Object (§3.3.1). Publishers write before anyone subscribes and send
nothing before a subscription's start. Two tracks, d22 and d18, raw QUIC
and WebTransport."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import SubgroupHeader
from aiomoqt.messages.params import LocationFilter
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack
from aiomoqt.types import FilterType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 20450
_NS = "widest/up"
# "history" answers FETCH from what it published; "bare" keeps nothing.
_NAMES = ("history", "bare")
_DRAFTS = pytest.mark.parametrize("draft", [22, 18], ids=["d22", "d18"])
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _port(case, draft, use_quic):
    return _PORT + 4 * case + 2 * (draft == 18) + (not use_quic)


def _reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
    relay._ns_subs.clear()


class _Groups(PublishedTrack):
    """One object per group, each on its own stream. A subscription gets
    the published objects its window admits, then new ones."""

    def write(self, group_id):
        self._remember(group_id, 0, self._payload(group_id))

    def _payload(self, group_id):
        return f"{self.trackname}-{group_id}".encode()

    async def generate(self, session, track_alias):
        sub = self._sub_for(session)
        sent = set()
        while True:
            last = -1 if self._largest is None else self._largest[0]
            for g in range(last + 1):
                if g in sent or not sub.passes(g, 0):
                    continue
                sent.add(g)
                sid = await session.open_uni_stream()
                hdr = SubgroupHeader(track_alias=track_alias, group_id=g,
                                     subgroup_id=0, publisher_priority=128,
                                     extensions_present=True,
                                     prof=session._profile)
                session.stream_write(sid, hdr.serialize().data)
                await session.stream_write_drain(sid, hdr.next_object(
                    payload=self._payload(g), extensions=None,
                    object_id=0).data)
                session.stream_fin(sid)
            await asyncio.sleep(0.01)


class _Rx:
    """A subscriber session: groups received and how their streams
    ended, by track."""

    def __init__(self, session, draft):
        self.session = session
        self.draft = draft
        self.got = {n: [] for n in _NAMES}
        self.ends = {n: {} for n in _NAMES}

    def _filter(self, how):
        if how == "next":
            return ({"location_filter": LocationFilter.next_object()}
                    if self.draft >= 20
                    else {"filter_type": FilterType.LATEST_OBJECT})
        if how == "none":
            return {"filter_type": None}
        if self.draft >= 20:
            return {"location_filter": LocationFilter.absolute(how)}
        return {"filter_type": FilterType.ABSOLUTE_START,
                "start_group": how, "start_object": 0}

    async def subscribe(self, how):
        for name in _NAMES:
            ok = await self.session.subscribe(
                _NS, name, forward=1, wait_response=True, **self._filter(how),
                on_object=lambda m, _s, _t, g, _sg, n=name:
                self.got[n].append(g) if m.payload else None)
            self.session.register_stream_end_handler(
                ok.track_alias,
                lambda g, sg, clean=True, reset_code=0, n=name:
                self.ends[n].setdefault(g, clean))

    async def has(self, groups, bare=None):
        """Each track got `groups` (the "bare" one `bare` if given), each
        on a stream that ended with a FIN."""
        want = {"history": groups,
                "bare": groups if bare is None else bare}
        await _until(lambda: all(len(self.got[n]) >= len(want[n])
                                 and set(want[n]) <= set(self.ends[n])
                                 for n in _NAMES))
        for name in _NAMES:
            assert self.got[name] == want[name], (name, self.got)
            assert all(self.ends[name].get(g) for g in want[name]), (
                name, self.ends)


async def _until(cond, tries=300):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


@contextlib.asynccontextmanager
async def _relay(port, use_quic, draft, groups):
    """Yields (connect, tracks): a relay, and a publisher session that
    announced _NS with `groups` groups already written on each track."""
    _reset()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=(22, 18))
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return _Rx(s, draft)
            pub = (await connect()).session
            tracks = []
            for name in _NAMES:
                t = _Groups(pub, namespace=_NS, trackname=name,
                            fetch_history_bytes=(1 << 20)
                            if name == "history" else 0)
                t.attach()
                for g in range(groups):
                    t.write(g)
                tracks.append(t)
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            yield connect, tracks
    finally:
        handle.close()
        _reset()


@_TRANSPORTS
@_DRAFTS
@pytest.mark.parametrize("order", ["next-first", "none-first"])
async def test_first_subscriber_does_not_narrow_the_upstream(
        use_quic, draft, order):
    # Next Object first: the relay holds nothing yet, so it is owed
    # {0, 0} on, and so is the unfiltered one behind it. Unfiltered
    # first: the relay's Largest is {0, 0} when the Next Object one
    # arrives, so it starts at {0, 1}. A late unfiltered one gets all.
    case = 0 if order == "next-first" else 1
    async with _relay(_port(case, draft, use_quic), use_quic, draft,
                      groups=1) as (connect, tracks):
        first, second = await connect(), await connect()
        if order == "next-first":
            await first.subscribe("next")
            await second.subscribe("none")
        else:
            await first.subscribe("none")
            await second.subscribe("next")
        await first.has([0])
        for t in tracks:
            t.write(1)
        await first.has([0, 1])
        await second.has([0, 1] if order == "next-first" else [1])
        # Joining a warm track unfiltered: the cached objects first.
        late = await connect()
        await late.subscribe("none")
        await late.has([0, 1])
        for rx in (first, second, late):
            assert rx.session._close_err is None


@_TRANSPORTS
@_DRAFTS
async def test_a_wider_subscriber_widens_the_upstream(use_quic, draft):
    # The upstream SUBSCRIBE starts at group 1 for the first subscriber;
    # an unfiltered one joining later is owed group 0 too, fetched where
    # the publisher keeps it.
    async with _relay(_port(2, draft, use_quic), use_quic, draft,
                      groups=2) as (connect, tracks):
        first = await connect()
        await first.subscribe(1)
        await first.has([1])
        second = await connect()
        await second.subscribe("none")
        for t in tracks:
            t.write(2)
        await first.has([1, 2])
        await second.has([0, 1, 2], bare=[1, 2])
        assert second.session._close_err is None
