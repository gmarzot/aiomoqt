"""Object capture times: draft-frindell-moq-timestamp-00 Track and Object
Properties, LOC TIMESTAMP (loc-04) and the legacy bench property 0x20.

capture_time_us() resolves one object, in that order of precedence, to
wall-clock µs since the epoch and the source it came from. A track's
frindell Track Properties are parsed once into a TrackTiming; a
violation of the draft's rules raises TimestampMalformed (the track is
malformed, MOQT §12.1).
"""
import time
from fractions import Fraction
from math import gcd
from typing import Any, Dict, Iterable, Optional, Tuple


from .buffer import Buffer, BufferReadError

# draft-frindell-moq-timestamp-00 §9 (provisional code points).
TIMESCALE = 0x2C7A51E0
CLOCK_ID = 0x3E8D2B70
TIMESTAMP_ORIGIN = 0x31B49A6E
TIMESTAMP_MAPPING = 0x27F308C5
OBJECT_TIMESTAMP = 0x2D1A
TRACK_IDS = frozenset((TIMESCALE, CLOCK_ID, TIMESTAMP_ORIGIN,
                       TIMESTAMP_MAPPING))
TIMING_IDS = TRACK_IDS | {OBJECT_TIMESTAMP}
NAMES = {TIMESCALE: "TIMESCALE", CLOCK_ID: "CLOCK_ID",
         TIMESTAMP_ORIGIN: "TIMESTAMP_ORIGIN",
         TIMESTAMP_MAPPING: "TIMESTAMP_MAPPING",
         OBJECT_TIMESTAMP: "OBJECT_TIMESTAMP"}

# loc-04: TIMESTAMP is wall-clock µs unless a TIMESCALE gives its units.
LOC_TIMESTAMP = 0x10
LOC_TIMESCALE = 0x08
BENCH_TIMESTAMP = 0x20

SOURCE_FRINDELL = "frindell"
SOURCE_LOC = "loc"
SOURCE_BENCH = "bench"

U64_MAX = (1 << 64) - 1


class TimestampMalformed(ValueError):
    """Timing properties that make the track malformed."""


def zigzag_encode(v: int) -> int:
    """§2.1: 0, -1, 1, -2, ... -> 0, 1, 2, 3, ..."""
    if not -(1 << 63) <= v < 1 << 63:
        raise ValueError(f"{v} is outside the 64-bit signed range")
    return (v << 1) ^ (v >> 63)


def zigzag_decode(u: int) -> int:
    return (u >> 1) ^ -(u & 1)


def encode_mapping(base_group: int, base_timestamp: int,
                   group_multiplier: int, object_multiplier: int,
                   vi64: bool) -> bytes:
    """TIMESTAMP_MAPPING value (§4.4): four integers in the session's
    variable-length integer encoding."""
    buf = Buffer(capacity=40, vi64=vi64)
    for v in (base_group, base_timestamp, group_multiplier,
              object_multiplier):
        buf.push_vint(v)
    return bytes(buf.data_slice(0, buf.tell()))


def decode_mapping(raw: bytes, vi64: bool) -> Tuple[int, int, int, int]:
    """§4.4: exactly four integers, else TimestampMalformed."""
    raw = bytes(raw)
    buf = Buffer(data=raw, vi64=vi64)
    out = []
    try:
        while buf.tell() < len(raw) and len(out) < 4:
            out.append(buf.pull_vint())
    except (BufferReadError, ValueError):
        raise TimestampMalformed(
            "TIMESTAMP_MAPPING is not four integers") from None
    if len(out) != 4 or buf.tell() != len(raw):
        raise TimestampMalformed("TIMESTAMP_MAPPING is not four integers")
    return out[0], out[1], out[2], out[3]


def _name(ptype: int) -> str:
    return NAMES.get(ptype, f"0x{ptype:x}")


