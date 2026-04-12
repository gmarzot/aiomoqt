# Red5 Relay Test Plan

Target: `red5-moq-test.marzresearch.net:4433` (raw QUIC)
Published namespace: `live/stream1`
Tracks: `audio0`, `catalog`, `metadata`, `video-sap-timeline`, `video0`
Admin/catalog: `http://150.136.113.27:5080/live/catalog`

## Prerequisites

- Red5 relay running with active publisher on `live/stream1`
- aiomoqt dev branch checked out and venv active

## 1. Version Probe

Determine which MoQT draft versions the relay supports.

```bash
# Probe d14 (moq-00 ALPN)
.venv/bin/python -m aiomoqt.examples.moq_interop_client \
  -r moqt://red5-moq-test.marzresearch.net:4433 --draft 14 -v -t setup-only

# Probe d16 (moqt-16 ALPN)
.venv/bin/python -m aiomoqt.examples.moq_interop_client \
  -r moqt://red5-moq-test.marzresearch.net:4433 --draft 16 -v -t setup-only
```

**Expected:** At least d14 should succeed (setup-only = CLIENT_SETUP/SERVER_SETUP handshake).

## 2. Subscribe Namespace

Subscribe to the `live/stream1` namespace. Red5 doesn't support prefix
matching, so use the exact namespace.

```bash
# d14: subscribe_namespace on control stream
.venv/bin/python -c "
import asyncio
from aiomoqt.client import MOQTClient
from aiomoqt.types import ParamType, MOQTException

async def main():
    client = MOQTClient('red5-moq-test.marzresearch.net', 4433,
                        endpoint='', use_quic=True, draft_version=14)
    async with client.connect() as session:
        await session.client_session_init()
        print('Session established')

        response = await session.subscribe_namespace(
            namespace_prefix='live/stream1',
            parameters={},
            wait_response=True,
        )
        print(f'subscribe_namespace response: {response}')

        # Wait for track announcements
        import asyncio as aio
        try:
            async with aio.timeout(5):
                while True:
                    pub = await session._publish_announcements.get()
                    ns = '/'.join(p.decode() if isinstance(p,bytes) else p
                                 for p in pub.track_namespace)
                    tn = pub.track_name.decode() if isinstance(pub.track_name, bytes) else pub.track_name
                    print(f'  Track announced: {ns}/{tn}')
        except (aio.TimeoutError, Exception) as e:
            print(f'Done (or timeout): {e}')

asyncio.run(main())
"
```

**Expected:** Should receive track announcements for audio0, catalog, metadata, video-sap-timeline, video0.

## 3. Subscribe to Catalog Track

Fetch the catalog to understand the media format.

```bash
# Subscribe to the catalog track
.venv/bin/python -c "
import asyncio
from aiomoqt.client import MOQTClient
from aiomoqt.types import ParamType, FilterType

async def main():
    client = MOQTClient('red5-moq-test.marzresearch.net', 4433,
                        endpoint='', use_quic=True, draft_version=14)
    async with client.connect() as session:
        await session.client_session_init()
        print('Session established')

        objects = []
        def on_obj(msg, size, ts, gid, sid):
            payload = msg.payload
            print(f'  Catalog object g={gid} s={sid} oid={msg.object_id} '
                  f'size={size}B')
            if payload:
                try:
                    text = payload.decode('utf-8')
                    print(text[:2000])
                except:
                    print(f'  (binary, {len(payload)} bytes)')
            objects.append(msg)

        session.on_object_received = on_obj

        await session.subscribe(
            namespace='live/stream1',
            track_name='catalog',
            forward=1,
            filter_type=FilterType.LATEST_OBJECT,
            parameters={},
            wait_response=True,
        )
        print('Subscribed to catalog')

        await asyncio.sleep(3)
        print(f'Received {len(objects)} catalog object(s)')

asyncio.run(main())
"
```

**Expected:** Should receive catalog JSON describing the media tracks (codec, resolution, packaging format).

## 4. Fetch Catalog (Standalone FETCH)

Test FETCH support — request the catalog via standalone fetch instead of subscribe.

