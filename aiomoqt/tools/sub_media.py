#!/usr/bin/env python3
"""LOC/MSF media subscriber — consumes an MSF broadcast and writes
playable files, or pipes one track straight into a player.

  %(prog)s moqt://localhost:4433/ -N demo/live -t 30 --out ./media-out

Reads the catalog track, subscribes every LOC track it describes, and
writes:
  <out>/video.h264   Annex-B elementary stream (SPS/PPS injected from
                     the catalog/wire decoder config at keyframes)
  <out>/audio.wav    pcm-s16 with header from the catalog track entry

Play them:  ffplay video.h264   /   ffplay audio.wav

Live piping (--pipe sends that track's raw stream to stdout, status
goes to stderr; the other track still writes to --out):

  %(prog)s URL -N demo/live --pipe video | \\
      ffplay -fflags nobuffer -flags low_delay -probesize 32 -f h264 -i -
  %(prog)s URL -N demo/live --pipe audio | \\
      ffplay -f s16le -ar 48000 -ch_layout stereo -i -
"""
import asyncio
import logging
import os
import sys
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
    parser.add_argument('--pipe', choices=('video', 'audio'), default=None,
                        help='Stream this track raw to stdout for piping '
                             'into a player (video: Annex-B h264; audio: '
                             's16le pcm). Status moves to stderr; the '
                             'other track still writes to --out.')
    _cli.add_run(parser, duration=30, interval=False)
    _cli.add_session(parser, keepalive=True)
    _cli.add_help(parser)
    return parser.parse_args()


class _Writers:
    """Per-track sinks: LOC video → Annex-B .h264, pcm audio → .wav.
    One track may stream raw to stdout instead (pipe_role)."""

    def __init__(self, out_dir: str, subscriber: MediaSubscriber,
                 pipe_role: str = None):
        self.out = out_dir
        self.sub = subscriber
        self.pipe_role = pipe_role
        self.video = None
        self.wav = None
        self.counts = {}
        self.pipe_closed = False

    def _pipe(self, data: bytes) -> None:
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (BrokenPipeError, ValueError):
            self.pipe_closed = True

    def on_frame(self, name, frame, group_id, object_id):
        self.counts[name] = self.counts.get(name, 0) + 1
        entry = self.sub.catalog.find(name) if self.sub.catalog else None
        role = entry.role if entry else None
        if role == 'video':
            config = self.sub.tracks[name].config
            param_sets = (avcc_param_sets(config)
                          if frame.key_frame and config else b'')
            if self.pipe_role == 'video':
                self._pipe(param_sets + lp_to_annexb(frame.payload))
                return
            if self.video is None:
                self.video = open(os.path.join(self.out, 'video.h264'),
                                  'wb')
            self.video.write(param_sets)
            self.video.write(lp_to_annexb(frame.payload))
        elif role == 'audio' and (entry.codec or '').startswith('pcm-s16'):
            if self.pipe_role == 'audio':
                self._pipe(frame.payload)
                return
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


def _status(*parts):
    print(*parts, file=sys.stderr)


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
    _status(f"  relay: {relay}  namespace: {args.namespace}")
    async with client.connect() as session:
        await session.client_session_init()
        sub = MediaSubscriber(session, args.namespace)
        writers = _Writers(args.out, sub, pipe_role=args.pipe)
        sub.on_frame = writers.on_frame
        catalog = await sub.start(timeout=args.duration)
        _status(f"  catalog: {[t.name for t in catalog.tracks]}")
        if args.pipe == 'audio':
            a = catalog.find('audio')
            layout = ('mono' if (a and a.channelConfig == '1')
                      else 'stereo')
            _status(f"  piping s16le — play with: ffplay -f s16le "
                    f"-ar {a.samplerate if a else 48000} "
                    f"-ch_layout {layout} -i -")
        elif args.pipe == 'video':
            _status("  piping h264 — play with: ffplay -fflags nobuffer "
                    "-flags low_delay -probesize 32 -f h264 -i -")
        try:
            async with asyncio.timeout(args.duration + 5):
                while not writers.pipe_closed:
                    if session._moqt_session_closed.done():
                        break
                    await asyncio.sleep(0.1)
        except asyncio.TimeoutError:
            pass
        finally:
            writers.close()
    for name, n in sorted(writers.counts.items()):
        _status(f"  {name}: {n} frames")
    if args.pipe is None:
        _status(f"  wrote {args.out}/  — play with: "
                f"ffplay {args.out}/video.h264 | "
                f"ffplay {args.out}/audio.wav")


def main():
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
