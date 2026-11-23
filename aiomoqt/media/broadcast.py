"""MSF broadcast orchestration — one session, many tracks.

MediaPublisher publishes the catalog track plus LOC media tracks and
demuxes relay control messages to the right track (PublishedTrack's own
handler registration assumes one track per session and clobbers).
MediaSubscriber consumes the catalog, then subscribes the media tracks
it describes.

Catalog wire placement (msf-01 §5): each update is one object in
subgroup 0; Object 0 of a group is a complete independent catalog,
higher objects are deltas; a new independent catalog starts a new group.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Optional

from ..track import PublishedTrack, TrackState
from ..types import (
    MOQTMessageType, MOQTRequestError, ObjectStatus, RequestErrorCode,
)
from ..utils.logger import get_logger
from .catalog import (
    Catalog, CatalogError, CATALOG_TRACK_NAME, MEDIA_PACKAGINGS,
    PACKAGING_LOC, PACKAGING_LOCMAF, PACKAGING_MPEG2TS,
)
from .loc import LocTrackPublisher, LocTrackSubscriber
from .locmaf import (
    LocmafError, LocmafTrackSubscriber, SUPPORTED_LOCMAF_VERSIONS,
    TrackContext,
)
from .msfts import MsftsError, MsftsTrack, MsftsTrackSubscriber

logger = get_logger(__name__)

# §5.2: the catalog track SHOULD outrank every track it describes.
# MoQT priority is ascending-urgency (lower = more urgent).
CATALOG_PUBLISHER_PRIORITY = 64
# Audio outranks video: a video group's long stream must not hold audio.
AUDIO_PUBLISHER_PRIORITY = 64
VIDEO_PUBLISHER_PRIORITY = 128
_DEFAULT_PUBLISHER_PRIORITY = 128

# Catalog history kept for FETCH: players fetch the catalog.
CATALOG_FETCH_HISTORY = 64 << 10


class CatalogTrackPublisher(PublishedTrack):
    """Publishes the "catalog" track: each independent catalog opens a
    group as Object 0; deltas follow as Objects >= 1.

    Objects are numbered once, kept in the FETCH history and fanned out
    to every subscription's lane. A subscription that joins, or whose
    Forward State returns to 1, opens a new group with the current
    catalog, so it starts at a complete one (§11.2); every subscription
    takes that group. At Forward State 0 it gets nothing (§5.1). Group
    IDs continue across subscriptions.
    """

    def __init__(self, session, namespace: str, catalog: Optional[Catalog]):
        super().__init__(session, namespace, CATALOG_TRACK_NAME,
                         priority=CATALOG_PUBLISHER_PRIORITY,
                         fetch_history_bytes=CATALOG_FETCH_HISTORY)
        self.catalog = catalog
        # The current catalog is numbered on first demand, so
        # SUBSCRIBE_OK says ContentExists even before that.
        self._largest = (0, 0)
        # A relay holding later groups would read a restarted 0 as stale.
        self._next_group = 0
        # Open group and its next Object ID; None until first demand.
        self._group: Optional[int] = None
        self._next_object = 0
        self._finished = asyncio.Event()

    @property
    def next_group(self) -> int:
        """Group ID the next independent catalog opens."""
        return self._next_group

    @next_group.setter
    def next_group(self, group_id: int) -> None:
        self._next_group = group_id

    def _number(self, group_start: bool) -> tuple:
        """The next object's Location."""
        if group_start:
            self._group, self._next_object = self._next_group, 0
            self._next_group += 1
        loc = (self._group, self._next_object)
        self._next_object += 1
        return loc

    def _open(self) -> None:
        """Number the current catalog on first demand, into the history
        for FETCH; no peer has a lane yet."""
        if self._group is None and self.catalog is not None:
            group_id, object_id = self._number(group_start=True)
            self._remember(group_id, object_id, self.catalog.to_json().encode())

    async def _write(self, payload: bytes, group_start: bool) -> None:
        group_id, object_id = self._number(group_start)
        if self._out is None:
            self._remember(group_id, object_id, payload)
        else:
            await self._out.write(group_id, object_id, payload,
                                  group_start=group_start)

    async def publish_catalog(self, catalog: Catalog) -> None:
        """Publish a new independent catalog: it opens a new group. Before
        first demand it only replaces the catalog to be numbered."""
        if self._finished.is_set():
            return
        self.catalog = catalog
        if self._group is not None:
            await self._write(catalog.to_json().encode(), group_start=True)

    async def publish_delta(self, delta: Catalog) -> None:
        """Publish a delta update: the next object in the open group."""
        if not delta.is_delta:
            await self.publish_catalog(delta)
            return
        if self._finished.is_set():
            return
        self._open()
        self.catalog.apply(delta)
        await self._write(delta.to_json().encode(), group_start=False)

    async def finish(self) -> None:
        """End the track: End of Track, then PUBLISH_DONE, on each
        subscription served."""
        self._finished.set()

    async def produce(self, out) -> None:
        # publish_*() write the objects; this holds the track open.
        await self._finished.wait()

    async def _new_group(self) -> None:
        """Open a group with the current catalog for a joining or
        resuming subscription; after finish() too, ahead of End of
        Track."""
        await self._write(self.catalog.to_json().encode(), group_start=True)

    async def _start_generating(self, session, trigger: str, sub=None):
        self._open()
        sub = self._sub_for(session) if sub is None else sub
        delivery = sub.delivery
        await super()._start_generating(session, trigger, sub=sub)
        if sub.delivery is not delivery and sub.forward:
            # The new lane joins at the next group boundary.
            await self._new_group()

    def _set_forward(self, sub, forward: Optional[int]) -> None:
        resumed = bool(forward) and not sub.forward and sub.generating
        super()._set_forward(sub, forward)
        lane = getattr(sub.delivery, "lane", None)
        if resumed and lane is not None and self._group is not None:
            # Drop the rest of the open group; resume at the next one.
            lane.resume(self._group)
            asyncio.ensure_future(self._new_group())

    def _resume_lane(self, sub) -> None:
        """_set_forward resumed the lane."""

    def _on_sent(self, group_id: int, object_id: int) -> None:
        self._total_sent += 1

    def _stop_producing(self, sub=None) -> None:
        """Idle one peer and reset its open stream (§5.1.1). Production
        ends once every peer's session has closed."""
        sub = self._subs[0] if sub is None else sub
        super()._stop_producing(sub)
        if sub.delivery is not None and not sub.generating:
            sub.delivery.cancel()
        prod = self._production
        if prod is not None and not prod.done() and all(
                getattr(s.session, '_close_err', None) is not None
                for s in self._subs):
            prod.cancel()

    async def _on_fetch(self, session, msg) -> None:
        """FETCH from the catalog objects published; before any was, the
        current catalog is numbered first."""
        self._open()
        await super()._on_fetch(session, msg)


