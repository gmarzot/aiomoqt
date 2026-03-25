#!/usr/bin/env python3
"""moqperf publisher - sends timestamped MoQT objects through a relay.

Usage:
  # H3/WebTransport (default)
  python -m aiomoqt.examples.bench_pub https://relay.example.com:4433/moq

  # Raw QUIC
  python -m aiomoqt.examples.bench_pub moqt://relay.example.com:4443

  # Bare hostname (defaults to https, port 4433, endpoint /moq)
  python -m aiomoqt.examples.bench_pub relay.example.com

  # With options
  python -m aiomoqt.examples.bench_pub relay.example.com -s 4096 -P 4 -r 120 -t 60
"""
import argparse
import asyncio
import logging
import time
from functools import partial

from aiomoqt.types import (
    MOQTMessageType, ParamType, ObjectStatus, MOQT_TIMESTAMP_EXT,
)
from aiomoqt.messages import (
    Subscribe, SubgroupHeader, ObjectDatagram, ObjectDatagramStatus,
)
from aiomoqt.client import MOQTClient
from aiomoqt.protocol import MOQTSession
from aiomoqt.utils.logger import set_log_level, get_logger
from aiomoqt.utils.url import parse_relay_url


async def subscribe_data_generator(session: MOQTSession, msg: Subscribe,
                                   num_tasks: int = 1, object_size: int = 1024,
                                   group_size: int = 60, rate: float = 0) -> None:
    """Subscribe handler that spawns subgroup stream data generation."""
    ok = session.subscribe_ok(request_msg=msg)

    for subgroup_id in range(num_tasks):
        priority = 255 if subgroup_id == 0 else 0
        task = asyncio.create_task(
            generate_subgroup_stream(
                session=session,
                subgroup_id=subgroup_id,
                track_alias=ok.track_alias,
                priority=priority,
                num_subgroups=num_tasks,
                object_size=object_size,
                group_size=group_size,
                rate=rate,
            )
        )
        task.add_done_callback(lambda t: session._tasks.discard(t))
        session._tasks.add(task)

    await session.async_closed()
    session._close_session()


async def dgram_subscribe_data_generator(session: MOQTSession, msg: Subscribe,
                                         object_size: int = 1100,
                                         group_size: int = 60,
                                         rate: float = 0) -> None:
    """Subscribe handler for datagram mode."""
    ok = session.subscribe_ok(request_msg=msg)
    task = asyncio.create_task(
        generate_group_dgram(
            session=session,
            track_alias=ok.track_alias,
            priority=255,
            object_size=object_size,
            group_size=group_size,
            rate=rate,
        )
    )
    task.add_done_callback(lambda t: session._tasks.discard(t))
    session._tasks.add(task)
    await session.async_closed()
    session._close_session()


async def generate_group_dgram(session: MOQTSession, track_alias: int, priority: int,
                               object_size: int = 1100, group_size: int = 60,
                               rate: float = 0):
    """Generate datagram objects. rate=0 means max speed."""
    logger = get_logger(__name__)
    pad = b'\x00' * object_size
    paced = rate > 0
    frame_interval = 1.0 / rate if paced else 0
    total_sent = 0

    next_frame_time = time.monotonic()
    object_id = 0
    group_id = -1

    try:
        while True:
            if (object_id % group_size) == 0:
                group_id += 1
                if group_id > 0:
                    obj = ObjectDatagramStatus(
                        track_alias=track_alias,
                        group_id=group_id - 1,
                        object_id=object_id,
                        publisher_priority=priority,
                        status=ObjectStatus.END_OF_GROUP,
                        extensions={MOQT_TIMESTAMP_EXT: int(time.time() * 1000)}
                    )
                    msg = obj.serialize()
                    if session._close_err is not None:
                        raise asyncio.CancelledError
                    session._quic.send_datagram_frame(b'\0' + msg.data)
                    session.transmit()
                object_id = 0

            seq_info = f"{group_id}.{object_id}".encode()
            payload = (seq_info + b'|' + pad)[:object_size]

            obj = ObjectDatagram(
                track_alias=track_alias,
                group_id=group_id,
                object_id=object_id,
                publisher_priority=priority,
                extensions={MOQT_TIMESTAMP_EXT: int(time.time() * 1000)},
                payload=payload,
                end_of_group=(object_id == group_size - 1),
            )
            msg = obj.serialize()
            if session._close_err is not None:
                raise asyncio.CancelledError
            session._quic.send_datagram_frame(b'\0' + msg.data)
            session.transmit()
            total_sent += 1

            object_id += 1
            if paced:
                next_frame_time += frame_interval
                sleep_time = max(0, next_frame_time - time.monotonic())
                await asyncio.sleep(sleep_time)
            else:
                # Yield to event loop periodically
                if total_sent % 64 == 0:
                    await asyncio.sleep(0)

    except asyncio.CancelledError:
        logger.info(f"moqperf pub: sent {total_sent} datagrams")


