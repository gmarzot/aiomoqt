"""Ersatz-relay namespace discovery (§9.4).

d18 splits discovery in two: SUBSCRIBE_NAMESPACE reports the NAMESPACEs
under a prefix, then SUBSCRIBE_TRACKS asks one of them for its tracks.
d14/d16 have no NAMESPACE message, so the prefix subscription is
answered with a PUBLISH per matching track. The relay answered neither
before — it acked and went silent, and a subscriber waited forever.
"""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.messages import SubgroupHeader
from aiomoqt.messages.namespace import SubscribeNamespace
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import D16MessageType, MOQTRequestError

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 16500
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_FRAMES = [b"o0", b"o1"]


def _session(draft):
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s._bidi_streams = {5: 9}
    s._request_cancel_handlers = {}
    s._namespaces = []
    s._acks = []
    s.namespace = lambda suffix, request_id=None, stream_id=None: (
        s._namespaces.append((tuple(suffix), request_id)))
    s.subscribe_namespace_ok = lambda msg, stream_id=None: (
        s._acks.append(stream_id))
    return s


@pytest.fixture(autouse=True)
def _clean_tables():
    tracks, announced = dict(relay._tracks), dict(relay._announced)
    watch = relay._watch_session
    relay._watch_session = lambda sess, ns=None: None
    relay._tracks.clear()
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()
    yield
    relay._tracks.clear()
    relay._tracks.update(tracks)
    relay._announced.clear()
    relay._announced.update(announced)
    relay._announce_requests.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()
    relay._watch_session = watch


def _sub_ns(prefix):
    return SubscribeNamespace(request_id=5, namespace_prefix=prefix)


def test_d18_reports_namespaces_under_the_prefix():
    s = _session(18)
    relay._announced[(b"live", b"cam1")] = {}
    relay._announced[(b"live", b"cam2")] = {}
    relay._announced[(b"other",)] = {}
    asyncio.run(relay._on_subscribe_namespace(s, _sub_ns((b"live",))))
    assert s._acks == [9]                       # acked on the request stream
    assert sorted(s._namespaces) == [((b"cam1",), 5), ((b"cam2",), 5)]


def test_d18_later_announcement_reaches_the_subscriber():
    s = _session(18)
    asyncio.run(relay._on_subscribe_namespace(s, _sub_ns((b"live",))))
    assert s._namespaces == []
    relay._announce_namespace((b"live", b"cam9"))
    assert s._namespaces == [((b"cam9",), 5)]


def test_d18_ignores_namespaces_outside_the_prefix():
    s = _session(18)
    asyncio.run(relay._on_subscribe_namespace(s, _sub_ns((b"live",))))
    relay._announce_namespace((b"elsewhere", b"cam"))
    assert s._namespaces == []


@pytest.mark.parametrize("draft", [14, 16])
def test_pre_d18_registers_for_publish_fanout(draft):
    # No NAMESPACE message before d18: the prefix subscription is served
    # by PUBLISH per track, so the subscriber is registered for it.
    s = _session(draft)
    asyncio.run(relay._on_subscribe_namespace(s, _sub_ns((b"live",))))
    assert s._namespaces == []
    assert [(e[1], e[2]) for e in relay._track_subs] == [((b"live",), 5)]


# -- end to end: publisher -> relay -> subscriber -------------------------

class _Pub(PublishedTrack):
    """One group of _FRAMES on a single subgroup."""

    async def generate(self, session, track_alias):
        sid = await session.open_uni_stream()
        hdr = SubgroupHeader(
            track_alias=track_alias, group_id=0, subgroup_id=0,
            publisher_priority=128, extensions_present=True,
            prof=session._profile)
        session.stream_write(sid, hdr.serialize().data)
        for i, payload in enumerate(_FRAMES):
            buf = hdr.next_object(payload=payload, extensions=None,
                                  object_id=i)
            await session.stream_write_drain(sid, buf.data)
        self._largest = (0, len(_FRAMES) - 1)


