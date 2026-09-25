"""AgentSession and Reader — bounded reads over a stubbed session.

No relay and no event loop beyond asyncio: the point is the interface
contract, so the session is a stub that records what it was asked for
and objects are injected directly into the reader's ingest callback.
"""
import asyncio

import pytest

from aiomoqt.agent import Priority, StartAt, SubscribeSpec, TrackRef
from aiomoqt.agent.errors import AgentError
from aiomoqt.agent.reader import Obj, ReadTimeout
from aiomoqt.agent.session import AgentSession, start_at_to_wire
from aiomoqt.types import FilterType, GroupOrder


class _Ok:
    def __init__(self, alias):
        self.track_alias = alias


class _StubSession:
    def __init__(self, alias=1):
        self.calls = []
        self.handlers = {}
        self._alias = alias

    async def subscribe(self, **kwargs):
        self.calls.append(kwargs)
        alias = self._alias
        self._alias += 1
        return _Ok(alias)

    def register_object_handler(self, alias, cb):
        self.handlers[alias] = cb

    def unregister_object_handler(self, alias):
        self.handlers.pop(alias, None)


class _Msg:
    def __init__(self, object_id, payload=b"x", extensions=None):
        self.object_id = object_id
        self.payload = payload
        self.extensions = extensions


def _feed(reader, group, count, start=0, recv_us=1_000_000):
    for i in range(start, start + count):
        reader.on_object(_Msg(i), len(b"x"), recv_us + i, group, 0)


def _spec(name="video", **kw):
    return SubscribeSpec(track=TrackRef("live/cam-1", name), **kw)


# -- StartAt to wire --------------------------------------------------

@pytest.mark.parametrize("start,expected", (
    (StartAt.latest(), (FilterType.LATEST_OBJECT, 0, 0, 0)),
    (StartAt.next_group(), (FilterType.NEXT_GROUP_START, 0, 0, 0)),
    (StartAt.at(120, 3), (FilterType.ABSOLUTE_START, 120, 3, 0)),
    (StartAt.range(100, 200), (FilterType.ABSOLUTE_RANGE, 100, 0, 200)),
))
def test_start_at_maps_to_wire(start, expected):
    assert start_at_to_wire(start) == expected


# -- subscribe plumbing ----------------------------------------------

async def test_reader_sends_the_spec_to_the_session():
    agent = AgentSession(_StubSession())
    await agent.reader(_spec(
        start_at=StartAt.at(120), priority=Priority(subscriber=8,
                                                    group_order="descending"),
        forward=False))
    sent = agent.session.calls[0]
    assert sent["namespace"] == "live/cam-1"
    assert sent["track_name"] == "video"
    assert sent["filter_type"] is FilterType.ABSOLUTE_START
    assert sent["start_group"] == 120
    assert sent["priority"] == 8
    assert sent["group_order"] is GroupOrder.DESCENDING
    assert sent["forward"] == 0


async def test_reader_registers_for_its_alias():
    stub = _StubSession(alias=7)
    agent = AgentSession(stub)
    rdr = await agent.reader(_spec())
    assert 7 in stub.handlers
    agent.close_reader(rdr.name)
    assert 7 not in stub.handlers


async def test_two_readers_share_one_connection():
    """Per-alias dispatch, so tracks do not overwrite each other."""
    stub = _StubSession()
    agent = AgentSession(stub)
    a = await agent.reader(_spec("video"))
    b = await agent.reader(_spec("audio"))
    assert set(stub.handlers) == {a.alias, b.alias}
    _feed(a, group=1, count=2)
    assert a.buffered == 2 and b.buffered == 0


async def test_reader_requires_a_track_name():
    agent = AgentSession(_StubSession())
    with pytest.raises(AgentError, match="track.name is required"):
        await agent.reader(SubscribeSpec(track=TrackRef("ns")))


async def test_duplicate_subscription_rejected():
    agent = AgentSession(_StubSession())
    await agent.reader(_spec())
    with pytest.raises(AgentError, match="already subscribed"):
        await agent.reader(_spec())


async def test_closed_session_refuses_work():
    agent = AgentSession(_StubSession())
    await agent.close()
    with pytest.raises(AgentError, match="closed"):
        await agent.reader(_spec())


# -- bounded reads ----------------------------------------------------

async def test_read_returns_exactly_n():
    agent = AgentSession(_StubSession())
    rdr = await agent.reader(_spec())
    _feed(rdr, group=1, count=10)
    got = await rdr.read(4)
    assert [o.object_id for o in got] == [0, 1, 2, 3]
    assert rdr.buffered == 6


async def test_read_waits_for_late_objects():
    agent = AgentSession(_StubSession())
    rdr = await agent.reader(_spec())

    async def later():
        await asyncio.sleep(0.01)
        _feed(rdr, group=1, count=3)

    asyncio.ensure_future(later())
    got = await rdr.read(3, timeout=2.0)
    assert len(got) == 3


