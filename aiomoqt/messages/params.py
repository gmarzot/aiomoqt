"""Message Parameter tables and value codecs for draft-20 and draft-22.

d22 differs from d20 only in LOCATION_FILTER (a typed form instead of a
Length) and in TRACK_NAMESPACE_PREFIX (a bare Track Namespace instead of a
length-prefixed one). DraftProfile.param_table
selects the table; DraftProfile.location_filter_typed selects the
LOCATION_FILTER form.
"""
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict, Iterable, Optional, Tuple

from ..context import DraftProfile
from ..types import D18_PARAM_KINDS, D18_PARAM_SCOPE, MOQTProtocolViolation, ParamType
from ..utils.buffer import Buffer, BufferReadError

__all__ = [
    'D20ParamType', 'D20RequestErrorCode', 'LocationFilterType',
    'LocationFilter', 'RangeFilter', 'param_kinds', 'param_scope',
    'uses_location_filter', 'check_param_scope', 'range_filter_error',
    'range_filter_count', 'fill_range', 'param_name', 'RangeFilters',
    'merge_range_filters',
]

U64_MAX = (1 << 64) - 1


class D20ParamType(IntEnum):
    """Message Parameters new or redefined in d20 (§10.2, §15.7)."""
    FILL_TIMEOUT = 0x0A
    LOCATION_FILTER = 0x21
    FILL_PARAMETERS = 0x23
    SUBGROUP_FILTER = 0x25
    OBJECTID_FILTER = 0x26
    PRIORITY_FILTER = 0x27
    OBJECT_PROPERTY_FILTER = 0x28
    TRACK_PROPERTY_FILTER = 0x29
    INCLUDE_PROPERTIES = 0x35


class D20RequestErrorCode(IntEnum):
    """REQUEST_ERROR codes added in d19/d20 (§15.11.2)."""
    CONFLICTING_FILTERS = 0x35
    INVALID_FILTER = 0x36


RANGE_FILTERS = frozenset(range(0x25, 0x2A))
PROPERTY_FILTERS = frozenset({0x28, 0x29})

_D20_KINDS: Dict[int, str] = {
    0x02: "varint",           # OBJECT_DELIVERY_TIMEOUT
    0x03: "bytes",            # AUTHORIZATION_TOKEN
    0x04: "varint",           # RENDEZVOUS_TIMEOUT
    0x06: "varint",           # SUBGROUP_DELIVERY_TIMEOUT
    0x08: "varint",           # EXPIRES
    0x09: "location",         # LARGEST_OBJECT
    0x0A: "varint",           # FILL_TIMEOUT
    0x10: "uint8",            # FORWARD
    0x20: "uint8",            # SUBSCRIBER_PRIORITY
    0x21: "location_filter",  # LOCATION_FILTER
    0x22: "uint8",            # GROUP_ORDER
    0x23: "fill",             # FILL_PARAMETERS: length-prefixed parameters
    0x25: "range_filter",     # SUBGROUP_FILTER
    0x26: "range_filter",     # OBJECTID_FILTER
    0x27: "range_filter",     # PRIORITY_FILTER
    0x28: "range_filter",     # OBJECT_PROPERTY_FILTER
    0x29: "range_filter",     # TRACK_PROPERTY_FILTER
    0x32: "varint",           # NEW_GROUP_REQUEST
    0x34: "tuple",            # TRACK_NAMESPACE_PREFIX, length-prefixed
    0x35: "uint8",            # INCLUDE_PROPERTIES
}
# d22 §9.20 lists Track Namespace as its own encoding: no outer Length.
_D22_KINDS: Dict[int, str] = {**_D20_KINDS, 0x34: "namespace"}

_SUBSCRIBE = frozenset({0x02, 0x03, 0x04, 0x06, 0x10, 0x20, 0x21, 0x22, 0x23,
                        0x25, 0x26, 0x27, 0x28, 0x32, 0x35})
_UPDATE_SUBSCRIPTION = frozenset({0x02, 0x03, 0x06, 0x10, 0x20, 0x21, 0x23,
                                  0x25, 0x26, 0x27, 0x28, 0x32})
