# aiomoqt media pipeline demos

Four flows. Three publish through a deployed relay into the browser
player — CMAF from a file, LOC from a file, and OBS over SRT as live
MPEG-TS — and one consumes an outside publisher. Goal for each: stable
low latency with the overlay numbers to prove it.

Synthetic load, ramps and churn are in
[bench-runbook.md](bench-runbook.md).

Actors: SHELL = a shell with the venv active in this repo · OBS =
OBS Studio · BROWSER = Chrome. RELAY =
https://moqx-main.ci.openmoq.org:4433/moq-relay (deployed; never
restart it). Draft 18 everywhere (`--draft 18` / `v=18`).

Each publisher below prints the player URL to paste. What still has to
be right:

| Setting | Value | What it is for |
|---|---|---|
| catalog refresh | `--catalog-interval 1` | a viewer joining after the first waits for the next catalog object; at 10 that is 0–10 s of TTFF per tab (loopback 09-12: ~5 s at 10, ~1 s at 1) |
| source asset | no B-frames | pub_media stamps decode order with no composition offsets, so B-frame sources judder. bbb-720p-2000k.mp4 and sintel-1280-demo.mp4 are fine; sintel-1280-surround.mp4 is bf=2 |
| start order | publisher, then viewer | pub_media sends nothing until a viewer subscribes; its `dropped` counter is the frames skipped until then |

## Before anything: paste this in every shell

```
export RELAY_WT=https://moqx-main.ci.openmoq.org:4433/moq-relay
export ASSETS=$HOME/Projects/moq/media-assets
export PLAYA=$HOME/Projects/moq/moq-playa-v059
```

That is the whole setup. **No namespace has to be handled by hand**: each
publisher mints its own with `demo/$(date +%H%M%S)` and prints both the
namespace and a ready-to-paste player URL. A fresh namespace per run
matters because a reused one leaves stale objects in moxygen's cache.

The only step that needs the namespace typed anywhere is a second tool
pointed at a running broadcast (a wire check, or the audience in the
benchmarking runbook). Copy it from the publisher's `namespace:` line.

## Prep (once)
- SHELL: `cd "$PLAYA" && pnpm build && pnpm --filter @moqt/examples dev`
  → :5173. Leave running. After player edits: `pnpm -r --filter "./packages/**" build`
  and restart vite (the examples import the packages' dist).
- SHELL: `python -m aiomoqt.tools.relay_probe --url https://moqx-main.ci.openmoq.org:4433/moq-relay --draft 18` → expect ✓
- SHELL: `hostname -I` → WSL IP for the OBS SRT URL (changes across reboots)
- Assets: `$ASSETS/` (bbb-720p-2000k.mp4, sintel-1280-demo.mp4,
  sintel-1280-surround.mp4, bbb-1080p*.mp4, bbb-av1-60s.mp4).

## Player URL parameters (/simple/)
| Param | Meaning | Default |
|---|---|---|
| `url=` | relay WT URL | probe page host |
| `ns=` | namespace, split on `/` (`nsField=` repeatable for literal fields) | live |
| `v=` | draft 14/16/18; a 16/18 pin never retries WT bare | 16 |
| `catalogBootstrap=` | auto / joining-fetch / strict / subscribe | auto |
| `warmStart=1` | joining FETCH of the current group (LOC only; startup decode error at the seam) | off |
| `catchUp=` | max playback rate chasing `targetLatency=` from the URL (not the catalog); wall-clock based | 1.0 = off |
| `targetLatency=` | catch-up set point (ms); on CMAF also the seek landing; LOC cushion cap | catalog value; CMAF landing 2 s |
| `cushion=` / `cushionMax=` | LOC render-cushion floor / cap (ms) | 200 (50 if RTT<5 ms) / target latency, at most 750 |
| `debug=1` | engine debug log, MSE tracing, media-element events in the page log | off |

The page log prints `Options: …` at load with exactly what reached the
engine. Overlay "cushion ms": MSE = buffered ahead of the playhead;
WebCodecs = scheduled audio ahead, or the render cushion when video-only.

## Demo A — CMAF file → glass (MSE playback)
- SHELL 1 (BBB 720p30, 2 s GOP):
  `python -m aiomoqt.tools.pub_media "$RELAY_WT" --draft 18 -k -N demo/$(date +%H%M%S) --mp4 "$ASSETS"/bbb-720p-2000k.mp4 --packaging cmaf --loop --target-latency 500 --keepalive 10 --catalog-interval 1 -t 3600`
  Sintel variant: same line with
  `--mp4 "$ASSETS"/sintel-1280-demo.mp4` (same command, different asset)
  (24 fps on a 60 Hz display shows a mild 3:2 cadence on pans; inherent).
- BROWSER: paste the `player:` URL the publisher printed. It already
  carries the relay, the namespace, `v=18`, `catalogBootstrap=subscribe`
  and the per-packaging knobs. Add `&debug=1` for the engine log.
- Targets: cushion ≈ 500 ms and flat; latency P50 < 50 ms, jitter < 5 ms
  (wire numbers, 09-10: 37 / 3.3); stalls 0; gaps 0.
- Levers: `--target-latency` on the publisher is what the catalog
  advertises and what the player's cushion target follows (seek landing
  and soft-chase set point); `targetLatency=` on the player overrides it.
