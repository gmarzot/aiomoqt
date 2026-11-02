"""Bench payloads follow from their location, and --verify-payload
catches objects that do not: every generator's output passes, swapped,
shifted, truncated and corrupted payloads fail."""
import asyncio
from types import SimpleNamespace

import pytest

from aiomoqt.tools.pub_bench import _GapTrack
from aiomoqt.tools.sub_bench import BenchReporter
from aiomoqt.track import PublishedTrack, SubscribedTrack, VideoTrack
from aiomoqt.types import ForwardingPreference
from aiomoqt.utils.payload import PayloadCheck, counted

from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests._procs import free_udp_port
from aiomoqt.tests.test_immutable_properties import _loopback


def _good(g, o, n, ft=None):
    head = f"{g}.{o}.{n}" + (f".{ft}" if ft else "") + "|"
    return (head.encode() + counted(n))[:n]


@pytest.mark.parametrize("n", [1, 4, 7, 8, 9, 600, 4096, 300_000])
def test_layout_passes_at_every_size(n):
    assert PayloadCheck().matches(12, 345, _good(12, 345, n))


def test_video_frames_pass():
    assert PayloadCheck().matches(3, 0, _good(3, 0, 200_000, "I"))
    assert PayloadCheck().matches(3, 7, _good(3, 7, 10_000, "B"))


@pytest.mark.parametrize("bad", [
    _good(5, 2, 4096),                                   # another object's
    _good(5, 1, 4096)[:-1],                              # truncated
    _good(5, 1, 4096)[:100] + _good(5, 1, 4096)[101:] + b"x",   # shifted
    _good(5, 1, 4096)[:2000] + b"\xff" + _good(5, 1, 4096)[2001:],  # corrupt
    bytes(4096),                                         # zeros
], ids=["swapped", "truncated", "shifted", "corrupt", "zeros"])
def test_broken_payloads_fail(bad):
    assert not PayloadCheck().matches(5, 1, bad)


def test_every_nth_object():
    check = PayloadCheck(every=4)
    for o in range(16):
        check(0, o, b"wrong")
    assert (check.seen, check.checked, check.bad) == (16, 4, 4)


def test_reporter_counts_bad_objects():
    rep = BenchReporter(verify_payload=1)
    for o, payload in ((0, _good(0, 0, 64)), (1, b"garbage")):
        msg = SimpleNamespace(status=None, group_id=0, object_id=o,
                              payload=payload, extensions=None)
        rep.on_object(msg, len(payload), 0, 0, 0)
    assert rep.payload_bad == 1
    assert rep.payload_check.first_bad[:2] == (0, 1)


def _tracks(session):
    return {
        "plain": PublishedTrack(session, "pc", "plain", object_size=700,
                                group_size=8, num_subgroups=2, rate=400),
        "dgram": PublishedTrack(session, "pc", "dgram", object_size=500,
                                group_size=8, rate=400,
                                forwarding=ForwardingPreference.DATAGRAM),
        "video": VideoTrack(session, "pc", "video", resolution="240p",
                            fps=100, gop_size=10),
        "gaps": _GapTrack(session, "pc", "gaps", object_size=300,
                          group_size=4, rate=200),
    }


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_every_generator_writes_the_layout(use_quic):
    publishes = asyncio.Queue()
    checks = {}
    async with _loopback(free_udp_port(), 18, use_quic, publishes) as session:
        tracks = _tracks(session)
        for t in tracks.values():
            t.attach()
        await session.publish_namespace(namespace="pc", wait_response=True)
        peer, _ = await asyncio.wait_for(publishes.get(), 5.0)
        for name in tracks:
            check = checks[name] = PayloadCheck()
            sub = SubscribedTrack(peer, "pc", name)
            sub.on_object = (lambda msg, size, recv, gid, sg, _c=check:
                             None if msg.status
                             else _c(gid, msg.object_id, msg.payload))
            await sub.subscribe()
        for _ in range(300):
            if all(c.checked >= 40 for c in checks.values()):
                break
            await asyncio.sleep(0.01)
    for name, c in checks.items():
        assert c.checked >= 40 and c.bad == 0, (name, c.checked, c.first_bad)


@requires_certs
async def test_prefilled_history_writes_the_layout():
    publishes = asyncio.Queue()
    async with _loopback(free_udp_port(), 18, True, publishes) as session:
        track = PublishedTrack(session, "pc", "pre", object_size=900,
                               group_size=5, fetch_history_bytes=1 << 20)
        seen = []
        track._remember = lambda g, o, p, *a, **k: seen.append((g, o, p))
        track.prefill(3)
    check = PayloadCheck()
    for g, o, p in seen:
        check(g, o, p)
    assert check.checked == 15 and check.bad == 0, check.first_bad