class TrackTiming:
    """One track's timing context.

    `timescale` is None unless the track declares frindell Track
    Properties. `clock_id` 0 (absent) is the POSIX wall clock; another
    value is a media clock with no latency figure. `loc_timescale` is a
    LOC TIMESCALE (0x08) seen as a Track Property or on any object of
    the track, after which LOC timestamps are media time. `plain` is
    True while neither is known: LOC TIMESTAMP then needs no resolver."""

    __slots__ = ('timescale', 'clock_id', 'origin', 'mapping',
                 'loc_timescale', '_wall', 'plain')

    def __init__(self, timescale: Optional[int] = None, clock_id: int = 0,
                 origin: int = 0,
                 mapping: Optional[Tuple[int, int, int, int]] = None,
                 loc_timescale: Optional[int] = None):
        self.timescale = timescale
        self.clock_id = clock_id
        self.origin = origin
        self.mapping = mapping
        self.loc_timescale = loc_timescale
        self._wall = clock_id == 0
        self.plain = timescale is None and loc_timescale is None

    def assign(self, other: 'TrackTiming') -> None:
        """Take `other`'s values, keeping this object's identity."""
        for name in self.__slots__:
            setattr(self, name, getattr(other, name))

    @property
    def declared(self) -> bool:
        """The track declares frindell timestamps."""
        return self.timescale is not None

    @property
    def wall_clock(self) -> bool:
        return self._wall

    @classmethod
    def from_properties(cls, props: Optional[Dict[int, Any]], vi64: bool,
                        duplicates: Iterable[int] = ()) -> 'TrackTiming':
        """Parse merged Track Properties (Immutable Properties unwrapped);
        `duplicates` are the types seen more than once across both sets.
        Raises TimestampMalformed (§3, §4)."""
        props = props or {}
        for ptype in TIMING_IDS.intersection(duplicates):
            raise TimestampMalformed(f"{_name(ptype)} appears more than once")
        if OBJECT_TIMESTAMP in props:
            raise TimestampMalformed("OBJECT_TIMESTAMP as a Track Property")
        loc_ts = props.get(LOC_TIMESCALE)
        loc_ts = loc_ts if isinstance(loc_ts, int) else None
        if TRACK_IDS.isdisjoint(props):
            return cls(loc_timescale=loc_ts)
        timescale = props.get(TIMESCALE)
        if not timescale:
            raise TimestampMalformed("TIMESCALE missing or 0")
        clock_id = props.get(CLOCK_ID)
        origin = props.get(TIMESTAMP_ORIGIN)
        if origin is not None and clock_id is None:
            raise TimestampMalformed("TIMESTAMP_ORIGIN without CLOCK_ID")
        raw = props.get(TIMESTAMP_MAPPING)
        mapping = None if raw is None else decode_mapping(raw, vi64)
        return cls(timescale, clock_id or 0, origin or 0, mapping, loc_ts)

    def timestamp(self, group_id: int, object_id: int,
                  exts: Optional[Dict[int, Any]]) -> Optional[int]:
        """The object's timestamp in ticks (§5), None when it has none.
        Assumes a declared track; raises TimestampMalformed."""
        ots = None
        if exts:
            ots = exts.get(OBJECT_TIMESTAMP)
            # Only the Python codec yields KeyValuePairs (duplicates).
            if not TRACK_IDS.isdisjoint(exts) or (
                    ots is not None and type(exts) is not dict):
                check_object_properties(exts)
        m = self.mapping
        if m is not None:
            ts = m[1] + (group_id - m[0]) * m[2] + object_id * m[3]
            if ots is not None:
                ts += zigzag_decode(ots)
        elif ots is not None:
            ts = ots
        else:
            return None
        if ts < 0 or ts > U64_MAX:
            raise TimestampMalformed(f"timestamp {ts} outside 0..2^64-1")
        return ts

    def media_us(self, group_id: int, object_id: int,
                 exts: Optional[Dict[int, Any]]) -> Optional[int]:
        """The object's time on the track's clock in µs, wall clock or
        not; None when it has no timestamp."""
        ts = self.timestamp(group_id, object_id, exts)
        if ts is None:
            return None
        return (self.origin + ts) * 1_000_000 // self.timescale


