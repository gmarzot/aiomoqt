"""SubscribedTrack.subscribe forwards the full SUBSCRIBE parameter set.

Start Location, subscriber priority and group order were accepted by
session.subscribe but dropped by the track wrapper, so an absolute-start
subscription silently became a live one. These assert the wrapper passes
what it is given.
"""
import pytest

from aiomoqt.track import SubscribedTrack
from aiomoqt.types import FilterType, GroupOrder


class _RecordingSession:
    """Captures the kwargs SubscribedTrack hands to session.subscribe."""

    def __init__(self):
        self.calls = []
        self.libquicr_compat = False
        self.handlers = {}

    async def subscribe(self, **kwargs):
        self.calls.append(kwargs)
        return _SubscribeOk()

    def register_object_handler(self, alias, cb):
        self.handlers[alias] = cb

    def _watch_done(self, *a, **kw):
        pass


class _SubscribeOk:
    track_alias = 7
    request_id = 1


@pytest.fixture
def track():
    session = _RecordingSession()
    t = SubscribedTrack(session, "ns", "trackname")
    t._watch_done = lambda *a, **kw: None
    return t


async def test_absolute_start_is_forwarded(track):
    await track.subscribe(filter_type=FilterType.ABSOLUTE_START,
                          start_group=120, start_object=3)
    sent = track.session.calls[0]
    assert sent["filter_type"] is FilterType.ABSOLUTE_START
    assert (sent["start_group"], sent["start_object"]) == (120, 3)


async def test_absolute_range_forwards_end_group(track):
    await track.subscribe(filter_type=FilterType.ABSOLUTE_RANGE,
                          start_group=100, end_group=200)
    sent = track.session.calls[0]
    assert (sent["start_group"], sent["end_group"]) == (100, 200)


async def test_priority_and_group_order_are_forwarded(track):
    await track.subscribe(priority=8, group_order=GroupOrder.DESCENDING)
    sent = track.session.calls[0]
    assert sent["priority"] == 8
    assert sent["group_order"] is GroupOrder.DESCENDING


async def test_defaults_stay_unspecified(track):
    """None means omit: the relay applies the protocol default."""
    await track.subscribe()
    sent = track.session.calls[0]
    assert sent["priority"] is None
    assert sent["group_order"] is None
    assert sent["filter_type"] is FilterType.LATEST_OBJECT
    assert (sent["start_group"], sent["start_object"],
            sent["end_group"]) == (0, 0, 0)