async def test_read_returns_short_on_deadline_by_default():
    agent = AgentSession(_StubSession())
    rdr = await agent.reader(_spec())
    _feed(rdr, group=1, count=2)
    got = await rdr.read(5, timeout=0.05)
    assert len(got) == 2


async def test_read_can_demand_the_full_count():
    agent = AgentSession(_StubSession())
    rdr = await agent.reader(_spec())
    _feed(rdr, group=1, count=2)
    with pytest.raises(ReadTimeout):
        await rdr.read(5, timeout=0.05, partial=False)


async def test_read_group_stops_at_the_boundary():
    agent = AgentSession(_StubSession())
    rdr = await agent.reader(_spec())
    _feed(rdr, group=1, count=3)
    _feed(rdr, group=2, count=3)
    first = await rdr.read_group(timeout=0.05)
    assert [o.group for o in first] == [1, 1, 1]
    second = await rdr.read_group(timeout=0.05)
    assert [o.group for o in second] == [2, 2, 2]


async def test_read_returns_when_the_publisher_is_done():
    agent = AgentSession(_StubSession())
    rdr = await agent.reader(_spec())
    _feed(rdr, group=1, count=1)
    rdr.mark_done()
    got = await rdr.read(10, timeout=5.0)
    assert len(got) == 1 and rdr.done


async def test_read_rejects_a_nonsense_count():
    agent = AgentSession(_StubSession())
    rdr = await agent.reader(_spec())
    with pytest.raises(AgentError):
        await rdr.read(0)


# -- ring policy and stats -------------------------------------------

async def test_ring_sheds_oldest_when_full():
    agent = AgentSession(_StubSession())
    rdr = await agent.reader(_spec(buffer=4))
    _feed(rdr, group=1, count=10)
    got = rdr.drain()
    assert len(got) == 4
    assert [o.object_id for o in got] == [6, 7, 8, 9]
    assert rdr.stats.dropped == 6


async def test_drop_new_keeps_the_oldest():
    agent = AgentSession(_StubSession())
    rdr = await agent.reader(_spec(buffer=4, on_full="drop_new"))
    _feed(rdr, group=1, count=10)
    assert [o.object_id for o in rdr.drain()] == [0, 1, 2, 3]
    assert rdr.stats.dropped == 6


async def test_stats_count_groups_and_bytes():
    agent = AgentSession(_StubSession())
    rdr = await agent.reader(_spec())
    _feed(rdr, group=1, count=3)
    _feed(rdr, group=2, count=2)
    assert rdr.stats.received == 5
    assert rdr.stats.groups == 2
    assert rdr.stats.bytes == 5
    assert rdr.stats.last_group == 2


async def test_ingest_never_raises_into_the_parse_path():
    """A malformed message counts an error rather than propagating."""
    agent = AgentSession(_StubSession())
    rdr = await agent.reader(_spec())
    rdr.on_object(object(), "not-a-size", None, 1, 0)
    assert rdr.stats.errors == 1
    assert rdr.buffered == 0


# -- age is measured, never assumed ----------------------------------

def test_age_is_none_without_a_timestamp():
    obj = Obj(track="t", group=1, object_id=0, subgroup=0,
              payload=b"x", size=1, recv_us=5_000)
    assert obj.age_ms is None


def test_age_comes_from_the_object_timestamp():
    obj = Obj(track="t", group=1, object_id=0, subgroup=0, payload=b"x",
              size=1, recv_us=1_500_000, sent_us=1_000_000)
    assert obj.age_ms == 500.0


def test_payload_helpers():
    obj = Obj(track="t", group=1, object_id=0, subgroup=0,
              payload=b'{"a": 1}', size=8, recv_us=0)
    assert obj.json() == {"a": 1}
    assert obj.text() == '{"a": 1}'


# -- the publication relationship ------------------------------------

async def test_priority_plan_reports_what_it_cannot_yet_enforce():
    """The relationship is expressible now; enforcement is 0.5.0.

    Readers-only here; the reader+writer span is covered where writers
    exist, in test_agent_writer.py.
    """
    agent = AgentSession(_StubSession())
    await agent.reader(_spec(priority=Priority(subscriber=8)))
    plan = agent.priority_plan()
    assert plan["enforced"] is False
    assert plan["tracks"] == [
        {"track": "live/cam-1/video", "publisher": None, "subscriber": 8}]


async def test_session_closes_its_readers():
    stub = _StubSession()
    async with AgentSession(stub) as agent:
        await agent.reader(_spec("video"))
        await agent.reader(_spec("audio"))
        assert len(stub.handlers) == 2
    assert stub.handlers == {}
