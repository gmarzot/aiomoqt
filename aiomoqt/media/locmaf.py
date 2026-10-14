# port of Eyevinn/locmaf v0.1.1 (MIT)
"""LOCMAF packaging (draft-einarsson-moq-locmaf, locmafVersion 0.3).

A LOCMAF Object carries one CMAF chunk: zero or more genBox elements
(pre-moof boxes, verbatim), one full or delta header of (field id,
value) tuples standing for the moof, then the untagged mdat payload. A
delta header is relative to the previous chunk of the same MOQT group.
A chunk outside the header's field model rides as a single rawBoxes
element instead. The receiver rebuilds a canonical CMAF chunk, byte for
byte the same in every conformant receiver; the CMAF header rides the
catalog initDataList unchanged.

Integers are MOQT vi64; signed values are zigzag-mapped.
"""
from __future__ import annotations

import struct
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from ..delivery import StreamMapping
from ..utils.buffer import Buffer, BufferReadError
from ..utils.logger import get_logger
from .loc import LocTrackPublisher, LocTrackSubscriber

logger = get_logger(__name__)

LOCMAF_VERSION = "0.3"
SUPPORTED_LOCMAF_VERSIONS = (LOCMAF_VERSION,)

ELEMENT_GEN_BOX = 1
ELEMENT_FULL_HEADER = 2
ELEMENT_DELTA_HEADER = 3
ELEMENT_RAW_BOXES = 4

# Header field ids: an even id carries one vi64, an odd id a length-prefixed
# value.
FIELD_SAMPLE_SIZES = 1              # n-1 entries; the last size is derived
FIELD_SAMPLE_DESCRIPTION_INDEX = 2
FIELD_SAMPLE_DURATIONS = 3
FIELD_DEFAULT_SAMPLE_DURATION = 4
FIELD_COMPOSITION_OFFSETS = 5       # zigzag in full and delta headers
FIELD_DEFAULT_SAMPLE_SIZE = 6
FIELD_SAMPLE_FLAGS = 7
FIELD_DEFAULT_SAMPLE_FLAGS = 8
FIELD_IVS = 9                       # raw bytes, overwritten by a delta
FIELD_BASE_MEDIA_DECODE_TIME = 10   # full headers only
FIELD_SUBSAMPLE_COUNTS = 11
FIELD_FIRST_SAMPLE_FLAGS = 12
FIELD_CLEAR_BYTES = 13
FIELD_SAMPLE_COUNT = 14
FIELD_PROTECTED_BYTES = 15
FIELD_IV_SIZE = 16
FIELD_DELETED = 27                  # delta headers only: ids that left

# Bound on sample and subsample counts, checked before any allocation.
MAX_SAMPLE_COUNT = 1 << 24
# Groups whose reference a receiver keeps: each group rides its own
# stream, so the tail of one interleaves with the head of the next.
MAX_TRACKED_GROUPS = 8

_SCALARS = frozenset((2, 4, 6, 8, 10, 12, 14, 16))
_LISTS = frozenset((1, 3, 7, 11, 13, 15))
_KNOWN = _SCALARS | _LISTS | {5, 9, 27}
_CENC_FIELDS = (9, 11, 13, 15, 16)
_DELTA_SCALARS = (2, 4, 6, 8, 12, 16)

_U16 = 0xFFFF
_U32 = 0xFFFFFFFF
_U64 = (1 << 64) - 1
_I32_MIN, _I32_MAX = -(1 << 31), (1 << 31) - 1

# Sample entries a protection scheme info box (sinf) is read from.
_PROTECTED_ENTRIES = (b'encv', b'enca')
_TRAF_MODEL = frozenset((b'tfhd', b'tfdt', b'trun', b'senc', b'saiz', b'saio'))


class LocmafError(ValueError):
    """LOCMAF codec error."""


class LocmafMalformed(LocmafError):
    """An Object or CMAF header a receiver must reject."""


class LocmafSourceError(LocmafError):
    """Source content LOCMAF cannot carry."""


class GenBox(NamedTuple):
    """A pre-moof box: FourCC and the contents after its 8-byte header."""
    name: bytes
    payload: bytes


@dataclass
class EffectiveValues:
    """A chunk's meaning: per-sample vectors after deltas, deletions and
    the size and decode-time derivations. `subsample_counts` is None
    when the chunk has no subsample map; `ivs` concatenates the
    per-sample IVs."""
    bmdt: int
    sample_description_index: int
    durations: List[int]
    sizes: List[int]
    flags: List[int]
    ctos: List[int]
    iv_size: int = 0
    ivs: bytes = b''
    subsample_counts: Optional[List[int]] = None
    clear_bytes: List[int] = field(default_factory=list)
    protected_bytes: List[int] = field(default_factory=list)
    gen_boxes: List[GenBox] = field(default_factory=list)
    mdat: bytes = b''

    @property
    def sample_count(self) -> int:
        return len(self.durations)


@dataclass
class LocmafObject:
    """One Object as carried: a rawBoxes Object (`raw_boxes` set), or
    genBoxes, one header's fields as wire values and the mdat payload.
    Wire values are absolute in a full header and zigzag deltas in a
    delta header; field 5 is zigzag in both."""
    full: bool = True
    fields: Dict[int, Any] = field(default_factory=dict)
    gen_boxes: List[GenBox] = field(default_factory=list)
    mdat: bytes = b''
    raw_boxes: Optional[bytes] = None


# -- integers ---------------------------------------------------------

def _zigzag(n: int) -> int:
    return 2 * n if n >= 0 else -2 * n - 1


def _unzigzag(z: int) -> int:
    return -((z + 1) >> 1) if z & 1 else z >> 1


def _vi64(v: int) -> bytes:
    buf = Buffer(capacity=9)
    buf.push_uint_vi64(v)
    return buf.data


def _vi64_list(raw: bytes) -> Tuple[int, ...]:
    if not raw:
        return ()
    buf = Buffer(data=raw)
    out = []
    while not buf.eof():
        out.append(buf.pull_uint_vi64())
    return tuple(out)


def _uniform(v: List[int]) -> bool:
    return all(x == v[0] for x in v)


def _all_but_first(v: List[int]) -> bool:
    """v[1:] share one value that differs from v[0]; len(v) > 1."""
    return len(v) > 1 and v[0] != v[1] and all(x == v[1] for x in v[2:])


# -- ISO BMFF ---------------------------------------------------------

