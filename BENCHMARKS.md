# aiomoqt Performance Baselines

Sample numbers from `loopback_bench.py` (direct pub→sub, no relay).
Throughput ceiling is Python/qh3-overhead-bound and varies by host CPU;
the numbers below are for shape, not absolute targets.

## Environment (sample run)

- Transport: qh3 (H3/WebTransport over QUIC)
- Platform: Linux 6.6.87 (WSL2)
- Python: 3.14.4

## Paced (media-profile workloads)

| Config            | Streams | Duration | Recv Rate | Throughput | Lat avg | p99 | Jitter |
|-------------------|---------|----------|-----------|------------|---------|-----|--------|
| 4KB, 30fps, g=30  | x4      | 30s      | 120 obj/s | 3.93 Mbps  | 1.7ms   | 3ms | 0.43ms |
| 16KB, 30fps, g=30 | x4      | 30s      | 120 obj/s | 15.70 Mbps | 4.0ms   | 7ms | 0.48ms |

## Max rate (throughput ceiling)

| Config           | Streams | Duration | Recv Rate   | Throughput | Lat avg | p99  | Lost |
|------------------|---------|----------|-------------|------------|---------|------|------|
| 4KB, max, g=10K  | x1      | 30s      | 4366 obj/s  | 143.5 Mbps | 14.3ms  | 78ms | 0%   |
