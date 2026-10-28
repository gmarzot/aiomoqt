# Release performance sweep — aiomoqt 0.14.0 / aiopquic 0.7.0

**Status:** active program
**Execution:** Shen (marzresearch Hermes, shenzhen)
**Planning / verification:** Marz (marzinc Hermes, argo)
**Opened:** 2026-10-28
**Horizon:** ~4 weeks, weekly checkpoint

---

## 0. How to use this document

This is the charter. `STATE.md` (same directory) is the live position: current phase,
arms built, last run id, open questions, next action. Every task on the `release-perf`
board names a section of this file.

Two hard rules:

1. A number without a run id, an env fingerprint and a git SHA does not exist.
2. When something was not measured, write **not measured** — never interpolate,
   never quote a number from a different topology as if it were this one's.

---

## 1. Why this program exists

The 0.14.0 (aiomoqt) / 0.7.0 (aiopquic) trains are not a straight-line improvement.
Two things moved at once:

- **Forward:** the picoquic submodule advanced (BBRv3 hardening, stream-lifecycle
  fixes, GSO batch-end handling, PATH_CHALLENGE burst handling, RNG thread safety),
  and aiopquic/aiomoqt added real capability (pull-model TX, bounded RX flow control,
  event-ring caps, priority scheduling, fan-out, d16/d18).
- **Backward (suspected):** several of those changes are delivery-correctness work
  that sits on the hot path or changes pacing/framing behaviour. Some are suspected
  of costing throughput or tail latency.

The release decision needs cost and benefit separated: which changes are essential
tax, which are avoidable overhead, which knobs buy the loss back, and where the
new ceiling actually is.

**Deliverable:** an evidence-backed answer to *"what did 0.14.0/0.7.0 cost relative
to 0.13.3/0.6.1, on which delivery pattern, why — and what should change before
release?"*

---

## 2. Questions, ranked

| # | Question | Answered by |
|---|---|---|
| Q1 | Which commits between 0.6.1→0.7.0rc2 and 0.13.3→main cost throughput or tail latency, and how much? | Phase 2 (compare + bisect) |
| Q2 | Which runtime knobs recover the loss (CC, GSO, TX wake threshold, budgets, ring caps, allocator)? | Phase 3a |
| Q3 | How do multiple subgroup streams and group turnover scale — where is the knee, what breaks first? | Baseline + Phase 3b |
| Q4 | What does the relay/fan-out delivery pattern cost vs direct and loopback — is the wall the stack, the relay, or the host? | Baseline, Phase 3c |
| Q5 | How far apart are loopback (upper bound) and two-node (real path), and is the gap stack or network? | Baseline report |

---

## 3. Metrics

Per **point** (one configuration × one arm × one repetition), record:

| Metric | Definition | Source |
|---|---|---|
| `mbps` | delivered payload bytes ÷ active seconds (not wire bytes) | subscriber final summary |
| `obj_s` | delivered objects ÷ active seconds | subscriber |
| `grp_s` | groups completed ÷ active seconds (= turnover rate) | subscriber |
| `p50/p95/p99/p99_9` | object delivery latency = receive time − LOC timestamp (property 0x10), per object | subscriber |
| `jitter_ms` | mean absolute successive difference of latency | subscriber |
| `lost`, `ooo` | counters | subscriber |
| `interval_cv` | stdev ÷ mean of per-interval `mbps` (steady-state stability) | per-interval rows |
| `ttff_ms` | time-to-first-object after SUBSCRIBE (late-join points only) | harness timestamp |
| `cpu_pub`, `cpu_sub` | process CPU seconds ÷ wall seconds (cores used) | `/proc/<pid>/stat` sampling |
| `rss_max_mb` | peak RSS per process | `/proc/<pid>/status` |
| `fanout_ratio` | relay egress ÷ ingress bytes | moqx stats or per-sub sums |
| `relay_cpu` | relay process CPU cores used | moqx pid |

**Tail latency is the headline metric.** Report p99 and p99.9 from a run of
≥ 120 s for claims; 60 s runs are screening only. Unpaced (`-r 0`) latency is
queue depth, not stack latency — always pair it with the TX budget in the label
(DATAFLOW §4). Cross-host absolute latency is corrected by the measured clock
offset (§5.6); jitter and interval CV are offset-free.

---

## 4. Topologies

