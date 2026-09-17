"""Track discovery — handler registration, the ack, and the walk.

d14/d16 fuse discovery into one request: SUBSCRIBE_NAMESPACE, answered
with a PUBLISH per track. d18 splits it in two — SUBSCRIBE_NAMESPACE
reports which namespaces exist (NAMESPACE), and SUBSCRIBE_TRACKS then
asks one of them for its tracks — so a broad prefix cannot oblige a
relay to announce every track it holds.

The split renumbered SUBSCRIBE_NAMESPACE from 0x11 to 0x50 — the one
control type whose number moved between drafts, and the reason
register_handler must be alias-aware.
"""
from unittest.mock import MagicMock

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import (
    D18MessageType, HANDLER_ALIASES, MOQTMessageType,
)
from aiomoqt.messages.request import Namespace, RequestOk
from aiomoqt.messages.namespace import SubscribeNamespaceOk

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 14520


# -- handler aliasing ------------------------------------------------

def _peer():
    return MOQTClient("localhost", 4433, path="/", use_quic=True)


def test_pre_d18_registration_also_covers_the_d18_point():
    """SUBSCRIBE_NAMESPACE is 0x11 pre-d18 and 0x50 on d18. Registering
    by either name must reach both, or the handler is silently dead on
    one draft and the built-in default runs in its place."""
    def _h(session, msg):
        pass

    peer = _peer()
    peer.register_handler(MOQTMessageType.SUBSCRIBE_NAMESPACE, _h)
    assert peer._control_msg_handlers[
        MOQTMessageType.SUBSCRIBE_NAMESPACE] is _h
    assert peer._control_msg_handlers[
        D18MessageType.SUBSCRIBE_NAMESPACE] is _h


def test_d18_registration_also_covers_the_pre_d18_point():
    def _h(session, msg):
        pass

    peer = _peer()
    peer.register_handler(D18MessageType.SUBSCRIBE_NAMESPACE, _h)
    assert peer._control_msg_handlers[
        MOQTMessageType.SUBSCRIBE_NAMESPACE] is _h


def test_alias_table_is_symmetric():
    """An asymmetric entry covers one direction and leaves the other
    silently dead."""
    for point, aliases in HANDLER_ALIASES.items():
        for alias in aliases:
            assert point in HANDLER_ALIASES.get(alias, ()), (
                f"{point:#x} -> {alias:#x} has no return edge")


def test_aliased_points_are_disjoint_across_drafts():
    """What makes blind expansion safe: an alias number is absent from
    every other draft's table, so the extra key is inert rather than
    hijacking a different message."""
    registry = _MOQTSessionMixin.CONTROL_REGISTRY
    for point, aliases in HANDLER_ALIASES.items():
        drafts = {d for d, tbl in registry.items() if point in tbl}
        for alias in aliases:
            alias_drafts = {d for d, tbl in registry.items() if alias in tbl}
            assert not (drafts & alias_drafts), (
                f"{point:#x} and {alias:#x} both live in draft(s) "
                f"{drafts & alias_drafts} — expansion would collide")


# -- the ack ---------------------------------------------------------

def _fake_session(draft):
    s = MagicMock()
    s.negotiated_draft = draft
    s.sent = []
    s.send_stream_message = lambda sid, m: s.sent.append(m)
    s.send_control_message = lambda m: s.sent.append(m)
    s._make_namespace_tuple = _MOQTSessionMixin._make_namespace_tuple
    return s


@pytest.mark.parametrize("draft,expect", [
    (14, SubscribeNamespaceOk),   # 0x12
    (16, RequestOk),              # 0x12 was removed in d16
    (18, RequestOk),
])
def test_subscribe_namespace_ack_type_per_draft(draft, expect):
    s = _fake_session(draft)
    msg = MagicMock()
    msg.request_id = 3
    _MOQTSessionMixin.subscribe_namespace_ok(s, msg, stream_id=1)
    assert isinstance(s.sent[0], expect)