_UPDATE_FETCH = frozenset({0x03, 0x20})
_UPDATE_PUBLISH_NAMESPACE = frozenset({0x03})
_UPDATE_SUBSCRIBE_NAMESPACE = frozenset({0x03, 0x34})
_UPDATE_SUBSCRIBE_TRACKS = frozenset({0x03, 0x10, 0x29, 0x34})

# Keyed by message class name. Composite keys name the request a
# REQUEST_UPDATE or REQUEST_OK belongs to; the class-name entries are the
# union, applied at decode before the request is known.
_D20_SCOPE: Dict[str, frozenset] = {
    "Subscribe": _SUBSCRIBE,
    "SubscribeOk": frozenset({0x08, 0x09}),
    "Publish": frozenset({0x02, 0x03, 0x06, 0x08, 0x09, 0x10, 0x20, 0x21,
                          0x22}),
    "PublishOk": frozenset({0x08}),
    "PublishStateNotify": frozenset({0x09, 0x10, 0x21}),
    "Fetch": frozenset({0x03, 0x0A, 0x20, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28,
                        0x35}),
    "FetchOk": frozenset(),
    "TrackStatus": frozenset({0x03, 0x35}),
    "PublishNamespace": frozenset({0x03}),
    "SubscribeNamespace": frozenset({0x03}),
    # §10.20.1 (d22 §3.6.2): any SUBSCRIBE parameter, plus the Track
    # Property filter. d22 §9.18 lists fewer; its own §3.6.2 keeps this.
    "SubscribeTracks": _SUBSCRIBE | {0x29},
    "RequestUpdate": (_UPDATE_SUBSCRIPTION | _UPDATE_FETCH
                      | _UPDATE_SUBSCRIBE_NAMESPACE
                      | _UPDATE_SUBSCRIBE_TRACKS),
    "RequestOk": frozenset({0x08, 0x09}),
    "FillParameters": frozenset({0x0A, 0x20, 0x21, 0x22, 0x25, 0x26, 0x27,
                                 0x28}),
    "RequestUpdate:Subscription": _UPDATE_SUBSCRIPTION,
    "RequestUpdate:Fetch": _UPDATE_FETCH,
    "RequestUpdate:PublishNamespace": _UPDATE_PUBLISH_NAMESPACE,
    "RequestUpdate:SubscribeNamespace": _UPDATE_SUBSCRIBE_NAMESPACE,
    "RequestUpdate:SubscribeTracks": _UPDATE_SUBSCRIBE_TRACKS,
    "RequestOk:Publish": frozenset({0x08}),
    "RequestOk:RequestUpdate": frozenset({0x08, 0x09}),
    "RequestOk:TrackStatus": frozenset({0x09}),
    "RequestOk:SubscribeNamespace": frozenset({0x08}),
    "RequestOk:SubscribeTracks": frozenset({0x08}),
    "RequestOk:PublishNamespace": frozenset({0x08}),
}

_KINDS = {18: D18_PARAM_KINDS, 20: _D20_KINDS, 22: _D22_KINDS}
_SCOPES = {18: D18_PARAM_SCOPE, 20: _D20_SCOPE, 22: _D20_SCOPE}

# Parameters a message may carry more than once (§10.2.2, §5.1.4).
REPEATABLE = frozenset({0x03}) | RANGE_FILTERS

# Restricted values, closed with PROTOCOL_VIOLATION (§10.2.18, §10.2.21).
VALUE_RANGES = {0x35: (0, 1)}


# Message Parameter names from d18 (§15.7); earlier drafts use ParamType.
_D18_NAMES: Dict[int, str] = {
    0x02: "OBJECT_DELIVERY_TIMEOUT",
    0x03: "AUTHORIZATION_TOKEN",
    0x04: "RENDEZVOUS_TIMEOUT",
    0x06: "SUBGROUP_DELIVERY_TIMEOUT",
    0x08: "EXPIRES",
    0x09: "LARGEST_OBJECT",
    0x0A: "FILL_TIMEOUT",
    0x10: "FORWARD",
    0x20: "SUBSCRIBER_PRIORITY",
    0x21: "SUBSCRIPTION_FILTER",
    0x22: "GROUP_ORDER",
    0x32: "NEW_GROUP_REQUEST",
    0x34: "TRACK_NAMESPACE_PREFIX",
}
_D20_NAMES: Dict[int, str] = {**_D18_NAMES,
                              **{int(p): p.name for p in D20ParamType}}