async def generate_subgroup_stream(session: MOQTSession, subgroup_id: int,
                                   track_alias: int, priority: int,
                                   num_subgroups: int = 1,
                                   object_size: int = 1024, group_size: int = 60,
                                   rate: float = 0):
    """Generate subgroup stream objects. rate=0 means max speed.

    Each subgroup sends a disjoint slice of object IDs within the group:
    subgroup N sends object IDs N, N+num_subgroups, N+2*num_subgroups, ...
    This avoids payload conflicts in the relay cache.
    """
    logger = get_logger(__name__)
    if session._h3 is None:
        return

    pad = b'\x00' * object_size
    paced = rate > 0
    frame_interval = 1.0 / rate if paced else 0
    total_sent = 0

    stream_id = session._h3.create_webtransport_stream(
        session_id=session._session_id,
        is_unidirectional=True
    )

    next_frame_time = time.monotonic()
    group_id = -1
    header = None

    try:
        while True:
            if header is None or header.next_object_id >= group_size:
                group_id += 1

                if header is not None:
                    extensions = {MOQT_TIMESTAMP_EXT: int(time.time() * 1000)}
                    buf = header.end_group(extensions=extensions)
                    if session._close_err or session._h3 is None:
                        raise asyncio.CancelledError
                    session._quic.send_stream_data(stream_id, buf.data, end_stream=True)
                    session.transmit()

                    if stream_id in session._data_streams:
                        del session._data_streams[stream_id]
                    if stream_id in session._stream_tasks:
                        session._stream_tasks[stream_id].cancel()
                        del session._stream_tasks[stream_id]

                    stream_id = session._h3.create_webtransport_stream(
                        session_id=session._session_id,
                        is_unidirectional=True
                    )

                header = SubgroupHeader(
                    track_alias=track_alias,
                    group_id=group_id,
                    subgroup_id=subgroup_id,
                    publisher_priority=priority,
                    extensions_present=True,
                )
                msg = header.serialize()
                if session._close_err is not None:
                    raise asyncio.CancelledError
                session._quic.send_stream_data(stream_id, msg.data, end_stream=False)
                session.transmit()

            obj_id = header.next_object_id
            # Payload must be identical across subgroups for same
            # (group_id, object_id) to avoid relay cache conflicts
            seq_info = f"{group_id}.{obj_id}".encode()
            payload = (seq_info + b'|' + pad)[:object_size]

            extensions = {MOQT_TIMESTAMP_EXT: int(time.time() * 1000)}
            buf = header.next_object(payload=payload, extensions=extensions)

            if session._close_err is not None:
                raise asyncio.CancelledError
            session._quic.send_stream_data(stream_id, buf.data, end_stream=False)
            session.transmit()
            total_sent += 1

            if paced:
                next_frame_time += frame_interval
                sleep_time = max(0, next_frame_time - time.monotonic())
                await asyncio.sleep(sleep_time)
            else:
                if total_sent % 64 == 0:
                    await asyncio.sleep(0)

    except asyncio.CancelledError:
        logger.info(f"moqperf pub: stream {subgroup_id} sent {total_sent} objects")
        raise


