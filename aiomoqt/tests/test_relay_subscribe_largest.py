"""A downstream SUBSCRIBE_OK reports the newest Largest the relay holds
(§10.2.11): objects that arrived while the first subscriber attached,
still queued for it, count, not only the upstream SUBSCRIBE_OK's Largest.
Two tracks on one publisher session, over raw QUIC and WebTransport."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_PORT = 29800
_NS = "relay/largest"
_SIZES = {"video": 300, "audio": 100}
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])


def _key(name):
    return ((b"relay", b"largest"), name.encode())


def _reset():
    relay._announced.clear()
    relay._announce_requests.clear()
    relay._tracks.clear()
    relay._track_subs.clear()
    relay._ns_subs.clear()


async def _until(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond() and loop.time() < deadline:
        await asyncio.sleep(0.02)
    return cond()


@_TRANSPORTS
async def test_subscribe_ok_reports_objects_already_in(use_quic,
                                                       monkeypatch):
    # The first subscriber's attach is held up while objects keep coming
    # in, as an upstream backfill FETCH would.
    attach = relay._attach_subscriber
    held = {}

    def noted_attach(session, msg, track, largest):
        held[track.key[1].decode()] = (track.largest, track.arrived)
        return attach(session, msg, track, largest)

    backfill = relay._backfill

    async def slow_backfill(track, msg, largest):
        await _until(lambda: track.arrived is not None
                     and track.largest is not None
                     and track.arrived > track.largest, 2.0)
        await backfill(track, msg, largest)

    monkeypatch.setattr(relay, "_attach_subscriber", noted_attach)
    monkeypatch.setattr(relay, "_backfill", slow_backfill)
    _reset()
    port = _PORT + (not use_quic)
    handle = await relay._build_server("localhost", port, CERT, KEY,
                                       use_quic=use_quic,
                                       draft=(22, 18)).serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=(22,))
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s

            pub = await connect()
            for name, size in _SIZES.items():
                track = PublishedTrack(pub, namespace=_NS, trackname=name,
                                       object_size=size, group_size=5,
                                       rate=200, fetch_history_bytes=1 << 20)
                track.prefill(2)
                track.attach()
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            rx = await connect()
            got = {name: [] for name in _SIZES}
            sizes = {size: name for name, size in _SIZES.items()}
            rx.on_object_received = (
                lambda m, _n, _ts, g, _sg: got[sizes[len(m.payload)]].append(
                    (g, m.object_id)) if len(m.payload) in sizes else None)
            oks = dict(zip(_SIZES, await asyncio.gather(*(
                rx.subscribe(_NS, name, forward=1, wait_response=True)
                for name in _SIZES))))
            for name, ok in oks.items():
                reported = (ok.largest_group_id, ok.largest_object_id)
                upstream, arrived = held[name]
                assert arrived > upstream, (name, upstream, arrived)
                assert reported >= arrived, (name, reported, arrived)
            assert await _until(lambda: all(len(v) > 3 for v in got.values()))
            assert rx._close_err is None
    finally:
        handle.close()
        _reset()