- Adapter behavior (fork, from 92e420d onward): late previous-group objects
  are kept (no one-frame holes); a buffered hole is jumped after a wait
  scaled to its width (300 ms floor), landing near the live edge; a
  cushion above target + 0.5 s is drained at 1.05x until within 0.1 s.
  Before the port (upstream v0.5.9 adapter) the same run showed 40 ms
  holes, a 2 s wait and a 2 s → 4.3 s cushion climb; a stall line with
  `[a–b][b+0.04–c]` ranges after the port would be a regression.

## Demo B — LOC file → glass (WebCodecs, A/V, joining fetch)
- SHELL 1:
  `python -m aiomoqt.tools.pub_media "$RELAY_WT" --draft 18 -k -N demo/$(date +%H%M%S) --mp4 "$ASSETS"/bbb-720p-2000k.mp4 --loop --target-latency 100 --keepalive 10 --catalog-interval 1 -t 3600`
- BROWSER: paste the `player:` URL the publisher printed. It already
  carries the relay, the namespace, `v=18`, `catalogBootstrap=subscribe`
  and `cushion=<--target-latency>`. Add `&debug=1` for the engine log.
- Targets: render cushion flat at the target (floor = cap =
  `--target-latency`); audio late / snap 0; audio underruns 0; stalls 0.
  TTFF waits for the next IDR (up to one 2 s GOP).
- **The cushion must exceed the source's A/V arrival skew.** The publisher
  stats line reports it per track as `lag mean/max`; the overlay reports
  the end-to-end result as A/V skew. On the mp4 sources both are ~0 and
  100 ms is fine. On the MPEG-TS path audio measured ~130 ms behind video
  before the Demo C muxer flags; Demo C uses `--target-latency 200`.
- Levers: `--target-latency` sets both the catalog target (cushion cap) and
  the printed `cushion=` (floor). A lower `cushion=` in the URL lets the
  cushion adapt between floor and target. The audio output clamps its own
  lead (2 % chase above 150 ms, drop-and-re-anchor above 750 ms); audio
  that falls behind the cushion for 250 ms re-anchors (overlay "sync resets").
- Not in the printed URL: `warmStart=1` (startup decode error at the
  fetch/live seam, under investigation) and `catchUp=` (measures against
  the browser wall clock, so the WSL/Windows clock offset reads as latency
  and pitch-shifts audio).
- Late join = reload the tab.

