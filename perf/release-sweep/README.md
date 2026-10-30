# perf/release-sweep — measurement harness

Orchestration for the `aiomoqt` release sweep described in `PROGRAM.md`. This
directory holds the harness, not results: manifests describe *what* to measure,
`scripts/sweep.py` runs it, `scripts/collect.py` turns logs into the Appendix A
summary, and `runs/<UTC>/` holds one directory per point/repeat.

Two nodes are involved. `shenzhen` (this host, WSL2) runs the publisher/relay;
`seawolf` (LAN, reached over `ssh seawolf`) runs the subscriber and must be idle
(no llama.cpp slot busy) before every point.

## Layout

```
manifests/baseline-v0.yaml   the 24 PROGRAM §7 points
manifests/smoke.yaml         four 30 s topology smokes (D-raw, D-WT, R-raw, R-WT)
moqx-perf.yaml               pinned moqx relay config for the sweep (udp/9768, draft 16)
certs/                       self-signed cert/key for pub_server and moqx
scripts/hostcheck.py         host state + abort conditions, `--record`, `--require`
scripts/sweep.py             manifest driver (flock, PID files, atomic results)
scripts/collect.py           log parser + Appendix A summary + CSV rollup
scripts/validate_result.py   Appendix A + honesty-rule validator for result.json
scripts/bootstrap_seawolf.sh make/verify the seawolf venv == the local arm venv
scripts/tests/               parser, clock-sign and validator unit tests + fixture logs
runs/<UTC>/<point>/rep-NN/   result.json, pub/sub/relay logs, hostcheck JSON
```

## Prerequisites

```sh
# one-off: certs, then make seawolf's venv identical to the local arm venv
perf/release-sweep/scripts/gen-certs.sh
perf/release-sweep/scripts/bootstrap_seawolf.sh \
    /home/gmarzot/Projects/marzresearch/perf-sweep/arms/base-070rc2 \
    /tmp/aiomoqt-wheels/aiomoqt-0.13.4.dev3+ged6dbdc3c-py3-none-any.whl
# any time: change nothing, exit non-zero on drift
perf/release-sweep/scripts/bootstrap_seawolf.sh --verify-only <same two args>
```

The arm venv on shenzhen is `arms/<arm>/venv`; on seawolf it is
`/home/gmarzot/perf-sweep/venv`. The bootstrap installs the **full** pinned freeze
of the local arm venv (not just the two wheels - a wheels-only install once left
`sortedcontainers` out and aiomoqt unimportable on seawolf), then verifies: freeze
identical, same CPython, same wheel sha256, imports work. Modes: default *ensure*
(idempotent), `--rebuild`, `--verify-only`.

## Run one point

```sh
python3 perf/release-sweep/scripts/sweep.py \
  --manifest perf/release-sweep/manifests/smoke.yaml \
  --only D-raw --reps 1 -v
```

`-v` prints a timestamped line per phase (step start, pid, interrupt rc). This is
the fastest way to see where a point spends its time.

## Run a manifest

```sh
python3 perf/release-sweep/scripts/sweep.py \
  --manifest perf/release-sweep/manifests/baseline-v0.yaml \
  --arm base-070rc2 --out perf/release-sweep/runs
```

Flags: `--manifest`, `--only ID[,ID]`, `--reps N`, `--arm NAME`, `--dry-run`,
`--force`, `--resume`, `--out DIR`, `--tag SUFFIX`, `--smoke`, `--waive RULE[,RULE]`,
`--lock-wait SECONDS`, `--timeout-margin`, `--kill-grace`, `--hostcheck-samples`,
`--clock-peer HOST:PORT`, `--iface`, `--cc`, `--gso`.
Exit codes: 0 all points completed, 1 a point failed/aborted (its result.json is
written first), 75 another sweep holds the lock. SIGTERM/SIGHUP run the cleanup
path, so children are killed before the harness exits.

`--dry-run` expands the manifest and prints each point's argv without executing
anything — use it to check a manifest edit.

## Resume semantics

Results are written per repeat, atomically (temp file + `rename`), so a killed run
loses at most the in-flight repeat; everything finished before it is already on disk.

