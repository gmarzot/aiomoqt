# STATE.md — release-sweep program state

| field | value |
|---|---|
| phase | **0 — setup, frozen arms, first smoke: COMPLETE** |
| program | `perf/release-sweep` (charter: `PROGRAM.md`, this directory) |
| workspace root | `/home/gmarzot/Projects/marzresearch/perf-sweep` |
| host | SHENZHEN — Ubuntu 24.04.1 on WSL2, i9-14900KF (16 vCPU) |
| date | 2026-10-28 |
| next action | **Phase 1 — harness + baseline matrix v0** (PROGRAM.md §7) |

Arms are frozen and reproducible: `arms/<NAME>/{src,wheels,venv,arm.json,versions.txt,build.log}`,
built by `scripts/build_arm.sh NAME REF` (which refuses to rebuild an arm that
already has `arm.json`). Environment and build recipe: `env-freeze.md`.
Raw captures: `logs/env-freeze-raw.txt`, `logs/env-raw.txt`, `logs/smoke/`.

## 1. Repositories and pins

| repo | role | ref | sha | describe |
|---|---|---|---|---|
| aiomoqt | code under test (both arms) | `origin/main` → branch `perf/release-sweep` | `1a444c0f1bc055738ec7cd4b466691b524fb9ede` | `0.13.3` |
| aiopquic | `base-070rc2` arm | `v0.7.0rc2` (detached worktree) | `51a80c9721609d1bc41f6debd36ae2e4f013da88` | `v0.7.0rc2` |
| aiopquic | `cmp-061` arm | `v0.6.1` (detached worktree) | `ccf37d8ab0b9fa2919298665779daa3b681642e2` | `v0.6.1` |

`aiomoqt origin/main` is **`1a444c0` = tag `0.13.3`**, exactly the SHA the card
expected. No drift to report.

Submodules (identical picotls/liburing, **different picoquic**):

| submodule | base-070rc2 | cmp-061 |
|---|---|---|
| picoquic | `70b5fd5aafdd2d6c349ff783202ece9d778bcaf2` (1.1.53.0, 2026.10.22) | `c9088724e02f8cab955107cd5b5cd726267c751b` (1.1.53.0, 2026.10.08) |
| picotls | `f06553b877ada93c860dd1db9002eabf7365292d` | same |
| liburing | `5227d48b28ad8671e61d444b72678da584d2e6c3` | same |

The aiomoqt clone is `perf-sweep/aiomoqt` (local clone of
`https://github.com/gmarzot/aiomoqt`, `origin` = GitHub, credentials present).
The aiopquic clone is `perf-sweep/aiopquic`; both arms are **detached git
worktrees** of it, so a single object store backs both and the dev checkouts
under `~/Projects/marzresearch/{aiomoqt,aiopquic}` were never touched.

## 2. Arms

| arm | aiopquic | wheel sha256 | interpreter |
|---|---|---|---|
| `base-070rc2` | 0.7.0rc2 | `35a13fd69e27c60b82a727bf4254e09b4b5e120d648a032d31259669419bd31c` | CPython 3.14.6 |
| `cmp-061` | 0.6.1 | `3220fb9f2dfdebadd6ee810ab20a5b2a56336718b16fc5eb70bcfd4898246f05` | CPython 3.14.6 |

Both print `aiomoqt 0.13.3` (same editable tree) and their own aiopquic version,
with `aiopquic.__file__` inside the arm's venv — no cross-arm leakage.
Verbatim version prints are in `env-freeze.md` §6.

`cmp-061` (v0.6.1) built cleanly on CPython 3.14.6; the documented
`python3.12` fallback was **not** needed.

## 3. Smoke results

### 3.1 Relay probe — PASS

```
cmd: arms/base-070rc2/venv/bin/python -P -m aiomoqt.tools.relay_probe \
       --url moqt://127.0.0.1:9668 -k
out: moqt://127.0.0.1:9668   QUIC   ✓  draft-14,draft-16  (279ms)
rc=0
```

Two corrections to the literal card command, both recorded rather than
silently applied:

