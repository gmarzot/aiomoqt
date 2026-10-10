"""AgentSession and Reader — bounded reads over a stubbed session.

No relay and no event loop beyond asyncio: the point is the interface
contract, so the session is a stub that records what it was asked for
and objects are injected directly into the reader's ingest callback.
"""
import asyncio
import json

import pytest

from aiomoqt.agent import (
    DecodeError, FetchSpec, Priority, StartAt, SubscribeSpec, TrackRef,
    Unsupported,
)
from aiomoqt.agent.errors import AgentError
from aiomoqt.agent.reader import Obj, ReadTimeout
from aiomoqt.agent.session import AgentSession, start_at_to_wire
from aiomoqt.types import (
    FilterType, GroupOrder, MOQTRequestError, ObjectStatus,
)


class _Ok:
    def __init__(self, alias=None, request_id=0):
        self.track_alias = alias
        self.request_id = request_id


class _StubSession:
    """Records requests. Like the session, binds a subscription's object
    handler to its alias and keeps its PUBLISH_DONE handler by request
    id; fetches finish when the test sets `fetch_done[rid]`."""

    def __init__(self, alias=1, grace=None):
        self.calls = []
        self.fetch_calls = []
        self.handlers = {}
        self.done = {}
        self.stream_end = {}
        self.fetch_handlers = {}
        self.fetch_done = {}
        self.refuse_fetch = False
        self._alias = alias
        self._rid = 0
        if grace is not None:
            self.PUBLISH_DONE_GRACE_S = grace

    def _next_rid(self):
        rid, self._rid = self._rid, self._rid + 2
        return rid

    async def subscribe(self, **kwargs):
        self.calls.append(kwargs)
        alias, rid = self._alias, self._next_rid()
        self._alias += 1
        if kwargs.get("on_object") is not None:
            self.handlers[alias] = kwargs["on_object"]
        if kwargs.get("on_done") is not None:
            self.done[rid] = kwargs["on_done"]
        return _Ok(alias, rid)

    def register_object_handler(self, alias, cb):
        self.handlers[alias] = cb

    def unregister_object_handler(self, alias):
        self.handlers.pop(alias, None)

    def register_stream_end_handler(self, alias, cb):
        self.stream_end[alias] = cb

    def unregister_stream_end_handler(self, alias):
        self.stream_end.pop(alias, None)

    async def fetch(self, **kwargs):
        self.fetch_calls.append(kwargs)
        if self.refuse_fetch:
            raise MOQTRequestError(error_code=0x3, reason="no")
        rid = self._next_rid()
        self.fetch_handlers[rid] = kwargs["on_object"]
        self.fetch_done[rid] = asyncio.get_running_loop().create_future()
        return _Ok(request_id=rid)

    async def await_fetch_done(self, rid, timeout=10.0):
        try:
            return await asyncio.wait_for(self.fetch_done[rid], timeout)
        except asyncio.TimeoutError:
            return False

    def unregister_fetch_object_handler(self, rid):
        self.fetch_handlers.pop(rid, None)


class _Msg:
    def __init__(self, object_id, payload=b"x", extensions=None, status=0):
        self.object_id = object_id
        self.payload = payload
        self.extensions = extensions
        self.status = status


class _Done:
    def __init__(self, stream_count, status_code=0x2, reason="done"):
        self.stream_count = stream_count
        self.status_code = status_code
        self.reason = reason


class _FetchObj:
    def __init__(self, group_id, object_id, payload=b"f", subgroup_id=0):
        self.group_id = group_id
        self.object_id = object_id
        self.subgroup_id = subgroup_id
        self.payload = payload
        self.extensions = None
        self.status = 0


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
    assert stub.stream_end == {}


# -- decode ------------------------------------------------------------

@pytest.mark.parametrize("decode,expected", (
    ("bytes", b'{"a": "\xc3\xa9"}'),
    ("text", '{"a": "é"}'),
    ("json", {"a": "é"}),
), ids=["bytes", "text", "json"])
async def test_decode_surfaces_value(decode, expected):
    agent = AgentSession(_StubSession())
    rdr = await agent.reader(_spec(decode=decode))
    payload = json.dumps({"a": "é"}, ensure_ascii=False).encode()
    rdr.on_object(_Msg(0, payload), len(payload), 1, 1, 0)
    obj, = rdr.drain()
    assert obj.value == expected
    assert obj.payload == payload


