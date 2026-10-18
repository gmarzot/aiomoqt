"""A truncated subgroup is reset, never closed as if complete.

END_OF_GROUP + FIN claims the group's last object was delivered, so a
publisher that gives up part-way through a group resets the stream
(CANCELLED). A group that really completed still ends with END_OF_GROUP
and FIN. Two tracks on one session, over raw QUIC and WebTransport, so a
reset cannot be credited to the wrong track.
"""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.track import PublishedTrack
from aiomoqt.types import MOQTMessageType, ObjectStatus, StreamResetCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 17100
_DRAFTS = [14, 16, 18, 20]
_NS = "trunc/ns"


class _Gated(PublishedTrack):
    """Produces once `go` is set, after every peer has joined."""

    go: asyncio.Event


class _Stalls(_Gated):
    """Two objects of group 0, then waits forever mid-group."""

    async def produce(self, out):
        await self.go.wait()
        for oid in range(2):
            await out.write(0, oid, b"s" * 64, group_start=oid == 0)
        await asyncio.Event().wait()


class _Completes(_Gated):
    """Group 0 in full, then waits."""

    async def produce(self, out):
        await self.go.wait()
        for oid in range(2):
            await out.write(0, oid, b"c" * 64, group_start=oid == 0)
        out.end_group()
        await asyncio.Event().wait()


class _Seen:
    def __init__(self):
        self.objects = {}   # alias -> [(object_id, status)]
        self.ends = {}      # alias -> [(clean, reset_code)]

    def watch(self, session, alias):
        def _obj(obj, *_):
            self.objects.setdefault(alias, []).append(
                (obj.object_id, obj.status))
        session.register_object_handler(alias, _obj)
        session.register_stream_end_handler(
            alias, lambda g, sg, clean, reset_code:
            self.ends.setdefault(alias, []).append((clean, reset_code)))

    def payloads(self, alias):
        return [o for o, st in self.objects.get(alias, ())
                if st == ObjectStatus.NORMAL]


async def _until(cond, timeout=5.0):
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(timeout):
            while not cond():
                await asyncio.sleep(0.02)
    return cond()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", _DRAFTS)
async def test_abort_mid_group_resets_and_complete_group_fins(draft, use_quic):
    port = _BASE_PORT + 2 * _DRAFTS.index(draft) + (0 if use_quic else 1)
    rx = {}

    async def _on_ns(session, msg):
        rx['session'] = session
        session.publish_namepace_ok(msg)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.PUBLISH_NAMESPACE, _on_ns)
    handle = await server.serve()
    seen = _Seen()
    try:
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as session:
            await session.client_session_init()
            stalls = _Stalls(session, namespace=_NS, trackname="stalls")
            completes = _Completes(session, namespace=_NS,
                                   trackname="completes")
            go = asyncio.Event()
            for track in (stalls, completes):
                track.go = go
                track.attach()
            await session.publish_namespace(namespace=_NS,
                                            wait_response=True)
            sub = rx['session']
            aliases = {}
            for name in ("stalls", "completes"):
                ok = await sub.subscribe(_NS, name, forward=1,
                                         wait_response=True)
                seen.watch(sub, ok.track_alias)
                aliases[name] = ok.track_alias
            a_s, a_c = aliases["stalls"], aliases["completes"]
            await asyncio.sleep(0.1)
            go.set()
            assert await _until(lambda: len(seen.payloads(a_s)) == 2
                                and seen.ends.get(a_c))

            stalls._production.cancel()  # gives up mid-group
            assert await _until(lambda: seen.ends.get(a_s))
            completes._production.cancel()
            await asyncio.sleep(0.1)
    finally:
        handle.close()

    assert seen.ends[a_s] == [(False, int(StreamResetCode.CANCELLED))], (
        "a truncated group was not reset")
    assert all(st == ObjectStatus.NORMAL for _, st in seen.objects[a_s])
    assert seen.ends[a_c] == [(True, 0)]
    assert (2, ObjectStatus.END_OF_GROUP) in seen.objects[a_c]
