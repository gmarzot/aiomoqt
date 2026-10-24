"""Receive bounds. An object declaring more than max_object_bytes stops
its own stream and nothing else; bytes held in incomplete objects are
bounded per session; a stream on a track alias nothing binds is reaped
at the binding deadline; the announcement queues are bounded. Two
tracks on one session throughout; the oversize case also over raw QUIC
and WebTransport."""
import asyncio
import contextlib
import logging
import time
from collections import OrderedDict
from types import SimpleNamespace

import pytest

import aiomoqt.protocol as protocol_mod
from aiomoqt.client import MOQTClient
from aiomoqt.context import profile_for
from aiomoqt.messages.data import SubgroupHeader
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType, SessionCloseCode, StreamResetCode
from aiopquic.buffer import Buffer

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 22300
BOUND, OTHER = 7, 8


def _stub(max_object_bytes=1000):
    s = object.__new__(_MOQTSessionMixin)
    s.negotiated_draft = 18
    s._data_streams = {}
    s._control_chains = {}
    s._uni_peek_stash = {}
    s._stream_torn_down = {}
    s._stream_torn_down_last_sweep = time.monotonic()
    s._stream_torn_down_evict_after = 30.0
    s._stream_end_handlers = {}
    s._early_stream_ends = OrderedDict()
    s._reset_early_objects()
    s._early_objects_dropped = 0
    s._fetch_done_futures = {}
    s._subgroup_stream_by_key = {}
    s._fetch_stream_by_request = {}
    s._track_aliases = {BOUND: 1, OTHER: 3}
    s._unbound_aliases = {}
    s._unbound_escalated = set()
    s._malformed_aliases = set()
    s._timing_watch = {}
    s._group_bound = {}
    s._track_bound = {}
    s._track_default_priority = {}
    s._object_cb_errors = 0
    s._close_err = None
    s._max_object_bytes = max_object_bytes
    s._rx_held_limit = 4 * max_object_bytes
    s._loop = SimpleNamespace(call_later=lambda delay, cb: SimpleNamespace(
        cancel=lambda: None))
    s.delivered = []
    s.closed = []
    s.stopped = []

    def _route(alias):
        return lambda msg, *_: s.delivered.append((alias, msg.object_id))
    s._object_handlers = {a: _route(a) for a in (BOUND, OTHER)}
    s.on_object_received = _route(None)
    s._close_session = lambda code, reason: s.closed.append((int(code), reason))
    s.stream_stop_sending = lambda sid, code: s.stopped.append((sid, int(code)))
    return s


def _vint(*values):
    buf = Buffer(capacity=64, vi64=True)
    for v in values:
        buf.push_vint(v)
    return bytes(buf.data)


def _open(s, sid, alias, group_id=1):
    hdr = SubgroupHeader(track_alias=alias, group_id=group_id, subgroup_id=0,
                         prof=profile_for(18))
    s._on_stream_data(sid, bytes(hdr.serialize().data), False)
    return hdr


def _send(s, sid, hdr):
    s._on_stream_data(sid, bytes(hdr.next_object(payload=b"o" * 10).data),
                      False)


def test_an_oversize_object_stops_its_stream_and_nothing_else():
    s = _stub(max_object_bytes=1000)
    _open(s, 3, BOUND)
    other = _open(s, 7, OTHER)
    _send(s, 7, other)
    s._on_stream_data(3, _vint(0, 1001) + b"x" * 8, False)   # declares 1001
    assert s.stopped == [(3, StreamResetCode.EXCESSIVE_LOAD)]
    assert 3 not in s._data_streams and not s.closed
    s._on_stream_data(3, b"x" * 993, False)                  # late bytes: dropped
    _send(s, 7, other)
    _open(s, 11, BOUND, group_id=2)
    s._on_stream_data(11, _vint(0, 1000) + b"y" * 1000, False)   # at the limit
    assert s.delivered == [(OTHER, 0), (OTHER, 1), (BOUND, 0)]
    assert s.stopped == [(3, StreamResetCode.EXCESSIVE_LOAD)]
    assert s._rx_held_bytes == 0


def test_bytes_held_in_incomplete_objects_are_bounded_per_session():
    s = _stub(max_object_bytes=1000)                 # session limit 4000
    part = _vint(0, 1000) + b"x" * 990
    for i, sid in enumerate((3, 7, 11, 15)):
        _open(s, sid, (BOUND, OTHER)[i % 2], group_id=i)
        s._on_stream_data(sid, part, False)
    assert s._rx_held_bytes == 4 * len(part) and not s.closed
    s._on_stream_data(3, b"x" * 10, False)           # completes one object
    assert s.delivered == [(BOUND, 0)]
    assert s._rx_held_bytes == 3 * len(part)
    s._on_stream_data(3, b"", True)                  # FIN, nothing held
    _open(s, 19, OTHER, group_id=4)
    s._on_stream_data(19, part, False)
    assert s._rx_held_bytes == 4 * len(part) and not s.closed
    _open(s, 23, BOUND, group_id=5)
    s._on_stream_data(23, part, False)
    assert s.closed and s.closed[0][0] == SessionCloseCode.INTERNAL_ERROR


