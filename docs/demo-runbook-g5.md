# Browser broadcaster ↔ player demos (g5)

The moq-playa fork's two pages — `/g5-broadcast/` (camera or screen →
relay) and `/g5-player/` — through the deployed relay, on their own or
with aiomoqt tools at either end. File and SRT publishers into the player
are in [demo-runbook.md](demo-runbook.md); synthetic load in
[bench-runbook.md](bench-runbook.md).

Actors: SHELL = a shell with the exports below · BROWSER = Chrome. RELAY =
https://moqx-main.ci.openmoq.org:4433/moq-relay (deployed; never restart
it). Draft 18 unless a demo says otherwise.

## Setup

```
cd $HOME/Projects/moq/aiomoqt && source .venv/bin/activate
```
```
export RELAY_WT=https://moqx-main.ci.openmoq.org:4433/moq-relay PLAYA=$HOME/Projects/moq/moq-playa-v059
```

- SHELL 1, and leave it running: `cd "$PLAYA" && pnpm install && pnpm --filter @moqt/examples dev`
  → http://localhost:5173/g5-broadcast/ and http://localhost:5173/g5-player/.
  Player and broadcaster edits hot-reload; nothing to rebuild.
- BROWSER: open `/g5-broadcast/`, press **Camera** or **Screen**, then
  **viewer link** → open it. The link carries relay, namespace, draft and
  any `compat=`.

### Broadcaster settings (⚙)
Kept per tab across reloads; the last applied also seeds new tabs. A URL
parameter of the same name overrides a saved value.

| Setting | Meaning |
|---|---|
| Relay WebTransport URL | default moqx-main |
| Namespace | blank: a fresh `g5-<hex>` for every broadcast (Start), kept across reconnects; a typed name is kept |
| Draft version | 18 (default), 16, 14 |
| Packaging | LOC: WebCodecs on the viewer · CMAF: fMP4 fragments, MSE on the viewer |
| Catalog refresh interval | 0 = never re-sent (draft 18 viewers FETCH it); default 1000 ms on draft 14/16 |
| Congestion control | browser default / low-latency / throughput hint |
| Video codec, bitrate, bitrate mode | H.264 Baseline/High or AV1; constant mode tames keyframe bursts |
| Capture frame rate | requested; the camera may deliver less (log: `Video: … @ NNfps`) |
| Keyframe interval | frames per group |
| Target playback latency | catalog `targetLatency`, the viewer's playout target |
| Audio over datagrams | draft 18 only |

### Readouts
- Broadcaster: SETUP / PUB_NS / per-track FWD badges (green = a relay
  subscription forwarding); bitrate, encoder fps, objects, keyframes, queue;
  log lines for each relay SUBSCRIBE, FETCH, Forward State change and the
  per-minute capture drift.
