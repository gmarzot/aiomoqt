"""A producer task that fails (a write racing the session close) has its
exception retrieved: no "Task exception was never retrieved"."""
import asyncio
import gc

from aiomoqt.track import PublishedTrack
from aiomoqt.types import ForwardingPreference


async def test_a_failed_subgroup_producer_is_retrieved(monkeypatch):
    async def closed(self, **_kw):
        raise ConnectionError("WT session closed")

    monkeypatch.setattr(PublishedTrack, "_generate_subgroup", closed)
    loop = asyncio.get_running_loop()
    unretrieved = []
    loop.set_exception_handler(lambda _loop, ctx: unretrieved.append(ctx))
    track = object.__new__(PublishedTrack)
    track.namespace, track.trackname = "ns", "t"
    track._tasks = set()
    track._largest = None
    track.timestamps = "loc"
    track.forwarding = ForwardingPreference.SUBGROUP
    track.num_subgroups = 2
    track.priority = 128
    try:
        track._spawn_producers(None, 1, b"")
        for _ in range(5):
            await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)
    assert not track._tasks
    assert not unretrieved, unretrieved
