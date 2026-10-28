"""aiomoqt-relay subgroup headers: written with the drained write, so a full
TX ring delays a header and never drops it; a header write that cannot
finish resets its stream; a lane parked on a header still sheds."""
import asyncio
import os
import signal
import subprocess
import sys

import pytest

from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import StreamResetCode

from aiomoqt.tests.test_relay_memory_bounds import _STALLED_SUB, _ring_backlog
from aiomoqt.tests.test_relay_send_lanes import (_Fake, _Sub, _clear_relay,
                                                 _obj, _relay, _settle)
from aiomoqt.tests._certs import requires_certs

_PORT = 27300
_NS = ("lanes", "live")


@pytest.fixture
def relay_tables():
    _clear_relay()
    yield
    _clear_relay()


def _headed(fake):
    """Every stream written to got its header first."""
    hdr = set()
    for e in fake.log:
        if e[0] == "hdr":
            hdr.add(e[1])
        elif e[0] == "data":
            assert e[1] in hdr, (e, fake.log)


# -- against fake sessions -------------------------------------------------

async def test_a_header_write_cut_short_resets_its_stream():
    track = relay._RelayedTrack(("ns", "t"))
    slow = _Fake(blocked=True)
    d = track.add_downstream(slow, 1, 1)
    try:
        track.on_object(_obj(0, 0), 0, 0, 0, 0)
        await _settle()
        (sid,) = [e[1] for e in slow.kinds("open")]
        assert d.lane.busy and d.lane.opening
        track._end(d, 0x2, None, "ended")
        await _settle()
        assert ("reset", sid, int(StreamResetCode.CANCELLED)) in slow.log
        assert not slow.kinds("hdr") and not slow.kinds("data")
    finally:
        track.close()


async def test_a_failed_header_write_resets_its_stream():
    track = relay._RelayedTrack(("ns", "t"))
    bad = _Fake()

    async def refuse(sid, data):
        raise RuntimeError("write failed")
    bad.stream_write_drain = refuse
    track.add_downstream(bad, 1, 1)
    try:
        track.on_object(_obj(0, 0), 0, 0, 0, 0)
        await _settle()
        (sid,) = [e[1] for e in bad.kinds("open")]
        assert ("reset", sid, int(StreamResetCode.CANCELLED)) in bad.log
        assert track._drops == 1
    finally:
        track.close()


async def test_a_lane_parked_on_a_header_resets_shed_groups(monkeypatch):
    monkeypatch.setattr(relay, "LANE_BYTES", 10_000)
    track = relay._RelayedTrack(("ns", "t"))
    slow = _Fake()
    d = track.add_downstream(slow, 1, 1)
    try:
        for oid in range(2):
            track.on_object(_obj(0, oid), 0, 0, 0, 0)
        await _settle()
        (g0,) = [e[1] for e in slow.kinds("open")]
        slow.gate.clear()
        track.on_object(_obj(1, 0), 0, 0, 1, 0)
        await _settle()
        assert d.lane.busy and d.lane.opening
        for gid in range(2, 6):
            for oid in range(4):
                track.on_object(_obj(gid, oid), 0, 0, gid, 0)
                await _settle(5)
        # Reset while the header write is still parked.
        assert d.lane.floor > 1
        assert ("reset", g0, int(StreamResetCode.CANCELLED)) in slow.log
        slow.gate.set()
        await _settle()
        g1 = slow.kinds("open")[1][1]
        assert ("reset", g1, int(StreamResetCode.CANCELLED)) in slow.log
        _headed(slow)
    finally:
        track.close()


# -- live ------------------------------------------------------------------

