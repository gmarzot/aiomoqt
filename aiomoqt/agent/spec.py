"""Declarative, JSON-serializable specs for the agent API.

Field names are the JSON keys, so serialization is identity-shaped and a
model can emit a spec directly. Shapes stay flat and union-free: a mode
string plus optional fields, never a union of variant types. That is where
a hand-rolled schema/validator pair gets expensive, and it is also what an
LLM emits most reliably.

Priority values carry MoQT semantics only (transport §7.1: 0-255, lower
number = higher priority, 0 highest). Transport-specific remapping belongs
where the priority is applied, not here.

Follows the aiomoqt.media.catalog model — dataclass, explicit
to_dict/from_dict, `extra` for round-tripping unknown keys. It differs in
one deliberate way: from_dict is STRICT by default. A catalog parses
someone else's wire data, where msf-01 §5.1 requires ignoring unknown
fields; a spec comes from a model or a config file, where a silently
ignored key is the failure. Pass lenient=True for replay and migration.

This module imports nothing from aiomoqt core. Mapping a spec onto wire
enums lives separately, so draft churn stays in one place.
"""
from __future__ import annotations

import difflib
from dataclasses import (
    MISSING, dataclass, field, fields as _dc_fields, is_dataclass,
)
from typing import (
    Annotated, Any, Dict, List, Optional, Tuple, get_args, get_origin,
    get_type_hints,
)

from .errors import SpecError

SPEC_VERSION = "1"

START_MODES = ("latest", "next_group", "group", "range")
GROUP_ORDERS = ("ascending", "descending", "publisher_default")
ON_FULL = ("drop_oldest", "drop_new", "block", "error")
MAPPINGS = ("per_group", "per_object", "datagram")
DECODES = ("bytes", "text", "json")

DEFAULT_BUFFER = 256
DEFAULT_TIMEOUT_S = 30.0


# -- annotation markers ----------------------------------------------
# Read by both the validator below and the JSON Schema emitter, so a
# constraint is declared once and cannot drift between them.

class Choices:
    __slots__ = ("values",)

    def __init__(self, *values: str) -> None:
        self.values = tuple(values)


class Range:
    __slots__ = ("minimum", "maximum")

    def __init__(self, minimum: Optional[float] = None,
                 maximum: Optional[float] = None) -> None:
        self.minimum = minimum
        self.maximum = maximum


class Doc:
    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


# -- shared machinery ------------------------------------------------

def spec_keys(cls: type) -> Tuple[str, ...]:
    """Declared field names, excluding the `extra` bucket."""
    return tuple(f.name for f in _dc_fields(cls) if f.name != "extra")


def markers(cls: type) -> Dict[str, Tuple[Optional[Choices], Optional[Range],
                                          Optional[Doc]]]:
    """Per-field (Choices, Range, Doc) from Annotated metadata."""
    out: Dict[str, Tuple[Any, Any, Any]] = {}
    hints = get_type_hints(cls, include_extras=True)
    for name in spec_keys(cls):
        ch = rng = doc = None
        hint = hints.get(name)
        if get_origin(hint) is Annotated:
            for meta in get_args(hint)[1:]:
                if isinstance(meta, Choices):
                    ch = meta
                elif isinstance(meta, Range):
                    rng = meta
                elif isinstance(meta, Doc):
                    doc = meta
        out[name] = (ch, rng, doc)
    return out


def _unwrap(hint: Any) -> Any:
    """Strip Annotated and Optional down to the bare target type."""
    if get_origin(hint) is Annotated:
        hint = get_args(hint)[0]
    if get_origin(hint) is not None and type(None) in get_args(hint):
        rest = [a for a in get_args(hint) if a is not type(None)]
        if len(rest) == 1:
            return _unwrap(rest[0])
    return hint


def _coerce(value: Any, target: Any, where: str) -> Any:
    """Closed, listed coercions. Anything else is an error, not a guess.

    Models emit "128" and "true"; accepting those is worth 40 lines. Wide
    quiet coercion is how validation hides bugs, so the list stays short.
    """
    origin = get_origin(target)
    if origin in (list, List):
        inner = (get_args(target) or (Any,))[0]
        items = value if isinstance(value, list) else [value]
        return [_coerce(v, inner, where) for v in items]
    if target is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise SpecError(f"{where}: expected a boolean, got {value!r}")
    if target is int:
        if isinstance(value, bool):
            raise SpecError(f"{where}: expected an integer, got a boolean")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value, 10)
            except ValueError:
                pass
        raise SpecError(f"{where}: expected an integer, got {value!r}")
    if target is float:
        if isinstance(value, bool):
            raise SpecError(f"{where}: expected a number, got a boolean")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                pass
        raise SpecError(f"{where}: expected a number, got {value!r}")
    if target is str:
        if isinstance(value, str):
            return value
        raise SpecError(f"{where}: expected a string, got {value!r}")
    return value


