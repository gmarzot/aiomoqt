# Glass to Glass — aiomoqt media pipeline demo runbook

Live demonstrations of the aiomoqt media stack: MSF catalogs with LOC
and CMAF packaging, published over MoQ Transport through a relay into
real players. Conventions below: `$RELAY` is a MoQT relay endpoint
(WebTransport URL, e.g. `https://relay.example:4433/moq-relay`);
assets are H.264+AAC progressive mp4 (for lowest join latency use
~2s GOPs and no B-frames: `-g 2×fps -sc_threshold 0 -bf 0`).

Fresh namespace per publisher run (`-N demo-$(date +%H%M%S)`) keeps
relay state deterministic across restarts.

## Demo 1 — file to glass (LOC)

A plain mp4 becomes a live broadcast: samples pass through
byte-for-byte as LOC objects, the MSF catalog carries measured
bitrates and decoder config, and late joiners bootstrap through the
relay.

```
# publisher
python -m aiomoqt.tools.pub_media $RELAY -N demo-$(date +%H%M%S) \
  --mp4 content.mp4 --loop --target-latency 500 --draft 16 -t 3600

# subscriber — playable elementary streams + per-frame wire view
python -m aiomoqt.tools.sub_media $RELAY -N <ns> --draft 16 -t 30 \
  --inspect 5 --show-catalog
ffplay media-out/video.h264
```

Browser (moq-playa examples, `pnpm --filter @moqt/examples dev`):
`http://localhost:5173/simple/?url=$RELAY&ns=<ns>&v=16&catalogBootstrap=subscribe`
(`--loc01-compat` on the publisher while playa is on loc-01 property
ids). AV1 sources work identically (IVF output at the subscriber).

## Demo 2 — camera glass to browser glass (live OBS)

OBS encodes once; SRT carries it into a live Annex-B ingest that
stamps frames with wall-clock arrival time — the player's latency
chart and a millisecond clock burned into the scene independently
witness the same number.

- OBS → Stream: Custom, `srt://<host>:9000?latency=20`; Output:
  keyframe interval 2s, x264 `tune=zerolatency bf=0`, CBR.
- Listener first, then Start Streaming in OBS:

```
ffmpeg -i 'srt://0.0.0.0:9000?mode=listener&latency=20000' \
  -map 0:v -c:v copy -f h264 - 2>/tmp/srt-ffmpeg.log \
  | python -m aiomoqt.tools.pub_media $RELAY -N obs-$(date +%H%M%S) \
      --h264 - --no-audio --target-latency 300 --draft 16 -t 3600
```

## Demo 3 — packagings and implementations

**CMSF/CMAF end to end** — same catalog model, CMAF chunks
(moof+mdat per frame, header in the catalog initDataList):

```
python -m aiomoqt.tools.pub_media $RELAY -N cmsf-$(date +%H%M%S) \
  --mp4 content.mp4 --packaging cmaf --loop --draft 16 -t 3600
python -m aiomoqt.tools.sub_media $RELAY -N <ns> --draft 16 -t 30
ffplay media-out/video.mp4        # received chunks as playable fMP4
```

**Cross-implementation** — any independent MSF consumer against the
same broadcast (e.g. moqlivemock's `mlmsub -addr <relay-host:port>
-draft 16 -namespace <ns> -videoname video -audioname audio
-muxout - | ffplay -`), or a browser publisher
(playa `/broadcast/`) into `sub_media`.

**DVR capture** — any live broadcast to a shareable mp4:

```
python -m aiomoqt.tools.sub_media $RELAY -N <ns> --draft 16 -t 30
ffmpeg -r 30 -i media-out/video.h264 -i media-out/audio.aac -c copy grab.mp4
```

## Observability

| Surface | Shows |
| --- | --- |
| `sub_media --inspect N` | per-frame group/object ids, size, keyframe, `ts_skew_ms` (wire latency), extension properties |
| `sub_media --show-catalog` | the catalog JSON as subscribers receive it |
| `AIOPQUIC_QLOG_DIR=...` | standard qlog traces of the QUIC layer for post-run analysis |
| namespaces are tuples | `-N a/b` → 2 fields; `-N 'a\/b'` → one field with a literal slash (for single-field peers) |

## Troubleshooting

| Symptom | Cause → fix |
| --- | --- |
| `no such namespace` | publisher not running / different `-N` — restart it |
| connects but no catalog | subscriber raced the publisher's announce — reload/resubscribe after "publishing…" |
| audio ok, video errors in a player | check the source for B-frames (`ffprobe -show_entries stream=has_b_frames`) — use `-bf 0` encodes |
| publisher exits with `session closed: code=…` | the relay's stated reason; fresh namespace and retry |
