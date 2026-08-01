#!/usr/bin/env python3
"""LOC/MSF media subscriber — consumes an MSF broadcast and writes
playable files.

  %(prog)s moqt://localhost:4433/ -N demo/live -t 30 --out ./media-out

Reads the catalog track, subscribes every LOC track it describes, and
writes:
  <out>/video.h264   Annex-B elementary stream (SPS/PPS injected from
                     the catalog/wire decoder config at keyframes)
  <out>/audio.wav    pcm-s16 with header from the catalog track entry

Play them:  ffplay video.h264   /   ffplay audio.wav
"""
import asyncio
import logging
import os
import wave

from aiomoqt.client import MOQTClient
from aiomoqt.media import MediaSubscriber
from aiomoqt.media.sources import avcc_param_sets, lp_to_annexb
from aiomoqt.utils import cli as _cli
from aiomoqt.utils.logger import set_log_level
from aiomoqt.utils.url import parse_relay_url


def parse_args():
    parser = _cli.make_parser(
        'LOC/MSF media subscriber (catalog-driven, writes playable '
        'files)', epilog=__doc__)
    _cli.add_endpoint(parser)
    _cli.add_identity(parser, namespace='demo/live')
    parser.add_argument('--out', type=str, default='./media-out',
                        help='Output directory (default: ./media-out)')
    _cli.add_run(parser, duration=30, interval=False)
    _cli.add_session(parser, keepalive=True)
    _cli.add_help(parser)
    return parser.parse_args()


class _Writers:
    """Per-track sinks: LOC video → Annex-B .h264, pcm audio → .wav."""

    def __init__(self, out_dir: str, subscriber: MediaSubscriber):
        self.out = out_dir
        self.sub = subscriber
        self.video = None
        self.wav = None
        self.counts = {}

    def on_frame(self, name, frame, group_id, object_id):
        self.counts[name] = self.counts.get(name, 0) + 1
        entry = self.sub.catalog.find(name) if self.sub.catalog else None
        role = entry.role if entry else None
        if role == 'video':
            if self.video is None:
                self.video = open(os.path.join(self.out, 'video.h264'),
                                  'wb')
            config = self.sub.tracks[name].config
            if frame.key_frame and config:
                self.video.write(avcc_param_sets(config))
            self.video.write(lp_to_annexb(frame.payload))
        elif role == 'audio' and (entry.codec or '').startswith('pcm-s16'):
            if self.wav is None:
                self.wav = wave.open(
                    os.path.join(self.out, 'audio.wav'), 'wb')
                self.wav.setnchannels(int(entry.channelConfig or 2))
                self.wav.setsampwidth(2)
                self.wav.setframerate(entry.samplerate or 48000)
            self.wav.writeframes(frame.payload)

    def close(self):
        if self.video:
            self.video.close()
        if self.wav:
            self.wav.close()


async def run(args):
    set_log_level(logging.DEBUG if args.debug else logging.WARNING)
    relay = parse_relay_url(args.url)
    os.makedirs(args.out, exist_ok=True)

    client = MOQTClient(
        relay.host, relay.port, path=relay.path,
        use_quic=relay.use_quic, verify_tls=not args.insecure,
        supported_drafts=args.draft, debug=args.debug,
        keylog_filename=args.keylogfile,
        congestion_control_algorithm=args.cc_algo,
        keep_alive_interval=args.keepalive,
    )
    print(f"  relay: {relay}  namespace: {args.namespace}")
    async with client.connect() as session:
        await session.client_session_init()
        sub = MediaSubscriber(session, args.namespace)
        writers = _Writers(args.out, sub)
        sub.on_frame = writers.on_frame
        catalog = await sub.start(timeout=args.duration)
        print(f"  catalog: {[t.name for t in catalog.tracks]}")
        try:
            await asyncio.wait_for(session.async_closed(),
                                   timeout=args.duration + 5)
        except asyncio.TimeoutError:
            pass
        finally:
            writers.close()
    for name, n in sorted(writers.counts.items()):
        print(f"  {name}: {n} frames")
    print(f"  wrote {args.out}/  — play with: "
          f"ffplay {args.out}/video.h264 | ffplay {args.out}/audio.wav")


def main():
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
