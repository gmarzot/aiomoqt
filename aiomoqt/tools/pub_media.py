#!/usr/bin/env python3
"""LOC/MSF media publisher — publishes an MSF broadcast (catalog +
audio, plus video from an mp4) to a relay.

  # audio-only (synthesized pcm-s16 tone), 30 s
  %(prog)s moqt://localhost:4433/ -N demo/live -t 30

  # audio + H.264 video lifted from an mp4 (no re-encode)
  %(prog)s https://relay.example/moq-relay -N demo/live --mp4 clip.mp4

The video track sends the mp4's samples byte-for-byte as LOC canonical
payloads (loc-02 §2.1.3); the avcC extradata rides the catalog
initDataList and VIDEO_CONFIG group-start properties. Frames pace to
their timestamps (--no-pace to blast).
"""
import asyncio
import logging
import time

from aiomoqt.client import MOQTClient
from aiomoqt.media import (
    Catalog, CatalogTrack, InitData, LocTrackPublisher, MediaPublisher,
    StreamMapping,
)
from aiomoqt.media.sources import (
    Mp4Reader, avcc_codec_string, pcm_tone_frames,
)
from aiomoqt.utils import cli as _cli
from aiomoqt.utils.logger import set_log_level
from aiomoqt.utils.url import parse_relay_url

_SAMPLERATE = 48000
_CHANNELS = 2
_FRAME_MS = 20


def parse_args():
    parser = _cli.make_parser(
        'LOC/MSF media publisher (catalog + pcm tone + optional mp4 '
        'video)', epilog=__doc__)
    _cli.add_endpoint(parser)
    _cli.add_identity(parser, namespace='demo/live')
    parser.add_argument('--mp4', type=str, default=None, metavar='FILE',
                        help='Publish this mp4\'s H.264 track as LOC '
                             'video (samples pass through, no decode)')
    parser.add_argument('--loop', action='store_true',
                        help='Loop the mp4 for the full duration')
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
    parser.add_argument('--loc01-compat', action='store_true',
                        help='Also emit timestamps under loc-01\'s '
                             'property id 0x02 for players not yet on '
                             'loc-02 numbering (moq-playa)')
    _cli.add_run(parser, duration=30, interval=False)
    _cli.add_session(parser, keepalive=True)
    _cli.add_help(parser)
    return parser.parse_args()


def _build_catalog(args, video, audio) -> Catalog:
    tracks = []
    init = []
    if audio is not None:
        tracks.append(CatalogTrack(
            name='audio', packaging='loc', isLive=True, role='audio',
            renderGroup=1, codec=audio.codec_string,
            samplerate=audio.samplerate,
            channelConfig=str(audio.channels),
            bitrate=audio.avg_bitrate or 128_000, initRef='a0'))
        init.append(InitData.from_bytes('a0', audio.asc))
    elif not args.no_audio:
        tracks.append(CatalogTrack(
            name='audio', packaging='loc', isLive=True, role='audio',
            renderGroup=1, codec='pcm-s16', samplerate=_SAMPLERATE,
            channelConfig=str(_CHANNELS),
            bitrate=_SAMPLERATE * _CHANNELS * 16))
    if video is not None:
        tracks.insert(0, CatalogTrack(
            name='video', packaging='loc', isLive=True, role='video',
            renderGroup=1, codec=avcc_codec_string(video.avcc),
            width=video.width, height=video.height,
            framerate=video.fps,
            bitrate=video.avg_bitrate or 2_000_000, initRef='v0'))
        init.append(InitData.from_bytes('v0', video.avcc))
    return Catalog(generatedAt=int(time.time() * 1000), tracks=tracks,
                   initDataList=init or None)


async def _pace(start: float, ts_us: int, pace: bool):
    if pace:
        delay = start + ts_us / 1e6 - time.monotonic()
        if delay > 0.001:
            await asyncio.sleep(delay)


async def _feed_tone(track, args, epoch_us: int):
    start = time.monotonic()
    for payload, ts in pcm_tone_frames(
            duration_s=args.duration, freq=args.freq,
            samplerate=_SAMPLERATE, channels=_CHANNELS,
            frame_ms=_FRAME_MS):
        await _pace(start, ts, not args.no_pace)
        # LOC timestamps without a TIMESCALE property are µs since the
        # Unix epoch — players schedule against the wall clock.
        await track.send_frame(payload, key_frame=True,
                               timestamp=epoch_us + ts)
    await track.finish()


async def _feed_mp4_track(track, source, args, epoch_us: int, *,
                          all_key=False, gap_us=33_333):
    """Feed an mp4 track's samples, paced to their media timestamps and
    stamped as wall-clock µs (LOC default clock); --loop restarts the
    file at later timestamps (audio: every AU is a sync frame, giving
    LOC's one-object-per-group audio mapping)."""
    start = time.monotonic()
    base_us = 0
    while True:
        last = 0
        for s in source.samples():
            ts = base_us + s.timestamp_us
            if ts > args.duration * 1_000_000:
                break
            await _pace(start, ts, not args.no_pace)
            await track.send_frame(s.payload,
                                   key_frame=all_key or s.key_frame,
                                   timestamp=epoch_us + ts)
            last = ts
        base_us = last + gap_us
        if not args.loop or base_us > args.duration * 1_000_000:
            break
    await track.finish()


async def run(args):
    set_log_level(logging.DEBUG if args.debug else logging.WARNING)
    relay = parse_relay_url(args.url)
    reader = Mp4Reader(args.mp4) if args.mp4 else None
    video = reader.video if reader else None
    mp4_audio = (reader.audio
                 if reader and not (args.tone or args.no_audio) else None)
    catalog = _build_catalog(args, video, mp4_audio)

    client = MOQTClient(
        relay.host, relay.port, path=relay.path,
        use_quic=relay.use_quic, verify_tls=not args.insecure,
        supported_drafts=args.draft, debug=args.debug,
        keylog_filename=args.keylogfile,
        congestion_control_algorithm=args.cc_algo,
        keep_alive_interval=args.keepalive,
    )
    print(f"  relay: {relay}  namespace: {args.namespace}")
    print(f"  tracks: {', '.join(t.name for t in catalog.tracks)}")
    async with client.connect() as session:
        await session.client_session_init()
        pub = MediaPublisher(session, args.namespace, catalog)
        epoch_us = int(time.time() * 1_000_000)
        feeders = []
        if not args.no_audio:
            audio_track = pub.add_track(LocTrackPublisher(
                session, args.namespace, 'audio',
                mapping=(StreamMapping.DATAGRAM if args.datagram
                         else StreamMapping.PER_GROUP),
                loc01_compat=args.loc01_compat))
            if mp4_audio is not None:
                feeders.append(_feed_mp4_track(
                    audio_track, mp4_audio, args, epoch_us, all_key=True,
                    gap_us=1_000_000 * 1024 // mp4_audio.samplerate))
            else:
                feeders.append(_feed_tone(audio_track, args, epoch_us))
        if video is not None:
            feeders.append(_feed_mp4_track(
                pub.add_track(LocTrackPublisher(
                    session, args.namespace, 'video', config=video.avcc,
                    loc01_compat=args.loc01_compat)),
                video, args, epoch_us,
                gap_us=int(1e6 / (video.fps or 30))))
        await pub.start()
        print("  publishing...")
        await asyncio.gather(*feeders)
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
