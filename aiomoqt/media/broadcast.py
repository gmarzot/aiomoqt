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
from typing import Callable, Dict, Optional

from ..messages import FetchHeader, FetchObject, SubgroupHeader
from ..track import PublishedTrack, SubscribedTrack
from ..types import MOQTMessageType, ObjectStatus
from ..utils.logger import get_logger
from .catalog import (
    Catalog, CATALOG_TRACK_NAME, PACKAGING_CMAF, PACKAGING_LOC,
)
from .loc import LocTrackPublisher, LocTrackSubscriber

logger = get_logger(__name__)

# §5.2: the catalog track SHOULD outrank every track it describes.
# MoQT priority is ascending-urgency, so this sits below the
# PublishedTrack default of 128 that the media tracks take.
CATALOG_PUBLISHER_PRIORITY = 64


class CatalogTrackPublisher(PublishedTrack):
    """Publishes the "catalog" track: the current independent catalog
    opens each group; queued deltas follow as Objects >= 1."""

    def __init__(self, session, namespace: str, catalog: Catalog):
        super().__init__(session, namespace, CATALOG_TRACK_NAME,
                         priority=CATALOG_PUBLISHER_PRIORITY)
        self.catalog = catalog
        # The catalog track has content by construction: the current
        # independent catalog is emitted as (0, 0) on first demand, so
        # SUBSCRIBE_OK must say ContentExists even before generation.
        self._largest = (0, 0)
        self._updates: asyncio.Queue = asyncio.Queue()

    async def publish_catalog(self, catalog: Catalog) -> None:
        """Queue a new independent catalog (starts a new group)."""
        await self._updates.put(catalog)

    async def publish_delta(self, delta: Catalog) -> None:
        """Queue a delta update (next object in the current group)."""
        await self._updates.put(delta)

    async def finish(self) -> None:
        await self._updates.put(None)

    async def generate(self, session, track_alias: int):
        prof = session._profile
        header = None
        stream_id = None

        async def _emit(payload: bytes, new_group: bool):
            nonlocal header, stream_id
            if new_group:
                if stream_id is not None:
                    buf = header.end_group(object_id=header.next_object_id)
                    session.stream_write(stream_id, buf.data,
                                         end_stream=True)
                stream_id = await session.open_uni_stream()
                self._stream_count += 1
                header = SubgroupHeader(
                    track_alias=track_alias,
                    group_id=0 if header is None else header.group_id + 1,
                    subgroup_id=0, publisher_priority=self.priority,
                    prof=prof)
                session.stream_write(stream_id, header.serialize().data)
            buf = header.next_object(payload=payload)
            await session.stream_write_drain(stream_id, buf.data)
            self._largest = (header.group_id, header._last_object_id)
            self._total_sent += 1

        # A joining subscriber needs a complete catalog first (§11.2).
        await _emit(self.catalog.to_json().encode(), new_group=True)
        try:
            while True:
                update = await self._updates.get()
                if update is None:
                    break
                await _emit(update.to_json().encode(),
                            new_group=not update.is_delta)
                # self.catalog tracks what has been emitted, so a group
                # start always opens with every prior delta folded in.
                if update.is_delta:
                    self.catalog.apply(update)
                else:
                    self.catalog = update
        finally:
            if stream_id is not None:
                buf = header.end_group(object_id=header.next_object_id)
                session.stream_write(stream_id, buf.data, end_stream=True)
        self._send_publish_done(session)


class MediaPublisher:
    """Publishes an MSF broadcast: catalog + LOC media tracks on one
    session, with control demux by track name / request id."""

    def __init__(self, session, namespace: str, catalog: Catalog):
        self.session = session
        self.namespace = namespace
        self.catalog_track = CatalogTrackPublisher(session, namespace,
                                                   catalog)
        self._by_name: Dict[str, PublishedTrack] = {
            CATALOG_TRACK_NAME: self.catalog_track}

    def add_track(self, track: LocTrackPublisher) -> LocTrackPublisher:
        self._by_name[track.trackname] = track
        return track

    async def start(self) -> None:
        """PUBLISH every track (catalog first, §11.2), then install the
        session-level demux over PublishedTrack's per-track handlers.

        forward=1: a live broadcast generates immediately — waiting for
        a subscriber to flip the forward state leaves the session idle
        and relays drop it."""
        for track in self._by_name.values():
            await track.publish(publish_track=True, forward=1)
        self.session.register_handler(
            MOQTMessageType.SUBSCRIBE, self._demux_subscribe)
        self.session.register_handler(
            MOQTMessageType.PUBLISH_OK, self._demux_publish_ok)
        self.session.register_handler(
            MOQTMessageType.SUBSCRIBE_UPDATE, self._demux_update)
        self.session.register_handler(
            MOQTMessageType.FETCH, self._demux_fetch)

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
            return
        await track._on_subscribe(session, msg)

    async def _demux_publish_ok(self, session, msg) -> None:
        track = self._track_for_request(msg.request_id)
        if track is not None:
            await track._on_publish_ok(session, msg)

    async def _demux_fetch(self, session, msg) -> None:
        """Serve a joining/absolute FETCH of the catalog track with the
        current complete catalog (a joining subscriber's first need,
        msf-01 §5); other fetches keep the default bare FETCH_OK."""
        track = None
        name = msg.track_name
        if name is not None:
            name = name.decode() if isinstance(name, bytes) else name
            track = self._by_name.get(name)
        if track is None and msg.joining_request_id is not None:
            track = self._track_for_request(msg.joining_request_id)
        session.fetch_ok(request_id=msg.request_id)
        if track is not self.catalog_track:
            return
        sid = await session.open_uni_stream()
        prof = session._profile
        session.stream_write(
            sid,
            FetchHeader(request_id=msg.request_id).serialize(prof).data)
        obj = FetchObject(
            payload=self.catalog_track.catalog.to_json().encode())
        session.stream_write(sid, obj.serialize(prof=prof).data,
                             end_stream=True)

    async def _demux_update(self, session, msg) -> None:
        from ..messages import RequestUpdate
        # d16 REQUEST_UPDATE references the original request via
        # existing_request_id (its request_id is the update's own);
        # d18 dropped that field — its request_id IS the reference —
        # and d14 SUBSCRIBE_UPDATE keys on request_id directly.
        rid = (msg.existing_request_id
               if isinstance(msg, RequestUpdate)
               and msg.existing_request_id is not None
               else msg.request_id)
        track = self._track_for_request(rid)
        if track is None:
            logger.warning(f"MediaPublisher: update for unknown "
                           f"request_id={rid}")
            return
        if isinstance(msg, RequestUpdate):
            await track._on_request_update(session, msg)
        else:
            await track._on_subscribe_update(session, msg)