1. **`-k` is required.** The shared relay runs `tls.insecure: true`
   (self-signed), so without `-k` the handshake fails on certificate
   verification — an artefact of the relay config, not a MoQ result. The
   original failure is preserved as
   `logs/smoke/relay_probe.no-insecure.attempt1.log`.
2. **`--draft` was left unset** so the probe offers its whole set; it
   negotiated `draft-14,draft-16` in a single session (279 ms).

Relay state (`/tmp/moqx-smoke.log`, `/proc/1492225`): the process started
2026-10-22 from `build/moqx`, whose on-disk binary is newer (mtime 2026-10-25)
and `/proc/1492225/exe` is already `(deleted)` — i.e. **the running relay is an
older build than the tree**. A forced-ALPN probe showed it accepts `moqt-16`
and rejects `moq-00`, `moqt-14`, `moqt-18`, `moqt-16-meta-05`, `h3`: a
pre-standard-ALPN stack. It is therefore **not a draft-18 peer** (open question
O2). No restart, no reconfiguration — read-only, per the card.

### 3.2 Loopback bench — PASS

```
cmd: arms/base-070rc2/venv/bin/python -P -m aiomoqt.tools.loopback_bench \
       -Q --cert certs/cert.pem --key certs/key.pem -P 8 -s 4096 -g 200 -t 10
rc=0
```

Delivered objects present, p99 present:

| metric | value |
|---|---|
| groups | 1,676 |
| **objects delivered** | **335,125** |
| bytes | 1,377,028,625 |
| throughput | **1068.50 Mbps** (133.56 MB/s) |
| latency | p50 24.6 / p95 35.9 / **p99 39.5** / max 51.1 ms |
| jitter | 0.11 ms |
| loss / out-of-order | 0 / 0 |
| negotiated | draft 14, raw QUIC, subgroup streams, bbr1 |

Deviations from the literal command, both required:

1. `-q` is **not a flag** of `loopback_bench` (usage offers `-Q/--quic` and
   `-W/--wt`, default `-W` = H3/WebTransport). Interpreted as "validate the
   raw-QUIC path" and run as `-Q`; a bare `-q` run produced no statistics.
2. `--cert`/`--key` were needed because the built-in default test cert is not
   present on this host; a self-signed localhost cert was generated under
   `perf-sweep/certs/` (CN=localhost, SAN `DNS:localhost,IP:127.0.0.1`) rather
   than borrowing the dev checkout's.

**First real finding — the latency is the queue budget, not the network.**
The tool's default `--max-queued-bytes` is 4 MiB. At the measured 133.56 MB/s
that is a standing latency of `4 MiB / 133.56 MB/s ≈ 31.4 ms`; measured p50 is
24.6 ms and p99 39.5 ms. So on loopback the observed latency is dominated by
aiomoqt's aggregate TX budget, and `--max-queued-bytes` (1 MiB → ≈7.9 ms) is a
first-order lever, not a micro-tune. Phase 1 must sweep it explicitly at fixed
offered load, for both arms, before any latency comparison between arms is
meaningful.

## 4. Deviations from the card, in one place

| # | card said | did | why |
|---|---|---|---|
| D1 | `loopback_bench -q` | `-Q` | `-q` is not a flag; `-Q` = raw QUIC, the layer under test |
| D2 | (no TLS args) | `--cert certs/cert.pem --key certs/key.pem` | default test cert absent on host |
| D3 | `relay_probe --url ...` | `relay_probe --url ... -k` | relay is `tls.insecure: true` |
| D4 | interpreter | CPython 3.14.6 (uv) | 3.14.9 has no uv managed build |
| D5 | — | `--draft` unset on the probe | offer the full set; result records what negotiated |
| D6 | push to origin | `origin` URL switched to `ssh://git@github.com/gmarzot/aiomoqt.git` | HTTPS had no stored credentials (non-interactive); SSH key for `gmarzot` authenticates. Only the remote URL in this clone changed. |