```bash
.venv/bin/python -c "
import asyncio
from aiomoqt.client import MOQTClient
from aiomoqt.types import MOQTRequestError

async def main():
    client = MOQTClient('red5-moq-test.marzresearch.net', 4433,
                        endpoint='', use_quic=True, draft_version=14)
    async with client.connect() as session:
        await session.client_session_init()
        print('Session established')

        fetched = []
        def on_fetch(msg, size, ts, rid):
            print(f'  Fetched g={msg.group_id} oid={msg.object_id} '
                  f'size={size}B rid={rid}')
            if msg.payload:
                try:
                    print(msg.payload.decode('utf-8')[:500])
                except:
                    print(f'  (binary, {len(msg.payload)} bytes)')
            fetched.append(msg)

        session.on_fetch_object = on_fetch

        try:
            resp = await session.fetch(
                namespace='live/stream1',
                track_name='catalog',
                start_group=0, start_object=0,
                end_group=0, end_object=0,
                wait_response=True,
            )
            print(f'FETCH response: {resp}')
            await asyncio.sleep(2)
            print(f'Fetched {len(fetched)} object(s)')
        except MOQTRequestError as e:
            print(f'FETCH error: {e} (relay may not support FETCH)')

asyncio.run(main())
"
```

**Expected:** Either receives catalog data via fetch, or FETCH_ERROR if Red5 doesn't support FETCH yet.

## 5. Subscribe to Video Track

Subscribe to the video track and receive live objects.

```bash
# Short subscription to video0 — receive and count objects for 10s
.venv/bin/python -m aiomoqt.examples.sub_bench \
  moqt://red5-moq-test.marzresearch.net:4433 \
  -n "live/stream1" --trackname video0 \
  -t 10 -i 5 --draft 14
```

**Expected:** Receive video objects at the published frame rate with latency stats.

## 6. Fan-out Benchmark

Multi-subscriber fan-out test through the Red5 relay.

```bash
# Single subscriber baseline (30s)
.venv/bin/python -m aiomoqt.examples.sub_bench \
  moqt://red5-moq-test.marzresearch.net:4433 \
  -n "live/stream1" --trackname video0 \
  -t 30 -i 10 --draft 14

# Multi-subscriber fan-out (if multi_sub_bench supports relay URL)
.venv/bin/python -m aiomoqt.examples.multi_sub_bench \
  moqt://red5-moq-test.marzresearch.net:4433 \
  -n "live/stream1" --trackname video0 \
  -N 10 -t 30 --draft 14
```

**Expected:** Sustained video delivery with acceptable latency across multiple subscribers.

## 7. Compliance/Conformance Checks

Protocol compliance tests against the published tracks.

```bash
# Run standard interop suite
.venv/bin/python -m aiomoqt.examples.moq_interop_client \
  -r moqt://red5-moq-test.marzresearch.net:4433 --draft 14 -v

# Test subscribe to non-existent track (should get SUBSCRIBE_ERROR)
.venv/bin/python -c "
import asyncio
from aiomoqt.client import MOQTClient
from aiomoqt.types import MOQTRequestError

async def main():
    client = MOQTClient('red5-moq-test.marzresearch.net', 4433,
                        endpoint='', use_quic=True, draft_version=14)
    async with client.connect() as session:
        await session.client_session_init()
        try:
            await session.subscribe(
                namespace='live/stream1',
                track_name='nonexistent-track',
                forward=1,
                parameters={},
                wait_response=True,
            )
            print('UNEXPECTED: subscribe to nonexistent track succeeded')
        except MOQTRequestError as e:
            print(f'EXPECTED: subscribe error: {e}')

asyncio.run(main())
"

# Test subscribe_namespace with wrong prefix
.venv/bin/python -c "
import asyncio
from aiomoqt.client import MOQTClient
from aiomoqt.types import MOQTRequestError
from aiomoqt.messages import SubscribeNamespaceError

async def main():
    client = MOQTClient('red5-moq-test.marzresearch.net', 4433,
                        endpoint='', use_quic=True, draft_version=14)
    async with client.connect() as session:
        await session.client_session_init()
        try:
            resp = await session.subscribe_namespace(
                namespace_prefix='nonexistent/namespace',
                parameters={},
                wait_response=True,
            )
            if isinstance(resp, SubscribeNamespaceError):
                print(f'EXPECTED: namespace error: {resp}')
            else:
                print(f'Response: {resp}')
        except MOQTRequestError as e:
            print(f'EXPECTED: namespace error: {e}')
        except Exception as e:
            print(f'Error: {type(e).__name__}: {e}')

asyncio.run(main())
"
```

## Results Checklist

| Test | Result | Notes |
|------|--------|-------|
| 1. Version probe d14 | | |
| 1. Version probe d16 | | |
| 2. Subscribe namespace | | |
| 3. Subscribe catalog | | |
| 4. Fetch catalog | | |
| 5. Subscribe video (10s) | | |
| 6. Fan-out baseline (30s) | | |
| 6. Fan-out x10 (30s) | | |
| 7. Interop suite | | |
| 7. Nonexistent track | | |
| 7. Wrong namespace prefix | | |