| invocation | block used | repeat that already has a result |
| --- | --- | --- |
| plain | new `runs/<UTC>[-<tag>]/` (never overwrites another invocation) | n/a - the block is empty |
| `--resume` | newest block under `--out` | **skipped** if `meta.status == "ok"` and the config fingerprint (arm, transport, config, duration) is unchanged; otherwise re-run |
| `--resume --force` | newest block | re-run and overwritten |

* Only `ok` counts as valid. `failed`, `timeout`, `aborted_pre`, `aborted_post` are
  retried by `--resume`. `dirty` (completed, but with waived violations or an
  orphan - see *Smoke mode*) is skipped only when `--smoke` is set; baseline work
  re-runs it.
* A failed or aborted repeat stops the manifest (exit 1) *after* writing its
  result.json, so the reason is on disk. Fix the cause and `--resume`.
* Two sweeps can never overlap: `sweep.py` takes an exclusive `flock` on
  `<out>/.sweep.lock` and exits **75** if another sweep holds it
  (`--lock-wait N` blocks up to N seconds instead).

## Read a result

`runs/<block>/<point>/rep-NN/result.json` follows PROGRAM Appendix A; check it with

```sh
python3 perf/release-sweep/scripts/validate_result.py runs/<block>/*/rep-*/result.json
```

| key | meaning |
| --- | --- |
| `point_id`, `arm`, `topology`, `transport`, `draft`, `config` | what ran (`config.t` = duration) |
| `hosts` | relay / pub / load host and subscriber count |
| `shas` | `aiomoqt` (repo HEAD), `aiomoqt_dirty` (changes *outside* `perf/`), `aiomoqt_dist`, `aiopquic` (+ `_sha`), `picoquic`, `picotls`, `liburing`, wheel sha256 for the local arm and for the copies on seawolf |
| `env` | cc, jemalloc, gso, qdisc, pinning, `clock_offset_us`, `clock_method`, `clock_error_us`, per-host fingerprint |
| `samples` | per-interval rows (`t_s, mbps, lat_ms, p50, p99`) |
| `summary` | Appendix A summary + counters. **A `null` is always explained in `summary.not_measured`.** |
| `meta.status` | `ok`, `dirty`, `failed`, `timeout`, `aborted_pre`, `aborted_post` |
| `meta.reason`, `meta.notes` | why a point failed / what was suspicious / which violations were waived |
| `meta.point_argv` | exact argv per role, as executed |
| `meta.hostcheck_pre_*`, `meta.hostcheck_post_*` | full host state before/after, both hosts |
| `meta.clock` | cross-host probe pre/post, applied correction, `latency_why` |
| `meta.latency_corrected` | `false` => every latency field is `null` (see below) |
| `meta.proc_stats` | per-process CPU % (of one core, over the process lifetime) and peak RSS from `/proc`; `summary.cpu_pub/cpu_sub/rss_max_mb` come from here |
| `meta.waived_violations`, `meta.smoke` | rules recorded but not enforced for this point |
| `meta.foreign_bench_procs`, `meta.orphans` | bench-pattern processes that are not ours / ours that outlived the point (killed) |
| `meta.diagnostics.raw_latency_uncorrected_do_not_quote` | raw latency when it could not be corrected; for debugging the clock only |

A point whose tool printed no parseable summary is recorded as `failed`
(`reason = "no parsed summary from sub log"`), never as a silent `ok`.

`collect.py` re-derives the summary from a log (or a whole run directory) and writes
the CSV rollup:

```sh
python3 perf/release-sweep/scripts/collect.py runs/<block> --csv /tmp/rollup.csv
python3 -m unittest discover -s perf/release-sweep/scripts/tests
```

## Clock offset and one-way latency (read before quoting any latency)

`sub_bench` reports `rx time (subscriber clock) - LOC timestamp (publisher clock)`.
With the publisher on shenzhen and the subscriber on seawolf that is
`true latency + (seawolf clock - shenzhen clock)`.

* `sweep.py` starts a TCP clock responder on shenzhen (`--clock-peer`, default
  `10.14.25.173:9876`) for the lifetime of the sweep. seawolf probes it **before and
  after every point**: 20 NTP-style exchanges, min-RTT filter, error = min RTT / 2.
* The correction applied is `corrected = measured - mean(pre, post)`. The sign is
  fixed by `tests/test_clock.py`; it was wrong in the first harness version.