## Demo C — OBS → SRT → LOC live (glass-to-glass latency evidence)
Two ingest paths. `--ts -` (2026-09-10) carries video AND audio on one
MPEG-TS pipe with the encoder's PTS, so pacing and A/V sync follow the
source; `--h264 -` is the older video-only Annex-B pipe stamped on
arrival. Prefer `--ts`.
- OBS: Stream = Custom, `srt://<WSL-IP>:9000?latency=20000`
  (microseconds, as in ffmpeg: `latency=20` is 20 µs = 0 ms). Output: x264
  CBR, keyint 2 s, `tune=zerolatency`, custom option `bframes=0`
  (not `bf=0`). Resolution from Settings → Video. Scene carries a ms
  clock burn-in. No media source in any scene may point at an srt://
  input (it retries every 8 s and kills the stream socket).
- **The millisecond clock.** Glass-to-glass is measured by photographing
  two clocks at once, so the scene needs one. Two ways:
  - **Browser clock** — open `docs/clock.html` (UTC mm:ss.mmm, white on
    black) and add it to the scene as a Browser or Window Capture source.
    It repaints on requestAnimationFrame, so the value is accurate at the
    moment it is painted but only refreshes at display rate.
  - **Burn-in on a synthetic source** — ffmpeg 4.4's `drawtext` has no
    `%N`, so wall-clock milliseconds need the RTCTIME detour: stretch the
    timebase to microseconds, stamp presentation time from the real
    clock, draw it, then restore the frame timebase.
    `-vf "settb=1/1000000,setpts=RTCTIME,drawtext=text='%{pts\:hms}':fontsize=48:fontcolor=white:box=1:boxcolor=black:x=20:y=20,settb=1/30,setpts=N"`
  - OBS's own burn-in is not wall time; on 09-10 it sat about 23 minutes
    off, which is fine for a same-screen preview-versus-player compare but
    useless as an absolute reference.
- **Picture quality.** `tune=zerolatency` disables lookahead and mb-tree,
  which costs real quality at a given bitrate, so pay for it in bits:
  6000 kbps CBR at 720p30, 10000 at 1080p30. Preset `faster` or `fast`
  if the CPU allows (OBS defaults to `veryfast`), profile `high`.
  Settings → Video: Base and Output resolution EQUAL (any downscale
  there is a second quality loss; if you must scale, filter = Lanczos).
  ffmpeg is `-c copy`, so it costs nothing. A soft picture that no
  encoder setting improves was the canvas bug below — rebuild the player.
- SHELL 1 (listener first, then start OBS streaming), A/V over TS:
  `ffmpeg -hide_banner -loglevel warning -fflags nobuffer -analyzeduration 0 -probesize 32768 -i 'srt://0.0.0.0:9000?mode=listener&latency=20000' -map 0:v -map 0:a -c copy -f mpegts -pes_payload_size 0 -omit_video_pes_length 0 -muxdelay 0 -flush_packets 1 - | python -m aiomoqt.tools.pub_media "$RELAY_WT" --draft 18 -k -N demo/$(date +%H%M%S) --ts - --target-latency 200 --keepalive 10 --catalog-interval 1 -t 3600`
  pub_media waits for the PMT and both codec configs before connecting,
  then prints the namespace and a `player:` line — paste that URL.
  Video-only fallback: swap `-map 0:v -map 0:a -c copy -f mpegts` for
  `-map 0:v -c:v copy -bsf:v h264_mp4toannexb -f h264` and `--ts -` for
  `--h264 - --no-audio`.