| ID | Shape | Where | What it isolates |
|---|---|---|---|
| **L** | `loopback_bench` — pub+sub in one process, no relay | shenzhen | stack upper bound; shares one CPU pool |
| **P** | `pub_server` + `sub_bench` — two processes, no relay | shenzhen | per-side CPU isolation without network |
| **D** | `pub_server` (shenzhen) → `sub_bench` (seawolf) | 2 nodes | transport+MoQT over a real path, no relay |
| **R** | publisher co-located with moqx (shenzhen) → N subs (seawolf) | 2 nodes | relay egress + fan-out — the subscriber path we ship |
| **Ri** | publisher on seawolf → moqx (shenzhen) → sub co-located | 2 nodes | relay ingress |
| **F** | 1 publisher → N subscribers through moqx | 2 nodes | fan-out scaling and churn |
| **M** | media profile (1080p/720p GOP) through moqx | 2 nodes | video-shaped objects, not flat streams |

Loopback is an **upper bound and a build-health check**, never evidence about the
network path. Two-node numbers are the ones a deployment sees.

---

## 5. Environment freeze

### 5.1 Hosts

| Role | Host | Cores | Notes |
|---|---|---|---|
| relay + publisher | shenzhen, WSL2 (10.14.25.173) | 16 logical | mirrored/bridged LAN IP; inbound UDP works |
| load host | seawolf (10.14.25.37) | 6 | shared with llama.cpp on :8080 — must be idle, guarded |
| dev/verify | argo (macOS) | — | review, spot checks; never a benchmark arm |

The pair is expected to be **1 GbE-class**. Verify the link rate; if the driver
does not report it, record measured goodput instead. A plateau at line rate is
the NIC, not the stack — label it.

### 5.2 Pinning map (constant for the whole program)

- shenzhen: relay `0-1`, publisher `2-5`, everything else `6-15`.
- seawolf: subscriber workers `0-3`, everything else `4-5`.
- Fan-out points: subscriber processes share the load-host budget — record the
  process count and note the CPU constraint in the result. Do not compare a
  20-subscriber point against a 1-subscriber point as if only fan-out changed.

### 5.3 Host tuning (per `docs/bench-runbook.md`; verify, don't assume)

Persistent sysctls (record values, do not silently change): `rmem_max/wmem_max`
64 MiB, defaults 8 MiB, `udp_mem`, `netdev_max_backlog 250000`,
`netdev_budget 1200`, `netdev_budget_usecs 12000`, `optmem_max 1 MiB`,
`nofile 1048576`.

Non-persistent (record and restore): qdisc `fq_codel` (not `fq`) on the measured
interface, `txqueuelen 20000`, NIC rings at max, RPS off unless single-queue.
`hostcheck.py` re-reads all of it before and after every point.

### 5.4 Allocator / build

Baseline runs use the default allocator. `LD_PRELOAD=jemalloc` is a **tweak arm**
(T7), never baseline — otherwise the baseline is not what users get.

### 5.5 Clock

Record the shenzhen↔seawolf offset with 20 NTP-style samples (min-RTT filter,
error = min RTT / 2) before each measurement block. Apply the offset to absolute
latency; keep the raw and corrected values in the result.

### 5.6 Arms

| Arm | Contents |
|---|---|
| `base-070rc2` | aiopquic `v0.7.0rc2` + aiomoqt `0.13.3` (editable, branch `perf/release-sweep`) |
| `cmp-061` | aiopquic `v0.6.1` + same aiomoqt tree |
| `tweak-<id>` | base + exactly one knob/patch, documented in `tweaks/<id>.md` |

Each arm is a wheel + venv under `perf-sweep/arms/<name>/`; `build_arm.sh` prints
the versions and records the wheel SHA-256. An arm whose version print disagrees
with its label is a bug, not a result.

---

## 6. Measurement protocol

1. **Noise floor first.** One D point (raw, P=4, s=4096, g=200, unpaced, 60 s)
   × 7 and one M point (1080p, 60 s) × 7. Compute run-to-run CV per metric.
   The minimum detectable effect (MDE) for each metric is published before any
   A/B is run; no claim smaller than its MDE.
2. **Repetitions.** Screening N=3, claims N≥5, alternating arms A/B/A/B to
   cancel drift. Never run all of A then all of B.
3. **Warmup** 5 s discarded; measure 60 s (120 s for tail claims).
4. **One variable at a time.** Any run with two differences is labelled
   exploratory and cannot support a verdict.
