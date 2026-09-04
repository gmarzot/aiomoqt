"""Verb-surface matrix: every public session verb, both roles, all
drafts — the API must exist and emit exactly one spec-legal frame on
the draft-correct channel (or terminate the right stream where the
draft removed the message).

Each frame is independently checked: type present in the draft's
CONTROL_MESSAGE_TYPES table, 16-bit length exactly covering the body,
body deserializable by the draft registry's class.

The GAPS section pins the verbs the API cannot yet initiate; closing
one without updating the matrix fails the ratchet on purpose.
"""
import asyncio
from collections import deque

import pytest

from aiomoqt.protocol import _MOQTSessionMixin, MOQTSessionQuic
from aiomoqt.context import profile_for
from aiomoqt.types import CONTROL_MESSAGE_TYPES
from aiomoqt.messages.subscribe import Subscribe
from aiomoqt.messages.namespace import (
    PublishNamespace, SubscribeNamespace, SubscribeTracks,
)
from aiopquic.buffer import Buffer


class _Cfg:
    libquicr_compat = False


def _session(draft, is_client=True):
    s = object.__new__(_MOQTSessionMixin)
    s._session = _Cfg()
    try:
        s._loop = asyncio.get_running_loop()
    except RuntimeError:
        s._loop = None
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s.is_client = is_client
    s._next_request_id = 0 if is_client else 1
    s._sent_requests = deque(maxlen=64)
    s._pending_requests = {}
    s._next_track_alias = 0
    s._track_aliases = {}
    s._subscriptions = {}
    s._fetch_done_futures = {}
    s._bidi_streams = {7: 9}
    s._bidi_stream_requests = {9: 7}
    s._request_cancel_handlers = {}
    s._peer_request_max = -1
    s._peer_goaway = False
    s.frames = []          # (channel, wire bytes)
    s.resets = []
    s.send_control_message = lambda m: s.frames.append(
        ("control", bytes(m.serialize(prof=s._profile).data)))
    s.send_stream_message = lambda sid, m: s.frames.append(
        ("request", bytes(m.serialize(prof=s._profile).data)))
    s._send_request = lambda rid, m: s.frames.append(
        ("request", bytes(m.serialize(prof=s._profile).data)))
    s.stream_reset = lambda sid, code: s.resets.append(("reset", sid, code))
    s.stream_stop_sending = lambda sid, code: s.resets.append(
        ("stop", sid, code))

    async def _open(*a, **k):
        return 9
    s.open_bidi_stream = _open
    s.open_uni_stream = _open
    return s


def _check_frame(draft, blob):
    prof = profile_for(draft)
    buf = Buffer(data=blob, vi64=prof.vi64)
    t = int(buf.pull_vint())
    assert t in CONTROL_MESSAGE_TYPES[draft], (
        f"type 0x{t:x} not defined by draft-{draft}")
    mlen = buf.pull_uint16()
    assert buf.tell() + mlen == len(blob), "length must cover the body"
    entry = next(v for k, v in
                 MOQTSessionQuic.CONTROL_REGISTRY[draft].items()
                 if int(k) == t)
    entry[0].deserialize(buf, prof=prof, buf_end=buf.tell() + mlen)
    return t


# --- the matrix ------------------------------------------------------
# (name, drafts, expected, call). expected: "control" / "request" /
# "reply" (request stream at d18, control before) / "reset".

def _sub_msg():
    return Subscribe(request_id=7, track_namespace=(b"a",),
                     track_name=b"t", filter_type=2)