- No OBS at hand — synthetic A/V over the same TS path (`-pix_fmt
  yuv420p` is REQUIRED: lavfi testsrc is rgb24 and libx264 would pick
  High 4:4:4, which no browser decodes):
  `ffmpeg -re -f lavfi -i testsrc=size=1280x720:rate=30 -f lavfi -i sine=frequency=440:sample_rate=48000 -vf "settb=1/1000000,setpts=RTCTIME,drawtext=text='%{eif\:mod(floor(t/60)\,60)\:d\:2}\:%{eif\:mod(floor(t)\,60)\:d\:2}.%{eif\:mod(floor(t*1000)\,1000)\:d\:3}':fontsize=64:fontcolor=white:box=1:boxcolor=black@0.8:boxborderw=12:x=40:y=40,settb=1/30,setpts=N" -c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p -bf 0 -g 60 -c:a aac -f mpegts -pes_payload_size 0 -omit_video_pes_length 0 -muxdelay 0 -flush_packets 1 - | python -m aiomoqt.tools.pub_media "$RELAY_WT" --draft 18 -k -N demo/$(date +%H%M%S) --ts - --target-latency 200 --keepalive 10 --catalog-interval 1 -t 3600`
- BROWSER: the printed `player:` URL (`cushion=200`), then click in the
  page once: until a gesture the audio context stays suspended, audio does
  not play and A/V skew grows with runtime.
- Muxer flags: `-pes_payload_size 0` stops ffmpeg bundling AAC frames into
  ~2930-byte PES (≈180 ms at 128 kbps, up to `muxdelay`/2 on silence);
  `-omit_video_pes_length 0` lets pub_media emit a video frame when it
  completes instead of when the next one starts (keyframes > 64 KB still
  wait); `-muxdelay 0` drops the 0.7 s default bound.
- Measure: burn-in clock vs player frame (screenshot both), overlay
  latency P50/P95 (capture→arrival), cushion, stalls. Glass: 09-09
  640 ms (before the 64 KB read fix and the cushion knob); 09-16 1.17 s
  without the muxer flags and 0.58 s with them (OBS 3440x1440, cushion 200).
- On `--h264` frame pacing follows arrival stamps, so SRT/ffmpeg
  burstiness shows as uneven presentation; `--ts` stamps from the PES
  PTS and does not have this. The latency stat on `--ts` includes a
  constant offset equal to the encoder-to-first-arrival delay (the clock
  anchors on the first demuxed unit).

## Demo D — someone else's publisher → our subscriber (interop)
The only flow that tests the receive path against an independent
implementation, and the only one carrying HEVC, AV1 and Opus. Nothing to
start; Eyevinn's moqlivemock endpoint is always on.
- SHELL: `python -m aiomoqt.tools.sub_media "moqt://moqlivemock.demo.osaas.io:443" -N 'msf\/clear' -t 12 --inspect 2 --draft 18`
- **Escaped slash is required.** Their namespace is a ONE-element tuple
  containing a literal slash, not two elements, so plain `msf/clear` is
  rejected as "non-matching namespace".
- Expect 13 tracks (AVC/HEVC/AV1 × 400/600/900 kbps, AAC + Opus),
  `ts_skew_ms` in the tens of ms (the loc-04 0x10 timestamp path),
  `extra_props=[]`, and playable `video.h264` + `video.ivf` written.
  Verified 2026-09-13.
- Reverse direction unavailable: the pinned `mlmsub` is a d16-era build
  and dies on ALPN at draft 18; no Go toolchain here to build a current
  one. They also host a warp-player and an MSF/CMSF validator.

## Reading a stall or a pinned cushion
- Publisher `lag mean/max ms` per track = how late a frame was against
  its own capture stamp when the feeder sent it (source, pipe, demux).
  Signed: NEGATIVE means the stamp is ahead of the wall clock, which a
  receiver reads as negative end-to-end latency (the player labels it
  "clock skew"). `tx` appears only when handing to the track costs
  ≥ 0.05 ms, so it showing at all means backpressure here. Neither is
  end-to-end latency: a live source's stamps anchor to their own
  arrival, so constant upstream delay reads as zero.
