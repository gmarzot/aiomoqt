"""LOC packaging over MoQT — draft-ietf-moq-loc-02.

A LOC object payload is the raw encoded-media chunk (WebCodecs
EncodedAudio/VideoChunk bytes); metadata rides MOQ object properties
(even ID = bare vi64 value, odd ID = length-prefixed bytes — the
codec in messages/base.py already implements this rule).

Mapping (loc-02 §4): video groups rotate at random-access points with
Object 0 the RAP, ObjectID++ in decode order; audio is one object per
group. Publishers here rotate on `key_frame`, so an audio caller marks
every frame key_frame=True.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional

from ..messages import SubgroupHeader
from ..messages.track import ObjectDatagram
from ..track import PublishedTrack, SubscribedTrack
from ..utils.logger import get_logger

logger = get_logger(__name__)

# MOQ Properties registered by loc-02 §6.1.
LOC_PROP_TIMESTAMP = 0x06   # vi64; µs since Unix epoch unless TIMESCALE
LOC_PROP_TIMESCALE = 0x08   # vi64; timestamp units per second

# Provisional ("IANA, please assign") — subject to renumbering. The
# draft's Audio Level placeholder (6) collides with the registered
# TIMESTAMP, so no AUDIO_LEVEL constant is defined here yet.
LOC_PROP_VIDEO_CONFIG = 13  # odd → bytes: codec extradata (avcC/hvcC…)
LOC_PROP_FRAME_MARKING = 4  # vi64: RFC 9626 flags

# loc-01's Capture Timestamp ID — players still on loc-01 numbering
# (moq-playa) read timestamps here instead of 0x06.
LOC01_PROP_CAPTURE_TS = 0x02

# MoQ Streaming Format registry (loc-02 §6.2).
LOC_STREAMING_FORMAT_TYPE = 0x002


class StreamMapping(Enum):
    PER_GROUP = "per_group"    # loc-02 §4.2: one uni stream per group
    PER_OBJECT = "per_object"  # msf-01 §6: one uni stream per object
    DATAGRAM = "datagram"      # loc-02 §4.1: one datagram per object


@dataclass
class LocFrame:
    """One encoded media chunk. `timestamp` is in track timescale units
    (µs since Unix epoch when the track carries no TIMESCALE)."""
    payload: bytes
    key_frame: bool = False
    timestamp: Optional[int] = None
    extensions: Optional[Dict[int, Any]] = None


class LocTrackPublisher(PublishedTrack):
    """Push-model LOC publisher: the app feeds frames via send_frame();
    groups rotate on key frames. Announce/subscribe handshake, relay
    forward-state handling, and PUBLISH_DONE come from PublishedTrack;
    generation consumes the frame queue instead of synthesizing
    payloads.

    `config` (video extradata) is emitted as VIDEO_CONFIG on Object 0 of
    every group so mid-stream joiners can configure a decoder.
    """

    def __init__(self, session, namespace: str, trackname: str, *,
                 config: Optional[bytes] = None,
                 mapping: StreamMapping = StreamMapping.PER_GROUP,
                 priority: int = 128,
                 timescale: Optional[int] = None,
                 auth_token: bytes = b"bench-token",
                 queue_size: int = 256,
                 loc01_compat: bool = False):
        super().__init__(session, namespace, trackname,
                         priority=priority, auth_token=auth_token)
        self.config = config
        self.mapping = mapping
        self.timescale = timescale
        # Dual-emit the timestamp under loc-01's 0x02 as well; unknown
        # properties are ignored by conformant receivers, so this only
        # costs a few bytes per object.
        self.loc01_compat = loc01_compat
        self._frames: asyncio.Queue = asyncio.Queue(maxsize=queue_size)

    async def send_frame(self, payload: bytes, *, key_frame: bool = False,
                         timestamp: Optional[int] = None,
                         extensions: Optional[Dict[int, Any]] = None) -> None:
        """Queue one frame (awaits when the queue is full)."""
        await self._frames.put(LocFrame(payload, key_frame, timestamp,
                                        extensions))

    async def finish(self) -> None:
        """Signal end of track; generation drains the queue then stops."""
        await self._frames.put(None)

    def _object_extensions(self, frame: LocFrame,
                           group_start: bool) -> Dict[int, Any]:
        exts: Dict[int, Any] = dict(frame.extensions or ())
        exts[LOC_PROP_TIMESTAMP] = (
            frame.timestamp if frame.timestamp is not None
            else int(time.time() * 1_000_000))
        if self.loc01_compat:
            exts[LOC01_PROP_CAPTURE_TS] = exts[LOC_PROP_TIMESTAMP]
        if group_start:
            if self.timescale is not None:
                exts[LOC_PROP_TIMESCALE] = self.timescale
            if self.config is not None:
                exts[LOC_PROP_VIDEO_CONFIG] = self.config
        return exts

    async def generate(self, session, track_alias: int):
        """Consume the frame queue until finish(); replaces the bench
        generator that PublishedTrack's forward-state handling starts."""
        group_id = -1
        obj_id = 0
        header: Optional[SubgroupHeader] = None  # PER_GROUP open stream
        stream_id: Optional[int] = None
        prof = session._profile

        def _close_group_stream():
            nonlocal stream_id, header
            if stream_id is not None:
                buf = header.end_group(object_id=header.next_object_id)
                session.stream_write(stream_id, buf.data, end_stream=True)
            stream_id = None
            header = None

        try:
            while True:
                frame = await self._frames.get()
                if frame is None:
                    break
                if frame.key_frame or group_id < 0:
                    _close_group_stream()
                    group_id += 1
                    obj_id = 0
                group_start = obj_id == 0
                exts = self._object_extensions(frame, group_start)

                if self.mapping is StreamMapping.DATAGRAM:
                    dgram = ObjectDatagram(
                        track_alias=track_alias, group_id=group_id,
                        object_id=obj_id,
                        publisher_priority=self.priority,
                        extensions=exts, payload=frame.payload)
                    await session.dgram_write_drain(
                        dgram.serialize(prof=prof))
                elif self.mapping is StreamMapping.PER_OBJECT:
                    # One stream per object ⇒ one subgroup per object
                    # (a subgroup owns exactly one stream); the object
                    # keeps its decode-order id via subgroup_id.
                    sid = await session.open_uni_stream()
                    self._stream_count += 1
                    hdr = SubgroupHeader(
                        track_alias=track_alias, group_id=group_id,
                        subgroup_id=obj_id,
                        publisher_priority=self.priority,
                        extensions_present=True, prof=prof)
                    session.stream_write(sid, hdr.serialize().data)
                    buf = hdr.next_object(payload=frame.payload,
                                          extensions=exts,
                                          object_id=obj_id)
                    await session.stream_write_drain(sid, buf.data)
                    session.stream_write(sid, b"", end_stream=True)
                else:  # PER_GROUP
                    if group_start:
                        stream_id = await session.open_uni_stream()
                        self._stream_count += 1
                        header = SubgroupHeader(
                            track_alias=track_alias, group_id=group_id,
                            subgroup_id=0,
                            publisher_priority=self.priority,
                            extensions_present=True, prof=prof)
                        session.stream_write(stream_id,
                                             header.serialize().data)
                    buf = header.next_object(payload=frame.payload,
                                             extensions=exts,
                                             object_id=obj_id)
                    await session.stream_write_drain(stream_id, buf.data)

                obj_id += 1
                self._total_sent += 1
                self._total_bytes += len(frame.payload)
        except asyncio.CancelledError:
            raise
        finally:
            _close_group_stream()
        self._send_publish_done(session)