No sysctl, qdisc, txqueuelen, governor or relay config was changed. Nothing
outside `perf-sweep/` was written; no version file, tag, `main` or release
branch was touched.

## 5. Next action — Phase 1 (harness + baseline matrix v0)

Per PROGRAM.md §7, the harness must:

1. Run `N` interleaved repetitions of **both** arms per cell (never arm-major
   blocks), with the pinning map from `env-freeze.md` §2 applied identically.
2. Sweep the baseline matrix at fixed offered load: `-P ∈ {1,8,32}`,
   `-s ∈ {1200,4096,65536}` and `--max-queued-bytes ∈ {0,1MiB,4MiB}` (see §3.2),
   `-t 30`, `-i 5`, `-Q`, draft 14 first.
3. Emit the Appendix A result schema per cell: arm, sha, wheel sha256, run
   index, throughput, p50/p95/p99, loss, ooo, jitter, plus the env hash so a
   row can never be attributed to the wrong environment.
4. Fail loudly on: non-zero loss, arm-version mismatch, or a missing p99 — the
   same checks the smoke just used.

Relay-path work (draft-18, fan-out) stays out of Phase 1 until O2 is resolved.

## 6. Open questions

| # | question | who decides / how |
|---|---|---|
| O1 | **picoquic is confounded with the arm.** v0.7.0rc2 bundles 2026.10.22, v0.6.1 bundles 2026.10.08. Any base-vs-cmp delta is "aiopquic tag + 2 weeks of picoquic". Add a control arm (`v0.7.0rc2` with picoquic `c908872`, or `v0.6.1` with `70b5fd5`) before Q1 attribution. | Giovanni: is a 3rd control arm in scope for Phase 1, or accept the confound and attribute only to the released-tag pair? |
| O2 | The running relay is a pre-standard-ALPN build (accepts `moqt-16` only, not `moqt-18`). Restarting it from the current `build/moqx` would fix that but is forbidden in Phase 0 and is a shared service. | Giovanni: schedule a relay restart window? Until then, no draft-18 / relay-path claims. |
| O3 | **Frequency is not freezable in WSL2** (no governor, no `no_turbo`). Absolute throughput/latency are not comparable across sessions or hosts. | Program stance: report A/B ratios measured in-session. Any absolute number needs a bare-metal host (argo/seawolf) — confirm whether that is ever required. |
| O4 | `net.core.rmem_max = wmem_max = 208 KiB` may cap throughput before the stack does at higher RTT. Both arms share the ceiling so the A/B holds, but a real deployment would not. | Phase 3a tweak card: raise per-run and re-measure *both* arms; never for one arm only. |
| O5 | `ulimit -n` soft = 4096 bounds how far `-P` can sweep. | Phase 1: set `ulimit -n 65536` per run and record it in the manifest. |
| O6 | `-P 8` at 4096 B reached only 1.07 Gbps on loopback — far below a 10 Gb/s synthetic link. Is the wall the queue budget (Q1 above), GSO/batching, or the single publisher thread? | Phase 1 scaling sweep answers it; do not tune before the baseline exists. |

## 7. Reproducing Phase 0

```bash
cd /home/gmarzot/Projects/marzresearch/perf-sweep
export ARM_PY_SPEC=/home/gmarzot/.local/share/uv/python/cpython-3.14-linux-x86_64-gnu/bin/python3.14
aiomoqt/perf/release-sweep/scripts/build_arm.sh base-070rc2 v0.7.0rc2
aiomoqt/perf/release-sweep/scripts/build_arm.sh cmp-061     v0.6.1
aiomoqt/perf/release-sweep/scripts/env_probe.sh            # -> logs/env-freeze-raw.txt
arms/base-070rc2/venv/bin/python -P -m aiomoqt.tools.relay_probe --url moqt://127.0.0.1:9668 -k
arms/base-070rc2/venv/bin/python -P -m aiomoqt.tools.loopback_bench -Q \
    --cert certs/cert.pem --key certs/key.pem -P 8 -s 4096 -g 200 -t 10
```

Builds refuse to overwrite an arm that already has `arm.json`; delete
`arms/<NAME>/` deliberately if a rebuild is intended.

