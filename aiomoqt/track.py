"""MoQT Track abstractions — protocol state machine for published and subscribed tracks.

Tracks own the d14/d16 protocol flow and provide a clean interface
for applications to publish and subscribe without knowing wire details.

Usage:
    # Publisher
    track = PublishedTrack(session, "bench", "500k-30fps-x1",
                           object_size=500000, group_size=30, rate=30)
    await track.publish()
    await track.wait_closed()

    # Subscriber
    track = SubscribedTrack(session, "bench")
    track.on_object = stats.on_object
    await track.subscribe()
    await track.wait_closed()

    # Join (subscribe + fetch for playback buffer fill)
    # Uses session.join() directly — fetch is a protocol primitive,
    # not a Track subclass.
    sub_resp, fetch_resp = await session.join(
        namespace="bench", track_name="track", joining_start=3)
    # session.on_fetch_object fires for historic objects
    # session.on_object_received fires for live objects
"""
import asyncio
import time
from enum import IntEnum
from typing import Optional, Callable

from .types import (
    MOQTMessageType, ParamType,
    MOQT_TIMESTAMP_EXT,
)
from .messages import (
    SubgroupHeader, PublishOk, RequestUpdate,
)
from .context import is_draft16_or_later
from .utils.logger import get_logger

logger = get_logger(__name__)


class TrackState(IntEnum):
    """Track lifecycle states."""
    IDLE = 0
    ANNOUNCED = 1       # publish_namespace sent/received
    PUBLISHED = 2       # publish (track) sent/received
    SUBSCRIBED = 3      # subscribe active, data flowing
    CLOSED = 4


class Track:
    """Base MoQT track — shared state for published and subscribed tracks.

    Owns namespace, trackname, and protocol state. Subclasses implement
    the publisher or subscriber side of the protocol.
    """

    def __init__(
        self,
        session,  # MOQTSession
        namespace: str,
        trackname: str = 'track',
        object_size: int = 1024,
        group_size: int = 60,
        num_subgroups: int = 1,
        rate: float = 0,
        draft: Optional[int] = None,
    ):
        self.session = session
        self.namespace = namespace
        self.trackname = trackname
        self.object_size = object_size
        self.group_size = group_size
        self.num_subgroups = num_subgroups
        self.rate = rate
        self.draft = draft
        self.track_alias: int = 0
        self.request_id: int = 0
        self.state: TrackState = TrackState.IDLE
        self._tasks: set = set()

    @property
    def fqtn(self) -> str:
        """Fully qualified track name."""
        return f"{self.namespace}/{self.trackname}"

    def __repr__(self):
        return f"{self.__class__.__name__}({self.fqtn}, state={self.state.name})"


