#!/usr/bin/env python3
import argparse
import asyncio
from qh3.h3.connection import H3_ALPN

from aiomoqt.types import MOQTMessageType, ParamType, ObjectStatus, MOQTException
from aiomoqt.messages import (
    Subscribe, 
    SubgroupHeader, 
    ObjectHeader, 
    ObjectDatagram, 
    ObjectDatagramStatus,
)
from aiomoqt.client import *
from aiomoqt.utils import *

# Create fixed padding buffers once
NUM_SUBGROUP_TASKS = 4
# Create fixed padding buffers once
I_FRAME_PAD = b'I' * 1024 * 64
P_FRAME_PAD = b'P' * 1024 * 64

FRAME_INTERVAL = 1/60 
GROUP_SIZE = 60


async def dgram_subscribe_data_generator(session: MOQTSession, msg: Subscribe) -> None:
    """Wrapper for subscribe handler - spawns stream generators after standard handler"""
    session.default_message_handler(msg.type, msg)  
    logger.debug(f"dgram_subscribe_data_generator: track_alias: {msg.track_alias}")
    # Base layer 
    task = asyncio.create_task(
        generate_group_dgram(
            session=session,
            track_alias=msg.track_alias,
            priority=255  # High priority
        )
    )
    task.add_done_callback(lambda t: session._tasks.discard(t))
    session._tasks.add(task)

    await asyncio.sleep(150)
    session._close_session()
    
async def subscribe_data_generator(session: MOQTSession, msg: Subscribe, num_tasks: int = NUM_SUBGROUP_TASKS) -> None:
    """Wrapper for subscribe handler - spawns stream generators after standard handler
    
    Args:
        session: The MOQT session to use
        msg: The Subscribe message
        num_tasks: Number of subgroup stream tasks to create
    """
    
    session.default_message_handler(msg.type, msg)
    
    # Create the specified number of tasks
    for subgroup_id in range(num_tasks):
        # Base layer has high priority, enhancement layers have lower priority
        priority = 255 if subgroup_id == 0 else 0
        
        task = asyncio.create_task(
            generate_subgroup_stream(
                session=session,
                subgroup_id=subgroup_id,
                track_alias=msg.track_alias,
                priority=priority
            )
        )
        task.add_done_callback(lambda t: session._tasks.discard(t))
        session._tasks.add(task)

    await session.async_closed()
    session._close_session()

async def generate_group_dgram(session: MOQTSession, track_alias: int, priority: int):
    """Generate a stream of objects simulating video frames"""
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
                    status = ObjectStatus.END_OF_GROUP
                    obj = ObjectDatagramStatus(
                        track_alias=track_alias,
                        group_id=group_id,
                        object_id=object_id,
                        publisher_priority=priority,
                        status=status,
                        extensions = {
                            0x00: 4207849484,
                            0x25: f"MOQT-TS: {int(time.time()*1000)}",
                            MOQT_TIMESTAMP_EXT: int(time.time()*1000)
                        }
                    )

                    msg = obj.serialize()
                    if session._close_err is not None:
                        logger.error(f"MOQT app: session closed with error: {session._close_err}")
                        raise MOQTException(*session._close_err)
                    logger.info(f"MOQT app: sending: ObjectDatagramStatus: id: {group_id-1}.{object_id} alias: {obj.track_alias} status: END_OF_GROUP")
                    if session._close_err is not None:
                        raise asyncio.CancelledError
                    session._quic.send_datagram_frame(b'\0' + msg.data)
                    session.transmit()
                    
                object_id = 0
                # prepare I frame
                info = f"| {group_id}.{object_id} |".encode()
                payload = info + I_FRAME_PAD            
            else:
                # prepare P frame            
                info = f"| {group_id}.{object_id} |".encode()
                payload = info + P_FRAME_PAD 

            # system local timestamp
            extensions = {MOQT_TIMESTAMP_EXT: int(time.time()*1000)}

            payload = payload[:1100]    
            obj = ObjectDatagram(
                track_alias=track_alias,
                group_id=group_id,
                object_id=object_id,
                publisher_priority=priority,
                extensions=extensions,
                payload=payload,
            )
            if obj is None:
                logger.error(f"MOQT app: error: ObjectDatagram: constructor failed")
                raise RuntimeError()
            msg = obj.serialize()
            msg_len = len(msg.data)

            if session._close_err is not None:
                raise asyncio.CancelledError
            logger.info(f"MOQT app: sending: ObjectDatagram: id: {group_id}.{object_id} size: {msg_len} bytes")
            session._quic.send_datagram_frame(b'\0' + msg.data)
            session.transmit()
            
            object_id += 1
            next_frame_time += FRAME_INTERVAL
            sleep_time = next_frame_time - time.monotonic()
            sleep_time = 0 if sleep_time < 0 else sleep_time
            logger.debug(f"MOQT app: sleeping: {sleep_time} sec")
            await asyncio.sleep(sleep_time)
                             
    except asyncio.CancelledError:
        logger.warning(f"MOQT app: stream generation cancelled")
        pass
    
