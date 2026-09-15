"""Binding-deadline reaper: a uni data stream whose header never parses
is abandoned with STOP_SENDING after STREAM_BIND_DEADLINE_S; bound
streams and young streams are left alone; the reaper re-arms only while
data streams remain."""
import asyncio
import time

import pytest

from aiomoqt.context import profile_for
from aiomoqt.protocol import _MOQTSessionMixin, _DataStreamState
from aiomoqt.types import StreamResetCode


def _stub():
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = 18
    s._profile = profile_for(18)
    s._loop = asyncio.get_running_loop()
    s._close_err = None
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
    s.stopped = []
    s.stream_stop_sending = lambda sid, code: s.stopped.append((sid, int(code)))
    return s


def _state(parser, age_s):
    return _DataStreamState(chain=None, parser=parser,
                            last_activity=time.monotonic() - age_s)


@pytest.mark.asyncio
async def test_only_old_header_less_streams_are_reaped():
    s = _stub()
    s._data_streams = {3: _state(None, 10.0),      # orphan: reaped
                       7: _state(object(), 60.0),  # bound: kept
                       11: _state(None, 1.0)}      # young: kept
    s._reap_streams()
    assert s.stopped == [(3, int(StreamResetCode.DELIVERY_TIMEOUT))]
    assert sorted(s._data_streams) == [7, 11]
    assert s._reaper_handle is not None                 # streams remain: re-armed
    s._reaper_handle.cancel()


@pytest.mark.asyncio
async def test_reaper_stops_when_no_streams_remain_and_after_close():
    s = _stub()
    s._data_streams = {3: _state(None, 10.0)}
    s._reap_streams()
    assert s._data_streams == {} and s._reaper_handle is None
    s._data_streams = {5: _state(None, 10.0)}
    s._close_err = (0, "closed")
    s._reap_streams()
    assert s.stopped == [(3, int(StreamResetCode.DELIVERY_TIMEOUT))]


@pytest.mark.asyncio
async def test_first_data_stream_arms_the_reaper():
    s = _stub()
    s._on_stream_data(9, b"\x10", False)               # a header fragment
    assert 9 in s._data_streams and s._data_streams[9].last_activity > 0
    assert s._reaper_handle is not None
    s._reaper_handle.cancel()