def _walk(data: bytes, start: int, end: int) -> List[Tuple[bytes, int, int, int]]:
    """Boxes tiling data[start:end] exactly, as (type, start, body, end)."""
    out = []
    pos = start
    while pos < end:
        if end - pos < 8:
            raise LocmafMalformed(f"truncated box header at {pos}")
        size, btype = struct.unpack_from('>I4s', data, pos)
        body = pos + 8
        if size == 1:
            if end - pos < 16:
                raise LocmafMalformed(f"truncated largesize header at {pos}")
            size = struct.unpack_from('>Q', data, pos + 8)[0]
            body = pos + 16
        elif size == 0:
            size = end - pos
        if size < body - pos or size > end - pos:
            raise LocmafMalformed(f"box {btype!r} size {size} at {pos} "
                                  f"does not fit its parent")
        out.append((btype, pos, body, pos + size))
        pos += size
    return out


def _child(data: bytes, parent: Tuple, btype: bytes, need: bool = True):
    box = next((b for b in _walk(data, parent[2], parent[3])
                if b[0] == btype), None)
    if box is None and need:
        raise LocmafMalformed(f"init: no {btype.decode()} box")
    return box


def _check_raw_boxes(data: bytes, error: type) -> None:
    """One or more complete boxes with 32-bit sizes of at least 8 (no
    size escapes), tiling `data` exactly."""
    if not data:
        raise error("empty rawBoxes content")
    pos = 0
    while pos < len(data):
        if len(data) - pos < 8:
            raise error(f"truncated box header at {pos} in rawBoxes")
        size = struct.unpack_from('>I', data, pos)[0]
        if size < 8:
            raise error(f"box size {size} at {pos} in rawBoxes")
        if size > len(data) - pos:
            raise error(f"box at {pos} exceeds the rawBoxes content")
        pos += size


@dataclass(frozen=True)
class TrackContext:
    """What a LOCMAF codec takes from the CMAF header: track id, trex
    defaults and tenc defaults."""
    track_id: int
    timescale: int
    default_sample_description_index: int
    default_sample_duration: int
    default_sample_size: int
    default_sample_flags: int
    protected: bool = False
    iv_size: int = 0

    @classmethod
    def from_init(cls, init: bytes) -> "TrackContext":
        """Parse a CMAF header (ftyp + moov with exactly one trak)."""
        init = bytes(init)
        root = (b'', 0, 0, len(init))
        moov = _child(init, root, b'moov')
        traks = [b for b in _walk(init, moov[2], moov[3]) if b[0] == b'trak']
        if len(traks) != 1:
            raise LocmafSourceError(
                f"LOCMAF needs exactly one trak, the init has {len(traks)}")
        trak = traks[0]
        tkhd = _child(init, trak, b'tkhd')
        off = 20 if init[tkhd[2]] == 1 else 12
        if tkhd[2] + off + 4 > tkhd[3]:
            raise LocmafMalformed("init: tkhd too short")
        track_id = struct.unpack_from('>I', init, tkhd[2] + off)[0] or 1
        mdia = _child(init, trak, b'mdia')
        mdhd = _child(init, mdia, b'mdhd')
        off = 20 if init[mdhd[2]] == 1 else 12
        if mdhd[2] + off + 4 > mdhd[3]:
            raise LocmafMalformed("init: mdhd too short")
        timescale = struct.unpack_from('>I', init, mdhd[2] + off)[0]
        protected, iv_size = _tenc(init, mdia)
        mvex = _child(init, moov, b'mvex')
        trex = next((b for b in _walk(init, mvex[2], mvex[3])
                     if b[0] == b'trex' and b[3] - b[2] >= 24
                     and struct.unpack_from('>I', init, b[2] + 4)[0]
                     == track_id), None)
        if trex is None:
            raise LocmafMalformed(f"init: no trex for track {track_id}")
        sdi, dur, size, flags = struct.unpack_from('>4I', init, trex[2] + 8)
        return cls(track_id, timescale, sdi, dur, size, flags,
                   protected, iv_size)


def _tenc(init: bytes, mdia: Tuple) -> Tuple[bool, int]:
    """(default_isProtected, default_Per_Sample_IV_Size) of the first
    sample entry's tenc; (False, 0) for an unprotected entry."""
    minf = _child(init, mdia, b'minf', need=False)
    stbl = minf and _child(init, minf, b'stbl', need=False)
    stsd = stbl and _child(init, stbl, b'stsd', need=False)
    if not stsd or stsd[3] - stsd[2] < 8:
        return False, 0
    if struct.unpack_from('>I', init, stsd[2] + 4)[0] == 0:
        return False, 0
    entries = _walk(init, stsd[2] + 8, stsd[3])
    if not entries or entries[0][0] not in _PROTECTED_ENTRIES:
        return False, 0
    etype, _, body, end = entries[0]
    fixed = 78
    if etype == b'enca':
        if end - body < 28:
            raise LocmafMalformed("init: enca sample entry too short")
        version = struct.unpack_from('>H', init, body + 8)[0]
        fixed = {1: 44, 2: 64}.get(version, 28)
    if body + fixed > end:
        raise LocmafMalformed(f"init: {etype.decode()} sample entry too short")
    entry = (etype, 0, body + fixed, end)
    sinf = _child(init, entry, b'sinf', need=False)
    schi = sinf and _child(init, sinf, b'schi', need=False)
    tenc = schi and _child(init, schi, b'tenc', need=False)
    if not tenc:
        return False, 0
    if tenc[3] - tenc[2] < 24:
        raise LocmafMalformed("init: tenc too short")
    return init[tenc[2] + 6] == 1, init[tenc[2] + 7]


# -- source chunks ----------------------------------------------------

def parse_chunk(chunk: bytes, ctx: TrackContext) -> Optional[EffectiveValues]:
    """Effective values of a CMAF chunk (genBoxes, one moof, one mdat), or
    None when the chunk falls outside the LOCMAF field model."""
    read = _read_chunk(bytes(chunk), ctx)
    return read[0] if read else None


def _read_chunk(chunk: bytes, ctx: TrackContext):
    """(effective values, senc IV size or None without senc), or None
    outside the field model."""
    try:
        return _read_chunk_boxes(chunk, ctx)
    except (LocmafMalformed, struct.error, IndexError):
        return None


