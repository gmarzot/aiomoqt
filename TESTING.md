# Pre-Release Test Plan

Run all tests before tagging a release. All must pass unless noted as known issues.

Use unique tracknames for every test to avoid relay cache collisions.
The bench tools auto-generate unique tracknames from test parameters + uuid.

## Prerequisites

```bash
# Local moqx docker relay running (fresh restart for clean cache)
docker rm -f moqx 2>/dev/null
cd ~/Projects/moq/openmoq/moqx
docker compose -f docker/docker-compose.yml up -d moqx
sleep 3
docker ps --filter name=moqx

# Remote relay accessible
# moqx-000.ci.openmoq.org:4433
```

## 1. Unit Tests

```bash
.venv/bin/python -m pytest aiomoqt/tests/ -v
```

Expected: all pass (86+ tests including track module)

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

## 4. Loopback Benchmark

```bash
.venv/bin/python -m aiomoqt.examples.loopback_bench -P 4 -s 16384 -r 60 -t 10
```

Expected: ~31 Mbps, ~240 obj/s, p50 latency <5ms

## 5. Bench — D16 Raw QUIC, 500KB objects (local relay)

Shell 1 (publisher):
```bash
.venv/bin/python -m aiomoqt.examples.pub_bench \
  moqt://moqx-local-000.marzresearch.net:4433 \
  -s 500000 -t 120 -r 30 -g 30 -k --draft 16
```

Shell 2 (subscriber):
```bash
.venv/bin/python -m aiomoqt.examples.sub_bench \
  moqt://moqx-local-000.marzresearch.net:4433 \
  -t 120 -k --draft 16
```

Expected: ~116 Mbps, ~30 obj/s, zero loss, auto-discovers trackname

## 6. Bench — D16 Raw QUIC, 1KB, 4 streams (local relay)

Shell 1 (publisher):
```bash
.venv/bin/python -m aiomoqt.examples.pub_bench \
  moqt://moqx-local-000.marzresearch.net:4433 \
  -s 1024 -t 120 -r 120 -g 60 -P 4 -k --draft 16
```

Shell 2 (subscriber):
```bash
.venv/bin/python -m aiomoqt.examples.sub_bench \
  moqt://moqx-local-000.marzresearch.net:4433 \
  -t 120 -k --draft 16
```

Expected: ~480 obj/s, ~4 Mbps, p50 latency ~1ms

## 7. Example — D16 Raw QUIC, 4 streams (local relay)

Shell 1 (publisher):
```bash
.venv/bin/python -m aiomoqt.examples.pub_example \
  --host moqx-local-000.marzresearch.net --port 4433 \
  --use-quic --insecure --draft 16 \
  --namespace test --trackname vid-d16q -P 4 -t 120
```

Shell 2 (subscriber):
```bash
.venv/bin/python -m aiomoqt.examples.sub_example \
  --host moqx-local-000.marzresearch.net --port 4433 \
  --use-quic --insecure --draft 16 \
  --namespace test --trackname vid-d16q -t 120
```

Expected: continuous data flow, ~30 obj/s

## 8. Example — D14 Raw QUIC (local relay)

Shell 1 (publisher):
```bash
.venv/bin/python -m aiomoqt.examples.pub_example \
  --host moqx-local-000.marzresearch.net --port 4433 \
  --use-quic --insecure \
  --namespace test --trackname vid-d14q -P 4 -t 120
```

Shell 2 (subscriber):
```bash
.venv/bin/python -m aiomoqt.examples.sub_example \
  --host moqx-local-000.marzresearch.net --port 4433 \
  --use-quic --insecure \
  --namespace test --trackname vid-d14q -t 120
```

Expected: continuous data flow, ~30 obj/s

## 9. Example — D14 H3/WebTransport (local relay)

Shell 1 (publisher):
```bash
.venv/bin/python -m aiomoqt.examples.pub_example \
  --host moqx-local-000.marzresearch.net --port 4433 \
  --endpoint moq-relay --insecure \
  --namespace test --trackname vid-d14wt -P 4
```

Shell 2 (subscriber):
```bash
.venv/bin/python -m aiomoqt.examples.sub_example \
  --host moqx-local-000.marzresearch.net --port 4433 \
  --endpoint moq-relay --insecure \
  --namespace test --trackname vid-d14wt
```

Expected: continuous data flow, ~30 obj/s

## 10. Example — D16 Raw QUIC (remote relay)

Shell 1 (publisher):
```bash
.venv/bin/python -m aiomoqt.examples.pub_example \
  --host moqx-000.ci.openmoq.org --port 4433 \
  --use-quic --draft 16 \
  --namespace test --trackname vid-d16q-remote -P 4
```

Shell 2 (subscriber):
```bash
.venv/bin/python -m aiomoqt.examples.sub_example \
  --host moqx-000.ci.openmoq.org --port 4433 \
  --use-quic --draft 16 \
  --namespace test --trackname vid-d16q-remote
```

Expected: continuous data flow, ~30 obj/s

## 11. Bench — D16 Raw QUIC, max rate (local relay)

Shell 1 (publisher):
```bash
.venv/bin/python -m aiomoqt.examples.pub_bench \
  moqt://moqx-local-000.marzresearch.net:4433 \
  -s 4096 -t 120 -g 1000 -P 4 -k --draft 16
```

Shell 2 (subscriber):
```bash
.venv/bin/python -m aiomoqt.examples.sub_bench \
  moqt://moqx-local-000.marzresearch.net:4433 \
  -t 120 -k --draft 16
```

Expected: high throughput, tests congestion control under load

## Known Issues

- Occasional corrupt timestamp extension at high throughput (re_buf
  misalignment). Filtered in BenchStats, logged as warning. Pre-existing
  issue in protocol.py buffer reassembly.
- moqx stale cache: restarting publisher with different object size on
  same namespace/trackname causes Payload mismatch. Bench tools generate
  unique tracknames per run to avoid this.
- Loopback throughput limited to ~54 Mbps by Python/qh3 overhead.
- Stream idle timeout warnings at group boundaries (downgraded to DEBUG).
- d16 PUBLISH_OK establishes subscription — no explicit subscribe()
  needed after subscribe_namespace + PUBLISH flow.
