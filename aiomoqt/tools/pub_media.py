#!/usr/bin/env python3
"""LOC/MSF media publisher — publishes an MSF broadcast (catalog +
audio, plus video from an mp4) to a relay.

  # audio-only (synthesized pcm-s16 tone), 30 s
  %(prog)s moqt://localhost:4433/ -N demo/live -t 30

  # audio + H.264 video lifted from an mp4 (no re-encode)
  %(prog)s https://relay.example/moq-relay -N demo/live --mp4 clip.mp4

  # relays that ignore bare PUBLISH: announce the namespace instead
  %(prog)s https://relay.example/ -N demo/live --mp4 clip.mp4 --pub-ns

  # the same broadcast to two relays: one process, one set of timestamps
  %(prog)s moqt://relay-a:4433/ moqt://relay-b:4433/ -N demo/live --mp4 clip.mp4

The video track sends the mp4's samples byte-for-byte as LOC canonical
payloads (loc-02 §2.1.3); the avcC extradata rides the catalog
initDataList and VIDEO_CONFIG group-start properties. Frames pace to
their timestamps (--no-pace to blast).

  # live H.264 Annex-B ingest (OBS/ffmpeg pipe; frames stamped on arrival)
  ffmpeg -i srt://0.0.0.0:9000?mode=listener -c:v copy -bsf:v h264_mp4toannexb \\
    -f h264 - | %(prog)s https://relay.example/moq-relay -N obs --h264 -

  # live MPEG-TS ingest: H.264 + AAC on one pipe, stamped from the PES PTS
  ffmpeg -fflags nobuffer -i srt://0.0.0.0:9000?mode=listener -map 0:v -map 0:a \\
    -c copy -f mpegts -flush_packets 1 - | %(prog)s https://relay.example/moq-relay -N obs --ts -

Each run prints a ready-to-paste player URL per relay (see
--player-base). Relays on different drafts need a --draft all of
them accept.
"""
import asyncio
import contextlib
import logging
import sys
import time
import uuid
from typing import Optional

from aiomoqt.client import MOQTClient
from aiomoqt.fanout import FanoutPublisher
from aiomoqt.media import (
    Catalog, CatalogTrack, InitData, LocTrackPublisher, StreamMapping,
)
from aiomoqt.media.cmaf import CmafChunker
from aiomoqt.media.mpegts import TsDemuxer
from aiomoqt.media.sources import (
    AnnexBAssembler, Mp4Reader, avcc_codec_string, pcm_tone_frames,
    sps_dimensions,
)
from aiomoqt.track import TrackState
from aiomoqt.utils import cli as _cli
from aiomoqt.utils.logger import set_log_level
from aiomoqt.utils.workers import apply_compat
from aiomoqt.utils.url import parse_relay_url

_SAMPLERATE = 48000
_CHANNELS = 2
_FRAME_MS = 20