def _ring_full_once(session):
    """The first write on each new data stream of `session` finds the TX
    ring full; the ring drains 5 ms later."""
    quic = session._quic
    real = quic.send_stream_data
    seen = set()
    loop = asyncio.get_running_loop()

    def send(stream_id, data, end_stream=False):
        if stream_id % 4 == 3 and stream_id not in seen:
            seen.add(stream_id)
            loop.call_later(0.005, quic.get_tx_data_drain_event(stream_id).set)
            raise BufferError("TX event ring full")
        return real(stream_id, data, end_stream=end_stream)
    quic.send_stream_data = send
    return seen


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_full_tx_ring_delays_headers_and_drops_none(
        use_quic, relay_tables):
    group = 5
    subs = {"video": _Sub(), "audio": _Sub()}
    async with _relay(_PORT + (not use_quic), use_quic, 18) as connect:
        pub = await connect()
        for name, size in (("video", 8_000), ("audio", 200)):
            PublishedTrack(pub, namespace=_NS, trackname=name,
                           object_size=size, group_size=group,
                           rate=100).attach()
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        viewer = await connect()
        for name, sub in subs.items():
            await sub.subscribe(viewer, name)
        tracks = [relay._tracks[(relay._ns_tuple(_NS), n.encode())]
                  for n in subs]
        (session,) = {d.session for t in tracks for d in t.downstream}
        hit = _ring_full_once(session)
        await asyncio.sleep(1.5)
        assert not viewer._close_err
    assert len(hit) >= 20, hit
    for name, sub in subs.items():
        gids = sorted(k[0] for k in sub.objects)
        assert len(gids) >= 10, (name, gids)
        # Groups past the first and before the last arrived whole.
        for k in sorted(sub.objects)[1:-1]:
            assert sub.objects[k] == set(range(group)), (name, k, sub.objects[k])
            assert sub.ends.get(k, (True,))[0], (name, k, sub.ends[k])


@requires_certs
@pytest.mark.skipif(sys.platform == "win32", reason="SIGSTOP")
async def test_a_stalled_viewer_parks_only_its_own_lane(
        monkeypatch, relay_tables):
    # Above aiopquic's 4 MiB per-connection gate on new streams, so the
    # stalled viewer's header writes park before its lane sheds.
    monkeypatch.setattr(relay, "LANE_BYTES", 6 << 20)
    ns, name, rate, group = "hdrdrain/stall", "video", 200, 10
    port = _PORT + 2
    proc = None
    healthy = _Sub()
    try:
        async with _relay(port, True, 18) as connect:
            pub = await connect()
            PublishedTrack(pub, namespace=ns, trackname=name,
                           object_size=20_000, group_size=group,
                           rate=rate).attach()
            await pub.publish_namespace(namespace=ns, wait_response=True)
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", _STALLED_SUB, str(port), ns, name,
                stdout=subprocess.PIPE, cwd=os.getcwd())
            line = await asyncio.wait_for(proc.stdout.readline(), 10)
            assert line.strip() == b"subscribed", line
            track = relay._tracks[(relay._ns_tuple(ns), name.encode())]
            (d_stalled,) = track.downstream
            viewer = await connect()
            await SubscribedTrack(viewer, ns, name,
                                  on_object=healthy.on_object).subscribe(
                                      timeout=5)
            os.kill(proc.pid, signal.SIGSTOP)
            lane = d_stalled.lane
            await asyncio.sleep(1.0)
            before = {k[0] for k in healthy.objects}
            sheds0, parked, peak = lane.sheds, 0, 0
            for _ in range(50):
                await asyncio.sleep(0.1)
                parked += lane.busy and lane.opening
                peak = max(peak, _ring_backlog(d_stalled.session))
            got = {k[0] for k in healthy.objects} - before
            print("stalled sheds", sheds0, "->", lane.sheds, "parked",
                  parked, "peak backlog", peak, "healthy groups", len(got))
            assert parked, "the stalled lane never parked on a header"
            assert lane.sheds - sheds0 >= 2, (sheds0, lane.sheds)
            assert peak <= relay.LANE_BYTES + 2 * 200_000, peak
            # 5 s at rate / group groups per second.
            assert len(got) >= 0.8 * 5 * rate / group, len(got)
    finally:
        if proc is not None:
            proc.kill()
            await proc.wait()