## Phase 1a progress — harness built, smoke BLOCKED on host reachability

Delivered in `perf/release-sweep/`:
- `scripts/hostcheck.py` (stdlib-only; `--record`, `--require`, `serve-time`; qdisc,
  txqueuelen, rings, RPS, §5.3 sysctls, governor, disk, stray procs, llama guard,
  20-sample min-RTT clock offset). Runs locally and under `ssh seawolf`.
- `scripts/sweep.py` (manifest-driven; `--manifest/--only/--reps/--arm/--dry-run/
  --force/--resume/--out`; flock, PID files, SIGTERM->SIGKILL escalation,
  hostcheck pre/post, atomic per-rep `result.json`). `--dry-run` on
  `manifests/baseline-v0.yaml` prints all 24 points with argv.
- `scripts/collect.py` + `scripts/tests/test_collect.py` + captured fixtures
  (`sub_bench` has no machine-readable output on this branch; interval rows are
  parsed instead). Unit tests pass.
- `scripts/gen-certs.sh`, `scripts/bootstrap_seawolf.sh`,
  `manifests/baseline-v0.yaml` (24 points, §7), `manifests/smoke.yaml`,
  `moqx-perf.yaml` (udp/9768, endpoint /moq-relay, admin :9769, draft 16).

### BLOCKER: shenzhen (WSL2) accepts no inbound from the LAN
Verified three ways from seawolf -> 10.14.25.173: ICMP 100% loss,
UDP send to a bound socket `RECV_TIMEOUT`, TCP connect `TCP_TIMEOUT`.
Outbound shenzhen -> seawolf works (ssh, and the arm venv is usable there).
Consequence: the PROGRAM §7 direction "publisher/relay on shenzhen, subscriber on
seawolf" cannot open an MoQT session at all — `sub_bench` reports
`Connection failed: no session within 10s`. Windows Firewall / WSL2 NAT blocks
inbound; fixing it needs admin on the Windows host
(`netsh interface portproxy` + firewall rule) or WSL `networkingMode=mirrored`
plus an inbound allow rule — neither is available to a non-interactive worker.

### Next concrete step
1. Restore inbound reachability (operator action, see above) **or** decide to
   record mirrored roles (`pub`/relay on seawolf, `sub` on shenzhen) for D/R.
2. Rerun: `python3 perf/release-sweep/scripts/sweep.py --manifest
   perf/release-sweep/manifests/smoke.yaml --only D-raw --reps 1`, then D-WT,
   R-raw, R-WT (relay needs a moqx binary on seawolf: `build/moqx` is not there).
3. Validate the four `result.json` against Appendix A and commit `README.md`.

Also note: remote arm venv was missing `sortedcontainers` + build deps; installed
from the local arm freeze (`uv pip install -r`). `bootstrap_seawolf.sh` must be
fixed to install the full arm freeze, not just the two wheels.

## P1a status (2026-10-28, 18:15Z)

Harness is complete and committed (`66af34b`, pushed to `perf/release-sweep`).

Smoke results in `perf/release-sweep/runs/2026-10-28T17-58-58Z/`:
- `D-raw` **ok** — 763.67 Mbps, 6 interval samples, hostcheck pre/post, shas recorded
- `D-WT`  **ok** — 734.06 Mbps, 6 interval samples
- `R-raw` **aborted_pre** — `relay_probe failed rc=1 after 10 attempts`
  (`moqt://10.14.25.173:9768  QUIC ✗ connection refused - no response (8001ms)`)
- `R-WT`  not reached (same pre-check).

Manual control, same config file, same binary:

    moqx serve --config perf/release-sweep/moqx-perf.yaml &
    relay_probe --url moqt://10.14.25.173:9768 --draft 16 -k   # => draft-16 (128 ms)

so the pinned config, the port and the firewall path are fine; the difference is
*how the sweep starts the relay*. The rep directory contains no relay log at all,
so the failure is not observable from the artefacts — that is the next defect to
fix: capture relay stdout/stderr to `<rep>/relay.log` in the relay step, restart
the R point, and read the log. Prime suspects, in order:

