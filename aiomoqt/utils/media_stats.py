"""Decoder-free delivery analysis of a live media subscription.

Measures per track, from the wire alone, whether a subscription was
delivered well enough to play: latency, RFC 3550 jitter, continuity,
group integrity, keyframe cost, bitrate, and a pseudo-playout model that
counts objects a player would have had too late.

Clocks: latency is receive wall clock minus the publisher's timestamp,
so it carries the clock offset between two machines and is reported only
for wall-clock timestamps. Jitter, the playout model, keyframe cost and
cross-track skew are built from differences, in which a constant offset
cancels.

Sans-I/O: feed on_arrival() and pass the current time to interval() and
summary(); nothing here reads a clock.
"""
import statistics
from collections import deque
from typing import Dict, List, Optional, Tuple

from ..media.cmaf import chunk_decode_time
from ..types import ObjectStatus
from .format import fmt_bps
from .stats import LOC_TIMESCALE, _pct, _Reservoir

DEFAULT_CUSHION_MS = 500
PRIOR_GROUP_ID_GAP = 0x3C

# A µs timestamp within a day of the receive clock is wall-clock.
_WALL_WINDOW_US = 86_400_000_000
# Missing ids tracked individually per gap; the excess is lost outright.
_HOLE_CAP = 4096
# Arrival history kept for the underrun test.
_ARRIVAL_KEEP_US = 60_000_000
_SCAN_EVERY_US = 100_000
# Window over which the fastest delivery sets the playout target.
_BASE_WINDOW_US = 10_000_000


def wall_clock_us(ts: Optional[int], timescale: Optional[int],
                  recv_us: int) -> Optional[int]:
    """`ts` as µs since the epoch when it is a wall-clock stamp: µs
    units (no TIMESCALE, or 1e6) within a day of `recv_us`. None for
    media time."""
    if ts is None or timescale not in (None, 1_000_000):
        return None
    return ts if abs(recv_us - ts) <= _WALL_WINDOW_US else None


def _med(xs) -> Optional[float]:
    return statistics.median(xs) if xs else None


class _Group:
    __slots__ = ('seen', 'max_oid', 'holes', 'ended', 'eog_oid',
                 'last_rx', 'joined', 'final')

    def __init__(self, joined: bool, now: int):
        self.seen: set = set()
        self.max_oid: Optional[int] = None
        self.holes: Dict[int, list] = {}   # oid -> [detect_us, lost]
        self.ended = False
        self.eog_oid: Optional[int] = None  # first id that does not exist
        self.last_rx = now
        self.joined = joined     # subscription began inside this group
        self.final = False