def _validate(cls: type, name: str, value: Any, lenient: bool) -> Any:
    ch, rng, _ = markers(cls).get(name, (None, None, None))
    where = f"{cls.__name__}.{name}"
    if value is None:
        return None
    if ch is not None and value not in ch.values:
        raise SpecError(
            f"{where}: {value!r} is not one of {', '.join(ch.values)}")
    if rng is not None and isinstance(value, (int, float)):
        lo, hi = rng.minimum, rng.maximum
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            if not lenient:
                raise SpecError(f"{where}: {value} outside {lo}..{hi}")
            value = min(hi, max(lo, value)) if lo is not None and hi is not None else value
    return value


def to_dict(obj: Any) -> Dict[str, Any]:
    """Field-driven serialization. None is omitted; `extra` merges last."""
    out: Dict[str, Any] = {}
    for name in spec_keys(type(obj)):
        value = getattr(obj, name)
        if value is None:
            continue
        if is_dataclass(value):
            value = to_dict(value)
            if not value:  # an all-default nested spec carries no information
                continue
        elif isinstance(value, list):
            value = [to_dict(v) if is_dataclass(v) else v for v in value]
        elif isinstance(value, dict):
            value = {k: (to_dict(v) if is_dataclass(v) else v)
                     for k, v in value.items()}
        out[name] = value
    extra = getattr(obj, "extra", None)
    if extra:
        out.update(extra)
    return out


def from_dict(cls: type, data: Dict[str, Any], *, lenient: bool = False) -> Any:
    """Build `cls` from a plain dict.

    Strict by default: an unknown key raises, with a spelling suggestion
    when one is close. lenient=True routes unknowns to `extra` instead.
    """
    if not isinstance(data, dict):
        raise SpecError(f"{cls.__name__}: expected an object, got {data!r}")

    keys = spec_keys(cls)
    hints = get_type_hints(cls, include_extras=True)
    unknown = [k for k in data if k not in keys]
    if unknown and not lenient:
        hint = ""
        near = difflib.get_close_matches(unknown[0], keys, n=1, cutoff=0.6)
        if near:
            hint = f" (did you mean {near[0]!r}?)"
        raise SpecError(
            f"{cls.__name__}: unknown field{'s' if len(unknown) > 1 else ''} "
            f"{', '.join(repr(k) for k in unknown)}{hint}. "
            f"Known fields: {', '.join(keys)}")

    kwargs: Dict[str, Any] = {}
    for name in keys:
        if name not in data:
            continue
        target = _unwrap(hints.get(name, Any))
        value = data[name]
        if is_dataclass(target) and isinstance(value, dict):
            value = from_dict(target, value, lenient=lenient)
        elif get_origin(target) in (list, List):
            inner = (get_args(target) or (Any,))[0]
            inner_bare = _unwrap(inner)
            if is_dataclass(inner_bare):
                value = [from_dict(inner_bare, v, lenient=lenient)
                         if isinstance(v, dict) else v for v in value]
            else:
                value = _coerce(value, target, f"{cls.__name__}.{name}")
        else:
            value = _coerce(value, target, f"{cls.__name__}.{name}")
        kwargs[name] = _validate(cls, name, value, lenient)

    missing = [f.name for f in _dc_fields(cls)
               if f.name not in kwargs and f.default is MISSING
               and f.default_factory is MISSING]
    if missing:
        raise SpecError(
            f"{cls.__name__}: missing required field"
            f"{'s' if len(missing) > 1 else ''} "
            f"{', '.join(repr(k) for k in missing)}")

    if lenient and unknown:
        kwargs["extra"] = {k: data[k] for k in unknown}
    return cls(**kwargs)


# -- specs -----------------------------------------------------------

@dataclass(frozen=True)
class TrackRef:
    """Where a track lives."""
    namespace: Annotated[str, Doc(
        "MoQT track namespace, e.g. 'live/cam-1'")]
    name: Annotated[Optional[str], Doc(
        "Track name. Omit to discover tracks under the namespace")] = None
    relay: Annotated[Optional[str], Doc(
        "moqt:// (raw QUIC) or https:// (WebTransport) URL. Omit to use "
        "the session already open")] = None

    def __str__(self) -> str:
        return f"{self.namespace}/{self.name}" if self.name else self.namespace

    @classmethod
    def parse(cls, text: str) -> "TrackRef":
        """'ns/track', or a relay URL with the namespace and track in its path."""
        relay = None
        rest = text
        for scheme in ("moqt://", "https://", "http://"):
            if text.startswith(scheme):
                body = text[len(scheme):]
                host, _, path = body.partition("/")
                if not path:
                    raise SpecError(
                        f"TrackRef.parse: {text!r} names a relay but no track")
                relay, rest = f"{scheme}{host}", path
                break
        namespace, sep, name = rest.rpartition("/")
        if not sep:
            namespace, name = rest, None
        if not namespace:
            raise SpecError(f"TrackRef.parse: {text!r} has no namespace")
        return cls(namespace=namespace, name=name or None, relay=relay)

    def to_dict(self) -> Dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any], *, lenient: bool = False) -> "TrackRef":
        return from_dict(cls, d, lenient=lenient)