def parse_args():
    parser = _cli.make_parser(
        'LOC/MSF media publisher (catalog + pcm tone + optional mp4 '
        'video)', epilog=__doc__)
    _cli.add_endpoints(parser)
    # Fresh per run: a relay caches objects under a namespace, so
    # reusing one serves a later subscriber stale groups from the
    # previous broadcast. The suffix sits under a fixed prefix so a
    # subscriber can still find it by namespace discovery (§9.4)
    # without being told, and the player URL carries it verbatim.
    _cli.add_identity(
        parser, namespace=f'aiomoqt/demo-{uuid.uuid4().hex[:4]}',
        namespace_help='MoQT namespace (default: aiomoqt/demo-<rand4>, '
                       'fresh per run; pass one explicitly to pin it)')
    parser.add_argument('--mp4', type=str, default=None, metavar='FILE',
                        help='Publish this mp4\'s H.264 track as LOC '
                             'video (samples pass through, no decode)')
    parser.add_argument('--loop', action='store_true',
                        help='Loop the mp4 for the full duration')
    parser.add_argument('--h264', type=str, default=None, metavar='FILE',
                        help='Publish a live H.264 Annex-B elementary '
                             'stream ("-" = stdin, e.g. an ffmpeg/OBS '
                             'pipe); frames are stamped on arrival')
    parser.add_argument('--ts', type=str, default=None, metavar='FILE',
                        help='Publish a live MPEG-TS stream ("-" = stdin): '
                             'H.264 video and AAC audio, stamped from '
                             'the PES timestamps')
    parser.add_argument('--player-base', type=str,
                        default='http://localhost:5173/g5-player/',
                        metavar='URL',
                        help='Base of the player URL printed at start '
                             '(default: the moq-playa simple example)')
    parser.add_argument('--packaging', choices=('loc', 'cmaf'),
                        default='loc',
                        help='Media packaging (cmsf draft): loc = '
                             'per-frame LOC payloads (default); cmaf = '
                             'CMAF chunks (moof+mdat per frame, header '
                             'in catalog initDataList). cmaf needs '
                             '--mp4.')
    parser.add_argument('--freq', type=float, default=440.0,
                        help='Tone frequency Hz (default: 440)')
    parser.add_argument('--no-pace', action='store_true',
                        help='Send frames as fast as accepted instead '
                             'of pacing to their timestamps')
    parser.add_argument('-D', '--datagram', action='store_true',
                        help='Send the AUDIO track as ObjectDatagrams '
                             '(raw QUIC only)')
    parser.add_argument('--no-audio', action='store_true',
                        help='Video only — omit the audio track')
    parser.add_argument('--tone', action='store_true',
                        help='Synthesized pcm-s16 tone audio even when '
                             'the mp4 has an AAC track (default: use '
                             'the mp4\'s AAC audio when present)')
    parser.add_argument('--target-latency', type=int, default=None,
                        metavar='MS',
                        help='Catalog targetLatency (msf-01 §5.2.8) — '
                             'players size their playout buffer from it')
    parser.add_argument('--loc01-compat', action='store_true',
                        help='Also emit timestamps under loc-01\'s '
                             'property id 0x02 for players not yet on '
                             'loc-02 numbering (moq-playa)')
    parser.add_argument('--loc-codecstring', action='store_true',
                        help='Make every LOC object self-describing for '
                             'players without a catalog '
                             '(moq-encoder-player): codec string (0x11, '
                             'proposed, not in loc-04), timescale (0x08) '
                             'and, on video, frame marking (0x09). With a '
                             'timescale, LOC reads timestamps as media '
                             'time rather than wall clock.')
    pub_mode = parser.add_mutually_exclusive_group()
    pub_mode.add_argument('--pub-ns', action='store_true',
                          help='PUBLISH_NAMESPACE only; the relay forwards '
                               'each SUBSCRIBE and objects start on its '
                               'Forward State. Default: bare PUBLISH per '
                               'track (see --forward).')
    pub_mode.add_argument('--pub-both', action='store_true',
                          help='PUBLISH_NAMESPACE plus per-track PUBLISH '
                               '(relays that want both)')
    parser.add_argument('--forward', type=int, default=0, choices=(0, 1),
                        help='Initial Forward State in PUBLISH (§9.13). '
                             '0 (default): send nothing until PUBLISH_OK, '
                             'SUBSCRIBE or an update carries forward=1. '
                             '1: start right after PUBLISH.')
    parser.add_argument('--catalog-interval', type=float, default=1.0,
                        metavar='SECS',
                        help='Re-emit the full catalog as a new group every '
                             'SECS seconds so later viewers can join (0 = '
                             'once at start; default: 1)')
    parser.add_argument('--stats', type=float, default=5.0, metavar='SECS',
                        help='Print per-track publish metrics every SECS '
                             'seconds (0 disables; default: 5)')
    _cli.add_run(parser, duration=30, interval=False)
    _cli.add_session(parser, keepalive=10, compat=True)
    _cli.add_help(parser)
    args = parser.parse_args()
    if sum(bool(s) for s in (args.mp4, args.h264, args.ts)) > 1:
        parser.error('--mp4, --h264 and --ts are mutually exclusive')
    if args.packaging == 'cmaf' and not args.mp4:
        parser.error('--packaging cmaf requires --mp4')
    if args.loc_codecstring and args.packaging != 'loc':
        parser.error('--loc-codecstring applies to LOC packaging only')
    return args