1. the harness relay step exits immediately (config path resolved relative to a
   different cwd than the repo root, or the stray-bench guard reaping it), or
2. the relay binds only after the process is reaped by `Host.kill` on a
   short-lived step, or
3. `url_for()` builds a `moqt://` URL while the relay listener for that point is
   expected on the WebTransport path.

Next concrete step: add the relay log capture (one-line change next to
`Host.start` for the relay step), rerun
`sweep.py --manifest manifests/smoke.yaml --only R-raw,R-WT --force`, and only then
mark the smoke complete.

Known-good acceptance items: `--dry-run` prints all 24 baseline points with argv;
`--only D-raw --reps 1` passes end to end; collect parser unit test passes;
no orphan bench processes on either host after the D runs.

## P1a status (2026-10-30, 18:35Z) - harness complete; four topology smokes ran (status dirty = waived violations)

Block `runs/2026-10-30T18-28-46Z-smoke-final/` (gitignored); committed copies of the four
result.json files are in `perf/release-sweep/smoke-results/2026-10-30T18-28-46Z-smoke-final/`.
All four pass `scripts/validate_result.py` (Appendix A + honesty rules). Smoke only, no tuning.
D rows are unpaced (r=0, queue-depth regime), R rows are paced (r=8000, Appendix B shape).

| point | status | Mbps | obj/s | lost | cpu pub/sub/relay % | peak RSS MB | measured s |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D-raw | dirty | 804.2 | 24464 | 0 | 62.8 / 148.1 / - | 224.2 | 33.0 |
| D-WT | dirty | 783.1 | 23823 | 49 | 61.5 / 167.3 / - | 394.1 | 31.4 |
| R-raw | dirty | 249.0 | 7576 | 0 | 19.2 / 27.4 / 23.6 | 43.5 | 29.5 |
| R-WT | dirty | 247.1 | 7516 | 0 | 19.5 / 29.7 / 23.4 | 50.5 | 29.1 |

Latency (p50/p95/p99/lat_*) is **not measured** in all four: see blocker 1. p99.9 is **not measured**
(sub_bench prints p50/p95/p99 only). cpu_* is the mean over the process lifetime (includes connect).

### What changed in this round
- Clock: the first harness applied the cross-host offset with the wrong sign, never started the TCP
  clock responder (so the offset silently became 0), and judged drift on single-host NTP whose error
  (~9 ms) exceeds the 2 ms tolerance. Now: sweep.py runs the responder on shenzhen, seawolf probes it
  (20 samples) before and after every point, correction = measured - mean(pre, post), and latency is
  nulled with a reason when the probe fails or the offset moves more than max(tol, pre+post error).
  Sign is covered by tests/test_clock.py.
- Remote pid bug: the ssh launch backgrounded an `a && b && c` list, so `$!` was a wrapper shell;
  kill/alive//proc sampling hit the wrapper while the real process lived on (an orphan whenever a
  remote process did not self-exit). Now the pid is the real process (own session); verified with a
  probe: kill leaves nothing behind.
- seawolf abort reasons (busy llama.cpp slot) were never evaluated; now enforced and never waivable.
- `--smoke` / `--waive`: record-but-do-not-enforce stray_bench_process, clock_offset_jump,
  clock_unavailable; point becomes dirty. Foreign bench-pattern processes are listed, never touched;
  only our own interpreter outliving a point counts as an orphan (killed).
- Short-run guard: a subscriber that measured under 80 percent of the window is `failed`, not `ok`.
- Appendix A completeness: shas now carry real aiopquic/picoquic/picotls/liburing SHAs (was
  "unknown"), per-process cpu/rss from /proc, every null explained in summary.not_measured.
- bootstrap_seawolf.sh installs the FULL arm freeze (sortedcontainers was missing) and verifies
  freeze, CPython, wheel sha256 and imports; modes ensure / --rebuild / --verify-only. Run: identical.

