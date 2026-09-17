"""LOC packaging over MoQT — draft-ietf-moq-loc-04.

A LOC object payload is the raw encoded-media chunk (WebCodecs
EncodedAudio/VideoChunk bytes); metadata rides MOQ object properties
(even ID = bare vi64 value, odd ID = length-prefixed bytes — the
codec in messages/base.py already implements this rule).

Mapping (loc-04 §4): video groups rotate at random-access points with
Object 0 the RAP, ObjectID++ in decode order; audio is one object per
group. Publishers here rotate on `key_frame`, so an audio caller marks
every frame key_frame=True.

LOC carries no version on the wire, so numbering is handled by emitting
the timestamp under every id a deployed receiver might read and by
accepting all of them on receive (newest first). The legacy ids are
draft-gated: MOQT d18 §15.8 registers 0x06 and 0x02 as Track-scope
properties, so emitting them as Object Properties there is refused.
"""
from __future__ import annotations

import asyncio
import functools
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ..delivery import StreamMapping
from ..track import PublishedTrack, SubscribedTrack
from ..types import ObjectStatus
from ..utils.logger import get_logger

logger = get_logger(__name__)

# MOQ Properties registered by loc-04 §6.1.
LOC_PROP_TIMESCALE = 0x08            # vi64: timestamp units per second
LOC_PROP_VIDEO_FRAME_MARKING = 0x09  # bytes: RFC 9626 flags
LOC_PROP_AUDIO_LEVEL = 0x0C          # vi64: RFC 6464 level + voice activity
LOC_PROP_VIDEO_CONFIG = 0x0D         # bytes: extradata (avcC/hvcC/av1C…)
LOC_PROP_AUDIO_CONFIG = 0x0F         # bytes: codec config (AAC ASC…)
LOC_PROP_TIMESTAMP = 0x10            # vi64: µs since epoch unless TIMESCALE

# Codec string (UTF-8): proposed LOC property, not in loc-04. Catalog-less
# receivers need it to configure a decoder.
LOC_PROP_CODECSTRING = 0x11

# RFC 9626 short-form frame marking: Start, End, Independent.
_FM_START, _FM_END, _FM_INDEPENDENT = 0x80, 0x40, 0x20

# Timestamp ids from superseded numbering, still read by deployed
# receivers: 0x06 = loc-02/loc-03 TIMESTAMP (moqlivemock), 0x02 =
# loc-01 Capture Timestamp (moq-playa).
LOC02_PROP_TIMESTAMP = 0x06
LOC01_PROP_CAPTURE_TS = 0x02

# Receive preference. 0x02 outranks 0x06 because loc-01 assigned 6 to
# Audio Level: a publisher using 0x02 is on loc-01 numbering, where a
# 0x06 alongside it is a level, not a timestamp.
_TIMESTAMP_IDS = (LOC_PROP_TIMESTAMP, LOC01_PROP_CAPTURE_TS,
                  LOC02_PROP_TIMESTAMP)
_CONFIG_IDS = (LOC_PROP_VIDEO_CONFIG, LOC_PROP_AUDIO_CONFIG)
_CONSUMED_IDS = frozenset(_CONFIG_IDS + (LOC_PROP_TIMESCALE,
                                         LOC_PROP_CODECSTRING))