class MediaSubscriber:
    """Consumes an MSF broadcast: reads the catalog track, then
    subscribes every LOC media track it describes.

    on_frame(track_name, frame, group_id, object_id) receives media;
    on_catalog(catalog) fires on every independent catalog or applied
    delta.
    """

    def __init__(self, session, namespace: str, *,
                 on_frame: Optional[Callable] = None,
                 on_catalog: Optional[Callable] = None,
                 track_filter: Optional[Callable] = None):
        self.session = session
        self.namespace = namespace
        self.on_frame = on_frame
        self.on_catalog = on_catalog
        self.track_filter = track_filter or (lambda t: True)
        self.catalog: Optional[Catalog] = None
        self.tracks: Dict[str, LocTrackSubscriber] = {}
        self._have_catalog = asyncio.Event()
        self._catalog_sub: Optional[SubscribedTrack] = None

    async def start(self, timeout: float = 10.0) -> Catalog:
        """Join the catalog track (SUBSCRIBE + joining FETCH, msf-01 §5
        — a late joiner needs the relay-cached complete catalog), await
        the first one, subscribe its media tracks. Falls back to plain
        SUBSCRIBE when the peer can't serve the fetch."""
        self.session.on_fetch_object = self._on_catalog_fetch_object
        # Global fallback catches catalog objects that arrive before the
        # per-alias registration (§10.4.2 data-before-OK race).
        self.session.on_object_received = self._on_catalog_object
        try:
            sub_ok, _fetch_ok = await self.session.join(
                self.namespace, CATALOG_TRACK_NAME, joining_start=0,
                wait_response=True)
            alias = getattr(sub_ok, 'track_alias', None)
            if alias is not None:
                self.session.register_object_handler(
                    alias, self._on_catalog_object)
        except Exception as e:
            logger.info(f"MediaSubscriber: catalog join failed ({e}); "
                        f"falling back to plain subscribe")
            self._catalog_sub = SubscribedTrack(
                self.session, self.namespace, CATALOG_TRACK_NAME,
                on_object=self._on_catalog_object)
            await self._catalog_sub.subscribe()
        await asyncio.wait_for(self._have_catalog.wait(), timeout)
        await self._subscribe_media()
        return self.catalog

    def _on_catalog_fetch_object(self, msg, size, ts,
                                 request_id) -> None:
        self._on_catalog_object(msg, size, ts, None, None)

    def _on_catalog_object(self, msg, size, ts, group_id,
                           subgroup_id) -> None:
        # End-of-group / end-of-track markers carry no catalog.
        if getattr(msg, "status", None) not in (None, ObjectStatus.NORMAL):
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
        if self.on_catalog:
            self.on_catalog(self.catalog)
        self._have_catalog.set()

    async def _subscribe_media(self) -> None:
        for entry in list(self.catalog.tracks):
            if entry.packaging not in (PACKAGING_LOC, PACKAGING_CMAF):
                logger.info(f"MediaSubscriber: skipping {entry.name!r} "
                            f"(packaging={entry.packaging!r})")
                continue
            if entry.name in self.tracks or not self.track_filter(entry):
                continue
            name = entry.name
            sub = LocTrackSubscriber(
                self.session,
                entry.namespace or self.namespace, name,
                on_frame=(lambda f, gid, oid, _n=name:
                          self.on_frame and self.on_frame(_n, f, gid, oid)))
            if entry.packaging == PACKAGING_LOC:
                # cmaf init is a CMAF header consumed by the sink, not
                # decoder extradata — leave config unset there.
                init = self.catalog.resolve_init(entry)
                if init is not None:
                    sub.set_config(init)
            await sub.subscribe()
            self.tracks[name] = sub