def _read_chunk_boxes(chunk: bytes, ctx: TrackContext):
    u32 = struct.Struct('>I').unpack_from
    top = _walk(chunk, 0, len(chunk))
    if any(u32(chunk, b[1])[0] < 8 for b in top):
        return None                           # ISO size escape
    moofs = [i for i, b in enumerate(top) if b[0] == b'moof']
    if len(moofs) != 1:
        return None
    mi = moofs[0]
    if mi + 2 != len(top) or top[mi + 1][0] != b'mdat':
        return None
    gen = []
    for btype, start, _, end in top[:mi]:
        if btype == b'mdat':
            return None
        gen.append(GenBox(btype, chunk[start + 8:end]))
    moof = top[mi]
    mdat = chunk[top[mi + 1][2]:top[mi + 1][3]]
    kids = _walk(chunk, moof[2], moof[3])
    if any(k[0] not in (b'mfhd', b'traf') for k in kids):
        return None
    trafs = [k for k in kids if k[0] == b'traf']
    if len(trafs) != 1:
        return None
    boxes = {}
    for k in _walk(chunk, trafs[0][2], trafs[0][3]):
        if k[0] not in _TRAF_MODEL or k[0] in boxes:
            return None
        boxes[k[0]] = k
    tfhd, tfdt, trun = boxes.get(b'tfhd'), boxes.get(b'tfdt'), boxes.get(b'trun')
    if tfhd is None or tfdt is None or trun is None:
        return None

    _, _, body, end = tfhd
    if end - body < 8:
        return None
    tf_flags = u32(chunk, body)[0] & 0xFFFFFF
    if tf_flags & 0x01 or u32(chunk, body + 4)[0] != ctx.track_id:
        return None                           # base-data-offset / other track
    pos = body + 8
    tf = [ctx.default_sample_description_index, ctx.default_sample_duration,
          ctx.default_sample_size, ctx.default_sample_flags]
    for i, bit in enumerate((0x02, 0x08, 0x10, 0x20)):
        if tf_flags & bit:
            if pos + 4 > end:
                return None
            tf[i] = u32(chunk, pos)[0]
            pos += 4
    sdi, ddur, dsize, dflags = tf

    _, _, body, end = tfdt
    if chunk[body] == 1:
        if end - body < 12:
            return None
        bmdt = struct.unpack_from('>Q', chunk, body + 4)[0]
    else:
        if end - body < 8:
            return None
        bmdt = u32(chunk, body + 4)[0]

    _, _, body, end = trun
    if end - body < 8:
        return None
    version = chunk[body]
    tr = u32(chunk, body)[0] & 0xFFFFFF
    n = u32(chunk, body + 4)[0]
    if n > MAX_SAMPLE_COUNT or not tr & 0x001:
        return None
    per = 4 * bin(tr & 0xF00).count('1')
    if 12 + (4 if tr & 0x004 else 0) + n * per > end - body:
        return None
    pos = body + 8
    if struct.unpack_from('>i', chunk, pos)[0] != moof[3] - moof[1] + 8:
        return None                           # data_offset not at the payload
    pos += 4
    first = None
    if tr & 0x004:
        first = u32(chunk, pos)[0]
        pos += 4
    durations, sizes, flags, ctos = [], [], [], []
    for i in range(n):
        if tr & 0x100:
            durations.append(u32(chunk, pos)[0])
            pos += 4
        else:
            durations.append(ddur)
        if tr & 0x200:
            sizes.append(u32(chunk, pos)[0])
            pos += 4
        else:
            sizes.append(dsize)
        if tr & 0x400:
            flags.append(u32(chunk, pos)[0])
            pos += 4
        else:
            flags.append(first if i == 0 and first is not None else dflags)
        if tr & 0x800:
            cto = struct.unpack_from('>i' if version else '>I', chunk, pos)[0]
            if cto > _I32_MAX:
                return None                   # receivers take 32-bit signed
            ctos.append(cto)
            pos += 4
        else:
            ctos.append(0)
    if sum(sizes) != len(mdat):
        return None

    eff = EffectiveValues(bmdt, sdi, durations, sizes, flags, ctos,
                          gen_boxes=gen, mdat=mdat)
    senc = boxes.get(b'senc')
    if senc is None:
        if ctx.protected and ctx.iv_size and n:
            return None
        return eff, None
    if not ctx.protected:
        return None
    parsed = _read_senc(chunk, senc, sizes, ctx)
    if parsed is None:
        return None
    eff.iv_size, eff.ivs, counts, eff.clear_bytes, eff.protected_bytes = parsed
    eff.subsample_counts = counts
    return eff, eff.iv_size


def _read_senc(chunk: bytes, senc: Tuple, sizes: List[int], ctx: TrackContext):
    """senc as (iv_size, ivs, counts or None, clear, protected). Its IV
    size is not self-describing: the tenc default, then 8, 16 and 0, the
    first that tiles the box."""
    _, _, body, end = senc
    if end - body < 8:
        return None
    sflags = struct.unpack_from('>I', chunk, body)[0] & 0xFFFFFF
    n = len(sizes)
    if sflags & 0x1 or struct.unpack_from('>I', chunk, body + 4)[0] != n:
        return None
    mapped = bool(sflags & 0x2)
    for iv in dict.fromkeys((ctx.iv_size, 8, 16, 0)):
        pos = body + 8
        ivs = bytearray()
        counts, clear, prot = [], [], []
        ok = True
        for _ in range(n):
            if pos + iv > end:
                ok = False
                break
            ivs += chunk[pos:pos + iv]
            pos += iv
            if mapped:
                if pos + 2 > end:
                    ok = False
                    break
                c = struct.unpack_from('>H', chunk, pos)[0]
                pos += 2
                if pos + 6 * c > end:
                    ok = False
                    break
                counts.append(c)
                for _ in range(c):
                    a, b = struct.unpack_from('>HI', chunk, pos)
                    clear.append(a)
                    prot.append(b)
                    pos += 6
        if not ok or pos != end:
            continue
        if not (mapped and n):
            return iv, bytes(ivs), None, [], []
        k = 0
        for i, c in enumerate(counts):
            if iv + 2 + 6 * c > 255:
                return None
            if c and sum(clear[k:k + c]) + sum(prot[k:k + c]) != sizes[i]:
                return None
            k += c
        return iv, bytes(ivs), counts, clear, prot
    return None