@dataclass(frozen=True)
class StartAt:
    """Where a subscription or fetch begins.

    Flat by design: a mode plus the fields that mode uses. The wire
    filter type is derived at the transport boundary, not stored here.
    """
    mode: Annotated[str, Choices(*START_MODES), Doc(
        "latest = newest object; next_group = next group boundary; "
        "group = an absolute group; range = a bounded group range")] = "latest"
    group: Annotated[Optional[int], Range(0, None), Doc(
        "First group, for mode 'group' or 'range'")] = None
    object: Annotated[Optional[int], Range(0, None), Doc(
        "First object within `group`. Defaults to 0")] = None
    end_group: Annotated[Optional[int], Range(0, None), Doc(
        "Last group, for mode 'range'")] = None

    def __post_init__(self) -> None:
        if self.mode == "group" and self.group is None:
            raise SpecError("StartAt: mode 'group' needs `group`")
        if self.mode == "range":
            if self.group is None or self.end_group is None:
                raise SpecError(
                    "StartAt: mode 'range' needs `group` and `end_group`")
            if self.end_group < self.group:
                raise SpecError(
                    f"StartAt: end_group {self.end_group} precedes "
                    f"group {self.group}")
        if self.mode in ("latest", "next_group"):
            for unused in ("group", "object", "end_group"):
                if getattr(self, unused) is not None:
                    raise SpecError(
                        f"StartAt: mode {self.mode!r} does not use `{unused}`")

    @classmethod
    def latest(cls) -> "StartAt":
        return cls()

    @classmethod
    def next_group(cls) -> "StartAt":
        return cls(mode="next_group")

    @classmethod
    def at(cls, group: int, object: int = 0) -> "StartAt":
        return cls(mode="group", group=group, object=object)

    @classmethod
    def range(cls, group: int, end_group: int) -> "StartAt":
        return cls(mode="range", group=group, end_group=end_group)

    def to_dict(self) -> Dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any], *, lenient: bool = False) -> "StartAt":
        return from_dict(cls, d, lenient=lenient)


@dataclass(frozen=True)
class Priority:
    """MoQT priority and ordering (transport §7.1).

    Lower number = higher priority; 0 is highest. These are wire values
    only — how a sender schedules against them is a transport concern.
    """
    subscriber: Annotated[Optional[int], Range(0, 255), Doc(
        "Subscriber priority 0-255, lower = more urgent")] = None
    publisher: Annotated[Optional[int], Range(0, 255), Doc(
        "Default publisher priority 0-255, lower = more urgent")] = None
    group_order: Annotated[Optional[str], Choices(*GROUP_ORDERS), Doc(
        "Delivery order across groups")] = None
    delivery_timeout_ms: Annotated[Optional[int], Range(0, None), Doc(
        "Staleness budget in ms. ADVISORY: optional in the spec, 0 means "
        "unset, and relays differ in whether they act on it")] = None

    def to_dict(self) -> Dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any], *, lenient: bool = False) -> "Priority":
        return from_dict(cls, d, lenient=lenient)


@dataclass
class SubscribeSpec:
    """A complete, serializable subscription request."""
    track: TrackRef
    start_at: StartAt = field(default_factory=StartAt)
    priority: Priority = field(default_factory=Priority)
    forward: Annotated[bool, Doc(
        "Ask the publisher to forward live objects")] = True
    buffer: Annotated[int, Range(1, 65536), Doc(
        "Reader ring depth in objects")] = DEFAULT_BUFFER
    on_full: Annotated[str, Choices(*ON_FULL), Doc(
        "What happens when the reader ring fills")] = "drop_oldest"
    decode: Annotated[str, Choices(*DECODES), Doc(
        "How object payloads are surfaced")] = "bytes"
    timeout_s: Annotated[float, Range(0, None), Doc(
        "Default deadline for operations on this subscription")] = DEFAULT_TIMEOUT_S
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any], *,
                  lenient: bool = False) -> "SubscribeSpec":
        return from_dict(cls, d, lenient=lenient)


@dataclass
class PublishSpec:
    """A complete, serializable publish request."""
    track: TrackRef
    priority: Priority = field(default_factory=Priority)
    mapping: Annotated[str, Choices(*MAPPINGS), Doc(
        "Stream mapping: one stream per group, per object, or datagrams")] = "per_group"
    group_size: Annotated[Optional[int], Range(1, None), Doc(
        "Objects per group. Omit to control grouping explicitly")] = None
    announce: Annotated[bool, Doc(
        "Publish the namespace before serving the track")] = True
    buffer: Annotated[int, Range(1, 65536), Doc(
        "Writer ring depth in objects")] = DEFAULT_BUFFER
    on_full: Annotated[str, Choices(*ON_FULL), Doc(
        "What happens when the writer ring fills")] = "block"
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any], *,
                  lenient: bool = False) -> "PublishSpec":
        return from_dict(cls, d, lenient=lenient)
