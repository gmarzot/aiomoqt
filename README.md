# aiomoqt - Media over QUIC Transport (MoQT)

`aiomoqt` is an implementation of [MoQT](https://moq-wg.github.io/moq-transport/draft-ietf-moq-transport.html) for `asyncio`, layered on [aiopquic](https://pypi.org/project/aiopquic/).

## Overview

`aiomoqt` implements the MoQT protocol with **draft-14 / draft-16 / draft-18 (beta) support**, both publish and subscribe roles, H3/WebTransport and raw QUIC transports, and ALPN-based draft negotiation. The architecture extends `aiopquic.asyncio.QuicConnectionProtocol`. The package ships publisher / subscriber example clients, a benchmark suite, a relay version probe, and a [moq-interop-runner](https://github.com/englishm/moq-interop-runner)-compatible test client.

### Features

- H3/WebTransport and raw QUIC transports
- Draft-14 / draft-16 / draft-18 negotiation (ALPN `moq-00` / `moqt-16` / `moqt-18`, plus WT-Protocol over H3)
- Draft-16 wire format: delta-encoded param keys, track extensions, unified request/response
- Draft-18 (beta): vi64 varints, uni-stream control pair, per-request bidi streams, Request-ID-less replies — over both raw QUIC and WebTransport
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

Pure Python, requires Python 3.12+ (tested on 3.12, 3.13, 3.14). For a clean, uv-managed `.venv`, run `./bootstrap_python.sh`; otherwise install into your current environment:

```bash
uv pip install aiomoqt    # or: pip install aiomoqt
```

`aiopquic` (the QUIC transport) installs as a binary wheel automatically. Only Linux (glibc 2.34+, RHEL 9 / Ubuntu 22.04+) and macOS arm64 have prebuilt wheels; other systems pull `aiopquic` via sdist and need a C build toolchain — see [aiopquic install notes](https://github.com/gmarzot/aiopquic#installation).

### Reporting issues

Include the full version report in any issue. It captures aiomoqt, aiopquic, and the picoquic + picotls submodule SHAs aiopquic was built from — useful for diagnosing version-pair mismatches across the stack:

```bash
python -m aiomoqt.versions   # or the console script: aiomoqt-versions
```

Sample output:

```
aiomoqt:   0.9.7.dev7+g0fa185338 (~/src/aiomoqt/aiomoqt) [2026-06-11 11:57]
aiopquic:  0.3.7.dev12+g6eef9caf6 (~/src/aiopquic/src/aiopquic) [2026-06-11 11:58]
  - picoquic:  1.1.49.2 (d6c5653d) [2026-06-05]
  - picotls:   master (bfa67875) [2026-04-20]
```

## Quick Start

### Verify install + relay liveness

Confirm the stack and reach a relay before writing any code (probe exits 0 if any draft handshakes):

```bash
python -m aiomoqt.versions

python -m aiomoqt.examples.relay_probe --url moqt://moqx-main.ci.openmoq.org:4433
moqt://moqx-main.ci.openmoq.org:4433              QUIC   ✓  draft-14,draft-16,draft-18  (540ms)

python -m aiomoqt.examples.relay_probe --url https://moqx-main.ci.openmoq.org:4433/moq-relay
https://moqx-main.ci.openmoq.org:4433/moq-relay   H3/WT  ✓  draft-14,draft-16,draft-18  (435ms)
```

### Subscriber

```python
import asyncio
from aiomoqt.client import MOQTClient

def on_object(msg, size, recv_time_ms, group_id=None, subgroup_id=None):
    print(f"g={group_id} obj={msg.object_id} {size}B payload={msg.payload}")

async def main():
    client = MOQTClient('relay.example.com', 443, path='moq',
                         use_quic=True, supported_drafts=16)
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
                         use_quic=True, supported_drafts=16)
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

### Auth

`AUTH_TOKEN` rides the SETUP handshake (session-level) and any request message (namespace- or track-level). Values are arbitrary bytes — the codec wraps them in the spec Token structure. Read a peer's token back from the message's `parameters`.

```python
from aiomoqt.types import SetupParamType, ParamType

# session auth — on the SETUP handshake
await session.client_session_init(parameters={SetupParamType.AUTH_TOKEN: b"session-tok"})

# namespace auth — on PUBLISH_NAMESPACE (or SUBSCRIBE_NAMESPACE)
await session.publish_namespace('ns', parameters={ParamType.AUTH_TOKEN: b"ns-tok"}, wait_response=True)

# track auth — on SUBSCRIBE (also FETCH / PUBLISH)
ok = await session.subscribe('ns', 'track', parameters={ParamType.AUTH_TOKEN: b"track-tok"}, wait_response=True)
print(ok.parameters.get(ParamType.AUTH_TOKEN))   # token echoed by the peer, if any
```

## Examples

### Publisher / Subscriber

```bash
# Publish (SubgroupHeader streams)
python -m aiomoqt.examples.pub_example -h relay.ex.com -q

# Publish (ObjectDatagrams)
python -m aiomoqt.examples.pub_example -h relay.ex.com -q --datagram

# Subscribe
python -m aiomoqt.examples.sub_example -h relay.ex.com -q

# Subscribe + FETCH (join mid-stream)
python -m aiomoqt.examples.join_example -h relay.ex.com -q
```

Common options: `--namespace`, `--trackname`, `--path`, `--debug`, `--keylogfile`. Every tool prints its full option set with `-?` / `--help` (note: `-h` is `--host`, not help).

### Benchmarks

Bench tools take a positional relay URL — `moqt://host[:port]` for raw QUIC, `https://host[:port]/[endpoint]` for H3/WebTransport — except `loopback_bench`, which needs no relay at all:

```bash
# Local loopback (canonical stack benchmark, no relay)
python -m aiomoqt.examples.loopback_bench -s 4096 -P 4 -t 20

# Publisher / subscriber through a relay
python -m aiomoqt.examples.pub_bench moqt://relay.ex.com -s 4096 -P 4 -r 120 -t 60
python -m aiomoqt.examples.sub_bench moqt://relay.ex.com
```

The full tool matrix (two-process, fanout, adaptive ramp), all options, latency methodology (paced vs unpaced, TX budgets), and observed numbers live in [PERFORMANCE.md](PERFORMANCE.md).

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

Batch liveness + draft-version check: reads a relay list, does a real CLIENT_SETUP / SERVER_SETUP handshake per (endpoint × draft), and writes a JSON status report. Accepts CLI flags, environment variables, or both (CLI overrides env). A single endpoint can also be probed inline with `--url` (see Quick Start above).

```bash
# Probe a relay list once and write a status report (CLI form)
python -m aiomoqt.examples.relay_probe -f relays.json -o status.json

# Same, env form (typical container/daemon deployment)
RELAYS_FILE=relays.json OUTPUT_FILE=status.json python -m aiomoqt.examples.relay_probe

# Long-running monitor: re-probe every 300s
python -m aiomoqt.examples.relay_probe -f relays.json -o status.json --interval 300
```

| CLI flag | Env var | Default | Meaning |
|----------|---------|---------|---------|
| `-f / --relays-file` | `RELAYS_FILE` | `/app/relays.json` | input relay list |
| `-o / --output-file` | `OUTPUT_FILE` | `/output/relay-status.json` | status report destination |
| `--timeout` | `PROBE_TIMEOUT` | `8` | per-probe handshake timeout (s) |
| `--interval` | `PROBE_INTERVAL` | `0` | re-probe cadence (s); `0` probes once and exits |
| `--draft` | — | all | draft(s) to probe: `--draft 18` or `--draft 18,16` (add `--offer` to offer the list in one session) |

### WebTransport Server

```bash
python -m aiomoqt.examples.server_example --certificate cert.pem --private-key key.pem --port 443
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
| `relay_probe.py` | Relay version probe (draft-14/16/18) |
| `moq_interop_client.py` | Interop test client (TAP v14 out; 6 standard + `fetch`/`join`) |
| `moq_interop_relay.py` | Minimal interop relay (control plane; experimental) |

## Interop

Validated against live public relays — OpenMoQ moqx, Meta moxygen, Cloudflare moq-rs, Quicr libquicr, Meetecho imquic, OzU moqtail, Nokia — across draft-14/draft-16/draft-18 and both transports, using the [moq-interop-runner](https://github.com/englishm/moq-interop-runner) cases plus a multi-subscriber pub-sub bench. The full point-in-time matrix lives in [PERFORMANCE.md](PERFORMANCE.md#interop-matrix-point-in-time); the relay catalog with per-endpoint notes is [`tests/relays.json`](tests/relays.json). Red5 Pro was interop-tested in earlier cycles and is currently untested/unverified.

## Performance

`aiomoqt` sits on [`aiopquic`](https://github.com/gmarzot/aiopquic), which sits on picoquic + the kernel UDP path; throughput at this layer is bounded by the layers below. Observed on commodity hardware over loopback: multi-Gbps sustained throughput at ~4 KiB objects with bounded memory under stream churn, and sub-millisecond to low-millisecond latency when paced below saturation. Numbers vary substantially by platform — methodology, observed figures, the paced-vs-unpaced distinction, and TX budget tuning are in [PERFORMANCE.md](PERFORMANCE.md).

## Development

```bash
git clone https://github.com/gmarzot/aiomoqt.git
cd aiomoqt
python3 -m venv .venv && source .venv/bin/activate
uv pip install -e ".[test]"    # or: pip install -e ".[test]"

# One-time self-signed cert for the loopback server + test_loopback_* suites (skipped if certs/ is absent)
mkdir -p certs && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -keyout certs/key.pem -out certs/cert.pem -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

pytest aiomoqt/tests/
```

> **Install editable (`-e`).** The standalone bench scripts and the
> cross-importing loopback tests resolve `certs/` and sibling modules
> relative to the working tree. A non-editable copy in site-packages
> silently skips the loopback suites with "TLS certs not found in certs/"
> even when the certs exist. `pytest` auto-generates `certs/` on first run;
> the `openssl` line above is only needed for the standalone bench scripts.

### Developing against a locally built aiopquic

The PyPI `aiopquic` wheel is **portable** (any CPU of the architecture). A locally compiled `aiopquic` is **host-tuned** (`-O3 -march=native -flto`, plus picotls Fusion AES-GCM on x86_64) and is measurably faster — build from source when benchmarking, optimizing, or targeting bare metal.

```bash
# Build aiopquic from source (host-tuned by default; AIOPQUIC_WHEEL_BUILD=1 for a portable build)
git clone https://github.com/gmarzot/aiopquic.git
cd aiopquic && git submodule update --init --recursive && ./build_picoquic.sh
```

With a **separate venv per repo**, install the local aiopquic into the aiomoqt venv **first**, so the dependency is already satisfied and no wheel is fetched (re-run it any time the wheel sneaks back in — it replaces the wheel install):

```bash
# in the aiomoqt venv:
uv pip install -e ~/aiopquic    # local source, editable — BEFORE aiomoqt
uv pip install -e '.[test]'     # aiopquic already satisfied → no wheel
```

Confirm the local build is actually in use:

```bash
python -c "import aiopquic; print(aiopquic.__file__)"   # must be <repo>/src/aiopquic/…, not …/site-packages/
python -m aiomoqt.versions                              # paths + picoquic/picotls SHAs match your build
```

Both venvs must use the **same Python version**. C/Cython changes need a rebuild (`./build_picoquic.sh` then `uv pip install -e ~/aiopquic`); pure-Python edits are live.

## Known Limitations

- **draft-18 is beta.** Negotiated by default (`(18, 16, 14)`, newest-first) over raw QUIC and WebTransport, with control, FETCH, and SUBGROUP object delivery validated end to end (including subscribers that join a track which already has objects). Not yet complete: the SUBSCRIBE / PUBLISH subscription-filter still uses the d16 nested form; Track-Properties extensions encode as RFC9000 varints (correct for the small values in use). draft-14 / draft-16 are unaffected and remain the stable path.
- **WebTransport fetch / join routing** -- four `[wt]` test variants of FETCH and JOINING_SUBSCRIBE return empty results when the underlying transport is WebTransport. Raw-QUIC variants of the same tests pass and cover the MoQT-level invariant. Tracked separately; affects WT-only consumers of fetch/join.

## TODO

* Fix WebTransport fetch / join routing (the 4 `[wt]` tests currently skipped)
* Track data modules:
  - File transfer (or [MOQT File Format](https://datatracker.ietf.org/doc/html/draft-jennings-moq-file-00)?)
  - Interactive chat
  - MSF/LOC media packaging
  - CMSF media packaging
* Simple relay implementation

## Contributing

Contributions are welcome! Please fork the repository, create a branch, and submit a pull request. For major changes, open an issue first.

## Resources

- [MoQT Specification](https://moq-wg.github.io/moq-transport/draft-ietf-moq-transport.html)
- [Media Over QUIC Working Group](https://datatracker.ietf.org/wg/moq/about/)
- [MoQ Interop Runner](https://github.com/englishm/moq-interop-runner)
- [`aiomoqt` GitHub Repository](https://github.com/gmarzot/aiomoqt)

---

## Author

Giovanni Marzot — [gmarzot@marzresearch.net](mailto:gmarzot@marzresearch.net)

A [Marz Research](https://github.com/gmarzot) project.

## Acknowledgements

This project takes inspiration from, and has benefited from the great work done by the [OpenMOQ/moqx](https://github.com/openmoq/moqx) team, and the continued efforts of the MOQ IETF WG.