def _wt_url(relay, url: str) -> str:
    """The WebTransport URL a browser uses to reach this relay."""
    if url.startswith('https://'):
        return url
    return f"https://{relay.host}:{relay.port}{relay.path or '/moq-relay'}"


def _player_url(args, relay, url: str, draft=None) -> str:
    """The moq-playa URL that plays this run from one relay: its WT URL,
    the namespace, the draft it negotiated, and the playout target.
    LOC pins the render cushion to --target-latency."""
    q = [f"url={_wt_url(relay, url)}", f"ns={args.namespace}"]
    if draft is None:
        draft = args.draft[0] if isinstance(args.draft, list) else args.draft
    if draft is not None:
        q.append(f"v={draft}")
    q.append("catalogBootstrap=subscribe")
    if args.target_latency:
        knob = 'targetLatency' if args.packaging == 'cmaf' else 'cushion'
        q.append(f"{knob}={args.target_latency}")
    return args.player_base + '?' + '&'.join(q)


def _build_catalog(args, video, audio, chunkers=None) -> Catalog:
    packaging = args.packaging
    chunkers = chunkers or {}
    tracks = []
    init = []
    if audio is not None:
        tracks.append(CatalogTrack(
            name='audio', packaging=packaging, isLive=True, role='audio',
            renderGroup=1, codec=audio.codec_string,
            samplerate=audio.samplerate,
            channelConfig=str(audio.channels),
            bitrate=audio.avg_bitrate or 128_000, initRef='a0'))
        init.append(InitData.from_bytes(
            'a0', chunkers['audio'].init_segment()
            if 'audio' in chunkers else audio.asc))
    elif not args.no_audio:
        tracks.append(CatalogTrack(
            name='audio', packaging='loc', isLive=True, role='audio',
            renderGroup=1, codec='pcm-s16', samplerate=_SAMPLERATE,
            channelConfig=str(_CHANNELS),
            bitrate=_SAMPLERATE * _CHANNELS * 16))
    if video is not None:
        cmaf = 'video' in chunkers
        tracks.insert(0, CatalogTrack(
            name='video', packaging=packaging, isLive=True, role='video',
            renderGroup=1, codec=video.codec_string,
            width=video.width, height=video.height,
            framerate=video.fps,
            bitrate=video.avg_bitrate or 2_000_000,
            initRef='v0' if (cmaf or video.config) else None))
        if cmaf:
            init.append(InitData.from_bytes(
                'v0', chunkers['video'].init_segment()))
        elif video.config:
            init.append(InitData.from_bytes('v0', video.config))
    if getattr(args, 'target_latency', None) is not None:
        for t in tracks:
            t.targetLatency = args.target_latency
    return Catalog(generatedAt=int(time.time() * 1000), tracks=tracks,
                   initDataList=init or None)


class _LiveH264:
    """Catalog-facing view of a live Annex-B source."""

    def __init__(self, asm: AnnexBAssembler):
        self.config = asm.config
        self.codec_string = avcc_codec_string(self.config)
        self.width, self.height = sps_dimensions(asm.sps)
        self.fps = None
        self.avg_bitrate = None