5. **Control point** every measurement block (a fixed D point). If the control
   drifts > MDE against its own history, the block is invalid — stop and record
   why.
6. **Abort rules:** seawolf inference slot busy, CPU steal > 2 %, qdisc changed,
   link renegotiated, free disk < 10 GiB, clock offset jumped > 2 ms.
7. **Resumable:** every point writes `result.json` atomically on completion;
   `sweep.py` skips valid results unless `--force`.
8. **No concurrent benchmark tasks.** `sweep.py` takes an exclusive `flock`;
   a second invocation waits or exits 75.
9. **Statistics:** report median and bootstrap 95 % CI; A/B significance by
   Mann-Whitney U on per-interval samples (not per-run medians alone). State the
   test and the n.
10. **Raw material kept:** pub/sub logs, counters dump, qlog only when the point
    is diagnostic (qlog costs ~half throughput — never on a timing point).

---

## 7. Baseline matrix v0

24 points; N=3 screening. ID prefix = topology (§4).

| ID | Config | Purpose |
|---|---|---|
| L1 | raw, P=1, s=4096, g=200, unpaced, 60 s | single-stream ceiling |
| L2 | raw, P=8, s=4096, g=200, unpaced, 60 s | multi-subgroup ceiling |
| L3 | WT, P=8, s=4096, g=200, unpaced, 60 s | WT vs raw at the ceiling |
| L4 | raw, P=8, s=4096, g=40, unpaced, 60 s | churn at the ceiling |
| L5 | raw, P=8, s=1200, g=200, unpaced, 60 s | small objects, pps-bound |
| P1 | raw, P=4, s=4096, g=200, unpaced, 60 s | two-process, no network |
| P2 | raw, P=4, s=4096, g=200, r=20000, 60 s | paced two-process |
| P3 | WT, P=4, s=4096, g=200, unpaced, 60 s | WT two-process |
| D1 | raw, P=1, s=4096, g=200, ~300 Mbps, 60 s | single stream on the wire |
| D2 | raw, P=4, s=4096, g=200, ~600 Mbps, 60 s | multi-stream on the wire |
| D3 | raw, P=4, s=4096, g=200, unpaced, 60 s | direct ceiling |
| D4 | raw, P=8, s=4096, g=40, unpaced, 60 s | churn at the direct ceiling |
| D5 | WT, P=4, s=4096, g=200, ~600 Mbps, 60 s | WT on the wire |
| R1 | raw, P=4, s=4096, g=200, 1 sub, ~200 Mbps, 60 s | relay egress, controlled |
| R2 | raw, P=4, s=4096, g=200, 1 sub, unpaced, 60 s | relay ceiling |
| R3 | raw, P=8, s=4096, g=40, 1 sub, unpaced, 60 s | relay + churn |
| R4 | raw, P=4, s=4096, g=200, 5 subs, ~200 Mbps agg, 60 s | fan-out 5 |
| R5 | raw, P=4, s=4096, g=200, 20 subs, ~400 Mbps agg, 60 s | fan-out 20 |
| R6 | WT, P=4, s=4096, g=200, 5 subs, ~200 Mbps agg, 60 s | WT fan-out |
| Ri1 | raw, P=4, s=4096, g=200, 1 sub, ~300 Mbps, 60 s | relay ingress |
| M1 | video 1080p, 1 sub, 120 s | media shape |
| M2 | video 720p, 5 subs, 120 s | media fan-out |
| C1 | `load_sim -f churn-storm`, 50 subs, 300 s | join/leave pressure |
| C2 | late join at t=30 s, 1080p, 1 sub, 90 s | TTFF and catch-up |

