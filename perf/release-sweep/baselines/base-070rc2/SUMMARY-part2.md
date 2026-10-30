# Phase 1c — two-node direct baseline (D + Ri)

Arm `base-070rc2` (aiopquic v0.7.0rc2 @51a80c9) on CPython 3.14.6.
Run block: `runs/2026-10-30T22-56-13Z-p1c2` (tag `p1c2`). N=3, 60 s per point, 10 s gaps,
same arm throughout. Subscriber-side numbers unless stated otherwise; `pubMb` is the
publisher's own accounting over its own elapsed window.

## Pair verification (recorded in each `result.json.env`)

| item | shenzhen | seawolf |
|---|---|---|
| role in this phase | publisher (D), relay+subscriber (Ri1) | subscriber (D), publisher (Ri1) |
| IP | 10.14.25.173/24 | 10.14.25.37/24 |
| kernel | 6.6.87.2-microsoft-standard-WSL2 | 6.18.54.1-microsoft-standard-WSL2 |
| nproc | 16 | 6 |
| governor | n/a (virtual) | n/a (virtual) |
| guest NIC (sysfs) | eth1 10000 Mb/s full | eth0 10000 Mb/s full |
| **physical NIC** | **Realtek PCIe GbE, 1 Gbps, MTU 1500** | not visible from guest |
| clock method | tcp-peer, tolerance 2000 µs | same |
| clock error per rep | 1982–2900 µs | same |
| RTT (5× ping) | → seawolf 4.25/5.02/5.96 ms (min/avg/max, mdev 0.64) | |

Both endpoints are WSL2 guests. The 10 Gb/s figure reported by `sysfs` is the Hyper-V
vNIC's nominal speed, **not** the physical link: the Windows adapter that owns
10.14.25.173 is a 1 Gbps Realtek PCIe GbE (`Get-NetAdapter`: `Ethernet`, `1 Gbps`,
MTU 1500). Wi-Fi on that host is disconnected, so the path is wired. All ceiling
arithmetic below uses 1 GbE (≈941 Mb/s theoretical payload rate at MTU 1500).

`llama-server` is resident on seawolf (idle, `--threads 8`); it consumed no measurable
CPU during the points (subscriber host busy 31–32 %, of which the bench was ~1.4 cores).

## Results

| point | config | n | cmd Mb/s | delivered Mb/s (mean ± sd) | CV% | obj/s | grp/s | cpuPub% | cpuSub% | ivCV% | pubMb |
|---|---|---|---|---|---|---|---|---|---|---|---|
| D1 | P=1 s=4096 g=200 r=9155 | 3 | 300 | 286.49 ± 1.02 | 0.35 | 8 715 | 43.6 | 18.3 | 26.1 | 4.9 | 301 |
| D2 | P=4 s=4096 g=200 r=18311 | 3 | 600 | 576.87 ± 2.37 | 0.41 | 17 549 | 87.8 | 33.5 | 79.7 | 2.6 | 602 |
| D3 | P=4 s=4096 g=200 r=0 | 3 | unpaced | **812.11 ± 30.20** | 3.72 | 24 705 | 123.5 | 61.0 | 140.4 | 17.0 | 856 |
| D4 | P=8 s=4096 g=40 r=0 | 3 | unpaced | 787.14 ± 3.93 | 0.50 | 23 946 | 598.7 | 74.8 | 153.1 | 3.4 | 833 |
| D5 | P=4 s=4096 g=200 r=18311 (WT) | 3 | 600 | 569.69 ± 0.25 | 0.04 | 17 330 | 86.7 | 35.4 | 71.4 | 0.2 | 602 |
| Ri1 | P=4 s=4096 g=200 r=9155, pub on seawolf, relay+sub on shenzhen | 3 | 300 | 317.94 ± 0.42 | 0.13 | 9 672 | 48.4 | – | 26.7 (sub+relay) | n/a | 301 |

`cpu*` is percent of one core (`sweep.py` accounting). Paced points meet their command
within measurement basis: D1 301 vs 300, D2/D5 602 vs 600 on the publisher side, so no
point silently fell back to unpaced. Subscriber-side paced numbers run 4–5 % below the
publisher's because the two elapsed windows differ (subscriber `elapsed_s` 83–87 s vs the
publisher's 60 s pacing window); the publisher column is the one to compare against the
command, the delivered column is what a receiving application actually sees.

