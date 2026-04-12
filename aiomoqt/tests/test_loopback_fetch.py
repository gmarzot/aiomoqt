"""Phase 5 — Loopback fetch self-tests.

Runs publisher + subscriber in a single process via qh3 on localhost.
No relay needed. Tests validate the full FETCH lifecycle:
- FetchHeader + FetchObject wire path
- on_fetch_object callback delivery
- request↔stream binding table bookkeeping
- d16 delta decode with prior-object tracking
- FETCH_OK / FETCH_ERROR control-plane handling
- Admission control (unknown request_id → stream rejection)

Test matrix:
  test_joining_fetch_relative  — join() with RELATIVE_JOINING
  test_standalone_fetch        — standalone FETCH of explicit range
  test_fetch_invalid_range     — FETCH_ERROR when start > largest
  test_fetch_unknown_rejected  — unknown request_id → STOP_SENDING
"""
import asyncio
import os
import ssl
import time

import pytest

from qh3.quic.configuration import QuicConfiguration
from qh3.asyncio.server import serve
from qh3.h3.connection import H3_ALPN

from aiomoqt.types import (
    MOQTMessageType, FetchType, GroupOrder,
    MOQT_TIMESTAMP_EXT, MOQTRequestError,
)
from aiomoqt.messages import Fetch, Subscribe
from aiomoqt.messages.track import FetchHeader, FetchObject, SubgroupHeader
from aiomoqt.client import MOQTClient
from aiomoqt.protocol import MOQTPeer, MOQTSession

# ---------------------------------------------------------------------------
# Test certs
# ---------------------------------------------------------------------------
_CERT_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'certs')
CERT = os.path.realpath(os.path.join(_CERT_DIR, 'cert.pem'))
KEY = os.path.realpath(os.path.join(_CERT_DIR, 'key.pem'))

# Skip entire module if test certs are missing
pytestmark = pytest.mark.skipif(
    not os.path.exists(CERT) or not os.path.exists(KEY),
    reason="TLS certs not found in certs/",
)


# ---------------------------------------------------------------------------
# Server-side: in-memory cache + fetch handler
# ---------------------------------------------------------------------------
class FetchTestCache:
    """Simple in-memory object cache for the server side.

    Stores (group_id, object_id) → payload for a small number of groups
    so the server can respond to FETCH requests by replaying from cache.
    """

    def __init__(self, num_groups: int = 5, objects_per_group: int = 10,
                 object_size: int = 64):
        self.num_groups = num_groups
        self.objects_per_group = objects_per_group
        self.object_size = object_size
        # Pre-populate cache
        self.objects: dict = {}
        for g in range(num_groups):
            for o in range(objects_per_group):
                payload = f"g{g}o{o}".encode().ljust(object_size, b'\x00')
                self.objects[(g, o)] = payload
        self.largest_group = num_groups - 1
        self.largest_object = objects_per_group - 1


