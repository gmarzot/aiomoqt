"""Writer — push-style publication, numbering and queue policy.

The track is driven with a stub session and a recording delivery, so the
numbering contract and the on_full policy are tested without a relay.
Numbering matters: objects are numbered ONCE into a delivery the track
spreads across peers, so a wrong number here is wrong for every peer.
"""
import asyncio

import pytest

from aiomoqt.agent import Priority, PublishSpec, SubscribeSpec, TrackRef
from aiomoqt.agent.errors import AgentError
from aiomoqt.agent.session import AgentSession
from aiomoqt.agent.writer import (
    WriteRefused, _as_bytes, build_writer, writers_by_priority,
)
from aiomoqt.delivery import StreamMapping


class _StubSession:
    def __init__(self):
        self.calls = []
        self.handlers = {}

    async def subscribe(self, **kw):
        self.calls.append(kw)
        return type("Ok", (), {"track_alias": 1, "request_id": 1})()

    def register_object_handler(self, alias, cb):
        self.handlers[alias] = cb

    def unregister_object_handler(self, alias):
        self.handlers.pop(alias, None)


class _RecordingOut:
    """Stands in for the fan-out delivery produce() writes into."""

    def __init__(self):
        self.written = []

    async def write(self, group_id, object_id, payload, group_start=False,
                    extensions=None):
        self.written.append((group_id, object_id, payload, group_start))


def _pub(name="decisions", **kw):
    return PublishSpec(track=TrackRef("agent", name), **kw)


def _writer(spec=None):
    return build_writer(_StubSession(), spec or _pub())


async def _drain(track, out, expected):
    """Run produce() until it has written `expected` objects."""
    task = asyncio.ensure_future(track.produce(out))
    for _ in range(500):
        if len(out.written) >= expected:
            break
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# -- numbering --------------------------------------------------------

async def test_snapshot_starts_a_group_and_write_appends():
    wtr, track = _writer()
    await wtr.snapshot({"state": 1})
    await wtr.write({"delta": 1})
    await wtr.write({"delta": 2})
    out = _RecordingOut()
    await _drain(track, out, 3)
    groups = [(g, o, start) for g, o, _p, start in out.written]
    assert groups == [(0, 0, True), (0, 1, False), (0, 2, False)]


async def test_second_snapshot_starts_a_new_group():
    wtr, track = _writer()
    await wtr.snapshot(b"a")
    await wtr.write(b"b")
    await wtr.snapshot(b"c")
    out = _RecordingOut()
    await _drain(track, out, 3)
    assert [(g, o) for g, o, _p, _s in out.written] == [(0, 0), (0, 1), (1, 0)]
    assert [s for *_x, s in out.written] == [True, False, True]


async def test_write_without_a_snapshot_opens_a_group():
    wtr, track = _writer()
    await wtr.write(b"first")
    out = _RecordingOut()
    await _drain(track, out, 1)
    assert out.written[0][:2] == (0, 0)
    assert out.written[0][3] is True


async def test_end_group_forces_the_next_write_into_a_new_group():
    wtr, track = _writer()
    await wtr.write(b"a")
    wtr.end_group()
    await wtr.write(b"b")
    out = _RecordingOut()
    await _drain(track, out, 2)
    assert [g for g, *_r in out.written] == [0, 1]


async def test_group_size_rotates_automatically():
    wtr, track = _writer(_pub(group_size=2))
    for i in range(5):
        await wtr.write(bytes([i]))
    out = _RecordingOut()
    await _drain(track, out, 5)
    assert [g for g, *_r in out.written] == [0, 0, 1, 1, 2]
    assert [o for _g, o, *_r in out.written] == [0, 1, 0, 1, 0]


def test_group_is_negative_before_anything_is_written():
    wtr, _track = _writer()
    assert wtr.group == -1


# -- queue policy -----------------------------------------------------

async def test_drop_new_refuses_once_full():
    wtr, _track = _writer(_pub(buffer=2, on_full="drop_new"))
    for i in range(5):
        await wtr.write(bytes([i]))
    assert wtr.stats.queued == 2
    assert wtr.stats.dropped == 3


async def test_drop_oldest_keeps_the_newest():
    wtr, track = _writer(_pub(buffer=2, on_full="drop_oldest"))
    for i in range(4):
        await wtr.write(bytes([i]))
    out = _RecordingOut()
    await _drain(track, out, 2)
    assert [p for _g, _o, p, _s in out.written] == [b"\x02", b"\x03"]
    assert wtr.stats.dropped == 2


