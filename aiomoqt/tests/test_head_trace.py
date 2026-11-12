"""PublishedTrack.trace_heads: one aiomoqt.data line per group's first
object and peer, with its open and pickup times after the write; tracks
without it log nothing."""
import asyncio
import logging
import re

import pytest

from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_multi_track_publish import _loopback, _Receiver, _until
from aiomoqt.track import PublishedTrack

pytestmark = requires_certs

_BASE_PORT = 31800
_DRAFT = 22
_NS = "head/trace"
_TRACED = ("video", "video_1")
_UNTRACED = "audio"
_MIN_GROUPS = 3
_LINE = re.compile(
    r"head (\S+) g(\d+) to \S+: (\d+) B made \S+, stream open \+([\d.]+) ms, "
    r"in packets \+([\d.]+) ms; cwin (\d+)->(\d+) KB, in flight (\d+)->(\d+) KB")


class _Groups(PublishedTrack):
    """Groups of 5 objects at about 200/s."""

    async def produce(self, out):
        group = 0
        while True:
            for oid in range(5):
                await out.write(group, oid, bytes(self.object_size),
                                group_start=oid == 0)
                await asyncio.sleep(0.005)
            group += 1


class _Lines(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_traced_tracks_log_each_head_once_per_peer(use_quic):
    log = logging.getLogger("aiomoqt.data")
    handler, level = _Lines(), log.level
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    rx = _Receiver()
    try:
        async with _loopback(_BASE_PORT + (0 if use_quic else 1), _DRAFT, rx,
                             use_quic=use_quic) as session:
            tracks = []
            for name in (*_TRACED, _UNTRACED):
                track = _Groups(session, namespace=_NS, trackname=name,
                                object_size=1000)
                track.trace_heads = name in _TRACED
                await track.publish(forward=0)
                tracks.append(track)
            for _ in tracks:
                msg = await asyncio.wait_for(rx.publishes.get(), 5.0)
                rx.watch(rx.session, msg.track_alias)
                rx.session.publish_ok(msg, forward=1)

            def heads():
                found = {}
                for line in handler.lines:
                    m = _LINE.match(line)
                    if m:
                        found.setdefault(m.group(1), []).append(m)
                return found

            enough = await _until(lambda: all(
                len(heads().get(n, ())) >= _MIN_GROUPS for n in _TRACED))
            found = heads()
            assert enough, f"head lines per track: " \
                f"{ {n: len(v) for n, v in found.items()} }; {handler.lines[-5:]}"
            assert _UNTRACED not in found
            assert not [ln for ln in handler.lines
                        if "not taken into packets" in ln]
            for name in _TRACED:
                groups = [int(m.group(2)) for m in found[name]]
                assert len(groups) == len(set(groups)), groups
                for m in found[name]:
                    assert int(m.group(3)) > 1000
                    assert 0 <= float(m.group(4)) <= float(m.group(5))
                    assert int(m.group(6)) > 0 and int(m.group(7)) > 0
    finally:
        log.removeHandler(handler)
        log.setLevel(level)
