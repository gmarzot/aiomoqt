#!/usr/bin/env python3
"""moq-peer-probe: join a moq-peering fabric (moqx, LAPS) as a leaf node.

Dials each --peer over raw QUIC (ALPN moq-peering) and, with -p, accepts
peers' dials. Logs and records every node, announce, subscribe and SNS set
the fabric sends, checks each announce and subscribe hash, and with
--announce / --subscribe originates its own (control plane only: no
objects are sent or decoded). It advertises only itself, at metric 2^32,
and forwards nothing, so no relay routes through it.

-o writes the report JSON on exit and every -i seconds. Exit status: 0 when
--exit COND is met (or -t elapses with no --exit), 1 when -t elapses
first, 130 on a signal.

Examples:
  # observe one relay for 30 s, no listener
  moq-peer-probe --peer 10.0.0.2:33434 -p 0 -t 30 -o fabric.json

  # stop once anything under moq-test is announced
  moq-peer-probe --peer 10.0.0.2 -p 0 --exit announce:moq-test

  # mock agent on two relays: announce a track, subscribe to another
  moq-peer-probe --peer 10.0.0.2 --peer 10.0.0.3 -p 0 \\
      --announce aiomoqt/probe --subscribe moq-test/live/video -t 60
"""
import argparse
import asyncio
import logging
import signal
import sys
import time
from typing import Optional, Tuple

from aiomoqt.peering import (FabricNode, build_report, node_id_format,
                             parse_identity, parse_track, write_report)
from aiomoqt.peering.report import summary_line
from aiomoqt.utils import cli as grid
from aiomoqt.utils.certs import ensure_self_signed, find_cert, find_key


class ExitCondition:
    """--exit: peers-up[:N] | announce:NS[/TRACK] | subscribe:NS[/TRACK] |
    idle:SEC. announce and subscribe match learned records whose
    namespace-and-name path starts with the given one."""

    def __init__(self, kind: str, count: Optional[int] = None,
                 path: Tuple[bytes, ...] = (), seconds: float = 0.0,
                 text: str = ""):
        self.kind = kind
        self.count = count
        self.path = path
        self.seconds = seconds
        self.text = text

    def _matches(self, records) -> bool:
        n = len(self.path)
        for _, rec in records:
            body = rec.body
            full = tuple(body.namespace) + ((body.name,) if body.name else ())
            if full[:n] == self.path:
                return True
        return False

    def met(self, node: FabricNode) -> bool:
        if self.kind == "peers-up":
            return len(node.connected()) >= (self.count or len(node.peers) or 1)
        if self.kind == "announce":
            return self._matches(node.learned_announces())
        if self.kind == "subscribe":
            return self._matches(node.learned_subscribes())
        return self.idle_for(node) >= self.seconds

    @staticmethod
    def idle_for(node: FabricNode) -> float:
        return (time.monotonic_ns() - node.last_rx_mono_ns) / 1e9

    def wake_in(self, node: FabricNode) -> Optional[float]:
        """Seconds until an idle condition could next be met."""
        if self.kind != "idle":
            return None
        return max(0.0, self.seconds - self.idle_for(node))


def exit_condition(value: str) -> ExitCondition:
    kind, sep, arg = value.partition(":")
    try:
        if kind == "peers-up":
            count = int(arg) if sep else None
            if count is not None and count < 1:
                raise ValueError("N must be at least 1")
            return ExitCondition(kind, count=count, text=value)
        if kind in ("announce", "subscribe"):
            if not arg:
                raise ValueError("needs NS[/TRACK]")
            return ExitCondition(kind, path=tuple(
                t.encode() for t in arg.strip("/").split("/")), text=value)
        if kind == "idle":
            seconds = float(arg)
            if seconds <= 0:
                raise ValueError("SEC must be above 0")
            return ExitCondition(kind, seconds=seconds, text=value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"--exit {value}: {e}")
    raise argparse.ArgumentTypeError(
        f"--exit {value}: use peers-up[:N], announce:NS[/TRACK], "
        f"subscribe:NS[/TRACK] or idle:SEC")


