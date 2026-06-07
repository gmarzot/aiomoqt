# aiomoqt - Media over QUIC Transport (MoQT)

`aiomoqt` is an implementation of [MoQT](https://moq-wg.github.io/moq-transport/draft-ietf-moq-transport.html) for `asyncio`, layered on [aiopquic](https://pypi.org/project/aiopquic/).

## Overview

`aiomoqt` implements the MoQT protocol with **dual draft-14 / draft-16 support**, both publish and subscribe roles, H3/WebTransport and raw QUIC transports, and ALPN-based draft negotiation. The architecture extends `aiopquic.asyncio.QuicConnectionProtocol`. The package ships publisher / subscriber example clients, a benchmark suite, a relay version probe, and a [moq-interop-runner](https://github.com/englishm/moq-interop-runner)-compatible test client.

### Features

- H3/WebTransport and raw QUIC transports
- Draft-14 / draft-16 ALPN negotiation (`moq-00` / `moqt-16`)
- Draft-16 wire format: delta-encoded param keys, track extensions, unified request/response
- SubgroupHeader / ObjectDatagram flag encoding, delta-encoded object IDs
- Version-independent control: `MOQTRequestError` exception across drafts
- Async context manager for session lifecycle
- Sync / async response handling on every control message via `wait_response`
- High-level publisher: [`PublishedTrack`](aiomoqt/track.py) — stream setup, subgroup writing, pacing
- High-level subscriber: [`SubscribedTrack`](aiomoqt/track.py) — object reassembly, FETCH / JOIN handling
- Pluggable message handlers via `register_handler()`
- Data publishing via SubgroupHeader streams or ObjectDatagrams
- Data reception via `on_object_received` callback
- Low-level message serialization / deserialization for custom protocol work

## Installation

Pure Python, requires Python 3.12+ (tested on 3.12, 3.13, 3.14):

```bash
uv pip install aiomoqt    # or: pip install aiomoqt
```

`aiopquic` (the QUIC transport) installs as a binary wheel automatically. Only Linux (glibc 2.34+, RHEL 9 / Ubuntu 22.04+) and macOS arm64 have prebuilt wheels; other systems pull `aiopquic` via sdist and need a C build toolchain — see [aiopquic install notes](https://github.com/gmarzot/aiopquic#installation).

A `./bootstrap_python.sh` script is provided for a uv-managed `.venv` if you want a clean dev environment.

### Reporting issues

Include the full version report in any issue. It captures aiomoqt, aiopquic, and the picoquic + picotls submodule SHAs aiopquic was built from — useful for diagnosing version-pair mismatches across the stack:

```bash
python -m aiomoqt.versions   # or the console script: aiomoqt-versions
```

Sample output:

```
aiomoqt  0.9.5.dev7+g69f55724e.d20260520
         /path/to/aiomoqt
aiopquic 0.3.5.dev4+g2ffe8947d.d20260522
         /path/to/aiopquic
picoquic 2b1e14d5a46532eadf691edef5bd747da6de6557
picotls  f350eab60742138ac62b42ee444adf04c7898b0d
```

## Quick Start

### Verify install + relay liveness

Confirm the stack is wired up before writing any code:

```bash
# Versions of aiomoqt + aiopquic + picoquic + picotls
python -m aiomoqt.versions   # or: aiomoqt-versions

# Liveness + supported drafts against a single relay (one line per probed transport).
# Exit 0 if the relay answered SERVER_SETUP for at least one draft.
python -m aiomoqt.examples.relay_probe --url moqt://moqx-main.ci.openmoq.org:4433
# → moqx-main.ci.openmoq.org:4433  quic   draft-14,draft-16  ✓ (405ms)

python -m aiomoqt.examples.relay_probe --url https://moqx-main.ci.openmoq.org:4433/moq-relay
# → moqx-main.ci.openmoq.org:4433  h3/wt  draft-14,draft-16  ✓ (315ms)
```

### Subscriber

```python
import asyncio
from aiomoqt.client import MOQTClient

def on_object(msg, size, recv_time_ms, group_id=None, subgroup_id=None):
    print(f"g={group_id} obj={msg.object_id} {size}B payload={msg.payload}")

async def main():
    client = MOQTClient('relay.example.com', 443, path='moq',
                         use_quic=True, draft_version=16)
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
    client = MOQTClient('relay.example.com', 443, path='moq',
                         use_quic=True, draft_version=16)
    client.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
    async with client.connect() as session:
        await session.client_session_init()
        await session.publish_namespace('ns', wait_response=True)
        await session.async_closed()  # serve until closed

async def on_subscribe(session, msg):
    """Called when a subscriber requests a track."""
    ok = session.subscribe_ok(request_msg=msg)
    stream_id = session.open_uni_stream()
    hdr = SubgroupHeader(track_alias=ok.track_alias, group_id=0, subgroup_id=0, publisher_priority=0)
    session.stream_write(stream_id, hdr.serialize().data)
    session.stream_write(stream_id, hdr.next_object(payload=b"hello").data)
    session.transmit()

asyncio.run(main())
```

The `on_subscribe` handler above — stream setup, header serialization, object writing — is wrapped by the higher-level `PublishedTrack` / `SubscribedTrack` classes in `aiomoqt.track`. See `*_bench.py` examples for typical usage.

### Control Message API

Control messages support both sync and async patterns via `wait_response`:

```python
# Blocking — awaits and returns the response message
resp = await session.subscribe('ns', 'track', wait_response=True)

# Non-blocking — returns request, response arrives via handler
req = await session.subscribe('ns', 'track')
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

Common options: `--namespace`, `--trackname`, `--path`, `--debug`, `--keylogfile`

### Benchmarks

Bench tools take a positional relay URL:

```
moqt://host[:port]              Raw QUIC (default port 443)
https://host[:port]/[endpoint]  H3/WebTransport (default port 443)
```

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
python -m aiomoqt.examples.moq_interop_client -r "moqt://relay.ex.com:4433"

# All tests (draft-16)
python -m aiomoqt.examples.moq_interop_client -r "moqt://relay.ex.com:4433" --draft 16

# Single test case
python -m aiomoqt.examples.moq_interop_client -r "moqt://relay.ex.com:4433" -t subscribe-error

# List test cases
python -m aiomoqt.examples.moq_interop_client -l
```

### Relay Probe

Batch liveness + draft-version check. Reads a relay list, does a
real CLIENT_SETUP / SERVER_SETUP handshake per (endpoint × draft) —
no bare-ALPN tricks — and writes a JSON status report.

Accepts CLI flags, environment variables, or both (CLI overrides env):

```bash
# CLI form (typical interactive use)
python -m aiomoqt.examples.relay_probe -f relays.json -o status.json --once

# Env form (typical container/daemon deployment)
RELAYS_FILE=relays.json OUTPUT_FILE=status.json PROBE_ONCE=1 \
  python -m aiomoqt.examples.relay_probe

# Long-running monitor (re-probe every --interval seconds)
python -m aiomoqt.examples.relay_probe -f relays.json -o status.json
```

| CLI flag | Env var | Default | Meaning |
|----------|---------|---------|---------|
| `-f / --relays-file` | `RELAYS_FILE` | `/app/relays.json` | input relay list |
| `-o / --output-file` | `OUTPUT_FILE` | `/output/relay-status.json` | status report destination |
| `--timeout` | `PROBE_TIMEOUT` | `8` | per-probe handshake timeout (s) |
| `--interval` | `PROBE_INTERVAL` | `300` | re-probe cadence in monitor mode (s) |
| `--once` | `PROBE_ONCE=1` | unset | probe once and exit |

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
| `adaptive_bench.py` | Ramps rate until buffer growth; loopback (`--mp-loopback` for proc-isolated pub/sub) or relay |
| `server_example.py` | WebTransport server (origin) |
| `relay_probe.py` | Relay version probe (draft-14/16) |
| `moq_interop_client.py` | Interop test client (TAP v14 out; 6 standard + `fetch`/`join`) |

## Interop Test Results

Results against live public relays, as probed by
`tests/release_regression_test.py --test-tier interop` in v0.8.1.
Tests use the 6 [moq-interop-runner](https://github.com/englishm/moq-interop-runner)
cases plus `relay-pub-sub` (3-subscriber multi-sub bench).
Error codes are validated to spec-conformant values
(e.g. `subscribe-error` requires `TRACK_DOES_NOT_EXIST`, not `INTERNAL_ERROR`).

| Relay | Draft | Transport | ctrl-msg | pub-sub |
|-------|-------|-----------|----------|---------|
| OpenMoQ moqx | d14 | QUIC | 6/6 | 3/3 |
| OpenMoQ moqx | d14 | H3/WT | 6/6 | 3/3 |
| OpenMoQ moqx | d16 | QUIC | 6/6 | 3/3 |
| OpenMoQ moqx | d16 | H3/WT | 6/6 | 3/3 |
| Meta moxygen | d14 | QUIC | 6/6 | 3/3 |
| Meta moxygen | d14 | H3/WT | 6/6 | 3/3 |
| Meta moxygen | d16 | QUIC | 6/6 | 3/3 |
| Meta moxygen | d16 | H3/WT | 6/6 | 3/3 |
| Cloudflare moq-rs | d14 | QUIC | 5/6 | 3/3 |
| Cloudflare moq-rs (d16 interop branch) | d16 | QUIC | 6/6 | unverified |
| Red5 Pro | d14 | QUIC | unreachable | unreachable |
| Red5 Pro | d14 | H3/WT | 6/6 | unverified |
| Red5 Pro | d16 | QUIC | unreachable | unreachable |
| Red5 Pro | d16 | H3/WT | 6/6 | unverified |
| Quicr libquicr | d14 | QUIC | 5/6 | 3/3 |
| Quicr libquicr | d14 | H3/WT | 5/6 | 3/3 |
| Quicr libquicr | d16 | QUIC | unverified | unverified |
| Quicr libquicr | d16 | H3/WT | 5/6 | 3/3 |
| Meetecho imquic | d16 | QUIC | 6/6 | unverified |
| Meetecho imquic | d16 | H3/WT | 5/6 | unverified |
| OzU moqtail | d14 | H3/WT | 6/6 | unverified |

- `unverified` — suite did not complete end-to-end.
- `unreachable` — no response to QUIC Initial.
- See [`tests/relays.json`](tests/relays.json) for the full catalog,
  per-endpoint notes, and relays disabled by default.

Test cases: `setup-only`, `announce-only`, `publish-namespace-done`,
`subscribe-error`, `announce-subscribe`, `subscribe-before-announce`,
plus `fetch` and `join` probes (not in default catalog matrix —
most relays do not implement these yet).

## Performance

`aiomoqt` sits on top of [`aiopquic`](https://github.com/gmarzot/aiopquic), which sits on picoquic + the kernel UDP path. Throughput at the aiomoqt layer is bounded by the layer below.

On AMD Ryzen 7 PRO 7840U / WSL2 / Linux 6.6, single-publisher single-subscriber MoQT loopback (raw QUIC, 30s steady-state, in-process publisher and subscriber):

| obj | obj/s | throughput | notes |
|---|---|---|---|
| 8 KiB | 16,084 | 1,055 Mbps | b6 subgroup-churn microbench |

`aiopquic` highlevel throughput on the same hardware sits at ~2.0 Gbps for ≥4 KiB objects (UDP-loopback-bound at QUIC MTU). The aiomoqt layer adds per-object MoQT framer + asyncio orchestration cost; the gap between aiomoqt and aiopquic is the headroom we work on in 0.9.x. See the [aiopquic Performance section](https://github.com/gmarzot/aiopquic#performance) for the layer breakdown including the `sim_link` protocol-only reference.

Numbers vary by platform — kernel UDP loopback rates differ noticeably (Apple M-series macOS sits around 1 Gbps regardless of obj size due to a slower UDP loopback path). Calibrate on your own hardware:

```bash
python -m aiomoqt.examples.adaptive_bench -P 4 \
    --start-mbps 50 --max-mbps 2000 --step-mbps 100 \
    --interval 5 -s 4096 -t 60 --mp-loopback
```

## Development

```bash
git clone https://github.com/gmarzot/aiomoqt.git
cd aiomoqt
python3 -m venv .venv && source .venv/bin/activate
uv pip install -e ".[test]"    # or: pip install -e ".[test]"
pytest aiomoqt/tests/
```

## Known Limitations

- **WebTransport fetch / join routing** -- four `[wt]` test variants of FETCH and JOINING_SUBSCRIBE return empty results when the underlying transport is WebTransport. Raw-QUIC variants of the same tests pass and cover the MoQT-level invariant. Tracked separately; affects WT-only consumers of fetch/join.
- **Subscriber framer desync at high tx rates** -- under sustained tx > ~400 Mbps the subscriber's data-stream parser occasionally rejects a stream with a `framer desync` error. Stream-level reject (not session-fatal); the publisher continues. Pre-existing; surfaced more visibly by `--mp-loopback` headroom. Tracked for 0.9.x.

## TODO

* Diagnose and fix the subscriber framer desync at high tx rates
* Close the aiomoqt-level perf gap to the aiopquic transport ceiling (framer batching)
* Fix WebTransport fetch / join routing (the 4 `[wt]` tests currently skipped)
* Track data modules:
  - File transfer (or [MOQT File Format](https://datatracker.ietf.org/doc/html/draft-jennings-moq-file-00)?)
  - Interactive chat
  - MSF/LOC media packaging
  - CMSF media packaging
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
