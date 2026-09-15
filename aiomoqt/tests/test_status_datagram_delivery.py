"""Status datagrams (END_OF_GROUP / END_OF_TRACK) reach the object
consumer like their subgroup-stream counterparts; consumers filter on
msg.status."""
from aiomoqt.context import profile_for
from aiomoqt.messages.data import ObjectDatagram, ObjectDatagramStatus
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.types import ObjectStatus
from aiopquic.buffer import Buffer


def _session(draft):
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = draft
    s._object_handlers = {}
    s._track_default_priority = {}
    s._malformed_aliases = set()
    s._group_bound = {}
    s._track_bound = {}
    s.delivered = []
    s.on_object_received = lambda msg, size, ts, gid, sgid: \
        s.delivered.append((msg.track_alias, gid, msg.object_id, msg.status))
    s._close_session = lambda code, reason: s.delivered.append(("closed", reason))
    return s


def _feed(s, raw):
    buf = Buffer(data=raw)
    s._moqt_handle_data_dgram(buf)


def test_merged_layout_status_datagram_is_delivered_at_d16_and_d18():
    for draft in (16, 18):
        s = _session(draft)
        dg = ObjectDatagram(track_alias=4, group_id=9, object_id=3,
                            publisher_priority=1,
                            status=ObjectStatus.END_OF_GROUP)
        _feed(s, bytes(dg.serialize(prof=profile_for(draft)).data))
        assert s.delivered == [(4, 9, 3, ObjectStatus.END_OF_GROUP)], draft


def test_d14_status_datagram_is_delivered():
    s = _session(14)
    ds = ObjectDatagramStatus(track_alias=4, group_id=9, object_id=3,
                              publisher_priority=1,
                              status=ObjectStatus.END_OF_TRACK)
    _feed(s, bytes(ds.serialize(prof=profile_for(14)).data))
    assert s.delivered == [(4, 9, 3, ObjectStatus.END_OF_TRACK)]