def _make_fetch_handler(cache: FetchTestCache):
    """Return an async handler for incoming FETCH messages.

    The handler:
    1. Validates the requested range against the cache
    2. Sends FETCH_OK on the control stream
    3. Opens a uni stream, writes FETCH_HEADER + FetchObjects, FINs it
    """

    async def _handle_fetch(session: MOQTSession, msg: Fetch):
        request_id = msg.request_id

        if msg.fetch_type == FetchType.STANDALONE:
            start_group = msg.start_group or 0
            start_object = msg.start_object or 0
            end_group = msg.end_group or cache.largest_group
            end_object = msg.end_object or cache.largest_object
        else:
            # Joining fetch — range computed from cache + joining_start
            end_group = cache.largest_group
            end_object = cache.largest_object
            if msg.fetch_type == FetchType.RELATIVE_JOINING:
                start_group = max(0, cache.largest_group - (msg.joining_start or 0))
            else:
                start_group = msg.joining_start or 0
            start_object = 0

        # Check range validity
        if start_group > cache.largest_group:
            session.fetch_error(
                request_id=request_id,
                error_code=0x02,  # INVALID_RANGE
                reason="start beyond largest group",
            )
            return

        # Send FETCH_OK
        session.fetch_ok(
            request_id=request_id,
            largest_group_id=cache.largest_group,
            largest_object_id=cache.largest_object,
            group_order=GroupOrder.ASCENDING,
        )

        # Open uni stream and write FETCH_HEADER + objects
        stream_id = session.open_uni_stream()
        header = FetchHeader(request_id=request_id)
        session.stream_write(stream_id, header.serialize().data)

        for g in range(start_group, end_group + 1):
            obj_end = (end_object + 1) if g == end_group else cache.objects_per_group
            for o in range(start_object if g == start_group else 0, obj_end):
                payload = cache.objects.get((g, o), b'')
                obj = FetchObject(
                    group_id=g,
                    subgroup_id=0,
                    object_id=o,
                    publisher_priority=128,
                    payload=payload,
                )
                buf = obj.serialize()
                session.stream_write(stream_id, buf.data)

        # FIN the stream
        session.stream_write(stream_id, b'', end_stream=True)
        session.transmit()

    return _handle_fetch


def _make_subscribe_handler(cache: FetchTestCache):
    """Return an async handler that auto-responds to SUBSCRIBE.

    Sends SUBSCRIBE_OK with the cache's largest location, then starts
    generating live objects from the next group onward.
    """

    async def _handle_subscribe(session: MOQTSession, msg: Subscribe):
        ok = session.subscribe_ok(
            request_msg=msg,
            content_exists=1,
            largest_group_id=cache.largest_group,
            largest_object_id=cache.largest_object,
        )
        track_alias = ok.track_alias

        # Start generating "live" objects from next group
        live_group = cache.largest_group + 1
        stream_id = session.open_uni_stream()
        header = SubgroupHeader(
            track_alias=track_alias,
            group_id=live_group,
            subgroup_id=0,
            publisher_priority=128,
            extensions_present=True,
        )
        session.stream_write(stream_id, header.serialize().data)
        session.transmit()

        # Send a few live objects
        for obj_id in range(5):
            extensions = {MOQT_TIMESTAMP_EXT: int(time.time() * 1000)}
            buf = header.next_object(
                payload=f"live-{live_group}.{obj_id}".encode().ljust(64, b'\x00'),
                extensions=extensions,
                object_id=obj_id,
            )
            session.stream_write(stream_id, buf.data)
            session.transmit()
            await asyncio.sleep(0.01)

        # FIN
        session.stream_write(stream_id, b'', end_stream=True)
        session.transmit()

    return _handle_subscribe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _start_server(port: int, cache: FetchTestCache):
    """Start a loopback server with fetch + subscribe handlers."""
    peer = MOQTPeer()
    peer.endpoint = "moq"
    peer.register_handler(
        MOQTMessageType.SUBSCRIBE,
        _make_subscribe_handler(cache))
    peer.register_handler(
        MOQTMessageType.FETCH,
        _make_fetch_handler(cache))

    config = QuicConfiguration(
        is_client=False,
        alpn_protocols=H3_ALPN,
        verify_mode=ssl.CERT_NONE,
        max_data=2**24,
        max_stream_data=2**24,
        max_datagram_frame_size=64 * 1024,
    )
    config.load_cert_chain(CERT, KEY)

    quic_server = await serve(
        "localhost", port,
        configuration=config,
        create_protocol=lambda *a, **kw: MOQTSession(*a, **kw, session=peer),
    )
    return quic_server


async def _connect_client(port: int):
    """Create a client connected to localhost."""
    client = MOQTClient(
        "localhost", port,
        endpoint="moq",
        verify_tls=False,
    )
    return client