def _open_live_h264(args):
    """Read the stream until SPS+PPS arrive so the catalog can carry
    codec/config; returns (fh, assembler, frames-read-so-far, view)."""
    fh = sys.stdin.buffer if args.h264 == '-' else open(args.h264, 'rb')
    asm = AnnexBAssembler()
    first = []
    while asm.config is None:
        chunk = fh.read1(65536)
        if not chunk:
            raise SystemExit('  error: h264 stream ended before SPS/PPS')
        first += asm.feed(chunk)
    return fh, asm, first, _LiveH264(asm)


async def _feed_h264_live(track, fh, asm, first, args, stats):
    loop = asyncio.get_running_loop()
    deadline = time.monotonic() + args.duration
    frames = list(first)
    eof = False
    while True:
        for payload, key in frames:
            await _send(track, stats, payload, key)
        if eof or time.monotonic() >= deadline:
            break
        # read1: return on first available bytes; read(n) waits for 64 KB.
        chunk = await loop.run_in_executor(None, fh.read1, 65536)
        if chunk:
            frames = asm.feed(chunk)
        else:
            frames, eof = asm.close(), True
    await track.finish()


class _LiveAac:
    """Catalog-facing view of a live ADTS audio stream."""

    def __init__(self, dmx: TsDemuxer):
        self.asc = dmx.audio_asc
        self.samplerate = dmx.audio_samplerate
        self.channels = dmx.audio_channels
        self.avg_bitrate = None

    @property
    def codec_string(self) -> str:
        return f"mp4a.40.{(self.asc[0] >> 3) & 0x1F}"


def _open_live_ts(args):
    """Read the stream until the PMT and the codec configs the catalog
    needs are in; returns (fh, demuxer, units-so-far, video, audio)."""
    fh = sys.stdin.buffer if args.ts == '-' else open(args.ts, 'rb')
    dmx = TsDemuxer()
    first = []

    def ready():
        if not dmx.pmt_seen:
            return False
        if dmx.video_pid is not None and dmx.video.config is None:
            return False
        want_audio = not args.no_audio and dmx.audio_pid is not None
        return not want_audio or dmx.audio_asc is not None

    while not ready():
        chunk = fh.read1(65536)
        if not chunk:
            raise SystemExit('  error: ts stream ended before PMT and codec configs')
        units = dmx.feed(chunk)
        if units and not first:
            dmx.to_us(units[0].pts)  # anchor at arrival, not at first send
        first += units
    video = _LiveH264(dmx.video) if dmx.video_pid is not None else None
    audio = (_LiveAac(dmx)
             if dmx.audio_pid is not None and not args.no_audio else None)
    if video is None and audio is None:
        raise SystemExit('  error: ts stream has no H.264 video or AAC audio')
    return fh, dmx, first, video, audio


async def _apply_config_change(dmx: TsDemuxer, tracks: dict, pub):
    """A new SPS/PPS or ADTS header: the track's group-start config and
    the catalog follow it, and the catalog is republished."""
    cat = pub.catalog_track.catalog
    init = {i.id: i for i in (cat.initDataList or [])}
    video = tracks.get('video')
    if video is not None and dmx.video.config != video.config:
        video.config = dmx.video.config
        view = _LiveH264(dmx.video)
        if video.codec_string is not None:
            video.codec_string = view.codec_string
        for t in cat.tracks:
            if t.name == 'video':
                t.codec, t.width, t.height = (view.codec_string, view.width,
                                              view.height)
        if 'v0' in init:
            init['v0'].data = InitData.from_bytes('v0', view.config).data
        print(f"  video config changed: {view.width}x{view.height} "
              f"{view.codec_string}")
    audio = tracks.get('audio')
    if audio is not None and dmx.audio_asc != audio.config:
        audio.config = dmx.audio_asc
        view = _LiveAac(dmx)
        if audio.codec_string is not None:
            audio.codec_string = view.codec_string
        for t in cat.tracks:
            if t.name == 'audio':
                t.codec, t.samplerate = view.codec_string, view.samplerate
                t.channelConfig = str(view.channels)
        if 'a0' in init:
            init['a0'].data = InitData.from_bytes('a0', view.asc).data
        print(f"  audio config changed: {view.codec_string} "
              f"{view.samplerate}Hz {view.channels}ch")
    cat.generatedAt = int(time.time() * 1000)
    if pub.catalog_track.state == TrackState.SUBSCRIBED:
        await pub.catalog_track.publish_catalog(cat)