def _represent(eff: EffectiveValues, senc_iv: Optional[int],
               ctx: TrackContext) -> Dict[int, Any]:
    """The chunk's represented fields: the minimal absolute set the
    emission rules allow, the shape both sides keep as the reference."""
    n = eff.sample_count
    f: Dict[int, Any] = {FIELD_SAMPLE_COUNT: n,
                         FIELD_BASE_MEDIA_DECODE_TIME: eff.bmdt}
    if eff.sample_description_index != ctx.default_sample_description_index:
        f[FIELD_SAMPLE_DESCRIPTION_INDEX] = eff.sample_description_index
    if n:
        if not _uniform(eff.durations):
            f[FIELD_SAMPLE_DURATIONS] = tuple(eff.durations)
        elif eff.durations[0] != ctx.default_sample_duration:
            f[FIELD_DEFAULT_SAMPLE_DURATION] = eff.durations[0]
    if n > 1:
        if not _uniform(eff.sizes):
            f[FIELD_SAMPLE_SIZES] = tuple(eff.sizes[:-1])
        elif eff.sizes[0] != ctx.default_sample_size:
            f[FIELD_DEFAULT_SAMPLE_SIZE] = eff.sizes[0]
    if n:
        if _uniform(eff.flags):
            if eff.flags[0] != ctx.default_sample_flags:
                f[FIELD_DEFAULT_SAMPLE_FLAGS] = eff.flags[0]
        elif _all_but_first(eff.flags):
            f[FIELD_FIRST_SAMPLE_FLAGS] = eff.flags[0]
            if eff.flags[1] != ctx.default_sample_flags:
                f[FIELD_DEFAULT_SAMPLE_FLAGS] = eff.flags[1]
        else:
            f[FIELD_SAMPLE_FLAGS] = tuple(eff.flags)
    if any(eff.ctos):
        f[FIELD_COMPOSITION_OFFSETS] = tuple(eff.ctos)
    if senc_iv is not None:
        if senc_iv != ctx.iv_size:
            f[FIELD_IV_SIZE] = senc_iv
        if senc_iv:
            f[FIELD_IVS] = eff.ivs
        if eff.subsample_counts is not None:
            f[FIELD_SUBSAMPLE_COUNTS] = tuple(eff.subsample_counts)
            f[FIELD_CLEAR_BYTES] = tuple(eff.clear_bytes)
            f[FIELD_PROTECTED_BYTES] = tuple(eff.protected_bytes)
    return f


def _full_wire(fields: Dict[int, Any]) -> Dict[int, Any]:
    wire = dict(fields)
    if FIELD_COMPOSITION_OFFSETS in wire:
        wire[FIELD_COMPOSITION_OFFSETS] = tuple(
            _zigzag(c) for c in fields[FIELD_COMPOSITION_OFFSETS])
    return wire


def _delta_wire(cur: Dict[int, Any], prev: Dict[int, Any]) -> Dict[int, Any]:
    """Exactly the fields whose represented values changed, as zigzag
    deltas (a missing previous element counts as 0; bytes overwrite), plus
    the ids that left the reference."""
    wire: Dict[int, Any] = {}
    for fid, v in cur.items():
        p = prev.get(fid)
        if fid in _SCALARS:
            if fid == FIELD_BASE_MEDIA_DECODE_TIME or p == v:
                continue
            wire[fid] = _zigzag(v - (p or 0))
        elif fid == FIELD_IVS:
            if v != (p or b''):
                wire[fid] = v
        else:
            p = p or ()
            if tuple(v) != tuple(p):
                wire[fid] = tuple(_zigzag(x - (p[i] if i < len(p) else 0))
                                  for i, x in enumerate(v))
    gone = sorted(fid for fid in prev if fid not in cur)
    if gone:
        wire[FIELD_DELETED] = tuple(gone)
    return wire


# -- Objects ----------------------------------------------------------

def parse_object(payload: bytes) -> LocmafObject:
    """Parse one Object payload; LocmafMalformed on any structural
    violation."""
    payload = bytes(payload)
    total = len(payload)
    buf = Buffer(data=payload) if payload else None
    gen: List[GenBox] = []
    try:
        while True:
            if buf is None or buf.tell() >= total:
                raise LocmafMalformed("object ends before a header element")
            etype = buf.pull_uint_vi64()
            if etype == ELEMENT_GEN_BOX:
                size = buf.pull_uint_vi64()
                if size < 4 or size > 0xFFFFFFFB:
                    raise LocmafMalformed(f"genBox box_size {size} out of range")
                if size > total - buf.tell():
                    raise LocmafMalformed("genBox exceeds the object payload")
                name = buf.pull_bytes(4)
                gen.append(GenBox(name, buf.pull_bytes(size - 4)))
            elif etype in (ELEMENT_FULL_HEADER, ELEMENT_DELTA_HEADER):
                length = buf.pull_uint_vi64()
                start = buf.tell()
                if length > total - start:
                    raise LocmafMalformed("property block exceeds the object payload")
                full = etype == ELEMENT_FULL_HEADER
                fields = _parse_props(payload[start:start + length], full)
                return LocmafObject(full, fields, gen, payload[start + length:])
            elif etype == ELEMENT_RAW_BOXES:
                if gen:
                    raise LocmafMalformed("rawBoxes element after a genBox")
                raw = payload[buf.tell():]
                _check_raw_boxes(raw, LocmafMalformed)
                return LocmafObject(raw_boxes=raw)
            else:
                raise LocmafMalformed(f"unknown element_type {etype}")
    except BufferReadError as e:
        raise LocmafMalformed(f"truncated integer: {e}") from None


def _parse_props(props: bytes, full: bool) -> Dict[int, Any]:
    """Wire values of the known fields; unknown ids are skipped by their
    parity framing."""
    out: Dict[int, Any] = {}
    if not props:
        return out
    buf = Buffer(data=props)
    seen = set()
    while not buf.eof():
        fid = buf.pull_uint_vi64()
        if fid in seen:
            raise LocmafMalformed(f"field {fid} repeated in one header")
        seen.add(fid)
        if fid % 2 == 0:
            value = buf.pull_uint_vi64()
            if fid in _SCALARS:
                out[fid] = value
            continue
        length = buf.pull_uint_vi64()
        if length > len(props) - buf.tell():
            raise LocmafMalformed(f"field {fid} overruns the property block")
        raw = buf.pull_bytes(length)
        if fid == FIELD_IVS:
            out[fid] = raw
        elif fid in _KNOWN:
            out[fid] = _vi64_list(raw)
    if full and FIELD_DELETED in out:
        raise LocmafMalformed("field 27 in a full header")
    if not full and FIELD_BASE_MEDIA_DECODE_TIME in out:
        raise LocmafMalformed("field 10 in a delta header")
    return out