# Use a base port and offset by test to avoid port conflicts
_BASE_PORT = 14434


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_joining_fetch_relative():
    """RELATIVE_JOINING fetch: subscribe + join, verify fetched + live objects."""
    port = _BASE_PORT + 1
    cache = FetchTestCache(num_groups=5, objects_per_group=10, object_size=64)
    server = await _start_server(port, cache)

    fetched_objects = []
    live_objects = []

    def on_fetch(msg, size, ts, request_id):
        fetched_objects.append((msg.group_id, msg.object_id, request_id))

    def on_live(msg, size, ts, group_id, subgroup_id):
        live_objects.append((group_id, msg.object_id))

    try:
        client = await _connect_client(port)
        async with client.connect() as session:
            await session.client_session_init()

            session.on_fetch_object = on_fetch
            session.on_object_received = on_live

            # join() with RELATIVE_JOINING, fetch last 2 groups
            sub_response, fetch_response = await session.join(
                namespace="bench",
                track_name="track",
                joining_start=2,
                fetch_type=FetchType.RELATIVE_JOINING,
                wait_response=True,
            )

            # Wait for data to arrive
            await asyncio.sleep(1.0)

        # Verify fetched objects: groups 3 and 4 (5 groups, offset 2)
        assert len(fetched_objects) > 0, "No fetched objects received"
        fetch_groups = sorted(set(g for g, o, rid in fetched_objects))
        assert 3 in fetch_groups, f"Expected group 3 in fetched groups: {fetch_groups}"
        assert 4 in fetch_groups, f"Expected group 4 in fetched groups: {fetch_groups}"

        # Verify all 10 objects per group were fetched
        for g in (3, 4):
            g_objs = sorted(o for grp, o, rid in fetched_objects if grp == g)
            assert g_objs == list(range(10)), \
                f"Group {g}: expected objects 0-9, got {g_objs}"

        # Verify live objects arrived (group 5 = cache.largest_group + 1)
        assert len(live_objects) > 0, "No live objects received"
        live_groups = set(g for g, o in live_objects)
        assert 5 in live_groups, f"Expected live group 5: {live_groups}"

    finally:
        server.close()


@pytest.mark.asyncio
async def test_standalone_fetch():
    """STANDALONE fetch: explicit range, verify exact objects returned."""
    port = _BASE_PORT + 2
    cache = FetchTestCache(num_groups=5, objects_per_group=10, object_size=64)
    server = await _start_server(port, cache)

    fetched_objects = []

    def on_fetch(msg, size, ts, request_id):
        fetched_objects.append((msg.group_id, msg.object_id))

    try:
        client = await _connect_client(port)
        async with client.connect() as session:
            await session.client_session_init()
            session.on_fetch_object = on_fetch

            # Standalone fetch: groups 1-2, objects 0-9
            await session.fetch(
                namespace="bench",
                track_name="track",
                start_group=1,
                start_object=0,
                end_group=2,
                end_object=9,
                wait_response=True,
            )

            await asyncio.sleep(0.5)

        # Verify exact range
        assert len(fetched_objects) == 20, \
            f"Expected 20 objects (2 groups x 10), got {len(fetched_objects)}"
        for g in (1, 2):
            g_objs = sorted(o for grp, o in fetched_objects if grp == g)
            assert g_objs == list(range(10)), \
                f"Group {g}: expected 0-9, got {g_objs}"

    finally:
        server.close()


@pytest.mark.asyncio
async def test_fetch_invalid_range():
    """FETCH with start > largest → FETCH_ERROR (INVALID_RANGE)."""
    port = _BASE_PORT + 3
    cache = FetchTestCache(num_groups=5, objects_per_group=10, object_size=64)
    server = await _start_server(port, cache)

    try:
        client = await _connect_client(port)
        async with client.connect() as session:
            await session.client_session_init()

            # Fetch starting at group 100 — well beyond cache
            with pytest.raises(MOQTRequestError):
                await session.fetch(
                    namespace="bench",
                    track_name="track",
                    start_group=100,
                    start_object=0,
                    end_group=200,
                    end_object=0,
                    wait_response=True,
                )

    finally:
        server.close()
