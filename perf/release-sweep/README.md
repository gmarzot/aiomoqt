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
scripts/tests/               parser unit test + captured fixture logs
runs/<UTC>/<point>/rep-NN/   result.json, pub/sub/relay logs, hostcheck JSON
```

## Prerequisites

```sh
# one-off, both hosts: frozen arm venv + repo mirror on seawolf
perf/release-sweep/scripts/gen-certs.sh
perf/release-sweep/scripts/bootstrap_seawolf.sh
```

The arm venv on shenzhen is `arms/<arm>/venv`; on seawolf it is
`/home/gmarzot/perf-sweep/venv`. Both must import `aiomoqt`, `aiopquic` and
`sortedcontainers`.

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
`--force`, `--resume`, `--out DIR`, `--timeout-margin`, `--kill-grace`,
`--hostcheck-samples`, `--clock-peer`, `--iface`, `--cc`, `--gso`.

`--dry-run` expands the manifest and prints each point's argv without executing
anything — use it to check a manifest edit.

## Resume semantics

Results are written per point **and** per repeat, atomically (temp file +
`rename`), so a killed run loses at most the in-flight repeat.

* `runs/<UTC>/<point>/rep-NN/result.json` with `"meta": {"status": "ok"}` is a
  valid result.
* A new invocation creates a fresh `runs/<UTC>/` block. `<UTC>` is the block
  start time, so distinct invocations never overwrite each other.
* `--resume` skips a (point, rep) only when a valid `result.json` for it already
  exists **in the same block**; everything else runs. Use it to continue an
  interrupted block: pass the same `--out`/block by re-running the same command
  after a `SIGKILL` — the block directory is reused when `--resume` is set.
* Without `--resume` and without `--force`, points that already have a valid
  result in the current block are skipped (same effect as `--resume`).
* `--force` re-runs everything, overwriting existing results.

Two sweeps can never overlap: `sweep.py` takes an exclusive `flock` on
`<out>/.sweep.lock` and exits **75** if another sweep holds it.

## Read a result

`runs/<UTC>/<point>/rep-NN/result.json` follows Appendix A:

| key | meaning |
| --- | --- |
| `meta.status` | `ok`, `failed`, `timeout`, `skipped` |
| `meta.reason`, `meta.notes` | why it failed / what was suspicious |
| `meta.arm`, `meta.sha`, `meta.wheel_sha256` | which code was measured |
| `meta.config` | transport, draft, cc, gso, gso segments, pinning |
| `meta.argv` | exact argv per role, as executed |
| `meta.env` | kernel, python, NIC, qdisc, cpu governor fingerprint |
| `meta.hostcheck_pre/post` | host state before/after, incl. clock offset |
| `meta.clock_offset_us` | min-RTT NTP estimate, error = min RTT / 2 |
| `samples` | per-interval rows parsed from the subscriber log |
| `summary` | final summary block (throughput, loss, jitter, CV) |
| `duration_s` | wall time of the point |

A point whose tool printed no parseable summary is recorded as `failed` with
`reason = "no parsed summary from sub log"` rather than a silent `ok`.

`collect.py` re-derives the summary from a log (or a whole run directory) and
writes the CSV rollup used for charts:

```sh
python3 perf/release-sweep/scripts/collect.py runs/<UTC> --csv /tmp/rollup.csv
python3 -m pytest perf/release-sweep/scripts/tests -q
```

## How a timed point runs

`sub_bench` has no `--duration`: it runs until the publisher closes, and the
publisher never closes on its own. The harness therefore owns the measurement
window: it starts the subscriber, sleeps `warmup_s + duration_s`, then sends
`SIGINT` to the subscriber's process group so `sub_bench` takes its
`KeyboardInterrupt` path and prints the summary it already has. `kill -TERM`
would skip that path and lose the summary. Grace is `max(--kill-grace,
kill_grace_s)` (20 s for subscribers), then `SIGKILL`.

Remote processes are started in one ssh round-trip
(`cd <root> && nohup setsid <argv> >> log 2>&1 < /dev/null & echo $!`). Do not
poll a PID file over ssh here, and do not add `ControlMaster`: a persistent
master inherits the calling ssh's stdout/stderr, so `subprocess` capture never
sees EOF and every call blocks until its timeout.

Per-point hostchecks run with at most 3 NTP samples (the block-level record uses
the full `--hostcheck-samples`) because two 20-sample checks per point would
double the cost of a 30 s window.

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

`hostcheck.py --require` fails the run on the PROGRAM §6.6 conditions: busy
llama.cpp slot on seawolf, stray bench process, wrong qdisc/`txqueuelen`,
missing RPS/sysctl state, non-`performance` governor, low disk, or a clock offset
above `--clock-tolerance-us`. The same checks run before and after every point;
a pre-flight violation aborts the point instead of measuring a dirty host.

Check a host by hand:

```sh
python3 perf/release-sweep/scripts/hostcheck.py --samples 20 --record /tmp/hc.json --require
ssh seawolf 'python3 /home/gmarzot/perf-sweep/hostcheck.py --samples 20 --require'
```

`hostcheck.py serve-time --port 9876` turns the host into a clock peer so the
other side can measure offset without internet NTP.