class TrackAnalysis:
    """One track's accounting. `cushion_ms` is the playout margin over
    the fastest recent delivery, and also the settle horizon: a missing
    object still absent after it is lost, and one that turns up later
    reverses that loss."""

    _COUNTERS = ('objects', 'bytes', 'groups', 'lost', 'lost_groups',
                 'reorders', 'dups', 'late', 'underruns', 'stall_us')

    def __init__(self, name: str, role: Optional[str] = None,
                 timescale: Optional[int] = None, cmaf: bool = False,
                 cushion_ms: float = DEFAULT_CUSHION_MS):
        self.name = name
        self.role = role
        self.timescale = timescale     # CMAF media timescale (tfdt units)
        self.cmaf = cmaf
        self.cushion_ms = cushion_ms
        self._cushion_us = int(cushion_ms * 1000)
        self.wall: Optional[bool] = None
        self.timed = False

        self.objects = 0
        self.bytes = 0
        self.groups = 0
        self.dups = 0
        self.lost = 0
        self.pending = 0
        self.reorders = 0
        self.late_reorders = 0
        self.lost_groups = 0
        self.partial_groups = 0
        self.missing_eog = 0
        self.pre_anchor = 0
        self.late = 0
        self.underruns = 0
        self.stall_us = 0
        self.ended = False
        self.first_rx: Optional[int] = None
        self.last_rx: Optional[int] = None

        self._groups: Dict[int, _Group] = {}
        self._max_gid: Optional[int] = None
        self._gaps: Dict[int, list] = {}    # missing gid -> [detect, lost]
        self._holes_q: deque = deque()      # (detect_us, gid, oid)
        self._gaps_q: deque = deque()       # (detect_us, gid)
        self._last_scan = 0

        self.jitter_ms = 0.0
        self.jitter_max_ms = 0.0
        self._prev_rm: Optional[Tuple[int, int]] = None

        self._lat = _Reservoir(10000)
        self.lat_max: Optional[float] = None
        self._settle = _Reservoir(1000)
        self.settle_max: Optional[float] = None
        self._gop = _Reservoir(1000)
        self._kf_int = _Reservoir(1000)
        self._prev_key: Optional[Tuple[int, int]] = None   # (gid, media)
        self._transit_key = _Reservoir(2000)
        self._transit_p = _Reservoir(2000)
        self._key_bytes = self._key_n = self._p_bytes = self._p_n = 0
        self._buf = _Reservoir(10000)
        self.buf_min: Optional[float] = None

        self._keyed = False
        self._base: deque = deque()       # (recv_us, transit), rising
        self._stall_end = 0
        self._max_m: Optional[int] = None
        self._arrivals: deque = deque()   # (recv_us, max media so far)

        self._iv_base = self._counters()
        self._iv_lat: List[float] = []
        self._iv_buf: List[float] = []

    def _counters(self) -> dict:
        return {k: getattr(self, k) for k in self._COUNTERS}

    # -- ingest ------------------------------------------------------

    def observe(self, gid: int, oid: int, size: int, recv_us: int,
                ts: Optional[int] = None,
                timescale: Optional[int] = None, *,
                ends_group: bool = False, last_in_group: bool = False,
                prior_gap: int = 0) -> None:
        """One media object. `ts` is in `timescale` units (None = µs).
        ends_group: its stream's FIN ends the group (header bit);
        last_in_group: it is the group's final object (datagram bit);
        prior_gap: the Prior Group ID Gap property."""
        self._tick(recv_us)
        if self.first_rx is None:
            self.first_rx = recv_us
        self.last_rx = recv_us
        if not self._continuity(gid, oid, recv_us):
            return                          # duplicate
        if prior_gap:
            self._declare_absent(gid, prior_gap)
        self.objects += 1
        self.bytes += size
        g = self._groups.get(gid)
        if g is not None and (ends_group or last_in_group):
            g.ended = True
            if last_in_group:
                g.eog_oid = oid + 1
        if oid == 0:
            self._key_bytes += size
            self._key_n += 1
        else:
            self._p_bytes += size
            self._p_n += 1
        if ts is None:
            return
        self.timed = True
        m_us = (ts if timescale in (None, 1_000_000)
                else ts * 1_000_000 // timescale)
        if self.wall is None:
            self.wall = wall_clock_us(ts, timescale, recv_us) is not None
        if self.wall:
            lat = (recv_us - m_us) / 1000
            self._lat.add(lat)
            self._iv_lat.append(lat)
            self.lat_max = lat if self.lat_max is None else max(
                self.lat_max, lat)
        if self._prev_rm is not None:
            d = (recv_us - self._prev_rm[0]) - (m_us - self._prev_rm[1])
            self.jitter_ms += (abs(d) / 1000 - self.jitter_ms) / 16
            self.jitter_max_ms = max(self.jitter_max_ms, self.jitter_ms)
        self._prev_rm = (recv_us, m_us)
        transit = recv_us - m_us
        if oid == 0:
            self._transit_key.add(transit)
            if self._prev_key is not None and gid > self._prev_key[0]:
                span = m_us - self._prev_key[1]
                if span > 0:
                    self._kf_int.add(span / 1000 / (gid - self._prev_key[0]))
            self._prev_key = (gid, m_us)
        else:
            self._transit_p.add(transit)
        self._playout(oid, m_us, recv_us)

    def end_group(self, gid: int, eog_oid: int, recv_us: int) -> None:
        """END_OF_GROUP status: no object at or past `eog_oid` exists."""
        self._tick(recv_us)
        g = self._groups.get(gid)
        if g is None:
            if not self._open_group(gid, recv_us, eog_oid):
                return
            g = self._groups[gid]
        g.ended = True
        g.eog_oid = eog_oid
        g.last_rx = recv_us
        if g.max_oid is not None:
            self._add_holes(g, gid, g.max_oid + 1, eog_oid, recv_us)
        elif not g.joined:
            self._add_holes(g, gid, 0, eog_oid, recv_us)
        if eog_oid > 0:
            g.max_oid = max(g.max_oid if g.max_oid is not None else -1,
                            eog_oid - 1)

    # -- continuity --------------------------------------------------

    def _continuity(self, gid: int, oid: int, now: int) -> bool:
        """Account one object id; False for a duplicate."""
        g = self._groups.get(gid)
        if g is None:
            opened = self._open_group(gid, now, oid)
            if not opened:
                return True     # outside tracked range: count, don't judge
            if isinstance(opened, list):
                self._reordered(opened, now)
            g = self._groups[gid]
        g.last_rx = now
        if oid in g.seen:
            self.dups += 1
            return False
        g.seen.add(oid)
        h = g.holes.pop(oid, None)
        if h is not None:
            self._reordered(h, now)
            if h[1]:
                self.lost -= 1
            else:
                self.pending -= 1
        elif g.max_oid is None:
            if not g.joined:
                self._add_holes(g, gid, 0, oid, now)
            g.max_oid = oid
        elif oid > g.max_oid:
            self._add_holes(g, gid, g.max_oid + 1, oid, now)
            g.max_oid = oid
        # Below max_oid and never a hole: before the join point.
        return True

    def _reordered(self, miss: list, now: int) -> None:
        """A missing object or group arrived; `miss` is its [detect_us,
        declared_lost] record."""
        self.reorders += 1
        if miss[1]:
            self.late_reorders += 1
        settle = (now - miss[0]) / 1000
        self._settle.add(settle)
        self.settle_max = (settle if self.settle_max is None
                           else max(self.settle_max, settle))

    def _open_group(self, gid: int, now: int, oid: int):
        """Start tracking a group. False if it predates the subscription
        or has already been let go; the gap record if it fills a gap."""
        if self._max_gid is None:
            self._max_gid = gid
            self._new_group(gid, oid > 0, now)
            return True
        gap = self._gaps.pop(gid, None)
        if gap is not None:
            if gap[1]:
                self.lost_groups -= 1
            else:
                self.pending -= 1
            self._new_group(gid, False, now)
            return gap
        if gid <= self._max_gid:
            return False
        missing = gid - self._max_gid - 1
        tracked = min(missing, _HOLE_CAP)
        self.lost_groups += missing - tracked
        for x in range(gid - tracked, gid):
            self._gaps[x] = [now, False]
            self._gaps_q.append((now, x))
            self.pending += 1
        self._max_gid = gid
        self._new_group(gid, False, now)
        return True

    def _new_group(self, gid: int, joined: bool, now: int) -> None:
        self._groups[gid] = _Group(joined, now)
        self.groups += 1

    def _declare_absent(self, gid: int, n: int) -> None:
        """Prior Group ID Gap: the n groups before `gid` never exist."""
        for x in range(max(gid - n, 0), gid):
            gap = self._gaps.pop(x, None)
            if gap is None:
                continue
            if gap[1]:
                self.lost_groups -= 1
            else:
                self.pending -= 1

    def _add_holes(self, g: _Group, gid: int, lo: int, hi: int,
                   now: int) -> None:
        n = hi - lo
        if n <= 0:
            return
        tracked = min(n, _HOLE_CAP)
        self.lost += n - tracked
        for oid in range(hi - tracked, hi):
            if oid not in g.seen and oid not in g.holes:
                g.holes[oid] = [now, False]
                self._holes_q.append((now, gid, oid))
                self.pending += 1

    def _tick(self, now: int) -> None:
        """Declare overdue holes lost; finalize and evict old groups."""
        cut = now - self._cushion_us
        q = self._holes_q
        while q and q[0][0] <= cut:
            _, gid, oid = q.popleft()
            g = self._groups.get(gid)
            h = g.holes.get(oid) if g is not None else None
            if h is not None and not h[1]:
                h[1] = True
                self.pending -= 1
                self.lost += 1
        q = self._gaps_q
        while q and q[0][0] <= cut:
            _, gid = q.popleft()
            gap = self._gaps.get(gid)
            if gap is not None and not gap[1]:
                gap[1] = True
                self.pending -= 1
                self.lost_groups += 1
        if now - self._last_scan >= _SCAN_EVERY_US:
            self._finalize(now)

    def _finalize(self, now: int, force: bool = False) -> None:
        self._last_scan = now
        keep = 4 * self._cushion_us
        for gid in list(self._groups):
            g = self._groups[gid]
            newest = gid == self._max_gid
            if not g.final and not newest and (
                    force or now - g.last_rx > self._cushion_us):
                g.final = True
                self._judge(g)
            if g.final and now - g.last_rx > keep:
                del self._groups[gid]
        for gid in [x for x, gap in self._gaps.items()
                    if gap[1] and now - gap[0] > keep]:
            del self._gaps[gid]

    def _judge(self, g: _Group) -> None:
        if g.joined:
            return
        if g.holes:
            self.partial_groups += 1
        if not g.ended:
            self.missing_eog += 1
        n = (g.eog_oid if g.eog_oid is not None
             else (g.max_oid + 1 if g.max_oid is not None else 0))
        if n:
            self._gop.add(n)

    # -- playout -----------------------------------------------------

    def _playout(self, oid: int, m_us: int, recv_us: int) -> None:
        """A live player holding its target latency: the fastest transit
        in the last _BASE_WINDOW_US plus the cushion. Deadline = media
        time + target. Late: arrived after it. Underrun: late with nothing
        newer buffered by the deadline; late objects inside one starved
        stretch are one underrun. Playout starts at the first keyframe."""
        if not self._keyed:
            if oid != 0:
                self.pre_anchor += 1
                return
            self._keyed = True
        transit = recv_us - m_us
        q = self._base
        while q and q[-1][1] >= transit:
            q.pop()
        q.append((recv_us, transit))
        while q[0][0] < recv_us - _BASE_WINDOW_US:
            q.popleft()
        target = q[0][1] + self._cushion_us
        if self._max_m is not None:
            buf = max(0, self._max_m + target - recv_us) / 1000
            self._buf.add(buf)
            self._iv_buf.append(buf)
            self.buf_min = buf if self.buf_min is None else min(
                self.buf_min, buf)
        deadline = m_us + target
        if recv_us > deadline:
            self.late += 1
            if not self._newer_by(m_us, deadline):
                if deadline > self._stall_end:
                    self.underruns += 1
                    self.stall_us += recv_us - deadline
                else:
                    self.stall_us += max(0, recv_us - self._stall_end)
                self._stall_end = max(self._stall_end, recv_us)
        self._max_m = m_us if self._max_m is None else max(self._max_m,
                                                           m_us)
        self._arrivals.append((recv_us, self._max_m))
        while (len(self._arrivals) > 1
               and self._arrivals[0][0] < recv_us - _ARRIVAL_KEEP_US):
            self._arrivals.popleft()

    def _newer_by(self, m_us: int, t_us: int) -> bool:
        """Had an object newer than m_us arrived by t_us?"""
        for rx, mx in reversed(self._arrivals):
            if rx <= t_us:
                return mx > m_us
        return False

    # -- views -------------------------------------------------------

    def interval(self, now_us: int, dt_s: float) -> dict:
        self._tick(now_us)
        c = self._counters()
        d = {k: c[k] - self._iv_base[k] for k in c}
        self._iv_base = c
        lat, self._iv_lat = sorted(self._iv_lat), []
        buf, self._iv_buf = sorted(self._iv_buf), []
        dt_s = dt_s or 1e-9
        return dict(
            track=self.name, objects=d['objects'],
            obj_rate=d['objects'] / dt_s,
            bitrate_bps=d['bytes'] * 8 / dt_s,
            lat_p50_ms=_pct(lat, 50) if lat else None,
            lat_p95_ms=_pct(lat, 95) if lat else None,
            lat_max_ms=lat[-1] if lat else None,
            jitter_ms=(self.jitter_ms if self.timed and d['objects']
                       else None),
            lost=d['lost'], lost_groups=d['lost_groups'],
            pending=self.pending, reorders=d['reorders'], dups=d['dups'],
            late=d['late'], underruns=d['underruns'],
            stall_ms=d['stall_us'] / 1000,
            buf_min_ms=buf[0] if buf else None,
            buf_p50_ms=_pct(buf, 50) if buf else None,
            groups=d['groups'])

    def summary(self, now_us: int) -> dict:
        self._tick(now_us)
        self._finalize(now_us, force=True)
        active = ((self.last_rx - self.first_rx) / 1e6
                  if self.first_rx is not None else 0) or 1e-9
        lat_p50, lat_p95 = (self._lat.percentiles(50, 95)
                            if self._lat.items else (None, None))
        settle_p50, = (self._settle.percentiles(50)
                       if self._settle.items else (None,))
        buf_p50, = (self._buf.percentiles(50)
                    if self._buf.items else (None,))
        gop = self._gop.items
        multi = bool(gop) and max(gop) > 1
        kf = self._kf_int.items
        key_cost = ratio = None
        if multi and self._transit_key.items and self._transit_p.items:
            key_cost = (_med(self._transit_key.items)
                        - _med(self._transit_p.items)) / 1000
        if multi and self._key_n and self._p_n and self._p_bytes:
            ratio = ((self._key_bytes / self._key_n)
                     / (self._p_bytes / self._p_n))
        return dict(
            track=self.name, objects=self.objects,
            obj_rate=self.objects / active,
            bitrate_bps=self.bytes * 8 / active,
            lat_p50_ms=lat_p50, lat_p95_ms=lat_p95, lat_max_ms=self.lat_max,
            jitter_ms=self.jitter_ms if self.timed else None,
            jitter_max_ms=self.jitter_max_ms if self.timed else None,
            lost=self.lost, lost_groups=self.lost_groups,
            pending=self.pending, reorders=self.reorders,
            late_reorders=self.late_reorders,
            settle_p50_ms=settle_p50, settle_max_ms=self.settle_max,
            dups=self.dups, groups=self.groups,
            partial_groups=self.partial_groups,
            missing_eog=self.missing_eog,
            gop_p50=_med(gop) if gop else None,
            kf_int_ms=_med(kf) if multi and kf else None,
            kf_int_sd_ms=(statistics.pstdev(kf)
                          if multi and len(kf) > 1 else None),
            kf_int_min_ms=min(kf) if multi and kf else None,
            kf_int_max_ms=max(kf) if multi and kf else None,
            key_p_ratio=ratio, key_cost_ms=key_cost,
            cushion_ms=self.cushion_ms, late=self.late,
            underruns=self.underruns, stall_ms=self.stall_us / 1000,
            buf_min_ms=self.buf_min, buf_p50_ms=buf_p50,
            pre_anchor=self.pre_anchor, wall=self.wall, timed=self.timed)


CSV_FIELDS = (
    'kind', 't0_s', 't1_s', 'track', 'objects', 'obj_rate', 'bitrate_bps',
    'lat_p50_ms', 'lat_p95_ms', 'lat_max_ms', 'jitter_ms', 'lost',
    'lost_groups', 'pending', 'reorders', 'dups', 'late', 'underruns',
    'stall_ms', 'buf_min_ms', 'buf_p50_ms', 'groups', 'partial_groups',
    'missing_eog', 'settle_p50_ms', 'gop_p50', 'kf_int_ms', 'kf_int_sd_ms',
    'kf_int_min_ms', 'kf_int_max_ms', 'key_p_ratio', 'key_cost_ms',
    'skew_ms',
)


class MediaAnalysis:
    """All tracks of one subscription plus cross-track delivery skew:
    median latency of the first video track minus that of the first
    audio track, when both carry wall-clock timestamps. `cushion_ms`
    overrides every track's catalog targetLatency."""

    def __init__(self, cushion_ms: Optional[float] = None):
        self.cushion_ms = cushion_ms
        self.tracks: Dict[str, TrackAnalysis] = {}
        self._t0: Optional[int] = None
        self._iv_t: Optional[int] = None
        self._skews: List[float] = []

    def add_track(self, name: str, *, role: Optional[str] = None,
                  timescale: Optional[int] = None, cmaf: bool = False,
                  target_latency_ms: Optional[float] = None
                  ) -> TrackAnalysis:
        cushion = (self.cushion_ms if self.cushion_ms is not None
                   else target_latency_ms or DEFAULT_CUSHION_MS)
        t = TrackAnalysis(name, role, timescale, cmaf, cushion)
        self.tracks[name] = t
        return t

    def start(self, now_us: int) -> None:
        self._t0 = self._iv_t = now_us

    def on_arrival(self, name: str, msg, recv_us: int, group_id: int,
                   subgroup_id: Optional[int], frame) -> None:
        """MediaSubscriber.on_arrival sink."""
        t = self.tracks.get(name) or self.add_track(name)
        status = getattr(msg, 'status', None)
        if status == ObjectStatus.END_OF_GROUP:
            t.end_group(group_id, msg.object_id, recv_us)
            return
        if status == ObjectStatus.END_OF_TRACK:
            t.ended = True
            return
        if frame is None:
            return
        exts = getattr(msg, 'extensions', None) or {}
        ts, scale = frame.timestamp, exts.get(LOC_TIMESCALE)
        if ts is None and t.cmaf:
            ts, scale = chunk_decode_time(frame.payload), t.timescale
        if subgroup_id is None:
            ends, last = False, bool(getattr(msg, 'end_of_group', False))
        else:
            flags = getattr(msg, 'stream_flags', None)
            ends, last = bool(flags and flags[1]), False
        t.observe(group_id, msg.object_id, len(frame.payload), recv_us,
                  ts, scale, ends_group=ends, last_in_group=last,
                  prior_gap=exts.get(PRIOR_GROUP_ID_GAP, 0) or 0)

    def _pair(self) -> Tuple[Optional[TrackAnalysis],
                             Optional[TrackAnalysis]]:
        video = next((t for t in self.tracks.values()
                      if t.role == 'video'), None)
        audio = next((t for t in self.tracks.values()
                      if t.role == 'audio'), None)
        return video, audio

    def interval(self, now_us: int
                 ) -> Tuple[float, float, List[dict], Optional[float]]:
        """(t0_s, t1_s, per-track rows, skew_ms) since the last call."""
        if self._t0 is None:
            self.start(now_us)
        dt = (now_us - self._iv_t) / 1e6
        t0 = (self._iv_t - self._t0) / 1e6
        self._iv_t = now_us
        rows = {n: t.interval(now_us, dt) for n, t in self.tracks.items()}
        skew = None
        video, audio = self._pair()
        if video is not None and audio is not None:
            v = rows[video.name]['lat_p50_ms']
            a = rows[audio.name]['lat_p50_ms']
            if v is not None and a is not None:
                skew = v - a
                self._skews.append(skew)
            rows[video.name]['skew_ms'] = skew
        return t0, t0 + dt, list(rows.values()), skew

    def summary(self, now_us: int) -> Tuple[List[dict], dict]:
        """(per-track rows, skew dict with p50_ms / max_ms / n). The
        video row of the pair carries the skew p50 as skew_ms."""
        rows = {n: t.summary(now_us) for n, t in self.tracks.items()}
        s = sorted(self._skews)
        skew = dict(p50_ms=_pct(s, 50) if s else None,
                    max_ms=max(s, key=abs) if s else None, n=len(s))
        video, audio = self._pair()
        if video is not None and audio is not None:
            rows[video.name]['skew_ms'] = skew['p50_ms']
        return list(rows.values()), skew

    def silent_tracks(self) -> List[str]:
        return [n for n, t in self.tracks.items() if t.objects == 0]


# -- rendering ---------------------------------------------------------

# One number per cell, times in ms: '-' is a zero count, blank is n/a.
LEGEND = ("times in ms; '-' = none; blank = n/a. Latency includes any "
          "clock offset between publisher and subscriber.")

_IV_COLS = (('Obj/s', 6), ('Bitrate', 9), ('Lat50', 7), ('Lat95', 6),
            ('LatMax', 7), ('Jitter', 7), ('BufMin', 8), ('Buf50', 6),
            ('Lost', 7), ('Reord', 6), ('Dup', 5), ('Late', 5),
            ('Under', 6), ('AVskew', 8))
_SUM_COLS = (('Objects', 8), ('Groups', 7), ('GOP', 5), ('KeyInt', 7),
             ('Key/P', 6), ('KeyCost', 8), ('Partial', 8), ('NoEOG', 6),
             ('Settle', 7), ('Stall', 6), ('Cushion', 8), ('PreKey', 7))


def _n(v: Optional[float]) -> str:
    """A measured value: one decimal below 10, blank when unavailable."""
    if v is None:
        return ''
    a = abs(v)
    s = f"{a:.1f}" if a < 10 else f"{a:.0f}"
    if s == '0.0':
        return '0'
    return '-' + s if v < 0 else s


def _c(v) -> str:
    """A count: '-' when zero."""
    return '-' if not v else _n(v) if isinstance(v, float) else str(v)


def _cells(values, cols) -> str:
    return ''.join(f"{v:>{w}}" for v, (_, w) in zip(values, cols)).rstrip()


def _track_width(names) -> int:
    return max([5] + [len(n) for n in names]) + 2


def interval_header(names) -> str:
    """Column header for format_interval() rows of these tracks."""
    tw = _track_width(names)
    return (f"  {'Interval':<10}{'Track':<{tw}}"
            + _cells([c for c, _ in _IV_COLS], _IV_COLS))


def _iv_line(label: str, tw: int, r: dict) -> str:
    g = r['lost_groups']
    lost = f"{r['lost']}+{g}g" if g else _c(r['lost'])
    values = (
        _c(r['obj_rate']),
        fmt_bps(r['bitrate_bps']) if r['bitrate_bps'] else '-',
        _n(r['lat_p50_ms']), _n(r['lat_p95_ms']), _n(r['lat_max_ms']),
        _n(r['jitter_ms']), _n(r['buf_min_ms']), _n(r['buf_p50_ms']),
        lost, _c(r['reorders']), _c(r['dups']), _c(r['late']),
        _c(r['underruns']), _n(r.get('skew_ms')))
    return f"  {label:<10}{r['track']:<{tw}}" + _cells(values, _IV_COLS)


def format_interval(t0: float, t1: float, rows: List[dict]) -> List[str]:
    tw = _track_width(r['track'] for r in rows)
    label = f"{t0:.0f}-{t1:.0f}s"
    return [_iv_line(label if i == 0 else '', tw, r)
            for i, r in enumerate(rows)]


def _notes(r: dict) -> List[str]:
    name, out = r['track'], []
    if not r['timed']:
        out.append(f"{name}: no timestamps; latency, jitter and playout "
                   f"not measured")
    elif not r['wall']:
        out.append(f"{name}: media-time timestamps; latency not measured")
    lo, hi, mid = r['kf_int_min_ms'], r['kf_int_max_ms'], r['kf_int_ms']
    if mid and (lo < 0.9 * mid or hi > 1.1 * mid):
        out.append(f"{name}: keyframe interval ranged {_n(lo)}-{_n(hi)} ms")
    if r['lost_groups']:
        out.append(f"{name}: {r['lost_groups']} whole groups lost")
    if r['late_reorders']:
        out.append(f"{name}: {r['late_reorders']} objects arrived after "
                   f"the cushion; counted as reorders, not losses")
    if r['pending']:
        out.append(f"{name}: {r['pending']} missing objects still pending "
                   f"at the end")
    return out


def format_summary(rows: List[dict], skew: dict) -> List[str]:
    """Totals in the interval columns, then per-track group, keyframe
    and playout detail, notes, and the legend."""
    tw = _track_width(r['track'] for r in rows)
    out = [_iv_line('total' if i == 0 else '', tw, r)
           for i, r in enumerate(rows)]
    out.append('')
    out.append(f"  {'Track':<{tw}}" + _cells([c for c, _ in _SUM_COLS],
                                              _SUM_COLS))
    for r in rows:
        ratio, gop = r['key_p_ratio'], r['gop_p50']
        values = (
            str(r['objects']), str(r['groups']),
            '' if gop is None else f"{gop:.0f}",
            _n(r['kf_int_ms']), f"{ratio:.1f}" if ratio else '',
            _n(r['key_cost_ms']), _c(r['partial_groups']),
            _c(r['missing_eog']), _n(r['settle_p50_ms']),
            _c(r['stall_ms']), _n(r['cushion_ms']), _c(r['pre_anchor']))
        out.append(f"  {r['track']:<{tw}}" + _cells(values, _SUM_COLS))
    out += [f"  {line}" for r in rows for line in _notes(r)]
    out.append(f"  {LEGEND}")
    return out


def csv_row(kind: str, t0: Optional[float], t1: Optional[float],
            row: dict, skew: Optional[float]) -> list:
    vals = dict(row, kind=kind, t0_s=t0, t1_s=t1, skew_ms=skew)
    out = []
    for f in CSV_FIELDS:
        v = vals.get(f)
        out.append('' if v is None
                   else f"{v:.3f}" if isinstance(v, float) else v)
    return out