def parse_args():
    parser = argparse.ArgumentParser(
        description='moqperf publisher - MoQT benchmark sender',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
relay URL forms:
  moqt://host:port            raw QUIC (default port 4443)
  https://host:port/endpoint  H3/WebTransport (default port 4433)
  host:port                   H3/WebTransport
  host                        H3/WebTransport, port 4433, endpoint /moq

examples:
  %(prog)s moqt://relay.example.com:4443
  %(prog)s relay.example.com -s 4096 -P 4
  %(prog)s relay.example.com --datagram -s 1100 -r 120 -t 60
""")
    parser.add_argument('relay', type=str,
                        help='Relay URL: moqt://host:port, https://host:port/ep, or host[:port]')
    parser.add_argument('-Q', '--force-quic', action='store_true',
                        help='Force raw QUIC even for https:// URLs')
    parser.add_argument('-n', '--namespace', type=str, default='bench',
                        help='MoQT namespace (default: bench)')
    parser.add_argument('--trackname', type=str, default='track',
                        help='MoQT track name (default: track)')
    parser.add_argument('-D', '--datagram', action='store_true',
                        help='Use datagrams instead of streams')
    parser.add_argument('-s', '--object-size', type=int, default=1024,
                        help='Object payload size in bytes (default: 1024)')
    parser.add_argument('-g', '--group-size', type=int, default=10000,
                        help='Objects per group (default: 10000)')
    parser.add_argument('-P', '--streams', type=int, default=1,
                        help='Parallel subgroup streams (default: 1)')
    parser.add_argument('-r', '--rate', type=float, default=0,
                        help='Objects/sec per stream (0=max, default: max)')
    parser.add_argument('-t', '--duration', type=int, default=30,
                        help='Duration in seconds (default: 30)')
    parser.add_argument('-d', '--debug', action='store_true')
    parser.add_argument('--keylogfile', type=str, default=None)
    return parser.parse_args()


def print_banner(relay, args):
    mode = "DATAGRAM" if args.datagram else f"STREAM x{args.streams}"
    if args.rate > 0:
        n = 1 if args.datagram else args.streams
        mbps = args.object_size * args.rate * n * 8 / 1e6
        rate_s = f"{args.rate}/s per stream"
        target_s = f"{mbps:.2f} Mbps"
    else:
        rate_s = "max"
        target_s = "max"
    print("─" * 56)
    print("  moqperf publisher")
    print("─" * 56)
    print(f"  relay:       {relay}")
    print(f"  transport:   {relay.transport_name}")
    print(f"  namespace:   {args.namespace}/{args.trackname}")
    print(f"  mode:        {mode}")
    print(f"  object size: {args.object_size} B")
    print(f"  group size:  {args.group_size} objects")
    print(f"  rate:        {rate_s}")
    print(f"  target:      {target_s}")
    print(f"  duration:    {args.duration}s")
    print("─" * 56)


async def run(args):
    log_level = logging.DEBUG if args.debug else logging.WARNING
    set_log_level(log_level)

    relay = parse_relay_url(args.relay, force_quic=args.force_quic)
    print_banner(relay, args)

    client = MOQTClient(
        relay.host, relay.port,
        endpoint=relay.endpoint,
        use_quic=relay.use_quic,
        debug=args.debug,
        keylog_filename=args.keylogfile,
    )

    if args.datagram:
        handler = partial(dgram_subscribe_data_generator,
                          object_size=args.object_size,
                          group_size=args.group_size,
                          rate=args.rate)
    else:
        handler = partial(subscribe_data_generator,
                          num_tasks=args.streams,
                          object_size=args.object_size,
                          group_size=args.group_size,
                          rate=args.rate)
    client.register_handler(MOQTMessageType.SUBSCRIBE, handler)

    print(f"  Connecting...")
    async with client.connect() as session:
        try:
            await session.client_session_init()

            response = await session.publish_namespace(
                namespace=args.namespace,
                parameters={ParamType.AUTH_TOKEN: b"bench-token"},
                wait_response=True,
            )
            print(f"  Published namespace '{args.namespace}', waiting for subscriber...")

            try:
                await asyncio.wait_for(session.async_closed(), timeout=args.duration)
            except asyncio.TimeoutError:
                print(f"\n  Duration {args.duration}s reached.")
        except Exception as e:
            print(f"  Error: {e}")

    print("  Done.")


if __name__ == "__main__":
    try:
        args = parse_args()
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  Interrupted.")
