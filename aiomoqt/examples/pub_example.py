#!/usr/bin/env python3
import argparse
import asyncio

from aiomoqt.types import MOQTMessageType, ParamType, ObjectStatus, MOQTException, MOQT_TIMESTAMP_EXT
from aiomoqt.messages import (
    Subscribe,
    SubgroupHeader,
    ObjectDatagram,
    ObjectDatagramStatus,
)
from aiomoqt.client import *
from aiomoqt.utils import *

# Defaults
NUM_SUBGROUP_TASKS = 1
DEFAULT_OBJECT_SIZE = 1024

FRAME_INTERVAL = 1/30
GROUP_SIZE = 30


async def dgram_subscribe_data_generator(session: MOQTSession, msg: Subscribe) -> None:
    """Subscribe handler that spawns datagram data generation."""
    ok = session.subscribe_ok(request_msg=msg)
    logger.debug(f"dgram_subscribe_data_generator: track_alias: {ok.track_alias}")
    task = asyncio.create_task(
        generate_group_dgram(
            session=session,
            track_alias=ok.track_alias,
            priority=255
        )
    )
    task.add_done_callback(lambda t: session._tasks.discard(t))
    session._tasks.add(task)

    await asyncio.sleep(150)
    session._close_session()


async def subscribe_data_generator(session: MOQTSession, msg: Subscribe,
                                   num_tasks: int = NUM_SUBGROUP_TASKS,
                                   object_size: int = DEFAULT_OBJECT_SIZE) -> None:
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
                object_size=object_size,
            )
        )
        task.add_done_callback(lambda t: session._tasks.discard(t))
        session._tasks.add(task)
        # Stagger stream starts so relay processes each header before the next
        await asyncio.sleep(0.1)

    await session.async_closed()
    session._close_session()


async def generate_group_dgram(session: MOQTSession, track_alias: int, priority: int):
    """Generate datagram objects simulating video frames."""
    logger = get_logger(__name__)

    next_frame_time = time.monotonic()
    object_id = 0
    group_id = -1
    logger.debug(f"MOQT app: generating dgram group data: {track_alias}")
    try:
        while True:
            if (object_id % GROUP_SIZE) == 0:
                group_id += 1
                if group_id > 0:
                    # Send END_OF_GROUP status for previous group
                    obj = ObjectDatagramStatus(
                        track_alias=track_alias,
                        group_id=group_id - 1,
                        object_id=object_id,
                        publisher_priority=priority,
                        status=ObjectStatus.END_OF_GROUP,
                        extensions={MOQT_TIMESTAMP_EXT: int(time.time()*1000)}
                    )
                    msg = obj.serialize()
                    if session._close_err is not None:
                        raise asyncio.CancelledError
                    logger.info(f"MOQT app: sending ObjectDatagramStatus END_OF_GROUP: "
                                f"{group_id-1}.{object_id}")
                    session._quic.send_datagram_frame(b'\0' + msg.data)
                    session.transmit()

                object_id = 0
                info = f"| {group_id}.{object_id} |".encode()
                payload = (info + I_FRAME_PAD)[:1100]
            else:
                info = f"| {group_id}.{object_id} |".encode()
                payload = (info + P_FRAME_PAD)[:1100]

            obj = ObjectDatagram(
                track_alias=track_alias,
                group_id=group_id,
                object_id=object_id,
                publisher_priority=priority,
                extensions={MOQT_TIMESTAMP_EXT: int(time.time()*1000)},
                payload=payload,
                end_of_group=(object_id == GROUP_SIZE - 1),
            )
            msg = obj.serialize()
            if session._close_err is not None:
                raise asyncio.CancelledError
            logger.info(f"MOQT app: sending ObjectDatagram: "
                        f"{group_id}.{object_id} {len(msg.data)} bytes")
            session._quic.send_datagram_frame(b'\0' + msg.data)
            session.transmit()

            object_id += 1
            next_frame_time += FRAME_INTERVAL
            sleep_time = max(0, next_frame_time - time.monotonic())
            await asyncio.sleep(sleep_time)

    except asyncio.CancelledError:
        logger.warning(f"MOQT app: dgram generation cancelled")


