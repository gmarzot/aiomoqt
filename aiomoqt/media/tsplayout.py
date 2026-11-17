"""PCR-locked playout and delivery analysis of an MPEG-TS stream received
over MSFTS (sub_media --ts-playout, and --analyze on an mpeg2ts track).

Each TS packet is due at first_PCR_arrival + buffer + (PCR - PCR0) /
27 MHz. PCRs come from one PID, unwrapped and rebased across
discontinuities; packets between them are placed at the rate of the
last two PCRs, which is exact for a constant-rate mux. A packet that
becomes available after it is due is late: dropped, or emitted at once.
A gap of `break_ms` or more in the released media is an output break.

The model runs for the configured buffer and for each of BUFFERS_MS at
once, so one run answers every buffer size. Times are monotonic ns; each
object's arrival on the wall clock dates the arrival log, the breaks and
the end-to-end delay against a publisher's wall clock (a wall clock can
step against the monotonic one).
"""
from __future__ import annotations

import bisect
import collections
import json
from typing import IO, Any, Deque, Dict, List, Optional, Tuple

from .msfts import Psi

TS_PACKET = 188
BUFFERS_MS = (100, 200, 300, 500, 1000)
BREAK_MS = 200
_NULL_PID = 0x1FFF
_WRAP = (1 << 33) * 300
# A PCR that goes back, or ahead by this much more than the time since
# the last one arrived, is a discontinuity; a jump the arrival time
# explains is lost media.
_JUMP_NS = 1_000_000_000
# Histogram bin: 0.1 ms.
_BIN_NS = 100_000
# Group arrivals kept to match media timeline records.
_TIMELINE_GROUPS = 256