class MediaPublisher:
    """Publishes an MSF broadcast: catalog + LOC media tracks on one
    session, with control demux by track name / request id.

    With `catalog` None no catalog track is published or served;
    `catalog_track` stays, unpublished, so callers need no special case."""

    def __init__(self, session, namespace: str,
                 catalog: Optional[Catalog]):
        self.session = session
        self.namespace = namespace
        self.catalog_track = CatalogTrackPublisher(session, namespace,
                                                   catalog)
        self._by_name: Dict[str, PublishedTrack] = (
            {} if catalog is None else {CATALOG_TRACK_NAME: self.catalog_track})

    def add_track(self, track: LocTrackPublisher) -> LocTrackPublisher:
        """A track left at the default priority takes its media kind's."""
        if track.priority == _DEFAULT_PUBLISHER_PRIORITY:
            track.priority = (AUDIO_PUBLISHER_PRIORITY
                              if getattr(track, 'media_kind', None) == "audio"
                              else VIDEO_PUBLISHER_PRIORITY)
        self._by_name[track.trackname] = track
        return track

    async def start(self, announce_namespace: bool = False,
                    publish_track: bool = True, forward: int = 0) -> None:
        """Install the session-level demux over PublishedTrack's per-track
        handlers, then announce the broadcast.

        announce_namespace: one PUBLISH_NAMESPACE for the broadcast; the
        relay forwards subscriber SUBSCRIBEs per track.
        publish_track: PUBLISH every track (catalog first, §11.2).
        forward: initial Forward State in PUBLISH (§9.13). 0 sends no
        objects until PUBLISH_OK, SUBSCRIBE or an update carries
        forward=1; 1 starts immediately, and a peer's forward=0 pauses.
        """
        if not (announce_namespace or publish_track):
            raise ValueError("start(): need announce_namespace or "
                             "publish_track")
        self._started = dict(publish_track=publish_track, forward=forward)
        # Before any announcement: a SUBSCRIBE may overtake its reply.
        self.session.register_handler(
            MOQTMessageType.SUBSCRIBE, self._demux_subscribe)
        self.session.register_handler(
            MOQTMessageType.PUBLISH_OK, self._demux_publish_ok)
        self.session.register_handler(
            MOQTMessageType.SUBSCRIBE_UPDATE, self._demux_update)
        self.session.register_handler(
            MOQTMessageType.FETCH, self._demux_fetch)
        if not publish_track:
            # Set first: an early SUBSCRIBE moves it on to SUBSCRIBED.
            for track in self._by_name.values():
                track._sub_for(self.session).state = TrackState.ANNOUNCED
        if announce_namespace:
            await self.session.publish_namespace(
                namespace=self.namespace, wait_response=True)
            logger.info(f"MediaPublisher: announced '{self.namespace}'")
        if publish_track:
            for track in self._by_name.values():
                # The track may also serve other sessions; this is ours.
                await track.publish(publish_track=True, forward=forward,
                                    session=self.session)

    async def publish_track(self, track: PublishedTrack) -> None:
        """Offer a track added after start() as start() offered the
        others: PUBLISH, or held for a SUBSCRIBE under the announced
        namespace."""
        started = getattr(self, '_started', None)
        if started is None:
            return
        if started['publish_track']:
            await track.publish(publish_track=True, forward=started['forward'],
                                session=self.session)
        else:
            track._sub_for(self.session).state = TrackState.ANNOUNCED

    def remove_track(self, name: str) -> None:
        """Route nothing more to track `name`; a SUBSCRIBE for it is
        refused (DOES_NOT_EXIST)."""
        self._by_name.pop(name, None)

    def _track_for_request(self, request_id) -> Optional[PublishedTrack]:
        for track in self._by_name.values():
            if track.request_id == request_id:
                return track
        return None

    async def _demux_subscribe(self, session, msg) -> None:
        name = msg.track_name
        name = name.decode() if isinstance(name, bytes) else name
        track = self._by_name.get(name)
        if track is None:
            logger.warning(f"MediaPublisher: SUBSCRIBE for unknown "
                           f"track {name!r}")
            session.subscribe_error(
                msg.request_id,
                RequestErrorCode.DOES_NOT_EXIST, "track not published here")
            return
        await track._on_subscribe(session, msg)

    async def _demux_publish_ok(self, session, msg) -> None:
        track = self._track_for_request(msg.request_id)
        if track is not None:
            await track._on_publish_ok(session, msg)

    async def _demux_fetch(self, session, msg) -> None:
        """Route a FETCH to its track, which serves it from the objects it
        has published; FETCH_OK's End is the end of that range."""
        track = None
        name = msg.track_name
        if name is not None:
            name = name.decode() if isinstance(name, bytes) else name
            track = self._by_name.get(name)
        if track is None and msg.joining_request_id is not None:
            track = next(
                (t for t in self._by_name.values()
                 if msg.joining_request_id in t._sub_for(session).subscribers),
                None)
        if track is None:
            session.fetch_error(
                request_id=msg.request_id,
                error_code=RequestErrorCode.DOES_NOT_EXIST,
                reason="track not published here")
            return
        await track._on_fetch(session, msg)

    async def _demux_update(self, session, msg) -> None:
        from ..messages import RequestUpdate
        # d16 REQUEST_UPDATE references the original request via
        # existing_request_id (its request_id is the update's own);
        # d18 dropped that field — its request_id IS the reference.
        rid = (msg.existing_request_id
               if isinstance(msg, RequestUpdate)
               and msg.existing_request_id is not None
               else msg.request_id)
        track = self._track_for_request(rid)
        if track is None:
            logger.warning(f"MediaPublisher: update for unknown "
                           f"request_id={rid}")
            return
        await track._on_request_update(session, msg)