def track_arg(value: str):
    try:
        return parse_track(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def subscribe_arg(value: str):
    ns, name = track_arg(value)
    if not name:
        raise argparse.ArgumentTypeError(f"--subscribe {value}: NS/TRACK")
    return ns, name


def parse_args(argv=None):
    p = grid.make_parser(
        description="Join a moq-peering fabric as a leaf node: observe it "
                    "and originate announces and subscribes",
        epilog=__doc__)
    grid.add_peering(p)
    grid.add_run(p)
    grid.add_logging(p)
    grid.add_interval(p)
    grid.add_keepalive(p, default=1.5)
    p.add_argument('-o', '--output-file', default=None, metavar='PATH',
                   help='Report JSON, written on exit and refreshed every '
                        '-i seconds')
    p.add_argument('--exit', dest='exit_on', type=exit_condition,
                   default=None, metavar='COND',
                   help='Exit 0 once COND holds: peers-up[:N] (N sessions '
                        'connected; default every --peer), '
                        'announce:NS[/TRACK] or subscribe:NS[/TRACK] (one '
                        'learned under that path), idle:SEC (no control '
                        'message for SEC seconds). With -t, exit 1 if -t '
                        'elapses first')
    p.add_argument('--announce', action='append', type=track_arg,
                   default=None, metavar='NS[/TRACK]',
                   help='Announce a track (last element is the name), or '
                        'with no "/" or a trailing "/" a namespace. '
                        'Repeatable; no objects are sent')
    p.add_argument('--subscribe', action='append', type=subscribe_arg,
                   default=None, metavar='NS/TRACK',
                   help='Subscribe to a track and record the SNS set that '
                        'comes back. Repeatable; objects are counted, not '
                        'decoded')
    grid.add_help(p)
    args = p.parse_args(argv)
    if not args.peer and not args.port:
        p.error('nothing to do: give --peer, or a listen port with -p')
    return args


def _listener_cert(args) -> Tuple[Optional[str], Optional[str]]:
    if not args.port:
        return None, None
    cert = args.cert or find_cert()
    key = args.key or find_key(cert)
    if cert and key:
        return cert, key
    if args.cert:
        raise SystemExit(f"--cert {args.cert}: no key beside it; use --key")
    return ensure_self_signed()


async def run(args) -> int:
    try:
        node_id, label = parse_identity(args.node_id)
    except ValueError as e:
        raise SystemExit(f"--node-id: {e}")
    cert, key = _listener_cert(args)
    node = FabricNode(node_id=node_id, label=label, metric=args.node_metric,
                      peers=args.peer or (), bind=args.bind, port=args.port,
                      cert=cert, key=key, keepalive=args.keepalive)
    cond = args.exit_on
    print(f"moq-peer-probe: node {node_id_format(node.node_id)} "
          f"(0x{node.node_id:016x}) contact={label!r} "
          f"metric={args.node_metric} listen={node.listen_text() or 'off'} "
          f"peers={len(node.peers)} -t {grid.duration_label(args.duration)}"
          + (f" --exit {cond.text}" if cond else ""), flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_signal():
        stop.set()
        node.notify()
    handled = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_signal)
            handled.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    def save():
        if args.output_file:
            write_report(args.output_file, build_report(node))

    start = time.monotonic()
    deadline = start + args.duration if args.duration else None
    tick = start + args.interval if args.interval > 0 else None
    rc = 0
    try:
        await node.start()
        for ns, name in args.announce or ():
            node.announce(ns, name)
        for ns, name in args.subscribe or ():
            node.subscribe(ns, name)
        while True:
            now = time.monotonic()
            if cond is not None and cond.met(node):
                print(f"exit: {cond.text} met after {now - start:.1f}s",
                      flush=True)
                break
            if stop.is_set():
                rc = 130
                break
            if deadline is not None and now >= deadline:
                if cond is not None:
                    print(f"exit: {cond.text} not met in {args.duration}s",
                          flush=True)
                    rc = 1
                break
            if tick is not None and now >= tick:
                if not args.no_stats:
                    print(summary_line(node, now - start), flush=True)
                save()
                while tick <= now:
                    tick += args.interval
            wakes = [t - now for t in (deadline, tick) if t is not None]
            idle = cond.wake_in(node) if cond is not None else None
            if idle is not None:
                wakes.append(idle)
            await node.wait_changed(min(wakes) if wakes else None)
        # Before stop(): closing a session forgets what it learned.
        if not args.no_stats:
            print(summary_line(node, time.monotonic() - start), flush=True)
        save()
    finally:
        await node.stop()
        for sig in handled:
            loop.remove_signal_handler(sig)
    return rc


def main(argv=None) -> int:
    args = parse_args(argv)
    grid.apply_logging(args, default=logging.INFO)
    return asyncio.run(run(args))


def cli():
    """Console entry point (moq-peer-probe)."""
    sys.exit(main())


if __name__ == "__main__":
    cli()