async def generate_subgroup_stream(session: MOQTSession, subgroup_id: int,
                                   track_alias: int, priority: int,
                                   object_size: int = DEFAULT_OBJECT_SIZE):
    """Generate subgroup stream objects simulating video frames.

    Uses SubgroupHeader.next_object() for automatic delta encoding
    and object_id tracking.
    """
    logger = get_logger(__name__)
    I_FRAME_PAD = b'I' * object_size
    P_FRAME_PAD = b'P' * object_size
    stream_id = session.open_uni_stream()
    logger.info(f"MOQT app: created data stream({stream_id}): subgroup: {subgroup_id}")

    next_frame_time = time.monotonic()
    group_id = -1
    use_extensions = True
    header = None

    try:
        while True:
            # Check if we need a new group
            if header is None or header.next_object_id >= GROUP_SIZE:
                group_id += 1

                # End the previous group
                if header is not None:
                    extensions = {MOQT_TIMESTAMP_EXT: int(time.time()*1000)} if use_extensions else None
                    buf = header.end_group(extensions=extensions)
                    if session._close_err:
                        raise asyncio.CancelledError
                    logger.info(f"MOQT app: sending END_OF_GROUP: "
                                f"{group_id-1}.{subgroup_id}.{header._last_object_id} "
                                f"{buf.tell()} bytes")
                    session.stream_write(stream_id, buf.data, end_stream=True)
                    session.transmit()

                    # Clean up old stream
                    if stream_id in session._data_streams:
                        del session._data_streams[stream_id]
                    if stream_id in session._stream_tasks:
                        session._stream_tasks[stream_id].cancel()
                        del session._stream_tasks[stream_id]

                    # Create new stream for next group
                    stream_id = session.open_uni_stream()

                # Start new subgroup header — tracks object_id and delta state
                header = SubgroupHeader(
                    track_alias=track_alias,
                    group_id=group_id,
                    subgroup_id=subgroup_id,
                    publisher_priority=priority,
                    extensions_present=use_extensions,
                )
                msg = header.serialize()
                if session._close_err is not None:
                    raise asyncio.CancelledError
                logger.info(f"MOQT app: sending {header} {msg.tell()} bytes")
                session.stream_write(stream_id, msg.data)
                session.transmit()

                # I-frame for first object in group
                obj_id = 0
                info = f"| {group_id}.{obj_id} |".encode()
                payload = (info + I_FRAME_PAD)[:object_size]
            else:
                # P-frame for subsequent objects
                obj_id = header.next_object_id
                info = f"| {group_id}.{obj_id} |".encode()
                payload = (info + P_FRAME_PAD)[:object_size]

            # Send next object — delta encoding handled automatically
            extensions = {MOQT_TIMESTAMP_EXT: int(time.time()*1000)} if use_extensions else None
            buf = header.next_object(payload=payload, extensions=extensions)

            if session._close_err is not None:
                raise asyncio.CancelledError
            logger.info(f"MOQT app: sending ObjectHeader: "
                        f"{group_id}.{subgroup_id}.{header._last_object_id} "
                        f"{buf.tell()} bytes")
            session.stream_write(stream_id, buf.data)
            session.transmit()

            next_frame_time += FRAME_INTERVAL
            sleep_time = max(0, next_frame_time - time.monotonic())
            await asyncio.sleep(sleep_time)

    except asyncio.CancelledError:
        logger.warning(f"MOQT app: stream generation cancelled")
        raise


def parse_args():
    parser = argparse.ArgumentParser(description='MOQT WebTransport Client')
    parser.add_argument('--host', type=str, default='localhost', help='Host to connect to')
    parser.add_argument('--port', type=int, default=443, help='Port to connect to')
    parser.add_argument('--namespace', type=str, default='test', help='Namespace')
    parser.add_argument('--trackname', type=str, default='track', help='Track')
    parser.add_argument('--use-quic', action='store_true', help='Enable QUIC transport')
    parser.add_argument('--endpoint', type=str, default='moq', help='MOQT endpoint')
    parser.add_argument('--datagram', action='store_true', help='Emit ObjectDatagrams')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    parser.add_argument('--quic-debug', action='store_true', help='Enable quic debug output')
    parser.add_argument('--keylogfile', type=str, default=None, help='TLS secrets file')
    parser.add_argument('--insecure', action='store_true', help='Skip TLS certificate verification')
    parser.add_argument('--auth-token', type=str, default=None, help='Auth token')
    parser.add_argument('--draft', type=int, default=None, help='MoQT draft version (e.g. 14, 16)')
    parser.add_argument('-P', '--streams', type=int, default=1, help='Parallel subgroup streams (default: 1)')
    parser.add_argument('-s', '--object-size', type=int, default=1024, help='Object payload size bytes (default: 1024)')

    return parser.parse_args()


async def main(host: str, port: int, endpoint: str, namespace: str, trackname: str,
               debug: bool, datagram: bool, use_quic: bool, quic_debug: bool,
               insecure: bool = False, auth_token: str = None, draft: int = None,
               streams: int = 1, object_size: int = 1024):
    log_level = logging.DEBUG if debug else logging.INFO
    set_log_level(log_level)
    logger = get_logger(__name__)

    client = MOQTClient(
        host,
        port,
        endpoint=endpoint,
        use_quic=use_quic,
        verify_tls=not insecure,
        draft_version=draft,
        debug=debug,
        quic_debug=quic_debug,
        keylog_filename=args.keylogfile,
    )
    # Register our data gen version of the subscribe handler
    if datagram:
        client.register_handler(MOQTMessageType.SUBSCRIBE, dgram_subscribe_data_generator)
    else:
        from functools import partial
        handler = partial(subscribe_data_generator, num_tasks=streams, object_size=object_size)
        client.register_handler(MOQTMessageType.SUBSCRIBE, handler)

    logger.info(f"MOQT app: publish session connecting: {client}")
    async with client.connect() as session:
        try:
            await session.client_session_init()

            logger.info(f"MOQT app: publish_namespace: {namespace}")
            params = {ParamType.AUTH_TOKEN: auth_token.encode()} if auth_token else {}
            response = await session.publish_namespace(
                namespace=namespace,
                parameters=params,
                wait_response=True,
            )
            logger.info(f"MOQT app: publish_namespace response: {response}")

            # Process subscriptions until closed
            await session.async_closed()
        except Exception as e:
            logger.error(f"MOQT session exception: {e}")

    logger.info(f"MOQT app: publish session closed: {class_name(client)}")


if __name__ == "__main__":

    try:
        args = parse_args()
        asyncio.run(main(
            host=args.host,
            port=args.port,
            endpoint=args.endpoint,
            use_quic=args.use_quic,
            namespace=args.namespace,
            trackname=args.trackname,
            datagram=args.datagram,
            debug=args.debug,
            quic_debug=args.quic_debug,
            insecure=args.insecure,
            auth_token=args.auth_token,
            draft=args.draft,
            streams=args.streams,
            object_size=args.object_size,
        ))

    except KeyboardInterrupt:
        pass