# MoQ Streaming Format registry (loc-02 §6.2; dropped in loc-04).
LOC_STREAMING_FORMAT_TYPE = 0x002


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
    forward-state handling, delivery to each peer, and PUBLISH_DONE come
    from PublishedTrack; produce() consumes the frame queue.

    `config` (codec extradata) is emitted on Object 0 of every group so
    mid-stream joiners can configure a decoder, under VIDEO_CONFIG or
    AUDIO_CONFIG per `media_kind`.

    `codec_string` makes every object self-describing for receivers
    without a catalog: each carries CODECSTRING and TIMESCALE (µs unless
    `timescale` is set), and video objects carry VIDEO_FRAME_MARKING. With
    TIMESCALE present, LOC reads timestamps as media time, not wall clock.
    """

    def __init__(self, session, namespace: str, trackname: str, *,
                 config: Optional[bytes] = None,
                 mapping: StreamMapping = StreamMapping.PER_GROUP,
                 priority: int = 128,
                 timescale: Optional[int] = None,
                 media_kind: str = "video",
                 auth_token: bytes = b"bench-token",
                 queue_size: int = 256,
                 loc01_compat: bool = False,
                 codec_string: Optional[str] = None):
        super().__init__(session, namespace, trackname,
                         priority=priority, auth_token=auth_token)
        self.config = config
        self.mapping = mapping
        self.timescale = timescale
        self.media_kind = media_kind
        self.codec_string = codec_string
        self.frames_dropped = 0  # discarded while Forward State was 0
        self._config_id = (LOC_PROP_AUDIO_CONFIG if media_kind == "audio"
                           else LOC_PROP_VIDEO_CONFIG)
        # Unknown properties are ignored by conformant receivers, so
        # each extra timestamp id costs only a few bytes per object.
        self.loc01_compat = loc01_compat
        self._frames: asyncio.Queue = asyncio.Queue(maxsize=queue_size)

    def _timestamp_ids(self, session) -> tuple:
        """Timestamp ids to emit, given the session's negotiated draft.

        MOQT d18 §15.8 registers 0x06 (SUBGROUP_DELIVERY_TIMEOUT) and
        0x02 (OBJECT_DELIVERY_TIMEOUT) as Track-scope properties, so an
        Object Property under either id is refused there. d14/d16 carry
        no scope rule, which is where the loc-02 ecosystem lives. An
        unknown draft emits only 0x10, the id that is legal everywhere.
        """
        draft = getattr(session, 'negotiated_draft', None)
        if draft is None or draft >= 18:
            return (LOC_PROP_TIMESTAMP,)
        if self.loc01_compat:
            return (LOC_PROP_TIMESTAMP, LOC02_PROP_TIMESTAMP,
                    LOC01_PROP_CAPTURE_TS)
        return (LOC_PROP_TIMESTAMP, LOC02_PROP_TIMESTAMP)

    async def send_frame(self, payload: bytes, *, key_frame: bool = False,
                         timestamp: Optional[int] = None,
                         extensions: Optional[Dict[int, Any]] = None) -> None:
        """Queue one frame (awaits when the queue is full)."""
        await self._frames.put(LocFrame(payload, key_frame, timestamp,
                                        extensions))

    async def finish(self) -> None:
        """Signal end of track; generation drains the queue then stops."""
        await self._frames.put(None)

    def _object_extensions(self, frame: LocFrame, group_start: bool,
                           session=None) -> Dict[int, Any]:
        exts: Dict[int, Any] = dict(frame.extensions or ())
        ts = (frame.timestamp if frame.timestamp is not None
              else int(time.time() * 1_000_000))
        for prop_id in self._timestamp_ids(session or self.session):
            exts[prop_id] = ts
        if self.codec_string is not None:
            exts[LOC_PROP_CODECSTRING] = self.codec_string.encode()
            exts[LOC_PROP_TIMESCALE] = self.timescale or 1_000_000
            if self.media_kind == "video":
                exts[LOC_PROP_VIDEO_FRAME_MARKING] = bytes([
                    _FM_START | _FM_END
                    | (_FM_INDEPENDENT if frame.key_frame else 0)])
        if group_start:
            if self.timescale is not None:
                exts[LOC_PROP_TIMESCALE] = self.timescale
            if self.config is not None:
                exts[self._config_id] = self.config
        return exts

    async def produce(self, out) -> None:
        """Consume the frame queue until finish(), numbering each object
        once; groups rotate on key frames. Properties are built per peer,
        since the timestamp ids a peer accepts depend on its draft."""
        group_id = -1
        obj_id = 0
        resume_on_key = True  # first group opens on a key frame
        while True:
            frame = await self._frames.get()
            if frame is None:
                break
            if not self.producing:
                # Forward State 0: drop the frame unnumbered, end the open
                # group, resume only at a key frame in a new group.
                out.end_group()
                resume_on_key = True
                self.frames_dropped += 1
                continue
            if resume_on_key and not frame.key_frame:
                self.frames_dropped += 1
                continue
            resume_on_key = False
            if frame.key_frame or group_id < 0:
                group_id += 1
                obj_id = 0
            group_start = obj_id == 0
            if frame.timestamp is None:
                frame.timestamp = int(time.time() * 1_000_000)
            await out.write(
                group_id, obj_id, frame.payload,
                extensions=functools.partial(
                    self._object_extensions, frame, group_start),
                group_start=group_start)
            self._note_largest(group_id, obj_id)
            obj_id += 1
            self._total_sent += 1
            self._total_bytes += len(frame.payload)


class LocTrackSubscriber(SubscribedTrack):
    """Subscribes to a LOC track and delivers LocFrames in arrival
    order via on_frame(frame, group_id, object_id). Decoder config is
    captured from VIDEO_CONFIG / AUDIO_CONFIG properties (set_config()
    seeds it from a catalog initRef instead); timestamps are read under
    any LOC numbering, newest first. Arrival order == decode order for
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
        self.codec_string: Optional[str] = None
        self.frames_received = 0

    def set_config(self, config: Optional[bytes]) -> None:
        """Seed decoder config out-of-band (catalog initRef, §5.2.13)."""
        self.config = config

    def _on_object(self, msg, size, ts, group_id, subgroup_id) -> None:
        # End-of-group / end-of-track markers carry no media.
        if getattr(msg, "status", None) not in (None, ObjectStatus.NORMAL):
            return
        exts = msg.extensions or {}
        for prop_id in _CONFIG_IDS:
            if prop_id in exts:
                self.config = bytes(exts[prop_id])
        if LOC_PROP_TIMESCALE in exts:
            self.timescale = exts[LOC_PROP_TIMESCALE]
        if LOC_PROP_CODECSTRING in exts:
            self.codec_string = bytes(exts[LOC_PROP_CODECSTRING]).decode(
                'utf-8', 'replace')
        gid = getattr(msg, 'group_id', None)
        gid = gid if gid is not None else group_id
        ts = next((exts[p] for p in _TIMESTAMP_IDS if p in exts), None)
        # Redundant copies of the chosen timestamp are noise; an id
        # holding a different value carries something else (loc-01
        # Audio Level at 0x06) and is passed through.
        consumed = _CONSUMED_IDS.union(
            p for p in _TIMESTAMP_IDS if exts.get(p, object()) == ts)
        frame = LocFrame(
            payload=bytes(msg.payload),
            key_frame=(msg.object_id == 0),
            timestamp=ts,
            extensions={k: v for k, v in exts.items()
                        if k not in consumed} or None,
        )
        self.frames_received += 1
        if self.on_frame:
            self.on_frame(frame, gid, msg.object_id)
