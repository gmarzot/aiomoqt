"""aiomoqt-relay memory bounds: cache and send lanes charge every retained
object its payload, properties and record overhead; the cache has a
relay-wide hard ceiling; a lane counts the bytes its subscriber's streams
still hold in the transport."""
import asyncio
import contextlib
import gc
import os
import signal
import subprocess
import sys
import tracemalloc
from types import SimpleNamespace

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.delivery import (OBJECT_RECORD_BYTES, ObjectHistory,
                              retained_object_bytes)
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import ObjectStatus

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_PORT = 27100
_N = 100_000


@pytest.fixture(autouse=True)
def fresh_budget(monkeypatch):
    monkeypatch.setattr(relay, "_budget", relay._CacheBudget())


def _track(name=b"t"):
    t = relay._RelayedTrack(((b"membounds",), name))
    t.upstream = object()
    return t


def _subscribe(t):
    """A live subscriber, attached without the forward task."""
    d = relay._Downstream(SimpleNamespace(), 1, 7)
    t.downstream.append(d)
    return d


def _accounted(*tracks):
    for t in tracks:
        assert t._cache_bytes == sum(retained_object_bytes(c[3], c[4])
                                     for c in t._cache)
    assert relay._budget.bytes == sum(t._cache_bytes for t in tracks)


def _traced(fn):
    """Bytes still allocated after `fn` runs."""
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        fn()
        return tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()


async def _stop(lane):
    lane.stop()
    await asyncio.gather(lane.task, return_exceptions=True)


# Payloads are distinct objects, as off the wire; the Object IDs are new
# ints. The charge has to cover what each record holds beyond its payload.
def _payload(oid):
    return b"ab" + bytes([oid & 0xFF])


def test_the_record_charge_covers_a_cached_object(monkeypatch):
    monkeypatch.setattr(relay, "CACHE_BYTES", 1 << 40)
    monkeypatch.setattr(relay, "CACHE_BUDGET_BYTES", 1 << 40)
    t = _track()
    per = _traced(lambda: [t._remember(0, 0, oid, _payload(oid), None, 128,
                                       ObjectStatus.NORMAL)
                           for oid in range(_N)]) / _N - 3
    assert 0.75 * OBJECT_RECORD_BYTES < per <= OBJECT_RECORD_BYTES, per
    t._drop_cache()


async def test_the_record_charge_covers_a_queued_lane_object():
    t = _track()
    lane = relay._SendLane(t, _subscribe(t))
    oids = list(range(1 << 20, (1 << 20) + _N))
    pays = [_payload(oid) for oid in oids]
    per = _traced(lambda: [lane.offer(0, False, 0, oid, p, None, 128,
                                      ObjectStatus.NORMAL, None)
                           for oid, p in zip(oids, pays)]) / _N
    assert per <= OBJECT_RECORD_BYTES, per
    await _stop(lane)


def test_the_record_charge_covers_a_history_object():
    h = ObjectHistory(1 << 40)
    per = _traced(lambda: [h.add(0, oid, _payload(oid))
                           for oid in range(_N)]) / _N - 3
    assert per <= OBJECT_RECORD_BYTES, per


def test_empty_objects_are_charged_to_the_cache(monkeypatch):
    budget = 4 << 20
    monkeypatch.setattr(relay, "CACHE_BUDGET_BYTES", budget)
    t = _track()

    def write():
        for oid in range(_N):
            t._remember(0, 0, oid, b"", None, 128, ObjectStatus.NORMAL)
            assert relay._budget.bytes <= budget
    held = _traced(write)
    assert relay._budget.evictions > 0
    assert held <= budget, held
    _accounted(t)
    t._drop_cache()


def test_empty_objects_are_charged_to_a_subscribed_track(monkeypatch):
    # The live group is held to the per-track bound.
    monkeypatch.setattr(relay, "CACHE_BYTES", 4 << 20)
    t = _track()
    _subscribe(t)
    held = _traced(lambda: [t._remember(0, 0, oid, b"", None, 128,
                                        ObjectStatus.NORMAL)
                            for oid in range(_N)])
    assert 0 < t._cache_bytes <= relay.CACHE_BYTES
    assert len(t._cache) < _N
    assert held <= relay.CACHE_BYTES, held
    _accounted(t)
    t._drop_cache()


async def test_empty_objects_are_charged_to_a_send_lane():
    t = _track()
    lane = relay._SendLane(t, _subscribe(t))
    # No yield: a subscriber whose lane never drains.
    for oid in range(_N):
        lane.offer(0, True, 0, oid, b"", None, 128, ObjectStatus.NORMAL,
                   None)
        assert lane.bytes <= relay.LANE_BYTES
    assert lane.shed > 0 and lane.sheds == 1
    assert len(lane.items) <= relay.LANE_BYTES // OBJECT_RECORD_BYTES + 1
    await _stop(lane)


async def test_properties_are_charged():
    exts = {0x0B: b"p" * 16_384, 0x10: 7}
    cost = retained_object_bytes(b"x", exts)
    assert cost >= 1 + 16_384 + 8 + OBJECT_RECORD_BYTES
    t = _track()
    t._remember(0, 0, 0, b"x", exts, 128, ObjectStatus.NORMAL)
    assert t._cache_bytes == relay._budget.bytes == cost
    lane = relay._SendLane(t, _subscribe(t))
    lane.offer(0, True, 0, 0, b"x", exts, 128, ObjectStatus.NORMAL, None)
    assert lane.bytes == cost
    await _stop(lane)
    t._drop_cache()