Draft 16 for every relay point (moqx's validated set); draft 18 is allowed on
direct points only, labelled. Transport pinned per point, never inferred.

---

## 8. Regression attribution (Phase 2)

1. Run the **reduced key set** (D3, R2, R3, M1, C1) on `cmp-061` and compare with
   the `base-070rc2` baseline: per-point delta, CI, and a first ranking of where
   the loss is.
2. **Bisect** the largest regression with `git bisect run` driving a fast
   discriminator: single point, 45 s, N=3 median, threshold = half the observed
   delta. One bisect per affected metric (throughput vs tail), because the
   first-bad commit may differ.
3. If the regression lands in the picoquic submodule, bisect the submodule pin
   the same way (build each step into its own wheel; ~10–15 min each, run
   detached).
4. Produce the **hardening ledger**: one row per suspect commit —
   *what it fixes, why it is on the hot path, measured cost, can it be deferred
   or narrowed?* This is the document the release decision actually needs.

Bisect is allowed to conclude "inconclusive" only with the evidence attached
(steps run, variance, why the discriminator could not separate).

---

## 9. Tweak queue (dataflow-guided)

Each entry: hypothesis tied to a dataflow location, one knob or patch, one
verdict. Acceptance: keep if throughput **+≥3 %** with p99 no worse than +5 %,
**or** p99 **−≥15 %** with throughput no worse than −2 %, at the designated
point, N=5, CI excluding zero. Otherwise discard — and still publish the number.

### Transport (aiopquic)

| ID | Knob | Hypothesis | Test points |
|---|---|---|---|
| T1 | `congestion_control_algorithm` bbr1/bbr/cubic/dcubic | bbr1's max-filter reads bursty service as bandwidth (DATAFLOW §4); cubic is the shared posture. Known paced-flow spikes. | D2, R1, M1 |
| T2 | `AIOPQUIC_SEND_LENGTH_MAX` 65535/16384/8192, `AIOPQUIC_GSO=0` | GSO batch size vs syscall batching; macOS path is per-datagram. | L2, D3, R2 |
| T3 | `AIOPQUIC_TX_RING_WAKE_PCT` 25/50/75 | wake earlier = lower per-send latency, more context switches (PERFORMANCE §TX wake threshold). | D2, R1 |
| T4 | `tx_max_queued_bytes` 1/2/4/8 MiB | aggregate gate sets standing queue ≈ 0.75×cap ÷ drain (DATAFLOW §4). Tail-vs-throughput trade. | D3, R3 |
| T5 | `event_ring_capacity` RX 8192/16384/32768 | overflow vs cache/memory at high stream churn; watch `rx_overflow_max_depth`. | L4, R3 |
| T6 | `max_streams_uni` 512→2048 + `max_stream_data` 16→4 MiB | MAX_STREAMS replenishment vs per-stream window at high subgroup counts; memory trade. | L4, R3 |
| T7 | jemalloc preload | allocator tail: documented sd 7.1→4.3 ms, max 437→310 ms. | R2 (120 s), M1 |
| T8 | one TransportContext per connection vs shared | worker-thread contention at fan-out; compare 1 vs N contexts. | R5 |

### MoQT (aiomoqt)

| ID | Knob | Hypothesis | Test points |
|---|---|---|---|
| M1 | P = 1/2/4/8/16 at fixed aggregate rate | where subgroup parallelism stops paying; per-stream queue fairness. | D2, R1 |
| M2 | g = 40/120/400/2000 at fixed rate | turnover cost per group (stream open/close, FC credit, scheduler churn). | L2, R2 |
| M3 | `tx_max_inflight_bytes` 256 KiB/1 MiB/4 MiB | per-stream budget below the aggregate gate (DATAFLOW §4 floor). | D3, R3 |
| M4 | priority scheduler on/off (commit 73ab653) | does declared publisher priority cost unpaced throughput / fairness? | L2, R3 |
| M5 | encode/parse path (microbench b1/b1b/b7) | GC pressure from per-object bytes allocs; `gc.freeze()`/threshold tuning. | microbench + M1 |
| M6 | fan-out: in-process fan-out vs relay | is the wall the relay or the Python fan-out? | R4/R5 |
| M7 | WT vs raw at identical point | WT send wake is unconditional (DATAFLOW §6); quantify. | D2/D5, R1/R6 |

---

## 10. Deliverables and cadence

```text
perf/release-sweep/
  PROGRAM.md              this charter
  STATE.md                live position (phase, arms, last run, next action)
  env-freeze.md           hosts, build, sysctls, link, clock
  manifests/*.yaml        point sets, versioned
  scripts/                build_arm.sh, sweep.py, hostcheck.py, collect.py, stats.py
  runs/<UTC>/<point>/     meta.json, pub.log, sub.log, counters.json, result.json
  baselines/<label>/      SUMMARY.md + CSV rollup
  regressions/            compare-*.md, bisect-*.md, hardening-ledger.md
  tweaks/                 <id>.md (hypothesis, change, numbers, verdict)
  REPORT-0.14.0-0.7.0.md  final
```

Cadence: weekly checkpoint note to Giovanni (what ran, what it says, what is
next); STATE.md updated at the end of every run, not just at task completion.
Raw `runs/` stay on disk; summaries and harness are committed on
`perf/release-sweep` and pushed (branch only).

---

## 11. Guardrails

- **No external communication.** No PRs, issues, or posts to GitHub, IETF, MSF,
  MoQ, or any public forum without Giovanni's explicit approval.
- **No changes to `main`, release branches, version files, or tags** in either
  repo. Work happens on `perf/release-sweep` (and `tweak/<id>` branches), pushed
  as branches only.
- **Do not touch the shared moqx relay** on `:9668` or its config. The program
  runs its own moqx instance on `:9768` from a pinned config.
- **Do not benchmark while seawolf's inference slot is processing**, and do not
  stop or restart the inference service without asking.
- **Restore host state** (qdisc, txqueuelen, RPS, sysctls) after every block;
  verify clean rather than assume.
- **No cherry-picked runs.** Report median + CI, including discarded points.
- **No fabricated metrics.** Missing measurement = "not measured".
- Anything irreversible, or any change outside `perf/release-sweep/` and the
  dedicated branches, comes back to Giovanni first.

---

## 12. Release gate (proposal, for Giovanni to confirm)

Relative to `0.13.3 / 0.6.1` on the same points:

| Gate | Threshold |
|---|---|
| Throughput, paced points | no point < −5 % (median, CI) |
| Throughput, saturation points | no point < −8 % without a named cause |
| Tail latency, paced points | p99 ≤ +10 % or +2 ms, whichever is larger |
| Subgroup scaling | knee not earlier than 0.13.3 at equal aggregate rate |
| Group turnover | no loss > 1 %, no p99.9 blow-up at equal groups/s |
| Fan-out 20 | 0 loss at target bitrate; TTFF p99 < 2 s |
| Stability | interval CV < 3 % at paced points |

A gate failure is not automatically a blocker — it is a decision with a number
attached, and the hardening ledger explains which side of the trade it sits on.

---

## Appendix A — result schema

```json
{
  "point_id": "R2",
  "arm": "base-070rc2",
  "topology": "R",
  "transport": "raw",
  "draft": 16,
  "config": {"P": 4, "s": 4096, "g": 200, "r": 0, "t": 60},
  "hosts": {"relay": "shenzhen", "load": "seawolf", "subs": 1},
  "shas": {"aiomoqt": "1a444c0", "aiopquic": "v0.7.0rc2", "picoquic": "<sha>"},
  "env": {"cc": "bbr1", "jemalloc": false, "gso": true, "qdisc": "fq_codel",
          "pinning": "relay 0-1 pub 2-5 sub 0-3", "clock_offset_us": 42},
  "started_utc": "...", "duration_s": 65,
  "samples": [{"t_s": 1, "mbps": 0, "p50": 0, "p99": 0}],
  "summary": {"mbps": 0, "obj_s": 0, "grp_s": 0, "p50": 0, "p95": 0, "p99": 0,
              "p99_9": 0, "jitter_ms": 0, "lost": 0, "ooo": 0,
              "interval_cv": 0, "cpu_pub": 0, "cpu_sub": 0, "rss_max_mb": 0},
  "artifacts": ["pub.log", "sub.log"]
}
```

## Appendix B — canonical commands

```bash
# loopback (upper bound)
python -m aiomoqt.tools.loopback_bench -P 8 -s 4096 -g 200 -t 60 -q

# two-process, no relay
python -m aiomoqt.tools.pub_server -P 4 -s 4096 -g 200 -r 20000 -n aiomoqt
python -m aiomoqt.tools.sub_bench moqt://127.0.0.1:4434 -n aiomoqt --draft 16 -k -t 60 -i 5

# through a relay
python -m aiomoqt.tools.pub_bench moqt://10.14.25.173:9768 -s 4096 -P 4 -r 8000 -t 60 --pub-both
python -m aiomoqt.tools.sub_bench moqt://10.14.25.173:9768 -i 5 -t 60

# media shape
python -m aiomoqt.tools.pub_bench moqt://10.14.25.173:9768 -N <ns> -T v1080p --video 1080p --draft 16 -k -t 120
python -m aiomoqt.tools.sub_bench moqt://10.14.25.173:9768 -N <ns> -T v1080p --draft 16 -t 120 -i 5

# churn
python -m aiomoqt.tools.load_sim https://10.14.25.173:9768/moq-relay -f churn-storm --draft 16 -k --report churn.csv
```

Flags drift between versions — verify with `--help` before every phase; the
manifest records the exact argv used.