_NAMES = {18: _D18_NAMES, 20: _D20_NAMES, 22: _D20_NAMES}


def param_name(ptype: int, prof: Optional[DraftProfile] = None) -> str:
    """Display name of a Message Parameter at `prof`'s draft (ParamType
    names when None or pre-d18); hex when the draft defines none."""
    if prof is not None and prof.vi64:
        name = _NAMES[prof.param_table].get(ptype)
    else:
        try:
            name = ParamType(ptype).name
        except ValueError:
            name = None
    return name or f"0x{ptype:02x}"


def param_kinds(prof: DraftProfile) -> Dict[int, str]:
    """Value encoding of each Message Parameter the draft defines."""
    return _KINDS[prof.param_table]


def param_scope(prof: DraftProfile) -> Dict[str, frozenset]:
    """The parameters each message may carry."""
    return _SCOPES[prof.param_table]


def uses_location_filter(prof: DraftProfile) -> bool:
    """True when 0x21 is LOCATION_FILTER (d20+), not SUBSCRIPTION_FILTER."""
    return prof.draft >= 18 and prof.param_table >= 20


def check_param_scope(params: Iterable[int], scope: str, *,
                      prof: DraftProfile) -> None:
    """Close on a parameter outside `scope` (§10.2.1). Unknown scopes
    and drafts before d20 are not checked."""
    if prof.draft < 18 or prof.param_table < 20:
        return
    allowed = _D20_SCOPE.get(scope)
    if allowed is None:
        return
    for ptype in params:
        if ptype not in allowed:
            raise MOQTProtocolViolation(
                f"parameter 0x{ptype:x} not allowed in {scope}")


class LocationFilterType(IntEnum):
    """Location Filter forms, numbered as d22 Table 6. d20 expresses
    each by its field count instead."""
    NONE = 0x00
    RELATIVE_START = 0x01
    ABSOLUTE_START = 0x02
    ABSOLUTE_GROUP_END = 0x03
    ABSOLUTE_RANGE = 0x04
    NEXT_OBJECT = 0x05


_START = ("start_group", "start_object")
_FIELDS = {
    LocationFilterType.NONE: (),
    LocationFilterType.RELATIVE_START: ("start_group",),
    LocationFilterType.ABSOLUTE_START: _START,
    LocationFilterType.ABSOLUTE_GROUP_END: _START + ("end_group",),
    LocationFilterType.ABSOLUTE_RANGE: _START + ("end_group", "end_object"),
    LocationFilterType.NEXT_OBJECT: (),
}