def _props_bytes(wire: Dict[int, Any]) -> bytes:
    buf = Buffer(capacity=64)
    for fid in sorted(wire):
        v = wire[fid]
        buf.push_uint_vi64(fid)
        if fid in _SCALARS:
            buf.push_uint_vi64(v)
            continue
        if fid == FIELD_IVS:
            raw = bytes(v)
        else:
            lb = Buffer(capacity=max(16, 2 * len(v)))
            for x in v:
                lb.push_uint_vi64(x)
            raw = lb.data
        buf.push_uint_vi64(len(raw))
        buf.push_bytes(raw)
    return buf.data


def serialize_object(obj: LocmafObject) -> bytes:
    """Canonical serialization: genBoxes in order, header fields in
    ascending id order, shortest vi64 forms."""
    buf = Buffer(capacity=64 + len(obj.mdat))
    if obj.raw_boxes is not None:
        _check_raw_boxes(obj.raw_boxes, LocmafSourceError)
        buf.push_uint_vi64(ELEMENT_RAW_BOXES)
        buf.push_bytes(bytes(obj.raw_boxes))
        return buf.data
    for name, payload in obj.gen_boxes:
        if len(name) != 4:
            raise LocmafSourceError(f"genBox name {name!r} is not a FourCC")
        if 4 + len(payload) > 0xFFFFFFFB:
            raise LocmafSourceError(f"genBox {name!r} too large")
        buf.push_uint_vi64(ELEMENT_GEN_BOX)
        buf.push_uint_vi64(4 + len(payload))
        buf.push_bytes(bytes(name))
        buf.push_bytes(bytes(payload))
    props = _props_bytes(obj.fields)
    buf.push_uint_vi64(ELEMENT_FULL_HEADER if obj.full else ELEMENT_DELTA_HEADER)
    buf.push_uint_vi64(len(props))
    buf.push_bytes(props)
    buf.push_bytes(bytes(obj.mdat))
    return buf.data


# -- in-group state ---------------------------------------------------

class GroupState:
    """The in-group reference both encoder and decoder keep per MOQT
    group: the previous chunk's represented fields, the decode time the
    next chunk derives, and the last Object ID."""
    __slots__ = ('fields', 'next_bmdt', 'last_object_id')

    def __init__(self) -> None:
        self.fields: Optional[Dict[int, Any]] = None
        self.next_bmdt: Optional[int] = None
        self.last_object_id = -1

    def reset(self) -> None:
        """No reference: the next moof-carrying Object must be full."""
        self.fields = None
        self.next_bmdt = None

    def store(self, fields: Dict[int, Any], eff: EffectiveValues,
              object_id: int) -> None:
        self.fields = fields
        nxt = eff.bmdt + sum(eff.durations)
        self.next_bmdt = nxt if nxt <= _U64 else None
        self.last_object_id = object_id


# -- receiver ---------------------------------------------------------

def reconstruct(obj: LocmafObject, state: GroupState, ctx: TrackContext,
                object_id: int) -> Tuple[bytes, Optional[EffectiveValues]]:
    """(canonical CMAF chunk, effective values) for a moof-carrying
    Object, (boxes, None) for a rawBoxes Object. Applies every
    must-reject rule; a rejection raises LocmafMalformed and clears the
    group's reference, so its deltas are refused until a full header."""
    try:
        if obj.raw_boxes is not None:
            state.reset()
            state.last_object_id = object_id
            return obj.raw_boxes, None
        if obj.full:
            fields = dict(obj.fields)
            if FIELD_COMPOSITION_OFFSETS in fields:
                fields[FIELD_COMPOSITION_OFFSETS] = tuple(
                    _unzigzag(z) for z in fields[FIELD_COMPOSITION_OFFSETS])
            for fid in (FIELD_SAMPLE_COUNT, FIELD_BASE_MEDIA_DECODE_TIME):
                if fid not in fields:
                    raise LocmafMalformed(f"full header lacks field {fid}")
        else:
            if state.fields is None:
                raise LocmafMalformed("delta header without an in-group reference")
            if object_id != state.last_object_id + 1:
                raise LocmafMalformed(
                    f"object id gap before a delta header "
                    f"({state.last_object_id} -> {object_id})")
            fields = _apply_delta(obj.fields, state)
        eff = _expand(fields, obj.gen_boxes, obj.mdat, ctx)
        chunk = build_chunk(eff, ctx)
    except LocmafMalformed:
        state.reset()
        raise
    state.store(fields, eff, object_id)
    return chunk, eff


def _apply_delta(wire: Dict[int, Any], state: GroupState) -> Dict[int, Any]:
    """Deletions first, then the sample count (it fixes every list
    length), the other scalars, the lists and the IVs; the decode time
    is derived."""
    cur = dict(state.fields)
    for fid in wire.get(FIELD_DELETED, ()):
        cur.pop(fid, None)

    def scalar(fid: int) -> None:
        if fid in wire:
            v = cur.get(fid, 0) + _unzigzag(wire[fid])
            if v < 0 or v > _U64:
                raise LocmafMalformed(f"field {fid} delta yields {v}")
            cur[fid] = v

    def listed(fid: int, want: int, signed: bool = False) -> None:
        prev = cur.get(fid)
        if fid not in wire:
            if prev is not None:
                if len(prev) < want:
                    raise LocmafMalformed(
                        f"inherited field {fid} has {len(prev)} elements, "
                        f"need {want}")
                cur[fid] = tuple(prev[:want])
            return
        deltas = wire[fid]
        if len(deltas) != want:
            raise LocmafMalformed(
                f"field {fid} carries {len(deltas)} elements, expected {want}")
        prev = prev or ()
        out = []
        for i, z in enumerate(deltas):
            v = (prev[i] if i < len(prev) else 0) + _unzigzag(z)
            if not signed and (v < 0 or v > _U64):
                raise LocmafMalformed(f"field {fid} element {i} delta yields {v}")
            out.append(v)
        cur[fid] = tuple(out)

    scalar(FIELD_SAMPLE_COUNT)
    n = cur.get(FIELD_SAMPLE_COUNT, 0)
    if n > MAX_SAMPLE_COUNT:
        raise LocmafMalformed(f"sample count {n} exceeds the limit")
    for fid in _DELTA_SCALARS:
        scalar(fid)
    listed(FIELD_SAMPLE_DURATIONS, n)
    listed(FIELD_SAMPLE_FLAGS, n)
    listed(FIELD_SAMPLE_SIZES, max(n - 1, 0))
    listed(FIELD_SUBSAMPLE_COUNTS, n)
    total = sum(cur.get(FIELD_SUBSAMPLE_COUNTS, ()))
    if total > MAX_SAMPLE_COUNT:
        raise LocmafMalformed("subsample total exceeds the limit")
    listed(FIELD_CLEAR_BYTES, total)
    listed(FIELD_PROTECTED_BYTES, total)
    listed(FIELD_COMPOSITION_OFFSETS, n, signed=True)
    if FIELD_IVS in wire:
        cur[FIELD_IVS] = wire[FIELD_IVS]
    if state.next_bmdt is None:
        raise LocmafMalformed("cannot derive the decode time of a delta chunk")
    cur[FIELD_BASE_MEDIA_DECODE_TIME] = state.next_bmdt
    return cur