@contextlib.asynccontextmanager
async def _relay(port, use_quic, draft):
    """A relay and two sessions on it: (publisher, subscriber)."""
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
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
            yield sessions
    finally:
        handle.close()


async def _receive(sub, namespace, name):
    """Subscribe and return the payloads that arrive."""
    got = []
    await SubscribedTrack(
        sub, namespace, name,
        on_object=lambda m, *_: got.append(bytes(m.payload)),
    ).subscribe(timeout=5.0)
    for _ in range(150):
        if len(got) >= len(_FRAMES):
            break
        await asyncio.sleep(0.02)
    return got


@requires_certs
@_TRANSPORTS
async def test_d18_cancelled_announcement_retracts_its_namespace(use_quic):
    # §3.3.2: d18 withdraws by cancelling the PUBLISH_NAMESPACE request.
    seen: asyncio.Queue = asyncio.Queue()

    async def _ns(session, msg):
        await seen.put(("NAMESPACE", tuple(msg.namespace_suffix)))

    async def _done(session, msg):
        await seen.put(("DONE", tuple(msg.namespace_suffix)))

    async with _relay(_BASE_PORT + (not use_quic), use_quic, 18) as (
            pub, sub):
        sub.register_handler(D16MessageType.NAMESPACE, _ns)
        sub.register_handler(D16MessageType.NAMESPACE_DONE, _done)
        await pub.publish_namespace(namespace="wd/a", wait_response=True)
        await pub.publish_namespace(namespace="wd/b", wait_response=True)
        await sub.subscribe_namespace("wd", wait_response=True)
        got = {await asyncio.wait_for(seen.get(), 5.0) for _ in range(2)}
        assert got == {("NAMESPACE", (b"a",)), ("NAMESPACE", (b"b",))}
        pub.publish_namespace_done(namespace="wd/a")
        assert await asyncio.wait_for(seen.get(), 5.0) == ("DONE", (b"a",))
        await asyncio.sleep(0.1)
        assert seen.empty()
        assert list(relay._announced) == [(b"wd", b"b")]


@requires_certs
@pytest.mark.parametrize("draft", [14, 16])
@_TRANSPORTS
async def test_namespace_done_withdraws_only_its_namespace(draft, use_quic):
    # PUBLISH_NAMESPACE_DONE names the namespace at d14, only the request
    # at d16.
    port = _BASE_PORT + 10 + 2 * (draft - 14) + (not use_quic)
    async with _relay(port, use_quic, draft) as (pub, sub):
        for ns in ("wd/a", "wd/b"):
            _Pub(pub, ns, "video").attach()
            await pub.publish_namespace(namespace=ns, wait_response=True)
        pub.publish_namespace_done(namespace="wd/a")
        for _ in range(100):
            if (b"wd", b"a") not in relay._announced:
                break
            await asyncio.sleep(0.02)
        assert list(relay._announced) == [(b"wd", b"b")]
        with pytest.raises(MOQTRequestError):
            await SubscribedTrack(sub, "wd/a", "video").subscribe(timeout=5.0)
        assert await _receive(sub, "wd/b", "video") == _FRAMES


@requires_certs
@_TRANSPORTS
async def test_a_slash_inside_a_namespace_element_survives_the_relay(
        use_quic):
    async with _relay(_BASE_PORT + 20 + (not use_quic), use_quic, 18) as (
            pub, sub):
        # Upstream SUBSCRIBE for an announced namespace.
        _Pub(pub, (b"x/y",), "announced").attach()
        await pub.publish_namespace(namespace=(b"x/y",), wait_response=True)
        assert await _receive(sub, (b"x/y",), "announced") == _FRAMES
        # PUBLISH offered to a SUBSCRIBE_TRACKS subscriber.
        await _Pub(pub, (b"p/q",), "offered").publish(
            announce_namespace=False, publish_track=True)
        await sub.subscribe_tracks(namespace=(b"p/q",))
        msg = await sub.await_publish(timeout=5.0)
        assert tuple(msg.track_namespace) == (b"p/q",)
        sub.publish_ok(msg, forward=0)
