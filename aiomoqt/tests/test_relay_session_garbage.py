"""Sessions that come and go leave no cyclic garbage on the relay: the
session, its send lanes, deliveries and downstreams are freed by
reference counting when it closes, so session churn adds no work to GC
passes. Raw QUIC and WT. What startup built is frozen out of GC passes."""
import asyncio
import gc
from collections import Counter

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.delivery import SendLane
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tests.test_relay_forwarding import _reset_relay_state

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 15680
_NS = "gc/churn"
_SESSIONS = 6


class _Prod(PublishedTrack):
    async def produce(self, out):
        g = 0
        while True:
            for o in range(5):
                await out.write(g, o, b"x" * 100, group_start=o == 0)
                await asyncio.sleep(0.01)
            g += 1


def _relay_side(garbage):
    """Garbage the relay's side of a session leaves: its sessions, lanes
    and relay objects; the clients here share the process."""
    out = []
    for o in garbage:
        mod = type(o).__module__
        if mod == relay.__name__ or isinstance(o, SendLane):
            out.append(o)
        elif (mod == "aiomoqt.protocol"
              and type(o).__name__.startswith("MOQTSession")
              and not getattr(o, "_is_client", True)):
            out.append(o)
    return out


async def _churn(port, use_quic):
    for _ in range(_SESSIONS):
        client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                            verify_tls=False, supported_drafts=22)
        async with client.connect() as s:
            await s.client_session_init()
            await SubscribedTrack(s, _NS, "video",
                                  on_object=lambda *a: None).subscribe(
                                      timeout=5.0)
            try:
                await SubscribedTrack(s, _NS, "missing",
                                      on_object=lambda *a: None).subscribe(
                                          timeout=5.0)
            except Exception:
                pass
            await asyncio.sleep(0.1)


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_closed_sessions_leave_no_cyclic_garbage(use_quic):
    _reset_relay_state()
    port = _BASE_PORT + (not use_quic)
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=22)
    handle = await server.serve()
    watcher = asyncio.create_task(relay._watch_lanes())
    left = []
    try:
        pub_client = MOQTClient("localhost", port, path="/",
                                use_quic=use_quic, verify_tls=False,
                                supported_drafts=22)
        async with pub_client.connect() as pub:
            await pub.client_session_init()
            _Prod(pub, _NS, "video", priority=128).attach()
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            gc.collect()
            # Relay objects from before the churn (this relay's publisher
            # side, an earlier test's teardown) are not counted.
            before = {id(o) for o in _relay_side(gc.get_objects())}
            gc.disable()
            try:
                await _churn(port, use_quic)
                # The relay tears the sessions down and their lanes finish.
                await asyncio.sleep(0.5)
                # Stopped lanes' time checks would lapse at their deadline.
                relay._lane_due.clear()
                gc.set_debug(gc.DEBUG_SAVEALL)
                gc.collect()
                left = [o for o in _relay_side(gc.garbage)
                        if id(o) not in before]
            finally:
                gc.set_debug(0)
                gc.garbage.clear()
                gc.enable()
    finally:
        watcher.cancel()
        handle.close()
        _reset_relay_state()
    assert not left, Counter(type(o).__qualname__ for o in left)


def test_the_startup_heap_leaves_gc_passes():
    try:
        relay._freeze_startup_heap()
        assert gc.get_freeze_count() > 1000
    finally:
        gc.unfreeze()
