# Media publishing

`aiomoqt.media` and the `pub_media` / `sub_media` tools publish and receive MSF broadcasts: a
catalog track plus audio and video tracks, packaged as LOC, CMAF or LOCMAF. This guide covers
what each source and option does; `python -m aiomoqt.tools.pub_media -h` lists every flag.

Examples use `$RELAY` for a relay URL. The scheme picks the transport: `moqt://host:port` is raw
QUIC, `https://host:port/path` (or bare `host:port`) is WebTransport.

## Sources

One source per run: `--mp4`, `--h264`, `--ts` and `--input` are exclusive. With none, the
broadcast is audio only, a synthesized tone.

| Flag | Input | Video | Audio | Timing |
|---|---|---|---|---|
| `--mp4 FILE` | mp4 file | H.264, H.265, AV1 | the file's AAC or Opus; tone with `--tone` | sample tables, edit list honoured |
| `--h264 FILE\|-` | live H.264 Annex B (ffmpeg, OBS) | H.264 | tone; none with `--no-audio` | stamped on arrival |
| `--ts FILE\|-` | live MPEG-TS | H.264, H.265 | AAC (ADTS), Opus | PES timestamps |
| `--input URL` | anything FFmpeg opens: HLS, DASH, files | H.264, H.265, AV1 | AAC, Opus | paced to the source timestamps |
| none | | | pcm-s16 tone at `--freq` Hz (440) | generated |

- **mp4.** Samples pass through with no decode or re-encode. Encoder priming (AAC, Opus pre-skip)
  is not sent and an empty edit delays its track; an unsupported edit list is ignored with a
  WARNING. `--loop` repeats the clip for the whole duration, every track on one period (the
  presented duration), so audio and video do not drift apart pass by pass.
- **MPEG-TS.** One pipe carries both tracks with the encoder's own timing. The stream head (PMT,
  codec configs) is read before any relay session opens, so start the encoder first. Make the
  muxer flush per frame, or ffmpeg packs about 300 ms of audio into one PES:
  `-f mpegts -pes_payload_size 0 -omit_video_pes_length 0 -muxdelay 0 -flush_packets 1`.
- **`--input`.** Needs PyAV: `pip install 'aiomoqt[media]'`. Publishes CMAF or LOCMAF only.
  `--rendition best` (default) takes the top video rendition; `all` takes every rendition as one
  catalog alternate group (experimental: renditions are fetched in turn, so large ladders fall
  behind real time). DASH manifests are parsed here, not by FFmpeg: SegmentTemplate with
  `$Number$` or `$Time$`, SegmentList, static and dynamic MPDs, one Period, UTCTiming. Each DASH
  segment is one group, numbered by the segment, so equal groups of the renditions align.
  Not supported: SegmentBase and encrypted HLS or DASH.

Short GOPs and no B-frames keep join latency low: `-g <2×fps> -sc_threshold 0 -bf 0`.

## Packaging

`--packaging loc|cmaf|locmaf`; default `loc`, `cmaf` with `--input`. `cmaf` and `locmaf` need
`--mp4` or `--input`.

- **loc** (draft-ietf-moq-loc-04): one object per frame, metadata and timestamps in object
  properties. The decoder config rides the catalog `initDataList` and each group's first object.
- **cmaf** (draft-ietf-moq-cmsf): each object is one CMAF chunk, a moof+mdat holding one sample.
  Groups start at key frames. The CMAF header (ftyp+moov) rides the catalog `initDataList`.
- **locmaf** (draft-einarsson-moq-locmaf, `locmafVersion` 0.3): the same chunks with the moof
  sent as a compact header, a delta of the previous one in the group. Receivers rebuild the
  CMAF chunk byte for byte.

LOC options for receivers that need something different:

| Flag | Effect |
|---|---|
| `--init-data catalog\|track\|object` | Decoder config in the catalog only, as a Track Property, or on each group's first object (default: catalog and object) |
| `--annexb` | H.264/H.265 as Annex B, parameter sets in every key frame, codec `avc3`/`hev1` |
| `--no-catalog` | No catalog track, for receivers that configure from the media; implies `--annexb` |
| `--loc-codecstring` | Every object carries its codec string and, on video, frame marking: players without a catalog |
| `--loc-timescale TICKS` | Timestamps as media time with TIMESCALE, instead of wall-clock µs |
| `--loc01-compat` | Timestamps also under loc-01's property ID |
| `--loc-bare` | No empty Private Properties block, for receivers predating loc-04 §2.2 |

## Groups, objects and streams

- **Video:** one group per key frame, object 0 the key frame. `--video-subgroups group`
  (default) sends each group on one subgroup stream; `object` opens a stream per object.
