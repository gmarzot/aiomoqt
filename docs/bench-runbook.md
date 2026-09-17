# Load and benchmarking runbook

Finding where a relay stops coping, and saying so with numbers. A
feedback-driven ramp on one axis at a time, from a loopback self-test
that needs no setup up to a split run with the publisher beside the
relay and the load generator a network hop away.

Companion to [demo-runbook.md](demo-runbook.md), which covers real media
through the same stack.

## Before anything: paste this in every shell

```
export RELAY_WT=https://moqx-main.ci.openmoq.org:4433/moq-relay
export RELAY_QUIC=moqt://moqx-main.ci.openmoq.org:4433/moq-relay
export ASSETS=$HOME/Projects/moq/media-assets
```

Publishers mint their own namespace and print it. Where two tools must
meet on the same namespace — flow B's pair and flow E's audience — set
it once in the shell and reuse it, rather than copying a timestamp
around:

```
export NS=demo/$(date +%H%M%S)
```

Paste that same line into the second shell only if it needs to agree;
otherwise ignore it. A fresh namespace per run keeps moxygen's cache
from serving stale objects.

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

### Verify before trusting a number

Re-read the settings rather than assuming they took: interface, CPU
count, queue counts, MTU, txqueuelen, the active qdisc, ring sizes, RPS
mask and flow entries, `net.core.wmem_max` / `rmem_max`, the hard
`nofile` limit, and whether GSO is on. A check that changes nothing and
only reports is the right thing to run immediately before a measured
sweep.

### Congestion control

`--cc-algo` selects it: bbr, bbr1, newreno, cubic, dcubic, prague, fast.
Default is bbr1. **Label every result with the algorithm actually used.**
"BBR" does not name the same algorithm across implementations, and a
cubic control run is the one posture every stack shares.

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

## A — loopback self-test

One process hosting both ends and its own relay. Nothing to set up and
nothing external to blame; this is the number that says the machine and
the build are healthy before anything else means much.

```
python -m aiomoqt.tools.adaptive_bench
```

No URL selects the loopback self-test. `-P 4 --step-mbps 20` for
parallel subgroup streams and a larger ramp step.

## B — fixed-rate pair at a video profile

No ramp: one publisher at a chosen resolution and one subscriber
measuring it. This is the controlled point measurement to compare across
builds.

```
python -m aiomoqt.tools.pub_bench "$RELAY_QUIC" -N "$NS" -T v1080p --video 1080p --draft 18 -k -t 120
```

```
python -m aiomoqt.tools.sub_bench "$RELAY_QUIC" -N "$NS" -T v1080p --draft 18 -t 120 -i 5
```

Both shells export the same `$NS`, so they agree.


- `--video` sets object size, rate and GOP together, overriding `-s`,
  `-r` and `-g`. Flat alternative: `-s 4096 -r 120 -g 240`, which is 2 s
  groups at 120 objects per second. Stream turnover is rate divided by
  group size, so lower `-g` to exercise churn.
- `--video` with `-D/--datagram` is refused: profile I-frames are far
  past the 1152-byte one-packet ceiling.
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
python -m aiomoqt.tools.adaptive_bench "$RELAY_QUIC" -k --draft 18 --start-mbps 10 --step-mbps 10 --max-mbps 500 -l 100 --report bw-ramp.csv
```

Add `--mp` when chasing a transmit ceiling: one Python process is
GIL-bound, so without it the interpreter can be what you measure.

Stops on p90 latency over `-l` (default 100 ms) for two intervals
running, or achieved-over-commanded below `--shortfall-ratio` (0.85).
Back-off multiplies by `--backoff-factor` (0.9; closer to 1.0 for gentler
steps). Hard caps are `--max-mbps` and `-t` seconds.

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
defaults to a random suffix, so it has to be pinned explicitly.

Ramp knobs:

| Flag | Meaning |
|---|---|
| `--step-subs` | subscribers per group, and the batch size: one worker process hosts a group |
| `-j` / `--join-rate` | seconds between groups. The ramp cadence, independent of `--interval`, which is only reporting |
| `-S` / `--stagger` | seconds between individual joins inside a group (default 0.1). Spaces the QUIC Initials so a group does not burst the relay; 0 opens the batch at once |
| `--sub-filter` | `next-group-start` skips the current-group replay some relays do on `latest-object`, the default |

**Absolute latency is not trustworthy across hosts.** It is the
receiver's clock minus the sender's timestamp, so a split run carries the
hosts' clock offset straight into the back-off signal. adaptive_bench
knows: whenever the role is not `both` it sets a skew-safe flag and leans
on signals that are differences or counts — jitter is a difference of
differences, shortfall is a count — and neither inherits the offset.

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

Shell 1 — the broadcast. It prints the namespace it chose:

```
python -m aiomoqt.tools.pub_media "$RELAY_WT" --draft 18 -k -N "$NS" --mp4 "$ASSETS/bbb-720p-2000k.mp4" --loop --keepalive 10 --catalog-interval 1 --target-latency 500 -t 600
```

Shell 2 — the audience, with the same `$NS` exported:

```
python -m aiomoqt.tools.load_sim "$RELAY_WT" -f viewers --role sub -N "$NS" --draft 18 -k -i 5
```

Observed on 2026-09-15: subscribers climb to 42–43 of the 43 target,
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
be compared with another number.
