"""Normal subscriber churn must not end a publisher.

A viewer leaving is the end of that subscription, not of the track or
the session: production idles when the last subscriber goes and resumes
when the next arrives, on every draft, and a publisher-only session
stays up throughout.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from aiomoqt.context import profile_for
from aiomoqt.messages.subscribe import SubscribeDone, Unsubscribe
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.track import PublishedTrack
from aiomoqt.types import ForwardingPreference


def _session(draft=18):
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s._subscriptions = {}
    s._had_subscription = False
    s._track_aliases = {}
    s._object_handlers = {}
    s._pending_requests = {}
    s._request_cancel_handlers = {}
    s._subgroup_stream_by_key = {}
    s._data_streams = {}
    s._control_chains = {}
    s._uni_peek_stash = {}
    s._stream_torn_down = {}
    s._stream_torn_down_last_sweep = time.monotonic()
    s._stream_torn_down_evict_after = 30.0
    s._stream_end_handlers = {}
    s._fetch_done_futures = {}
    s._fetch_stream_by_request = {}
    s._group_bound = {}
    s._track_bound = {}
    s._malformed_aliases = set()
    s.closed = []
    s._close_session = lambda code=0, reason="": s.closed.append(reason)
    s.stream_stop_sending = lambda sid, code: None
    s.stream_reset = lambda sid, code: None
    return s


def _done(request_id):
    return SubscribeDone(request_id=request_id, status_code=0x2,
                         stream_count=0, reason="track ended")


@pytest.mark.asyncio
async def test_publisher_session_survives_a_publish_done():
    # A publisher never subscribed to anything, so its audience leaving
    # is not its own end of life.
    s = _session()
    await s._handle_subscribe_done(_done(4))
    assert s.closed == []


@pytest.mark.asyncio
async def test_subscriber_session_still_closes_on_its_last_subscription():
    # The clean-exit signal the bench tools wait on is unchanged.
    s = _session()
    s._had_subscription = True
    s._subscriptions = {2: ["sub"], 4: ["sub"]}
    await s._handle_subscribe_done(_done(2))
    assert s.closed == []                       # one left
    await s._handle_subscribe_done(_done(4))
    assert s.closed and "subscribe done" in s.closed[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [14, 16])
async def test_unsubscribe_reaches_the_owner_before_d18(draft):
    # d18 cancels by terminating the request stream; earlier drafts send
    # UNSUBSCRIBE. Both must reach the track.
    s = _session(draft)
    cancelled = []
    s.register_request_cancel_handler(7, cancelled.append)
    await s._handle_unsubscribe(Unsubscribe(request_id=7))
    assert cancelled == [7]


def _track(session, subgroups=2):
    t = PublishedTrack(session, "ns", trackname="t", object_size=64,
                       group_size=4, rate=0.0,
                       forwarding=ForwardingPreference.SUBGROUP)
    t._quiet = True
    t.num_subgroups = subgroups
    t.track_alias = 1
    spawned = []
    t._spawn_producers = lambda *a: spawned.append(a)
    t._pad = b"pad"
    t._generating = True
    return t, spawned


@pytest.mark.asyncio
async def test_one_subscriber_leaving_does_not_stop_the_others():
    s = _session()
    t, spawned = _track(s)
    t._subscribers = {2, 4}
    t._on_request_cancelled(2)
    assert t._generating is True and t._done is False


@pytest.mark.asyncio
async def test_the_last_subscriber_idles_the_track_and_the_next_restarts_it():
    s = _session()
    t, spawned = _track(s)
    t._subscribers = {2}
    t._on_request_cancelled(2)
    assert t._generating is False        # idle
    assert t._done is False              # but not retired
    await t._start_generating(s, "SUBSCRIBE")
    assert spawned == [(s, 1, b"pad")]   # restarted under the live alias
    assert t._generating is True


@pytest.mark.asyncio
async def test_our_own_publish_done_still_retires_the_track():
    # The guard that keeps us from publishing after our own PUBLISH_DONE
    # must survive the churn handling.
    s = _session()
    t, spawned = _track(s)
    t._subscribe_request_id = 2
    t.request_id = 2
    s.subscribe_done = lambda **kw: None
    s._send_reply = lambda rid, msg, fin=False: None
    t._send_publish_done(s)
    assert t._done is True
    await t._start_generating(s, "SUBSCRIBE")
    assert spawned == []                 # refused


_PORT = 14812


def _churn_server(port, tracks):
    from aiomoqt.server import MOQTServer
    from aiomoqt.types import MOQTMessageType
    from aiomoqt.tests._certs import CERT, KEY

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=True,
                        supported_drafts=18)

    async def _on_subscribe(session, msg):
        # One track instance across successive subscribes, as the media
        # broadcast path does.
        track = tracks.get("t")
        if track is None:
            track = PublishedTrack(
                session, "churn/ns", trackname="t", object_size=64,
                group_size=4, rate=200.0,
                forwarding=ForwardingPreference.SUBGROUP)
            track._quiet = True
            tracks["t"] = track
        await track._on_subscribe(session, msg)

    server.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    return server


async def _collect(session, want, timeout=6.0):
    got = []
    done = asyncio.Event()

    def _on_object(msg, size, ts, gid, sgid):
        got.append((gid, msg.object_id))
        if len(got) >= want:
            done.set()

    session.on_object_received = _on_object
    sub = await session.subscribe("churn/ns", "t", wait_response=True)
    try:
        await asyncio.wait_for(done.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    return sub, got


@pytest.mark.asyncio
async def test_a_returning_subscriber_gets_objects_again():
    from aiomoqt.client import MOQTClient
    from aiomoqt.tests._certs import CERT, KEY
    import os
    if not (os.path.exists(CERT) and os.path.exists(KEY)):
        pytest.skip("TLS certs not found in certs/")

    tracks = {}
    server = await _churn_server(_PORT, tracks).serve()
    try:
        client = MOQTClient("localhost", _PORT, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as session:
            await session.client_session_init()

            first_sub, first = await _collect(session, 4)
            assert len(first) >= 4, "no objects on the first subscribe"

            # The viewer leaves: d18 cancels by resetting the request
            # stream. The publisher should idle, not retire the track.
            session.unsubscribe(first_sub.request_id)
            await asyncio.sleep(0.4)
            track = tracks["t"]
            assert track._generating is False
            assert track._done is False
            assert session._close_err is None

            # A new viewer arrives on the same publisher.
            _second_sub, second = await _collect(session, 4)
            assert len(second) >= 4, "publisher stayed silent for the " \
                                     "second subscriber"
            assert track._generating is True
            assert session._close_err is None
    finally:
        server.close()
