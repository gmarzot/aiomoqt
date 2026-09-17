"""Ersatz-relay namespace discovery (§9.4).

d18 splits discovery in two: SUBSCRIBE_NAMESPACE reports the NAMESPACEs
under a prefix, then SUBSCRIBE_TRACKS asks one of them for its tracks.
d14/d16 have no NAMESPACE message, so the prefix subscription is
answered with a PUBLISH per matching track. The relay answered neither
before — it acked and went silent, and a subscriber waited forever.
"""
import asyncio

import pytest

from aiomoqt.context import profile_for
from aiomoqt.messages.namespace import SubscribeNamespace
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.tools import moq_interop_relay as relay


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
    relay._watch_session = lambda sess: None
    relay._tracks.clear()
    relay._announced.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()
    yield
    relay._tracks.clear()
    relay._tracks.update(tracks)
    relay._announced.clear()
    relay._announced.update(announced)
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
