# Load and benchmarking runbook

Finding where a relay stops coping, and saying so with numbers. A
feedback-driven ramp on one axis at a time, from a loopback self-test
that needs no setup up to a split run with the publisher beside the
relay and the load generator a network hop away.

Companion to [demo-runbook.md](demo-runbook.md), which covers real media
through the same stack.

## Before anything: paste this in every shell

```
cd $HOME/Projects/moq/aiomoqt
source .venv/bin/activate
export RELAY_WT=https://moqx-main.ci.openmoq.org:4433/moq-relay
export RELAY_QUIC=moqt://moqx-main.ci.openmoq.org:4433/moq-relay
export ASSETS=$HOME/Projects/moq/media-assets
```

Every `python -m aiomoqt.tools.…` line below needs that venv.

**No namespace is set by hand, and there is no `NS` to export.** Two
flows need a pair to agree, and each handles it without one:

- **Flow B** — `pub_bench` and `sub_bench` both default `-N` to
  `aiomoqt`, so omitting it on both makes them meet. `-T v1080p` on both
  is what actually picks the track out.
- **Flow E** — `pub_media` mints `aiomoqt/demo-<rand4>` and prints it.
  Paste that one printed namespace into the audience. `load_sim`
  rewrites the scenario to whatever `-N` it is given; it has no prefix
  discovery, so this is the one copy in the whole runbook.

moqx-main is shared. If someone else is benching, give flow B's pair a
namespace of your own — the same `-N aiomoqt/<something>` on both lines
— rather than sharing the default.

## Hosts, by role

Nothing here is tied to particular machines. A run needs at most two:

| Role | What it runs | Notes |
|---|---|---|
| **relay host** | the relay under test, plus the fixed-rate publisher in a split run | Co-locating the publisher keeps ingest off the network so the measurement is of egress and fan-out. |
| **load host** | subscribers and, in a split run, the whole control loop | One network hop from the relay, which is the path a real subscriber takes. |

Use a **dedicated pair for measured numbers**. Shared CI machines are
fine for functional runs — flows A, B, C and E below — but they build,
test and serve at unpredictable times, and a benchmark that shares a box
with a compiler is not a benchmark. Keep the pair matched in shape; an
asymmetric pair measures the smaller half.

## Host tuning

**Skip this and the numbers are fiction.** Half of it does not survive a
reboot or a resize and reverts silently, so an untuned host still
produces a plausible-looking result.

### Persistent (sysctl, survives reboot)

```
net.core.rmem_max = 67108864
net.core.wmem_max = 67108864
net.core.rmem_default = 8388608
net.core.wmem_default = 8388608
net.ipv4.udp_mem = 1048576 1572864 2097152
net.ipv4.udp_rmem_min = 262144
net.ipv4.udp_wmem_min = 262144
net.core.netdev_max_backlog = 250000
net.core.netdev_budget = 1200
net.core.netdev_budget_usecs = 12000
net.core.optmem_max = 1048576
```

Plus file descriptors, since every subscriber costs some:
`* soft nofile 1048576` and `* hard nofile 1048576`.

### Not persistent (device and sysfs, reset on reboot or reshape)

- **Queueing discipline — `fq_codel`, not `fq`.** This one matters more
  than it looks. `fq` classifies by socket, and a relay serves every
  subscriber from a single UDP socket, so all of them collapse into one
  flow capped at a 100-packet flow limit. `fq_codel` hashes the 5-tuple
  instead, giving each subscriber its own queue, so one backlogged peer
  cannot head-of-line everyone else. `pfifo_fast` is the neutral
  baseline; `fq` is the instructive bad comparison.

  ```
  tc qdisc add dev $IF root fq_codel limit 200000 flows 65536 \
      target 20ms interval 200ms memory_limit 536870912 noecn
  ```

  Target and interval are deliberately well above default. The relay is
  what is being measured, and codel dropping into a standing queue shows
  up as retransmits that belong to the AQM, not the relay. Raise `flows`
  and `limit` past the subscriber count for the same reason.

- **Transmit queue**: `ip link set dev $IF txqueuelen 20000`.

