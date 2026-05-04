"""Loopback JOIN self-tests.

Covers JOINING_SUBSCRIBE varieties that complement the basic RELATIVE
case in test_loopback_fetch.py:
  - absolute join from a specific group id
  - relative join with offset 0 (joins at largest, fetches nothing past)

Shares the loopback server harness defined in test_loopback_fetch.
"""
import asyncio
import os

import pytest

from aiomoqt.types import FetchType

from aiomoqt.tests.test_loopback_fetch import (
    FetchTestCache, _start_server, _connect_client, CERT, KEY,
)

pytestmark = pytest.mark.skipif(
    not os.path.exists(CERT) or not os.path.exists(KEY),
    reason="TLS certs not found in certs/",
)


_BASE_PORT = 14460


@pytest.fixture(params=[True, False], ids=["use_quic", "wt"])
def use_quic(request):
    return request.param


@pytest.mark.asyncio
async def test_absolute_join(use_quic):
    """ABSOLUTE_JOINING fetch from group 1 — fetch portion covers groups
    1..largest, live portion starts from largest+1."""
    port = _BASE_PORT + 1 + (0 if use_quic else 100)
    cache = FetchTestCache(num_groups=4, objects_per_group=8, object_size=32)
    server = await _start_server(port, cache, use_quic)

    fetched_objects = []

    def on_fetch(msg, size, ts, request_id):
        fetched_objects.append((msg.group_id, msg.object_id))

    try:
        client = await _connect_client(port, use_quic)
        async with client.connect() as session:
            await session.client_session_init()
            session.on_fetch_object = on_fetch

            await session.join(
                namespace="bench",
                track_name="track",
                joining_start=1,
                fetch_type=FetchType.ABSOLUTE_JOINING,
                wait_response=True,
            )

            await asyncio.sleep(0.5)

        fetch_groups = sorted(set(g for g, _ in fetched_objects))
        assert fetch_groups == [1, 2, 3], \
            f"expected groups 1..3, got {fetch_groups}"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_relative_join_zero(use_quic):
    """RELATIVE_JOINING fetch with offset 0 — fetch portion covers only
    the current largest group; prior groups not in the fetched range."""
    port = _BASE_PORT + 2 + (0 if use_quic else 100)
    cache = FetchTestCache(num_groups=4, objects_per_group=8, object_size=32)
    server = await _start_server(port, cache, use_quic)

    fetched_objects = []

    def on_fetch(msg, size, ts, request_id):
        fetched_objects.append((msg.group_id, msg.object_id))

    try:
        client = await _connect_client(port, use_quic)
        async with client.connect() as session:
            await session.client_session_init()
            session.on_fetch_object = on_fetch

            await session.join(
                namespace="bench",
                track_name="track",
                joining_start=0,
                fetch_type=FetchType.RELATIVE_JOINING,
                wait_response=True,
            )

            await asyncio.sleep(0.5)

        fetch_groups = sorted(set(g for g, _ in fetched_objects))
        assert fetch_groups == [3], \
            f"expected group 3 only (largest), got {fetch_groups}"
    finally:
        server.close()