@pytest.mark.parametrize("draft", [14, 16, 18])
def test_ack_reports_no_namespace_of_its_own(draft):
    """The ack must not assert that the prefix is a namespace we serve —
    only the application knows what it serves."""
    s = _fake_session(draft)
    msg = MagicMock()
    msg.request_id = 3
    _MOQTSessionMixin.subscribe_namespace_ok(s, msg, stream_id=1)
    assert not any(isinstance(m, Namespace) for m in s.sent)


def test_namespace_empty_suffix_means_prefix_itself():
    s = _fake_session(18)
    _MOQTSessionMixin.namespace(s, stream_id=1)
    assert isinstance(s.sent[0], Namespace)
    assert s.sent[0].namespace_suffix == ()


def test_namespace_splits_a_string_suffix():
    s = _fake_session(18)
    _MOQTSessionMixin.namespace(s, "live/cam1", stream_id=1)
    assert s.sent[0].namespace_suffix == (b"live", b"cam1")


# -- end to end ------------------------------------------------------

pytestmark = requires_certs

_NS = "disc/ns"
_TRACK = "found-me"


async def _publish(session):
    track = PublishedTrack(
        session, namespace=_NS, trackname=_TRACK,
        object_size=64, group_size=4, rate=10)
    await track.publish()


async def _on_subscribe_namespace(session, msg):
    """Registered by the d14/d16 name only — on d18 it fires solely
    because register_handler expanded it to 0x50."""
    stream_id = session._bidi_streams.get(msg.request_id)
    session.subscribe_namespace_ok(msg, stream_id=stream_id)
    if session._profile.two_level_discovery:
        session.namespace(stream_id=stream_id)
        return
    await _publish(session)


async def _on_subscribe_tracks(session, msg):
    session.subscribe_tracks_ok(msg)
    await _publish(session)


async def _serve(port, draft):
    server = MOQTServer(
        host="localhost", port=port,
        certificate=CERT, private_key=KEY,
        path="/", use_quic=True, supported_drafts=draft,
    )
    server.register_handler(
        MOQTMessageType.SUBSCRIBE_NAMESPACE, _on_subscribe_namespace)
    server.register_handler(
        D18MessageType.SUBSCRIBE_TRACKS, _on_subscribe_tracks)
    return await server.serve()


@pytest.mark.asyncio
@pytest.mark.parametrize("draft,offset", [(16, 1), (18, 2)])
async def test_discovery_finds_trackname(draft, offset):
    """A subscriber that knows only the namespace learns the trackname —
    over the fused d16 flow and the two-level d18 flow alike."""
    port = _BASE_PORT + offset
    server = await _serve(port, draft)
    try:
        client = MOQTClient(
            "localhost", port, path="/", use_quic=True,
            verify_tls=False, supported_drafts=draft,
        )
        async with client.connect() as session:
            await session.client_session_init()
            track = SubscribedTrack(session, _NS)      # no trackname
            await track.subscribe(timeout=10.0)
            assert track.trackname == _TRACK
    finally:
        server.close()


@pytest.mark.asyncio
async def test_d18_server_reports_namespace_before_tracks():
    """Ordering is the contract: the subscriber cannot ask for tracks
    until it has been told a namespace exists."""
    port = _BASE_PORT + 3
    server = await _serve(port, 18)
    try:
        client = MOQTClient(
            "localhost", port, path="/", use_quic=True,
            verify_tls=False, supported_drafts=18,
        )
        async with client.connect() as session:
            await session.client_session_init()
            await session.subscribe_namespace(
                namespace_prefix=_NS, wait_response=True)
            ns = await session.await_namespace(timeout=10.0)
            assert ns.namespace_suffix == ()
            # No PUBLISH yet — d18 withholds tracks until asked.
            assert session._publish_announcements.empty()
            await session.subscribe_tracks(namespace=_NS)
            pub = await session.await_publish(timeout=10.0)
            assert pub.track_name.decode() == _TRACK
    finally:
        server.close()
