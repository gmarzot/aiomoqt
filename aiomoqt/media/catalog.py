"""MSF catalog model — draft-ietf-moq-msf-01 §5.

Dataclass field names mirror the spec's JSON keys exactly (camelCase) so
serialization is identity-shaped. Unknown/custom fields round-trip via
`extra` (§5.1: parsers MUST ignore fields they don't understand).

The catalog is itself a MoQT track named "catalog" (§5.2.3, case-
sensitive); wire placement (Object 0 = independent, Objects >= 1 =
deltas, subgroup 0, joining fetch) lives with the publisher/subscriber
integration, not in this model.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field, fields as _dc_fields
from typing import Any, Dict, List, Optional, Tuple

# §5.1.1: a subscriber MUST NOT parse a version it does not understand.
# Spec examples use "1"; the I-D convention is "draft-XX".
MSF_VERSIONS = ("1", "draft-01")

# §5.2.3: the catalog track's Track Name, exact and case-sensitive.
CATALOG_TRACK_NAME = "catalog"

# §5.2.4 packaging values (CMSF adds "cmaf").
PACKAGING_LOC = "loc"
PACKAGING_CMAF = "cmaf"
PACKAGING_MEDIA_TIMELINE = "mediatimeline"
PACKAGING_EVENT_TIMELINE = "eventtimeline"
PACKAGING_MOQLOG = "moqlog"
PACKAGING_MOQMETRICS = "moqmetrics"
_KNOWN_PACKAGING = frozenset({
    PACKAGING_LOC, PACKAGING_CMAF, PACKAGING_MEDIA_TIMELINE,
    PACKAGING_EVENT_TIMELINE, PACKAGING_MOQLOG, PACKAGING_MOQMETRICS,
})

_AV_ROLES = frozenset({"audio", "video"})


class CatalogError(ValueError):
    """Malformed catalog or invalid delta operation."""


@dataclass
class InitData:
    """§5.1.7 initialization reference (type "inline" = base64 data)."""
    id: str
    data: str = ""
    type: str = "inline"

    @classmethod
    def from_bytes(cls, id: str, payload: bytes) -> "InitData":
        return cls(id=id, data=base64.b64encode(payload).decode())

    @property
    def payload(self) -> bytes:
        return base64.b64decode(self.data)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "type": self.type, "data": self.data}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "InitData":
        return cls(id=d["id"], data=d.get("data", ""),
                   type=d.get("type", "inline"))


@dataclass
class CatalogTrack:
    """§5.2 track object. None = omitted from the wire."""
    name: Optional[str] = None
    namespace: Optional[str] = None
    packaging: Optional[str] = None
    eventType: Optional[str] = None
    isLive: Optional[bool] = None
    targetLatency: Optional[int] = None
    buffers: Optional[Dict[str, Any]] = None
    role: Optional[str] = None
    label: Optional[str] = None
    renderGroup: Optional[int] = None
    altGroup: Optional[int] = None
    initRef: Optional[str] = None
    depends: Optional[List[str]] = None
    temporalId: Optional[int] = None
    spatialId: Optional[int] = None
    codec: Optional[str] = None
    mimeType: Optional[str] = None
    framerate: Optional[float] = None
    timescale: Optional[int] = None
    bitrate: Optional[int] = None
    avgBitrate: Optional[int] = None
    maxGopDuration: Optional[int] = None
    maxGroupDuration: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    samplerate: Optional[int] = None
    channelConfig: Optional[str] = None
    displayWidth: Optional[int] = None
    displayHeight: Optional[int] = None
    lang: Optional[str] = None
    parentName: Optional[str] = None
    parentNamespace: Optional[str] = None
    trackDuration: Optional[int] = None
    template: Optional[List[Any]] = None
    authInfo: Optional[Any] = None
    accessibility: Optional[Any] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> Tuple[Optional[str], str]:
        return (self.namespace, self.name)

    def to_dict(self) -> Dict[str, Any]:
        d = {k: v for k in _TRACK_KEYS
             if (v := getattr(self, k)) is not None}
        d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CatalogTrack":
        known = {k: v for k, v in d.items() if k in _TRACK_KEYS}
        extra = {k: v for k, v in d.items() if k not in _TRACK_KEYS}
        return cls(**known, extra=extra)


_TRACK_KEYS = tuple(
    f.name for f in _dc_fields(CatalogTrack) if f.name != "extra")


@dataclass
class DeltaOp:
    """§5.1.6 delta operation: op in {"add", "remove", "clone"}."""
    op: str
    tracks: List[CatalogTrack] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"op": self.op, "tracks": [t.to_dict() for t in self.tracks]}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DeltaOp":
        return cls(op=d.get("op", ""),
                   tracks=[CatalogTrack.from_dict(t)
                           for t in d.get("tracks", [])])


@dataclass
class Catalog:
    """Root catalog (§5.1) — independent, or a delta when deltaUpdate is
    set (a delta carries no version and no tracks, §5.3)."""
    version: Optional[str] = "1"
    generatedAt: Optional[int] = None
    isComplete: Optional[bool] = None
    tracks: List[CatalogTrack] = field(default_factory=list)
    publishTracks: Optional[List[CatalogTrack]] = None
    deltaUpdate: Optional[List[DeltaOp]] = None
    initDataList: Optional[List[InitData]] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_delta(self) -> bool:
        return bool(self.deltaUpdate)

    @classmethod
    def delta(cls, ops: List[DeltaOp],
              generatedAt: Optional[int] = None) -> "Catalog":
        return cls(version=None, generatedAt=generatedAt, deltaUpdate=ops)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        # §5.3: a delta MUST NOT carry version or tracks. §5.1.7:
        # initDataList MUST follow tracks — emitted last.
        if not self.is_delta and self.version is not None:
            d["version"] = self.version
        if self.generatedAt is not None:
            d["generatedAt"] = self.generatedAt
        if self.isComplete:
            d["isComplete"] = True
        if not self.is_delta:
            d["tracks"] = [t.to_dict() for t in self.tracks]
        if self.publishTracks is not None:
            d["publishTracks"] = [t.to_dict() for t in self.publishTracks]
        if self.is_delta:
            d["deltaUpdate"] = [op.to_dict() for op in self.deltaUpdate]
        d.update(self.extra)
        if self.initDataList is not None:
            d["initDataList"] = [i.to_dict() for i in self.initDataList]
        return d

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: Dict[str, Any], *,
                  lenient: bool = False) -> "Catalog":
        known = {"version", "generatedAt", "isComplete", "tracks",
                 "publishTracks", "deltaUpdate", "initDataList"}
        is_delta = bool(d.get("deltaUpdate"))
        version = d.get("version")
        # MSF-00-era catalogs carry numeric versions; same value, same
        # meaning — normalize before the gate.
        if isinstance(version, int):
            version = str(version)
        if not is_delta and version not in MSF_VERSIONS and not lenient:
            raise CatalogError(f"unsupported MSF catalog version: {version!r}")
        return cls(
            version=version,
            generatedAt=d.get("generatedAt"),
            isComplete=d.get("isComplete"),
            tracks=[CatalogTrack.from_dict(t) for t in d.get("tracks", [])],
            publishTracks=(
                [CatalogTrack.from_dict(t) for t in d["publishTracks"]]
                if "publishTracks" in d else None),
            deltaUpdate=(
                [DeltaOp.from_dict(o) for o in d["deltaUpdate"]]
                if is_delta else None),
            initDataList=(
                [InitData.from_dict(i) for i in d["initDataList"]]
                if "initDataList" in d else None),
            extra={k: v for k, v in d.items() if k not in known},
        )

    @classmethod
    def from_json(cls, text: str, *, lenient: bool = False) -> "Catalog":
        try:
            d = json.loads(text)
        except json.JSONDecodeError as e:
            raise CatalogError(f"catalog is not valid JSON: {e}") from e
        if not isinstance(d, dict):
            raise CatalogError("catalog root must be a JSON object")
        return cls.from_dict(d, lenient=lenient)

    # -- track lookup / delta engine ----------------------------------

    def find(self, name: str,
             namespace: Optional[str] = None) -> Optional[CatalogTrack]:
        """Locate a track by name; namespace narrows when given (§5.2.2:
        an absent namespace inherits the catalog's — matched as None)."""
        matches = [t for t in self.tracks if t.name == name
                   and (namespace is None or t.namespace == namespace)]
        if len(matches) > 1:
            raise CatalogError(f"ambiguous track name {name!r}")
        return matches[0] if matches else None

    def apply(self, update: "Catalog") -> "Catalog":
        """Apply a §5.3 delta update in place (ops run sequentially,
        each against the result of the previous). Returns self."""
        if not update.is_delta:
            raise CatalogError("apply() requires a delta update catalog")
        for op in update.deltaUpdate:
            handler = {"add": self._op_add, "remove": self._op_remove,
                       "clone": self._op_clone}.get(op.op)
            if handler is None:
                raise CatalogError(f"unknown delta op: {op.op!r}")
            for t in op.tracks:
                handler(t)
        if update.generatedAt is not None:
            self.generatedAt = update.generatedAt
        if update.initDataList:
            self.initDataList = ((self.initDataList or [])
                                 + update.initDataList)
        return self

    def _op_add(self, t: CatalogTrack) -> None:
        if not t.name:
            raise CatalogError("add: track has no name")
        # §5.3: track attributes are immutable per (namespace, name).
        if any(x.key == t.key for x in self.tracks):
            raise CatalogError(f"add: track {t.key} already declared")
        self.tracks.append(t)

    def _op_remove(self, t: CatalogTrack) -> None:
        found = self.find(t.name, t.namespace)
        if found is None:
            raise CatalogError(f"remove: unknown track {t.key}")
        self.tracks.remove(found)

    def _op_clone(self, t: CatalogTrack) -> None:
        if not t.parentName:
            raise CatalogError("clone: parentName required")
        parent = self.find(t.parentName, t.parentNamespace)
        if parent is None:
            raise CatalogError(
                f"clone: unknown parent {(t.parentNamespace, t.parentName)}")
        merged = parent.to_dict()
        overrides = t.to_dict()
        overrides.pop("parentName", None)
        overrides.pop("parentNamespace", None)
        merged.update(overrides)
        clone = CatalogTrack.from_dict(merged)
        if not t.name or clone.key == parent.key:
            raise CatalogError("clone: a new track name is required")
        if any(x.key == clone.key for x in self.tracks):
            raise CatalogError(f"clone: track {clone.key} already declared")
        self.tracks.append(clone)

    # -- helpers ------------------------------------------------------

    def resolve_init(self, track: CatalogTrack) -> Optional[bytes]:
        """Decoder init payload for a track via initRef (§5.2.13)."""
        if track.initRef is None or not self.initDataList:
            return None
        for entry in self.initDataList:
            if entry.id == track.initRef:
                return entry.payload
        raise CatalogError(f"initRef {track.initRef!r} not in initDataList")

    def validate(self) -> List[str]:
        """Spec-conformance issues (empty list = clean). Advisory: parse
        stays tolerant; publishers should emit clean."""
        issues = []
        if self.is_delta:
            if self.version is not None:
                issues.append("delta update must not carry version")
            if self.tracks:
                issues.append("delta update must not carry tracks")
            return issues
        if self.version not in MSF_VERSIONS:
            issues.append(f"unknown version {self.version!r}")
        for t in self.tracks:
            tag = t.name or "<unnamed>"
            for req in ("name", "packaging", "isLive"):
                if getattr(t, req) is None:
                    issues.append(f"{tag}: missing required field {req}")
            if t.packaging is not None \
                    and t.packaging not in _KNOWN_PACKAGING:
                issues.append(f"{tag}: unknown packaging {t.packaging!r}")
            if t.targetLatency is not None and t.buffers is not None:
                issues.append(
                    f"{tag}: targetLatency and buffers are exclusive")
            if t.role in _AV_ROLES:
                if t.codec is None:
                    issues.append(f"{tag}: {t.role} track requires codec")
                if t.bitrate is None:
                    issues.append(f"{tag}: {t.role} track requires bitrate")
            if t.role == "audio":
                if t.samplerate is None:
                    issues.append(f"{tag}: audio track requires samplerate")
                if t.channelConfig is None:
                    issues.append(
                        f"{tag}: audio track requires channelConfig")
        return issues