class MediaSubscriber:
    """Consumes an MSF broadcast: reads the catalog track, then
    subscribes every LOC, CMAF, LOCMAF and mpeg2ts track it describes (a
    LOCMAF track only with a supported locmafVersion and an init, an
    mpeg2ts track only with valid MSFTS fields; its frames come in Group
    and Object order, see MsftsTrackSubscriber).

    on_frame(track_name, frame, group_id, object_id) receives media;
    on_arrival(track_name, msg, recv_us, group_id, subgroup_id, frame)
    sees every media-track object first (see LocTrackSubscriber);
    on_catalog(catalog) fires on every independent catalog or applied
    delta. `timestamp_errors` is each media track's
    SubscribedTrack policy; `repair_s` each mpeg2ts track's FETCH repair
    budget (see MsftsTrackSubscriber). A media track's Track Properties are its
    subscriber's (`tracks[name]`); `properties(name)` is the merged view.
    """

    def __init__(self, session, namespace: str, *,
                 on_frame: Optional[Callable] = None,
                 on_catalog: Optional[Callable] = None,
                 track_filter: Optional[Callable] = None,
                 discover: bool = False,
                 on_arrival: Optional[Callable] = None,
                 timestamp_errors: str = 'cancel',
                 repair_s: float = 0.0):
        self.session = session
        self.namespace = namespace
        self.timestamp_errors = timestamp_errors
        self.repair_s = repair_s
        # Treat `namespace` as a prefix and resolve it to the namespace
        # actually being published under it (§9.4), so a subscriber can
        # find a broadcast whose name it was never told.
        self.discover = discover
        self.on_frame = on_frame
        self.on_arrival = on_arrival
        self.on_catalog = on_catalog
        self.track_filter = track_filter or (lambda t: True)
        self.catalog: Optional[Catalog] = None
        # Location of the last catalog object applied; the joining FETCH
        # and the subscription can deliver the same or older ones late.
        self._catalog_at: Optional[tuple] = None
        self.tracks: Dict[str, LocTrackSubscriber] = {}
        self._have_catalog = asyncio.Event()

    def properties(self, name: str) -> Dict[int, Any]:
        """Merged Track Properties (0x0B unwrapped) of media track `name`;
        empty until its SUBSCRIBE_OK or PUBLISH arrives."""
        sub = self.tracks.get(name)
        return sub.properties if sub is not None else {}

    async def start(self, timeout: float = 10.0) -> Catalog:
        """Join the catalog track (SUBSCRIBE + joining FETCH, msf-01 §5
        — a late joiner needs the relay-cached complete catalog), await
        the first one, subscribe its media tracks. When the peer can't
        serve the fetch the subscription alone carries the catalog."""
        if self.discover:
            await self._resolve_namespace(timeout)
        self.session.on_fetch_object = self._on_catalog_fetch_object
        sub_msg, fetch_msg = await self.session.join(
            self.namespace, CATALOG_TRACK_NAME, joining_start=0,
            on_object=self._on_catalog_object)
        sub_ok = await self.session._await_response(sub_msg.request_id)
        alias = getattr(sub_ok, 'track_alias', None)
        if alias is not None:
            self.session.register_object_handler(
                alias, self._on_catalog_object)
        try:
            # From d20 the fill rides the SUBSCRIBE, already answered.
            if fetch_msg.request_id != sub_msg.request_id:
                await self.session._await_response(fetch_msg.request_id)
        except MOQTRequestError as e:
            # Resubscribing would be a duplicate subscription (§5.1).
            logger.warning(
                f"MediaSubscriber: catalog joining FETCH refused ({e}); "
                f"keeping the subscription. msf-01 §5 requires the fetch, "
                f"and without it only a catalog still being republished "
                f"will arrive — a late joiner to a settled broadcast gets "
                f"nothing")
        await asyncio.wait_for(self._have_catalog.wait(), timeout)
        await self._subscribe_media()
        return self.catalog

    async def _resolve_namespace(self, timeout: float) -> None:
        """Resolve `self.namespace` from a prefix to the namespace being
        published under it.

        Two-level discovery only (d18): SUBSCRIBE_NAMESPACE reports the
        NAMESPACEs under a prefix. Before that a prefix subscription is
        answered with a PUBLISH per track instead, which names no
        namespace, so the prefix is left as given.
        """
        if not self.session._profile.two_level_discovery:
            logger.info(f"MediaSubscriber: draft "
                        f"{self.session.negotiated_draft} has no namespace "
                        f"discovery; using '{self.namespace}' as given")
            return
        await self.session.subscribe_namespace(
            namespace_prefix=self.namespace, wait_response=True)
        ns_msg = await self.session.await_namespace(timeout=timeout)
        suffix = tuple(p.decode() if isinstance(p, bytes) else p
                       for p in (ns_msg.namespace_suffix or ()))
        full = '/'.join(p for p in (self.namespace, *suffix) if p)
        if full != self.namespace:
            logger.info(f"MediaSubscriber: discovered '{full}' under "
                        f"prefix '{self.namespace}'")
        self.namespace = full

    def _on_catalog_fetch_object(self, msg, size, ts,
                                 request_id) -> None:
        self._on_catalog_object(msg, size, ts, None, None)

    def _on_catalog_object(self, msg, size, ts, group_id,
                           subgroup_id) -> None:
        # End-of-group / end-of-track markers carry no catalog.
        if getattr(msg, "status", None) not in (None, ObjectStatus.NORMAL):
            return
        at = (msg.group_id if group_id is None else group_id, msg.object_id)
        if self._catalog_at is not None and at <= self._catalog_at:
            return
        try:
            update = Catalog.from_json(bytes(msg.payload).decode())
        except Exception as e:
            logger.error(f"MediaSubscriber: bad catalog object: {e}")
            return
        if update.is_delta:
            if self.catalog is None:
                logger.warning("MediaSubscriber: delta before any "
                               "independent catalog — dropped")
                return
            self.catalog.apply(update)
        else:
            self.catalog = update
        self._catalog_at = at
        if self.on_catalog:
            self.on_catalog(self.catalog)
        self._have_catalog.set()

    def _locmaf_init(self, entry) -> Optional[bytes]:
        """The CMAF header of a LOCMAF track this subscriber can decode,
        else None: a track of an unsupported locmafVersion must not be
        subscribed."""
        if entry.locmafVersion not in SUPPORTED_LOCMAF_VERSIONS:
            logger.warning(f"MediaSubscriber: skipping {entry.name!r} "
                           f"(locmafVersion={entry.locmafVersion!r})")
            return None
        try:
            init = self.catalog.resolve_init(entry)
        except CatalogError as e:
            init = None
            logger.warning(f"MediaSubscriber: {entry.name!r}: {e}")
        if init is None:
            logger.warning(f"MediaSubscriber: skipping {entry.name!r} "
                           f"(locmaf track without an init)")
            return None
        try:
            TrackContext.from_init(init)
        except LocmafError as e:
            logger.warning(f"MediaSubscriber: skipping {entry.name!r}: {e}")
            return None
        return init

    async def _subscribe_media(self) -> None:
        for entry in list(self.catalog.tracks):
            await self.subscribe_track(entry)

    async def subscribe_track(self, entry) -> Optional[LocTrackSubscriber]:
        """Subscribe one catalog track, as start() does each: None when
        it is not media this subscriber reads, already subscribed, or
        refused by `track_filter`."""
        ts = None
        if entry.packaging == PACKAGING_MPEG2TS:
            try:
                ts = MsftsTrack.from_entry(entry)
            except MsftsError as e:
                logger.warning(f"MediaSubscriber: skipping {e}")
                return None
        elif entry.packaging not in MEDIA_PACKAGINGS:
            logger.info(f"MediaSubscriber: skipping {entry.name!r} "
                        f"(packaging={entry.packaging!r})")
            return None
        if entry.name in self.tracks:
            return None
        init = None
        if entry.packaging == PACKAGING_LOCMAF:
            init = self._locmaf_init(entry)
            if init is None:
                return None
        if not self.track_filter(entry):
            return None
        name = entry.name

        def on_frame(f, gid, oid, _n=name):
            return self.on_frame and self.on_frame(_n, f, gid, oid)

        def on_arrival(m, rx, gid, sg, f, _n=name):
            return self.on_arrival and self.on_arrival(_n, m, rx, gid,
                                                       sg, f)
        callbacks = dict(on_frame=on_frame, on_arrival=on_arrival,
                         timestamp_errors=self.timestamp_errors)
        namespace = entry.namespace or self.namespace
        if ts is not None:
            sub = MsftsTrackSubscriber(
                self.session, namespace, name, packet_size=ts.packet_size,
                on_frame=on_frame, on_arrival=on_arrival,
                timestamp_errors=self.timestamp_errors,
                repair_s=self.repair_s)
        elif init is not None:
            sub = LocmafTrackSubscriber(self.session, namespace, name,
                                        init=init, **callbacks)
        else:
            sub = LocTrackSubscriber(self.session, namespace, name,
                                     **callbacks)
        if entry.packaging == PACKAGING_LOC:
            # cmaf init is a CMAF header consumed by the sink, not
            # decoder extradata — leave config unset there.
            init = self.catalog.resolve_init(entry)
            if init is not None:
                sub.set_config(init)
        # Registered first: its objects can arrive before subscribe()
        # returns.
        self.tracks[name] = sub
        try:
            await sub.subscribe()
        except BaseException:
            self.tracks.pop(name, None)
            raise
        return sub