def _u32_list(fields: Dict[int, Any], fid: int, n: int) -> List[int]:
    values = fields[fid]
    if len(values) != n:
        raise LocmafMalformed(
            f"field {fid} has {len(values)} elements for {n} samples")
    if any(v > _U32 for v in values):
        raise LocmafMalformed(f"field {fid} element overflows 32 bits")
    return list(values)


def _u32(fields: Dict[int, Any], fid: int, default: int) -> int:
    v = fields.get(fid, default)
    if v > _U32:
        raise LocmafMalformed(f"field {fid} overflows 32 bits")
    return v


def _expand(fields: Dict[int, Any], gen: List[GenBox], mdat: bytes,
            ctx: TrackContext) -> EffectiveValues:
    """Represented fields + CMAF header defaults + payload length ->
    effective values, applying the receiver must-reject rules."""
    n = fields.get(FIELD_SAMPLE_COUNT)
    if n is None:
        raise LocmafMalformed("no sample count")
    if n > MAX_SAMPLE_COUNT:
        raise LocmafMalformed(f"sample count {n} exceeds the limit")
    bmdt = fields.get(FIELD_BASE_MEDIA_DECODE_TIME)
    if bmdt is None:
        raise LocmafMalformed("no decode time")
    p = len(mdat)
    if n == 0 and p:
        raise LocmafMalformed("zero samples with a non-empty mdat payload")
    sdi = _u32(fields, FIELD_SAMPLE_DESCRIPTION_INDEX,
               ctx.default_sample_description_index)
    default_duration = _u32(fields, FIELD_DEFAULT_SAMPLE_DURATION,
                            ctx.default_sample_duration)
    default_flags = _u32(fields, FIELD_DEFAULT_SAMPLE_FLAGS,
                         ctx.default_sample_flags)
    first_flags = (_u32(fields, FIELD_FIRST_SAMPLE_FLAGS, 0)
                   if FIELD_FIRST_SAMPLE_FLAGS in fields else None)

    if FIELD_SAMPLE_DURATIONS in fields:
        durations = _u32_list(fields, FIELD_SAMPLE_DURATIONS, n)
    else:
        durations = [default_duration] * n

    if FIELD_SAMPLE_SIZES in fields:
        listed = fields[FIELD_SAMPLE_SIZES]
        if len(listed) != max(n - 1, 0):
            raise LocmafMalformed(
                f"{len(listed)} listed sizes for {n} samples")
        if any(s > _U32 for s in listed):
            raise LocmafMalformed("sample size overflows 32 bits")
        sizes = list(listed)
        if n:
            if sum(listed) > p:
                raise LocmafMalformed("listed sizes exceed the mdat payload")
            if p - sum(listed) > _U32:
                raise LocmafMalformed("derived last size overflows 32 bits")
            sizes.append(p - sum(listed))
    else:
        size = fields.get(FIELD_DEFAULT_SAMPLE_SIZE)
        if size is None and n != 1 and ctx.default_sample_size:
            size = ctx.default_sample_size
        if size is not None:
            if size > _U32:
                raise LocmafMalformed("default sample size overflows 32 bits")
            if n * size != p:
                raise LocmafMalformed(
                    f"{n} samples of {size} bytes do not match the "
                    f"{p}-byte mdat payload")
            sizes = [size] * n
        elif n == 1:
            if p > _U32:
                raise LocmafMalformed("sample size overflows 32 bits")
            sizes = [p]
        elif p == 0:
            sizes = [0] * n
        else:
            raise LocmafMalformed(f"no sample size information for {n} samples")

    if FIELD_SAMPLE_FLAGS in fields:
        flags = _u32_list(fields, FIELD_SAMPLE_FLAGS, n)
    else:
        flags = [default_flags] * n
        if n and first_flags is not None:
            flags[0] = first_flags

    ctos = [0] * n
    if FIELD_COMPOSITION_OFFSETS in fields:
        ctos = list(fields[FIELD_COMPOSITION_OFFSETS])
        if len(ctos) != n:
            raise LocmafMalformed(
                f"{len(ctos)} composition offsets for {n} samples")
        if any(c < _I32_MIN or c > _I32_MAX for c in ctos):
            raise LocmafMalformed("composition offset outside 32-bit range")

    eff = EffectiveValues(bmdt, sdi, durations, sizes, flags, ctos,
                          gen_boxes=list(gen), mdat=mdat)
    if not ctx.protected:
        present = [fid for fid in _CENC_FIELDS if fid in fields]
        if present:
            raise LocmafMalformed(f"CENC field {present[0]} on an unprotected track")
        return eff

    iv_size = fields.get(FIELD_IV_SIZE, ctx.iv_size)
    if iv_size > 255:
        raise LocmafMalformed(f"per-sample IV size {iv_size} out of range")
    eff.iv_size = iv_size
    ivs = fields.get(FIELD_IVS, b'')
    if ivs:
        if not iv_size:
            raise LocmafMalformed("IVs with a per-sample IV size of 0")
        if len(ivs) != iv_size * n:
            raise LocmafMalformed(
                f"{len(ivs)} IV bytes for {n} samples of {iv_size}")
        eff.ivs = bytes(ivs)
    elif iv_size and n:
        raise LocmafMalformed(f"per-sample IV size {iv_size} but no IVs")

    if FIELD_SUBSAMPLE_COUNTS in fields:
        counts = list(fields[FIELD_SUBSAMPLE_COUNTS])
        if len(counts) != n:
            raise LocmafMalformed(f"{len(counts)} subsample counts for {n} samples")
        if any(c > _U16 for c in counts):
            raise LocmafMalformed("subsample count overflows 16 bits")
        clear = list(fields.get(FIELD_CLEAR_BYTES, ()))
        prot = list(fields.get(FIELD_PROTECTED_BYTES, ()))
        if len(clear) != sum(counts) or len(prot) != sum(counts):
            raise LocmafMalformed("subsample byte lists do not match the counts")
        if any(c > _U16 for c in clear) or any(c > _U32 for c in prot):
            raise LocmafMalformed("subsample byte count overflows its field")
        k = 0
        for i, c in enumerate(counts):
            if c and sum(clear[k:k + c]) + sum(prot[k:k + c]) != sizes[i]:
                raise LocmafMalformed(f"subsample map does not cover sample {i}")
            k += c
        eff.subsample_counts, eff.clear_bytes, eff.protected_bytes = counts, clear, prot
    elif FIELD_CLEAR_BYTES in fields or FIELD_PROTECTED_BYTES in fields:
        raise LocmafMalformed("subsample byte lists without subsample counts")
    return eff


