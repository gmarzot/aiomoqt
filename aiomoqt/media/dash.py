"""MPEG-DASH for media ingest: MPD parsing (ISO/IEC 23009-1), segment
addressing, and a per-representation segment feed that PyAV's mp4 reader
consumes as a file.

PyAV's wheels carry no DASH demuxer (FFmpeg's needs libxml2, which they
leave out), so the manifest is parsed here with the stdlib XML parser.
Supported: SegmentTemplate with $Number$ or $Time$ (SegmentTimeline),
SegmentList, static and dynamic MPDs, one Period, UTCTiming. Not
supported: SegmentBase (indexed single file), encrypted representations.

Network, clock and sleep are injected: `fetch(url, byte_range) -> bytes`.
"""
from __future__ import annotations

import collections
import datetime
import math
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Callable, Deque, List, Optional, Tuple

from ..utils.logger import get_logger

logger = get_logger(__name__)

VIDEO_CODECS = ('avc1', 'avc3', 'hvc1', 'hev1', 'av01')
AUDIO_CODECS = ('mp4a', 'opus', 'Opus')
_LIVE_WINDOW = 30           # segments listed behind a $Number$ live edge
_POLL = 0.1                 # seconds between live availability checks
_REFRESH_MIN = 1.0          # seconds between MPD reloads
_LIVE_RETRIES = 3           # attempts for a live segment not there yet
_SLACK = Fraction(1, 10)    # seconds a key frame may precede its segment
_STALL_SEGMENTS = 4         # segment durations a live feed waits for the next


class DashError(ValueError):
    pass


_DURATION = re.compile(
    r'^P(?:(\d+(?:\.\d+)?)D)?'
    r'(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$')
_TEMPLATE = re.compile(
    r'\$(RepresentationID|Number|Bandwidth|Time)(?:%0(\d+)d)?\$|\$\$')