async def generate_subgroup_stream(session: MOQTSession, subgroup_id: int, track_alias: int, priority: int):
    """Generate a stream of objects simulating video frames"""
    logger = get_logger(__name__)
    if session._h3 is None:
        return
    stream_id = session._h3.create_webtransport_stream(
        session_id=session._session_id, 
        is_unidirectional=True
    )
    logger.info(f"MOQT app: created data stream({stream_id}): sub-group: {subgroup_id}")

    # Pre-allocate common extension keys to avoid dictionary creation overhead
    moqt_ts_ext = MOQT_TIMESTAMP_EXT
    extensions={MOQT_TIMESTAMP_EXT: None}
    next_frame_time = time.monotonic()
    object_id = 0
    group_id = -1

    try:
        while True:
            if (object_id % GROUP_SIZE) == 0:
                group_id += 1
                if group_id > 0:
                    extensions[moqt_ts_ext] = int(time.time()*1000)
                    status = ObjectStatus.END_OF_GROUP
                    header = ObjectHeader(
                        object_id=object_id,
                        status=status,
                        extensions=extensions
                    )
                    msg = header.serialize()
                    logger.debug(f"MOQT app: sending object status: {header} Ox{msg.data.hex()}")
                    if session._close_err or session._h3 is None or session._quic._close_pending:
                        raise asyncio.CancelledError
                    logger.info(f"MOQT app: sending: ObjectHeader END_OF_GROUP: id: {group_id-1}.{subgroup_id}.{object_id} {msg.tell()} bytes")
                    session._quic.send_stream_data(stream_id, msg.data, end_stream=True)
                    session.transmit()

                    if stream_id in session._data_streams:
                        del session._data_streams[stream_id]
                    
                    if stream_id in session._stream_tasks:
                        session._stream_tasks[stream_id].cancel()
                        del session._stream_tasks[stream_id]

                    # create next group data stream
                    stream_id = session._h3.create_webtransport_stream(
                        session_id=session._session_id,
                        is_unidirectional=True
                    )

                object_id = 0                    
                logger.debug(f"MOQT app: starting new group: id: {group_id}.{subgroup_id}.{object_id} stream: {stream_id}")
                header = SubgroupHeader(
                    track_alias=track_alias,
                    group_id=group_id,
                    subgroup_id=subgroup_id,
                    publisher_priority=priority
                )
                msg = header.serialize()
                
                if session._close_err is not None:
                    raise asyncio.CancelledError
                logger.info(f"MOQT app: sending: {header} {msg.tell()} bytes")
                session._quic.send_stream_data(stream_id, msg.data, end_stream=False)
                session.transmit()
                
                # prepare I frame
                info = f"| {group_id}.{subgroup_id}.{object_id} |".encode()
                payload = info + I_FRAME_PAD
            else:
                # prepare P frame            
                info = f"| {group_id}.{subgroup_id}.{object_id} |".encode()
                payload = info + P_FRAME_PAD    
                
            extensions = {moqt_ts_ext: int(time.time()*1000)}
                
            obj = ObjectHeader(
                object_id=object_id,
                    payload=payload,
                    extensions=extensions
            )                
            msg = obj.serialize()
            if session._close_err is not None:
                raise asyncio.CancelledError
            logger.debug(f"MOQT app: sending ObjectHeader: data: 0x{msg.data_slice(0,16).hex()}...")
            logger.info(f"MOQT app: sending ObjectHeader: id: {group_id}.{subgroup_id}.{object_id} size: {msg.tell()} bytes")
            session._quic.send_stream_data(stream_id, msg.data, end_stream=False)
            session.transmit()
            
            object_id += 1
            next_frame_time += FRAME_INTERVAL
            sleep_time = next_frame_time - time.monotonic()
            sleep_time = 0 if sleep_time < 0 else sleep_time
            await asyncio.sleep(sleep_time)

    except asyncio.CancelledError:
        logger.warning(f"MOQT app: stream generation cancelled")
        raise

