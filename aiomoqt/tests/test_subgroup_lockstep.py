"""Subgroups of one track advance groups together.

Each subgroup has its own producer task; a slow one must hold the others
at the group boundary, or a late subscriber starting at the newest group
misses the lagging subgroups' objects.
"""
import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from aiomoqt import track as track_mod
from aiomoqt.client import MOQTClient
from aiomoqt.track import PublishedTrack
from aiomoqt.tools import moq_interop_relay as relay

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 18500
_SUBGROUPS = 4
_GROUP = 8
_SLOW = 1


class _RecordingSender:
    """Stands in for _SubgroupSender: records group starts and objects;
    the slow subgroup takes longer per object."""

    events = []

    def __init__(self, track, session, sub, track_alias, subgroup_id,
                 priority):
        self.subgroup_id = subgroup_id
        self.header = None

    def new_group(self, group_id):
        self.header = SimpleNamespace(group_id=group_id)
        self.events.append(("group", self.subgroup_id, group_id))

    def encode_open(self, object_id, payload, extensions):
        return None

    async def send(self, object_id, payload, extensions):
        await asyncio.sleep(0.004 if self.subgroup_id == _SLOW else 0)
        return len(payload)

    def reset(self):
        pass


@pytest.mark.asyncio
async def test_a_slow_subgroup_holds_the_others_at_the_group_boundary(
        monkeypatch):
    _RecordingSender.events = []
    monkeypatch.setattr(track_mod, "_SubgroupSender", _RecordingSender)
    session = SimpleNamespace(_close_err=None)
    t = PublishedTrack(session, "ns", trackname="t", object_size=32,
                       group_size=_GROUP, num_subgroups=_SUBGROUPS, rate=0)
    t._quiet = True
    t._spawn_producers(session, 1, b"\0" * 32)
    await asyncio.sleep(0.5)
    tasks = list(t._tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    current = {}
    started = {sg: [] for sg in range(_SUBGROUPS)}
    for _kind, sg, group in _RecordingSender.events:
        started[sg].append(group)
        current[sg] = group
        if len(current) == _SUBGROUPS:
            assert max(current.values()) - min(current.values()) <= 1, current
    assert started[_SLOW][-1] >= 5
    for sg in range(_SUBGROUPS):
        n = len(started[sg])
        assert started[sg] == list(range(n)), (sg, started[sg][:10])
        assert abs(n - len(started[_SLOW])) <= 1


@pytest.mark.asyncio
async def test_one_subgroup_ending_releases_the_others(monkeypatch):
    _RecordingSender.events = []
    monkeypatch.setattr(track_mod, "_SubgroupSender", _RecordingSender)
    session = SimpleNamespace(_close_err=None)
    t = PublishedTrack(session, "ns", trackname="t", object_size=32,
                       group_size=_GROUP, num_subgroups=_SUBGROUPS, rate=0)
    t._quiet = True
    t._spawn_producers(session, 1, b"\0" * 32)
    await asyncio.sleep(0.1)
    tasks = list(t._tasks)
    tasks[0].cancel()
    await asyncio.wait_for(
        asyncio.gather(*tasks, return_exceptions=True), timeout=2.0)
    assert all(task.done() for task in tasks)


_NS = "lockstep/ns"
_NAME = "t"
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _reset_relay_state():
    relay._announced.clear()
    relay._tracks.clear()


@requires_certs
@pytest.mark.asyncio
@_TRANSPORTS
async def test_a_late_subscriber_gets_every_subgroup_of_each_group(
        use_quic, monkeypatch):
    real_send = track_mod._SubgroupSender.send

    async def _send(self, object_id, payload, extensions):
        if self.subgroup_id == _SLOW:
            await asyncio.sleep(0.008)
        return await real_send(self, object_id, payload, extensions)

    real_encode = track_mod._SubgroupSender.encode_open

    def _encode(self, object_id, payload, extensions):
        if self.subgroup_id == _SLOW:
            return None                     # every object goes through _send
        return real_encode(self, object_id, payload, extensions)

    monkeypatch.setattr(track_mod._SubgroupSender, "send", _send)
    monkeypatch.setattr(track_mod._SubgroupSender, "encode_open", _encode)
    port = _BASE_PORT + 10 + (not use_quic)
    _reset_relay_state()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=18)
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def _session():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=18)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s

            pub = await _session()
            track = PublishedTrack(pub, namespace=_NS, trackname=_NAME,
                                   object_size=64, group_size=_GROUP,
                                   num_subgroups=_SUBGROUPS, rate=800)
            track._quiet = True
            await track.publish(announce_namespace=False,
                                publish_track=True)

            early = await _session()
            await early.subscribe(_NS, _NAME, forward=1, wait_response=True)
            await asyncio.sleep(2.0)

            late = await _session()
            got = {}

            def _on_object(msg, _size, _ts, group_id, subgroup_id):
                if msg.object_id < _GROUP:  # skip END_OF_GROUP
                    got.setdefault(group_id, set()).add(
                        (subgroup_id, msg.object_id))
            late.on_object_received = _on_object
            await late.subscribe(_NS, _NAME, forward=1, wait_response=True)
            await asyncio.sleep(1.5)

            groups = sorted(got)
            assert len(groups) >= 4, groups
            # First and last group may be partial.
            for g in groups[1:-1]:
                subgroups = {sg for sg, _ in got[g]}
                assert subgroups == set(range(_SUBGROUPS)), (g, got[g])
                assert len(got[g]) == _GROUP, (g, sorted(got[g]))
    finally:
        handle.close()
        _reset_relay_state()