async def _feed_ts_live(tracks: dict, fh, dmx: TsDemuxer, first, args,
                        stats: dict, pub):
    """Dispatch demuxed access units to their tracks with PTS-derived
    stamps."""
    loop = asyncio.get_running_loop()
    deadline = time.monotonic() + args.duration
    units = list(first)
    eof = False
    while True:
        if dmx.config_changed:
            dmx.config_changed = False
            await _apply_config_change(dmx, tracks, pub)
        for u in units:
            track = tracks.get(u.kind)
            if track is not None:
                await _send(track, stats[u.kind], u.payload, u.key,
                            timestamp=dmx.to_us(u.pts))
        if eof or time.monotonic() >= deadline:
            break
        chunk = await loop.run_in_executor(None, fh.read1, 65536)
        if chunk:
            units = dmx.feed(chunk)
        else:
            units, eof = dmx.close(), True
    for track in tracks.values():
        await track.finish()


async def _pace(start: float, ts_us: int, pace: bool):
    if pace:
        delay = start + ts_us / 1e6 - time.monotonic()
        if delay > 0.001:
            await asyncio.sleep(delay)


async def _send(track, stats, payload: bytes, key: bool,
                timestamp: Optional[int] = None):
    """Send one frame, or drop it while no subscriber has started
    generation: a paced feeder stays on the clock instead of queueing a
    backlog for a late joiner.

    LOC timestamps without a TIMESCALE property are µs since the Unix
    epoch — players schedule against the wall clock. Sources without
    their own presentation clock are stamped at send time."""
    if track.demand[0] == 0:
        stats.dropped += 1
        return
    entry_us = int(time.time() * 1_000_000)
    if timestamp is None:
        timestamp = entry_us
    await track.send_frame(payload, key_frame=key, timestamp=timestamp)
    # lag: how late the frame already was when we got it (source, pipe,
    # demux). tx: what handing it to the track cost us.
    stats.count(payload, entry_us - timestamp,
                int(time.time() * 1_000_000) - entry_us)


class _TrackStats:
    """Publish-side counters: objects and bytes for the interval rate and
    bitrate, dropped for frames skipped before a subscriber arrived, and
    how far behind each frame's capture stamp it left this process.

    Send lag measures OUR delay — pacing error, demux and queueing — not
    absolute capture-to-send: a live source's stamps are anchored to
    their own arrival, so a constant upstream delay is inside the anchor
    and reads as zero. Capture-to-parse latency belongs to the receiver
    (sub_media's ts_skew_ms, the player's overlay)."""
    __slots__ = ('objects', 'bytes', 'dropped',
                 '_lag_sum', '_lag_n', '_lag_max', '_tx_sum')

    def __init__(self):
        self.objects = 0
        self.bytes = 0
        self.dropped = 0
        self._lag_sum = 0
        self._lag_n = 0
        self._lag_max = 0
        self._tx_sum = 0

    def count(self, payload: bytes, lag_us: int = 0, tx_us: int = 0):
        self.objects += 1
        self.bytes += len(payload)
        # Signed: negative means the stamp is AHEAD of the wall clock,
        # which a receiver reads as negative end-to-end latency.
        self._lag_sum += lag_us
        self._lag_n += 1
        if abs(lag_us) > abs(self._lag_max):
            self._lag_max = lag_us
        self._tx_sum += max(0, tx_us)

    def take_lag(self):
        """(mean lag, max lag, mean tx) in ms since the last call, or
        None when nothing was sent; resets the interval."""
        if not self._lag_n:
            return None
        out = (self._lag_sum / self._lag_n / 1000, self._lag_max / 1000,
               self._tx_sum / self._lag_n / 1000)
        self._lag_sum = self._lag_n = self._lag_max = self._tx_sum = 0
        return out