class LocTrackSubscriber(SubscribedTrack):
    """Subscribes to a LOC track and delivers LocFrames in arrival
    order via on_frame(frame, group_id, object_id). Decoder config is
    captured from VIDEO_CONFIG properties (set_config() seeds it from a
    catalog initRef instead). Arrival order == decode order for
    single-subgroup tracks; temporal-layer merge is not implemented.
    """

    def __init__(self, session, namespace: str, trackname: str = None,
                 on_frame: Optional[Callable] = None,
                 auth_token: Optional[bytes] = None):
        super().__init__(session, namespace, trackname,
                         on_object=self._on_object, auth_token=auth_token)
        self.on_frame = on_frame
        self.config: Optional[bytes] = None
        self.timescale: Optional[int] = None
        self.frames_received = 0

    def set_config(self, config: Optional[bytes]) -> None:
        """Seed decoder config out-of-band (catalog initRef, §5.2.13)."""
        self.config = config

    def _on_object(self, msg, size, ts, group_id, subgroup_id) -> None:
        exts = msg.extensions or {}
        if LOC_PROP_VIDEO_CONFIG in exts:
            self.config = bytes(exts[LOC_PROP_VIDEO_CONFIG])
        if LOC_PROP_TIMESCALE in exts:
            self.timescale = exts[LOC_PROP_TIMESCALE]
        gid = getattr(msg, 'group_id', None)
        gid = gid if gid is not None else group_id
        frame = LocFrame(
            payload=bytes(msg.payload),
            key_frame=(msg.object_id == 0),
            timestamp=exts.get(LOC_PROP_TIMESTAMP),
            extensions={k: v for k, v in exts.items()
                        if k not in (LOC_PROP_TIMESTAMP, LOC_PROP_TIMESCALE,
                                     LOC_PROP_VIDEO_CONFIG)} or None,
        )
        self.frames_received += 1
        if self.on_frame:
            self.on_frame(frame, gid, msg.object_id)