class PublishedTrack(Track):
    """Publisher-side track — announces namespace/track and generates data.

    Handles both d14 (SUBSCRIBE) and d16 (REQUEST_UPDATE) flows.
    When a subscriber arrives, calls generate() which can be overridden.
    """

    def __init__(self, session, namespace: str, trackname: str = 'track',
                 object_size: int = 1024, group_size: int = 60,
                 num_subgroups: int = 1, rate: float = 0,
                 priority: int = 255, draft: Optional[int] = None,
                 auth_token: bytes = b"bench-token"):
        super().__init__(session, namespace, trackname,
                         object_size, group_size, num_subgroups, rate, draft)
        self.priority = priority
        self.auth_token = auth_token
        self._subscriber_event = asyncio.Event()
        self._generating = False
        self._stream_count = 0  # tracks streams opened for PUBLISH_DONE
        self._subscribe_request_id = None  # request_id from relay's SUBSCRIBE

    async def publish(self):
        """Announce namespace and track to the relay."""
        # 1. Publish namespace
        await self.session.publish_namespace(
            namespace=self.namespace,
            parameters={ParamType.AUTH_TOKEN: self.auth_token},
            wait_response=True,
        )
        self.state = TrackState.ANNOUNCED
        logger.info(f"Track: announced namespace '{self.namespace}'")

        # 2. Publish track (d16 sends PUBLISH message)
        if is_draft16_or_later():
            response = self.session.publish(
                namespace=self.namespace,
                track_name=self.trackname,
            )
            self.track_alias = response.track_alias
            self.request_id = response.request_id
            self.state = TrackState.PUBLISHED
            logger.info(f"Track: published {self.fqtn} alias={self.track_alias}")

        # 3. Register handlers for incoming subscribe
        self.session.register_handler(
            MOQTMessageType.SUBSCRIBE, self._on_subscribe)

        # d16: REQUEST_UPDATE (code point 0x02) signals subscriber arrival
        if is_draft16_or_later():
            track = self
            async def _request_update_handler(session, msg):
                await track._on_request_update(session, msg)
            self.session.MOQT_D16_OVERRIDE_REGISTRY[0x02] = (
                RequestUpdate, _request_update_handler)

    async def _on_request_update(self, session, msg: RequestUpdate):
        """Handle d16 REQUEST_UPDATE — subscriber wants data.

        Unlike d14 SUBSCRIBE, no response is needed. Just start generating.
        """
        logger.info(f"Track: REQUEST_UPDATE: {msg}")
        forward = msg.parameters.get(ParamType.FORWARD) if msg.parameters else None
        if not forward or forward <= 0:
            return
        if self._generating:
            logger.info(f"Track: ignoring duplicate REQUEST_UPDATE")
            return

        logger.info(f"Track: subscriber arrived via REQUEST_UPDATE, "
                     f"alias={self.track_alias}")
        self.state = TrackState.SUBSCRIBED
        self._subscriber_event.set()
        self._generating = True
        await self.generate(session, self.track_alias)

    async def _on_subscribe(self, session, msg):
        """Handle incoming SUBSCRIBE (d14)."""
        ok = session.subscribe_ok(request_msg=msg)
        self.track_alias = ok.track_alias
        self._subscribe_request_id = msg.request_id
        logger.info(f"Track: subscriber via SUBSCRIBE, "
                     f"alias={self.track_alias}")

        self.state = TrackState.SUBSCRIBED
        self._subscriber_event.set()

        if not self._generating:
            self._generating = True
            await self.generate(session, self.track_alias)

    def _send_publish_done(self, session, status_code=0x2):
        """Send PUBLISH_DONE with stream count for clean shutdown.

        Status codes: 0x0=INTERNAL_ERROR, 0x2=TRACK_ENDED,
        0x3=SUBSCRIPTION_ENDED, 0x4=GOING_AWAY
        """
        from .messages import SubscribeDone
        req_id = self._subscribe_request_id
        if req_id is None:
            return
        msg = SubscribeDone(
            request_id=req_id,
            status_code=status_code,
            stream_count=self._stream_count,
            reason="track ended",
        )
        logger.info(f"Track: PUBLISH_DONE request_id={req_id} "
                    f"streams={self._stream_count}")
        try:
            session.send_control_message(msg.serialize())
        except Exception:
            pass  # session may already be closing

    _stats_header_printed = False

    def _print_stats_header(self):
        """Print the column header for periodic stats. Deferred to first interval."""
        if self._stats_header_printed:
            return
        self._stats_header_printed = True
        self._do_print_stats_header()

    def _do_print_stats_header(self):
        print(f"\n  {'Interval':<12}{'Groups':<18}{'Objects':<22}{'Bitrate'}")
        print("  " + "─" * 60)

    async def generate(self, session, track_alias: int):
        """Generate data for subscribers. Override for custom content.

        Default implementation sends padded objects at the configured rate.
        """

        pad = b'\xBB' * self.object_size
        paced = self.rate > 0
        frame_interval = 1.0 / self.rate if paced else 0

        for subgroup_id in range(self.num_subgroups):
            priority = self.priority if subgroup_id == 0 else 0
            task = asyncio.create_task(
                self._generate_subgroup(
                    session=session,
                    subgroup_id=subgroup_id,
                    track_alias=track_alias,
                    priority=priority,
                    pad=pad,
                    paced=paced,
                    frame_interval=frame_interval,
                )
            )
            task.add_done_callback(lambda t: self._tasks.discard(t))
            self._tasks.add(task)

        await session.async_closed()
        # Send PUBLISH_DONE to indicate clean track completion
        self._send_publish_done(session)
        # Release the namespace so the relay cleans up
        try:
            session.publish_namespace_done(
                namespace=self.namespace)
        except Exception:
            pass
        session._close_session()

    async def _generate_subgroup(self, session, subgroup_id: int,
                                  track_alias: int, priority: int,
                                  pad: bytes, paced: bool,
                                  frame_interval: float,
                                  report_interval: float = 5.0):
        """Generate a single subgroup stream."""
        total_sent = 0
        total_bytes = 0
        total_groups = 0
        start_time = time.monotonic()
        last_report = start_time
        iv_objects = 0
        iv_bytes = 0
        next_frame_time = time.monotonic()
        group_id = -1
        header = None

        # Only subgroup 0 prints stats (unless _quiet is set)
        report = (subgroup_id == 0
                  and not getattr(self, '_quiet', False))

        cur_obj_id = subgroup_id
        iv_groups = 0
        stream_id = session.open_uni_stream()
        self._stream_count += 1

        try:
            while True:
                if header is None or cur_obj_id >= self.group_size:
                    group_id += 1
                    total_groups += 1
                    iv_groups += 1
                    cur_obj_id = subgroup_id

                    if header is not None:
                        if session._close_err:
                            raise asyncio.CancelledError
                        if subgroup_id == 0:
                            buf = header.end_group(object_id=self.group_size)
                            session.stream_write(stream_id, buf.data,
                                                 end_stream=True)
                        else:
                            session.stream_write(stream_id, b'',
                                                 end_stream=True)
                        session.transmit()

                        if stream_id in session._data_streams:
                            del session._data_streams[stream_id]
                        if stream_id in session._stream_tasks:
                            session._stream_tasks[stream_id].cancel()
                            del session._stream_tasks[stream_id]

                        stream_id = session.open_uni_stream()
                        self._stream_count += 1

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
                    await session.stream_write_drain(stream_id, msg.data)
                    session.transmit()

                seq_info = f"{group_id}.{cur_obj_id}".encode()
                payload = (seq_info + b'|' + pad)[:self.object_size]

                extensions = {MOQT_TIMESTAMP_EXT: int(time.time() * 1000)}
                buf = header.next_object(payload=payload,
                                         extensions=extensions,
                                         object_id=cur_obj_id)
                obj_bytes = len(buf.data)
                cur_obj_id += self.num_subgroups

                if session._close_err is not None:
                    raise asyncio.CancelledError
                await session.stream_write_drain(stream_id, buf.data)
                session.transmit()
                total_sent += 1
                total_bytes += obj_bytes
                iv_objects += 1
                iv_bytes += obj_bytes

                # Periodic stats
                now = time.monotonic()
                if report and now - last_report >= report_interval:
                    dt = now - last_report
                    elapsed = now - start_time
                    obj_s = iv_objects / dt
                    grp_s = iv_groups / dt
                    mbps = (iv_bytes * 8) / (dt * 1e6)
                    iv = f"{elapsed - dt:.0f}-{elapsed:.0f}s"
                    self._print_stats_header()
                    grp_col = f"{total_groups} ({grp_s:.1f}/s)"
                    obj_col = f"{total_sent:,} ({obj_s:.1f}/s)"
                    print(f"  {iv:<12}{grp_col:<18}"
                          f"{obj_col:<22}{mbps:.2f} Mbps")
                    iv_objects = 0
                    iv_bytes = 0
                    iv_groups = 0
                    last_report = now

                if paced:
                    next_frame_time += frame_interval
                    sleep_time = max(0, next_frame_time - time.monotonic())
                    await asyncio.sleep(sleep_time)
                else:
                    if total_sent % 64 == 0:
                        await asyncio.sleep(0)

        except asyncio.CancelledError:
            dur = time.monotonic() - start_time
            if dur > 0 and report:
                mbps = (total_bytes * 8) / (dur * 1e6)
                obj_s = total_sent / dur
                print(f"\n  Sent: {total_sent:,} objects, "
                      f"{total_groups} groups, "
                      f"{obj_s:.1f} obj/s, "
                      f"{mbps:.2f} Mbps ({dur:.1f}s)")
            logger.info(f"Track: subgroup {subgroup_id} "
                        f"sent {total_sent} objects")
            raise

    async def wait_for_subscribers(self, timeout: float = None):
        """Wait until at least one subscriber arrives."""
        if timeout:
            await asyncio.wait_for(
                self._subscriber_event.wait(), timeout=timeout)
        else:
            await self._subscriber_event.wait()

    async def wait_closed(self, timeout: float = None):
        """Wait for session to close or timeout."""
        try:
            if timeout:
                await asyncio.wait_for(
                    self.session.async_closed(), timeout=timeout)
            else:
                await self.session.async_closed()
        except asyncio.TimeoutError:
            pass
        self.state = TrackState.CLOSED