- **NIC queues and rings**: ask for one combined queue per CPU and take
  what the driver grants (virtio exposes a single queue on small shapes),
  then set the ring sizes to the hardware maximum.

  ```
  ethtool -L $IF combined <ncpu>      # may be refused; that is fine
  ethtool -G $IF rx <max> tx <max>
  ```

- **RPS, only when the NIC has one RX queue.** With a single hardware
  queue every packet is processed on one core's softirq, which caps
  receive well below what the cores could do. Spread it in software with
  an all-CPU mask in `rps_cpus`, `net.core.rps_sock_flow_entries=65536`
  and a matching `rps_flow_cnt`. Once the NIC has real queues, RPS is
  pure overhead — turn it off.

- **Emulated RTT**, when testing a WAN-shaped path. `netem` installs as
  the *root* qdisc and would otherwise discard the fq_codel tuning above,
  so attach fq_codel as its child rather than replacing it. netem's own
  queue has to hold a full delay-bandwidth product or it drops on its
  own: at 10 Gbps and 50 ms that is around 60k packets.

  ```
  tc qdisc add dev $IF root handle 1: netem delay <ms>ms limit 200000
  tc qdisc add dev $IF parent 1:1 handle 10: fq_codel <as above>
  ```

  Applied on both hosts, so the resulting RTT is twice the one-way value.

- **Emulated RTT on one host.** `loopback_bench --rtt-ms MS` re-runs
  itself in a private network namespace and puts `netem delay MS/2` on
  that namespace's `lo`; the host's network is untouched. `--link-mbps`
  adds a rate cap and `--loss` random loss. One qdisc carries both
  directions, so the cap and the loss also hit ACKs, and its queue is
  deep (200000 packets): the cap models a deep buffer. It needs
  privileges to create a network namespace: unprivileged user
  namespaces (`unshare -rn`), or run the bench under `sudo`. Without
  them it exits 2. One stream carries at most one flow-control window
  per RTT (16 MiB: 2.7 Gbps at 50 ms, 0.9 Gbps at 150 ms); raise it with
  `--fc-window`, or cap the link below it with `--link-mbps` so that
  congestion control sets the rate.

### Verify before trusting a number

Re-read the settings rather than assuming they took: interface, CPU
count, queue counts, MTU, txqueuelen, the active qdisc, ring sizes, RPS
mask and flow entries, `net.core.wmem_max` / `rmem_max`, the hard
`nofile` limit, and whether GSO is on. A check that changes nothing and
only reports is the right thing to run immediately before a measured
sweep.

### Congestion control

`--cc-algo` selects it: bbr, bbr1, c4, cubic, dcubic, fast, newreno, prague.
Since 0.14.0 (with aiopquic 0.7.0) the default is bbr (BBRv3) in every
tool, the examples and aiomoqt-relay; before that the relay defaulted to
cubic.
**Label every result with the algorithm actually used.**
"BBR" does not name the same algorithm across implementations, and a
cubic control run is the one posture every stack shares.

**Use bbr for playback over real paths, not cubic.** cubic, newreno and
dcubic are loss-based: they read every lost packet as congestion and cut
the window. On a long-RTT path with some real loss (Wi-Fi, a busy peering
link) the window is cut faster than it regrows, which takes many RTTs at
long RTT, so the rate settles below what the path carries and can fall
below the video's bitrate, which stalls playback. bbr paces to the
measured bandwidth and RTT and does not back off on scattered losses.
Loopback and a clean LAN hide the difference: there they measure CPU
cost, not congestion control. Compare algorithms on the real path.