def check_object_properties(exts: Dict[int, Any]) -> None:
    """§3: no Track-scope timing property on an object, and
    OBJECT_TIMESTAMP at most once (seen only where the codec marks
    duplicates: datagrams and FETCH; subgroup objects are decoded by
    aiopquic, which keeps the last)."""
    for ptype in TRACK_IDS.intersection(exts):
        raise TimestampMalformed(f"{_name(ptype)} as an Object Property")
    dups = getattr(exts, 'duplicates', None)
    if dups and OBJECT_TIMESTAMP in dups:
        raise TimestampMalformed("OBJECT_TIMESTAMP appears more than once")


def check_object(timing: Optional['TrackTiming'], group_id: int,
                 object_id: int, exts: Optional[Dict[int, Any]]) -> None:
    """Raise TimestampMalformed when an object breaks §3-§5 on a track
    with this timing context."""
    if timing is not None and timing.timescale is not None:
        timing.timestamp(group_id, object_id, exts)
    elif exts and not TIMING_IDS.isdisjoint(exts):
        check_object_properties(exts)
        raise TimestampMalformed("OBJECT_TIMESTAMP without TIMESCALE")


def capture_time_us(timing: Optional[TrackTiming], group_id: Optional[int],
                    object_id: Optional[int],
                    exts: Optional[Dict[int, Any]]
                    ) -> Tuple[Optional[int], Optional[str]]:
    """(capture µs since the epoch, source) of one object, or (None,
    None). Precedence: frindell on a wall-clock track, then LOC
    TIMESTAMP without a LOC TIMESCALE (Track Property, or on any object
    of the track when `timing` keeps it), then the bench property 0x20.
    Raises TimestampMalformed."""
    if timing is not None and timing.timescale is not None:
        ts = timing.timestamp(group_id or 0, object_id or 0, exts)
        if ts is not None and timing._wall:
            return ((timing.origin + ts) * 1_000_000 // timing.timescale,
                    SOURCE_FRINDELL)
    elif exts and not TIMING_IDS.isdisjoint(exts):
        check_object_properties(exts)
        raise TimestampMalformed("OBJECT_TIMESTAMP without TIMESCALE")
    if not exts:
        return None, None
    if LOC_TIMESCALE in exts:
        if timing is not None:
            timing.loc_timescale = exts[LOC_TIMESCALE]
            timing.plain = False
    elif timing is None or timing.loc_timescale is None:
        ts = exts.get(LOC_TIMESTAMP)
        if ts is not None:
            return ts, SOURCE_LOC
    ts = exts.get(BENCH_TIMESTAMP)
    return (ts, SOURCE_BENCH) if ts is not None else (None, None)


TIMESTAMP_MODES = ('loc', 'frindell', 'both')