def _check_effective(eff: EffectiveValues) -> None:
    n = eff.sample_count
    if not len(eff.sizes) == len(eff.flags) == len(eff.ctos) == n:
        raise LocmafMalformed("per-sample vectors disagree on the sample count")
    if sum(eff.sizes) != len(eff.mdat):
        raise LocmafMalformed("sample sizes do not cover the mdat payload")
    if any(v < 0 or v > _U32 for vec in (eff.durations, eff.sizes, eff.flags)
           for v in vec) or not 0 <= eff.sample_description_index <= _U32:
        raise LocmafMalformed("per-sample value outside 32 bits")
    if any(c < _I32_MIN or c > _I32_MAX for c in eff.ctos):
        raise LocmafMalformed("composition offset outside 32-bit range")
    if len(eff.ivs) != eff.iv_size * n:
        raise LocmafMalformed("IV bytes do not match the per-sample IV size")
    if eff.subsample_counts is not None:
        total = sum(eff.subsample_counts)
        if (len(eff.subsample_counts) != n or len(eff.clear_bytes) != total
                or len(eff.protected_bytes) != total):
            raise LocmafMalformed("subsample vectors disagree")
    if not 0 <= eff.bmdt <= _U64:
        raise LocmafMalformed("decode time outside 64 bits")
    if len(eff.mdat) > 0xFFFFFFF7:
        raise LocmafMalformed("mdat payload exceeds a 32-bit box size")


def build_chunk(eff: EffectiveValues, ctx: TrackContext) -> bytes:
    """The canonical CMAF chunk: each genBox wrapped, then moof{mfhd,
    traf{tfhd, tfdt, trun[, saiz, saio, senc]}} and mdat. Every presence
    decision follows the effective values and the trex defaults alone."""
    _check_effective(eff)
    n = eff.sample_count
    pack = struct.pack
    def_dur = def_size = def_flags = None
    dur_p = size_p = flags_p = fsf = cto_p = False
    version = 0
    if n:
        if not _uniform(eff.durations):
            dur_p = True
        elif eff.durations[0] != ctx.default_sample_duration:
            def_dur = eff.durations[0]
        if not _uniform(eff.sizes):
            size_p = True
        elif eff.sizes[0] != ctx.default_sample_size:
            def_size = eff.sizes[0]
        if _uniform(eff.flags):
            if eff.flags[0] != ctx.default_sample_flags:
                def_flags = eff.flags[0]
        elif _all_but_first(eff.flags):
            fsf = True
            if eff.flags[1] != ctx.default_sample_flags:
                def_flags = eff.flags[1]
        else:
            flags_p = True
        cto_p = any(eff.ctos)
        version = 1 if any(c < 0 for c in eff.ctos) else 0
    sdi_p = eff.sample_description_index != ctx.default_sample_description_index
    tf_opts = [v for v in (eff.sample_description_index if sdi_p else None,
                           def_dur, def_size, def_flags) if v is not None]
    tfhd_size = 16 + 4 * len(tf_opts)
    per = 4 * (dur_p + size_p + flags_p + cto_p)
    trun_size = 20 + (4 if fsf else 0) + n * per
    traf_size = 8 + tfhd_size + 20 + trun_size

    counts = eff.subsample_counts
    cenc = eff.iv_size > 0 or counts is not None
    if cenc:
        aux = [eff.iv_size + (2 + 6 * counts[i] if counts is not None else 0)
               for i in range(n)]
        if any(a > 255 for a in aux):
            raise LocmafMalformed("aux info size exceeds the 8-bit saiz limit")
        aux_equal = _uniform(aux) if aux else True
        saiz_size = 17 + (0 if aux_equal else n)
        senc_size = 16 + sum(aux)
        senc_at = 8 + 16 + 8 + tfhd_size + 20 + trun_size + saiz_size + 20
        traf_size += saiz_size + 20 + senc_size
    moof_size = 24 + traf_size
    if moof_size + 8 > 0x7FFFFFFF:
        raise LocmafMalformed("moof too large for trun data_offset")

    out = bytearray()
    for name, payload in eff.gen_boxes:
        if len(name) != 4:
            raise LocmafMalformed(f"genBox name {name!r} is not a FourCC")
        if 8 + len(payload) > _U32:
            raise LocmafMalformed(f"genBox {name!r} exceeds a 32-bit box size")
        out += pack('>I4s', 8 + len(payload), bytes(name))
        out += payload
    out += pack('>I4sI4sII', moof_size, b'moof', 16, b'mfhd', 0, 0)
    out += pack('>I4s', traf_size, b'traf')
    tf = (0x020000 | (0x02 if sdi_p else 0) | (0x08 if def_dur is not None else 0)
          | (0x10 if def_size is not None else 0)
          | (0x20 if def_flags is not None else 0))
    out += pack(f'>I4sII{len(tf_opts)}I', tfhd_size, b'tfhd', tf,
                ctx.track_id, *tf_opts)
    out += pack('>I4sIQ', 20, b'tfdt', 1 << 24, eff.bmdt)
    tr = (0x001 | (0x004 if fsf else 0) | (0x100 if dur_p else 0)
          | (0x200 if size_p else 0) | (0x400 if flags_p else 0)
          | (0x800 if cto_p else 0))
    out += pack('>I4sIII', trun_size, b'trun', version << 24 | tr, n,
                moof_size + 8)
    if fsf:
        out += pack('>I', eff.flags[0])
    if per:
        cols = []
        if dur_p:
            cols.append(eff.durations)
        if size_p:
            cols.append(eff.sizes)
        if flags_p:
            cols.append(eff.flags)
        if cto_p:
            cols.append([c & _U32 for c in eff.ctos])
        out += pack(f'>{n * len(cols)}I', *(v for row in zip(*cols) for v in row))
    if cenc:
        out += pack('>I4sI', saiz_size, b'saiz', 0)
        if aux_equal:
            out += pack('>BI', aux[0] if n else 0, n)
        else:
            out += pack('>BI', 0, n) + bytes(aux)
        out += pack('>I4sIII', 20, b'saio', 0, 1, senc_at + 16)
        out += pack('>I4sII', senc_size, b'senc',
                    0x2 if counts is not None else 0, n)
        k = 0
        iv = eff.iv_size
        for i in range(n):
            out += eff.ivs[i * iv:(i + 1) * iv]
            if counts is not None:
                out += pack('>H', counts[i])
                for j in range(k, k + counts[i]):
                    out += pack('>HI', eff.clear_bytes[j], eff.protected_bytes[j])
                k += counts[i]
    out += pack('>I4s', 8 + len(eff.mdat), b'mdat')
    out += eff.mdat
    return bytes(out)