def parse_duration(text: Optional[str]) -> Optional[Fraction]:
    """ISO 8601 duration (PnDTnHnMnS) in seconds; None if absent."""
    if not text:
        return None
    m = _DURATION.match(text.strip())
    if not m:
        raise DashError(f"unsupported duration {text!r}")
    d, h, mi, s = (Fraction(g) if g else Fraction(0) for g in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def parse_datetime(text: Optional[str]) -> Optional[float]:
    """xs:dateTime as Unix seconds (UTC when no zone is given)."""
    if not text:
        return None
    dt = datetime.datetime.fromisoformat(text.strip().replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def expand(template: str, rep_id: str, number: Optional[int] = None,
           bandwidth: Optional[int] = None, time_: Optional[int] = None) -> str:
    """Substitute $RepresentationID$, $Number$, $Bandwidth$, $Time$ (with
    optional %0Nd width) and $$ in a SegmentTemplate URL."""
    values = {'RepresentationID': rep_id, 'Number': number,
              'Bandwidth': bandwidth, 'Time': time_}

    def sub(m):
        if m.group(0) == '$$':
            return '$'
        name, width = m.group(1), m.group(2)
        value = values[name]
        if value is None:
            raise DashError(f"${name}$ has no value in {template!r}")
        if name == 'RepresentationID':
            return str(value)
        return f"{int(value):0{int(width)}d}" if width else str(value)
    return _TEMPLATE.sub(sub, template)


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _children(el, name: str) -> list:
    return [] if el is None else [c for c in el if _local(c.tag) == name]


def _child(el, name: str):
    found = _children(el, name)
    return found[0] if found else None


def _join_base(url: str, *els) -> str:
    for el in els:
        base = _child(el, 'BaseURL')
        if base is not None and (base.text or '').strip():
            url = urllib.parse.urljoin(url, base.text.strip())
    return url


@dataclass(frozen=True)
class Segment:
    number: int
    start: int            # media time in timescale units (includes PTO)
    duration: int
    url: Optional[str] = None         # set by Representation.locate()
    byte_range: Optional[str] = None  # "first-last"


@dataclass
class Representation:
    id: str
    kind: str                       # 'video', 'audio' or other
    codecs: str
    bandwidth: int
    width: Optional[int]
    height: Optional[int]
    frame_rate: Optional[Fraction]
    sample_rate: Optional[int]
    adaptation: int                 # AdaptationSet index in the Period
    main: bool                      # AdaptationSet Role is "main"
    protected: bool                 # ContentProtection present
    base_url: str
    timescale: int
    pto: int                        # presentationTimeOffset
    start_number: int
    init_url: Optional[str]
    init_range: Optional[str]
    media: Optional[str]            # SegmentTemplate@media
    seg_duration: Optional[int]     # @duration, timescale units
    timeline: Optional[List[Tuple[Optional[int], int, int]]]  # S (t, d, r)
    seg_list: Optional[List[Tuple[str, Optional[str]]]]       # (url, range)
    period_start: Fraction
    period_duration: Optional[Fraction]
    segment_base: bool = False

    @property
    def supported(self) -> bool:
        codecs = VIDEO_CODECS if self.kind == 'video' else AUDIO_CODECS
        return (self.kind in ('video', 'audio') and not self.protected
                and self.codecs.startswith(codecs)
                and (self.media is not None or self.seg_list is not None))

    def nominal_duration(self) -> Optional[Fraction]:
        """Typical segment duration in seconds."""
        if self.seg_duration:
            return Fraction(self.seg_duration, self.timescale)
        if self.timeline:
            return Fraction(self.timeline[-1][1], self.timescale)
        return None

    def locate(self, seg: Segment, number: Optional[int] = None) -> Segment:
        """`seg` with its URL, renumbered when `number` is given."""
        number = seg.number if number is None else number
        if seg.url is not None:
            return replace(seg, number=number)
        return replace(seg, number=number, url=urllib.parse.urljoin(
            self.base_url, expand(self.media, self.id, seg.number,
                                  self.bandwidth, seg.start)))

    def segments(self, live_edge: Optional[Fraction] = None) -> List[Segment]:
        """Addressable segments, URLs unset (see locate()): all of a static
        presentation, or, given a dynamic one's live edge (seconds after
        the period start), those complete by then."""
        ts = self.timescale
        limit = None if live_edge is None else self.pto + live_edge * ts
        end = (None if self.period_duration is None
               else self.pto + self.period_duration * ts)
        out: List[Segment] = []
        if self.timeline is not None:
            t = self.pto
            entries = self.timeline
            for i, (start, d, r) in enumerate(entries):
                t = start if start is not None else t
                if r < 0:
                    nxt = entries[i + 1][0] if i + 1 < len(entries) else None
                    stop = nxt if nxt is not None else (limit if limit is not None else end)
                    if stop is None:
                        raise DashError("open-ended S@r=-1 without an end")
                    r = max(0, math.ceil((stop - t) / d) - 1)
                for _ in range(r + 1):
                    if limit is not None and t + d > limit:
                        return out
                    number = self.start_number + len(out)
                    out.append(Segment(number, t, d))
                    t += d
            return out
        if self.seg_list is not None:
            d = self.seg_duration or 0
            for i, (url, rng) in enumerate(self.seg_list):
                start = self.pto + i * d
                if limit is not None and start + d > limit:
                    break
                out.append(Segment(self.start_number + i, start, d,
                                   urllib.parse.urljoin(self.base_url, url), rng))
            return out
        d = self.seg_duration
        if not d:
            raise DashError(f"representation {self.id}: no segment duration")
        if limit is None:
            if self.period_duration is None:
                raise DashError("static $Number$ presentation without a duration")
            first, last = 0, math.ceil(self.period_duration * ts / d) - 1
        else:
            last = math.floor(live_edge * ts / d) - 1
            first = max(0, last - _LIVE_WINDOW + 1)
        for k in range(first, last + 1):
            start = self.pto + k * d
            out.append(Segment(self.start_number + k, start, d))
        return out


@dataclass
class Mpd:
    url: str
    dynamic: bool
    availability_start: Optional[float]
    minimum_update_period: Optional[Fraction]
    suggested_presentation_delay: Optional[Fraction]
    location: Optional[str]
    utc_timing: List[Tuple[str, str]]
    representations: List[Representation]


def _int(value, default=None):
    return int(value) if value not in (None, '') else default


def _segment_info(rep_id, base_url, templates, lists, bandwidth):
    """SegmentTemplate / SegmentList fields along Period, AdaptationSet
    and Representation, the innermost winning."""
    attrs, timeline = {}, None
    for el in templates:
        attrs.update(el.attrib)
        tl = _child(el, 'SegmentTimeline')
        if tl is not None:
            timeline = [(_int(s.get('t')), int(s.get('d')), _int(s.get('r'), 0))
                        for s in _children(tl, 'S')]
    info = dict(timescale=_int(attrs.get('timescale'), 1),
                pto=_int(attrs.get('presentationTimeOffset'), 0),
                start_number=_int(attrs.get('startNumber'), 1),
                seg_duration=_int(attrs.get('duration')),
                media=attrs.get('media'), timeline=timeline, seg_list=None,
                init_url=None, init_range=None)
    if attrs.get('initialization'):
        info['init_url'] = urllib.parse.urljoin(base_url, expand(
            attrs['initialization'], rep_id, bandwidth=bandwidth))
    if templates or not lists:
        return info
    sl = lists[-1]
    info.update(timescale=_int(sl.get('timescale'), 1),
                pto=_int(sl.get('presentationTimeOffset'), 0),
                start_number=_int(sl.get('startNumber'), 1),
                seg_duration=_int(sl.get('duration')), media=None,
                seg_list=[(u.get('media') or '', u.get('mediaRange'))
                          for u in _children(sl, 'SegmentURL')])
    init = _child(sl, 'Initialization')
    if init is not None:
        info['init_url'] = urllib.parse.urljoin(base_url, init.get('sourceURL') or '')
        info['init_range'] = init.get('range')
    return info


def parse_mpd(xml: bytes, url: str, now: Optional[float] = None) -> Mpd:
    """Parse an MPD fetched from `url`. Of several Periods, a static
    presentation uses the first and a dynamic one the latest started by
    `now` (Unix seconds)."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise DashError(f"MPD is not XML: {e}") from e
    if _local(root.tag) != 'MPD':
        raise DashError("not an MPD")
    dynamic = root.get('type') == 'dynamic'
    ast = parse_datetime(root.get('availabilityStartTime'))
    if dynamic and ast is None:
        raise DashError("dynamic MPD without availabilityStartTime")
    mpd_url = _join_base(url, root)
    periods, start = [], Fraction(0)
    for p in _children(root, 'Period'):
        start = parse_duration(p.get('start')) if p.get('start') else start
        periods.append((start, p))
        dur = parse_duration(p.get('duration'))
        start = start + dur if dur is not None else start
    if not periods:
        raise DashError("MPD has no Period")
    if dynamic:
        edge = Fraction((now if now is not None else time.time()) - ast)
        candidates = [x for x in periods if x[0] <= edge] or periods[:1]
        p_start, period = candidates[-1]
    else:
        p_start, period = periods[0]
    if len(periods) > 1:
        logger.warning("DASH: %d periods; following period %s only",
                       len(periods), period.get('id'))
    p_index = [p for _, p in periods].index(period)
    p_dur = parse_duration(period.get('duration'))
    if p_dur is None and p_index + 1 < len(periods):
        p_dur = periods[p_index + 1][0] - p_start
    if p_dur is None and root.get('mediaPresentationDuration'):
        p_dur = parse_duration(root.get('mediaPresentationDuration')) - p_start
    period_url = _join_base(mpd_url, period)
    reps = []
    for a_index, aset in enumerate(_children(period, 'AdaptationSet')):
        aset_url = _join_base(period_url, aset)
        role = _child(aset, 'Role')
        main = role is not None and role.get('value') == 'main'
        for rep in _children(aset, 'Representation'):
            def attr(name, default=None, rep=rep):
                return rep.get(name) or aset.get(name) or default
            rep_id = rep.get('id') or str(len(reps))
            kind = aset.get('contentType') or ''
            mime = attr('mimeType', '')
            codecs = attr('codecs', '')
            if not kind:
                kind = mime.split('/', 1)[0]
            if not kind:
                kind = ('video' if codecs.startswith(VIDEO_CODECS) else
                        'audio' if codecs.startswith(AUDIO_CODECS) else '')
            rate = attr('frameRate')
            bandwidth = _int(rep.get('bandwidth'), 0)
            base = _join_base(aset_url, rep)
            info = _segment_info(
                rep_id, base,
                [x for x in (_child(period, 'SegmentTemplate'),
                             _child(aset, 'SegmentTemplate'),
                             _child(rep, 'SegmentTemplate')) if x is not None],
                [x for x in (_child(aset, 'SegmentList'),
                             _child(rep, 'SegmentList')) if x is not None],
                bandwidth)
            reps.append(Representation(
                id=rep_id, kind=kind, codecs=codecs, bandwidth=bandwidth,
                width=_int(attr('width')), height=_int(attr('height')),
                frame_rate=Fraction(rate) if rate else None,
                sample_rate=_int(attr('audioSamplingRate')),
                adaptation=a_index, main=main,
                protected=bool(_children(aset, 'ContentProtection')
                               or _children(rep, 'ContentProtection')),
                base_url=base, period_start=p_start, period_duration=p_dur,
                segment_base=bool(_child(rep, 'SegmentBase') is not None
                                  or _child(aset, 'SegmentBase') is not None),
                **info))
    location = _child(root, 'Location')
    return Mpd(
        url=url, dynamic=dynamic, availability_start=ast,
        minimum_update_period=parse_duration(root.get('minimumUpdatePeriod')),
        suggested_presentation_delay=parse_duration(
            root.get('suggestedPresentationDelay')),
        location=(urllib.parse.urljoin(url, location.text.strip())
                  if location is not None and location.text else None),
        utc_timing=[(t.get('schemeIdUri') or '', t.get('value') or '')
                    for t in _children(root, 'UTCTiming')],
        representations=reps)


class Presentation:
    """A DASH presentation: its MPD, reloaded while dynamic, and the wall
    clock it is timed against (corrected by UTCTiming when given)."""

    def __init__(self, url: str, fetch: Callable[..., bytes],
                 clock: Callable[[], float] = time.time,
                 sleep: Optional[Callable[[float], None]] = None):
        self.url = url
        self._fetch = fetch
        self._clock = clock
        self._sleep = sleep
        self._offset = 0.0
        self.mpd = parse_mpd(fetch(url, None), url, now=clock())
        self._loaded = clock()
        if self.mpd.dynamic:
            self._offset = self._clock_offset()

    def fetch(self, url: str, byte_range: Optional[str] = None) -> bytes:
        return self._fetch(url, byte_range)

    def wait(self, stop, seconds: float) -> None:
        """Pause up to `seconds`, cut short when `stop` is set; an injected
        `sleep` replaces both."""
        if self._sleep is not None:
            self._sleep(seconds)
        else:
            stop.wait(seconds)

    def now(self) -> float:
        return self._clock() + self._offset

    def live_edge(self, rep: Representation) -> Fraction:
        """Seconds after the period start that are complete now."""
        edge = self.now() - self.mpd.availability_start
        return Fraction(edge).limit_denominator(1_000_000) - rep.period_start

    def refresh(self) -> bool:
        """Reload a dynamic MPD, at most once per _REFRESH_MIN."""
        if not self.mpd.dynamic or self._clock() - self._loaded < _REFRESH_MIN:
            return False
        url = self.mpd.location or self.url
        self.mpd = parse_mpd(self._fetch(url, None), url, now=self.now())
        self._loaded = self._clock()
        return True

    def representation(self, rep_id: str) -> Optional[Representation]:
        return next((r for r in self.mpd.representations if r.id == rep_id),
                    None)

    def select(self, rendition: str, audio: bool
               ) -> Tuple[List[Representation], Optional[Representation]]:
        """best: the highest-bandwidth video; all: every video, best
        first. Audio: the best of the main (else first) audio set."""
        reps = [r for r in self.mpd.representations if r.supported]
        videos = sorted((r for r in reps if r.kind == 'video'),
                        key=lambda r: (r.bandwidth, (r.width or 0) * (r.height or 0)),
                        reverse=True)
        if rendition == 'best':
            videos = videos[:1]
        chosen = None
        audios = [r for r in reps if r.kind == 'audio']
        if audio and audios:
            first = min(audios, key=lambda r: (not r.main, r.adaptation)).adaptation
            chosen = max((r for r in audios if r.adaptation == first),
                         key=lambda r: r.bandwidth)
        if not videos and chosen is None:
            why = []
            if any(r.protected for r in self.mpd.representations):
                why.append("encrypted representations are not supported")
            if any(r.segment_base and r.media is None and r.seg_list is None
                   for r in self.mpd.representations):
                why.append("SegmentBase (indexed single file) is not supported")
            raise DashError("no supported video or audio representation"
                            + (f" ({'; '.join(why)})" if why else ""))
        return videos, chosen

    def start_time(self, reps: List[Representation]) -> Fraction:
        """Seconds after the period start to begin at: 0 for a static
        presentation; for a dynamic one, behind the live edge by the
        suggested presentation delay, and at least two segments."""
        if not self.mpd.dynamic:
            return Fraction(0)
        seg = max((r.nominal_duration() or Fraction(2) for r in reps),
                  default=Fraction(2))
        delay = max(self.mpd.suggested_presentation_delay or Fraction(0), 2 * seg)
        return max(Fraction(0), self.live_edge(reps[0]) - delay)

    def _clock_offset(self) -> float:
        for scheme, value in self.mpd.utc_timing:
            try:
                if scheme.endswith(('http-xsdate:2014', 'http-iso:2014')):
                    t0 = self._clock()
                    server = parse_datetime(self._fetch(value, None).decode())
                    return server - (t0 + self._clock()) / 2
                if scheme.endswith('direct:2014'):
                    return parse_datetime(value) - self._clock()
            except Exception as e:
                logger.warning("DASH: UTCTiming %s failed: %s", scheme, e)
        return 0.0


class SegmentFeed:
    """One representation's init segment then its media segments, from
    `start` seconds after the period start, as a read-only, non-seekable
    file for PyAV. Live segments are awaited; `stop` ends the feed.
    segment_of() maps a media time to the number of the segment it came
    from, monotonic per feed."""

    def __init__(self, pres: Presentation, rep: Representation,
                 start: Fraction, stop):
        self._pres = pres
        self._rep_id = rep.id
        self._stop = stop
        self._start = start
        self._init: Optional[Tuple[Optional[str], Optional[str]]] = (
            rep.init_url, rep.init_range)
        self._data = memoryview(b'')
        self._pos = 0
        self._last: Optional[int] = None   # start of the last segment taken
        self._number: Optional[int] = None
        self._history: Deque[Tuple[Fraction, Fraction, int]] = (
            collections.deque(maxlen=64))
        self._static: Optional[List[Segment]] = None
        self.timescale = rep.timescale
        self.pto = rep.pto

    def read(self, n: int) -> bytes:
        while self._pos >= len(self._data):
            if self._stop.is_set():
                return b''
            data = self._next_data()
            if data is None:
                return b''
            self._data, self._pos = memoryview(data), 0
        out = bytes(self._data[self._pos:self._pos + n])
        self._pos += len(out)
        return out

    def segment_of(self, t: Fraction) -> Optional[int]:
        """Number of the segment holding media time `t` (seconds). The
        slack absorbs a key frame landing up to a few frames before a
        $Number$ segment's nominal start."""
        for start, dur, number in reversed(self._history):
            if t + min(dur / 4, _SLACK) >= start:
                return number
        return None

    def _next_data(self) -> Optional[bytes]:
        if self._init is not None:
            url, rng = self._init
            self._init = None
            if url:
                return self._pres.fetch(url, rng)
        while not self._stop.is_set():
            seg = self._next_segment()
            if seg is None:
                return None
            data = self._get(seg)
            if data is not None:
                return data
        return None

    def _get(self, seg: Segment) -> Optional[bytes]:
        """A segment's bytes, or None (logged) when it stays missing; a live
        one is retried, since it may not have reached the origin yet."""
        attempts = _LIVE_RETRIES if self._pres.mpd.dynamic else 1
        for attempt in range(1, attempts + 1):
            try:
                return self._pres.fetch(seg.url, seg.byte_range)
            except OSError as e:
                if attempt == attempts:
                    logger.warning("DASH: skipping segment %d (%s): %s",
                                   seg.number, seg.url, e)
                    return None
                self._pres.wait(self._stop, 0.5 * attempt)
        return None

    def _next_segment(self) -> Optional[Segment]:
        """The next segment, waiting for a live one; None at the end of a
        static presentation, on stop, or when a live one has not appeared
        within _stall_limit()."""
        deadline = None
        while not self._stop.is_set():
            rep = self._pres.representation(self._rep_id)
            if rep is None:
                logger.warning("DASH: representation %s left the MPD", self._rep_id)
                return None
            dynamic = self._pres.mpd.dynamic
            if dynamic:
                segs = rep.segments(self._pres.live_edge(rep))
            else:
                if self._static is None:
                    self._static = rep.segments()
                segs = self._static
            seg = self._pick(rep, segs)
            if seg is not None:
                return self._advance(rep, seg)
            if not dynamic:
                return None
            limit = self._stall_limit(rep)
            now = self._pres.now()
            deadline = now + limit if deadline is None else deadline
            if now > deadline:
                logger.warning("DASH: no new segment of %s in %.0f s; ending",
                               self._rep_id, limit)
                return None
            if rep.timeline is not None:
                self._pres.refresh()
            self._pres.wait(self._stop, _POLL)
        return None

    def _stall_limit(self, rep: Representation) -> float:
        """Seconds a live feed waits for its next segment before ending."""
        seg = float(rep.nominal_duration() or 2)
        mup = float(self._pres.mpd.minimum_update_period or 0)
        return max(_STALL_SEGMENTS * seg, mup)

    def _pick(self, rep: Representation, segs: List[Segment]) -> Optional[Segment]:
        """The first segment, ending after the start time; then the first
        starting after the last one taken (a gap is skipped over)."""
        if self._last is None:
            target = rep.pto + self._start * rep.timescale
            return next((s for s in segs if s.start + s.duration > target), None)
        return next((s for s in segs if s.start > self._last), None)

    def _advance(self, rep: Representation, seg: Segment) -> Segment:
        number = seg.number
        if self._number is not None and number <= self._number:
            number = self._number + 1  # MPD renumbered as its window slid
        self._last = seg.start
        self._number = number
        ts = rep.timescale
        self._history.append((Fraction(seg.start, ts), Fraction(seg.duration, ts),
                              number))
        return rep.locate(seg, number)


__all__ = ['DashError', 'Mpd', 'Presentation', 'Representation', 'Segment',
           'SegmentFeed', 'expand', 'parse_datetime', 'parse_duration',
           'parse_mpd']