MATRIX = [
    # -- initiators --
    ("subscribe", (14, 16, 18), "request",
     lambda s: s.subscribe(namespace="a/b", track_name="t",
                           wait_response=False)),
    ("fetch", (14, 16, 18), "request",
     lambda s: s.fetch(namespace="a", track_name="t", start_group=0,
                       start_object=0, end_group=1, end_object=0,
                       wait_response=False)),
    ("publish", (14, 16, 18), "request",
     lambda s: s.publish(namespace="a", track_name="t")),
    ("publish_namespace", (14, 16, 18), "request",
     lambda s: s.publish_namespace(namespace="a")),
    ("subscribe_namespace", (14, 16, 18), "request",
     lambda s: s.subscribe_namespace(namespace_prefix="a")),
    ("subscribe_tracks", (18,), "request",
     lambda s: s.subscribe_tracks(namespace="a")),
    ("goaway", (14, 16, 18), "control",
     lambda s: s.goaway()),
    # -- withdrawals: messages where they exist, stream ends after --
    ("unsubscribe", (14, 16), "control",
     lambda s: s.unsubscribe(request_id=7)),
    ("unsubscribe@18", (18,), "reset",
     lambda s: s.unsubscribe(request_id=7)),
    # NOTE: str tuples crash serialize here (API wart — tuple input
    # bypasses _make_namespace_tuple); bytes required.
    ("publish_namespace_done@14", (14,), "control",
     lambda s: s.publish_namespace_done(namespace=(b"a",))),
    ("publish_namespace_done", (16,), "control",
     lambda s: s.publish_namespace_done(request_id=7)),
    ("publish_namespace_done@18", (18,), "reset",
     lambda s: s.publish_namespace_done(request_id=7)),
    ("unsubscribe_namespace@14", (14,), "control",
     lambda s: s.unsubscribe_namespace(namespace_prefix="a")),
    ("unsubscribe_namespace", (16, 18), "reset",
     lambda s: s.unsubscribe_namespace(request_id=7)),
    # -- replies --
    ("subscribe_ok", (14, 16, 18), "reply",
     lambda s: s.subscribe_ok(request_msg=_sub_msg())),
    ("subscribe_error", (14, 16, 18), "reply",
     lambda s: s.subscribe_error(request_id=7)),
    ("subscribe_done", (14, 16, 18), "reply",
     lambda s: s.subscribe_done(request_id=7)),
    ("fetch_ok", (14, 16, 18), "reply",
     lambda s: s.fetch_ok(request_id=7)),
    ("fetch_error", (14, 16, 18), "reply",
     lambda s: s.fetch_error(request_id=7)),
    ("publish_namepace_ok", (14, 16, 18), "reply",
     lambda s: s.publish_namepace_ok(
         PublishNamespace(request_id=7, namespace=(b"a",),
                          parameters={}))),
    # Namespace-family replies ride the request bidi from d16 on
    # (d16 already moved SUBSCRIBE_NAMESPACE to its own stream).
    ("subscribe_namespace_ok", (14, 16, 18), "nsreply",
     lambda s: s.subscribe_namespace_ok(
         SubscribeNamespace(request_id=7, namespace_prefix=(b"a",),
                            parameters={}), stream_id=9)),
    ("subscribe_tracks_ok", (18,), "nsreply",
     lambda s: s.subscribe_tracks_ok(
         SubscribeTracks(request_id=7, namespace_prefix=(b"a",),
                         parameters={}))),
    ("namespace", (16, 18), "nsreply",
     lambda s: s.namespace(namespace_suffix=(b"a",), stream_id=9)),
]


@pytest.mark.parametrize(
    "name,drafts,expected,call",
    MATRIX, ids=[m[0] for m in MATRIX])
def test_verb_emits_a_legal_frame(name, drafts, expected, call):
    async def _run():
        for draft in drafts:
            s = _session(draft)
            result = call(s)
            if asyncio.iscoroutine(result):
                await result
            if expected == "reset":
                assert s.resets, f"{name}@{draft}: no stream termination"
                assert not s.frames, (
                    f"{name}@{draft}: sent a frame where the draft "
                    f"removed the message")
                continue
            assert len(s.frames) == 1, (
                f"{name}@{draft}: expected exactly one frame, got "
                f"{[c for c, _ in s.frames]}")
            channel, blob = s.frames[0]
            if expected == "reply":
                want = "request" if draft >= 18 else "control"
            elif expected == "nsreply":
                want = "request" if draft >= 16 else "control"
            else:
                want = expected
            # Initiators pre-d18 legally ride the control stream.
            if expected == "request" and draft < 18:
                want = channel  # either path is legal before d18
            assert channel == want, (
                f"{name}@{draft}: frame on {channel}, expected {want}")
            _check_frame(draft, blob)
    asyncio.run(_run())


# --- ratchet: known verb-surface gaps --------------------------------
# Closing a gap must update this list, so the matrix stays the single
# statement of API completeness.

GAPS = ["track_status", "request_update", "publish_ok"]


@pytest.mark.parametrize("verb", GAPS)
def test_gap_is_still_open(verb):
    s = _session(18)
    assert not hasattr(s, verb), (
        f"'{verb}' now exists — move it from GAPS into MATRIX")
