# Pre-Release Test Plan

Run all tests before tagging a release. All must pass.

Bench tools auto-generate unique tracknames from test parameters to
avoid relay cache collisions.

## Automated runner

`tests/release-regression-test.py` executes three tiers:

- **unit**:        `pytest`, `test_rebuf` — pure Python, no network
- **integration**: `loopback` — local pub/sub over QUIC, no relay
- **interop**:     `relay-smoke`, `multi-sub` × each relay in
                   `tests/relays.json` × each supported transport / draft

```bash
# full matrix (both tiers, every relay)
python tests/release-regression-test.py

# tier selection (repeatable)
python tests/release-regression-test.py --test-tier unit
python tests/release-regression-test.py --test-tier interop

# individual suite (repeatable; ignores tier grouping)
python tests/release-regression-test.py --test-suite pytest
python tests/release-regression-test.py --test-suite loopback --test-suite relay-smoke

# interop tier scoped to one relay from the catalog
python tests/release-regression-test.py --test-tier interop --only moqx-main
python tests/release-regression-test.py --test-tier interop --only cloudflare-d14

# alternate catalog
python tests/release-regression-test.py --catalog /path/to/relays.json
```

Exit 0 iff every test passed. Per-test logs land in a temp directory
printed at start/end of the run.

To add a relay to the catalog, edit `tests/relays.json`:

```json
{
  "name": "short-id",
  "urls": {
    "raw-quic": "moqt://host:port",
    "h3-wt":    "https://host:port/endpoint"
  },
  "drafts": [14, 16],
  "pub_mode": "publish"
}
```

`pub_mode` selects the multi-sub publisher behavior for that relay:
`publish` (default, sends PUBLISH only), `publish-ns` (PUBLISH_NAMESPACE
only, waits for SUBSCRIBE), or `publish-both` (both messages; required
by Cloudflare d14 moq-rs).

Sections below document the underlying commands for manual runs.

## Prerequisites

A local moqx relay and a remote one accessible at
`moqx-000.ci.openmoq.org:4433`.

## 1. Unit Tests

```bash
pytest aiomoqt/tests/ -v
```

Expected: all pass (156+ tests including track module)

## 2. Buffer Reassembly Tests

```bash
python tests/test_rebuf.py
```

Expected: 6/6 pass

## 3. Interop Tests (moqx-000 remote)

### D14 H3/WebTransport
```bash
python -m aiomoqt.examples.moq_interop_client -r https://moqx-000.ci.openmoq.org:4433/moq-relay
```

### D14 Raw QUIC
```bash
python -m aiomoqt.examples.moq_interop_client -r moqt://moqx-000.ci.openmoq.org:4433
```

### D16 H3/WebTransport
```bash
python -m aiomoqt.examples.moq_interop_client -r https://moqx-000.ci.openmoq.org:4433/moq-relay --draft 16
```

### D16 Raw QUIC
```bash
python -m aiomoqt.examples.moq_interop_client -r moqt://moqx-000.ci.openmoq.org:4433 --draft 16
```

Expected: 6/6 pass on each (24/24 total)

## 4. Loopback Benchmark

```bash
python -m aiomoqt.examples.loopback_bench -P 4 -s 16384 -r 60 -t 10
```

Expected: ~31 Mbps, ~240 obj/s, p50 latency <5ms

## 5. Bench — D16 Raw QUIC, 500KB objects (local relay)

Shell 1 (publisher):
```bash
python -m aiomoqt.examples.pub_bench moqt://moqx-local-000.marzresearch.net:4433 -s 500000 -t 120 -r 30 -g 30 -k --draft 16
```

Shell 2 (subscriber):
```bash
python -m aiomoqt.examples.sub_bench moqt://moqx-local-000.marzresearch.net:4433 -k --draft 16
```

Expected: ~116 Mbps, ~30 obj/s, zero loss, auto-discovers trackname

## 6. Bench — D16 Raw QUIC, 1KB, 4 streams (local relay)

Shell 1 (publisher):
```bash
python -m aiomoqt.examples.pub_bench moqt://moqx-local-000.marzresearch.net:4433 -s 1024 -t 120 -r 120 -g 60 -P 4 -k --draft 16
```

Shell 2 (subscriber):
```bash
python -m aiomoqt.examples.sub_bench moqt://moqx-local-000.marzresearch.net:4433 -k --draft 16
```

Expected: ~480 obj/s, ~4 Mbps, p50 latency ~1ms

## 7. Example — D16 Raw QUIC, 4 streams (local relay)

Shell 1 (publisher):
```bash
python -m aiomoqt.examples.pub_example --host moqx-local-000.marzresearch.net --port 4433 --use-quic --insecure --draft 16 --namespace test --trackname vid-d16q -P 4 -t 120
```

Shell 2 (subscriber):
```bash
python -m aiomoqt.examples.sub_example --host moqx-local-000.marzresearch.net --port 4433 --use-quic --insecure --draft 16 --namespace test --trackname vid-d16q
```

Expected: continuous data flow, ~30 obj/s

## 8. Example — D14 Raw QUIC (local relay)

Shell 1 (publisher):
```bash
python -m aiomoqt.examples.pub_example --host moqx-local-000.marzresearch.net --port 4433 --use-quic --insecure --namespace test --trackname vid-d14q -P 4 -t 120
```

Shell 2 (subscriber):
```bash
python -m aiomoqt.examples.sub_example --host moqx-local-000.marzresearch.net --port 4433 --use-quic --insecure --namespace test --trackname vid-d14q
```

Expected: continuous data flow, ~30 obj/s

## 9. Example — D14 H3/WebTransport (local relay)

Shell 1 (publisher):
```bash
python -m aiomoqt.examples.pub_example --host moqx-local-000.marzresearch.net --port 4433 --endpoint moq-relay --insecure --namespace test --trackname vid-d14wt -P 4
```

Shell 2 (subscriber):
```bash
python -m aiomoqt.examples.sub_example --host moqx-local-000.marzresearch.net --port 4433 --endpoint moq-relay --insecure --namespace test --trackname vid-d14wt
```

Expected: continuous data flow, ~30 obj/s

## 10. Example — D16 Raw QUIC (remote relay)

Shell 1 (publisher):
```bash
python -m aiomoqt.examples.pub_example --host moqx-000.ci.openmoq.org --port 4433 --use-quic --draft 16 --namespace test --trackname vid-d16q-remote -P 4
```

Shell 2 (subscriber):
```bash
python -m aiomoqt.examples.sub_example --host moqx-000.ci.openmoq.org --port 4433 --use-quic --draft 16 --namespace test --trackname vid-d16q-remote
```

Expected: continuous data flow, ~30 obj/s

## 11. Bench — D16 Raw QUIC, max rate (local relay)

Shell 1 (publisher):
```bash
python -m aiomoqt.examples.pub_bench moqt://moqx-local-000.marzresearch.net:4433 -s 4096 -t 120 -g 1000 -P 4 -k --draft 16
```

Shell 2 (subscriber):
```bash
python -m aiomoqt.examples.sub_bench moqt://moqx-local-000.marzresearch.net:4433 -k --draft 16
```

Expected: high throughput, tests congestion control under load

## Known Issues

- Occasional corrupt timestamp extension at high throughput (re_buf
  misalignment). Filtered in BenchStats, logged as warning.
- moqx caches by (namespace, trackname); reusing a trackname after a
  publisher size/rate change yields Payload mismatch. Bench tools
  auto-generate unique tracknames per run.