@dataclass(frozen=True, slots=True)
class LocationFilter:
    """A Location Filter (§5.1.2). start_group is a relative group count
    for RELATIVE_START and absolute otherwise; end_group is absolute
    (the wire carries it as a delta from start_group); end_object is
    inclusive."""
    type: LocationFilterType = LocationFilterType.NONE
    start_group: Optional[int] = None
    start_object: Optional[int] = None
    end_group: Optional[int] = None
    end_object: Optional[int] = None

    def __post_init__(self):
        ftype = LocationFilterType(self.type)
        object.__setattr__(self, 'type', ftype)
        need = _FIELDS[ftype]
        for name in ("start_group", "start_object", "end_group",
                     "end_object"):
            value = getattr(self, name)
            if (value is None) == (name in need):
                verb = "needs" if value is None else "has no"
                raise ValueError(f"{ftype.name} filter {verb} {name}")
            if value is not None and not 0 <= value <= U64_MAX:
                raise ValueError(f"{name} {value} outside 0..2^64-1")
        if self.end_group is not None and self.end_group < self.start_group:
            raise ValueError(
                f"end_group {self.end_group} < start_group {self.start_group}")

    @classmethod
    def none(cls) -> 'LocationFilter':
        return cls(LocationFilterType.NONE)

    @classmethod
    def next_object(cls) -> 'LocationFilter':
        return cls(LocationFilterType.NEXT_OBJECT)

    @classmethod
    def relative(cls, groups: int) -> 'LocationFilter':
        """Start `groups` before the Next Group: 0 is the Next Group, 1
        the current one."""
        return cls(LocationFilterType.RELATIVE_START, start_group=groups)

    @classmethod
    def absolute(cls, start_group: int, start_object: int = 0,
                 end_group: Optional[int] = None,
                 end_object: Optional[int] = None) -> 'LocationFilter':
        if end_group is None:
            if end_object is not None:
                raise ValueError("end_object needs end_group")
            return cls(LocationFilterType.ABSOLUTE_START, start_group,
                       start_object)
        if end_object is None:
            return cls(LocationFilterType.ABSOLUTE_GROUP_END, start_group,
                       start_object, end_group)
        return cls(LocationFilterType.ABSOLUTE_RANGE, start_group,
                   start_object, end_group, end_object)

    def _fields(self) -> Tuple[int, ...]:
        values = [getattr(self, n) for n in _FIELDS[self.type]]
        if len(values) >= 3:
            values[2] -= self.start_group
        return tuple(values)

    def encode(self, buf: Buffer, *, prof: DraftProfile) -> None:
        """Write the parameter value (after the Type Delta)."""
        if prof.location_filter_typed:
            buf.push_vint(int(self.type))
            for v in self._fields():
                buf.push_vint(v)
            return
        if self.type == LocationFilterType.NEXT_OBJECT:
            fields: Tuple[int, ...] = (0, 0)
        elif (self.type == LocationFilterType.ABSOLUTE_START
              and self.start_group == 0 and self.start_object == 0):
            # Two zeros mean Next Object here; an open filter from {0, 0}
            # is the unfiltered one (§5.1.2).
            fields = ()
        else:
            fields = self._fields()
        inner = Buffer(capacity=64, vi64=prof.vi64)
        for v in fields:
            inner.push_vint(v)
        buf.push_vint(inner.tell())
        buf.push_bytes(inner.data_slice(0, inner.tell()))

    @classmethod
    def decode(cls, buf: Buffer, *, prof: DraftProfile,
               buf_end: Optional[int] = None) -> 'LocationFilter':
        """Read a parameter value. A malformed one closes the session."""
        try:
            if prof.location_filter_typed:
                raw = buf.pull_vint()
                if raw > LocationFilterType.NEXT_OBJECT:
                    raise MOQTProtocolViolation(
                        f"unknown Location Filter Type 0x{raw:x}")
                ftype = LocationFilterType(raw)
                fields = [buf.pull_vint() for _ in _FIELDS[ftype]]
                if buf_end is not None and buf.tell() > buf_end:
                    raise MOQTProtocolViolation(
                        "LOCATION_FILTER overruns the message")
            else:
                length = buf.pull_vint()
                end = buf.tell() + length
                if buf_end is not None and end > buf_end:
                    raise MOQTProtocolViolation(
                        f"LOCATION_FILTER length {length} overruns the "
                        f"message")
                fields = []
                while buf.tell() < end:
                    if len(fields) == 4:
                        raise MOQTProtocolViolation(
                            "LOCATION_FILTER has more than four fields")
                    fields.append(buf.pull_vint())
                if buf.tell() != end:
                    raise MOQTProtocolViolation(
                        "LOCATION_FILTER field crosses its length")
                ftype = (LocationFilterType.NEXT_OBJECT if fields == [0, 0]
                         else LocationFilterType(len(fields)))
                if ftype == LocationFilterType.NEXT_OBJECT:
                    fields = []
        except BufferReadError:
            raise MOQTProtocolViolation("LOCATION_FILTER truncated") from None
        if len(fields) >= 3:
            fields[2] += fields[0]
            if fields[2] > U64_MAX:
                raise MOQTProtocolViolation(
                    f"LOCATION_FILTER end group {fields[2]} exceeds 2^64-1")
        return cls(ftype, *fields)

    # -- the d14-d18 field forms the message classes also expose --------

    @classmethod
    def from_subscription_filter(cls, filter_type: int,
                                 start_group: Optional[int] = None,
                                 start_object: Optional[int] = None,
                                 end_group: Optional[int] = None,
                                 ) -> 'LocationFilter':
        """The d14-d18 Filter Type 0x1-0x4 as a Location Filter."""
        if filter_type == 0x1:
            return cls.relative(0)
        if filter_type == 0x2:
            return cls.next_object()
        if filter_type == 0x3:
            return cls.absolute(start_group or 0, start_object or 0)
        if filter_type == 0x4:
            return cls.absolute(start_group or 0, start_object or 0,
                                end_group if end_group is not None else 0)
        raise ValueError(f"unknown subscription filter type {filter_type}")

    def subscription_filter(self) -> Optional[Tuple[Optional[int], ...]]:
        """(filter_type, start_group, start_object, end_group) in the
        d14-d18 form, None when no Filter Type says the same. The
        unfiltered form is (None, None, None, None)."""
        t = self.type
        if t == LocationFilterType.NONE:
            return (None, None, None, None)
        if t == LocationFilterType.RELATIVE_START and self.start_group == 0:
            return (0x1, None, None, None)
        if t == LocationFilterType.NEXT_OBJECT:
            return (0x2, None, None, None)
        if t == LocationFilterType.ABSOLUTE_START:
            return (0x3, self.start_group, self.start_object, None)
        if t == LocationFilterType.ABSOLUTE_GROUP_END:
            return (0x4, self.start_group, self.start_object, self.end_group)
        return None

    @classmethod
    def from_fetch_range(cls, start_group: Optional[int],
                         start_object: Optional[int],
                         end_group: Optional[int],
                         end_object: Optional[int]) -> 'LocationFilter':
        """A d14-d18 FETCH range: End Location is the last Object plus
        one, and Object 0 asks for the whole End Group. No End means up
        to Largest Object."""
        sg, so = start_group or 0, start_object or 0
        if end_group is None:
            return cls.absolute(sg, so)
        if not end_object:
            return cls.absolute(sg, so, end_group)
        return cls.absolute(sg, so, end_group, end_object - 1)

    def fetch_range(self) -> Optional[Tuple[int, int, Optional[int],
                                            Optional[int]]]:
        """(start_group, start_object, end_group, end_object) in the
        d14-d18 FETCH form, None for the forms relative to Largest
        Object. No End is (.., None, None)."""
        t = self.type
        if t == LocationFilterType.NONE:
            return (0, 0, None, None)
        if t == LocationFilterType.ABSOLUTE_START:
            return (self.start_group, self.start_object, None, None)
        if t == LocationFilterType.ABSOLUTE_GROUP_END:
            return (self.start_group, self.start_object, self.end_group, 0)
        if t == LocationFilterType.ABSOLUTE_RANGE:
            return (self.start_group, self.start_object, self.end_group,
                    self.end_object + 1)
        return None

    def resolve(self, largest: Optional[Tuple[int, int]], *,
                fetch: bool = False
                ) -> Tuple[Tuple[int, int], Optional[Tuple[int, int]]]:
        """(start, end) Locations, both inclusive, against the Largest
        Object (None when nothing was published). end is None for an
        open-ended subscription; a FETCH ends at most at Largest Object
        (§5.1.2). A whole End Group ends at (group, 2^64-1)."""
        t = self.type
        if t == LocationFilterType.RELATIVE_START:
            group = 0 if largest is None else largest[0] + 1
            start = (max(0, group - self.start_group), 0)
        elif t == LocationFilterType.NEXT_OBJECT:
            start = (0, 0) if largest is None else (largest[0],
                                                    largest[1] + 1)
        elif t == LocationFilterType.NONE:
            start = (0, 0)
        else:
            start = (self.start_group, self.start_object)
        if t == LocationFilterType.ABSOLUTE_GROUP_END:
            end: Optional[Tuple[int, int]] = (self.end_group, U64_MAX)
        elif t == LocationFilterType.ABSOLUTE_RANGE:
            end = (self.end_group, self.end_object)
        else:
            end = None
        if fetch and largest is not None:
            end = largest if end is None else min(end, tuple(largest))
        return start, end