async def test_error_policy_raises():
    wtr, _track = _writer(_pub(buffer=1, on_full="error"))
    await wtr.write(b"a")
    with pytest.raises(WriteRefused):
        await wtr.write(b"b")


async def test_stats_track_bytes_and_groups():
    wtr, _track = _writer()
    await wtr.snapshot(b"abc")
    await wtr.write(b"de")
    await wtr.snapshot(b"f")
    assert wtr.stats.queued == 3
    assert wtr.stats.bytes == 6
    assert wtr.stats.groups == 2


# -- payload encoding -------------------------------------------------

@pytest.mark.parametrize("value,expected", (
    (b"raw", b"raw"),
    ("text", b"text"),
    ({"a": 1}, b'{"a":1}'),
    ([1, 2], b"[1,2]"),
))
def test_payload_encoding(value, expected):
    assert _as_bytes(value) == expected


# -- lifecycle --------------------------------------------------------

async def test_writer_is_not_live_until_started():
    wtr, _track = _writer()
    assert wtr.live is False


async def test_write_after_close_is_refused():
    wtr, _track = _writer()
    await wtr.close()
    with pytest.raises(AgentError, match="closed"):
        await wtr.write(b"a")


async def test_close_is_idempotent():
    wtr, _track = _writer()
    await wtr.close()
    await wtr.close()


async def test_flush_times_out_when_nothing_drains():
    wtr, _track = _writer()
    await wtr.write(b"a")
    with pytest.raises(AgentError, match="still"):
        await wtr.flush(timeout=0.05)


async def test_produce_stops_on_the_close_sentinel():
    wtr, track = _writer()
    await wtr.write(b"a")
    await wtr.close()
    out = _RecordingOut()
    await asyncio.wait_for(track.produce(out), 1.0)
    assert len(out.written) == 1


# -- mapping and priority --------------------------------------------

@pytest.mark.parametrize("mapping,expected", (
    ("per_group", StreamMapping.PER_GROUP),
    ("per_object", StreamMapping.PER_OBJECT),
    ("datagram", StreamMapping.DATAGRAM),
))
def test_mapping_reaches_the_track(mapping, expected):
    _wtr, track = _writer(_pub(mapping=mapping))
    assert track.mapping is expected


def test_publisher_priority_reaches_the_track():
    _wtr, track = _writer(_pub(priority=Priority(publisher=0)))
    assert track.priority == 0


def test_default_priority_when_unset():
    _wtr, track = _writer()
    assert track.priority == 128


def test_writer_requires_a_track_name():
    with pytest.raises(AgentError, match="track.name is required"):
        build_writer(_StubSession(), PublishSpec(track=TrackRef("agent")))


def test_writers_by_priority_orders_most_urgent_first():
    urgent, _t1 = _writer(_pub("decisions", priority=Priority(publisher=0)))
    bulk, _t2 = _writer(_pub("reasoning", priority=Priority(publisher=200)))
    normal, _t3 = _writer(_pub("tools"))
    assert writers_by_priority([bulk, normal, urgent]) == [
        ("agent/decisions", 0), ("agent/tools", 128), ("agent/reasoning", 200)]


# -- session integration ---------------------------------------------

async def test_session_registers_writers():
    agent = AgentSession(_StubSession())
    wtr = agent.writer(_pub("decisions"))
    assert agent.writers == {"agent/decisions": wtr}


async def test_duplicate_writer_rejected():
    agent = AgentSession(_StubSession())
    agent.writer(_pub("decisions"))
    with pytest.raises(AgentError, match="already publishing"):
        agent.writer(_pub("decisions"))


async def test_priority_plan_spans_readers_and_writers():
    """The whole point of connection scope: one relative ordering."""
    agent = AgentSession(_StubSession())
    agent.writer(_pub("decisions", priority=Priority(publisher=0)))
    agent.writer(_pub("reasoning", priority=Priority(publisher=200)))
    await agent.reader(SubscribeSpec(track=TrackRef("world", "events"),
                                     priority=Priority(subscriber=8)))
    plan = agent.priority_plan()
    assert plan["enforced"] is False
    by_track = {t["track"]: t for t in plan["tracks"]}
    assert by_track["agent/decisions"]["publisher"] == 0
    assert by_track["agent/reasoning"]["publisher"] == 200
    assert by_track["world/events"]["subscriber"] == 8


async def test_closing_the_session_closes_writers():
    agent = AgentSession(_StubSession())
    wtr = agent.writer(_pub("decisions"))
    await agent.close()
    assert agent.writers == {}
    with pytest.raises(AgentError, match="closed"):
        await wtr.write(b"a")
