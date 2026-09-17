"""One MSF broadcast on several relays.

Composition only: a MediaPublisher per session for control demux, the
same track objects registered in each. Fan-out itself is in the track
and delivery layers.
"""
from __future__ import annotations

import asyncio
from typing import Dict, List

from .media.broadcast import CatalogTrackPublisher, MediaPublisher
from .media.catalog import Catalog
from .track import PublishedTrack, TrackState


class _FanoutCatalog:
    """The catalog track across sessions.

    Unlike a media track this is one publisher per session rather than
    one shared: every catalog object is a complete catalog, so nothing
    depends on the group numbers agreeing between relays.
    """

    def __init__(self, members: List[CatalogTrackPublisher]):
        self.members = list(members)

    @property
    def state(self) -> TrackState:
        if any(m.state == TrackState.SUBSCRIBED for m in self.members):
            return TrackState.SUBSCRIBED
        return self.members[0].state if self.members else TrackState.IDLE

    @property
    def catalog(self) -> Catalog:
        return self.members[0].catalog

    async def publish_catalog(self, catalog: Catalog) -> None:
        for m in self.members:
            await m.publish_catalog(catalog)

    async def publish_delta(self, delta: Catalog) -> None:
        for m in self.members:
            await m.publish_delta(delta)

    async def finish(self) -> None:
        for m in self.members:
            await m.finish()

    def drop_session(self, session) -> None:
        self.members = [m for m in self.members if m.session is not session]


class FanoutPublisher:
    """MediaPublisher over several sessions: one namespace, one catalog,
    one set of tracks, every relay fed from the same frames."""

    def __init__(self, sessions, namespace: str, catalog: Catalog):
        self.sessions = list(sessions)
        self.namespace = namespace
        self.publishers = [MediaPublisher(s, namespace, catalog)
                           for s in self.sessions]
        self.catalog_track = _FanoutCatalog(
            [p.catalog_track for p in self.publishers])
        self.tracks: Dict[str, PublishedTrack] = {}

    @property
    def session(self):
        """The session tracks are built against; the rest are added."""
        return self.sessions[0]

    def add_track(self, track: PublishedTrack) -> PublishedTrack:
        """One track object serving every session. It holds a
        subscription per peer and numbers its objects once for all of
        them, so the same content carries the same ids on every relay."""
        for session in self.sessions[1:]:
            track.add_session(session)
        for pub in self.publishers:
            pub.add_track(track)
        self.tracks[track.trackname] = track
        return track

    async def start(self, **kw) -> None:
        await asyncio.gather(*(p.start(**kw) for p in self.publishers))

    def drop(self, session) -> None:
        """Forget a relay whose session is gone; the rest carry on."""
        self.catalog_track.drop_session(session)
        for track in self.tracks.values():
            track.drop_session(session)
        self.publishers = [p for p in self.publishers
                           if p.session is not session]