def fill_range(fill: Optional[Dict[int, Any]],
               subscription_filter: Optional[LocationFilter],
               largest: Optional[Tuple[int, int]]
               ) -> Optional[Tuple[Tuple[int, int], Tuple[int, int]]]:
    """(start, end) Locations, both inclusive, a fill fetch stream carries
    (d22 §3.4): the LOCATION_FILTER in FILL_PARAMETERS, else the
    subscription's, read as for a FETCH. None when no fill stream opens:
    nothing published, or the range is empty or starts after Largest
    Object."""
    if largest is None:
        return None
    lf = (fill or {}).get(D20ParamType.LOCATION_FILTER)
    if lf is None:
        lf = subscription_filter or LocationFilter.none()
    start, end = lf.resolve(tuple(largest), fetch=True)
    if end is None or start > end:
        return None
    return start, end


@dataclass(frozen=True, slots=True)
class RangeFilter:
    """One Range Filter parameter instance (§5.1.4). ranges hold
    inclusive absolute (start, end) pairs; a None end leaves the last
    Range open. set_id None is the zero-length form that removes the
    filter in REQUEST_UPDATE. property_type belongs to the Object and
    Track Property filters (0x28, 0x29) only."""
    set_id: Optional[int] = 0
    ranges: Tuple[Tuple[int, Optional[int]], ...] = ()
    property_type: Optional[int] = None

    def encode(self, buf: Buffer, ptype: int, *, prof: DraftProfile) -> None:
        inner = Buffer(capacity=1024, vi64=prof.vi64)
        if self.set_id is not None:
            if not 0 <= self.set_id <= 255:
                raise ValueError(f"SetID {self.set_id} outside 0..255")
            inner.push_uint8(self.set_id)
            if ptype in PROPERTY_FILTERS:
                if self.property_type is None:
                    raise ValueError(
                        f"filter 0x{ptype:x} needs a property_type")
                inner.push_vint(self.property_type)
            elif self.property_type is not None:
                raise ValueError(
                    f"filter 0x{ptype:x} carries no property_type")
            prev = 0
            for i, (start, end) in enumerate(self.ranges):
                if start < prev or (end is not None and end < start):
                    raise ValueError(
                        f"range ({start}, {end}) is not ascending")
                if end is None and i != len(self.ranges) - 1:
                    raise ValueError("only the last Range may be open")
                inner.push_vint(start - prev)
                if end is not None:
                    inner.push_vint(end - start)
                    prev = end
        buf.push_vint(inner.tell())
        buf.push_bytes(inner.data_slice(0, inner.tell()))

    @classmethod
    def decode(cls, buf: Buffer, ptype: int, *, prof: DraftProfile,
               buf_end: Optional[int] = None) -> 'RangeFilter':
        """Read one instance. Malformed bytes close the session; values
        the request must refuse are left to range_filter_error()."""
        try:
            length = buf.pull_vint()
            end = buf.tell() + length
            if buf_end is not None and end > buf_end:
                raise MOQTProtocolViolation(
                    f"range filter 0x{ptype:x} length {length} overruns "
                    f"the message")
            if length == 0:
                return cls(set_id=None)
            set_id = buf.pull_uint8()
            property_type = (buf.pull_vint() if ptype in PROPERTY_FILTERS
                             else None)
            ranges = []
            prev = 0
            while buf.tell() < end:
                start = prev + buf.pull_vint()
                if buf.tell() < end:
                    stop: Optional[int] = start + buf.pull_vint()
                    prev = stop
                else:
                    stop = None
                ranges.append((start, stop))
        except BufferReadError:
            raise MOQTProtocolViolation(
                f"range filter 0x{ptype:x} truncated") from None
        if buf.tell() != end:
            raise MOQTProtocolViolation(
                f"range filter 0x{ptype:x} field crosses its length")
        return cls(set_id=set_id, ranges=tuple(ranges),
                   property_type=property_type)