- **Audio:** `--audio-frames-per-group N`, 1-100 (default 1; about 1 s with locmaf). With CMAF and
  LOCMAF a group is one fragment of N one-frame chunks. `--audio-subgroups` works as for video.
- **Datagrams:** `-D` sends audio as object datagrams.
- **Restrictions:** locmaf needs per-group streams, so it takes neither `object` mode nor `-D`.
  `--input` takes none of these options.

## Catalog

The catalog (draft-ietf-moq-msf) is a track named `catalog`. Object 0 of a catalog group is a
complete catalog.

- `--catalog-interval SECS` re-sends the full catalog as a new group so later viewers can join
  (default 1; 0 sends it once at start).
- `--target-latency MS` sets `targetLatency`, from which players size their playout buffer.
- `--catalog-bitrate BPS` and `--catalog-audio-bitrate BPS` set the advertised bitrates. Default:
  the mp4's mean; `--ts` measures it and leaves it out until measured.

## Timing

LOC objects carry their capture time in wall-clock µs; CMAF chunks carry decode time in the
track timescale. `--timestamp-mapping` adds draft-frindell-moq-timestamp timing:

- **Mapping.** TIMESCALE and TIMESTAMP_MAPPING are sent as Track Properties, anchored at the Unix
  epoch: Base Group and Base Timestamp 0, Group ID = capture ticks ÷ Group Multiplier. A
  restarted publisher sends the same properties and continues the same group numbering.
- **Start.** The run starts on the next video group boundary; audio starts on its nearest frame
  boundary at or after it.
- **Per-object timestamps.** OBJECT_TIMESTAMP goes only on a frame off schedule. Sources without a
  fixed cadence (live inputs without `--ts-cadence`, irregular key frames) send it on every object.
- **CMAF.** The first CMAF or LOCMAF object of each group carries a prft box.
- **Loops.** With `--loop`, the period is trimmed to a multiple of the video group and audio frame
  durations, with a WARNING, so seams need no OBJECT_TIMESTAMP.

Two options refine it:

- **`--ts-cadence fps=N[/D],gop=M`** (with `--ts`) declares the stream's fixed cadence. It implies
  `--timestamp-mapping` at TIMESCALE 1000 × fps (N for N/D) and gives the catalog its framerate. Output is held
  until each stamp comes due, which adds up to one GOP of delay at start.
- **`--start-at USEC`** (with `--timestamp-mapping` and `--mp4` or `--ts`) starts the run at that
  instant, µs since the epoch. It must be a group boundary in the future: allow a few seconds for
  the relay sessions (and, with `--ts`, the stream head) to come up.

`--no-pace` sends as fast as the transport accepts instead of pacing to the timestamps.

## Announcing, delivery and ending

- **Announcing:** a PUBLISH per track by default. `--pub-ns` sends PUBLISH_NAMESPACE only, and the
  relay forwards each SUBSCRIBE; `--pub-both` sends both.
- **Forwarding:** `--forward 0|1` sets the initial Forward State in PUBLISH. With 0, the default,
  nothing is sent until a subscriber asks.
- **Several relays:** list several URLs to send the same broadcast to each, from one process on
  one timeline. Relays on different drafts need a `--draft` they all accept.
- **Reconnects:** `--reconnect` redials a lost relay 1, 2, 4 … s apart (at most 30 s) and publishes
  to it again; the media timeline runs on meanwhile. A relay GOAWAY redials at once, to its New
  Session URI if it gives one.
- **FETCH:** `--fetch-history BYTES` keeps recent objects per track to answer FETCH (default 0
  refuses FETCH).
- **Ending:** at the end of `-t` (default 30 s), or on SIGINT/SIGTERM, each track sends End of
  Track, then PUBLISH_DONE, then PUBLISH_NAMESPACE_DONE for an announced namespace. `-t 0` runs
  until a signal.
- **Keepalive:** `--keepalive SEC` (default 10) keeps a quiet connection, such as one waiting at
  forward 0, from timing out.

Transport options are shared with the other tools: `--draft`, `-k`, `--cc-algo`, `--cc-options`,
`--fc-window`, the send budgets, `--compat`, `--log-level` and `--log`.

## Automation

- **Event stream:** `--stats-json` writes one `@mtt <json>` line per event to stdout and moves
  human output to stderr. Events:
  - `ready`, once the relays are up;
  - `anchor`, with `--timestamp-mapping`: `start_us`, and each track's `timescale`,
    `group_multiplier` and `object_multiplier`;
  - `stats`, every `--stats` seconds;
  - `endpoint_lost` and `endpoint_up`;
  - `done`.
- **Player links:** `--player-base URL` (or `$AIOMOQT_PLAYER_URL`) prints a ready-to-paste player
  URL per relay at start.