### Blockers and findings for P1b (read before the baseline)
1. **One-way latency is not measurable on this host pair right now.** The seawolf-minus-shenzhen
   clock offset moves by hundreds of ms to seconds inside one 30 s point and steps (R-raw: 1.63 s to
   0.29 s). Raw LOC-timestamp latencies are dominated by it (kept only under
   meta.diagnostics.raw_latency_uncorrected_do_not_quote). Needs host-level time discipline on both WSL2
   VMs (admin), or a same-host pub/sub pair for latency. Tail latency is the headline metric, so this gates P1b.
2. **The host is not exclusive.** Jobs from the moq-analytics board (load_sim, moqx serve, regression
   runners) were running on shenzhen during every point (meta.foreign_bench_procs). The smoke waived
   stray_bench_process; a baseline run will abort until the host is quiet or the work is scheduled.
3. **Unpaced relay run fails**: P=4 s=4096 r=0 through moqx made the relay log "Error parsing/consuming
   stream" and reset the subscriber stream (error=3) after 1.2 s. Evidence:
   `smoke-results/R-raw-unpaced-relay-reset-evidence/`. Triage before any R point with r=0.
4. Tuning (PROGRAM 5.3) was NOT applied (smoke): qdisc is mq/pfifo_fast, not fq_codel. hostcheck reports
   eth1 at 10000 Mbps (virtual) while goodput plateaus near 800 Mbps; do not read the plateau as the stack.
5. aiomoqt provenance: shenzhen runs the editable tree at HEAD, seawolf runs the wheel built from
   ed6dbdc; the only non-perf file that differs between them is .gitignore. Wheel sha256s are in shas.

Next concrete step: resolve blocker 1 (time discipline) and 2 (exclusive host or schedule), then P1b
baseline on manifests/baseline-v0.yaml without --smoke.

## P1b status (2026-10-30, 22:39Z) — noise floor + L/P baseline delivered; cross-host latency still blocked

| field | value |
|---|---|
| phase | **1b — noise floor + loopback/two-process baseline: DELIVERED** |
| completed points | Part A: N1 ×7, N2 ×7 (`runs/2026-10-30T21-40-50Z-partA`) · Part B: L1–L5, P1–P3 ×3 (`runs/2026-10-30T22-04-13Z-partB`, 24/24 `ok`) |
| noise floor | `noise-floor.md` — N1 mbps CV 2.38 % → MDE(N=3) **3.8 %**; N2 mbps CV 0.27 % → MDE(N=3) **0.43 %**; `interval_cv` unusable at N<10 (CV 72 %/36 %) |
| summary | `baselines/base-070rc2/SUMMARY-part1.md` (L/P table, loopback caveat, same-host latency CVs) |
| next action | **Phase 1c** — resolve the clock blocker, then D/R rows (D1–D5, R1–R6, Ri1) + M1/M2 at 120 s |

**What P1b established**

1. The process boundary is free: L2 1731.8 vs P1 1728.2 Mbps (0.2 %), L3 vs P3 +1.7 % — both inside
   the N=3 floor. The two-process harness path needs no correction; L/P rows with matching shapes
   can be pooled.
2. Latency at the ceiling is still the 4 MiB queue budget (`≈19 ms` predicted, p50 15.1–15.7 ms
   measured at 1.73 Gbps). `--max-queued-bytes` remains the first-order latency lever; L4/L5
   (p50 ≈ 1 ms) confirm it because they never fill the queue.
3. Churn costs throughput, not tail (L4 −15.8 % vs L2 at unchanged p99); small objects are
   pps-bound but healthy (L5 97 k obj/s, p99 7.3 ms); the pacer is exact (P2 20,000.2 obj/s, CV 0.0 %).
4. **First rep of a block is cold** (L1 rep-01 −18 %). Screening blocks should run one throwaway
   warmup rep, or flag rep-01, on any point that opens a block.
5. Same-host p99 is the noisy metric (CV 1.6–46 % across points, worst at churn/small objects):
   same-host p99 claims need N≥5, churn p99 needs N≥10.

