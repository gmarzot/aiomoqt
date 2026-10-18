"""A received object is a whole message: a callback may print, log or
forward it."""
import time
from types import SimpleNamespace

import pytest

from aiomoqt.context import profile_for
from aiomoqt.messages.data import ObjectHeader, SubgroupHeader
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.utils.buffer import Buffer


def _stub(draft):
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = draft
    s._data_streams = {}
    s._control_chains = {}
    s._uni_peek_stash = {}
    s._stream_torn_down = {}
    s._stream_torn_down_last_sweep = time.monotonic()
    s._stream_torn_down_evict_after = 30.0
    s._stream_end_handlers = {}
    s._fetch_done_futures = {}
    s._subgroup_stream_by_key = {}
    s._fetch_stream_by_request = {}
    s._track_aliases = {7: 1}
    s._unbound_aliases = {}
    s._unbound_escalated = set()
    s._malformed_aliases = set()
    s._group_bound = {}
    s._track_bound = {}
    s._object_handlers = {}
    s._track_default_priority = {}
    s._loop = SimpleNamespace(call_later=lambda delay, cb: SimpleNamespace(
        cancel=lambda: None))
    s.closed = []
    s._close_session = lambda code, reason: s.closed.append((int(code), reason))
    return s


@pytest.mark.parametrize("draft", [14, 16, 18])
def test_a_delivered_object_can_be_printed(draft):
    s = _stub(draft)
    seen = []
    s.on_object_received = lambda msg, size, ts, gid, sgid: seen.append(
        (str(msg), repr(msg), msg.type))
    hdr = SubgroupHeader(track_alias=7, group_id=1, subgroup_id=0,
                         prof=profile_for(draft))
    s._on_stream_data(3, bytes(hdr.serialize().data), False)
    s._on_stream_data(3, bytes(hdr.next_object(payload=b"abc").data), False)
    assert not s.closed
    assert len(seen) == 1
    text, rep, _ = seen[0]
    assert "object_id=0" in text and "object_id=0" in rep


def test_a_deserialized_object_can_be_printed():
    hdr = SubgroupHeader(track_alias=7, group_id=1, subgroup_id=0)
    raw = bytes(hdr.next_object(payload=b"abc").data)
    obj = ObjectHeader.deserialize(Buffer(data=raw), len(raw),
                                   extensions_present=False)
    assert obj.payload == b"abc"
    assert "object_id=0" in str(obj) and "object_id=0" in repr(obj)