**`--cc-options` for fan-out relays.** `--cc-options OPTS` passes an
option string to the controller; the session-flag tools and aiomoqt-relay
take it (`cc-options` in the relay's `--config` TOML). For bbr,
`Q<ratio>` sets the send quantum, the burst it may send at once as a
fraction of a second of pacing rate: `Q0.02` is 20 ms. A relay serving
many subscribers pays CPU per paced packet; a larger quantum sends fewer,
larger bursts, which raises how many subscribers one relay process can
carry at the cost of burstier traffic per subscriber. Measure it on your
relay and path before adopting it, and label results with it like the
algorithm:

```
aiomoqt-relay --port 4443 --cert cert.pem --key key.pem --cc-algo bbr --cc-options Q0.02
```

## Leave no trace

Tuning a host is half the job; handing it back clean is the other half.
**An untuned host produces a plausible-looking result, and so does a host
still carrying someone else's emulated delay.** The second is worse,
because it is invisible: nothing in the output says the path had 50 ms
bolted onto it, and the next person to use the box — or CI — inherits it
silently.

This has already happened here once. Both CI boxes had to have netem
removed after a sweep and were only declared clean once the path was
verified back to 0.38 ms with the native `mq` qdisc.

### After every run

- **Kill the processes first.** Publishers, subscribers, relays and any
  worker processes the ramp spawned. A ramp interrupted mid-flight can
  leave subscriber workers holding sessions open.
- **Remove the emulated delay.** This is the one that matters most. If
  the lab's script applied it, re-running it with the delay set to zero
  clears it; otherwise drop the root qdisc, which also takes netem with
  it and returns the interface to the kernel default:
  ```
  sudo tc qdisc del dev $IF root
  ```
- **Put the device settings back.** Transmit queue length to its original
  value, RPS off (`rps_cpus` to 0 and `net.core.rps_sock_flow_entries`
  to 0). NIC queue counts and ring sizes set with `ethtool -L` and `-G`
  persist until reboot, so either record the originals before changing
  them or plan on rebooting the box.
- **Leave the persistent sysctls alone.** The buffer and backlog settings
  in `/etc/sysctl.d/` are deliberate, shared, and survive reboot by
  design. They are configuration, not residue.

### Verify clean, do not assume

The same discipline as before a run: re-read rather than trust. Check
that the qdisc is back to the host's native one, that a ping between the
pair is back to its native sub-millisecond figure rather than whatever
the sweep emulated, and that no bench processes are still alive. Record
the clean RTT and qdisc name for the pair somewhere, so "back to normal"
is a number you can compare against instead of a feeling.

## What the synthetic load looks like

A **flat stream** is uniform objects at a fixed rate: `-s` object size,
`-r` rate, `-g` objects per group. A **video profile** models a GOP
instead — an I-frame opens each group, then a repeating B/P pattern — so
the large first object per group makes relay scheduling and subscriber
buffering behave the way they do under real media.

`VideoTrack.PROFILES`, and the cost at the default 30 fps, where a
one-second GOP is 1 I-frame, 20 B and 9 P under the `ibp` pattern:

| Profile | I | P | B | avg object | Mbps @ 30 fps |
|---|---|---|---|---|---|
| 240p | 8,000 | 1,500 | 800 | 1,250 | 0.30 |
| 270p | 13,000 | 2,000 | 1,000 | 1,700 | 0.41 |
| 360p | 26,000 | 4,000 | 2,000 | 3,400 | 0.82 |
| 480p | 40,000 | 6,000 | 3,000 | 5,133 | 1.23 |
| 720p | 80,000 | 12,000 | 6,000 | 10,266 | 2.46 |
| 1080p | 200,000 | 25,000 | 10,000 | 20,833 | 5.00 |
| 1440p | 350,000 | 40,000 | 18,000 | 35,666 | 8.56 |
| 4k | 600,000 | 60,000 | 30,000 | 58,000 | 13.92 |

**Rate does not scale with frame rate.** `-r` sets fps; at 60 fps the
same single I-frame amortises over twice as many frames, so the average
object falls and 1080p lands at 8.60 Mbps rather than 10. The three frame
sizes are individually overridable through `VideoTrack`, so the first
object of a group can be made as large or small as the question needs.

**There is no synthetic audio.** The bench tools are video-shaped only.
Audio lives in the media runbook, as a pcm-s16 tone or real AAC.

**Every payload byte follows from the object's location.** A generated
object is `{group}.{object}.{size}|` (a video frame:
`{group}.{object}.{size}.{type}|`), then the 0..255 counting pattern, cut
to size. `--verify-payload` on sub_bench and loopback_bench checks each
received object against that and fails the run on a mismatch, so objects
swapped, shifted, truncated or corrupted on the way show up. The check
costs the subscriber about 0.4 µs per 1–4 KiB object and 3.6 µs per
200 KB frame (Ryzen 7 PRO 7840U), so leave it off for ceiling runs, or
pass `--verify-payload N` to check every Nth object.

## A — loopback self-test

One process hosting both ends and its own relay. Nothing to set up and
nothing external to blame; this is the number that says the machine and
the build are healthy before anything else means much.

```
python -m aiomoqt.tools.adaptive_bench
```

No URL selects the loopback self-test. `-P 4 --step-mbps 20` for
parallel subgroup streams and a larger ramp step.

## Loopback ceiling — moq-loopback-bench

`loopback_bench` (console script `moq-loopback-bench`) runs a publisher
as a server and a subscriber dialing it, in one process, with no relay.
It measures what the Python stack and aiopquic deliver on this machine:
the build and host health check, and the number to compare across
builds.

```
python -m aiomoqt.tools.loopback_bench -Q --draft 18 -s 4096 -t 20
python -m aiomoqt.tools.loopback_bench -Q --draft 18 -s 16384 -P 4 -r 60 -t 20
python -m aiomoqt.tools.loopback_bench --draft 18 --fanout -s 4096 -t 20
python -m aiomoqt.tools.loopback_bench -Q --draft 18 --rtt-ms 50 -r 200 -t 10
```

- `-Q` raw QUIC, else H3/WebTransport. `--draft` applies to both ends
  (default 14, deprecated: pass 18, 20 or 22).
- `-s` object size, `-r` aggregate objects/s (0 = as fast as it goes),
  `-P` parallel subgroup streams, `-g` objects per group, `-t` seconds,
  `-i` report interval.
- `-D` sends ObjectDatagrams instead of streams (object at most ~1150 B);
  run it at the same `-s` and `-r` as a stream run to compare the two.
- `--fanout` publishes through `produce()` and the per-subscriber send
  lane, the path LOC media tracks take; `--per-peer-props` adds
  per-subscriber object properties.
- `--no-stats` counts objects and bytes only, for the delivery ceiling;
  `--verify-payload` checks each payload.
- `--rtt-ms`, `--link-mbps`, `--loss` emulate a link (see Host tuning).
- `--cc-algo`, `--cc-options`, `--fc-window`, `--max-queued-bytes` and
  `--max-inflight-bytes` set the transport as on the other tools.

It prints a results block (objects, throughput, latency percentiles,
jitter, loss) and exits 1 when no object arrived or a payload check
failed. The publisher and subscriber share one Python event loop,
so a loopback figure is a CPU figure: quote it with the `cpu:` line.

## B — fixed-rate pair at a video profile

No ramp: one publisher at a chosen resolution and one subscriber
measuring it. This is the controlled point measurement to compare across
builds.

```
python -m aiomoqt.tools.pub_bench "$RELAY_QUIC" -T v1080p --video 1080p --draft 18 -k -t 120
```

```
python -m aiomoqt.tools.sub_bench "$RELAY_QUIC" -T v1080p --draft 18 -t 120 -i 5
```

Nothing to set: both tools default to namespace `aiomoqt`, so they meet
there, and `-T v1080p` on both is what selects the track.


- `--video` takes I/P/B frame sizes from the resolution: one object per
  frame, one GOP per group opening on the I-frame, one subgroup stream.
  `-r` stays objects per second, so it is the frame rate (default 30), and
  `-g` stays objects per group, so it is the GOP length in frames (default
  one second of frames): `--video 1080p -r 60 -g 120` is 60 fps with 2 s
  GOPs. pub_bench, pub_server and adaptive_bench subs mode all take it.
- `--video` refuses `-s`, `-P` and `-D`: the profile sets each frame's
  size, sends one stream, and its I-frames are far past the 1152-byte
  one-packet datagram ceiling. Flat alternative: `-s 4096 -r 120 -g 240`,
  which is 2 s groups at 120 objects per second. Stream turnover is rate
  divided by group size, so lower `-g` to exercise churn.
- `--no-stats` counts objects and bytes only, measuring the delivery
  ceiling without the measurement in it.

**Observed**, 1080p profile against the deployed moqx relay over raw
QUIC on 2026-09-15 — a shared CI box over the WAN, so the latency is
mostly path RTT rather than relay cost:

```
publisher   1,113 objects, 38 GOPs, 30/s, 5.0 Mbps   I/P/B 200KB/25KB/10KB
subscriber    899 objects, 30 groups, 5.05 Mbps
            latency p50 40.3  p95 49.3  p99 128.7 ms   jitter 4.15 ms
            lost 0 (0.00%)   out-of-order 0
```

The measured 5.05 Mbps against the table's computed 5.00 is the profile
arithmetic confirming itself end to end.

## C — bandwidth ramp (single host)

The publisher's rate is nudged up every interval until the subscriber's
samples say stop.

```
python -m aiomoqt.tools.adaptive_bench "$RELAY_QUIC" -k --draft 18 --start-mbps 10 --step-mbps 10 --max-mbps 500 --latency-threshold 100 --report bw-ramp.csv
```

Against a relay the publisher and subscriber always run in separate
processes. `--mp` matters only for the loopback self-test (flow A), where
one Python process is GIL-bound and can be what you measure.

Latency is the subscriber's p90 over its last 5 s. The controller backs
off when it stays over `--latency-threshold` (default 100 ms) for two
intervals. A shortfall, achieved below `--shortfall-ratio` (0.85) of
commanded, holds the ramp and backs off once it lasts three intervals.
Back-off multiplies by `--backoff-factor` (0.9; closer to 1.0 for gentler
steps). Hard caps are `--max-mbps` and `-t` seconds. A run that receives
nothing exits nonzero.

**Set the threshold relative to the path RTT.** Absolute latency
includes the path, and the ramp takes full steps only while p90 is under
half the threshold; once it has backed off, it probes upward only then.
On a 49 ms path a 100 ms threshold freezes the ramp well below the
relay's ceiling. Use about 250 ms against a remote relay.

**Bandwidth mode cannot be split across hosts.** The controller actuates
the publisher while measuring at the subscriber, so separating them with
no channel between leaves an open loop. The tool refuses rather than
returning a number that looks fine.

## D — subscriber ramp, split across hosts

The fan-out question rather than the bitrate one. Splitting works here
because the publisher is fixed-rate and the whole control loop lives on
the subscriber side.

On the **relay host**, publisher only, no controller, publishing into the
local relay so ingest stays off the network:

```
python -m aiomoqt.tools.adaptive_bench moqt://127.0.0.1:4433 -k --draft 18 --mode subs --role pub -N perf -T subs-ramp --sub-mbps 10 -s 4096
```

On the **load host**:

```
python -m aiomoqt.tools.adaptive_bench "$RELAY_QUIC" -k --draft 18 --mode subs --role sub -N perf -T subs-ramp --sub-mbps 10 -s 4096 --start-subs 1 --step-subs 5 --max-subs 200 -j 10 -S 0.1 --sub-filter next-group-start --report subs-ramp.csv
```

It prints what it is waiting for:
`role=sub — expecting an external publisher on perf/subs-ramp at 10 Mbps`.

**Both sides need the same `-N`, `-T` and `--sub-mbps`**, and `-T`
defaults to a random suffix, so it has to be pinned explicitly. For a
video-shaped track, give both sides `--video RES` (and the same `-g`) in
place of `--sub-mbps` and `-s`; the profile sets the bitrate.

Ramp knobs:

| Flag | Meaning |
|---|---|
| `--step-subs` | subscribers per group, and the batch size: one worker process hosts a group |
| `-j` / `--join-rate` | seconds between groups. The ramp cadence, independent of `--interval`, which is only reporting |
| `-S` / `--stagger` | seconds between individual joins inside a group (default 0.1). Spaces the QUIC Initials so a group does not burst the relay; 0 opens the batch at once |
| `--sub-filter` | `next-group-start` skips the current-group replay some relays do on `latest-object`, the default |

A subscriber counts toward the high-water mark only while it receives at
least half of `--sub-mbps`, judged over the last few seconds and 3 s
after its latest subscribe. Fewer such subscribers than the mature
target, or a total below `--shortfall-ratio` of their combined rate, is a
shortfall; a lossy or starved subscriber is not counted as capacity. The
latency threshold works as in flow C, so set it from the path RTT here
too.

The ramp ends at `--max-subs`; it cannot hold a fixed count. For a fixed
audience use `load_sim --subs N` (flow E).

**Latency comes from the object's LOC timestamp** (property 0x10,
wall-clock µs): pub_bench and pub_media both stamp it, so an audience run
against a live broadcast reports real delivery latency. pub_media
`--loc-codecstring` adds a TIMESCALE, which makes the timestamp media
time — latency then reads 0.

