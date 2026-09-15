"""§2.4.2 malformed tracks: an object at or past a known end of group /
end of track makes the subscriber reset the stream with
MALFORMED_TRACK, cancel its subscription, refuse the track's later
streams, and keep the session up. Stream-end handlers see the reset
code (a relay terminates downstream from it)."""
import asyncio
import time

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.messages.data import SubgroupHeader
from aiomoqt.protocol import _MOQTSessionMixin, _DataStreamState, QuicErrorCode
from aiomoqt.types import MOQTMessageType, ObjectStatus, StreamResetCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_PORT = 14790
EOG, EOT, NORMAL = (ObjectStatus.END_OF_GROUP, ObjectStatus.END_OF_TRACK,
                    ObjectStatus.NORMAL)


def _stub():
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
    s._group_bound = {}
    s._track_bound = {}
    s._malformed_aliases = set()
    return s


def test_end_of_group_and_end_of_track_bound_later_objects():
    s = _stub()
    s._note_object_bound(7, 3, 10, EOG)
    assert s._object_out_of_bounds(7, 3, 9, NORMAL) is None
    assert s._object_out_of_bounds(7, 3, 10, EOG) is None       # same terminal again
    assert s._object_out_of_bounds(7, 3, 10, NORMAL)
    assert s._object_out_of_bounds(7, 3, 11, NORMAL)
    assert s._object_out_of_bounds(7, 4, 11, NORMAL) is None    # other group
    s._note_object_bound(7, 5, 0, EOT)
    assert s._object_out_of_bounds(7, 4, 99, NORMAL) is None
    assert s._object_out_of_bounds(7, 5, 0, EOT) is None
    assert s._object_out_of_bounds(7, 5, 0, NORMAL)
    assert s._object_out_of_bounds(7, 6, 0, NORMAL)
    assert s._object_out_of_bounds(8, 6, 0, NORMAL) is None     # other track


def test_fin_on_end_of_group_bit_stream_bounds_the_group():
    s = _stub()
    state = _DataStreamState(chain=None)
    state.key = ('subgroup', (7, 3, 0))
    state.object_id = 4
    state.parser = SubgroupHeader(track_alias=7, group_id=3, subgroup_id=0,
                                  end_of_group=True)
    s._data_streams[11] = state
    s._subgroup_stream_by_key[(7, 3, 0)] = 11
    s._cleanup_stream(11, QuicErrorCode.APPLICATION_ERROR, reset_code=1)
    assert s._group_bound == {}                                 # reset: nothing inferable
    s._data_streams[11] = state
    s._cleanup_stream(11)
    assert s._object_out_of_bounds(7, 3, 4, NORMAL) is None
    assert s._object_out_of_bounds(7, 3, 5, NORMAL)


def test_cancelling_a_request_stream_frees_the_track_bounds():
    s = _stub()
    s._bidi_stream_requests = {40: 5}
    s._bidi_streams = {5: 40}
    s._subscriptions = {5: ["sub"]}
    s._pending_requests = {}
    s._request_cancel_handlers = {}
    s._track_aliases = {7: 5}
    s._note_object_bound(7, 0, 3, EOG)
    s._malformed_aliases.add(7)
    s._on_request_stream_terminated(40)
    assert s._group_bound == {} and s._malformed_aliases == set()


def test_group_bounds_per_track_are_capped():
    s = _stub()
    for g in range(s.GROUP_BOUNDS_PER_TRACK + 10):
        s._note_object_bound(1, g, 0, EOG)
    assert len(s._group_bound[1]) == s.GROUP_BOUNDS_PER_TRACK
    assert 0 not in s._group_bound[1] and 9 not in s._group_bound[1]
    s._forget_track_bounds(1)
    assert s._group_bound == {}


def _make_server(port, cancelled):
    server = MOQTServer(
        host="localhost", port=port, certificate=CERT, private_key=KEY,
        path="/", use_quic=True, supported_drafts=18,
    )

    async def _on_subscribe(session, msg):
        ok = session.subscribe_ok(request_msg=msg, content_exists=0)
        session.register_request_cancel_handler(
            msg.request_id, lambda rid: cancelled.append(rid))
        await asyncio.sleep(0.05)
        # Subgroup 0: objects 0, 1, END_OF_GROUP at 2, FIN.
        sid = await session.open_uni_stream()
        hdr = SubgroupHeader(track_alias=ok.track_alias, group_id=0,
                             subgroup_id=0, prof=session._profile)
        session.stream_write(sid, hdr.serialize().data)
        session.stream_write(sid, hdr.next_object(payload=b"a").data)
        session.stream_write(sid, hdr.next_object(payload=b"b").data)
        session.stream_write(sid, hdr.end_group().data, end_stream=True)
        await asyncio.sleep(0.05)
        # Subgroup 1 of the same group claims object 5: malformed.
        sid = await session.open_uni_stream()
        hdr = SubgroupHeader(track_alias=ok.track_alias, group_id=0,
                             subgroup_id=1, prof=session._profile)
        session.stream_write(sid, hdr.serialize().data)
        session.stream_write(sid, hdr.next_object(payload=b"z", object_id=5).data)

    server.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    return server


@requires_certs
@pytest.mark.asyncio
async def test_object_past_end_of_group_resets_the_stream_and_cancels_the_track():
    cancelled = []
    server = await _make_server(_PORT, cancelled).serve()
    ended = []
    try:
        client = MOQTClient("localhost", _PORT, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()
            got = []
            session.on_object_received = lambda msg, *a: got.append(msg.object_id)
            ok = await session.subscribe("mal/ns", "t", wait_response=True)
            session.register_stream_end_handler(
                ok.track_alias,
                lambda g, sg, clean, reset_code: ended.append((sg, clean, reset_code)))
            for _ in range(150):
                if len(ended) == 2:
                    break
                await asyncio.sleep(0.02)
            assert ended == [(0, True, 0),
                             (1, False, StreamResetCode.MALFORMED_TRACK)]
            assert got == [0, 1, 2]                   # the offending object never delivered
            await asyncio.sleep(0.1)
            # Cancelling the subscription releases the track's state; a
            # fresh subscribe starts clean (aliases are per track, not
            # per subscription).
            assert ok.request_id not in session._subscriptions
            assert not [k for k in session._subgroup_stream_by_key
                        if k[0] == ok.track_alias]
            assert session._close_err is None         # session survives
    finally:
        server.close()
    assert cancelled                                  # our subscription was cancelled
