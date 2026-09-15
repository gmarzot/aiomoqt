"""Stream-end handlers learn whether a subgroup stream ended with a FIN
(end-of-group inferable, §11.4.2) or a reset (nothing inferable), and
the peer's reset code."""
import time

from aiomoqt.protocol import _MOQTSessionMixin, _DataStreamState, QuicErrorCode


def _session():
    s = object.__new__(_MOQTSessionMixin)
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
    s.ended = []
    s._stream_end_handlers[5] = lambda g, sg, clean, reset_code: \
        s.ended.append((g, sg, clean, reset_code))
    return s


def _bind(s, stream_id, group, subgroup):
    state = _DataStreamState(chain=None)
    state.key = ('subgroup', (5, group, subgroup))
    s._data_streams[stream_id] = state
    s._subgroup_stream_by_key[(5, group, subgroup)] = stream_id


def test_fin_reports_clean_and_reset_reports_the_peer_code():
    s = _session()
    _bind(s, 3, 10, 0)
    _bind(s, 7, 11, 0)
    s._cleanup_stream(3)                                   # FIN
    s._cleanup_stream(7, QuicErrorCode.APPLICATION_ERROR, reset_code=2)
    assert s.ended == [(10, 0, True, 0), (11, 0, False, 2)]
    assert s._subgroup_stream_by_key == {}