**Blocker 1 (clock) is now root-caused but not fixed.** A 20-sample probe sequence 2 s apart on a
4 ms RTT link returned offsets of −912 ms, −990 ms, −1058 ms, −27 ms, −96 ms — physically impossible,
so the `tcp-peer` estimate is unreliable (WSL2 `time.time()` is the suspect; `timedatectl` reports
synced). Part A attempts 1 and 2 aborted on `seawolf:clock_offset_jump`; attempt 3 ran with
`--waive clock_offset_jump`, giving 14 screening-grade `dirty` reps with `latency: null` and an
explicit note per rep. **Cross-host p50/p99 is not measurable on this host pair** until either the
clock is disciplined on both ends (admin on two WSL2 VMs) or latency is redefined from the QUIC
stack's own RTT. Same-host L/P latency is unaffected and is reported above.

**Blocker 2 (exclusive host) did not recur**: no foreign bench process appeared during either block,
and no waiver was needed for `stray_bench_process`.

Harness changes made during P1b (all committed): `--gap` (protocol 10 s idle, skipped after the last
rep), L-topology `--cert/--key/--draft` plumbing, L subscriber summary parsed from `bench.log`,
CPU fallback for single-process points, two warm-up remote hostcheck probes and an implausible-offset
re-probe to stop a stale first reply poisoning the clock baseline. New scripts: `noise_floor.py`,
`summarize.py`. Manifests: `noise-floor.yaml`, `phase1b-partb.yaml`, `phase1b-smoke.yaml`.

## Phase 1c — two-node direct baseline (D1–D5 + Ri1) — DONE

Run block `runs/2026-10-30T22-56-13Z-p1c2` (tag p1c2), 18/18 reps recorded, N=3 x 6 points,
60 s / 10 s gaps. Report: `baselines/base-070rc2/SUMMARY-part2.md`.

Delivered (subscriber side, Mbps mean +/- sd): D1 286.49 +/- 1.02 (CV 0.35%), D2 576.87 +/-
2.37 (0.41%), D3 812.11 +/- 30.20 (3.72%), D4 787.14 +/- 3.93 (0.50%), D5 569.69 +/- 0.25
(0.04%), Ri1 317.94 +/- 0.42 (0.13%). Publisher-side met every command (301/602/602).

Ceiling: **the 1 GbE NIC**. D3 = 812 Mbps payload (856 pub-side) while the Windows Realtek
GbE adapter (the physical NIC owning 10.14.25.173) counted 836.6 Mbps TX in a 45 s window =
89% of the 941 Mbps MTU-1500 payload ceiling; CPU 0.61 core/16 (pub) and 1.4 cores/6 (sub),
steal 0.00%; loopback on the same stack does 1731.8 Mbps (2.1x); P=8 unpaced (D4) does not
exceed P=4 unpaced (D3). Note both ends are WSL2 guests whose sysfs reports a cosmetic
10 Gb/s vNIC - do not trust it for link-rate math.

Cost vs loopback: -53.1% (1731.8 -> 812.1); RTT 4.25-5.96 ms (min/avg/max, 5 pings) is
dominated by the WSL2/Hyper-V virtual path, not the switch. Churn at the ceiling is -3.1%
(D4 vs D3) versus -15.8% on loopback. WT costs -1.3% (D5 vs D2).

Still open: cross-host latency percentiles (p50/p95/p99 null on all 18 reps; tcp-peer clock
offset moved up to 393 692 us inside one 60 s point against a 2000 us tolerance, so every rep
is `dirty` on clock alone). Throughput/CPU unaffected (per-host monotonic clock). Resolve
clock discipline before any latency claim.

Manifest fix in this phase: `r` was kbit/s (300000) but every aiomoqt tool takes objects/sec,
so paced points silently ran unpaced; rescaled (200M=6104, 300M=9155, 400M=12207, 600M=18311)
and documented in the manifest header. Pre-fix block 2026-10-30T22-52-58Z-p1c must not be cited.

Next action: Phase 1d (relay egress) - see PLAN/board; carry the clock-discipline item forward.