async def test_undecodable_payload_raises_on_access():
    agent = AgentSession(_StubSession())
    text = await agent.reader(_spec("text", decode="text"))
    data = await agent.reader(_spec("data", decode="json"))
    for rdr in (text, data):
        rdr.on_object(_Msg(0, b"\xff{"), 2, 1, 1, 0)
        obj, = rdr.drain()
        assert obj.payload == b"\xff{"
        with pytest.raises(DecodeError) as err:
            obj.value
        assert isinstance(err.value, AgentError)
    assert text.stats.errors == data.stats.errors == 0


async def test_fetch_objects_decode_too():
    stub = _StubSession()
    rdr = await AgentSession(stub).fetch(FetchSpec(
        track=TrackRef("a", "t"), start_at=StartAt.range(0, 1),
        decode="json"))
    rdr.on_fetch_object(_FetchObj(0, 0, b"[1, 2]"), 6, 1, rdr.request_id)
    assert rdr.drain()[0].value == [1, 2]


# -- completion ----------------------------------------------------------

async def test_status_objects_never_reach_reads():
    agent = AgentSession(_StubSession())
    rdr = await agent.reader(_spec())
    rdr.on_object(_Msg(0), 1, 1, 1, 0)
    rdr.on_object(_Msg(1, b"", status=ObjectStatus.END_OF_GROUP), 0, 2, 1, 0)
    assert [o.object_id for o in rdr.drain()] == [0]
    assert rdr.stats.received == 1


async def test_done_waits_for_stream_count():
    """§10.11: objects can follow PUBLISH_DONE, so it alone is not the end."""
    stub = _StubSession()
    rdr = await AgentSession(stub).reader(_spec())
    stub.done[rdr.request_id](_Done(stream_count=2))
    assert not rdr.done
    stub.stream_end[rdr.alias](0, 0, clean=True, reset_code=0)
    assert not rdr.done
    stub.stream_end[rdr.alias](1, 0, clean=False, reset_code=0)
    assert rdr.done and rdr.ended == "complete"
    assert rdr.publish_done == {"status_code": 0x2, "reason": "done",
                                "stream_count": 2}


async def test_streams_ended_before_publish_done_count():
    stub = _StubSession()
    rdr = await AgentSession(stub).reader(_spec())
    stub.stream_end[rdr.alias](0, 0, clean=True, reset_code=0)
    assert not rdr.done
    stub.done[rdr.request_id](_Done(stream_count=1))
    assert rdr.done and rdr.ended == "complete"


async def test_zero_streams_done_at_publish_done():
    stub = _StubSession()
    rdr = await AgentSession(stub).reader(_spec())
    stub.done[rdr.request_id](_Done(stream_count=0))
    assert rdr.done and rdr.ended == "complete"
    assert await rdr.read(5, timeout=5.0) == []


async def test_done_after_grace_when_streams_are_missing():
    stub = _StubSession(grace=0.05)
    rdr = await AgentSession(stub).reader(_spec())
    _feed(rdr, group=1, count=2)
    stub.done[rdr.request_id](_Done(stream_count=3))
    stub.stream_end[rdr.alias](1, 0, clean=True, reset_code=0)
    assert not rdr.done
    got = await rdr.read(10, timeout=5.0)
    assert len(got) == 2
    assert rdr.done and rdr.ended == "grace"


async def test_publish_done_reaches_only_its_reader():
    stub = _StubSession()
    agent = AgentSession(stub)
    a = await agent.reader(_spec("video"))
    b = await agent.reader(_spec("audio"))
    assert a.request_id != b.request_id and a.alias != b.alias
    stub.stream_end[b.alias](0, 0, clean=True, reset_code=0)
    stub.done[a.request_id](_Done(stream_count=1))
    assert not a.done
    stub.stream_end[a.alias](0, 0, clean=True, reset_code=0)
    assert a.done and not b.done
    assert b.publish_done is None and b.ended is None


async def test_a_closed_reader_reports_closed():
    stub = _StubSession(grace=0.01)
    agent = AgentSession(stub)
    rdr = await agent.reader(_spec())
    agent.close_reader(rdr.name)
    stub.done[rdr.request_id](_Done(stream_count=0))
    assert rdr.ended == "closed"


# -- fetch -----------------------------------------------------------------

def _fetch_spec(**kw):
    return FetchSpec(track=TrackRef("audit/s7", "decisions"),
                     start_at=StartAt(mode="range", group=100, object=2,
                                      end_group=200), **kw)


