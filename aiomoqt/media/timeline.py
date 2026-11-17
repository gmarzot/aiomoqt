"""MSF media timeline track (draft-ietf-moq-msf-01 §7): records of
[media ms, [group, object], wall-clock ms], each group's first object
holding every record still in the accessible window (§7.3)."""
from __future__ import annotations

import json
from collections import deque
from typing import Deque, List

from .catalog import CatalogTrack, PACKAGING_MEDIA_TIMELINE
from .loc import LocTrackPublisher

# Records kept, by wall clock, as moq2ts's --accessible-window default.
ACCESSIBLE_WINDOW_S = 300
MIME_TYPE = "application/json"


def timeline_name(track: str) -> str:
    """moq2ts's name for a track's timeline."""
    return f"{track}.timeline"


def timeline_track(name: str, depends: List[str]) -> CatalogTrack:
    """The catalog entry of a live media timeline track (§7.2)."""
    return CatalogTrack(name=name, packaging=PACKAGING_MEDIA_TIMELINE,
                        isLive=True, depends=list(depends),
                        mimeType=MIME_TYPE)


class MediaTimeline:
    """The records of one media track, `window_s` of them by wall clock."""

    def __init__(self, window_s: float = ACCESSIBLE_WINDOW_S):
        self.window_ms = int(window_s * 1000)
        self.records: Deque[list] = deque()

    def add(self, media_ms: int, group: int, obj: int,
            wall_ms: int) -> bytes:
        """Add a record; the independent timeline that carries it."""
        self.records.append([media_ms, [group, obj], wall_ms])
        while (len(self.records) > 1
               and self.records[0][2] < wall_ms - self.window_ms):
            self.records.popleft()
        return json.dumps(list(self.records),
                          separators=(',', ':')).encode()


class MediaTimelinePublisher(LocTrackPublisher):
    """Publishes a media timeline track: send_frame() with the payload
    MediaTimeline.add() returns, one object a group; no properties."""

    def __init__(self, session, namespace: str, trackname: str, *,
                 fetch_history_bytes: int = 0, priority: int = 128):
        super().__init__(session, namespace, trackname, config_property=None,
                         media_kind='timeline', queue_size=64,
                         priority=priority,
                         fetch_history_bytes=fetch_history_bytes)

    def _timestamp_ids(self, session) -> tuple:
        return ()
