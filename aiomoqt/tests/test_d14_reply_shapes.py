"""Replies to a draft-14 peer use draft-14's own messages. TRACK_STATUS is
answered with TRACK_STATUS_OK (SUBSCRIBE_OK's format, Track Alias 0) or
TRACK_STATUS_ERROR (SUBSCRIBE_ERROR's), never d16's REQUEST_OK or
REQUEST_ERROR: d14 assigns their code points (0x07, 0x05) to other
messages, so a d14 peer misparses them."""
import asyncio

import pytest

from aiomoqt.context import profile_for, request_error_code
from aiomoqt.messages.fetch import Fetch
from aiomoqt.messages.namespace import PublishNamespace, SubscribeNamespace
from aiomoqt.messages.publish import Publish
from aiomoqt.messages.request import RequestError
from aiomoqt.messages.subscribe import (Subscribe, SubscribeUpdate,
                                        TrackStatus)
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.track import PublishedTrack
from aiomoqt.types import ContentExistsCode, RequestErrorCode
from aiopquic.buffer import Buffer


def _session(draft=14):
    s = object.__new__(_MOQTSessionMixin)
    s._peer_requests = {}
    s._published_tracks = {}
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s._sent = []
    s.send_control_message = s._sent.append
    s._reply_to_gone_request = lambda *a: False
    return s


def _as_peer_reads(msg, draft=14):
    """What a peer at `draft` decodes from msg's bytes; its fields must
    consume exactly the payload length the message declares."""
    prof = profile_for(draft)
    raw = bytes(msg.serialize(prof=prof).data)
    r = Buffer(data=raw, vi64=prof.vi64)
    mtype = r.pull_vint()
    length = r.pull_uint16()
    start = r.tell()
    cls = _MOQTSessionMixin.CONTROL_REGISTRY[draft][mtype][0]
    back = cls.deserialize(r, prof=prof, buf_end=start + length)
    assert r.tell() - start == length, (cls.__name__, length,
                                        r.tell() - start)
    return back


def _status(rid=4, ns=(b"ns",), name=b"t"):
    return TrackStatus(request_id=rid, track_namespace=ns, track_name=name)


def _code(code):
    return request_error_code(code, profile_for(14))


def test_default_handler_answers_track_status_error():
    s = _session()
    asyncio.run(s._handle_track_status(_status()))
    assert len(s._sent) == 1
    back = _as_peer_reads(s._sent[0])
    assert type(back).__name__ == "TrackStatusError"
    assert back.request_id == 4
    assert back.error_code == _code(RequestErrorCode.NOT_SUPPORTED)


@pytest.mark.parametrize("largest", [None, (3, 7)])
def test_published_track_answers_track_status_ok(largest):
    s = _session()
    track = PublishedTrack(s, "ns", "t")
    track._largest = largest
    s._published_tracks[((b"ns",), b"t")] = track
    msg = _status()
    handler = s._track_handler(msg)
    assert handler is not None
    asyncio.run(handler(s, msg))
    assert len(s._sent) == 1
    back = _as_peer_reads(s._sent[0])
    assert type(back).__name__ == "TrackStatusOk"
    assert (back.request_id, back.track_alias) == (4, 0)
    if largest is None:
        assert back.content_exists == ContentExistsCode.NO_CONTENT
    else:
        assert back.content_exists == ContentExistsCode.EXISTS
        assert (back.largest_group_id, back.largest_object_id) == largest


def test_relay_answers_track_status_ok_and_error(monkeypatch):
    from aiomoqt.tools import moq_interop_relay as relay
    monkeypatch.setattr(relay, "_watch_session", lambda *a, **k: None)

    class _Track:
        finished = False
        pending_publish = ("sess", "msg")
        largest = (7, 42)
        properties = {}

    s = _session()
    monkeypatch.setattr(relay, "_tracks", {((b"live",), b"cam"): _Track()})
    asyncio.run(relay._on_track_status(
        s, _status(ns=(b"live",), name=b"cam")))
    ok = _as_peer_reads(s._sent[-1])
    assert type(ok).__name__ == "TrackStatusOk"
    assert (ok.track_alias, ok.largest_group_id, ok.largest_object_id) == \
        (0, 7, 42)

    asyncio.run(relay._on_track_status(s, _status(ns=(b"nope",), name=b"x")))
    err = _as_peer_reads(s._sent[-1])
    assert type(err).__name__ == "TrackStatusError"
    assert err.error_code == _code(RequestErrorCode.DOES_NOT_EXIST)
    assert len(s._sent) == 2


_REQUESTS = [
    (Subscribe(request_id=6), "SubscribeError"),
    (TrackStatus(request_id=6), "TrackStatusError"),
    (Fetch(fetch_type=1, request_id=6), "FetchError"),
    (Publish(request_id=6), "PublishError"),
    (PublishNamespace(request_id=6), "PublishNamespaceError"),
    (SubscribeNamespace(request_id=6), "SubscribeNamespaceError"),
    (SubscribeUpdate(request_id=6), None),    # d14 defines no reply
]


@pytest.mark.parametrize("msg,want", _REQUESTS,
                         ids=[type(m).__name__ for m, _ in _REQUESTS])
def test_relay_fallback_error_is_the_requests_own(msg, want):
    from aiomoqt.tools import moq_interop_relay as relay

    async def boom(session, m):
        raise RuntimeError("handler bug")

    s = _session()
    asyncio.run(relay._answered(boom)(s, msg))
    if want is None:
        assert s._sent == []
        return
    assert len(s._sent) == 1
    back = _as_peer_reads(s._sent[0])
    assert type(back).__name__ == want
    assert back.request_id == 6


def test_relay_fallback_error_is_request_error_from_d16():
    from aiomoqt.tools import moq_interop_relay as relay

    async def boom(session, m):
        raise RuntimeError("handler bug")

    s = _session(16)
    asyncio.run(relay._answered(boom)(s, Subscribe(request_id=6)))
    assert len(s._sent) == 1
    assert isinstance(s._sent[0], RequestError)
    assert type(_as_peer_reads(s._sent[0], 16)) is RequestError
