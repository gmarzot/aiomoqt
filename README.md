# aiomoqt - Media over QUIC Transport (MoQT)

`aiomoqt` is an implementation of [MoQT](https://moq-wg.github.io/moq-transport/draft-ietf-moq-transport.html) for `asyncio`, layered on [aiopquic](https://pypi.org/project/aiopquic/).

It follows the standard `asyncio` transport/protocol pattern via subclasses of `aiopquic.asyncio.QuicConnectionProtocol` for both raw QUIC and H3/WT sessions. Message encode/decode in `aiomoqt.messages` is sans-I/O. The session exposes APIs for all MoQT control-plane operations and supports a range of publish/subscribe workflows for both client and server roles. The package includes example clients, benchmarking and track simulation tools, a relay probe, and a [moq-interop-runner](https://github.com/englishm/moq-interop-runner)-compatible test client.

## Support matrix

| | Supported |
|---|---|
| Drafts | draft-14, draft-16, draft-18 — negotiated newest-first, default offer `(18, 16, 14)` |
| Transports | raw QUIC (ALPN `moq-00` / `moqt-16` / `moqt-18`), H3/WebTransport (WT-Protocol) |
| Roles | publisher, subscriber, server (origin), single-port dual-transport server |
| Data delivery | SubgroupHeader streams (TX + RX); OBJECT_DATAGRAM (TX + RX over raw QUIC, RX over WT) |
| Control | full message set with sync/async response handling; `MOQTRequestError` is draft-independent |
| Fetch / join | FETCH, JOINING_SUBSCRIBE (relative and absolute) over both transports |
| Media | MSF catalogs; LOC packaging (draft-ietf-moq-loc); CMSF/CMAF packaging (draft-ietf-moq-cmsf) |
| Python | 3.12+ (CI: 3.12, 3.13, 3.14 on Linux and macOS) |

Draft-16 adds delta-encoded param keys, track extensions, and unified request/response. Draft-18 adds vi64 varints, a uni-stream control pair, per-request bidi streams, and Request-ID-less replies. Wire-format conformance for all three is covered by `aiomoqt/tests/test_wire_conformance.py`.

## Installation

```bash
uv pip install aiomoqt    # or: pip install aiomoqt
```

Pure Python. `aiopquic` (the QUIC transport) installs as a binary wheel automatically. Prebuilt wheels exist for Linux (glibc 2.34+, RHEL 9 / Ubuntu 22.04+) and macOS arm64; other platforms build `aiopquic` from sdist and need a C toolchain — see [aiopquic install notes](https://github.com/gmarzot/aiopquic#installation).

For a clean, uv-managed `.venv`, run `./bootstrap_python.sh`.

## Quick start

### 1. Verify the install and reach a relay

Report the installed stack:

```bash
python -m aiomoqt.versions
```

Probe a relay over raw QUIC:

```bash
python -m aiomoqt.tools.relay_probe --url moqt://moqx-main.ci.openmoq.org:4433
# moqt://moqx-main.ci.openmoq.org:4433              QUIC   ✓  draft-14,draft-16,draft-18  (540ms)
```

Probe the same relay over H3/WebTransport:

```bash
python -m aiomoqt.tools.relay_probe --url https://moqx-main.ci.openmoq.org:4433/moq-relay
# https://moqx-main.ci.openmoq.org:4433/moq-relay   H3/WT  ✓  draft-14,draft-16,draft-18  (435ms)
```

The probe exits 0 if any draft handshakes, so it drops straight into a shell conditional.

### 2. Subscribe

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

### 3. Publish

```python
import asyncio
from aiomoqt.client import MOQTClient
from aiomoqt.types import MOQTMessageType
from aiomoqt.messages import SubgroupHeader

async def on_subscribe(session, msg):
    ok = session.subscribe_ok(request_msg=msg)
    stream_id = session.open_uni_stream()
    hdr = SubgroupHeader(track_alias=ok.track_alias, group_id=0,
                         subgroup_id=0, publisher_priority=0)
    session.stream_write(stream_id, hdr.serialize().data)
    session.stream_write(stream_id, hdr.next_object(payload=b"hello").data)

async def main():
    client = MOQTClient('relay.example.com', 443, path='moq',
                        use_quic=True, supported_drafts=16)
    client.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
    async with client.connect() as session:
        await session.client_session_init()
        await session.publish_namespace('ns', wait_response=True)
        await session.async_closed()

asyncio.run(main())
```

`PublishedTrack` / `SubscribedTrack` in [`aiomoqt/track.py`](aiomoqt/track.py) wrap the stream setup, header serialization, and object writing shown above; use them instead of hand-rolling `on_subscribe` unless you need the low-level surface.

## API guide

### Control messages

Every control message takes `wait_response`:

```python
resp = await session.subscribe('ns', 'track', wait_response=True)   # awaits the response
req  = await session.subscribe('ns', 'track')                       # response via handler
```

Register handlers for peer-initiated messages:

```python
client.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
```

Request failures raise `MOQTRequestError` regardless of negotiated draft.

### Tracks

| Class | Role |
|---|---|
| `PublishedTrack` | stream setup, subgroup writing, pacing, TX budget |
| `SubscribedTrack` | object reassembly, FETCH / JOIN handling |

`StreamMapping` selects the data-plane shape: `PER_GROUP` (subgroup stream per group) or `DATAGRAM` (one object per datagram; raw QUIC only — see Limitations).

### Auth

`AUTH_TOKEN` rides the SETUP handshake (session-level) and any request message (namespace- or track-level). Values are arbitrary bytes; the codec wraps them in the spec Token structure. Read a peer's token back from the message's `parameters`.

```python
from aiomoqt.types import SetupParamType, ParamType

await session.client_session_init(parameters={SetupParamType.AUTH_TOKEN: b"session-tok"})

await session.publish_namespace('ns', parameters={ParamType.AUTH_TOKEN: b"ns-tok"},
                                wait_response=True)

ok = await session.subscribe('ns', 'track',
                             parameters={ParamType.AUTH_TOKEN: b"track-tok"},
                             wait_response=True)
print(ok.parameters.get(ParamType.AUTH_TOKEN))
```

Auth is control-plane only; there is no per-object authentication.

## Media

`aiomoqt.media` implements MSF catalogs with two packagings: **LOC** (per-frame payloads, metadata in MOQ object properties) and **CMSF/CMAF** (moof+mdat chunks, CMAF header in the catalog `initDataList`).

### Command line

```bash
# Publish an mp4 as an MSF broadcast — LOC packaging, catalog + video + audio
python -m aiomoqt.tools.pub_media $RELAY -N demo/live --mp4 clip.mp4 --loop \
    --target-latency 500 --draft 16 -t 3600

# Same content, CMAF packaging (requires --mp4)
python -m aiomoqt.tools.pub_media $RELAY -N cmsf/live --mp4 clip.mp4 --packaging cmaf --loop

# Live H.264 Annex-B ingest (OBS / ffmpeg pipe); frames stamped on arrival
ffmpeg -i 'srt://0.0.0.0:9000?mode=listener' -map 0:v -c:v copy -f h264 - \
  | python -m aiomoqt.tools.pub_media $RELAY -N obs --h264 - --no-audio

# Subscribe: catalog-driven, writes playable files to ./media-out
python -m aiomoqt.tools.sub_media $RELAY -N demo/live --inspect 5 --show-catalog
ffplay media-out/video.h264      # LOC → elementary stream
ffplay media-out/video.mp4       # CMAF → fMP4
```

Source material should use short GOPs and no B-frames (`-g 2×fps -sc_threshold 0 -bf 0`) for low join latency. CMAF chunks carry one sample each with no composition-time offsets, so B-frame sources will not present correctly.

`--inspect N` prints per-frame group/object ids, size, keyframe flag, `ts_skew_ms` (wire latency), and extension properties. `--show-catalog` prints the catalog JSON as subscribers receive it.

### API

```python
from aiomoqt.media import (Catalog, CatalogTrack, InitData, MediaPublisher,
                           MediaSubscriber, LocTrackPublisher, StreamMapping)

# publisher: catalog track + one LOC track per medium
catalog = Catalog(generatedAt=..., tracks=[CatalogTrack(
    name='video', packaging='loc', isLive=True, role='video',
    codec='avc1.42C01E', width=1280, height=720, initRef='v0')],
    initDataList=[InitData.from_bytes('v0', avcc_extradata)])
pub = MediaPublisher(session, 'demo/live', catalog)
video = pub.add_track(LocTrackPublisher(session, 'demo/live', 'video',
                                        config=avcc_extradata,
                                        mapping=StreamMapping.PER_GROUP))
await pub.start()
await video.send_frame(payload, key_frame=True, timestamp=epoch_us)

# subscriber: reads the catalog, subscribes every track it describes
sub = MediaSubscriber(session, 'demo/live', on_frame=handle, on_catalog=handle_catalog)
catalog = await sub.start()
```

`MediaSubscriber.start()` joins the catalog track with SUBSCRIBE + joining FETCH (msf-01 §5) so a late joiner gets the relay-cached catalog, falling back to plain SUBSCRIBE when the peer cannot serve the fetch. `on_catalog` fires on the first catalog and every applied delta.

End-to-end pipeline walkthroughs — file to browser, live OBS ingest, CMAF, cross-implementation consumers — are in [docs/demo-runbook.md](docs/demo-runbook.md).

## Server

```bash
# WebTransport origin
python -m aiomoqt.examples.server_example --cert cert.pem --key key.pem -p 4433

# Standalone publisher server (no relay), raw QUIC or WT
python -m aiomoqt.tools.pub_server --cert cert.pem --key key.pem -p 4433 -Q
```

`MOQTServer` serves one transport with `serve()`, or both on a single UDP port with `serve_dual()`:

```python
from aiomoqt.server import MOQTServer

server = MOQTServer('0.0.0.0', 4433, certificate='cert.pem', private_key='key.pem',
                    path='moq', supported_drafts=[18, 16])
await server.serve_dual()   # raw QUIC + H3/WT on one port
```

`serve_dual()` routes each connection by negotiated ALPN (aiopquic `serve_dispatch`): raw connections select a draft via the per-draft MoQT ALPNs, WebTransport connections via WT-Protocol. It replaces two-listener arrangements that split transports across ports.

## Tools

Each tool runs as a module (`python -m aiomoqt.tools.NAME`) and most also install a console script. Every tool prints its full option set with `-?` / `--help` — note `-h` is `--host`, not help. Bench and media tools take a positional relay URL: `moqt://host[:port]` for raw QUIC, `https://host[:port]/[path]` for H3/WebTransport.

| Module | Console script | Purpose |
|---|---|---|
| `aiomoqt.versions` | `aiomoqt-versions` | version report (aiomoqt, aiopquic, picoquic/picotls SHAs) |
| `tools.pub_media` | — | MSF/LOC/CMAF media publisher (mp4, live H.264, tone) |
| `tools.sub_media` | — | catalog-driven media subscriber; writes playable files |
| `tools.pub_bench` | `moq-pub-bench` | publisher benchmark |
| `tools.sub_bench` | `moq-sub-bench` | subscriber benchmark — latency, jitter, loss |
| `tools.loopback_bench` | `moq-loopback-bench` | in-process publisher + subscriber, no relay |
| `tools.adaptive_bench` | `moq-adaptive-bench` | ramps rate or subscriber count until degradation |
| `tools.pub_server` | `moq-pub-server` | standalone publisher server |
| `tools.load_sim` | `moq-load-sim` | multi-session load generator |
| `tools.relay_probe` | `moq-relay-probe` | relay liveness and draft-version probe |
| `tools.moq_interop_client` | `moq-interop-client` | interop test client (TAP output) |
| `tools.moq_interop_relay` | `moq-interop-relay` | interop test relay (forwards; not production) |

Examples under `aiomoqt.examples` are minimal, readable clients rather than instrumented tools:

| Module | Purpose |
|---|---|
| `examples.pub_example` | publisher over SubgroupHeader streams |
| `examples.sub_example` | subscriber |
| `examples.join_example` | SUBSCRIBE + FETCH (join mid-stream) |
| `examples.server_example` | WebTransport origin |

Common options across clients: `--namespace`, `--trackname`, `--path`, `--draft`, `--debug`, `--keylogfile`.

```bash
python -m aiomoqt.examples.pub_example moqt://relay.ex.com
python -m aiomoqt.examples.sub_example moqt://relay.ex.com
python -m aiomoqt.tools.loopback_bench -s 4096 -P 4 -t 20
python -m aiomoqt.tools.pub_bench moqt://relay.ex.com -s 4096 -P 4 -r 120 -t 60
```

## Interop

Validated against live public relays — OpenMoQ moqx, Meta moxygen, Cloudflare moq-rs, Quicr libquicr, Meetecho imquic, OzU moqtail, Nokia — across draft-14/16/18 and both transports, using the [moq-interop-runner](https://github.com/englishm/moq-interop-runner) cases plus a multi-subscriber pub-sub bench. The point-in-time matrix is in [PERFORMANCE.md](PERFORMANCE.md#interop-matrix-point-in-time); the relay catalog with per-endpoint notes is [`tests/relays.json`](tests/relays.json).

### Interop client

Runs the six standard runner cases (`setup-only`, `announce-only`, `publish-namespace-done`, `subscribe-error`, `announce-subscribe`, `subscribe-before-announce`) plus `fetch` and `join`, emitting TAP.

```bash
python -m aiomoqt.tools.moq_interop_client -r "moqt://relay.ex.com:4433"              # all, draft auto
python -m aiomoqt.tools.moq_interop_client -r "moqt://relay.ex.com:4433" --draft 16
python -m aiomoqt.tools.moq_interop_client -r "moqt://relay.ex.com:4433" -t subscribe-error
python -m aiomoqt.tools.moq_interop_client -l                                         # list cases
```

### Interop relay

`moq_interop_relay` is a **test fixture, not a production relay**. It forwards objects from an upstream publisher to downstream subscribers, fans one upstream subscription out to several subscribers, serves both publish flows (`PUBLISH_NAMESPACE` and bare `PUBLISH`), and dials upstream origins with `--upstream`. It has no group cache — so no joining FETCH and a late subscriber sees only what arrives next — no forward-state propagation, no `PUBLISH` forwarding to `SUBSCRIBE_TRACKS` subscribers, no authentication, and no backpressure. Use moxygen, moq-rs, or another real relay for any workload.

Its purpose is to exercise aiomoqt's server-side primitives — `MOQTServer`, the announce and subscribe handlers, `serve_dual()` — and to be driven by an external conformance suite, so both sides of the stack are covered. Because the relay re-encodes everything it forwards, a conformance client checking those objects is checking aiomoqt's encoder, not merely its routing.

```bash
python -m aiomoqt.tools.moq_interop_relay --bind 0.0.0.0 --port 4443 \
    --cert cert.pem --key key.pem --dual     # raw QUIC + WT on one port
```

`--quic` serves raw QUIC only, `--quic-port N` runs the legacy second listener for runners that expect distinct endpoints, and `--upstream URL` (repeatable) dials an origin for tracks no inbound publisher serves — the arrangement the moq-test conformance suite expects.

### Relay probe

Reads a relay list, performs a real CLIENT_SETUP / SERVER_SETUP handshake per (endpoint × draft), and writes a JSON status report. Accepts CLI flags, environment variables, or both (CLI overrides env).

```bash
python -m aiomoqt.tools.relay_probe -f relays.json -o status.json
RELAYS_FILE=relays.json OUTPUT_FILE=status.json python -m aiomoqt.tools.relay_probe
python -m aiomoqt.tools.relay_probe -f relays.json -o status.json --interval 300
```

| CLI flag | Env var | Default | Meaning |
|---|---|---|---|
| `-f / --relays-file` | `RELAYS_FILE` | `/app/relays.json` | input relay list |
| `-o / --output-file` | `OUTPUT_FILE` | `/output/relay-status.json` | status report destination |
| `--timeout` | `PROBE_TIMEOUT` | `8` | per-probe handshake timeout (s) |
| `--interval` | `PROBE_INTERVAL` | `0` | re-probe cadence (s); `0` probes once and exits |
| `--draft` | — | all | draft(s) to probe: `--draft 18` or `--draft 18,16` (add `--offer` to offer the list in one session) |
| `--url` | — | — | probe one URL and print a line per draft; bypasses the file/report path |

## Performance

Throughput at this layer is bounded by `aiopquic`, picoquic, and the kernel UDP path beneath it. Methodology, observed figures, the paced-vs-unpaced distinction, TX budget tuning, and the full tool matrix are in [PERFORMANCE.md](PERFORMANCE.md).

## Development

```bash
git clone https://github.com/gmarzot/aiomoqt.git
cd aiomoqt
python3 -m venv .venv && source .venv/bin/activate
uv pip install -e ".[test]"    # or: pip install -e ".[test]"
pytest aiomoqt/tests/
```

Install editable. The standalone bench scripts and the cross-importing loopback tests resolve `certs/` and sibling modules relative to the working tree; a non-editable install skips the loopback suites with "TLS certs not found in certs/". `pytest` generates `certs/` on first run. For the standalone bench scripts, generate them directly:

```bash
mkdir -p certs && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout certs/key.pem -out certs/cert.pem -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
```

### Building against a local aiopquic

The PyPI `aiopquic` wheel is portable (any CPU of the architecture). A locally compiled `aiopquic` is host-tuned (`-O3 -march=native -flto`, plus picotls Fusion AES-GCM on x86_64); build from source when benchmarking or targeting known hardware.

```bash
git clone https://github.com/gmarzot/aiopquic.git
cd aiopquic && git submodule update --init --recursive && ./build_picoquic.sh
```

With a separate venv per repo, install the local aiopquic into the aiomoqt venv **first**, so the dependency is already satisfied and no wheel is fetched:

```bash
# in the aiomoqt venv:
uv pip install -e ~/aiopquic    # local source, editable — BEFORE aiomoqt
uv pip install -e '.[test]'
```

Confirm which build is in use:

```bash
python -c "import aiopquic; print(aiopquic.__file__)"   # must be <repo>/src/aiopquic/…
python -m aiomoqt.versions                              # paths + picoquic/picotls SHAs
```

Both venvs must use the same Python version. C/Cython changes need a rebuild (`./build_picoquic.sh`, then `uv pip install -e ~/aiopquic`); pure-Python edits are live.

### Reporting issues

Include the version report — it captures aiomoqt, aiopquic, and the picoquic + picotls submodule SHAs aiopquic was built from:

```bash
python -m aiomoqt.versions
```

```
aiomoqt:   0.11.0 (~/src/aiomoqt/aiomoqt) [2026-08-28 11:57]
aiopquic:  0.4.0 (~/src/aiopquic/src/aiopquic) [2026-08-28 11:58]
  - picoquic:  1.1.51.1 (7bbf9ef0) [2026-08-03]
  - picotls:   master (bfa67875) [2026-04-20]
```

## Limitations

- **WebTransport datagram TX is not implemented.** Datagram publishing and reception work over raw QUIC; on WebTransport, datagram reception works but `StreamMapping.DATAGRAM` publishing raises. Use `PER_GROUP` mapping over WebTransport.
- **`relay_probe -f` and `tests/relays.json` use different schemas.** The probe expects a top-level array of `{id, name, endpoints:[{url}]}`; the interop catalog is `{relays:[{name, urls:{...}, drafts:[...]}]}`. Use `--url` for single-endpoint probes until the loader accepts both.
- **`moq_interop_relay` is a conformance fixture**, not a production relay — it forwards and fans out, but has no group cache (hence no joining FETCH), no forward-state propagation, no authz, and no backpressure. See [Interop relay](#interop-relay).

## Roadmap

- Single-port interop-runner adapter — one relay image and one port via `serve_dual()`
- WebTransport datagram TX
- Binding-deadline reaper for data streams whose track-alias binding never completes
- Broader CI test coverage — the workflow currently names a subset of the suite explicitly

## Contributing

Fork, branch, and open a pull request. For major changes, open an issue first.

## Resources

- [MoQT Specification](https://moq-wg.github.io/moq-transport/draft-ietf-moq-transport.html)
- [Media Over QUIC Working Group](https://datatracker.ietf.org/wg/moq/about/)
- [MoQ Interop Runner](https://github.com/englishm/moq-interop-runner)
- [OpenMOQ](https://openmoq.org/) — [github.com/openmoq](https://github.com/openmoq)
- [`aiomoqt` GitHub Repository](https://github.com/gmarzot/aiomoqt)

---

## Author

Giovanni Marzot — [gmarzot@marzresearch.net](mailto:gmarzot@marzresearch.net)

A [Marz Research](https://github.com/gmarzot) project.

## Acknowledgements

This project takes inspiration from, and has benefited from the great work done by the [OpenMOQ](https://openmoq.org/) team ([github.com/openmoq](https://github.com/openmoq)), and the continued efforts of the MOQ IETF WG.