def _ns(ticks) -> int:
    """27 MHz ticks as ns."""
    return int(ticks * 1000 // 27)


class _Hist:
    """A sparse histogram of ms values in 0.1 ms bins."""
    __slots__ = ('bins', 'n')

    def __init__(self):
        self.bins: Dict[int, int] = collections.Counter()
        self.n = 0

    def add(self, ns: int) -> None:
        self.bins[ns // _BIN_NS] += 1
        self.n += 1

    def merge(self, other: '_Hist') -> None:
        self.bins.update(other.bins)
        self.n += other.n

    def pct(self, p: float) -> Optional[float]:
        """The p-th percentile in ms, the top of its bin (p = 0: the
        bottom of the minimum's)."""
        if not self.n:
            return None
        rank = max(1, -(-self.n * p // 100))
        seen = 0
        for b in sorted(self.bins):
            seen += self.bins[b]
            if seen >= rank:
                return round((b + 1) * _BIN_NS / 1e6, 1) if p else round(
                    b * _BIN_NS / 1e6, 1)
        return None

    def summary(self, *ps) -> Optional[Dict[str, Optional[float]]]:
        if not self.n:
            return None
        return {f"p{str(p).replace('.', '')}": self.pct(p) for p in ps}


class _Model:
    """The playout model at one buffer size: late packets and output
    breaks, the latest released packet's due time in `last`."""
    __slots__ = ('ns', 'late', 'breaks', 'break_ns', 'last', 'keep', 'log')

    def __init__(self, ms: int, keep: bool = False):
        self.ns = ms * 1_000_000
        self.late = 0
        self.breaks = 0
        self.break_ns = 0
        self.last: Optional[int] = None
        self.keep = keep
        self.log: List[Tuple[int, int]] = []

    def take(self, avail: int, due0: int, taus: List[int], emit: bool,
             break_ns: int) -> int:
        """Release one object whose packets are due at due0 + buffer +
        taus; the late ones, a prefix, are counted and with `emit`
        released at `avail`. Returns how many were late."""
        due0 += self.ns
        k = bisect.bisect_left(taus, avail - due0)
        self.late += k
        if emit and k:
            self._out(avail, break_ns)
        if k < len(taus):
            if taus[-1] - taus[k] >= break_ns:
                for t in taus[k:]:
                    self._out(due0 + t, break_ns)
            else:
                self._out(due0 + taus[k], break_ns)
                self.last = max(self.last or 0, due0 + taus[-1])
        return k

    def _out(self, at: int, break_ns: int) -> None:
        last = self.last
        if last is not None and at - last >= break_ns:
            self.breaks += 1
            self.break_ns += at - last
            if self.keep:
                self.log.append((last, at - last))
        if last is None or at > last:
            self.last = at


class _Window:
    """What one interval, or the whole run, counts."""

    def __init__(self, buffers):
        self.packets = 0
        self.cc_errors: Dict[int, int] = collections.Counter()
        self.cc_dups = 0
        self.pcrs = 0
        self.pcr_gaps = 0
        self.pcr_gap_sum = 0
        self.pcr_gap_max = 0
        self.discontinuities = 0
        self.delay = _Hist()
        self.e2e = _Hist()
        self.depth_min: Optional[int] = None
        self.depth_max: Optional[int] = None
        self.depth_sum = 0
        self.depth_n = 0
        self.objects = 0
        self.lost = 0
        self.groups = 0
        self.groups_missing = 0
        self.base = {ms: (0, 0, 0) for ms in buffers}
        self.dups0 = 0
        self.switches0 = 0
        self.breaks: List[Tuple[int, int]] = []


class TsPlayout:
    """Playout and analysis of one mpeg2ts track. push() takes each
    object in Group and Object order with when it became available;
    with `buffer_ms` and `release`, due() / pop() give the packets to
    release.

    `pcr_pid` (the catalog's mpeg2tsPcrPid) holds until the stream's PMT
    names one, which takes precedence (msfts-02 §6); None takes the
    first PID seen carrying a PCR. A PCR PID change is a discontinuity.
    `late` is 'drop' or 'emit'. switch() moves on to the next track of
    the stream (`switches`). `arrival_log` gets a JSON line per PCR: the PCR,
    its arrival and its release time, wall-clock ns. Without an object's
    wall clock, `wall_offset_ns` from monotonic stands in."""

    def __init__(self, buffer_ms: Optional[int] = None, *,
                 late: str = 'drop', break_ms: int = BREAK_MS,
                 buffers=BUFFERS_MS, pcr_pid: Optional[int] = None,
                 wall_offset_ns: int = 0,
                 arrival_log: Optional[IO[str]] = None,
                 release: bool = True):
        if late not in ('drop', 'emit'):
            raise ValueError(f"late {late!r}")
        self.buffer_ms = buffer_ms
        self.release = release and buffer_ms is not None
        self.emit_late = late == 'emit'
        self.break_ns = break_ms * 1_000_000
        self.buffers = tuple(sorted(set(buffers)))
        self.models = {ms: _Model(ms) for ms in self.buffers}
        self.model = (None if buffer_ms is None
                      else _Model(buffer_ms, keep=True))
        self.pcr_pid = pcr_pid
        self.wall_offset_ns = wall_offset_ns
        self.arrival_log = arrival_log
        self.queue: Deque[Tuple[int, bytes]] = collections.deque()
        self.dropped = 0
        self.reconnects = 0
        self.switches = 0
        self._psi = Psi()
        self._repcr = False
        self.dups = 0
        self.e2e_source: Optional[str] = None
        self._cc: Dict[int, int] = {}
        self._idx = 0
        self._anchor: Optional[int] = None
        self._last: Optional[Tuple[int, int, int]] = None
        self._rate: Optional[float] = None
        self._offset = 0
        self._tau = 0
        self._wall = 0
        self._off = wall_offset_ns
        self._jitter = 0.0
        # Objects whose packets wait for a PCR to place them: until two
        # give the rate, and after missing media (`_lost`), which no
        # rate spans.
        self._held: List[Tuple[Any, int, List]] = []
        self._hold = True
        self._lost = False
        self._loc: Optional[Tuple[int, int]] = None
        self._groups: 'collections.OrderedDict[int, int]' = (
            collections.OrderedDict())
        self._timeline: Dict[Tuple[int, int], int] = {}
        self._timeline_floor = 0
        self.total = _Window(self.buffers)
        self._win = _Window(self.buffers)

    # -- input --

    def push(self, payload, avail: int, group: Optional[int] = None,
             obj: Optional[int] = None, wall_us: Optional[int] = None,
             wall_ns: Optional[int] = None) -> None:
        """One object of whole TS packets, available at `avail`, at
        `wall_ns` on the wall clock; `wall_us` is the publisher's wall
        clock for it (its draft-frindell timestamp), if known."""
        self._wall = avail + self.wall_offset_ns if wall_ns is None else wall_ns
        self._off = self._wall - avail
        w = self._win
        if group is not None and obj is not None and self._location(
                group, obj, avail, w):
            self._hold = self._lost = True
        if wall_us is not None:
            self.e2e_source = 'frindell'
            self._e2e(self._wall - wall_us * 1000)
        taus = self._packets(payload, avail, w)
        if self._hold:
            self._held.append((payload, avail, taus))
            return
        for p, a, t in self._held:
            self._release(p, a, t)
        self._held = []
        self._release(payload, avail, taus)

    def switch(self) -> None:
        """The stream goes on in the next track: its groups start over,
        and the packets the publisher dropped before its first random
        access point are no CC error."""
        self.switches += 1
        self._loc = None
        self._cc = {}
        self._psi = Psi()
        self._hold = self._lost = True
        self._groups.clear()
        self._timeline.clear()
        self._timeline_floor = 0

    def timeline(self, records) -> None:
        """MSF media timeline records [media ms, [group, object], wall
        ms] (msf-01 §7.1.1): the publisher's wall clock per Location."""
        known, floor = self._timeline, self._timeline_floor
        for rec in records:
            try:
                (g, o), wall = rec[1], int(rec[2])
                g, o = int(g), int(o)
            except (TypeError, ValueError, IndexError):
                continue
            if not wall or g < floor or (g, o) in known:
                continue
            known[(g, o)] = wall
            if self.e2e_source != 'frindell' and o == 0:
                at = self._groups.get(g)
                if at is not None:
                    self.e2e_source = 'media-timeline'
                    self._e2e(at - wall * 1_000_000)
        if len(known) > _TIMELINE_GROUPS:
            # Keep the newest Locations; older records are skipped after.
            keep = sorted(known)[-_TIMELINE_GROUPS:]
            self._timeline = {k: known[k] for k in keep}
            self._timeline_floor = keep[0][0]

    def _e2e(self, ns: int) -> None:
        self._win.e2e.add(ns)

    def _location(self, g: int, o: int, avail: int, w: _Window) -> bool:
        """Count the object; True when objects before it are missing."""
        w.objects += 1
        last = self._loc
        self._loc = (g, o)
        missing = False
        if last is None or g > last[0]:
            w.groups += 1
            if last is not None:
                w.lost += o
                w.groups_missing += max(0, g - last[0] - 1)
                missing = o > 0 or g > last[0] + 1
            if o == 0:
                self._groups[g] = self._wall
                while len(self._groups) > _TIMELINE_GROUPS:
                    self._groups.popitem(last=False)
                wall = self._timeline.get((g, 0))
                if wall is not None and self.e2e_source != 'frindell':
                    self.e2e_source = 'media-timeline'
                    self._e2e(self._wall - wall * 1_000_000)
        elif g == last[0] and o > last[1] + 1:
            w.lost += o - last[1] - 1
            missing = True
        return missing

    def _packets(self, payload, avail: int, w: _Window) -> List:
        """CC, PCR and each packet's time since PCR0 in ns: None for one
        a later PCR places."""
        taus: List = []
        cc_map = self._cc
        n = len(payload)
        idx = self._idx
        for i in range(0, n - TS_PACKET + 1, TS_PACKET):
            b1 = payload[i + 1]
            pid = ((b1 & 0x1F) << 8) | payload[i + 2]
            b3 = payload[i + 3]
            af = b3 & 0x20 and payload[i + 4]
            disc = af and payload[i + 5] & 0x80
            if (pid == 0 or pid == self._psi.pmt_pid) and self._psi.feed(
                    payload, i, pid, b1 & 0x40):
                pcr_pid = self._psi.pcr_pid
                if pcr_pid is not None and pcr_pid != self.pcr_pid:
                    self._repcr = self._last is not None
                    self.pcr_pid = pcr_pid
            if b3 & 0x10 and pid != _NULL_PID:
                cc = b3 & 0x0F
                last = cc_map.get(pid)
                if last is not None and not disc:
                    if cc == last:
                        w.cc_dups += 1
                    elif cc != (last + 1) & 0x0F:
                        w.cc_errors[pid] += 1
                cc_map[pid] = cc
            if (af and af >= 7 and payload[i + 5] & 0x10
                    and (self.pcr_pid is None or pid == self.pcr_pid)):
                self.pcr_pid = pid
                tau = max(self._tau,
                          self._pcr(payload, i, idx, avail, bool(disc), w))
                if self._hold:
                    self._place_back(taus, tau)
                    self._hold = self._rate is None
                    self._lost = False
            elif self._hold:
                tau = None
            else:
                tau = self._tau
                rate, last_pcr = self._rate, self._last
                if rate is not None and last_pcr is not None:
                    tau = max(tau, _ns(last_pcr[0] + (idx - last_pcr[1])
                                       * rate))
            if tau is not None:
                self._tau = tau
            taus.append(tau)
            idx += 1
        w.packets += idx - self._idx
        self._idx = idx
        return taus

    def _place_back(self, taus: List, tau: int) -> None:
        """Place the packets held for this PCR (`taus` before it, then
        the held objects) back from it at the last known rate, no
        earlier than the last packet placed."""
        step = 0.0 if self._rate is None else self._rate * 1000 / 27
        floor = self._tau
        d = 0
        for lst in [taus] + [t for _, _, t in reversed(self._held)]:
            for j in range(len(lst) - 1, -1, -1):
                if lst[j] is not None:
                    return
                d += 1
                lst[j] = max(floor, int(tau - d * step))

    def _pcr(self, b, i: int, idx: int, avail: int, disc: bool,
             w: _Window) -> int:
        raw = ((b[i + 6] << 25 | b[i + 7] << 17 | b[i + 8] << 9
                | b[i + 9] << 1 | b[i + 10] >> 7) * 300
               + ((b[i + 10] & 1) << 8 | b[i + 11]))
        w.pcrs += 1
        last = self._last
        if last is None:
            self._offset = -raw
            self._last = (0, idx, avail)
            self._anchor = avail
            self._log(raw, avail, 0)
            return 0
        prev, at, prev_avail = last
        adj = raw + self._offset
        if adj < prev - _WRAP // 2:
            self._offset += _WRAP
            adj += _WRAP
        repcr, self._repcr = self._repcr, False
        if (disc or repcr or adj < prev
                or _ns(adj - prev) > avail - prev_avail + _JUMP_NS):
            w.discontinuities += 1
            expect = prev + int((idx - at) * (self._rate or 0))
            self._offset += expect - adj
            adj = expect
        else:
            gap = adj - prev
            w.pcr_gaps += 1
            w.pcr_gap_sum += gap
            w.pcr_gap_max = max(w.pcr_gap_max, gap)
            if idx > at and gap > 0 and not self._lost:
                self._rate = gap / (idx - at)
            d = (avail - prev_avail) - _ns(gap)
            self._jitter += (abs(d) - self._jitter) / 16
        self._last = (adj, idx, avail)
        tau = _ns(adj)
        self._log(raw, avail, tau)
        return tau

    def _log(self, raw: int, avail: int, tau: int) -> None:
        if self.arrival_log is None:
            return
        release = (None if self.buffer_ms is None else
                   (self._anchor or 0) + self.buffer_ms * 1_000_000 + tau
                   + self._off)
        self.arrival_log.write(json.dumps(
            {"pcr": raw, "arrival_ns": avail + self._off,
             "release_ns": release}, separators=(',', ':')) + '\n')

    # -- the model --

    def _release(self, payload, avail: int, taus: List[int]) -> None:
        if not taus:
            return
        w = self._win
        due0 = self._anchor or 0
        w.delay.add(avail - due0 - taus[0])
        for m in self.models.values():
            m.take(avail, due0, taus, self.emit_late, self.break_ns)
        m = self.model
        if m is None:
            return
        breaks = len(m.log)
        k = m.take(avail, due0, taus, self.emit_late, self.break_ns)
        w.breaks.extend((at + self._off, d) for at, d in m.log[breaks:])
        depth = due0 + m.ns + taus[-1] - avail
        lo, hi = w.depth_min, w.depth_max
        w.depth_min = depth if lo is None else min(lo, depth)
        w.depth_max = depth if hi is None else max(hi, depth)
        w.depth_sum += depth
        w.depth_n += 1
        if k and not self.emit_late:
            self.dropped += k
        if not self.release:
            return
        q = self.queue
        due0 += m.ns
        for j, tau in enumerate(taus):
            if j < k:
                if not self.emit_late:
                    continue
                at = avail
            else:
                at = due0 + tau
            if q and at < q[-1][0]:
                at = q[-1][0]
            q.append((at, payload[j * TS_PACKET:(j + 1) * TS_PACKET]))

    # -- output --

    def due(self) -> Optional[int]:
        """When the next packet is due; None when none is queued."""
        return self.queue[0][0] if self.queue else None

    def pop(self, now: int) -> bytes:
        """The packets due by `now`, in order."""
        q = self.queue
        out = []
        while q and q[0][0] <= now:
            out.append(q.popleft()[1])
        return b''.join(out)

    # -- reports --

    def interval(self) -> Dict[str, Any]:
        """The counts since the last call, which start the next."""
        w = self._win
        out = self._report(w)
        t = self.total
        t.packets += w.packets
        t.cc_errors.update(w.cc_errors)
        t.cc_dups += w.cc_dups
        t.pcrs += w.pcrs
        t.pcr_gaps += w.pcr_gaps
        t.pcr_gap_sum += w.pcr_gap_sum
        t.pcr_gap_max = max(t.pcr_gap_max, w.pcr_gap_max)
        t.discontinuities += w.discontinuities
        t.delay.merge(w.delay)
        t.e2e.merge(w.e2e)
        if w.depth_min is not None and w.depth_max is not None:
            lo, hi = t.depth_min, t.depth_max
            t.depth_min = w.depth_min if lo is None else min(lo, w.depth_min)
            t.depth_max = w.depth_max if hi is None else max(hi, w.depth_max)
            t.depth_sum += w.depth_sum
            t.depth_n += w.depth_n
        for key in ('objects', 'lost', 'groups', 'groups_missing'):
            setattr(t, key, getattr(t, key) + getattr(w, key))
        t.breaks.extend(w.breaks)
        nxt = _Window(self.buffers)
        nxt.base = {ms: self._counts(ms) for ms in self.buffers}
        if self.model is not None:
            nxt.base[None] = self._counts(None)
        nxt.dups0 = self.dups
        nxt.switches0 = self.switches
        self._win = nxt
        return out

    def summary(self) -> Dict[str, Any]:
        """The whole run; the open interval is folded in unreported."""
        self.interval()
        return self.totals()

    def totals(self) -> Dict[str, Any]:
        """The run up to the last interval."""
        t = self.total
        t.base = {ms: (0, 0, 0) for ms in self.buffers}
        t.base[None] = (0, 0, 0)
        return self._report(t)

    def _counts(self, ms) -> Tuple[int, int, int]:
        m = self.model if ms is None else self.models[ms]
        return (0, 0, 0) if m is None else (m.late, m.breaks, m.break_ns)

    def _delta(self, w: _Window, ms) -> Dict[str, Any]:
        late, breaks, bns = self._counts(ms)
        b = w.base.get(ms, (0, 0, 0))
        return {"late": late - b[0], "breaks": breaks - b[1],
                "break_ms": round((bns - b[2]) / 1e6, 1)}

    def _report(self, w: _Window) -> Dict[str, Any]:
        delay = w.delay.summary(0, 50, 99, 99.9, 100)
        playout = None
        if self.model is not None:
            playout = {"buffer_ms": self.buffer_ms,
                       "late_policy": 'emit' if self.emit_late else 'drop',
                       **self._delta(w, None),
                       "break_list": [
                           {"start_ns": s, "duration_ms": round(d / 1e6, 1)}
                           for s, d in w.breaks],
                       "depth_ms": None if not w.depth_n else {
                           "min": round((w.depth_min or 0) / 1e6, 1),
                           "mean": round(w.depth_sum / w.depth_n / 1e6, 1),
                           "max": round((w.depth_max or 0) / 1e6, 1)}}
        e2e = w.e2e.summary(50, 99, 100)
        return {
            "ts_packets": w.packets,
            "cc_errors": {str(pid): n for pid, n in sorted(w.cc_errors.items())},
            "cc_errors_total": sum(w.cc_errors.values()),
            "cc_dups": w.cc_dups,
            "pcr": {"pid": self.pcr_pid, "count": w.pcrs,
                    "interval_ms": None if not w.pcr_gaps else {
                        "mean": round(w.pcr_gap_sum / w.pcr_gaps / 27_000, 2),
                        "max": round(w.pcr_gap_max / 27_000, 2)},
                    "jitter_ms": round(self._jitter / 1e6, 3),
                    "discontinuities": w.discontinuities},
            "delay_ms": delay,
            "cushion_ms": None if delay is None else {
                "p999": max(0.0, delay["p999"] or 0.0),
                "p100": max(0.0, delay["p100"] or 0.0)},
            "playout": playout,
            "buffers": {str(ms): self._delta(w, ms) for ms in self.buffers},
            "objects": {"received": w.objects, "lost": w.lost,
                        "dups": self.dups - w.dups0, "groups": w.groups,
                        "groups_missing": w.groups_missing},
            "switches": self.switches - w.switches0,
            "reconnects": self.reconnects,
            "e2e_ms": None if e2e is None else {
                "source": self.e2e_source, "samples": w.e2e.n, **e2e},
        }