**Absolute latency is not trustworthy across hosts.** It is the
receiver's clock minus the sender's timestamp, so a split run carries the
hosts' clock offset. Whenever the role is not `both`, adaptive_bench
measures p90 over a running baseline — the lowest p90 seen, its 1st
percentile once it has 100 samples — which cancels a constant offset.
The threshold is then the queueing delay allowed over that baseline, and
the banner says so. A clock that drifts during the run still leaks in.

## E — scenario load and churn

`load_sim` is the shaped counterpart to the ramp: namespaces by tracks,
cohorts that join late and leave early, and an audience that can ride on
a real broadcast instead of a synthetic one.

Standalone, where the scenario supplies both ends:

```
python -m aiomoqt.tools.load_sim "$RELAY_WT" -f rich-matrix --draft 18 -k --report loadsim.csv
```

Packaged scenarios are `default`, `rich-matrix` (namespaces by tracks),
`churn-storm` (join and leave pressure), `datagram-mix` (mixed delivery
modes) and `viewers`. `--seed` makes the churn reproducible.
`--subs-per-proc` caps subscriber slots per host process, default 100.
The one-track case needs no scenario file at all:
`--subs 50 -N load-$(date +%H%M%S) -r 30 -s 4096 -t 120`.

`viewers` is an audience and nothing else: `--role sub` drops the
scenario's own publishers, so **it needs a broadcast already running or
every subscribe fails**. Two shells.