- **Exit status:** 0 when the duration ends or on a signal; 2 usage error; 64 an object does not
  fit a datagram; 69 no relay reachable, or every relay lost; 70 internal error, including an
  unreadable source.

## Recipes

```bash
# Loop a clip with epoch-anchored timing until stopped
python -m aiomoqt.tools.pub_media $RELAY -N demo/clock --mp4 clip.mp4 --loop --timestamp-mapping -t 0

# The same clip as CMAF
python -m aiomoqt.tools.pub_media $RELAY -N demo/cmaf --mp4 clip.mp4 --loop --packaging cmaf -t 0

# Start at a chosen instant: a 30 fps clip with a key frame every 60 frames has 2 s groups
python -m aiomoqt.tools.pub_media $RELAY -N demo/clock --mp4 clip.mp4 --loop --timestamp-mapping -t 0 --start-at $(( ($(date +%s) / 2 + 3) * 2000000 ))

# Live SRT or OBS feed over MPEG-TS on a fixed cadence
ffmpeg -fflags nobuffer -i 'srt://0.0.0.0:9000?mode=listener' -map 0:v -map 0:a -c copy -f mpegts -pes_payload_size 0 -omit_video_pes_length 0 -muxdelay 0 -flush_packets 1 - | python -m aiomoqt.tools.pub_media $RELAY -N live/obs --ts - --ts-cadence fps=30,gop=60 -t 0

# Re-publish HLS, or a DASH ladder as one alternate group
python -m aiomoqt.tools.pub_media $RELAY -N vod --input https://cdn.example/master.m3u8 -t 3600
python -m aiomoqt.tools.pub_media $RELAY -N abr --input https://cdn.example/live.mpd --rendition all -t 0

# One broadcast on two relays, redialing either if it drops
python -m aiomoqt.tools.pub_media moqt://relay-a:4433 moqt://relay-b:4433 -N demo/live --mp4 clip.mp4 --loop --reconnect -t 0

# For a player without a catalog
python -m aiomoqt.tools.pub_media $RELAY -N demo/bare --mp4 clip.mp4 --loop --no-catalog --loc-codecstring
```

## Receiving

`sub_media` reads the catalog, subscribes every media track it describes, and writes playable
files to `--out` (default `./media-out`):
- `video.h264` (Annex B);
- `audio.wav` (pcm-s16);
- `<role>.mp4` for CMAF and LOCMAF tracks.

Its other options:

| Flag | Effect |
|---|---|
| `--pipe video\|audio` | Streams one track to stdout for a player |
| `--inspect N` | Prints wire detail for each track's first N frames |
| `--show-catalog` | Prints the catalog and every update |
| `--analyze` | Measures delivery instead of writing files: latency, jitter, loss and reorder, group integrity, a playout model; `--report` writes them as CSV |
| `--discover` | Finds the broadcast under a namespace prefix |
| `--timestamp-errors cancel\|report` | Sets what a malformed timestamp property does |

```bash
python -m aiomoqt.tools.sub_media $RELAY -N demo/live --analyze -t 60 -i 5 --report run.csv
```

## Library

| Module | Provides |
|---|---|
| `media/broadcast.py` | `MediaPublisher`: a catalog plus media tracks on one session. `MediaSubscriber`: reads the catalog and subscribes its tracks |
| `media/catalog.py` | MSF catalog model: `Catalog`, `CatalogTrack`, `InitData`; JSON round-trip that keeps unknown fields |
| `media/loc.py` | LOC packaging: `LocTrackPublisher`, `LocTrackSubscriber`, the property IDs |
| `media/cmaf.py` | CMAF init segments and moof+mdat chunks from mp4 sample tables |
| `media/locmaf.py` | LOCMAF headers: `LocmafTrackPublisher`, `LocmafTrackSubscriber` |
| `media/sources.py` | mp4 reader (H.264, H.265, AV1; AAC, Opus; edit lists), Annex-B helpers, the pcm tone |
| `media/mpegts.py` | MPEG-TS demux to access units and audio frames, keeping the PES timestamps |
| `media/dash.py` | MPD parsing and segment addressing |
| `media/ingest.py` | PyAV ingest: any FFmpeg input to per-track CMAF chunks |
| `fanout.py` | `FanoutPublisher`: one broadcast on several relays |

The top-level [README](../../README.md#api) has a short publisher and subscriber example.

## Known limits

- One video track per run, except `--input --rendition all`.
- `--rendition all` fetches renditions in turn; large ladders fall behind real time.
- Every `--catalog-interval` resend carries a new `generatedAt`, so a player that compares it sees
  a catalog update each interval.
- With `--ts`, `-t` ends up to about 100 ms past the duration, partway into a group.