class TimestampWriter:
    """Timing properties for a generated track (`mode` loc, frindell or
    both; loc is LOC TIMESTAMP 0x10 alone).

    Paced (`rate` > 0): TIMESCALE in ticks that divide both a µs and the
    object period exactly, TIMESTAMP_MAPPING (first group, wall clock at
    start, group period, object period) and per object a zig-zag
    OBJECT_TIMESTAMP correction of actual minus scheduled send time,
    omitted when 0. Unpaced: Appendix A.2, TIMESCALE 1e6 and an explicit
    OBJECT_TIMESTAMP per object. No CLOCK_ID or TIMESTAMP_ORIGIN: the
    wall clock anchor is the mapping's Base Timestamp."""

    def __init__(self, mode: str, rate: float, group_size: int,
                 first_group: int, wall_us: int):
        if mode not in TIMESTAMP_MODES:
            raise ValueError(f"timestamps {mode!r}: one of "
                             f"{', '.join(TIMESTAMP_MODES)}")
        self.loc = mode in ('loc', 'both')
        self.frindell = mode in ('frindell', 'both')
        self.mapping: Optional[Tuple[int, int, int, int]] = None
        self.timescale = 1_000_000
        self.wall_us = wall_us
        if rate > 0:
            r = Fraction(rate).limit_denominator(1001)
            p, q = r.numerator, r.denominator
            self.timescale = 1_000_000 * p // gcd(1_000_000 * q, p)
            om = self.timescale * q // p
            self.mapping = (first_group, wall_us * (self.timescale
                                                   // 1_000_000),
                            om * group_size, om)
        self._k = self.timescale // 1_000_000

    def track_properties(self, vi64: bool) -> Dict[int, Any]:
        if not self.frindell:
            return {}
        props: Dict[int, Any] = {TIMESCALE: self.timescale}
        if self.mapping is not None:
            props[TIMESTAMP_MAPPING] = encode_mapping(*self.mapping, vi64)
        return props

    def scheduled_us(self, group_id: int, object_id: int) -> Optional[int]:
        """Wall-clock µs the mapping schedules an object at."""
        m = self.mapping
        if m is None:
            return None
        return (m[1] + (group_id - m[0]) * m[2]
                + object_id * m[3]) // self._k

    def next_group(self, now_us: int, at_least: int) -> int:
        """The first group, at or after `at_least`, not yet due."""
        m = self.mapping
        if m is None:
            return at_least
        ahead = now_us * self._k - m[1]
        due = m[0] + max(0, -(-ahead // m[2]))
        return max(at_least, due)

    def stamp(self, group_id: int, object_id: int) -> Dict[int, Any]:
        """Object Properties for an object sent now."""
        now = int(time.time() * 1_000_000)
        exts: Dict[int, Any] = {LOC_TIMESTAMP: now} if self.loc else {}
        if not self.frindell:
            return exts
        m = self.mapping
        if m is None:
            exts[OBJECT_TIMESTAMP] = now
            return exts
        c = now * self._k - (m[1] + (group_id - m[0]) * m[2]
                             + object_id * m[3])
        if c:
            exts[OBJECT_TIMESTAMP] = (c << 1) ^ (c >> 63)
        return exts


class MediaTimestamps:
    """Timing properties for a media track whose objects carry known
    capture times (wall-clock µs).

    On a fixed cadence (`group_multiplier` given): TIMESCALE and
    TIMESTAMP_MAPPING anchored at `wall0_us`, and OBJECT_TIMESTAMP only
    on an object off schedule. Otherwise Appendix A.2: TIMESCALE 1e6 and
    each object's capture time as OBJECT_TIMESTAMP. No CLOCK_ID or
    TIMESTAMP_ORIGIN: these are wall-clock tracks. `wall0_us` times the
    timescale should be a whole number of µs·ticks/1e6, or every mapped
    time is off by the remainder."""

    def __init__(self, wall0_us: int, timescale: int = 1_000_000,
                 group_multiplier: Optional[int] = None,
                 object_multiplier: int = 0):
        self.wall0_us = wall0_us
        self.mapped = group_multiplier is not None
        self.timescale = timescale if self.mapped else 1_000_000
        self.group_multiplier = group_multiplier
        self.object_multiplier = object_multiplier
        self.base = wall0_us * self.timescale // 1_000_000

    def track_properties(self, vi64: bool) -> Dict[int, Any]:
        props: Dict[int, Any] = {TIMESCALE: self.timescale}
        if self.mapped:
            props[TIMESTAMP_MAPPING] = encode_mapping(
                0, self.base, self.group_multiplier,
                self.object_multiplier, vi64)
        return props

    def _ticks(self, capture_us: int) -> int:
        """Ticks since wall0, rounded to the nearest."""
        d = (capture_us - self.wall0_us) * self.timescale
        return (2 * d + 1_000_000) // 2_000_000

    def group_for(self, capture_us: int) -> Optional[int]:
        """The scheduled group of a group-opening object."""
        if not self.mapped or not self.group_multiplier:
            return None
        g = self.group_multiplier
        return max(0, (2 * self._ticks(capture_us) + g) // (2 * g))

    def object_properties(self, group_id: int, object_id: int,
                          capture_us: int) -> Dict[int, Any]:
        if not self.mapped:
            return {OBJECT_TIMESTAMP: capture_us}
        c = self._ticks(capture_us) - (group_id * self.group_multiplier
                                       + object_id * self.object_multiplier)
        return {OBJECT_TIMESTAMP: (c << 1) ^ (c >> 63)} if c else {}