- Player: TTFF; LATENCY p50/p95/max (capture → arrival) with `E2E`
  (capture → drawn frame; `≈` on CMAF); JITTER; QUEUED per track against
  TARGET with the playback RATE; DROPPED, OBJ LOST, STALLS. `?debug=1` adds
  the engine log. URL parameters: [demo-runbook.md](demo-runbook.md#player-url-parameters-g5-player).

## Part 1 — browser only

### G1 — LOC basics
- Broadcast with defaults (LOC, draft 18, target 200), open the viewer link.
- Expect TTFF under 1 s, LATENCY p50 about 50 ms to moqx-main, E2E near
  target + capture + encode, stalls 0.

### G2 — LOC vs CMAF side by side
- Second broadcaster tab: settings → Packaging CMAF → Apply (settings are
  per tab, so the first keeps LOC). Open both viewer links next to each other.
- Expect CMAF a little above LOC. CMAF holds its buffer at the target; at
  24 fps it stalls with about 100–150 ms buffered, so a target below
  ~150 ms cycles stall → refill → catch-up (`[MSE] stalled with N ms …`).

### G3 — many viewers, late joins
- Open three or four viewer tabs, a few seconds apart.
- Each joins with SUBSCRIBE + Joining FETCH: one `FETCH served` line per
  viewer in the broadcaster log. The relay carries one subscription per track
  upstream.

### G4 — pause and resume
- Player **Pause** / **Play**. As the only viewer: broadcaster logs
  `Forward State 0`, then `1`, and forces a keyframe on resume.
- Expect playback to resume at live. On CMAF the order line stays at
  `missing 0`: the paused interval is not counted as lost.

### G5 — leave, return, restart
- Close a viewer: its session closes on the spot; media FWD goes grey when
  the last one leaves. On draft 18 moqx keeps the catalog subscription
  (catalog FWD stays green; relay-side, logged).
- Typed namespace: Stop, then Start. The open viewer (SUB_NS) re-joins on its
  own when the namespace is published again.
- Blank namespace: Start mints a new one; open the new viewer link.

### G6 — latency targets
- Target 200 → 150 → 100 (broadcaster setting, or `targetLatency=` on a
  single viewer). Watch QUEUED against TARGET, STALLS and the catch-up line.
- 60 fps: **Screen** with capture rate 60 (check the `Video:` log line).
  A floor set per frame would let CMAF hold a lower target at 60 fps.

### G7 — draft 16 vs 18
- Draft 16 broadcaster: the viewer link adds `catalogBootstrap=subscribe`;
  SUB_NS stays grey (not sent on 16); the catalog FWD drops when the last
  viewer leaves.

### G8 — transport knobs
- Congestion control low-latency vs throughput, and audio over datagrams
  (draft 18): A/B two broadcaster tabs and compare JITTER and audio underruns.

### G9 — non-conformant relay
- `?compat=request-credit,empty-objects` on both pages: requests without
  initial MAX_REQUEST_ID credit, and empty media objects skipped. The player
  logs a `compat:` line with what it had to tolerate.

## Part 2 — with aiomoqt tools

Namespaces: the g5 namespace is one field (`g5-<hex>`), shown in each
page's header (click to copy). Pass it with `-N` as is; `--discover` matches
whole fields and does not apply.

### M1 — browser broadcaster → aiomoqt subscriber
- SHELL 2: `python -m aiomoqt.tools.sub_media "$RELAY_WT" --draft 18 -N g5-<hex> --analyze -i 1 --report g5.csv`
- Per-track delivery, pacing and capture → arrival latency, independent of
  the player. The browser stamps with the Windows clock and sub_media reads
  with the WSL one: read the clock note below before comparing latencies.

### M2 — aiomoqt publisher → browser player
- `pub_media` prints a `/g5-player/` URL per relay: demos A–C in
  [demo-runbook.md](demo-runbook.md).

### M3 — path canary next to a g5 pair
- Publisher first, then subscriber, while a G1/G2 pair runs:
- SHELL 2: `python -m aiomoqt.tools.pub_bench "$RELAY_WT" --draft 18 -N canary -T video --video 1080p -r 30 -t 3600`
- SHELL 3: `python -m aiomoqt.tools.sub_bench "$RELAY_WT" --draft 18 -N canary -T video -i 1`
- A spike on the g5 pair alone is the pair; one in both at the same moment
  is the path or the host.

### M4 — one broadcast, mixed audience
- One g5 broadcast, viewers = two g5-player tabs + M1's `sub_media`. The
  player LATENCY chart and the `sub_media` intervals cover the same objects.

## Clock note (cross-host numbers)
Capture → arrival compares the stamping clock with the reading clock. A
Windows browser on both ends shares one clock; WSL tools use the WSL VM's.
Before trusting a cross-host latency, or when a WSL-side stream shows a
periodic sawtooth:
- `cat /sys/devices/system/clocksource/clocksource0/current_clocksource`
- Kernel tick and frequency (`sudo apt install adjtimex`): `adjtimex --print | grep -E 'tick|frequency'`.
  tick must be 10000 (µs per 100 Hz tick); about 9880 runs the clock 1.2 %
  slow whatever the clocksource, and the host time sync then steps it forward
  every 25–30 s. Reset: `sudo adjtimex --tick 10000 --frequency 0`, then
  re-measure; if it drifts back, something keeps steering it (`wsl --shutdown`
  restarts the VM — and every WSL session).
- WSL clock against NTP, every 3 s (offset in ms, ±RTT/2):
  `for i in $(seq 30); do python -c "import socket,struct,time;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(2);s.sendto(b'\x1b'+47*b'\0',('time.google.com',123));w=struct.unpack('!12I',s.recv(48));print(round((w[10]-2208988800+w[11]/2**32-time.time())*1000,1))"; sleep 3; done`
- Scheduling gaps in the WSL VM over 2 min (gaps over 50 ms, with their time):
  `python -c "import time;s=p=time.monotonic();exec('while p-s<120:\n time.sleep(0.001);n=time.monotonic()\n if n-p>0.05: print(round(n-s,1),round((n-p)*1000))\n p=n')"`
- A steady offset is a constant and reads as latency; a ramp with steps is a
  clock fault; regular gaps are VM pauses, which also stall the network.
- A slow WSL clock hits a WSL publisher twice: wall-clock timestamps get the
  sawtooth, and pacing on the monotonic clock sends slower than real time, so
  every viewer's buffer drains (2026-10-07: tick 9880, −12.8 ms/s, +350 ms
  step every ~25 s; periodic CMAF stalls and LOC underruns on pub_media
  streams, while g5-broadcast stayed clean).

## Known issues
- Namespace restarted under the same typed name: a new viewer can get no
  catalog — moqx answers its FETCH from the earlier run's cache
  (openmoq/moqx#795). Blank namespace avoids it.
- moqx keeps the draft-18 catalog subscription after its viewers leave.
- Hidden tabs: Chrome throttles timers; the player logs `Page hidden` /
  `Page visible after Ns away` to correlate.

Pinned: moq-playa-v059 2cf770f · moqx-main v0.3.5.