async def test_fetch_sends_the_range():
    stub = _StubSession()
    await AgentSession(stub).fetch(_fetch_spec(
        priority=Priority(subscriber=8, group_order="descending")))
    sent, = stub.fetch_calls
    assert (sent["namespace"], sent["track_name"]) == ("audit/s7", "decisions")
    assert (sent["start_group"], sent["start_object"]) == (100, 2)
    # End object 0: the whole end group.
    assert (sent["end_group"], sent["end_object"]) == (200, 0)
    assert sent["subscriber_priority"] == 8
    assert sent["group_order"] is GroupOrder.DESCENDING
    assert sent["wait_response"] is True and callable(sent["on_object"])


async def test_fetch_reader_done_on_stream_end():
    stub = _StubSession()
    rdr = await AgentSession(stub).fetch(_fetch_spec())
    deliver = stub.fetch_handlers[rdr.request_id]
    for obj in range(3):
        deliver(_FetchObj(100, obj + 2, b"o%d" % obj), 2, 1, rdr.request_id)
    assert not rdr.done
    stub.fetch_done[rdr.request_id].set_result(True)
    got = await rdr.read(10, timeout=5.0)
    assert [(o.group, o.object_id, o.payload) for o in got] == [
        (100, 2, b"o0"), (100, 3, b"o1"), (100, 4, b"o2")]
    assert rdr.done and rdr.ended == "complete"
    assert stub.fetch_handlers == {}


async def test_a_reset_fetch_ends_failed():
    stub = _StubSession()
    rdr = await AgentSession(stub).fetch(_fetch_spec())
    stub.fetch_done[rdr.request_id].set_result(False)
    assert await rdr.read(1, timeout=5.0) == []
    assert rdr.ended == "failed"


async def test_fetch_ring_is_unbounded():
    stub = _StubSession()
    rdr = await AgentSession(stub).fetch(_fetch_spec())
    for obj in range(1000):
        rdr.on_fetch_object(_FetchObj(100, obj), 1, 1, rdr.request_id)
    assert rdr.buffered == 1000 and rdr.stats.dropped == 0


async def test_two_fetches_get_their_own_readers():
    stub = _StubSession()
    agent = AgentSession(stub)
    a, b = await asyncio.gather(agent.fetch(_fetch_spec()),
                                agent.fetch(_fetch_spec()))
    assert a is not b and a.request_id != b.request_id
    stub.fetch_handlers[a.request_id](_FetchObj(100, 2), 1, 1, a.request_id)
    assert (a.buffered, b.buffered) == (1, 0)


async def test_a_refused_fetch_raises_agent_error():
    stub = _StubSession()
    stub.refuse_fetch = True
    with pytest.raises(AgentError, match="fetch audit/s7/decisions failed"):
        await AgentSession(stub).fetch(_fetch_spec())


async def test_closing_the_session_ends_its_fetches():
    stub = _StubSession()
    agent = AgentSession(stub)
    rdr = await agent.fetch(_fetch_spec())
    await agent.close()
    assert rdr.ended == "closed"
    assert stub.fetch_handlers == {}


# -- inputs the runtime would otherwise ignore -------------------------

@pytest.mark.parametrize("spec", (
    SubscribeSpec(
        track=TrackRef("live/cam-1", "video", relay="moqt://r.example:4433")),
    _spec(priority=Priority(delivery_timeout_ms=500)),
    _spec(priority=Priority(publisher=3)),
    _spec(on_full="block"),
    _spec(on_full="error"),
), ids=["relay", "delivery-timeout", "publisher-priority", "block", "error"])
async def test_reader_refuses_what_it_would_ignore(spec):
    stub = _StubSession()
    with pytest.raises(Unsupported) as err:
        await AgentSession(stub).reader(spec)
    assert isinstance(err.value, NotImplementedError)
    assert stub.calls == []


@pytest.mark.parametrize("spec", (
    FetchSpec(track=TrackRef("a", "t", relay="https://r.example"),
              start_at=StartAt.range(0, 1)),
    FetchSpec(track=TrackRef("a", "t"), start_at=StartAt.range(0, 1),
              priority=Priority(delivery_timeout_ms=1)),
    FetchSpec(track=TrackRef("a", "t"), start_at=StartAt.range(0, 1),
              priority=Priority(publisher=0)),
), ids=["relay", "delivery-timeout", "publisher-priority"])
async def test_fetch_refuses_what_it_would_ignore(spec):
    stub = _StubSession()
    with pytest.raises(Unsupported):
        await AgentSession(stub).fetch(spec)
    assert stub.fetch_calls == []


async def test_a_zero_delivery_timeout_is_unset():
    agent = AgentSession(_StubSession())
    await agent.reader(_spec(priority=Priority(delivery_timeout_ms=0)))