- Player devtools, the audio path's own margin:
  `a=__player.audioOutput; [__player.stats.cushionMs, a?.scheduledAheadSec, a?.underrunCount, a?.captureLeadSec, a?.chasing, a?.liveEdgeSnapCount, __player.stats.avSkewMs]`
  A small positive `scheduledAheadSec` with 0 underruns = zero margin,
  raise `--target-latency`. Exactly 0 with 0 underruns = nothing scheduled.
- CMAF stall, with `debug=1`: the stall line prints buffered ranges at
  start and end. A hole between ranges is delivery or packaging; a
  continuous range with a frozen playhead is the player.

## Observability
- **Relay dashboard** — moqx relay overview (Grafana on the CI runner),
  `localhost:8443/grafana`. Peers, sessions, ingress/egress BW, CDN
  efficiency, namespaces, tracks, subscribers, drops, loss, latency,
  subscriptions per track, QUIC connections/streams/packets/goodput/RTT,
  object-ACK latency. The relay's side of every flow.
- Overlay + `window.__player` (facade; `__player.stats` has cushionMs,
  audioUnderruns, avSkewMs, stallDurationMs, gapCount).
- `debug=1`: `[video] waiting/seeking/ratechange …` lines with
  `t= rs= rate= buffered=` sit next to the stall lines for correlation.
- Wire: `python -m aiomoqt.tools.sub_media <RELAY> -N <ns> --draft 18 --inspect 5 --show-catalog`
  (ts_skew_ms = wire latency). QUIC: `AIOPQUIC_QLOG_DIR=/tmp/qlog` on the publisher.

## Troubleshooting
- "no such namespace" → publisher down / wrong -N / reused namespace. Blank page → vite down.
- Publisher dies ~30 s after start with `session closed: code=0
  reason='ConnectionTerminated'` and 0 objects sent → **keepalive off**
  (`--keepalive 0`, or aiomoqt before 0.11.0). While Forward State is 0
  the publisher correctly sends nothing, so the QUIC connection is silent
  and moxygen's 30 s idle timeout closes it. Any gap between starting a
  publisher and opening a viewer longer than 30 s hits this.
  `--keepalive 10` (the default) fixes it.
- Second and later viewers hang without playing → the publisher's
  `--catalog-interval` is 0 (or aiomoqt before 0.11.0): the catalog is
  emitted once and only the first viewer ever gets it.
- Publisher exits with `peer request_id N reused or regressed` → OUR
  bug, fixed a5bc731: the §10.1 duplicate check used the high-water mark
  as a floor, so request ids arriving out of order across concurrent
  request streams (one SUBSCRIBE per track) read as a reuse. Update
  aiomoqt. The relay was correct.
- Two-frame judder, steady flow → B-frame source. Use a `-bf 0` asset.
- Soft/blurry picture on LOC or SRT, unchanged by encoder settings →
  the canvas backing store was never sized, so frames were drawn at the
  300x150 HTML default and stretched back by CSS (fixed e8e674c; CMAF
  never had it). Rebuild the packages and restart vite. Verify in
  devtools: `document.querySelector('canvas').width` must equal the
  source width, not 300.
- OBS no-connect → WSL IP changed, or listener not started first.
- Second and later tabs slow to show video, first tab fast → the
  publisher's `--catalog-interval` (use 1; see the rules above).
- High TTFF → keyframe wait: 2 s GOP, or CMAF startup seek not settling
  (upstream "startup seek … did not settle within 2000ms").
- Cushion climbs after a stall on CMAF → gap-jump landing debt (Demo A
  known state); on LOC → check `cushion=` and the Options line.
- Audio drops out, "audio late / snap" late count rising → cushion below
  the source's A/V skew; raise `--target-latency`.

Pinned: moq-playa-v059 0540d37 (branch gmarzot-playa-dev on upstream
v0.5.9; fork PR gmarzot/moq-playa#1) ·
aiomoqt gmarzot-0.11.0 607b668 ·
moqx-main v0.3.5 (linode-ci-000).