* Latency is declared **not measured** (`p50/p95/p99/lat_*` = `null`, reason in
  `summary.not_measured` and `meta.clock.latency_why`) when the probe fails, or when
  the offset moved more than `max(--clock-tolerance-us, pre_error + post_error)`
  during the point. Throughput, loss, jitter, `lat_sd_ms` and CV do not depend on
  the offset and stay reported.
* Points whose publisher and subscriber share a host (loopback) need no correction.
* The probe error (~2 ms here) bounds what any cross-host latency can resolve.

## Smoke mode and waivers

`--smoke` is for proving the orchestration path, not for numbers anyone quotes. It
*records but does not enforce* `stray_bench_process`, `clock_offset_jump` and
`clock_unavailable`; every other rule (busy llama.cpp slot, low disk, steal, qdisc
or link change...) still aborts. A point that ran with a waived violation gets
`meta.status = "dirty"` and `meta.waived_violations`. `--waive RULE[,RULE]` waives
individual rules the same way. Baseline runs use neither.

Bench-pattern processes that are *not* running our arm interpreter belong to other
jobs on the host. They are listed in `meta.foreign_bench_procs` and never touched.
Only processes running our own interpreter that outlive a point are orphans: they are
killed, noted, and mark the point `dirty`.

## How a timed point runs

`sub_bench -t N` ends itself after N seconds (the manifest argv carries
`-t <duration_s>`), so it normally exits a fraction of a second before the harness
window (`warmup_s + duration_s`) closes, having printed its summary. If it is still
alive at window end the harness sends `SIGINT` to its process group so `sub_bench`
takes its `KeyboardInterrupt` path and prints the summary it has (`SIGTERM` would
skip that path), grace `max(--kill-grace, kill_grace_s)`, then `SIGKILL`. `/proc`
CPU/RSS of every process the point started is sampled 6 s before window end (the
subscriber is still alive) and once more at the end (pub/relay final values; a
process that is gone keeps its earlier sample).

Remote processes are started in one ssh round-trip
(`cd <root> && nohup setsid <argv> >> log 2>&1 < /dev/null & echo $!`). Do not poll a
PID file over ssh here, and do not add `ControlMaster`: a persistent master inherits
the calling ssh's stdout/stderr, so `subprocess` capture never sees EOF and every
call blocks until its timeout.

Per-point hostchecks: shenzhen runs <=3 NTP samples (absolute-time sanity only);
seawolf runs the llama.cpp guard and the full 20-sample TCP clock probe.

## Topologies

* **D (direct)** — `pub_server` on shenzhen, `sub_bench` on seawolf. The
  subscriber host is pinned to `seawolf` in the manifest; if it ran on the
  publisher's host the point would silently measure loopback.
* **R (relay)** — `moqx` perf instance on shenzhen:9768 from `moqx-perf.yaml`,
  publisher co-located, one subscriber on seawolf. `relay_probe` verifies the
  relay before each point.
* **L / P / Ri / M / C** — local-only or synthetic points; see `PROGRAM.md` §7.

The shared relay on `:9668` is never touched; the sweep uses its own instance and
kills it by PID on the way out.

## Abort conditions

`hostcheck.py --require` exits non-zero on the PROGRAM §6.6 conditions. `sweep.py`
runs the same checks on both hosts before and after every point:

| rule | fires when | note |
| --- | --- | --- |
| `llama_slot_busy` | a llama.cpp slot on seawolf is processing | never waivable by `--smoke` |
| `stray_bench_process` | a bench-pattern process is already running | waived by `--smoke` |
| `cpu_steal`, `disk_low` | steal above threshold / too little free disk | |
| `qdisc_changed`, `txqueuelen_changed`, `link_renegotiated` | differs between pre and post | |
| `clock_unavailable` | cross-host point and the TCP clock probe failed | waived by `--smoke` |
| `clock_offset_jump` | seawolf-shenzhen offset moved by more than `max(tolerance, pre+post error)` during a cross-host point | waived by `--smoke`; single-host points are exempt |

A pre-flight violation aborts the point instead of measuring a dirty host; a
post-flight violation marks it `aborted_post`.

Check a host by hand:

```sh
python3 perf/release-sweep/scripts/hostcheck.py --samples 20 --record /tmp/hc.json --require
ssh seawolf 'python3 - --samples 20 --require --guard-llama' < perf/release-sweep/scripts/hostcheck.py
```

`hostcheck.py serve-time --port 9876` is the clock responder `sweep.py` runs for you.