class SubscribedTrack(Track):
    """Subscriber-side track — discovers and subscribes to a published track.

    Handles namespace subscription, track discovery via PUBLISH messages,
    and data reception.
    """

    def __init__(self, session, namespace: str, trackname: str = None,
                 draft: Optional[int] = None,
                 on_object: Optional[Callable] = None,
                 report_interval: float = 5.0):
        super().__init__(session, namespace, trackname or 'track',
                         draft=draft)
        self._auto_discover = (trackname is None)
        self.on_object = on_object
        self.report_interval = report_interval
        self.publish_done: Optional[object] = None  # received PUBLISH_DONE
        self.completed = False  # True if track ended cleanly

    async def subscribe(self, timeout: float = 30.0,
                        forward: int = 1):
        """Subscribe to the track.

        d16: subscribe_namespace → wait for PUBLISH → PUBLISH_OK(forward=1)
             establishes the subscription. No explicit subscribe() needed.
        d14: explicit subscribe() with parameters.

        Args:
            timeout: seconds to wait for PUBLISH or subscribe response
            forward: forwarding preference (1=send objects, 0=hold)
        """
        if self.on_object:
            self.session.on_object_received = self.on_object

        if is_draft16_or_later():
            # Register interest in namespace
            await self.session.subscribe_namespace(
                namespace_prefix=self.namespace,
                parameters={},
                wait_response=True,
            )
            self.state = TrackState.ANNOUNCED

            # Wait for PUBLISH from relay
            if self._auto_discover:
                print(f"  Waiting for publisher on "
                      f"'{self.namespace}'...")
            else:
                print(f"  Waiting for track '{self.fqtn}'...")
            pub_msg = await self.session.await_publish(
                timeout=timeout)

            # Extract namespace/trackname from PUBLISH
            if self._auto_discover:
                self.namespace = '/'.join(
                    p.decode() if isinstance(p, bytes) else p
                    for p in pub_msg.track_namespace
                )
                self.trackname = (
                    pub_msg.track_name.decode()
                    if isinstance(pub_msg.track_name, bytes)
                    else pub_msg.track_name
                )
                print(f"  Discovered: {self.fqtn}")

            # PUBLISH_OK with forward=1 establishes subscription
            ok = PublishOk(
                request_id=pub_msg.request_id,
                forward=forward,
                parameters={},
            )
            logger.info(f"Track: PUBLISH_OK {self.fqtn} "
                        f"forward={forward}")
            self.session.send_control_message(ok.serialize())

        else:
            # d14: explicit subscribe
            await self.session.subscribe(
                namespace=self.namespace,
                track_name=self.trackname,
                forward=forward,
                parameters={
                    ParamType.MAX_CACHE_DURATION: 100,
                    ParamType.AUTH_TOKEN: b"bench-token",
                    ParamType.DELIVERY_TIMEOUT: 10,
                },
                wait_response=True,
            )

        self.state = TrackState.SUBSCRIBED
        logger.info(f"Track: subscribed to {self.fqtn}")

    async def wait_closed(self, timeout: float = None):
        """Wait for session to close or timeout.

        Sets self.completed if track ended cleanly (no StreamReset).
        """
        try:
            if timeout:
                await asyncio.wait_for(
                    self.session.async_closed(), timeout=timeout)
            else:
                await self.session.async_closed()
        except asyncio.TimeoutError:
            self.completed = True  # duration reached = clean
        self.state = TrackState.CLOSED

        # Check close reason
        if hasattr(self.session, '_close_err') and self.session._close_err:
            code, reason = self.session._close_err
            if reason and 'StreamReset' in str(reason):
                self.completed = False
                logger.warning(f"Track: {self.fqtn} ended with "
                               f"StreamReset")
                return
        self.completed = True



