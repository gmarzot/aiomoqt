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

  # live MPEG-TS ingest: H.264 + AAC on one pipe, stamped from the PES PTS;
  # the muxer flags stop ffmpeg batching ~300 ms of audio into one PES
  ffmpeg -fflags nobuffer -i srt://0.0.0.0:9000?mode=listener -map 0:v -map 0:a \\
    -c copy -f mpegts -pes_payload_size 0 -omit_video_pes_length 0 -muxdelay 0 \\
    -flush_packets 1 - | %(prog)s https://relay.example/moq-relay -N obs --ts -

  # HLS/DASH (anything FFmpeg opens) as CMAF tracks; needs the media
  # extra: pip install 'aiomoqt[media]'. --rendition all is experimental.
  %(prog)s https://relay.example/moq-relay -N vod --input https://cdn.example/master.m3u8
  %(prog)s https://relay.example/moq-relay -N abr --input https://cdn.example/live.mpd --rendition all

With --player-base or $AIOMOQT_PLAYER_URL set, each run prints a
ready-to-paste player URL per relay. Relays on different drafts need a
--draft all of them accept.
"""
import asyncio
import contextlib
import functools
import json
import logging
import math
import os
import signal
import sys
import threading
import time
import uuid
from typing import Optional

from aiomoqt.client import MOQTClient
from aiomoqt.delivery import DatagramTooLarge
from aiomoqt.fanout import FanoutPublisher
from aiomoqt.media import (
    Catalog, CatalogTrack, InitData, LocTrackPublisher, LocmafTrackPublisher,
    StreamMapping,
)
from aiomoqt.media.catalog import (
    MEDIA_PACKAGINGS, PACKAGING_CMAF, PACKAGING_LOC, PACKAGING_LOCMAF,
)
from aiomoqt.media.cmaf import CmafChunker, chunk_decode_time, prft_box
from aiomoqt.media.ingest import INSTALL_HINT, RENDITIONS, Ingest
from aiomoqt.media.locmaf import LOCMAF_VERSION
from aiomoqt.media.mpegts import TsDemuxer
from aiomoqt.media.sources import (
    AnnexBAssembler, HevcAnnexBAssembler, Mp4Reader, avcc_codec_string,
    hevc_sps_info, hvcc_codec_string, pcm_tone_frames, sps_dimensions,
)
from aiomoqt.track import TrackState
from aiomoqt.utils import cli as _cli
from aiomoqt.utils.logger import set_log_level
from aiomoqt.utils.timing import MediaTimestamps
from aiomoqt.utils.workers import apply_compat
from aiomoqt.utils.url import parse_relay_url

# Exit status when an object does not fit a datagram.
EXIT_DATAGRAM_TOO_LARGE = 64

_SAMPLERATE = 48000
_CHANNELS = 2
_FRAME_MS = 20
# LOCMAF audio from --mp4 by default: ~1 s groups, so deltas carry most
# frames.
_LOCMAF_AUDIO_GROUP_US = 1_000_000


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
                        help='Publish this mp4\'s H.264, H.265 or AV1 track '
                             'as video (samples pass through, no decode)')
    parser.add_argument('--loop', action='store_true',
                        help='Loop the mp4 for the full duration')
    parser.add_argument('--h264', type=str, default=None, metavar='FILE',
                        help='Publish a live H.264 Annex-B elementary '
                             'stream ("-" = stdin, e.g. an ffmpeg/OBS '
                             'pipe); frames are stamped on arrival')
    parser.add_argument('--ts', type=str, default=None, metavar='FILE',
                        help='Publish a live MPEG-TS stream ("-" = stdin): '
                             'H.264 or H.265 video and AAC or Opus audio, '
                             'stamped from the PES timestamps')
    parser.add_argument('--input', type=str, default=None, metavar='URL',
                        help='Publish anything FFmpeg opens (HLS or DASH '
                             'playlist, file, ...) as CMAF tracks, paced '
                             'to its timestamps; needs PyAV '
                             f"({INSTALL_HINT})")
    parser.add_argument('--rendition', choices=RENDITIONS, default=None,
                        help='With --input: best = the top video '
                             'rendition (default); all = every video '
                             'rendition, as one catalog alternate group '
                             '(experimental: renditions are fetched in '
                             'turn, so large ladders fall behind real '
                             'time)')
    parser.add_argument('--player-base', type=str,
                        default=os.environ.get('AIOMOQT_PLAYER_URL'),
                        metavar='URL',
                        help='Player page for the URL printed at start '
                             '(default: $AIOMOQT_PLAYER_URL; no player '
                             'line when unset)')
    parser.add_argument('--packaging', choices=MEDIA_PACKAGINGS,
                        default=None,
                        help='Media packaging: loc = per-frame LOC '
                             'payloads (default); cmaf = CMAF chunks '
                             '(moof+mdat per frame, header in catalog '
                             'initDataList); locmaf = the same chunks with '
                             'compact moof headers (draft-einarsson-moq-'
                             'locmaf). cmaf and locmaf need --mp4 or '
                             '--input; --input defaults to cmaf.')
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
                             'the mp4 has an AAC or Opus track (default: '
                             'use the mp4\'s audio when present)')
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
                             'proposed, not in loc-04) and, on video, '
                             'frame marking (0x09). Timestamps stay '
                             'wall-clock µs (no timescale).')
    parser.add_argument('--loc-timescale', type=int, default=None,
                        metavar='TICKS',
                        help='LOC timestamps as media time at TICKS per '
                             'second: TIMESCALE (0x08) on the first object '
                             'of each group, and on every object with '
                             '--loc-codecstring. Default: wall-clock µs, '
                             'no TIMESCALE. 1000000 keeps µs values.')
    parser.add_argument('--loc-bare', action='store_true',
                        help='LOC payloads without the (empty) Private '
                             'Properties block 0x0A 0x00, for receivers '
                             'predating loc-04 §2.2')
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
    map_group = parser.add_argument_group('object mapping')
    map_group.add_argument(
        '--video-subgroups', choices=('group', 'object'), default='group',
        help='Video objects to subgroup streams: group = one subgroup '
             'stream per group (default); object = one per object, '
             'subgroup ID = object ID. Not with locmaf.')
    map_group.add_argument(
        '--audio-frames-per-group', type=int, default=None, metavar='N',
        help='Audio frames per group, 1-100, one object each (default: '
             '1; locmaf: about 1 s). cmaf and locmaf: a group is one CMAF '
             'fragment of N one-frame chunks.')
    map_group.add_argument(
        '--audio-subgroups', choices=('group', 'object'), default='group',
        help='Audio objects to subgroup streams, as --video-subgroups. '
             'Not with locmaf or --datagram.')
    ts_group = parser.add_argument_group('timestamp writing')
    ts_group.add_argument(
        '--timestamp-mapping', action='store_true',
        help='Also send draft-frindell-moq-timestamp timing: Track '
             'Properties TIMESCALE and TIMESTAMP_MAPPING (wall clock at '
             'start, group and frame period), OBJECT_TIMESTAMP only on a '
             'frame off schedule, and a prft box on the first CMAF/LOCMAF '
             'object of each group. Sources without a fixed GOP (live '
             'inputs, irregular keyframes) send OBJECT_TIMESTAMP on every '
             'object instead. LOC timestamps are unchanged.')
    out_group = parser.add_argument_group('machine output')
    out_group.add_argument(
        '--stats-json', action='store_true',
        help='Events as "@mtt <json>" lines on stdout (ready, stats every '
             '--stats interval, endpoint_lost, done); human output goes '
             'to stderr')
    _cli.add_run(parser, duration=30)
    _cli.add_session(parser)
    _cli.add_keepalive(parser, default=10)
    _cli.add_compat(parser)
    _cli.add_help(parser)
    args = parser.parse_args()
    if sum(bool(s) for s in (args.mp4, args.h264, args.ts, args.input)) > 1:
        parser.error('--mp4, --h264, --ts and --input are mutually exclusive')
    if args.input:
        if args.packaging == PACKAGING_LOC:
            parser.error('--input publishes cmaf or locmaf packaging')
        for flag, on in (('--loop', args.loop), ('--tone', args.tone),
                         ('--datagram', args.datagram),
                         ('--loc-codecstring', args.loc_codecstring)):
            if on:
                parser.error(f'{flag} does not apply to --input')
    elif args.rendition:
        parser.error('--rendition applies to --input only')
    args.packaging = args.packaging or (
        PACKAGING_CMAF if args.input else PACKAGING_LOC)
    args.rendition = args.rendition or 'best'
    if args.packaging != PACKAGING_LOC and not (args.mp4 or args.input):
        parser.error(f'--packaging {args.packaging} requires --mp4 or --input')
    if args.loc_timescale is not None and args.loc_timescale <= 0:
        parser.error('--loc-timescale must be positive')
    for flag, on in (('--loc-codecstring', args.loc_codecstring),
                     ('--loc-bare', args.loc_bare),
                     ('--loc-timescale', args.loc_timescale)):
        if on and args.packaging != PACKAGING_LOC:
            parser.error(f'{flag} applies to LOC packaging only')
    if args.datagram and args.packaging == PACKAGING_LOCMAF:
        parser.error('--datagram does not apply to locmaf: its objects '
                     'need subgroup streams')
    if args.video_subgroups == 'object':
        if args.packaging == PACKAGING_LOCMAF:
            parser.error('--video-subgroups object does not apply to '
                         'locmaf: its objects need per-group streams')
        if args.input:
            parser.error('--video-subgroups does not apply to --input')
    n = args.audio_frames_per_group
    if n is not None and not 1 <= n <= 100:
        parser.error('--audio-frames-per-group must be 1-100')
    if args.audio_subgroups == 'object':
        if args.packaging == PACKAGING_LOCMAF:
            parser.error('--audio-subgroups object does not apply to '
                         'locmaf: its objects need per-group streams')
        if args.datagram:
            parser.error('--audio-subgroups object and --datagram are '
                         'exclusive')
    if args.input and (n is not None or args.audio_subgroups != 'group'):
        parser.error('--audio-frames-per-group and --audio-subgroups do '
                     'not apply to --input')
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
        knob = 'cushion' if args.packaging == PACKAGING_LOC else 'targetLatency'
        q.append(f"{knob}={args.target_latency}")
    return args.player_base + '?' + '&'.join(q)


def _build_catalog(args, video, audio, chunkers=None) -> Catalog:
    packaging = args.packaging
    chunkers = chunkers or {}
    tracks = []
    init = []
    if audio is not None:
        # Opus over LOC needs no decoder description, so no initData.
        a_init = (chunkers['audio'].init_segment() if 'audio' in chunkers
                  else audio.config)
        tracks.append(CatalogTrack(
            name='audio', packaging=packaging, isLive=True, role='audio',
            renderGroup=1, codec=audio.codec_string,
            samplerate=audio.samplerate,
            channelConfig=str(audio.channels),
            bitrate=audio.avg_bitrate or 128_000,
            initRef='a0' if a_init else None))
        if a_init:
            init.append(InitData.from_bytes('a0', a_init))
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
    for t in tracks:
        if t.packaging == PACKAGING_LOCMAF:
            t.locmafVersion = LOCMAF_VERSION
    return Catalog(generatedAt=int(time.time() * 1000), tracks=tracks,
                   initDataList=init or None)


def _build_ingest_catalog(args, ingest: Ingest) -> Catalog:
    """One cmaf (or locmaf) track per ingested stream; several video
    renditions form one alternate group (msf §5.2.12)."""
    packaging = getattr(args, 'packaging', None) or PACKAGING_CMAF
    renditions = sum(t.kind == 'video' for t in ingest.tracks)
    tracks = []
    init = []
    for i, t in enumerate(ingest.tracks):
        ref = f"i{i}"
        if t.kind == 'video':
            entry = CatalogTrack(
                name=t.name, packaging=packaging, isLive=True, role='video',
                renderGroup=1, altGroup=1 if renditions > 1 else None,
                codec=t.codec, width=t.width, height=t.height,
                framerate=t.fps, bitrate=t.bitrate or 2_000_000,
                initRef=ref)
        else:
            entry = CatalogTrack(
                name=t.name, packaging=packaging, isLive=True, role='audio',
                renderGroup=1, codec=t.codec, samplerate=t.samplerate,
                channelConfig=str(t.channels),
                bitrate=t.bitrate or 128_000, initRef=ref)
        entry.targetLatency = args.target_latency
        if packaging == PACKAGING_LOCMAF:
            entry.locmafVersion = LOCMAF_VERSION
        tracks.append(entry)
        init.append(InitData.from_bytes(ref, t.init))
    return Catalog(generatedAt=int(time.time() * 1000), tracks=tracks,
                   initDataList=init)


async def _open_ingest(args) -> Ingest:
    """Open --input and read until every track has its CMAF header. A
    cancelled open (Ctrl-C) stops the executor thread too."""
    stop = threading.Event()
    open_ = functools.partial(Ingest, args.input, rendition=args.rendition,
                              audio=not args.no_audio, stop=stop)
    try:
        ingest = await asyncio.get_running_loop().run_in_executor(None, open_)
    except asyncio.CancelledError:
        stop.set()
        raise
    except ImportError as e:
        raise SystemExit(f"  error: {e}")
    except Exception as e:
        raise SystemExit(f"  error: {args.input}: {type(e).__name__}: {e}")
    unknown = [t.name for t in ingest.tracks if t.codec is None]
    if unknown:
        ingest.close()
        raise SystemExit(f"  error: no codec string for {', '.join(unknown)}")
    for t in ingest.tracks:
        size = f" {t.width}x{t.height}" if t.kind == 'video' else ''
        print(f"  input {t.name}: {t.codec}{size}"
              + (f" {t.bitrate // 1000} kbps" if t.bitrate else ''))
    return ingest


async def _feed_ingest(ingest: Ingest, tracks: dict, args, stats: dict):
    """Send each CMAF chunk paced to its decode time; the first chunk
    anchors the clock, so a live input runs as far behind its edge as
    the demuxer starts."""
    start = first = wall0 = None
    try:
        async for c in ingest.chunks():
            if first is None:
                start, first = time.monotonic(), c.time_us
                wall0 = int(time.time() * 1_000_000)
            ts = c.time_us - first
            if ts > args.duration * 1_000_000:
                break
            await _pace(start, ts, not args.no_pace)
            payload = c.payload
            if c.key and getattr(args, 'timestamp_mapping', False):
                payload = _with_prft(payload, wall0 + ts)
            await _send(tracks[c.track], stats[c.track], payload, c.key,
                        timestamp=wall0 + ts, group_id=c.group_id)
    finally:
        ingest.close()
    for track in tracks.values():
        await track.finish()


class _LiveVideo:
    """Catalog-facing view of a live Annex-B source. H.265 parameter
    sets stay in band, so its codec string is hev1."""

    def __init__(self, asm: AnnexBAssembler):
        self.config = asm.config
        if isinstance(asm, HevcAnnexBAssembler):
            self.codec_string = hvcc_codec_string(self.config, 'hev1')
            info = hevc_sps_info(asm.sps)
            self.width, self.height = info['width'], info['height']
        else:
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
    return fh, asm, first, _LiveVideo(asm)


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


class _LiveAudio:
    """Catalog-facing view of a live AAC (ADTS) or Opus audio stream."""

    def __init__(self, dmx: TsDemuxer):
        self.config = dmx.audio_asc
        self.samplerate = dmx.audio_samplerate
        self.channels = dmx.audio_channels
        self.avg_bitrate = None
        self.codec_string = ('opus' if dmx.audio_codec == 'opus'
                             else f"mp4a.40.{(self.config[0] >> 3) & 0x1F}")


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
        return not want_audio or dmx.audio_samplerate is not None

    while not ready():
        chunk = fh.read1(65536)
        if not chunk:
            raise SystemExit('  error: ts stream ended before PMT and codec configs')
        units = dmx.feed(chunk)
        if units and not first:
            dmx.to_us(units[0].pts)  # anchor at arrival, not at first send
        first += units
    video = _LiveVideo(dmx.video) if dmx.video_pid is not None else None
    audio = (_LiveAudio(dmx)
             if dmx.audio_pid is not None and not args.no_audio else None)
    if video is None and audio is None:
        raise SystemExit('  error: ts stream has no H.264/H.265 video or '
                         'AAC/Opus audio')
    return fh, dmx, first, video, audio


async def _apply_config_change(dmx: TsDemuxer, tracks: dict, pub):
    """A new SPS/PPS or ADTS header: the track's group-start config and
    the catalog follow it, and the catalog is republished."""
    cat = pub.catalog_track.catalog
    init = {i.id: i for i in (cat.initDataList or [])}
    video = tracks.get('video')
    if video is not None and dmx.video.config != video.config:
        video.config = dmx.video.config
        view = _LiveVideo(dmx.video)
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
    entry = cat.find('audio')
    if audio is not None and (
            dmx.audio_asc != audio.config or (
                entry is not None
                and entry.channelConfig != str(dmx.audio_channels))):
        audio.config = dmx.audio_asc
        view = _LiveAudio(dmx)
        if audio.codec_string is not None:
            audio.codec_string = view.codec_string
        if entry is not None:
            entry.codec, entry.samplerate = view.codec_string, view.samplerate
            entry.channelConfig = str(view.channels)
        if 'a0' in init and view.config:
            init['a0'].data = InitData.from_bytes('a0', view.config).data
        print(f"  audio config changed: {view.codec_string} "
              f"{view.samplerate}Hz {view.channels}ch")
    cat.generatedAt = int(time.time() * 1000)
    if pub.catalog_track.state == TrackState.SUBSCRIBED:
        await pub.catalog_track.publish_catalog(cat)


async def _feed_ts_live(tracks: dict, fh, dmx: TsDemuxer, first, args,
                        stats: dict, pub, audio_per_group: int = 1):
    """Dispatch demuxed access units to their tracks with PTS-derived
    stamps; audio opens a group every `audio_per_group` frames."""
    loop = asyncio.get_running_loop()
    deadline = time.monotonic() + args.duration
    units = list(first)
    eof = False
    n_audio = 0
    while True:
        if dmx.config_changed:
            dmx.config_changed = False
            await _apply_config_change(dmx, tracks, pub)
        for u in units:
            track = tracks.get(u.kind)
            if track is None:
                continue
            key = u.key
            if u.kind == 'audio':
                key = n_audio % audio_per_group == 0
                n_audio += 1
            await _send(track, stats[u.kind], u.payload, key,
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


def _cmaf_wrap(chunker: CmafChunker, video: bool):
    """_feed_mp4_track wrap: a CMAF chunk whose tfdt and duration are the
    sample's paced span in track ticks, so looping keeps tfdt on the
    pacing clock."""
    def ticks(us: int) -> int:
        return (us * chunker.timescale + 500_000) // 1_000_000

    def wrap(s, ts_us: int, end_us: Optional[int]) -> bytes:
        dt = ticks(ts_us)
        dur = s.duration if end_us is None else ticks(end_us) - dt
        return chunker.chunk(s.payload, dur, s.key_frame if video else True,
                             decode_time=dt)
    return wrap


def _with_prft(chunk: bytes, capture_us: int) -> bytes:
    """A CMAF chunk led by a prft for its first sample."""
    tfdt = chunk_decode_time(chunk)
    return chunk if tfdt is None else prft_box(capture_us, tfdt) + chunk


def _media_clock(timescales, align: bool):
    """(monotonic, wall-clock µs) every paced feeder starts from. With
    `align`, the wall clock is the next µs at which each timescale has a
    whole tick count, so mapped timestamps match LOC's exactly."""
    now = int(time.time() * 1_000_000)
    unit = 1
    if align:
        for t in timescales:
            unit = math.lcm(unit, 1_000_000 // math.gcd(t, 1_000_000))
        unit = min(unit, 1_000_000)
    wall0 = -(-now // unit) * unit
    return time.monotonic() + (wall0 - now) / 1e6, wall0


def _sample_cadence(source):
    """(sample duration, samples per group) of an mp4 track with one
    sample duration and evenly spaced sync samples, else None."""
    stts = [d for c, d in source._stts if c]
    if len(set(stts[:-1] if len(stts) > 1 else stts)) != 1:
        return None
    if source._sync is None:
        return stts[0], 1
    sync = sorted(source._sync)
    gaps = {b - a for a, b in zip(sync, sync[1:])}
    if sync[0] != 1 or len(gaps) > 1:
        return None
    return stts[0], (gaps.pop() if gaps else len(source._sizes))


def _sample_us(source) -> int:
    """Duration of an mp4 track's first sample in µs."""
    return source._stts[0][1] * 1_000_000 // source.timescale


def _audio_per_group(args, frame_us: float) -> int:
    """Audio frames per group: --audio-frames-per-group, else 1, or about
    1 s of frames for locmaf."""
    if args.audio_frames_per_group:
        return args.audio_frames_per_group
    if args.packaging == PACKAGING_LOCMAF:
        return math.ceil(_LOCMAF_AUDIO_GROUP_US / frame_us)
    return 1


def _frindell_for(source, wall0: int, per_group=None):
    """MediaTimestamps for an mp4 track: mapped on a fixed cadence in the
    track's own timescale, else explicit. `per_group` overrides the
    samples per group the sync samples give."""
    cadence = _sample_cadence(source)
    if cadence is None:
        return MediaTimestamps(wall0)
    dur, sync_group = cadence
    per_group = per_group or sync_group
    om = dur if per_group > 1 else 0
    return MediaTimestamps(wall0, source.timescale, dur * per_group, om)


async def _pace(start: float, ts_us: int, pace: bool):
    if pace:
        delay = start + ts_us / 1e6 - time.monotonic()
        if delay > 0.001:
            await asyncio.sleep(delay)


async def _send(track, stats, payload: bytes, key: bool,
                timestamp: Optional[int] = None,
                group_id: Optional[int] = None):
    """Send one frame, or drop it while no subscriber has started
    generation: a paced feeder stays on the clock instead of queueing a
    backlog for a late joiner.

    LOC timestamps without a TIMESCALE property are µs since the Unix
    epoch — players schedule against the wall clock. Paced sources pass
    their scheduled capture time; a live source without its own clock is
    stamped at send time."""
    if track.demand[0] == 0:
        stats.dropped += 1
        return
    entry_us = int(time.time() * 1_000_000)
    if timestamp is None:
        timestamp = entry_us
    stamp = (timestamp * track.timescale // 1_000_000 if track.timescale
             else timestamp)
    kw = {}
    frindell = getattr(track, 'frindell', None)
    if frindell is not None:
        kw['capture_us'] = timestamp
        if key and group_id is None:
            group_id = frindell.group_for(timestamp)
    await track.send_frame(payload, key_frame=key, timestamp=stamp,
                           group_id=group_id, **kw)
    # lag: how late the frame already was when we got it (source, pipe,
    # demux). tx: what handing it to the track cost us.
    stats.count(payload, entry_us - timestamp,
                int(time.time() * 1_000_000) - entry_us)
    if key:
        stats.groups += 1


class _TrackStats:
    """Publish-side counters: objects and bytes for the interval rate and
    bitrate, dropped for frames skipped before a subscriber arrived, and
    how far behind each frame's capture stamp it left this process.

    Send lag measures OUR delay — pacing error, demux and queueing — not
    absolute capture-to-send: a live source's stamps are anchored to
    their own arrival, so a constant upstream delay is inside the anchor
    and reads as zero. Capture-to-parse latency belongs to the receiver
    (sub_media's ts_skew_ms, the player's overlay)."""
    __slots__ = ('objects', 'bytes', 'dropped', 'groups',
                 '_lag_sum', '_lag_n', '_lag_max', '_tx_sum')

    def __init__(self):
        self.objects = 0
        self.bytes = 0
        self.dropped = 0
        self.groups = 0
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


class _MttEvents:
    """--stats-json: one `@mtt <compact json>` line per event."""

    def __init__(self, fh):
        self.fh = fh
        self.t0 = time.monotonic()

    def emit(self, event: str, **fields) -> None:
        self.fh.write('@mtt ' + json.dumps({'event': event, **fields},
                                           separators=(',', ':')) + '\n')
        self.fh.flush()


async def _report_stats(stats: dict, interval: float, pub: FanoutPublisher,
                        events: Optional[_MttEvents] = None):
    """One line per interval: per-track objects produced, object rate and
    bitrate over the interval, frames dropped before a subscriber
    arrived, frames the track discarded unnumbered (waiting for a key
    frame, Forward State 0), frames shed per relay (index = URL position)
    and, with several relays, the demand count. `events` also gets a
    stats event per interval."""
    t0 = time.monotonic()
    prev_obj = {name: 0 for name in stats}
    prev_bytes = {name: 0 for name in stats}
    while True:
        await asyncio.sleep(interval)
        parts = []
        tracks = {}
        for name, st in stats.items():
            track = pub.tracks.get(name)
            discarded = getattr(track, 'frames_dropped', 0)
            objects = st.objects - discarded
            ops = (objects - prev_obj[name]) / interval
            kbps = (st.bytes - prev_bytes[name]) * 8 / interval / 1000
            prev_obj[name] = objects
            prev_bytes[name] = st.bytes
            tracks[name] = dict(objects=objects, groups=st.groups,
                                kbps=round(kbps), dropped=st.dropped,
                                discarded=discarded)
            lag = st.take_lag()
            line = (f"{name}: {objects} obj · {ops:.0f} obj/s · {kbps:.0f} kbps"
                    + (f" · lag {lag[0]:.0f}/{lag[1]:.0f} ms" if lag else "")
                    + (f" · tx {lag[2]:.1f} ms" if lag and lag[2] >= 0.05 else "")
                    + (f" · dropped {st.dropped}" if st.dropped else "")
                    + (f" · discarded {discarded}" if discarded else ""))
            if track is not None and len(pub.sessions) > 1:
                have, total = track.demand
                line += f" · demand {have}/{total}"
            shed = getattr(track, 'shed', None) or {}
            if shed:
                line += " · shed " + ",".join(
                    f"{i}:{n}" for i, n in sorted(shed.items()))
            parts.append(line)
        m, s = divmod(time.monotonic() - t0, 60)
        print(f"  [pub {int(m)}:{s:06.3f}] " + " · ".join(parts))
        if events is not None:
            events.emit('stats', t=round(time.monotonic() - events.t0, 3),
                        tracks=tracks, subscribers=max(
                            (t.demand[0] for t in pub.tracks.values()),
                            default=0))


async def _feed_tone(track, args, stats: _TrackStats, clock=None,
                     per_group: int = 1):
    start, wall0 = clock or (time.monotonic(), int(time.time() * 1_000_000))
    for i, (payload, ts) in enumerate(pcm_tone_frames(
            duration_s=args.duration, freq=args.freq,
            samplerate=_SAMPLERATE, channels=_CHANNELS,
            frame_ms=_FRAME_MS)):
        await _pace(start, ts, not args.no_pace)
        await _send(track, stats, payload, i % per_group == 0,
                    timestamp=wall0 + ts)
    await track.finish()


async def _feed_mp4_track(track, source, args, stats: _TrackStats, *,
                          group_frames=None, gap_us=33_333,
                          wrap=None, clock=None):
    """Feed an mp4 track's samples, paced to their media timestamps and
    stamped with their scheduled wall-clock capture time; --loop
    restarts the file at later timestamps. Groups open on sync samples,
    or with `group_frames` (audio) every that many samples.
    wrap(sample, ts_us, end_us) transforms the payload (CMAF chunking)
    given its paced span, end_us None when unknown; a group's first
    chunk is led by a prft with --timestamp-mapping. `clock` is the
    (monotonic, wall µs) start shared with the other feeders."""
    start, wall0 = clock or (time.monotonic(), int(time.time() * 1_000_000))
    prft = wrap is not None and getattr(args, 'timestamp_mapping', False)
    base_us = 0
    n = 0
    while True:
        last = 0
        samples = source.samples()
        s = next(samples, None)
        while s is not None:
            nxt = next(samples, None)
            ts = base_us + s.timestamp_us
            if ts > args.duration * 1_000_000:
                break
            await _pace(start, ts, not args.no_pace)
            if wrap is None:
                payload = s.payload
            else:
                # A pass's last sample runs to the next pass's start.
                end = (base_us + nxt.timestamp_us if nxt is not None
                       else ts + gap_us if args.loop else None)
                payload = wrap(s, ts, end)
            if group_frames is not None:
                key = n % group_frames == 0
                n += 1
            else:
                key = s.key_frame
            if prft and key:
                payload = _with_prft(payload, wall0 + ts)
            await _send(track, stats, payload, key, timestamp=wall0 + ts)
            last = ts
            s = nxt
        base_us = last + gap_us
        if not args.loop or base_us > args.duration * 1_000_000:
            break
    await track.finish()


_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def _on_stop_signals(loop, handler) -> list:
    """Route SIGINT and SIGTERM to `handler`; the signals installed."""
    installed = []
    for sig in _STOP_SIGNALS:
        try:
            loop.add_signal_handler(sig, handler)
        except (NotImplementedError, RuntimeError, ValueError):
            continue
        installed.append(sig)
    return installed


async def run(args):
    """Publish until the duration, a signal or an error; with
    --stats-json, human output goes to stderr and events to stdout."""
    if not getattr(args, 'stats_json', False):
        await _run(args, None)
        return
    events = _MttEvents(sys.stdout)
    reason = 'error'
    try:
        with contextlib.redirect_stdout(sys.stderr):
            reason = await _run(args, events)
    except SystemExit as e:
        reason = 'error' if e.code else 'duration'
        raise
    except (KeyboardInterrupt, asyncio.CancelledError):
        reason = 'signal'
        raise
    finally:
        events.emit('done', reason=reason)


async def _run(args, events: Optional[_MttEvents]) -> str:
    """The run; 'duration' or 'signal' for how it ended."""
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
    if args.packaging != PACKAGING_LOC and not args.input:
        chunkers['video'] = CmafChunker(video)
        if mp4_audio is not None:
            chunkers['audio'] = CmafChunker(mp4_audio)
        elif not args.no_audio:
            print(f"  note: {args.packaging} packaging — tone audio skipped "
                  "(no AAC or Opus track in the mp4)")
            args.no_audio = True
    ingest = await _open_ingest(args) if args.input else None
    if ingest is not None:
        catalog = _build_ingest_catalog(args, ingest)
    else:
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
        if ingest is not None:
            stack.callback(ingest.close)

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
        if events is not None:
            endpoints = [dict(url=u, state='up' if s is not None else 'down',
                              draft=getattr(s, 'negotiated_draft', None))
                         for u, s in zip(urls, opened)]
        if not up:
            if events is not None:
                events.emit('ready', endpoints=endpoints)
            raise SystemExit(1)
        urls = [urls[i] for i in up]
        relays = [relays[i] for i in up]
        sessions = [opened[i] for i in up]
        for url, relay, s in zip(urls, relays, sessions):
            if args.player_base:
                draft = getattr(s, 'negotiated_draft', None)
                print(f"  player: {_player_url(args, relay, url, draft)}")

        def _cs(codec):
            return codec if args.loc_codecstring else None
        v_map = (StreamMapping.PER_OBJECT if args.video_subgroups == 'object'
                 else StreamMapping.PER_GROUP)
        a_map = (StreamMapping.DATAGRAM if args.datagram
                 else StreamMapping.PER_OBJECT
                 if args.audio_subgroups == 'object'
                 else StreamMapping.PER_GROUP)
        session = sessions[0]
        # CMAF objects are not LOC: no Private Properties block.
        loc_bare = args.loc_bare or args.packaging == PACKAGING_CMAF

        def _track(name, **kwargs):
            if args.packaging == PACKAGING_LOCMAF:
                init = catalog.resolve_init(catalog.find(name))
                return LocmafTrackPublisher(session, args.namespace, name,
                                            init=init, **kwargs)
            return LocTrackPublisher(session, args.namespace, name,
                                     loc_bare=loc_bare,
                                     timescale=args.loc_timescale, **kwargs)
        # One start for every paced feeder; with --timestamp-mapping its
        # wall clock anchors each track's TIMESTAMP_MAPPING.
        timescales = [_SAMPLERATE] + [
            s.timescale for s in (video if reader else None, mp4_audio)
            if s is not None]
        clock = _media_clock(timescales, args.timestamp_mapping)
        wall0 = clock[1]

        def _timed(track, frindell):
            if args.timestamp_mapping:
                track.frindell = frindell
            return track
        pub = FanoutPublisher(sessions, args.namespace, catalog)
        stats = {}
        feeders = []
        if ingest is not None:
            tracks = {}
            for t in ingest.tracks:
                tracks[t.name] = pub.add_track(_timed(
                    _track(t.name, media_kind=t.kind),
                    MediaTimestamps(wall0)))
                stats[t.name] = _TrackStats()
            feeders.append(_feed_ingest(ingest, tracks, args, stats))
        elif ts is not None:
            fh, dmx, first, _, _ = ts
            tracks = {}
            a_per = _audio_per_group(args, _FRAME_MS * 1000)
            if video is not None:
                tracks['video'] = pub.add_track(_timed(LocTrackPublisher(
                    session, args.namespace, 'video', config=video.config,
                    mapping=v_map, loc01_compat=args.loc01_compat, loc_bare=loc_bare,
                    timescale=args.loc_timescale,
                    codec_string=_cs(video.codec_string)),
                    MediaTimestamps(wall0)))
                stats['video'] = _TrackStats()
            if ts_audio is not None:
                tracks['audio'] = pub.add_track(_timed(LocTrackPublisher(
                    session, args.namespace, 'audio', media_kind='audio',
                    config=ts_audio.config, mapping=a_map,
                    loc01_compat=args.loc01_compat, loc_bare=loc_bare,
                    timescale=args.loc_timescale,
                    codec_string=_cs(ts_audio.codec_string)),
                    MediaTimestamps(wall0)))
                stats['audio'] = _TrackStats()
            feeders.append(_feed_ts_live(tracks, fh, dmx, first, args,
                                         stats, pub, a_per))
        elif not args.no_audio:
            spf = _SAMPLERATE * _FRAME_MS // 1000
            a_per = _audio_per_group(
                args, _sample_us(mp4_audio) if mp4_audio is not None
                else _FRAME_MS * 1000)
            audio_track = pub.add_track(_timed(_track(
                'audio', media_kind='audio',
                config=mp4_audio.config if mp4_audio is not None else None,
                mapping=a_map,
                loc01_compat=args.loc01_compat,
                codec_string=_cs(mp4_audio.codec_string
                                 if mp4_audio is not None else 'pcm-s16')),
                (_frindell_for(mp4_audio, wall0, a_per)
                 if mp4_audio is not None
                 else MediaTimestamps(wall0, _SAMPLERATE, spf * a_per,
                                      spf if a_per > 1 else 0))))
            stats['audio'] = _TrackStats()
            if mp4_audio is not None:
                a_ck = chunkers.get('audio')
                feeders.append(_feed_mp4_track(
                    audio_track, mp4_audio, args, stats['audio'],
                    group_frames=a_per,
                    gap_us=_sample_us(mp4_audio),
                    wrap=_cmaf_wrap(a_ck, video=False) if a_ck else None,
                    clock=clock))
            else:
                feeders.append(_feed_tone(audio_track, args,
                                          stats['audio'], clock=clock,
                                          per_group=a_per))
        if live is not None:
            fh, asm, first, _ = live
            stats['video'] = _TrackStats()
            feeders.append(_feed_h264_live(
                pub.add_track(_timed(LocTrackPublisher(
                    session, args.namespace, 'video', config=video.config,
                    mapping=v_map, loc01_compat=args.loc01_compat, loc_bare=loc_bare,
                    timescale=args.loc_timescale,
                    codec_string=_cs(video.codec_string)),
                    MediaTimestamps(wall0))),
                fh, asm, first, args, stats['video']))
        elif video is not None and ts is None:   # --ts wired both tracks above
            v_ck = chunkers.get('video')
            stats['video'] = _TrackStats()
            feeders.append(_feed_mp4_track(
                pub.add_track(_timed(_track(
                    'video', mapping=v_map,
                    config=None if v_ck else video.config,
                    loc01_compat=args.loc01_compat,
                    codec_string=_cs(video.codec_string)),
                    _frindell_for(video, wall0))),
                video, args, stats['video'],
                gap_us=int(1e6 / (video.fps or 30)),
                wrap=_cmaf_wrap(v_ck, video=True) if v_ck else None,
                clock=clock))
        await pub.start(announce_namespace=(args.pub_ns or args.pub_both),
                        publish_track=(not args.pub_ns or args.pub_both),
                        forward=args.forward)
        print("  publishing...")
        if events is not None:
            events.emit('ready', endpoints=endpoints)
        reporter = (asyncio.ensure_future(
            _report_stats(stats, args.stats, pub, events))
            if args.stats > 0 and stats else None)
        refresher = (asyncio.ensure_future(
            _refresh_catalog(pub, args.catalog_interval))
            if args.catalog_interval > 0 else None)
        # SIGINT and SIGTERM: PUBLISH_DONE on every track, drain, exit 0.
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        signals = _on_stop_signals(loop, stop.set)
        stack.callback(lambda: [loop.remove_signal_handler(sig)
                                for sig in signals])
        stopping = asyncio.ensure_future(stop.wait())
        feeders = [asyncio.ensure_future(f) for f in feeders]
        feed = asyncio.gather(*feeders)
        # An interrupted run cancels the feeders; collect that outcome so
        # Ctrl-C exits quietly instead of logging an unretrieved exception.
        feed.add_done_callback(
            lambda f: f.cancelled() or f.exception())
        closed = {asyncio.ensure_future(s.async_closed()): s
                  for s in sessions}
        # A relay lost mid-run is dropped and the rest continue; the
        # run fails only when no relay is left.
        while True:
            done, _ = await asyncio.wait({feed, stopping, *closed},
                                         return_when=asyncio.FIRST_COMPLETED)
            if feed in done or stopping in done:
                break
            for fut in done:
                s = closed.pop(fut)
                code, reason = getattr(s, '_close_err', None) or ('?', '')
                url = urls[sessions.index(s)]
                if events is not None:
                    events.emit('endpoint_lost', url=url,
                                reason=f"code={code} {reason}".strip())
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
                stopping.cancel()
                raise SystemExit(1)
        if reporter is not None:
            reporter.cancel()
        if refresher is not None:
            refresher.cancel()
        for fut in closed:
            fut.cancel()
        stopping.cancel()
        if stop.is_set():
            feed.cancel()
            for track in pub.tracks.values():
                track.end()
            await pub.catalog_track.finish()
            await asyncio.sleep(1.0)
            print("  done (signal)")
            return 'signal'
        error = None if feed.cancelled() else feed.exception()
        if isinstance(error, DatagramTooLarge):
            # The track sent PUBLISH_DONE; the other feeders stop here.
            for f in feeders:
                f.cancel()
            print(f"  error: {error}")
            await asyncio.sleep(1.0)
            raise SystemExit(EXIT_DATAGRAM_TOO_LARGE)
        await feed
        await pub.catalog_track.finish()
        await asyncio.sleep(1.0)  # drain tail before teardown
    print("  done")
    return 'duration'


def main():
    # Until run() takes over the signal, SIGTERM stops a run as SIGINT does.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