def _range_filter_items(params: Dict[int, Any]):
    for ptype in sorted(RANGE_FILTERS):
        value = params.get(ptype)
        if value is None:
            continue
        for item in (value if isinstance(value, (list, tuple)) else (value,)):
            yield ptype, item


def range_filter_count(params: Dict[int, Any]) -> int:
    """Ranges across all Range Filter parameters, for MAX_FILTER_RANGES."""
    return sum(len(f.ranges) for _, f in _range_filter_items(params))


def range_filter_error(params: Dict[int, Any], *,
                       max_ranges: Optional[int] = None) -> Optional[str]:
    """Why the request carrying these parameters MUST be refused with
    INVALID_FILTER (§5.1.4), or None. max_ranges is the MAX_FILTER_RANGES
    this endpoint sent (0 if none: no Range allowed); None skips it."""
    seen = set()
    for ptype, f in _range_filter_items(params):
        if f.set_id is None:
            continue
        key = (ptype, f.set_id, f.property_type)
        if key in seen:
            return (f"filter 0x{ptype:x} repeats SetID {f.set_id}"
                    f" and property type {f.property_type}")
        seen.add(key)
        if ptype in PROPERTY_FILTERS and f.property_type % 2:
            return f"filter 0x{ptype:x} names odd property type"
        top = 255 if ptype == 0x27 else U64_MAX
        for start, end in f.ranges:
            if start > top or (end is not None and end > top):
                return f"filter 0x{ptype:x} value exceeds {top}"
    if max_ranges is not None and range_filter_count(params) > max_ranges:
        return f"more than MAX_FILTER_RANGES {max_ranges} ranges"
    return None