class LocmafDecoder:
    """Per-track receiver: push Objects in Object ID order within each
    group, get canonical CMAF chunks (or rawBoxes content) back. One
    reference per recent group, so interleaved groups decode."""

    def __init__(self, init: bytes):
        self.ctx = TrackContext.from_init(init)
        self._groups: "OrderedDict[int, GroupState]" = OrderedDict()

    def push(self, group_id: int, object_id: int, payload: bytes) -> bytes:
        """The chunk for one Object; LocmafMalformed when it must be
        rejected (that group then waits for a full header)."""
        state = self._groups.get(group_id)
        if state is None:
            state = self._groups[group_id] = GroupState()
            while len(self._groups) > MAX_TRACKED_GROUPS:
                self._groups.popitem(last=False)
        try:
            obj = parse_object(payload)
        except LocmafMalformed:
            state.reset()
            raise
        return reconstruct(obj, state, self.ctx, object_id)[0]


# -- sender -----------------------------------------------------------

class LocmafEncoder:
    """Canonical encoding of one track's CMAF chunks. The caller keeps one
    GroupState per MOQT group. A full header opens a group, follows an
    Object ID gap and re-anchors a decode-time break; other chunks get a
    delta header. A chunk outside the field model is sent as rawBoxes,
    which clears the reference, and counted in `raw_fallbacks`."""

    def __init__(self, ctx: TrackContext):
        self.ctx = ctx
        self.raw_fallbacks = 0

    def encode(self, chunk: bytes, state: GroupState, object_id: int) -> bytes:
        chunk = bytes(chunk)
        read = _read_chunk(chunk, self.ctx)
        if read is None:
            obj = LocmafObject(raw_boxes=chunk)
            out = serialize_object(obj)
            state.reset()
            state.last_object_id = object_id
            self.raw_fallbacks += 1
            return out
        eff, senc_iv = read
        fields = _represent(eff, senc_iv, self.ctx)
        full = (state.fields is None
                or object_id != state.last_object_id + 1
                or eff.bmdt != state.next_bmdt)
        wire = _full_wire(fields) if full else _delta_wire(fields, state.fields)
        out = serialize_object(LocmafObject(full, wire, eff.gen_boxes, eff.mdat))
        state.store(fields, eff, object_id)
        return out


# -- self-framed carriage (.locmaf files) -----------------------------

def append_framed(out: bytearray, obj: bytes) -> bytearray:
    """Append one Object prefixed by its vi64 length."""
    out += _vi64(len(obj))
    out += obj
    return out


def split_framed(data: bytes) -> List[bytes]:
    """The Objects of a self-framed .locmaf file."""
    data = bytes(data)
    objs = []
    if not data:
        return objs
    buf = Buffer(data=data)
    try:
        while not buf.eof():
            length = buf.pull_uint_vi64()
            if length > len(data) - buf.tell():
                raise LocmafMalformed(
                    f"framed object of {length} bytes overruns the file")
            objs.append(buf.pull_bytes(length))
    except BufferReadError as e:
        raise LocmafMalformed(f"truncated framed object length: {e}") from None
    return objs


# -- tracks -----------------------------------------------------------

class LocmafTrackPublisher(LocTrackPublisher):
    """Publishes CMAF chunks (send_frame payloads) as LOCMAF Objects.
    `init` is the CMAF header the catalog carries. Objects stay on
    per-group subgroup streams so each arrives after its reference."""

    def __init__(self, session, namespace: str, trackname: str, *,
                 init: bytes,
                 mapping: StreamMapping = StreamMapping.PER_GROUP,
                 **kwargs: Any):
        if mapping is not StreamMapping.PER_GROUP:
            raise ValueError("LOCMAF objects need per-group subgroup streams")
        self.encoder = LocmafEncoder(TrackContext.from_init(init))
        super().__init__(session, namespace, trackname, mapping=mapping,
                         **kwargs)
        self._group_state = GroupState()
        self._state_group: Optional[int] = None

    @property
    def raw_fallbacks(self) -> int:
        """Chunks sent verbatim as rawBoxes."""
        return self.encoder.raw_fallbacks

    def _package(self, payload: bytes, group_id: int,
                 object_id: int) -> Optional[bytes]:
        if group_id != self._state_group:
            self._group_state = GroupState()
            self._state_group = group_id
        try:
            return self.encoder.encode(payload, self._group_state, object_id)
        except LocmafSourceError as e:
            logger.warning(f"LOCMAF {self.trackname}: frame dropped: {e}")
            return None


class LocmafTrackSubscriber(LocTrackSubscriber):
    """Subscribes to a LOCMAF track: each frame's payload is the rebuilt
    canonical CMAF chunk. Rejected Objects are counted in
    `objects_rejected` and reach no on_frame."""

    def __init__(self, session, namespace: str, trackname: str = None, *,
                 init: bytes, **kwargs: Any):
        self.decoder = LocmafDecoder(init)
        super().__init__(session, namespace, trackname, **kwargs)
        self.objects_rejected = 0

    def _unpackage(self, msg, group_id: int) -> Optional[bytes]:
        try:
            return self.decoder.push(group_id, msg.object_id, msg.payload)
        except LocmafMalformed as e:
            self.objects_rejected += 1
            logger.debug(f"LOCMAF {self.trackname}: g{group_id} "
                         f"o{msg.object_id} rejected: {e}")
            return None
