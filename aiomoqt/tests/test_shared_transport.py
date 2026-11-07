"""MOQTClient(shared_transport=): sessions on one picoquic worker, over raw QUIC
and WebTransport. Refused and closing sessions must leave the others alone."""
import asyncio
import contextlib
import os
import queue
import sys
import threading

import pytest

from aiomoqt.client import MOQTClient, SharedTransport
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.types import FilterType, MOQTRequestError
from aiomoqt.utils import workers

from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_relay_admission import (  # noqa: F401
    _NS, _TRANSPORTS, _assert_refused, _counted, _grows, _publisher, _relay, _reset,
)

_BASE_PORT = 30600
_DRAFT = 22
_HOST = "127.0.0.1"


def _port(case: int, use_quic: bool) -> int:
    return _BASE_PORT + 2 * case + (not use_quic)


def _client(port, use_quic, shared):
    return MOQTClient(_HOST, port, path="/", use_quic=use_quic, verify_tls=False,
                      supported_drafts=_DRAFT, shared_transport=shared)


async def _open(stack, port, use_quic, shared):
    session = await stack.enter_async_context(_client(port, use_quic, shared).connect())
    await session.client_session_init()
    return session


@requires_certs
@_TRANSPORTS
async def test_every_session_gets_every_track(use_quic):
    port = _port(0, use_quic)
    shared = SharedTransport()
    async with _relay(port, _DRAFT, use_quic) as connect:
        await _publisher(connect, ["a", "b"])
        try:
            async with contextlib.AsyncExitStack() as stack:
                counts = []
                for _ in range(3):
                    session = await _open(stack, port, use_quic, shared)
                    for name in ("a", "b"):
                        counts.append((await _counted(session, name))[1])
                for got in counts:
                    await _grows(got)
                    assert got
        finally:
            shared.close()


@requires_certs
@_TRANSPORTS
@pytest.mark.skipif(sys.platform != "linux", reason="counts /proc threads")
async def test_more_sessions_add_no_thread(use_quic):
    port = _port(1, use_quic)
    shared = SharedTransport()
    async with _relay(port, _DRAFT, use_quic) as connect:
        await _publisher(connect, ["a"])
        try:
            async with contextlib.AsyncExitStack() as stack:
                await _open(stack, port, use_quic, shared)
                threads = len(os.listdir("/proc/self/task"))
                for _ in range(4):
                    await _open(stack, port, use_quic, shared)
                assert len(os.listdir("/proc/self/task")) == threads
        finally:
            shared.close()


@requires_certs
@_TRANSPORTS
async def test_closing_one_session_leaves_the_others(use_quic):
    port = _port(2, use_quic)
    shared = SharedTransport()
    async with _relay(port, _DRAFT, use_quic) as connect:
        await _publisher(connect, ["a"])
        try:
            async with contextlib.AsyncExitStack() as stack:
                kept = await _open(stack, port, use_quic, shared)
                _, got = await _counted(kept, "a")
                async with _client(port, use_quic, shared).connect() as gone:
                    await gone.client_session_init()
                    _, gone_got = await _counted(gone, "a")
                    await _grows(gone_got)
                await _grows(got)
                late = await _open(stack, port, use_quic, shared)
                _, late_got = await _counted(late, "a")
                await _grows(late_got)
                await _grows(got)
        finally:
            shared.close()


@requires_certs
@_TRANSPORTS
async def test_refused_session_waits_on_its_connection(monkeypatch, use_quic):
    port = _port(3, use_quic)
    monkeypatch.setattr(relay, "MAX_SUBSCRIBERS", 1)
    shared = SharedTransport()
    async with _relay(port, _DRAFT, use_quic) as connect:
        await _publisher(connect, ["a"])
        try:
            async with contextlib.AsyncExitStack() as stack:
                first = await _open(stack, port, use_quic, shared)
                _, got = await _counted(first, "a")
                await _grows(got)
                other = await _open(stack, port, use_quic, shared)
                with pytest.raises(MOQTRequestError) as refused:
                    await other.subscribe(_NS, "a", wait_response=True)
                _assert_refused(refused.value, _DRAFT)
                await _grows(got)
                # Same connection, once the relay has room.
                monkeypatch.setattr(relay, "MAX_SUBSCRIBERS", 2)
                _, other_got = await _counted(other, "a")
                await _grows(other_got)
                await _grows(got)
        finally:
            shared.close()


async def _batch_report(port, use_quic, want):
    """Run a three-sub batch until a report satisfies want, and return it."""
    scheme = "moqt" if use_quic else "https"
    stop, events = threading.Event(), queue.Queue()
    task = asyncio.create_task(workers._subscriber_batch_task(dict(
        worker_id=0, batch_size=3, stagger=0.01, relay_url=f"{scheme}://{_HOST}:{port}/",
        namespace=_NS, trackname="a", draft=_DRAFT, insecure=True, force_quic=False,
        sub_filter=int(FilterType.LATEST_OBJECT), keep_window=3.0, shared_transport=True,
    ), stop, events))
    try:
        async with asyncio.timeout(20):
            while True:
                while events.empty():
                    await asyncio.sleep(0.05)
                report = events.get()
                if report.get("kind") == "batch_stats" and want(report):
                    return report
    finally:
        stop.set()
        await asyncio.wait_for(task, 15)


@requires_certs
@_TRANSPORTS
async def test_batch_worker_delivers_on_one_worker(use_quic):
    port = _port(4, use_quic)
    async with _relay(port, _DRAFT, use_quic) as connect:
        await _publisher(connect, ["a"])
        report = await _batch_report(port, use_quic,
                                     lambda r: r["delivering"] == 3 and r["rx_objs"] > 0)
    assert report["rx_bytes"] > 0 and report["refused"] == 0


@requires_certs
@_TRANSPORTS
async def test_batch_worker_keeps_delivering_beside_a_refused_slot(monkeypatch, use_quic):
    port = _port(5, use_quic)
    monkeypatch.setattr(relay, "MAX_SUBSCRIBERS", 2)
    async with _relay(port, _DRAFT, use_quic) as connect:
        await _publisher(connect, ["a"])
        report = await _batch_report(
            port, use_quic,
            lambda r: r["refusing"] == 1 and r["delivering"] == 2 and r["rx_objs"] > 0)
    assert report["refused"] >= 1 and report["refused_reason"]