async def _refresh_catalog(pub: FanoutPublisher, interval: float):
    """Periodically re-emit the current catalog as a new group, once a
    subscriber has started the catalog generator."""
    track = pub.catalog_track
    while True:
        await asyncio.sleep(interval)
        if track.state != TrackState.SUBSCRIBED:
            continue
        track.catalog.generatedAt = int(time.time() * 1000)
        await track.publish_catalog(track.catalog)


async def _report_stats(stats: dict, interval: float, pub: FanoutPublisher):
    """One line per interval: per-track object total, object rate and
    bitrate over the interval, frames dropped before a subscriber
    arrived, and with several relays the demand count and frames shed
    per relay (index = URL position)."""
    t0 = time.monotonic()
    prev_obj = {name: 0 for name in stats}
    prev_bytes = {name: 0 for name in stats}
    while True:
        await asyncio.sleep(interval)
        parts = []
        for name, st in stats.items():
            ops = (st.objects - prev_obj[name]) / interval
            kbps = (st.bytes - prev_bytes[name]) * 8 / interval / 1000
            prev_obj[name] = st.objects
            prev_bytes[name] = st.bytes
            lag = st.take_lag()
            line = (f"{name}: {st.objects} obj · {ops:.0f} obj/s · {kbps:.0f} kbps"
                    + (f" · lag {lag[0]:.0f}/{lag[1]:.0f} ms" if lag else "")
                    + (f" · tx {lag[2]:.1f} ms" if lag and lag[2] >= 0.05 else "")
                    + (f" · dropped {st.dropped}" if st.dropped else ""))
            track = pub.tracks.get(name)
            if track is not None and len(pub.sessions) > 1:
                have, total = track.demand
                line += f" · demand {have}/{total}"
                shed = getattr(track, 'shed', {})
                if shed:
                    line += " · shed " + ",".join(
                        f"{i}:{n}" for i, n in sorted(shed.items()))
            parts.append(line)
        m, s = divmod(time.monotonic() - t0, 60)
        print(f"  [pub {int(m)}:{s:06.3f}] " + " · ".join(parts))


async def _feed_tone(track, args, stats: _TrackStats):
    start = time.monotonic()
    for payload, ts in pcm_tone_frames(
            duration_s=args.duration, freq=args.freq,
            samplerate=_SAMPLERATE, channels=_CHANNELS,
            frame_ms=_FRAME_MS):
        await _pace(start, ts, not args.no_pace)
        await _send(track, stats, payload, True)
    await track.finish()


async def _feed_mp4_track(track, source, args, stats: _TrackStats, *,
                          all_key=False, gap_us=33_333, wrap=None):
    """Feed an mp4 track's samples, paced to their media timestamps and
    stamped with the wall clock at send (LOC default clock); --loop
    restarts the file at later timestamps (audio: every AU is a sync
    frame, giving LOC's one-object-per-group audio mapping).
    wrap(sample) transforms the payload (CMAF chunking)."""
    start = time.monotonic()
    base_us = 0
    while True:
        last = 0
        for s in source.samples():
            ts = base_us + s.timestamp_us
            if ts > args.duration * 1_000_000:
                break
            await _pace(start, ts, not args.no_pace)
            payload = wrap(s) if wrap else s.payload
            key = all_key or s.key_frame
            await _send(track, stats, payload, key)
            last = ts
        base_us = last + gap_us
        if not args.loop or base_us > args.duration * 1_000_000:
            break
    await track.finish()


