"""A FETCH's request stream is closed by both ends once its data stream
ends (§5.2), so finished FETCHes stop holding bidi stream credit: on the
subscriber, on aiomoqt-relay serving it, and on the publisher serving
the relay's upstream FETCH. The requester also sends STOP_SENDING, but
only once the reply is in. Two tracks on one session, every draft with
request streams, raw QUIC and WebTransport."""
import asyncio
import contextlib
import logging

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack

from aiomoqt.tests._certs import CERT, KEY, requires_certs


_PORT = 16870
_NS = "fetchclose/live"
_TRACKS = {"a": 40, "b": 60}
_GROUP = 3
_FETCHES = 30


class _FetchOnly(PublishedTrack):
    """History and a Largest Location, nothing sent on a subscription."""

    async def _start_generating(self, session, trigger):
        return None


def _clear():
    for table in (relay._announced, relay._announce_requests, relay._tracks,
                  relay._sessions, relay._catalogs):
        table.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()


@pytest.fixture
def relay_tables():
    _clear()
    yield
    _clear()


@contextlib.asynccontextmanager
async def _relay(port, use_quic, draft):
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s
            yield connect
    finally:
        handle.close()


def _bound(sessions):
    return {k: len(s._bidi_stream_requests) for k, s in sessions.items()}


def _relay_sessions():
    return {id(info["session"]): info["session"]
            for info in relay._sessions.values()}


async def _until(cond, what, tries=250):
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0.02)
    pytest.fail(what() if callable(what) else what)


class _Client(_MOQTSessionMixin):
    _is_client = True


class _Ops:
    """A requester session's FETCH request-stream hooks, recording the
    stream ops they make."""

    def __init__(self, fetch=True, peer_ended=False):
        s = object.__new__(_Client)
        s._bidi_streams = {4: 0}
        s._fetch_request_streams = {0} if fetch else set()
        s._fetch_steps, s._fetch_closing = {}, set()
        s._request_stream_ends = ({0: s._PEER_HALF} if peer_ended
                                  else None)
        self.ops = []
        s.stream_fin = lambda sid: self._end(s, sid, "fin", s._LOCAL_HALF)
        s.stream_stop_sending = lambda sid, code: self.ops.append(
            ("stop", sid))
        s._request_half_ended = lambda sid, half: self.ops.append(
            ("peer ended", sid))
        self.s = s

    def _end(self, s, sid, op, half):
        self.ops.append((op, sid))
        if s._request_stream_ends is None:
            s._request_stream_ends = {}
        s._request_stream_ends[sid] = s._request_stream_ends.get(sid, 0) | half


def test_stop_sending_waits_for_the_reply():
    t = _Ops()
    t.s._end_fetch_request(4)
    assert t.ops == [("fin", 0)]
    t.s._fetch_answered(4, error=False)
    assert t.ops == [("fin", 0), ("stop", 0), ("peer ended", 0)]
    assert t.s._fetch_closing == {0}


def test_a_reply_before_the_data_end_waits_for_it():
    t = _Ops()
    t.s._fetch_answered(4, error=False)
    assert t.ops == []
    t.s._end_fetch_request(4)
    assert t.ops == [("fin", 0), ("stop", 0), ("peer ended", 0)]


def test_a_request_error_closes_at_once():
    t = _Ops()
    t.s._fetch_answered(4, error=True)
    assert t.ops == [("fin", 0), ("stop", 0), ("peer ended", 0)]


def test_a_peer_half_already_ended_gets_no_stop_sending():
    t = _Ops(peer_ended=True)
    t.s._end_fetch_request(4)
    t.s._fetch_answered(4, error=False)
    assert t.ops == [("fin", 0)]


def test_only_a_fetch_request_stream_is_closed():
    # A SUBSCRIBE's fill ends a data stream under the SUBSCRIBE's id.
    t = _Ops(fetch=False)
    t.s._end_fetch_request(4)
    t.s._fetch_answered(4, error=False)
    assert t.ops == []


def test_a_peer_request_gets_no_stop_sending():
    t = _Ops()
    t.s._bidi_streams = {5: 0}
    t.s._end_fetch_request(5)
    t.s._fetch_answered(5, error=False)
    assert t.ops == [("fin", 0)]


@requires_certs
@pytest.mark.parametrize("draft", [18, 20, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_finished_fetches_release_their_request_streams(
        use_quic, draft, relay_tables, caplog):
    caplog.set_level(logging.INFO, logger="aiomoqt")
    got = {}
    port = _PORT + 2 * (draft - 18) + (not use_quic)
    async with _relay(port, use_quic, draft) as connect:
        pub = await connect()
        for name, size in _TRACKS.items():
            track = _FetchOnly(pub, namespace=_NS, trackname=name,
                               object_size=size, group_size=_GROUP,
                               fetch_history_bytes=1 << 20)
            track.prefill(1)
            track.attach()
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        rx = await connect()
        rx.on_fetch_object = lambda obj, _n, _ts, rid: got.setdefault(
            rid, []).append(len(obj.payload or b""))
        sessions = {"subscriber": rx, "publisher": pub, **_relay_sessions()}
        before = _bound(sessions)
        names = list(_TRACKS)
        for k in range(_FETCHES):
            name = names[k % len(names)]
            ok = await asyncio.wait_for(rx.fetch(
                _NS, name, start_group=0, start_object=0, end_group=0,
                end_object=_GROUP, wait_response=True), 5)
            assert await rx.await_fetch_done(ok.request_id, timeout=5)
            assert got.get(ok.request_id) == [_TRACKS[name]] * _GROUP, name
        assert len(got) == _FETCHES

        # The relay's session with the subscriber exists only from its
        # first FETCH, which is then all it carried.
        new = {k: s for k, s in _relay_sessions().items()
               if k not in sessions}
        assert len(new) == 1
        sessions.update(new)
        before.update(dict.fromkeys(new, 0))
        await _until(lambda: _bound(sessions) == before,
                     lambda: f"request streams bound {_bound(sessions)}, "
                             f"before {before}")
        assert not any(s._fetch_request_streams or s._fetch_closing
                       for s in sessions.values())
        cancelled = [r.getMessage() for r in caplog.records
                     if "cancelled by" in r.getMessage()]
        assert not cancelled, cancelled[:3]

        assert rx._close_err is None and pub._close_err is None