def test_a_stream_on_an_alias_nothing_binds_is_reaped():
    s = _stub()
    UNBOUND, OFFERED, HANDLED, LATE = 9, 10, 11, 12
    s._offer_alias(OFFERED)                          # a received PUBLISH
    s._object_handlers[HANDLED] = lambda *_: None
    sids = {BOUND: 3, UNBOUND: 7, OFFERED: 11, HANDLED: 15, OTHER: 19,
            LATE: 23}
    hdrs = {alias: _open(s, sid, alias) for alias, sid in sids.items()}
    s._track_aliases[LATE] = 5                       # SUBSCRIBE_OK, late
    for state in s._data_streams.values():
        state.last_activity -= s.STREAM_BIND_DEADLINE_S + 1
    _open(s, 27, 13)                                 # unbound, still young
    s._reap_streams()
    assert s.stopped == [(7, StreamResetCode.CANCELLED)]
    assert sorted(s._data_streams) == [3, 11, 15, 19, 23, 27]
    _send(s, 3, hdrs[BOUND])
    _send(s, 19, hdrs[OTHER])
    assert s.delivered[-2:] == [(BOUND, 0), (OTHER, 0)]
    assert not s.closed


# -- over a real session -----------------------------------------------

_NS = "rx/bounds"
_LIMIT = 64 * 1024
_SMALL = 40


async def _serve(session, alias, name):
    await asyncio.sleep(0.1)
    prof = session._profile
    sid = await session.open_uni_stream()
    hdr = SubgroupHeader(track_alias=alias, group_id=0, subgroup_id=0,
                         extensions_present=False, prof=prof)
    session.stream_write(sid, hdr.serialize().data)
    if name == b"big":
        buf = Buffer(capacity=32, vi64=prof.vi64)
        buf.push_vint(0)
        buf.push_vint(16 * _LIMIT)                   # declared, never sent
        session.stream_write(sid, bytes(buf.data) + b"x" * 4096)
        return
    for _ in range(_SMALL):
        session.stream_write(sid, hdr.next_object(payload=b"s" * 100).data)
        await asyncio.sleep(0.025)
    session.stream_write(sid, b"", end_stream=True)


@contextlib.asynccontextmanager
async def _loopback(port, use_quic):
    tasks = []
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)

    async def on_subscribe(session, msg):
        ok = session.subscribe_ok(request_msg=msg, content_exists=0)
        tasks.append(asyncio.ensure_future(
            _serve(session, ok.track_alias, bytes(msg.track_name))))

    server.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
    quic_server = await server.serve()
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=18,
                        max_object_bytes=_LIMIT)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            yield session
    finally:
        for t in tasks:
            t.cancel()
        quic_server.close()


async def _until(cond, timeout=5.0):
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(timeout):
            while not cond():
                await asyncio.sleep(0.02)
    return cond()


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_oversize_object_stops_one_track_the_other_keeps_delivering(
        use_quic):
    async with _loopback(_BASE_PORT + (not use_quic), use_quic) as session:
        stopped = []
        stop = session.stream_stop_sending

        def _spy(sid, code):
            stopped.append(int(code))
            return stop(sid, code)
        session.stream_stop_sending = _spy
        got = {"big": [], "small": []}
        ends = {"big": [], "small": []}
        for name in ("big", "small"):
            ok = await session.subscribe(_NS, name, wait_response=True)
            session.register_object_handler(
                ok.track_alias,
                lambda obj, *_, n=name: got[n].append(obj.object_id))
            session.register_stream_end_handler(
                ok.track_alias,
                lambda g, sg, clean, reset_code, n=name:
                ends[n].append((clean, reset_code)))
        assert await _until(lambda: ends["big"])
        assert ends["big"] == [(False, StreamResetCode.EXCESSIVE_LOAD)]
        assert StreamResetCode.EXCESSIVE_LOAD in stopped
        before = len(got["small"])
        assert before < _SMALL
        assert await _until(lambda: len(got["small"]) == _SMALL)
        assert got["big"] == []
        assert await _until(lambda: ends["small"])   # FIN follows the last object
        assert ends["small"] == [(True, 0)]
        assert session._close_err is None
        assert session._rx_held_bytes == 0


@requires_certs
@pytest.mark.asyncio
async def test_announcement_queues_are_bounded(caplog):
    async with _loopback(_BASE_PORT + 2, True) as session:
        n = session.ANNOUNCEMENT_QUEUE_MAX
        protocol_mod.logger.addHandler(caplog.handler)
        try:
            for i in range(n + 10):
                await session._handle_publish(SimpleNamespace(seq=i))
                await session._handle_namespace(SimpleNamespace(seq=i))
            full = [r for r in caplog.records
                    if r.levelno == logging.WARNING
                    and "queue full" in r.getMessage()]
            assert len(full) == 2                        # one per queue
            assert session._publish_announcements.qsize() == n
            assert session._namespace_announcements.qsize() == n
            assert (await session.await_publish(timeout=1)).seq == 10
            assert (await session.await_namespace(timeout=1)).seq == 10
            # Read from, then overrun again: a new run warns once more.
            for i in range(n + 10, n + 13):
                await session._handle_publish(SimpleNamespace(seq=i))
            full = [r for r in caplog.records
                    if "PUBLISH queue full" in r.getMessage()]
            assert len(full) == 2
        finally:
            protocol_mod.logger.removeHandler(caplog.handler)
