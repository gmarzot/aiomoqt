# aiomoqt Performance Baselines

Performance baselines captured before transport changes. All tests run on
the same machine using `loopback_bench.py` (direct pub→sub, no relay).

## Environment

- Transport: qh3 (H3/WebTransport over QUIC)
- Platform: Linux 6.6.87 (WSL2)
- Python: 3.14.4
- aiomoqt: v0.7.0-dev (commit a70b8fd)
- Date: 2026-04-12

## v0.7.0 qh3 Baseline (2026-04-12)

### Paced (realistic media profiles)

| Config | Streams | Groups | Duration | Recv Rate | Throughput | Lat avg | p99 | Jitter |
|--------|---------|--------|----------|-----------|-----------|---------|-----|--------|
| 4KB, 30fps, g=30 | x4 | 113 | 30s | 120 obj/s | 3.93 Mbps | 1.7ms | 3ms | 0.43ms |
| 16KB, 30fps, g=30 | x4 | 113 | 30s | 120 obj/s | 15.70 Mbps | 4.0ms | 7ms | 0.48ms |

### Max rate (throughput ceiling)

| Config | Streams | Groups | Duration | Recv Rate | Throughput | Lat avg | p99 | Lost |
|--------|---------|--------|----------|-----------|-----------|---------|-----|------|
| 4KB, max, g=10K | x1 | 14 | 30s | 4366 obj/s | 143.5 Mbps | 14.3ms | 78ms | 0% |

### Notes

- x4 paced tests report 73% "lost" — this is a stats artifact: subscriber
  callback only counts objects from the first subgroup it sees. All 4
  subgroups are delivered correctly (total recv = 120 obj/s = 4 x 30).
- x1 max-rate at 143 Mbps with 0% loss, 30s sustained.
- g=30 max-rate stress test now runs to completion after fixing WT header
  fragmentation and H3 stream misidentification bugs (was crashing at 0.4s).

## Bug fixes in this release affecting performance

1. **WT header fragmentation** — partial WebTransport stream headers
   (split across QUIC packets) were silently dropped, causing parse
   misalignment on subsequent data. Root cause of the long-standing
   "re_buf parser misalignment" issue (~1 per 1500 objects at high
   throughput). Fixed by buffering partial WT header bytes.

2. **H3 internal stream misidentification** — data streams whose first
   byte was 0x00 were misrouted to H3 handler (confused with SETTINGS
   stream type). Fixed by using qh3's tracked peer stream IDs instead
   of first-byte heuristic.

3. **sub_bench jitter crash** — TypeError when timestamp extension was
   missing or corrupt. Fixed guard in jitter calculation.