def parse_args():
    parser = argparse.ArgumentParser(description='MOQT WebTransport Client')
    parser.add_argument('--host', type=str, default='localhost', help='Host to connect to')
    parser.add_argument('--port', type=int, default=4433, help='Port to connect to')
    parser.add_argument('--namespace', type=str, default='test', help='Namespace')
    parser.add_argument('--trackname', type=str, default='track', help='Track')
    parser.add_argument('--endpoint', type=str, default='moq', help='MOQT WT endpoint')
    parser.add_argument('--datagram', action='store_true', help='Emit ObjectDatagrams')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    parser.add_argument('--quic-debug', action='store_true',  help='Enable quic debug output')
    parser.add_argument('--keylogfile', type=str, default=None, help='TLS secrets file')
                       
    return parser.parse_args()


async def main(host: str, port: int, endpoint: str, namespace: str, trackname: str, debug: bool, datagram: bool):
    log_level = logging.DEBUG if debug else logging.INFO
    set_log_level(log_level)
    logger = get_logger(__name__)

    client = MOQTClient(
        host,
        port,
        endpoint=endpoint,
        keylog_filename=args.keylogfile,
        debug=debug
    )
    # Register our data gen version of the subscribe handler
    if datagram:
        client.register_handler(MOQTMessageType.SUBSCRIBE, dgram_subscribe_data_generator)
    else:
        client.register_handler(MOQTMessageType.SUBSCRIBE, subscribe_data_generator)
                
    logger.info(f"MOQT app: publish session connecting: {client}")
    async with client.connect() as session:
        try:
            # # experiment socket options
            # sock = session._transport.get_extra_info('socket')
            # current_rcvbuf = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
            # current_sndbuf = sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
            # logger.info(f"Current socket buffers - RCVBUF: {current_rcvbuf} bytes, SNDBUF: {current_sndbuf} bytes")

            # Complete the MoQT session setup
            await session.client_session_init()

            logger.info(f"MOQT app: publish_namespace: {namespace}")
            response = await session.publish_namespace(
                namespace=namespace,
                parameters={ParamType.AUTHORIZATION_TOKEN: b"auth-token-123"},
                wait_response=True,
            )
            logger.info(f"MOQT app: publish_namespace reponse: {response}")
            
            # Process subscriptions until closed
            await session.async_closed()
        except Exception as e:
            logger.error(f"MOQT session exception: {e}")
            pass
    
    logger.info(f"MOQT app: publish session closed: {class_name(client)}")


if __name__ == "__main__":

    try:
        args = parse_args()
        asyncio.run(main(
            host=args.host,
            port=args.port,
            endpoint=args.endpoint,
            namespace=args.namespace,
            trackname=args.trackname,
            datagram=args.datagram,
            debug=args.debug
        ))
      
    except KeyboardInterrupt:
        pass