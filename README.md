 aiomoqt - Media over QUIC Transport (MoQT)

`aiomoqt` is an implementation of the MoQT protocol, based on `asyncio` and `qh3`.

## Overview

This package implements the [MoQT Specification](https://moq-wg.github.io/moq-transport/draft-ietf-moq-transport.html)
with **dual draft-14 and draft-16 support**. It is designed for general use as
an MoQT client and server library, supporting both 'publish' and 'subscribe' roles.

The architecture follows the [asyncio.Protocol](https://docs.python.org/3/library/asyncio-protocol.html)
design pattern, extending the [qh3 QuicConnectionProtocol](https://pypi.org/project/qh3/).
It supports both H3/WebTransport and raw QUIC transports, with ALPN-based
draft version negotiation, and has been interop tested against 6 relay
implementations across draft-14 and draft-16.

The package includes publisher and subscriber example clients, a benchmark
suite for throughput/latency measurement, a relay version probe tool, and
an interop test client compatible with the
[moq-interop-runner](https://github.com/englishm/moq-interop-runner) framework.

### Features

- H3/WebTransport and raw QUIC transports
- Async context manager for session lifecycle
- High-level control message API with sync/async response handling
- Low-level message serialization/deserialization
- Version-independent API: `MOQTRequestError` exception across drafts
- Pluggable message handlers via `register_handler()`
- Data publishing via SubgroupHeader streams or ObjectDatagrams
- Data reception via `on_object_received` callback
- **Draft-14/16:** ALPN negotiation (`moq-00` / `moqt-16`)
- **Draft-16:** delta-encoded parameter keys, track extensions,
  unified REQUEST_OK/REQUEST_ERROR
- **Wire format:** SubgroupHeader/ObjectDatagram flag encoding,
  delta-encoded object IDs

## Installation

Requires Python 3.12+ (tested on 3.12, 3.13, and 3.14).

```bash
pip install aiomoqt
# or
uv pip install aiomoqt
```

## Quick Start

### Subscriber

```python
import asyncio
from aiomoqt.client import MOQTClient

def on_object(msg, size, recv_time_ms, group_id=None, subgroup_id=None):
    print(f"g={group_id} obj={msg.object_id} {size}B payload={msg.payload}")

async def main():
    client = MOQTClient(
        'relay.example.com', 443, endpoint='moq',
        use_quic=True, draft_version=16, debug=True,
    )
    async with client.connect() as session:
        await session.client_session_init()
        session.on_object_received = on_object
        await session.subscribe('ns', 'track', wait_response=True)
        await session.async_closed()

asyncio.run(main())
```

### Publisher

```python
import asyncio
from aiomoqt.client import MOQTClient
from aiomoqt.types import MOQTMessageType
from aiomoqt.messages import SubgroupHeader

async def main():
    client = MOQTClient(
        'relay.example.com', 443,
        endpoint='moq', use_quic=True, draft_version=16,
    )
    client.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)

    async with client.connect() as session:
        await session.client_session_init()
        await session.publish_namespace('ns', wait_response=True)
        await session.async_closed()  # serve until closed

async def on_subscribe(session, msg):
    """Called when a subscriber requests a track."""
    ok = session.subscribe_ok(request_msg=msg)
    stream_id = session.open_uni_stream()
    hdr = SubgroupHeader(
        track_alias=ok.track_alias,
        group_id=0, subgroup_id=0, publisher_priority=0,
    )
    session.stream_write(stream_id, hdr.serialize().data)
    session.stream_write(stream_id, hdr.next_object(payload=b"hello").data)
    session.transmit()

asyncio.run(main())
```

The `on_subscribe` handler above — stream setup, header serialization,
object writing — is wrapped by the higher-level `PublishedTrack` /
`SubscribedTrack` classes in `aiomoqt.track`. See the `*_bench.py`
examples for typical usage.

### Control Message API

Control messages support both sync and async patterns via `wait_response`:

```python
# Blocking — awaits and returns the response message
resp = await session.subscribe('ns', 'track', wait_response=True)

# Non-blocking — returns request, response arrives via handler
req = await session.subscribe('ns', 'track')
```

### Relay URL Formats

Examples and benchmarks accept relay URLs in several forms:

```
moqt://host:port            Raw QUIC (default port 443)
https://host:port/endpoint  H3/WebTransport (default port 443)
host:port                   H3/WebTransport
host                        H3/WebTransport, port 443, endpoint /moq
```

## Examples

### Publisher / Subscriber

```bash
# Publish (SubgroupHeader streams)
python -m aiomoqt.examples.pub_example --host relay.ex.com --use-quic

# Publish (ObjectDatagrams)
python -m aiomoqt.examples.pub_example --host relay.ex.com --use-quic --datagram

# Subscribe
python -m aiomoqt.examples.sub_example --host relay.ex.com --use-quic

# Subscribe + FETCH (join mid-stream)
python -m aiomoqt.examples.join_example --host relay.ex.com --use-quic
```

Common options: `--namespace`, `--trackname`, `--endpoint`, `--debug`, `--keylogfile`

### Benchmarks

```bash
# Publisher — configurable size, rate, parallelism
python -m aiomoqt.examples.pub_bench moqt://relay.ex.com -s 4096 -P 4 -r 120 -t 60

# Subscriber — latency/jitter/loss stats (omit -t; subscriber exits on PUBLISH_DONE)
python -m aiomoqt.examples.sub_bench moqt://relay.ex.com

# Combined pub/sub in one process
python -m aiomoqt.examples.relay_bench moqt://relay.ex.com -s 1024 -g 10000 -t 30

# 1 publisher, N subscribers in one process (fanout capacity)
python -m aiomoqt.examples.multi_sub_bench moqt://relay.ex.com -n 100 --video 720p -t 60

# Local loopback (no relay needed)
python -m aiomoqt.examples.loopback_bench -s 4096 -P 4 -t 20
```

Publisher flow selection (for relays that require a specific message pattern):

```bash
--pub-ns     # send only PUBLISH_NAMESPACE (wait for SUBSCRIBE)
--pub-both   # send PUBLISH_NAMESPACE and PUBLISH (required by Cloudflare d14)
             # default: send only PUBLISH
```

| Option | Description | Default |
|--------|-------------|---------|
| `-s, --object-size` | Payload size (bytes) | 1024 |
| `-g, --group-size` | Objects per group | 10000 |
| `-P, --streams` | Parallel subgroup streams | 1 |
| `-r, --rate` | Objects/sec per stream (0=max) | 0 |
| `-t, --duration` | Duration (seconds) | 30 |
| `-i, --interval` | Report interval (seconds) | 5.0 |
| `-D, --datagram` | Use datagrams instead of streams | off |
| `-Q, --force-quic` | Force raw QUIC for https:// URLs | off |

### Interop Testing

```bash
# All tests (draft-14, auto-detected)
python -m aiomoqt.examples.moq_interop_client -r "moqt://moqx-000.ci.openmoq.org:4433"

# All tests (draft-16)
python -m aiomoqt.examples.moq_interop_client -r "moqt://moqx-000.ci.openmoq.org:4433" --draft 16

# Single test case
python -m aiomoqt.examples.moq_interop_client -r "moqt://relay" -t subscribe-error

# List test cases
python -m aiomoqt.examples.moq_interop_client -l
```

### Relay Probe

```bash
python -m aiomoqt.examples.relay_probe
```

Environment variables: `RELAYS_FILE`, `OUTPUT_FILE`,
`PROBE_TIMEOUT`, `PROBE_INTERVAL`, `PROBE_ONCE`

### WebTransport Server

```bash
python -m aiomoqt.examples.server_example \
    --certificate cert.pem --private-key key.pem --port 443
```

### Example Reference

| Example | Description |
|---------|-------------|
| `pub_example.py` | Publisher — SubgroupHeader streams or ObjectDatagrams |
| `sub_example.py` | Subscriber — receives data from a relay |
| `join_example.py` | SUBSCRIBE + FETCH (join mid-stream) |
| `pub_bench.py` | Publisher benchmark, configurable parameters |
| `sub_bench.py` | Subscriber with latency/jitter/loss stats |
| `relay_bench.py` | Combined pub/sub in one process |
| `multi_sub_bench.py` | 1 publisher, N subscribers in one process |
| `loopback_bench.py` | Local loopback (no relay) |
| `server_example.py` | WebTransport server (origin) |
| `relay_probe.py` | Relay version probe (draft-14/16) |
| `moq_interop_client.py` | Interop test client (6 test cases, TAP14) |

## Interop Test Results

All 6 [moq-interop-runner](https://github.com/englishm/moq-interop-runner)
test cases pass against multiple relays on draft-14 and draft-16:

| Relay | Draft | Transport | Tests | Result |
|-------|-------|-----------|-------|--------|
| OpenMoQ moqx | draft-16 | Raw QUIC | 6/6 | PASS |
| OpenMoQ moqx | draft-14 | Raw QUIC | 6/6 | PASS |
| Meta moxygen | draft-16 | Raw QUIC | 6/6 | PASS |
| Meta moxygen | draft-14 | Raw QUIC | 6/6 | PASS |
| Cloudflare moq-rs | draft-14 | Raw QUIC | 6/6 | PASS |
| Red5 Pro | draft-14 | Raw QUIC | 6/6 | PASS |
| Red5 Pro | draft-14 | WebTransport | 6/6 | PASS |
| Quicr libquicr | draft-14 | Raw QUIC | 5/6 | PASS |

Test cases: `setup-only`, `announce-only`, `publish-namespace-done`,
`subscribe-error`, `announce-subscribe`, `subscribe-before-announce`

## Development

```bash
git clone https://github.com/gmarzot/aiomoqt.git
cd aiomoqt
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
pytest aiomoqt/tests/
```

Optionally, `./bootstrap_python.sh` sets up a full uv-managed environment
with a specific Python version and Cython.

## TODO

* Transition from qh3 to aiopquic transport for performance
* Track data modules:
  - File transfer (or [MOQT File Format](https://datatracker.ietf.org/doc/html/draft-jennings-moq-file-00)?)
  - Interactive chat
  - MSF/LOC media packaging
  - CMSF media packaging
* Dockerize interop test client for moq-interop-runner registration
* Simple relay implementation

## Contributing

Contributions are welcome! Please fork the repository, create a branch,
and submit a pull request. For major changes, open an issue first.

## Resources

- [MoQT Specification](https://moq-wg.github.io/moq-transport/draft-ietf-moq-transport.html)
- [Media Over QUIC Working Group](https://datatracker.ietf.org/wg/moq/about/)
- [MoQ Interop Runner](https://github.com/englishm/moq-interop-runner)
- [`aiomoqt` GitHub Repository](https://github.com/gmarzot/aiomoqt)

---

## Author

Giovanni Marzot — [gmarzot@marzresearch.net](mailto:gmarzot@marzresearch.net) | [moqarean.marzresearch.net](https://moqarean.marzresearch.net)

## Acknowledgements

This project takes inspiration from, and has benefited from the great work
done by the [OpenMoQ/moxygen](https://github.com/openmoq/moxygen) team,
and the continued efforts of the MOQ IETF WG.