Shell 1 — the broadcast. It mints its own namespace and prints it as
`namespace: aiomoqt/demo-<rand4>`:

```
python -m aiomoqt.tools.pub_media "$RELAY_WT" --draft 18 -k --pub-both --mp4 "$ASSETS/bbb-1080p-2500k.mp4" --loop --keepalive 10 --catalog-interval 1 --target-latency 200 -t 600
```

Shell 2 — the audience. Paste the namespace Shell 1 printed:

```
python -m aiomoqt.tools.load_sim "$RELAY_WT" -f viewers --role sub -N aiomoqt/demo-XXXX --draft 18 -k -i 5
```

Observed on 2026-09-15 against the deployed moqx relay (the load
host's hardware was not recorded): subscribers climb to 42–43 of the 43 target,
`rx` settles at 21–44 Mbps and the failure counter stays at `x0`. If
subs stays at 0 and `x` climbs, nothing is publishing that namespace —
the tool now prints the reason on the first failure.

`viewers` is 43 subscribers across video, audio and catalog: a steady
low-churn cohort that keeps the audience above zero, and a second cohort
joining at 10 s that churns at 8 %. Real browser tabs can watch the same
namespace at the same time.

## Where the numbers come from

| Surface | What it shows |
|---|---|
| relay stats | Whatever the relay under test exposes. For moqx that is a Grafana overview: sessions, ingress and egress bandwidth, fan-out ratio, subscribers, drops, loss, latency, and QUIC connections, streams, goodput and RTT. The relay's own account of the run. |
| `--report PATH` | Per-sample CSV from adaptive_bench, per-interval per-track CSV from load_sim. The artifact to keep and diff across builds. |
| `AIOPQUIC_QLOG_DIR=<dir>` | Standard qlog traces of the QUIC layer: congestion control, RTT, loss. Free to the run. |
| `--keylogfile PATH` | TLS secrets for Wireshark. Under `--mp` each worker writes `PATH.pub.<pid>` and `PATH.sub.<pid>`, combined when decrypting. |

Record the tuning state, the congestion control algorithm and the
emulated RTT alongside every result. Without those three a number cannot
be compared with another number. The hardware is recorded for you: the
bench tools (pub_bench, sub_bench, loopback_bench, adaptive_bench,
load_sim, pub_server, sub_media `--analyze`) print the CPU model, CPU
count and clock in their banner (`cpu:`) and results (`Host:`), and
`--report` CSVs carry them as `cpu_model`, `cpu_count` and `cpu_mhz`
columns.