def test_the_hard_ceiling_holds_across_live_groups(monkeypatch):
    budget, per_track = 1 << 20, 2 << 20
    monkeypatch.setattr(relay, "CACHE_BUDGET_BYTES", budget)
    monkeypatch.setattr(relay, "CACHE_BYTES", per_track)
    tracks = [_track(b"t%d" % i) for i in range(4)]
    for t in tracks:
        _subscribe(t)
    obj = b"x" * 10_000
    # Each track's one live group grows to near its own bound.
    for oid in range(180):
        for t in tracks:
            t._remember(0, 0, oid, obj, None, 128, ObjectStatus.NORMAL)
            assert relay._budget.bytes <= budget + per_track
    assert relay._budget.truncations >= 1
    truncated = [t for t in tracks if t._uncached == 0]
    assert truncated and all(not t._cache for t in truncated)
    _accounted(*tracks)
    # The rest of a dropped group stays uncached; the next group is cached.
    t = truncated[0]
    t._remember(0, 0, 999, obj, None, 128, ObjectStatus.NORMAL)
    assert not t._cache and t.largest == (0, 999)
    t._remember(1, 0, 0, obj, None, 128, ObjectStatus.NORMAL)
    assert [c[0] for c in t._cache] == [1] and t._uncached is None
    _accounted(*tracks)
    for t in tracks:
        t._drop_cache()


# -- live, over both transports --------------------------------------------

@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_two_tracks_on_one_session_deliver_under_the_bounds(use_quic):
    for table in (relay._announced, relay._announce_requests, relay._tracks):
        table.clear()
    ns, sizes = "membounds/live", {"video": 20_000, "audio": 200}
    got = {n: 0 for n in sizes}
    port = _PORT + 2 + (not use_quic)
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=18)
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=18)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s
            pub = await connect()
            for name, size in sizes.items():
                PublishedTrack(pub, namespace=ns, trackname=name,
                               object_size=size, group_size=10,
                               rate=100).attach()
            await pub.publish_namespace(namespace=ns, wait_response=True)
            viewer = await connect()

            def count(name):
                def on_object(msg, *_):
                    got[name] += 1
                return on_object
            for name in sizes:
                await SubscribedTrack(viewer, ns, name,
                                      on_object=count(name)).subscribe(
                                          timeout=5)
            await asyncio.sleep(1.5)
            tracks = [relay._tracks[(relay._ns_tuple(ns), n.encode())]
                      for n in sizes]
            lanes = [d.lane for t in tracks for d in t.downstream]
            _accounted(*tracks)
    finally:
        handle.close()
        for table in (relay._announced, relay._announce_requests,
                      relay._tracks):
            table.clear()
    assert all(n >= 100 for n in got.values()), got
    assert len(lanes) == 2 and all(lane.sheds == 0 for lane in lanes)

_STALLED_SUB = r"""
import asyncio, sys
from aiomoqt.client import MOQTClient
from aiomoqt.track import SubscribedTrack

async def main(port, ns, name):
    client = MOQTClient("localhost", port, path="/", use_quic=True,
                        verify_tls=False, supported_drafts=18)
    async with client.connect() as s:
        await s.client_session_init()
        await SubscribedTrack(s, ns, name).subscribe(timeout=5)
        print("subscribed", flush=True)
        await asyncio.sleep(3600)

asyncio.run(main(int(sys.argv[1]), sys.argv[2], sys.argv[3]))
"""


def _ring_backlog(session):
    quic = session._quic
    return sum(quic._transport.tx_data_ring_used(sc)
               for sc in list(quic._stream_ctxs.values()))


@requires_certs
@pytest.mark.skipif(sys.platform == "win32", reason="SIGSTOP")
async def test_a_stalled_raw_quic_viewer_is_bounded(monkeypatch):
    monkeypatch.setattr(relay, "LANE_BYTES", 1 << 20)
    for table in (relay._announced, relay._announce_requests, relay._tracks):
        table.clear()
    ns, name = "membounds/stall", "video"
    server = relay._build_server("localhost", _PORT, CERT, KEY,
                                 use_quic=True, draft=18)
    handle = await server.serve()
    proc = None
    try:
        client = MOQTClient("localhost", _PORT, path="/", use_quic=True,
                            verify_tls=False, supported_drafts=18)
        async with client.connect() as pub:
            await pub.client_session_init()
            PublishedTrack(pub, namespace=ns, trackname=name,
                           object_size=20_000, group_size=10,
                           rate=200).attach()
            await pub.publish_namespace(namespace=ns, wait_response=True)
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", _STALLED_SUB, str(_PORT), ns, name,
                stdout=subprocess.PIPE, cwd=os.getcwd())
            line = await asyncio.wait_for(proc.stdout.readline(), 10)
            assert line.strip() == b"subscribed", line
            track = relay._tracks[(relay._ns_tuple(ns), name.encode())]
            (d,) = track.downstream
            os.kill(proc.pid, signal.SIGSTOP)
            peak = 0
            for _ in range(40):
                await asyncio.sleep(0.1)
                peak = max(peak, _ring_backlog(d.session))
            print("peak backlog", peak, "lane", d.lane and d.lane.bytes,
                  "sheds", d.lane and d.lane.sheds)
            assert peak <= relay.LANE_BYTES + 2 * 200_000, peak
            assert d.lane.sheds >= 1
    finally:
        if proc is not None:
            proc.kill()
            await proc.wait()
        handle.close()
        for table in (relay._announced, relay._announce_requests,
                      relay._tracks):
            table.clear()