async def run(args):
    set_log_level(logging.DEBUG if args.debug else logging.WARNING)
    libquicr = apply_compat(getattr(args, 'compat', ''))
    urls = args.url if isinstance(args.url, list) else [args.url]
    relays = [parse_relay_url(u) for u in urls]
    reader = Mp4Reader(args.mp4) if args.mp4 else None
    video = reader.video if reader else None
    live = _open_live_h264(args) if args.h264 else None
    if live:
        video = live[3]
    ts = _open_live_ts(args) if args.ts else None
    ts_audio = None
    if ts:
        video, ts_audio = ts[3], ts[4]
        if ts_audio is None:
            args.no_audio = True
    mp4_audio = (reader.audio
                 if reader and not (args.tone or args.no_audio) else None)
    chunkers = {}
    if args.packaging == 'cmaf':
        chunkers['video'] = CmafChunker(video)
        if mp4_audio is not None:
            chunkers['audio'] = CmafChunker(mp4_audio)
        elif not args.no_audio:
            print("  note: cmaf packaging — tone audio skipped "
                  "(no AAC track in the mp4)")
            args.no_audio = True
    catalog = _build_catalog(args, video, mp4_audio or ts_audio, chunkers)

    clients = [MOQTClient(
        relay.host, relay.port, path=relay.path,
        use_quic=relay.use_quic, verify_tls=not args.insecure,
        supported_drafts=args.draft, debug=args.debug,
        keylog_filename=args.keylogfile,
        congestion_control_algorithm=args.cc_algo,
        keep_alive_interval=args.keepalive,
        libquicr_compat=libquicr,
    ) for relay in relays]
    print(f"  relay: {', '.join(str(r) for r in relays)}  "
          f"namespace: {args.namespace}")
    print(f"  tracks: {', '.join(t.name for t in catalog.tracks)}")
    async with contextlib.AsyncExitStack() as stack:
        async def _open(url, client):
            try:
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s
            except Exception as e:
                print(f"  error: {url}: {e}")
                return None

        # A relay that cannot be reached is left out; the run fails only
        # when none can.
        opened = await asyncio.gather(*(_open(u, c)
                                        for u, c in zip(urls, clients)))
        up = [i for i, s in enumerate(opened) if s is not None]
        if not up:
            raise SystemExit(1)
        urls = [urls[i] for i in up]
        relays = [relays[i] for i in up]
        sessions = [opened[i] for i in up]
        for url, relay, s in zip(urls, relays, sessions):
            draft = getattr(s, 'negotiated_draft', None)
            print(f"  player: {_player_url(args, relay, url, draft)}")

        def _cs(codec):
            return codec if args.loc_codecstring else None
        session = sessions[0]
        pub = FanoutPublisher(sessions, args.namespace, catalog)
        stats = {}
        feeders = []
        if ts is not None:
            fh, dmx, first, _, _ = ts
            tracks = {}
            if video is not None:
                tracks['video'] = pub.add_track(LocTrackPublisher(
                    session, args.namespace, 'video', config=video.config,
                    loc01_compat=args.loc01_compat,
                    codec_string=_cs(video.codec_string)))
                stats['video'] = _TrackStats()
            if ts_audio is not None:
                tracks['audio'] = pub.add_track(LocTrackPublisher(
                    session, args.namespace, 'audio', media_kind='audio',
                    config=ts_audio.asc,
                    mapping=(StreamMapping.DATAGRAM if args.datagram
                             else StreamMapping.PER_GROUP),
                    loc01_compat=args.loc01_compat,
                    codec_string=_cs(ts_audio.codec_string)))
                stats['audio'] = _TrackStats()
            feeders.append(_feed_ts_live(tracks, fh, dmx, first, args,
                                         stats, pub))
        elif not args.no_audio:
            audio_track = pub.add_track(LocTrackPublisher(
                session, args.namespace, 'audio', media_kind='audio',
                config=mp4_audio.asc if mp4_audio is not None else None,
                mapping=(StreamMapping.DATAGRAM if args.datagram
                         else StreamMapping.PER_GROUP),
                loc01_compat=args.loc01_compat,
                codec_string=_cs(mp4_audio.codec_string
                                 if mp4_audio is not None else 'pcm-s16')))
            stats['audio'] = _TrackStats()
            if mp4_audio is not None:
                a_ck = chunkers.get('audio')
                feeders.append(_feed_mp4_track(
                    audio_track, mp4_audio, args, stats['audio'],
                    all_key=True,
                    gap_us=1_000_000 * 1024 // mp4_audio.samplerate,
                    wrap=(lambda s, ck=a_ck:
                          ck.chunk(s.payload, s.duration)) if a_ck
                    else None))
            else:
                feeders.append(_feed_tone(audio_track, args,
                                          stats['audio']))
        if live is not None:
            fh, asm, first, _ = live
            stats['video'] = _TrackStats()
            feeders.append(_feed_h264_live(
                pub.add_track(LocTrackPublisher(
                    session, args.namespace, 'video', config=video.config,
                    loc01_compat=args.loc01_compat,
                    codec_string=_cs(video.codec_string))),
                fh, asm, first, args, stats['video']))
        elif video is not None and ts is None:   # --ts wired both tracks above
            v_ck = chunkers.get('video')
            stats['video'] = _TrackStats()
            feeders.append(_feed_mp4_track(
                pub.add_track(LocTrackPublisher(
                    session, args.namespace, 'video',
                    config=None if v_ck else video.config,
                    loc01_compat=args.loc01_compat,
                    codec_string=_cs(video.codec_string))),
                video, args, stats['video'],
                gap_us=int(1e6 / (video.fps or 30)),
                wrap=(lambda s, ck=v_ck:
                      ck.chunk(s.payload, s.duration, s.key_frame))
                if v_ck else None))
        await pub.start(announce_namespace=(args.pub_ns or args.pub_both),
                        publish_track=(not args.pub_ns or args.pub_both),
                        forward=args.forward)
        print("  publishing...")
        reporter = (asyncio.ensure_future(
            _report_stats(stats, args.stats, pub))
            if args.stats > 0 and stats else None)
        refresher = (asyncio.ensure_future(
            _refresh_catalog(pub, args.catalog_interval))
            if args.catalog_interval > 0 else None)
        feed = asyncio.ensure_future(asyncio.gather(*feeders))
        # An interrupted run cancels the feeders; collect that outcome so
        # Ctrl-C exits quietly instead of logging an unretrieved exception.
        feed.add_done_callback(
            lambda f: f.cancelled() or f.exception())
        closed = {asyncio.ensure_future(s.async_closed()): s
                  for s in sessions}
        # A relay lost mid-run is dropped and the rest continue; the
        # run fails only when no relay is left.
        while True:
            done, _ = await asyncio.wait({feed, *closed},
                                         return_when=asyncio.FIRST_COMPLETED)
            if feed in done:
                break
            for fut in done:
                s = closed.pop(fut)
                code, reason = getattr(s, '_close_err', None) or ('?', '')
                url = urls[sessions.index(s)]
                if closed:
                    print(f"  error: {url} closed: code={code} "
                          f"reason='{reason}' — continuing on {len(closed)}")
                    pub.drop(s)
                    continue
                if reporter is not None:
                    reporter.cancel()
                if refresher is not None:
                    refresher.cancel()
                print(f"  error: session closed: code={code} "
                      f"reason='{reason}'")
                feed.cancel()
                raise SystemExit(1)
        if reporter is not None:
            reporter.cancel()
        if refresher is not None:
            refresher.cancel()
        for fut in closed:
            fut.cancel()
        await feed
        await pub.catalog_track.finish()
        await asyncio.sleep(1.0)  # drain tail before teardown
    print("  done")


def main():
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
