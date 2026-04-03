# Pre-Release Test Plan

Run all tests before tagging a release. All must pass unless noted as known issues.

## Prerequisites

```bash
# Local moqx docker relay running
docker ps --filter name=moqx

# Remote relay accessible
# moqx-000.ci.openmoq.org:4433
```

## 1. Unit Tests

```bash
.venv/bin/python -m pytest aiomoqt/tests/ -v
```

Expected: all pass

## 2. Buffer Reassembly Tests

```bash
.venv/bin/python tests/test_rebuf.py
```

Expected: 6/6 pass

## 3. Interop Tests (moqx-000 remote)

### D14 H3/WebTransport
```bash
.venv/bin/python -m aiomoqt.examples.moq_interop_client \
  -r https://moqx-000.ci.openmoq.org:4433/moq-relay
```

### D14 Raw QUIC
```bash
.venv/bin/python -m aiomoqt.examples.moq_interop_client \
  -r moqt://moqx-000.ci.openmoq.org:4433
```

### D16 H3/WebTransport
```bash
.venv/bin/python -m aiomoqt.examples.moq_interop_client \
  -r https://moqx-000.ci.openmoq.org:4433/moq-relay --draft 16
```

### D16 Raw QUIC
```bash
.venv/bin/python -m aiomoqt.examples.moq_interop_client \
  -r moqt://moqx-000.ci.openmoq.org:4433 --draft 16
```

Expected: 6/6 pass on each (24/24 total)

## 4. Relay Probe

```bash
RELAYS_FILE=/path/to/relays.json OUTPUT_FILE=/tmp/relay-status.json \
  .venv/bin/python -m aiomoqt.examples.relay_probe
```

Expected: all known-live relays detected

## 5. Data Streaming - D14 pub/sub (local docker relay)

### H3/WebTransport

Shell 1 (publisher):
```bash
.venv/bin/python -m aiomoqt.examples.pub_example \
  --host localhost --port 4433 --endpoint moq-relay \
  --namespace test/release --trackname track --insecure --draft 14
```

Shell 2 (subscriber):
```bash
.venv/bin/python -m aiomoqt.examples.sub_example \
  --host localhost --port 4433 --endpoint moq-relay \
  --namespace test/release --trackname track --insecure --draft 14
```

Expected: continuous data flow at 30fps, stats showing ~30 obj/s

### Raw QUIC

Same as above with `--use-quic` and without `--endpoint moq-relay`.

## 6. Data Streaming - D14 pub/sub (moqx-000 remote)

```bash
# Publisher
.venv/bin/python -m aiomoqt.examples.pub_example \
  --host moqx-000.ci.openmoq.org --port 4433 --endpoint moq-relay \
  --namespace test/release --trackname track --draft 14

# Subscriber
.venv/bin/python -m aiomoqt.examples.sub_example \
  --host moqx-000.ci.openmoq.org --port 4433 --endpoint moq-relay \
  --namespace test/release --trackname track --draft 14
```

Expected: continuous data flow, stats showing ~30 obj/s

## 7. Loopback Benchmark

```bash
.venv/bin/python -m aiomoqt.examples.loopback_bench -P 4 -s 16384 -r 60 -t 20
```

Expected: ~31 Mbps, zero loss, zero out-of-order

## Known Issues

- D16 data streaming dies at group boundary (first group OK, StreamReset on second group's new stream). Under investigation.
- D16 REQUEST_OK: moq-lite-rs expects REQUEST_OK for SubscribeNamespace response; we send SubscribeNamespaceOk. Works with moqx.
- moqx multi-subgroup: relay cache rejects different payloads for same (group_id, object_id) across subgroups.
- moqx stale cache: restarting publisher with different object size on same namespace causes Payload mismatch. Use fresh namespace or restart relay.
- Loopback throughput limited to ~54 Mbps by Python/qh3 overhead.