class VideoTrack(PublishedTrack):
    """Simulates realistic video track with I/B/P frame sizes.

    Models H.264/H.265 GOP structure with configurable frame sizes
    and B-frame pattern. Each group = one GOP (1 second by default).

    Usage:
        track = VideoTrack(session, "live", "1080p-120fps",
                           resolution="1080p", fps=120)
        await track.publish()
    """

    # Typical frame sizes by resolution (bytes)
    PROFILES = {
        "720p":  {"i_frame": 80_000,  "p_frame": 12_000, "b_frame": 6_000},
        "1080p": {"i_frame": 200_000, "p_frame": 25_000, "b_frame": 10_000},
        "1440p": {"i_frame": 350_000, "p_frame": 40_000, "b_frame": 18_000},
        "4k":    {"i_frame": 600_000, "p_frame": 60_000, "b_frame": 30_000},
    }

    def __init__(self, session, namespace: str, trackname: str = 'video',
                 resolution: str = "1080p", fps: float = 30,
                 gop_pattern: str = "ibp", gop_seconds: float = 1.0,
                 i_frame_size: int = None, p_frame_size: int = None,
                 b_frame_size: int = None,
                 draft: Optional[int] = None, **kwargs):
        # GOP = 1 second of frames by default
        gop_size = int(fps * gop_seconds)

        profile = self.PROFILES.get(resolution, self.PROFILES["1080p"])
        self.i_frame_size = i_frame_size or profile["i_frame"]
        self.p_frame_size = p_frame_size or profile["p_frame"]
        self.b_frame_size = b_frame_size or profile["b_frame"]
        self.gop_pattern_name = gop_pattern
        self.fps = fps

        # Build GOP pattern: I then repeating B..P sequence
        self._gop = self._build_gop(gop_pattern, gop_size)

        # Compute average object size for base class
        total = sum(self._frame_size(ft) for ft in self._gop)
        avg_size = total // len(self._gop)

        super().__init__(
            session, namespace, trackname,
            object_size=avg_size,
            group_size=gop_size,
            num_subgroups=1,
            rate=fps,
            draft=draft,
            **kwargs,
        )

    @staticmethod
    def _build_gop(pattern: str, length: int) -> str:
        """Build GOP frame type sequence.

        ibp: I B B P B B P B B P ... (3:1 B-to-P ratio)
        ip:  I P P P P P ...
        ionly: I I I I ...
        """
        if pattern == "ibp":
            gop = ['I']
            while len(gop) < length:
                gop.extend(['B', 'B', 'P'])
            return ''.join(gop[:length])
        elif pattern == "ip":
            return 'I' + 'P' * (length - 1)
        elif pattern == "ionly":
            return 'I' * length
        else:
            # Custom pattern string, repeat to fill
            reps = (length // len(pattern)) + 1
            return (pattern * reps)[:length]

    def _frame_size(self, frame_type: str) -> int:
        if frame_type == 'I':
            return self.i_frame_size
        elif frame_type == 'P':
            return self.p_frame_size
        return self.b_frame_size

    def _print_stats_header(self):
        """Print GOP info and column header."""
        gop_bytes = sum(self._frame_size(ft) for ft in self._gop)
        gop_mbps = (gop_bytes * 8 * self.fps
                    / self.group_size / 1e6)
        print(f"  GOP:         {self.gop_pattern_name} "
              f"({self.group_size} frames, "
              f"{self.group_size / self.fps:.1f}s)")
        print(f"  I/P/B:       {self.i_frame_size // 1000}KB / "
              f"{self.p_frame_size // 1000}KB / "
              f"{self.b_frame_size // 1000}KB")
        print(f"  bitrate:     ~{gop_mbps:.1f} Mbps")
        print(f"\n  {'Interval':<12}{'GOPs':<18}{'Objects':<22}{'Bitrate'}")
        print("  " + "─" * 60)

    async def _generate_subgroup(self, session, subgroup_id: int,
                                  track_alias: int, priority: int,
                                  pad: bytes, paced: bool,
                                  frame_interval: float,
                                  report_interval: float = 5.0):
        """Generate video frames with variable I/B/P sizes."""
        total_sent = 0
        total_bytes = 0
        total_groups = 0
        start_time = time.monotonic()
        last_report = start_time
        iv_objects = 0
        iv_bytes = 0
        iv_groups = 0
        next_frame_time = time.monotonic()
        group_id = -1
        header = None

        report = (subgroup_id == 0)

        cur_obj_id = subgroup_id
        stream_id = session.open_uni_stream()

        # Pre-generate padding per frame type
        i_pad = b'\x49' * self.i_frame_size  # 'I'
        p_pad = b'\x50' * self.p_frame_size  # 'P'
        b_pad = b'\x42' * self.b_frame_size  # 'B'

        try:
            while True:
                if header is None or cur_obj_id >= self.group_size:
                    group_id += 1
                    total_groups += 1
                    iv_groups += 1
                    cur_obj_id = subgroup_id

                    if header is not None:
                        if session._close_err:
                            raise asyncio.CancelledError
                        buf = header.end_group(
                            object_id=self.group_size)
                        session.stream_write(stream_id, buf.data,
                                             end_stream=True)
                        session.transmit()

                        if stream_id in session._data_streams:
                            del session._data_streams[stream_id]
                        if stream_id in session._stream_tasks:
                            session._stream_tasks[stream_id].cancel()
                            del session._stream_tasks[stream_id]

                        stream_id = session.open_uni_stream()
                        self._stream_count += 1

                    header = SubgroupHeader(
                        track_alias=track_alias,
                        group_id=group_id,
                        subgroup_id=subgroup_id,
                        publisher_priority=255,
                        extensions_present=True,
                    )
                    msg = header.serialize()
                    if session._close_err is not None:
                        raise asyncio.CancelledError
                    await session.stream_write_drain(
                        stream_id, msg.data)
                    session.transmit()

                # Frame type and size from GOP pattern
                ft = self._gop[cur_obj_id % len(self._gop)]
                frame_size = self._frame_size(ft)
                if ft == 'I':
                    frame_pad = i_pad
                elif ft == 'P':
                    frame_pad = p_pad
                else:
                    frame_pad = b_pad

                seq_info = f"{group_id}.{cur_obj_id}.{ft}".encode()
                payload = (seq_info + b'|'
                           + frame_pad)[:frame_size]

                extensions = {
                    MOQT_TIMESTAMP_EXT: int(time.time() * 1000)}
                buf = header.next_object(
                    payload=payload,
                    extensions=extensions,
                    object_id=cur_obj_id)
                obj_bytes = len(buf.data)
                cur_obj_id += self.num_subgroups

                if session._close_err is not None:
                    raise asyncio.CancelledError
                await session.stream_write_drain(
                    stream_id, buf.data)
                session.transmit()
                total_sent += 1
                total_bytes += obj_bytes
                iv_objects += 1
                iv_bytes += obj_bytes

                now = time.monotonic()
                if report and now - last_report >= report_interval:
                    dt = now - last_report
                    elapsed = now - start_time
                    obj_s = iv_objects / dt
                    grp_s = iv_groups / dt
                    mbps = (iv_bytes * 8) / (dt * 1e6)
                    iv = f"{elapsed - dt:.0f}-{elapsed:.0f}s"
                    self._print_stats_header()
                    grp_col = f"{total_groups} ({grp_s:.1f}/s)"
                    obj_col = f"{total_sent:,} ({obj_s:.1f}/s)"
                    print(f"  {iv:<12}{grp_col:<18}"
                          f"{obj_col:<22}{mbps:.2f} Mbps")
                    iv_objects = 0
                    iv_bytes = 0
                    iv_groups = 0
                    last_report = now

                if paced:
                    next_frame_time += frame_interval
                    sleep_time = max(0,
                        next_frame_time - time.monotonic())
                    await asyncio.sleep(sleep_time)
                else:
                    if total_sent % 64 == 0:
                        await asyncio.sleep(0)

        except asyncio.CancelledError:
            dur = time.monotonic() - start_time
            if dur > 0 and report:
                mbps = (total_bytes * 8) / (dur * 1e6)
                obj_s = total_sent / dur
                print(f"\n  Sent: {total_sent:,} objects, "
                      f"{total_groups} GOPs, "
                      f"{obj_s:.1f} obj/s, "
                      f"{mbps:.2f} Mbps ({dur:.1f}s)")
            logger.info(f"VideoTrack: subgroup {subgroup_id} "
                        f"sent {total_sent} objects")
            raise