Ri1 deserves its own note: the publisher reported 301 Mb/s (command met) while the
co-located relay+subscriber pair counted 317.9 Mb/s (+5.6 %). The sign of that gap is
opposite to the D points, which rules out a simple clock-rate artifact and points at the
two-process elapsed basis inside the relay topology. Ri1 is therefore a valid *ingress*
measurement at 300 Mb/s, not evidence that the relay multiplies bytes.

## The direct-path ceiling (D3) — it is the NIC

D3 (P=4, unpaced, 4096 B objects, 200-object groups) plateaus at **812 ± 30 Mb/s**
(publisher-side 856 Mb/s). Four independent lines of evidence say the plateau is the
1 GbE link, not the CPU and not our stack:

1. **Wire counters.** Windows adapter byte counters over a 45 s window inside D3 rep-02
   read **TX 836.6 Mb/s / RX 0.6 Mb/s** on a **1 Gbps** link — 89 % of the 941 Mb/s
   payload ceiling. The link is carrying essentially everything it can.
2. **CPU headroom on both sides.** Publisher 61 % of one core on a 16-core host;
   subscriber 140 % (1.4 cores) on a 6-core host. Neither side is close to saturated,
   and `steal` was 0.00 % on both.
3. **The stack is not the limit.** The same code path on loopback (Phase 1b, L2)
   delivered 1731.8 Mb/s — 2.1× the direct path. So QUIC/aiomoqt can move 1.7 Gb/s on
   this CPU; it is only the two-node path that stops at 812.
4. **Raising the offer does not raise delivery.** Paced 600 Mb/s (D2) sits below the
   plateau; unpaced P=4 (D3) reaches it; unpaced P=8 (D4) does not exceed it
   (787 Mb/s, −3.1 % vs D3). More concurrency past the ceiling only adds scheduling
   noise — consistent with a wire-rate limit, not a per-stream one.

Interval behaviour at the plateau is bursty: `interval_cv` 17 % in D3 (1 s intervals
swinging 463–1200 Mb/s) versus 0.2–2.6 % for the paced points. Wire-rate limiting on the
WSL2/vSwitch path delivers in clumps; that is visible in throughput jitter but not in
loss (`lost_pct` 0.00 on all 18 reps).

**Conclusion: the two-node direct plateau is the 1 GbE NIC.** Not CPU-bound (both sides
have headroom), not stack-bound (loopback does 2.1×), and not addressable in aiomoqt.

## What the two-node path costs versus loopback

Loopback → direct: 1731.8 Mb/s → 812.1 Mb/s, i.e. **−53.1 %** (0.47×). None of that loss
is CPU: the publisher drops from 100 % of a core on loopback to 61 % on the wire, and the
subscriber stays at 1.4 cores of 6. The cost is the physical link plus the WSL2
Hyper-V/vSwitch hop on both ends, which also shows up as **4.25–5.96 ms RTT** — far above
what a wired GbE LAN should show (<1 ms), so the virtual path, not the switch, dominates
latency. Small-object churn at the ceiling costs much less here than on loopback:
D4 (P=8, g=40) is −3.1 % vs D3, against −15.8 % for the loopback churn point in Phase 1b,
because a wire-limited path has spare CPU to absorb the extra group rate (599 grp/s).
WT framing is nearly free: D5 vs D2 = −1.3 % (569.7 vs 576.9 Mb/s) at the same command.

Cross-host **latency percentiles remain unmeasured**: `clock_offset_us` is null on all 18
reps because the tcp-peer offset moved up to 393 692 µs inside a single 60 s point (tolerance
2 000 µs, measured error 1 982–2 900 µs). All 18 reps are consequently `dirty`, with
`p50/p95/p99 = null`. Throughput and CPU numbers are unaffected — they use each host's own
monotonic clock — but no tail-latency claim can be made from this phase. That is the
open item carried into Phase 1d.

## Correction carried in this phase

`manifests/baseline-v0.yaml` authored `r` in **kbit/s** (300000) while every aiomoqt tool
takes `-r` as **objects/sec**, so the pacer sat far above line rate and every "paced"
point silently ran unpaced. Rescaled to objects/s at s=4096 B (200 Mb/s=6104,
300=9155, 400=12207, 600=18311) and the manifest header now documents the unit. All numbers
above are post-fix; the killed pre-fix block `runs/2026-10-30T22-52-58Z-p1c` should not be
cited.