def merge_range_filters(current: Dict[int, Any],
                        update: Dict[int, Any]) -> Dict[int, list]:
    """The Range Filter parameters after `update` applies to `current`
    (§3.3.2): a Parameter Type present in `update` replaces that type
    whole, a zero-length one removes it, an omitted one is unchanged.
    Also how FILL_PARAMETERS overrides its subscription (§9.20.15)."""
    out: Dict[int, list] = {}
    for ptype in sorted(RANGE_FILTERS):
        src = update if ptype in update else current
        kept = [f for p, f in _range_filter_items({ptype: src.get(ptype)})
                if f.set_id is not None]
        if kept:
            out[ptype] = kept
    return out


def _in_ranges(f: RangeFilter, value: Any) -> bool:
    if not isinstance(value, int):
        return False
    return any(start <= value and (end is None or value <= end)
               for start, end in f.ranges)


@dataclass(frozen=True, slots=True)
class RangeFilters:
    """The Range Filters of one request (§3.3.2), compiled: the filters
    of a SetID are AND'd, SetIDs OR'd. No sets passes everything."""
    sets: Tuple[Tuple[Tuple[int, RangeFilter], ...], ...] = ()

    @classmethod
    def from_params(cls, params: Optional[Dict[int, Any]]) -> 'RangeFilters':
        by_set: Dict[int, list] = {}
        for ptype, f in _range_filter_items(params or {}):
            if f.set_id is not None:
                by_set.setdefault(f.set_id, []).append((ptype, f))
        return cls(tuple(tuple(v) for _, v in sorted(by_set.items())))

    def __bool__(self) -> bool:
        return bool(self.sets)

    def for_track(self, properties: Optional[Dict[int, Any]]
                  ) -> Optional['RangeFilters']:
        """The object filters left for a track with these Track
        Properties: the sets whose TRACK_PROPERTY_FILTERs it passes,
        without them. None when no set admits the track."""
        if not self.sets:
            return self
        props = properties or {}
        kept = []
        for fs in self.sets:
            if all(_in_ranges(f, props.get(f.property_type))
                   for p, f in fs if p == D20ParamType.TRACK_PROPERTY_FILTER):
                kept.append(tuple((p, f) for p, f in fs
                                  if p != D20ParamType.TRACK_PROPERTY_FILTER))
        if not kept:
            return None
        if not all(kept):
            return RangeFilters()
        return RangeFilters(tuple(kept))

    def admits(self, subgroup_id: int, object_id: int, priority: int,
               properties: Optional[Dict[int, Any]]) -> bool:
        """An object passes (§9.20.10-13). A missing Object Property
        fails its filter; TRACK_PROPERTY_FILTERs are for_track()'s."""
        if not self.sets:
            return True
        props = properties or {}
        for fs in self.sets:
            for ptype, f in fs:
                if ptype == D20ParamType.SUBGROUP_FILTER:
                    value = subgroup_id
                elif ptype == D20ParamType.OBJECTID_FILTER:
                    value = object_id
                elif ptype == D20ParamType.PRIORITY_FILTER:
                    value = priority
                elif ptype == D20ParamType.OBJECT_PROPERTY_FILTER:
                    value = props.get(f.property_type)
                else:
                    continue
                if not _in_ranges(f, value):
                    break
            else:
                return True
        return False
