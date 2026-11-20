"""MPEG-TS over MoQT: the MSF "mpeg2ts" packaging of
draft-gregoire-moq-msfts-02 (MSFTS).

An object is a run of whole source packets (§5.1). The publisher here
uses unmodified carriage (§5.3.1, §5.3.2): every packet of the source in
arrival order, null packets included, so a subscriber rebuilds the
source byte for byte. Groups start at the random access points of the
first program (§5.2); the PAT and PMT ride the catalog initDataList
(§6.12). The catalog follows moq2ts (openmoq/moq2ts `--unmodified`).
"""
from __future__ import annotations

import asyncio
import binascii
import logging
import time
from dataclasses import dataclass
from collections import deque
from typing import (
    Any, Callable, Deque, Dict, List, NamedTuple, Optional, Set, Tuple, cast,
)

from ..types import ObjectStatus
from ..utils.timing import (
    OBJECT_TIMESTAMP, TIMESCALE, TIMESTAMP_MAPPING, encode_mapping,
    zigzag_encode,
)
from .catalog import Catalog, CatalogError, CatalogTrack, PACKAGING_MPEG2TS
from .loc import LocFrame, LocTrackPublisher, LocTrackSubscriber

logger = logging.getLogger(__name__)

TS_PACKET = 188
M2TS_PACKET = 192
PACKET_SIZES = (TS_PACKET, M2TS_PACKET)
MIME_TYPE = "video/mp2t"

MODE_UNMODIFIED_PROGRAM = "unmodified-program"
MODE_UNMODIFIED_MULTIPLEX = "unmodified-multiplex"
MODE_PER_PROGRAM = "per-program"
MODE_ES_PACKETS = "es-packets"
MODES = (MODE_UNMODIFIED_PROGRAM, MODE_UNMODIFIED_MULTIPLEX,
         MODE_PER_PROGRAM, MODE_ES_PACKETS)
_UNMODIFIED = (MODE_UNMODIFIED_PROGRAM, MODE_UNMODIFIED_MULTIPLEX)

# Seven packets, 1316 bytes: an SRT or RIST datagram's payload.
DEFAULT_PACKETS = 7
# Longest a partial object waits for more packets, well under a frame
# interval; a feed above 2.1 Mb/s fills seven packets sooner.
FLUSH_S = 0.005
# Longest a later group waits on the open one: long enough for a group
# tail retransmitted at a few hundred ms RTT. Its stream end releases it.
HOLD_S = 0.5
# Without video, groups a second apart (90 kHz).
_AUDIO_GROUP_PTS = 90_000
# Warn when no random access point has started a group by then.
_RAP_WARN_PACKETS = 20_000
# Longest PSI run held for the next group before it goes to the open one.
_RUN_PACKETS = 64

_PMT_TABLE = 0x02
_PAT_TABLE = 0x00
_NULL_PID = 0x1FFF
_MAX_PID = 0x1FFF
_PCR_WRAP = (1 << 33) * 300
# PcrTimestamps keeps the least arrival-minus-PCR offset of this bucket
# and the last; an offset this far above it starts them over.
_OFFSET_WINDOW_US = 5_000_000
_OFFSET_RESET_US = 1_000_000
# Video stream types: MPEG-1/2, MPEG-4 Part 2, H.264, H.265, H.266.
_VIDEO_TYPES = frozenset({0x01, 0x02, 0x10, 0x1B, 0x24, 0x33})
_MPEG2_KEY = (b'\x00\x00\x01\xb3', b'\x00\x00\x01\xb8')
_START = b'\x00\x00\x01'


class MsftsError(ValueError):
    """An object or catalog entry that breaks draft-gregoire-moq-msfts."""


def check_payload(payload, packet_size: int = TS_PACKET) -> None:
    """Raise MsftsError unless `payload` is whole source packets (§5.1)
    whose TS packets start with the sync byte."""
    n, rest = divmod(len(payload), packet_size)
    if rest or not n:
        raise MsftsError(f"{len(payload)} bytes is not a whole number of "
                         f"{packet_size}-byte packets")
    sync = payload[packet_size - TS_PACKET::packet_size]
    if sync != b'\x47' * n:
        bad = next(i for i, b in enumerate(sync) if b != 0x47)
        raise MsftsError(f"packet {bad} lacks the 0x47 sync byte")


# -- catalog (§6) -------------------------------------------------------

def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _pid_ok(v) -> bool:
    return _is_int(v) and 0 <= v <= _MAX_PID


def _issues(t: CatalogTrack) -> Tuple[List[str], List[str]]:
    """(errors, warnings) of an mpeg2ts track entry: errors make a
    subscriber reject it (§6.2, §6.3, §6.4, §10); warnings break a
    publisher rule only."""
    x = t.extra
    errors: List[str] = []
    warnings: List[str] = []
    if t.packaging != PACKAGING_MPEG2TS:
        errors.append(f"packaging {t.packaging!r} is not {PACKAGING_MPEG2TS!r}")
    mode = x.get("mpeg2tsMode")
    if mode not in MODES:
        errors.append(f"mpeg2tsMode {mode!r} is not one of {', '.join(MODES)}")
    size = x.get("mpeg2tsPacketSize")
    if not _is_int(size) or size not in PACKET_SIZES:
        errors.append(f"mpeg2tsPacketSize {size!r} is not 188 or 192")
    es = "mpeg2tsEsPid" in x
    if mode == MODE_ES_PACKETS and not es:
        errors.append("es-packets track without mpeg2tsEsPid")
    elif mode in MODES and mode != MODE_ES_PACKETS and es:
        errors.append(f"mpeg2tsEsPid on a {mode} track")
    for key in ("mpeg2tsEsPid", "mpeg2tsPcrPid", "mpeg2tsScte35Pid"):
        if key in x and not _pid_ok(x[key]):
            errors.append(f"{key} {x[key]!r} is not a PID")
    prog = x.get("mpeg2tsProgramNumber")
    if prog is not None and not (_is_int(prog) and 1 <= prog <= 0xFFFF):
        errors.append(f"mpeg2tsProgramNumber {prog!r} is not 1-65535")
    rate = x.get("mpeg2tsMuxRate")
    if rate is not None and not (isinstance(rate, (int, float))
                                 and not isinstance(rate, bool) and rate > 0):
        errors.append(f"mpeg2tsMuxRate {rate!r} is not a positive rate")
    si = x.get("mpeg2tsSiPids")
    if si is not None and not (isinstance(si, list)
                               and all(_pid_ok(p) for p in si)
                               and len(set(si)) == len(si)):
        errors.append(f"mpeg2tsSiPids {si!r} is not a list of distinct PIDs")
    ra = x.get("mpeg2tsRandomAccess")
    if ra is not None and not isinstance(ra, bool):
        errors.append(f"mpeg2tsRandomAccess {ra!r} is not a boolean")
    tsm = x.get("mpeg2tsTimestampMode")
    if tsm is not None and not isinstance(tsm, str):
        errors.append(f"mpeg2tsTimestampMode {tsm!r} is not a string")
    if tsm is not None and size == TS_PACKET:
        warnings.append("mpeg2tsTimestampMode on 188-octet packets")
    multiplex = mode == MODE_UNMODIFIED_MULTIPLEX
    if rate is not None and mode in _UNMODIFIED:
        warnings.append(f"mpeg2tsMuxRate on a {mode} track")
    if si is not None and (es or multiplex):
        warnings.append(f"mpeg2tsSiPids on a {mode} track")
    if "mpeg2tsScte35Pid" in x and (es or multiplex):
        warnings.append(f"mpeg2tsScte35Pid on a {mode} track")
    if ra is not None and multiplex and prog is None:
        warnings.append("mpeg2tsRandomAccess on a multiplex with no "
                        "reference program")
    if multiplex and t.altGroup is not None:
        warnings.append("unmodified-multiplex track in an altGroup")
    return errors, warnings


def track_issues(t: CatalogTrack, catalog: Optional[Catalog] = None) -> List[str]:
    """Every MSFTS issue of a catalog track entry; with `catalog`, also
    its initialization data (§6.12)."""
    errors, warnings = _issues(t)
    if catalog is not None and t.initRef is not None and not errors:
        try:
            init = catalog.resolve_init(t)
            if init is None:
                errors.append(f"initRef {t.initRef!r} is not inline data")
            else:
                check_payload(init, t.extra["mpeg2tsPacketSize"])
        except (CatalogError, MsftsError, binascii.Error, ValueError) as e:
            errors.append(f"initialization data: {e}")
    return errors + warnings


@dataclass
class MsftsTrack:
    """The MSFTS fields of a catalog track entry a subscriber may use."""
    mode: str
    packet_size: int
    program_number: Optional[int] = None
    pcr_pid: Optional[int] = None
    es_pid: Optional[int] = None
    random_access: bool = False

    @classmethod
    def from_entry(cls, t: CatalogTrack) -> "MsftsTrack":
        """Raise MsftsError for an entry a subscriber must reject."""
        errors, _ = _issues(t)
        if errors:
            raise MsftsError(f"{t.name!r}: {'; '.join(errors)}")
        x = t.extra
        return cls(mode=x["mpeg2tsMode"], packet_size=x["mpeg2tsPacketSize"],
                   program_number=x.get("mpeg2tsProgramNumber"),
                   pcr_pid=x.get("mpeg2tsPcrPid"),
                   es_pid=x.get("mpeg2tsEsPid"),
                   random_access=bool(x.get("mpeg2tsRandomAccess")))


def catalog_track(name: str, *, mode: str, packet_size: int = TS_PACKET,
                  program_number: Optional[int] = None,
                  pcr_pid: Optional[int] = None,
                  random_access: Optional[bool] = None,
                  init_ref: Optional[str] = None,
                  role: Optional[str] = None,
                  bitrate: Optional[int] = None,
                  target_latency: Optional[int] = None) -> CatalogTrack:
    """A live mpeg2ts track entry; None leaves a field out."""
    extra: Dict[str, Any] = {"mpeg2tsPacketSize": packet_size,
                             "mpeg2tsMode": mode}
    if program_number is not None:
        extra["mpeg2tsProgramNumber"] = program_number
    if pcr_pid is not None:
        extra["mpeg2tsPcrPid"] = pcr_pid
    if random_access is not None:
        extra["mpeg2tsRandomAccess"] = random_access
    return CatalogTrack(name=name, packaging=PACKAGING_MPEG2TS, isLive=True,
                        role=role, mimeType=MIME_TYPE,
                        targetLatency=target_latency, bitrate=bitrate,
                        initRef=init_ref, extra=extra)


def init_data_ref(name: str) -> str:
    """The initDataList id of a track's PAT and PMT, as moq2ts names it."""
    return f"init-{name}"


# -- publisher: packets to objects -------------------------------------

class TsObject(NamedTuple):
    """One MSFTS object of track generation `track`, in group `group`
    (numbered across generations): `key` opens it, at a random access
    point whose PES PTS (90 kHz, unwrapped) is `pts`. `at_us` is its
    first packet's arrival, µs since the epoch, and `pcr` that packet's
    place on the PCR timeline (27 MHz), None before any PCR."""
    payload: bytes
    key: bool
    at_us: int
    track: int = 1
    group: int = 0
    pts: Optional[int] = None
    pcr: Optional[int] = None


def _pes_pts(data, pos: int, end: int) -> Optional[int]:
    """PTS of a PES whose header starts at `pos`, if it fits before `end`."""
    if (pos + 14 > end or data[pos] or data[pos + 1] or data[pos + 2] != 1
            or not data[pos + 7] & 0x80):
        return None
    b = data[pos + 9:pos + 14]
    return ((((b[0] >> 1) & 7) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15)
            | (b[3] << 7) | (b[4] >> 1))


class Psi:
    """The PAT and the first program's PMT, from whole TS packets on
    their PIDs. feed() returns True when a table it completes differs
    from the last one."""

    def __init__(self):
        # PAT programs as (program_number, PMT PID); the first program's
        # PMT gives the PCR PID and the streams as (stream_type, PID).
        self.programs: List[Tuple[int, int]] = []
        self.pmt_pid: Optional[int] = None
        self.program_number: Optional[int] = None
        self.pcr_pid: Optional[int] = None
        self.streams: List[Tuple[int, int]] = []
        self.table_pids: Set[int] = set()
        self.pat: Optional[Tuple[bytes, List[bytes]]] = None
        self.pmt: Optional[Tuple[bytes, List[bytes]]] = None
        self._sections: Dict[int, Tuple[List[bytes], bytearray]] = {}

    @property
    def ready(self) -> bool:
        return self.pat is not None and self.pmt is not None

    def feed(self, data, pos: int, pid: int, pusi: int) -> bool:
        pkt = bytes(data[pos:pos + TS_PACKET])
        afc = (pkt[3] >> 4) & 3
        if not afc & 1:
            return False
        off = 4 + (1 + pkt[4] if afc & 2 else 0)
        if off >= TS_PACKET:
            return False
        payload = pkt[off:]
        if pusi:
            sec = bytearray(payload[1 + payload[0]:])
            pkts = [pkt]
            self._sections[pid] = (pkts, sec)
        else:
            held = self._sections.get(pid)
            if held is None:
                return False
            pkts, sec = held
            pkts.append(pkt)
            sec += payload
        if len(sec) < 3:
            return False
        length = ((sec[1] & 0x0F) << 8) | sec[2]
        if sec[0] == 0xFF or len(sec) >= 3 + length or length > 1021:
            del self._sections[pid]
        if (sec[0] != 0xFF and 9 <= length <= 1021
                and len(sec) >= 3 + length):
            return self._section(pid, bytes(sec[:3 + length]), pkts)
        return False

    def _section(self, pid: int, sec: bytes, pkts: List[bytes]) -> bool:
        if pid == 0 and sec[0] == _PAT_TABLE:
            if self.pat is not None and self.pat[0] == sec:
                return False
            programs = []
            for i in range(8, len(sec) - 4, 4):
                number = (sec[i] << 8) | sec[i + 1]
                if number:
                    programs.append(
                        (number, ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]))
            self.pat = (sec, pkts)
            self.programs = programs
            self.table_pids = {p for _, p in programs}
            pmt_pid = programs[0][1] if programs else None
            if pmt_pid != self.pmt_pid:
                self.pmt_pid = pmt_pid
                self.pmt = None
            return True
        if pid == self.pmt_pid and sec[0] == _PMT_TABLE:
            if self.pmt is not None and self.pmt[0] == sec:
                return False
            self.pmt = (sec, pkts)
            self.program_number = (sec[3] << 8) | sec[4]
            self.pcr_pid = ((sec[8] & 0x1F) << 8) | sec[9]
            i = 12 + (((sec[10] & 0x0F) << 8) | sec[11])
            streams = []
            while i + 5 <= len(sec) - 4:
                streams.append((sec[i], ((sec[i + 1] & 0x1F) << 8)
                                | sec[i + 2]))
                i += 5 + (((sec[i + 3] & 0x0F) << 8) | sec[i + 4])
            self.streams = streams
            return True
        return False


class TsPackager:
    """Unmodified MSFTS carriage: whole 188-byte TS packets, in arrival
    order, `packets` to an object.

    A group opens at each random access point of the first program in
    the PAT: a video PES start with the random_access_indicator, or with
    a sequence header (MPEG-1/2 video), SPS or IDR (H.264), or VPS, SPS
    or IRAP (H.265) in its first packet; without video, the first PES
    start of the first stream a second after the group's. A run of PSI,
    SI, null and adaptation-only packets right before it opens the group
    with it. Packets before the first random access point are dropped
    (`dropped`); bytes outside a packet are skipped (`resyncs`).

    A PAT that changes a field the track declares, the mode (one program
    or several) or an unmodified-program's program number, starts the
    next track generation (§6: a new track, not a changed one): the
    objects so far end the old one, and the new one starts as a live
    start would, at the next random access point. Group IDs go on
    across generations.

    Each object's first packet is placed on the PCR timeline: between
    the PCRs around it, or past the last at the last two PCRs' rate. The
    timeline is continuous: a PCR wrap is unwrapped, and a discontinuity
    (flagged, or a PCR going back) goes on from the last two PCRs.

    feed(chunk, now_us) returns the objects the chunk completes;
    flush(now_us) the partial one once flush_due() has passed; close()
    the rest at the end of the stream. The PAT, the first program's PMT
    and the mux rate measured from its PCR are kept for the catalog;
    `psi_changed` (a change within the track) and `bitrate_changed` are
    raised for the caller to clear."""

    def __init__(self, packets: int = DEFAULT_PACKETS,
                 flush_s: float = FLUSH_S):
        if packets < 1:
            raise ValueError("packets must be positive")
        self.packets = packets
        self._obj = packets * TS_PACKET
        self._flush_us = int(flush_s * 1_000_000)
        self._buf = bytearray()
        self._pend = bytearray()
        # Offset in _pend of a PSI/SI run that may lead the next group,
        # and its first packet's arrival.
        self._mark: Optional[int] = None
        self._mark_at = 0
        self._old = 0
        self._pend_at = 0
        self._now = 0
        self._key = False
        self._pend_idx = 0
        self.generation = 1
        self.groups = 0
        self.group_id = -1
        # Recent PCRs as (27 MHz ticks, unwrapped, packet index).
        self._pcrs: Deque[Tuple[int, int]] = deque(maxlen=8)
        self._pcr_base = 0
        self.packets_in = 0
        self.dropped = 0
        self.resyncs = 0
        self.psi = Psi()
        self.rap_pid: Optional[int] = None
        self._rap_type: Optional[int] = None
        self._rap_pts: Optional[int] = None
        self._pts_last: Optional[int] = None
        self._pts_base = 0
        self.psi_changed = False
        self.bitrate: Optional[int] = None
        self.bitrate_changed = False
        self._pcr0: Optional[Tuple[int, int]] = None
        self._group_pts: Optional[int] = None
        self._warned = False
        # The fields the current generation declares, once it has a PSI.
        self._fields: Optional[Tuple[str, Optional[int]]] = None
        self._cut = False

    # -- what the catalog says --

    @property
    def ready(self) -> bool:
        """The PAT and the first program's PMT are in."""
        return self.psi.ready

    @property
    def programs(self) -> List[Tuple[int, int]]:
        return self.psi.programs

    @property
    def program_number(self) -> Optional[int]:
        return self.psi.program_number

    @property
    def pcr_pid(self) -> Optional[int]:
        return self.psi.pcr_pid

    @property
    def streams(self) -> List[Tuple[int, int]]:
        return self.psi.streams

    @property
    def init_data(self) -> bytes:
        """The PAT and the first program's PMT packets, as received."""
        p = self.psi
        return b''.join((p.pat[1] if p.pat else [])
                        + (p.pmt[1] if p.pmt else []))

    @property
    def mode(self) -> str:
        return (MODE_UNMODIFIED_PROGRAM if len(self.programs) <= 1
                else MODE_UNMODIFIED_MULTIPLEX)

    @property
    def video(self) -> bool:
        return self._rap_type in _VIDEO_TYPES

    def _declared(self) -> Tuple[str, Optional[int]]:
        """The PSI-derived fields a track cannot change (§6, MSF §5.3)."""
        mode = self.mode
        program = (self.programs[0][0] if self.programs
                   and mode == MODE_UNMODIFIED_PROGRAM else None)
        return mode, program

    def catalog_track(self, name: str, *, bitrate: Optional[int] = None,
                      target_latency: Optional[int] = None) -> CatalogTrack:
        """The track entry for this stream. A multiplex names no
        reference program, so it claims no random access (§6.9) and
        takes no role, as moq2ts publishes it."""
        program = self.mode == MODE_UNMODIFIED_PROGRAM
        return catalog_track(
            name, mode=self.mode,
            program_number=self.program_number if program else None,
            pcr_pid=self.pcr_pid if program else None,
            random_access=True if program else None,
            init_ref=init_data_ref(name) if self.ready else None,
            role=(('video' if self.video else 'audio') if program else None),
            bitrate=bitrate or self.bitrate, target_latency=target_latency)

    # -- packets --

    def feed(self, chunk, now_us: int) -> List[TsObject]:
        self._now = now_us
        self._old = len(self._pend)
        if self._buf:
            self._buf += chunk
            data = self._buf
        else:
            data = chunk
        out: List[TsObject] = []
        n = len(data)
        pos = 0
        P = TS_PACKET
        while n - pos >= P:
            if data[pos] != 0x47 or (n - pos >= 2 * P
                                     and data[pos + P] != 0x47):
                pos += 1
                self.resyncs += 1
                continue
            self._packet(data, pos, out)
            pos += P
        self._buf = bytearray(data[pos:])
        return out

    def flush_due(self) -> Optional[int]:
        """When the partial object must go, µs since the epoch; None
        when there is none, or only a PSI run waits for what follows."""
        if not self.groups or not self._pend or self._mark == 0:
            return None
        return self._pend_at + self._flush_us

    def flush(self, now_us: int) -> List[TsObject]:
        """The partial object; a PSI run stays for what follows it."""
        self._now = now_us
        out: List[TsObject] = []
        if self.groups:
            self._emit_full(out)
            head = len(self._pend) if self._mark is None else self._mark
            if head:
                self._emit(head, out)
            if self._mark is not None:
                self._pend_at = self._mark_at
        return out

    def close(self) -> List[TsObject]:
        """Everything held, at the end of the stream."""
        self._buf = bytearray()
        self._mark = None
        return self.flush(self._now)

    def _packet(self, data, pos: int, out: List[TsObject]) -> None:
        b1 = data[pos + 1]
        pid = ((b1 & 0x1F) << 8) | data[pos + 2]
        self.packets_in += 1
        if pid < 0x20 or pid in self.psi.table_pids:
            if pid == 0 or pid == self.psi.pmt_pid:
                if self.psi.feed(data, pos, pid, b1 & 0x40):
                    self._psi()
            if self._cut:
                self._cut = False
                self._next_generation(out)
            if self._mark is None:
                if not self.groups:
                    self._pend.clear()
                self._mark = len(self._pend)
                self._mark_at = self._now
            self._append(data, pos)
            return
        if pid == self.pcr_pid:
            self._pcr(data, pos)
        if pid != _NULL_PID and data[pos + 3] & 0x10:
            if (pid == self.rap_pid and b1 & 0x40 and self.ready
                    and self._is_rap(data, pos)):
                self._open_group(data, pos, out)
                return
            # Elementary stream data: the PSI run leads no group.
            self._mark = None
            if not self.groups:
                self._pend.clear()
        if not self.groups and self._mark is None:
            self.dropped += 1
            if self.dropped == _RAP_WARN_PACKETS and not self._warned:
                self._warned = True
                logger.warning(f"msfts: no random access point in "
                               f"{_RAP_WARN_PACKETS} packets; nothing sent")
            return
        self._append(data, pos)
        if self.groups:
            self._emit_full(out)

    def _psi(self) -> None:
        """A PAT or PMT that differs from the last one."""
        p = self.psi
        if p.pmt is not None:
            first = next((s for s in p.streams if s[0] in _VIDEO_TYPES),
                         p.streams[0] if p.streams else (None, None))
            self._rap_type, self.rap_pid = first
        if not p.programs:
            return
        fields = self._declared()
        if self._fields is None:
            if p.ready:
                self._fields = fields
        elif fields != self._fields:
            self._fields = fields
            self._cut = True
        elif p.ready and self.groups:
            self.psi_changed = True

    def _next_generation(self, out: List[TsObject]) -> None:
        """End the open track before the PSI run that changed it; the
        run waits for the next track's first random access point."""
        head = len(self._pend) if self._mark is None else self._mark
        if self.groups and head:
            self._emit(head, out)
        else:
            self._drop(head)
        self._mark = 0 if self._pend else None
        self.generation += 1
        self.groups = 0
        self._key = False
        self._group_pts = None
        self.psi_changed = False
        self.bitrate = None
        self.bitrate_changed = False
        self._pcr0 = None

    def _append(self, data, pos: int) -> None:
        if not self._pend:
            self._pend_at = self._now
            self._pend_idx = self.packets_in - 1
        self._pend += data[pos:pos + TS_PACKET]

    def _drop(self, size: int) -> None:
        del self._pend[:size]
        self._pend_idx += size // TS_PACKET

    def _pcr_at(self, idx: int) -> Optional[int]:
        """Packet `idx` on the PCR timeline: between the PCRs around it,
        else at the rate of the last two."""
        pcrs = self._pcrs
        if not pcrs:
            return None
        after = next((k for k, (_, i) in enumerate(pcrs) if i >= idx), None)
        if after is not None and pcrs[after][1] == idx:
            return pcrs[after][0]
        if after is None:
            a, b = (pcrs[-2], pcrs[-1]) if len(pcrs) > 1 else (None, pcrs[-1])
        elif after == 0:
            a, b = (pcrs[0], pcrs[1]) if len(pcrs) > 1 else (None, pcrs[0])
        else:
            a, b = pcrs[after - 1], pcrs[after]
        if a is None or b[1] == a[1]:
            return b[0]
        return a[0] + (idx - a[1]) * (b[0] - a[0]) // (b[1] - a[1])

    def _emit(self, size: int, out: List[TsObject]) -> None:
        pend = self._pend
        pts = self._rap_pts if self._key else None
        out.append(TsObject(bytes(pend[:size]), self._key, self._pend_at,
                            self.generation, self.group_id, pts,
                            self._pcr_at(self._pend_idx)))
        del pend[:size]
        self._pend_idx += size // TS_PACKET
        self._key = False
        if self._mark is not None:
            self._mark -= size
        self._old -= size
        if self._old <= 0:
            self._old = 0
            self._pend_at = self._now

    def _emit_full(self, out: List[TsObject]) -> None:
        obj = self._obj
        if (self._mark is not None
                and len(self._pend) - self._mark > _RUN_PACKETS * TS_PACKET):
            self._mark = None   # no random access point followed the PSI
        limit = len(self._pend) if self._mark is None else self._mark
        while limit >= obj:
            self._emit(obj, out)
            limit -= obj

    def _open_group(self, data, pos: int, out: List[TsObject]) -> None:
        """A random access point: what precedes the PSI run before it
        ends the open group, and the run opens the next with it. Object 0
        holds the random access point (§5.2), so a run of `packets` or
        more makes it longer."""
        head = len(self._pend) if self._mark is None else self._mark
        if self.groups and head:
            self._emit(head, out)
        elif head:
            self._drop(head)
        self._mark = None
        self._key = True
        self.groups += 1
        self.group_id += 1
        self._append(data, pos)
        if len(self._pend) > self._obj:
            self._emit(len(self._pend), out)
        self._emit_full(out)

    def _unwrap(self, pts: Optional[int]) -> Optional[int]:
        if pts is None:
            return None
        if self._pts_last is not None and pts < self._pts_last - (1 << 32):
            self._pts_base += 1 << 33
        self._pts_last = pts
        return self._pts_base + pts

    def _is_rap(self, data, pos: int) -> bool:
        end = pos + TS_PACKET
        off = pos + 4
        rai = False
        if data[pos + 3] & 0x20:
            alen = data[off]
            rai = bool(alen and data[off + 1] & 0x40)
            off += 1 + alen
        pts = _pes_pts(data, off, end)
        if not self.video:
            if pts is None:
                return False
            if (self._group_pts is not None
                    and 0 <= pts - self._group_pts < _AUDIO_GROUP_PTS):
                return False
            self._group_pts = pts
            self._rap_pts = self._unwrap(pts)
            return True
        if not rai and not self._key_syntax(data, off, end):
            return False
        self._rap_pts = self._unwrap(pts)
        return True

    def _key_syntax(self, data, off: int, end: int) -> bool:
        """A key frame's own syntax in the PES's first packet."""
        if off + 9 > end:
            return False
        es = off + 9 + data[off + 8]
        seg = bytes(data[es:end])
        t = self._rap_type
        if t in (0x01, 0x02):
            return any(code in seg for code in _MPEG2_KEY)
        if t not in (0x1B, 0x24):
            return False
        i = seg.find(_START)
        while 0 <= i < len(seg) - 3:
            h = seg[i + 3]
            if t == 0x1B and (h & 0x1F) in (5, 7):
                return True
            nal = (h >> 1) & 0x3F
            if t == 0x24 and (16 <= nal <= 23 or nal in (32, 33)):
                return True
            i = seg.find(_START, i + 3)
        return False

    def _pcr(self, data, pos: int) -> None:
        """Keep the PCR on a continuous timeline; measure the mux rate
        from the packets between two a second or more apart."""
        if not data[pos + 3] & 0x20 or data[pos + 4] < 7:
            return
        flags = data[pos + 5]
        if not flags & 0x10:
            return
        b = data[pos + 6:pos + 12]
        pcr = ((b[0] << 25 | b[1] << 17 | b[2] << 9 | b[3] << 1 | b[4] >> 7)
               * 300 + ((b[4] & 1) << 8 | b[5]))
        idx = self.packets_in - 1
        pcrs = self._pcrs
        full = pcr + self._pcr_base
        if pcrs and full < pcrs[-1][0] - _PCR_WRAP // 2:
            self._pcr_base += _PCR_WRAP
            full += _PCR_WRAP
        if pcrs and (flags & 0x80 or full < pcrs[-1][0]):
            # A discontinuity: go on where the last two PCRs put it.
            last, at = pcrs[-1]
            step = ((last - pcrs[-2][0]) / (at - pcrs[-2][1])
                    if len(pcrs) > 1 and at > pcrs[-2][1] else 0)
            expect = last + int((idx - at) * step)
            self._pcr_base += expect - full
            full = expect
        pcrs.append((full, idx))
        if self.bitrate is not None:
            return
        if self._pcr0 is None or flags & 0x80:
            self._pcr0 = (pcr, self.packets_in)
            return
        pcr0, n0 = self._pcr0
        dt = pcr - pcr0
        if not 0 < dt <= 10 * 27_000_000:
            self._pcr0 = (pcr, self.packets_in)
        elif dt >= 27_000_000:
            self.bitrate = ((self.packets_in - n0) * TS_PACKET * 8
                            * 27_000_000 // dt)
            self.bitrate_changed = True


# -- subscriber: objects back in order ---------------------------------

class PacketOrder:
    """Objects of an mpeg2ts track released in Group ID and Object ID
    order (§5.1), each as soon as that order allows.

    The first object pushed sets the start. Objects of a later group wait
    while the current group is open; end_group() (its stream ended, or an
    End of Group) releases them, and so does timeout() once they have
    waited `hold_s` with no progress, or until `hold_until` if later,
    skipping what is missing (`skipped`). Groups passed to wait() are
    being repaired: the order stops at each until its end_group(). An
    object behind what was released is dropped (`late`). Items are
    opaque; None stands for a rejected object."""

    def __init__(self, hold_s: float = HOLD_S):
        self.hold_s = hold_s
        self._started = False
        self._group = 0
        self._next = 0
        self._done = False
        self._held: Dict[int, Dict[int, Any]] = {}
        self._ended: Set[int] = set()
        self._since: Optional[float] = None
        self._waiting: Set[int] = set()
        self.hold_until: Optional[float] = None
        self.late = 0
        self.skipped = 0

    @property
    def deadline(self) -> Optional[float]:
        """When timeout() gives up waiting; None when nothing waits."""
        if self._since is None:
            return None
        due = self._since + self.hold_s
        return due if self.hold_until is None else max(due, self.hold_until)

    @property
    def held(self) -> int:
        return sum(len(h) for h in self._held.values())

    def push(self, group: int, obj: int, item, now: float) -> List[tuple]:
        """(group, object, item) of each object this one releases."""
        out: List[tuple] = []
        if not self._started:
            self._started = True
            self._group, self._next = group, obj
        cur = self._group
        if group < cur or (group == cur and obj < self._next):
            self.late += 1
            return out
        if group == cur and obj == self._next:
            out.append((group, obj, item))
            self._next += 1
        elif (self._done and obj == 0 and group > cur
              and not any(g < group for g in self._held)):
            self._group, self._next, self._done = group, 1, False
            out.append((group, obj, item))
        else:
            self._held.setdefault(group, {})[obj] = item
        self._settle(now, out)
        return out

    def wait(self, groups) -> None:
        """Objects of `groups` are still to come, from a repair."""
        self._waiting.update(g for g in groups
                             if not self._started or g >= self._group)

    def end_group(self, group: int, now: float) -> List[tuple]:
        """Group `group` delivers nothing more."""
        out: List[tuple] = []
        self._waiting.discard(group)
        if not self._started or group < self._group:
            return out
        if group == self._group:
            self._done = True
        else:
            self._ended.add(group)
        self._settle(now, out)
        return out

    def timeout(self, now: float) -> List[tuple]:
        """Past the deadline, skip to the next object held."""
        out: List[tuple] = []
        due = self.deadline
        if due is None or now < due or not self._held:
            return out
        g = min(self._held)
        if g != self._group:
            self._group, self._done = g, False
        self._next = min(self._held[g])
        self.skipped += 1
        self._since = None
        self._settle(now, out)
        return out

    def drain(self) -> List[tuple]:
        """Everything held, in order."""
        out = [(g, o, self._held[g][o]) for g in sorted(self._held)
               for o in sorted(self._held[g])]
        self._held.clear()
        self._since = None
        return out

    def _settle(self, now: float, out: List[tuple]) -> None:
        progressed = bool(out)
        while True:
            g = self._group
            h = self._held.get(g)
            while h and self._next in h:
                out.append((g, self._next, h.pop(self._next)))
                self._next += 1
                progressed = True
            if h is not None and not h:
                del self._held[g]
            if g in self._ended:
                self._ended.discard(g)
                self._done = True
            if self._done:
                nxt = min(self._held) if self._held else None
                gap = [w for w in self._waiting if w > g]
                if gap and (nxt is None or min(gap) < nxt):
                    self._group, self._next, self._done = min(gap), 0, False
                    continue
                if nxt is not None and 0 in self._held[nxt]:
                    self._group, self._next, self._done = nxt, 0, False
                    continue
            break
        self._ended = {e for e in self._ended if e > self._group}
        self._waiting = {w for w in self._waiting if w >= self._group}
        if not self._held:
            self._since = None
        elif progressed or self._since is None:
            self._since = now


# -- tracks ---------------------------------------------------------------

class PcrTimestamps:
    """draft-frindell-moq-timestamp timing for an mpeg2ts track, its
    objects placed by the PCR at their first packet on the ingest wall
    clock (POSIX, no CLOCK_ID).

    TIMESCALE 90 kHz, the PTS clock and the PCR base. TIMESTAMP_MAPPING
    (first group, W0, 0, 0) anchors every object at W0, the track's
    creation; each object's OBJECT_TIMESTAMP is the signed correction
    to its own time: its PCR plus the least arrival-minus-PCR offset of
    the last 5 to 10 s. A time is thus never later than its arrival,
    and follows a wall clock that steps or a source clock that drifts
    within that window. An arrival more than 1 s past its time (the
    source stalled, or a new timebase) starts the window over."""

    TIMESCALE = 90_000
    mapped = True
    group_multiplier = 0

    def __init__(self, first_group: int, wall0_us: int):
        self.first_group = first_group
        self.base = wall0_us * self.TIMESCALE // 1_000_000
        self._bucket = -1
        self._min = 0
        self._prev: Optional[int] = None

    def track_properties(self, vi64: bool) -> Dict[int, Any]:
        return {TIMESCALE: self.TIMESCALE,
                TIMESTAMP_MAPPING: encode_mapping(self.first_group,
                                                  self.base, 0, 0)}

    def wall_us(self, pcr: Optional[int], at_us: int) -> int:
        """An object's time on the ingest wall clock, µs: by its PCR
        (27 MHz) when it has one, else its arrival."""
        if pcr is None:
            return at_us
        us = pcr // 27
        off = at_us - us
        bucket = at_us // _OFFSET_WINDOW_US
        if bucket != self._bucket:
            self._prev = self._min if bucket == self._bucket + 1 else None
            self._bucket, self._min = bucket, off
        elif off < self._min:
            self._min = off
        prev = self._prev
        low = self._min if prev is None else min(self._min, prev)
        if off - low > _OFFSET_RESET_US:
            self._prev, self._min = None, off
            low = off
        return us + low

    def group_for(self, capture_us) -> None:
        return None

    def object_properties(self, group_id: int, object_id: int,
                          capture_us: int) -> Dict[int, Any]:
        c = capture_us * self.TIMESCALE // 1_000_000 - self.base
        return {OBJECT_TIMESTAMP: zigzag_encode(c)}


class MsftsTrackPublisher(LocTrackPublisher):
    """Publishes an mpeg2ts track: each send_frame() payload is one
    object of whole TS packets, a key frame opening a group at a random
    access point. No LOC property rides the objects; `frindell` (a
    PcrTimestamps) adds the draft-frindell-moq-timestamp ones."""

    def __init__(self, session, namespace: str, trackname: str, *,
                 priority: int = 128, fetch_history_bytes: int = 0,
                 queue_size: int = 1024):
        super().__init__(session, namespace, trackname, priority=priority,
                         config_property=None, queue_size=queue_size,
                         fetch_history_bytes=fetch_history_bytes)

    def _timestamp_ids(self, session) -> tuple:
        return ()

    def _prepare(self, frame: LocFrame, group_id: int,
                 obj_id: int) -> Optional[bytes]:
        if self.frindell is not None and frame.capture_us is not None:
            frame.extensions = self.frindell.object_properties(
                group_id, obj_id, frame.capture_us)
        return frame.payload

    def _object_extensions(self, frame: LocFrame, group_start: bool,
                           session=None) -> Dict[int, Any]:
        """The object's own properties, shared by every peer; none on
        d14, which has no TIMESCALE to give OBJECT_TIMESTAMP."""
        exts = frame.extensions
        if not exts or not (session or self.session)._profile.track_properties:
            return {}
        return exts


class MsftsTrackSubscriber(LocTrackSubscriber):
    """Subscribes to an mpeg2ts track. on_frame(frame, group_id,
    object_id) gets the objects in Group ID and Object ID order through
    a PacketOrder; on_arrival sees them as they arrive. An object that is
    not whole source packets of `packet_size` is rejected
    (`objects_rejected`). `order` counts late and skipped objects.

    With `repair_s`, Objects the subscription will not deliver (the rest
    of a group whose stream was reset, groups skipped over) are FETCHed
    from the publisher, ahead of live objects, and the order waits up to
    `repair_s` for them: `repairs` FETCHes sent, `repaired` Objects they
    brought, `repairs_expired` given up."""

    def __init__(self, session, namespace: str,
                 trackname: Optional[str] = None, *,
                 packet_size: int = TS_PACKET, hold_s: float = HOLD_S,
                 on_frame: Optional[Callable] = None,
                 on_arrival: Optional[Callable] = None,
                 auth_token: Optional[bytes] = None,
                 timestamp_errors: str = 'cancel',
                 repair_s: float = 0.0):
        super().__init__(session, namespace, cast(str, trackname),
                         on_frame=on_frame, auth_token=auth_token,
                         on_arrival=on_arrival,
                         timestamp_errors=timestamp_errors)
        self.packet_size = packet_size
        self.order = PacketOrder(hold_s)
        self.repair_s = repair_s
        self.repairs = 0
        self.repaired = 0
        self.repairs_expired = 0
        self._top: Dict[int, int] = {}
        self._top_group: Optional[int] = None
        self._repairing: Dict[int, Tuple[range, float]] = {}
        self._timer = None
        self._due: Optional[float] = None
        # Set at End of Track or PUBLISH_DONE; on_end() is then called.
        self.ended = False
        self.on_end: Optional[Callable[[], None]] = None
        self.on_done = self._done

    async def subscribe(self, *args, **kwargs):
        await super().subscribe(*args, **kwargs)
        self.session.register_stream_end_handler(self.track_alias,
                                                 self._stream_ended)

    def _stream_ended(self, group_id, subgroup_id, clean=True,
                      reset_code=0) -> None:
        if not clean and self.repair_s:
            self._repair(group_id, self._top.get(group_id, -1) + 1, group_id)
            return
        self._release(self.order.end_group(group_id, _loop_time()))

    def _on_object(self, msg, size, recv_us, group_id, subgroup_id) -> None:
        status = getattr(msg, "status", None)
        if self.repair_s and status in (None, ObjectStatus.NORMAL):
            gid = getattr(msg, 'group_id', None)
            self._seen(group_id if gid is None else gid, msg.object_id)
        super()._on_object(msg, size, recv_us, group_id, subgroup_id)
        if status in (ObjectStatus.END_OF_GROUP, ObjectStatus.END_OF_TRACK):
            gid = getattr(msg, 'group_id', None)
            gid = gid if gid is not None else group_id
            self._release(self.order.end_group(gid, _loop_time()))
        if status == ObjectStatus.END_OF_TRACK:
            self._done(None)

    def _seen(self, group: int, obj: int) -> None:
        """Track the highest Object per group; a jump over whole groups
        repairs them."""
        top = self._top_group
        if top is not None and group > top + 1:
            self._repair(top + 1, 0, group - 1)
        if top is None or group > top:
            self._top_group = group
            for g in [g for g in self._top if g < group - 8]:
                del self._top[g]
        if obj > self._top.get(group, -1):
            self._top[group] = obj

    def _repair(self, first_group: int, first_object: int,
                last_group: int) -> None:
        """FETCH {first_group, first_object} to the end of last_group,
        and hold the order for them up to repair_s."""
        groups = range(first_group, last_group + 1)
        self.order.wait(groups)
        due = _loop_time() + self.repair_s
        try:
            msg = self.session.fetch(
                self.namespace, self.trackname, subscriber_priority=0,
                start_group=first_group, start_object=first_object,
                end_group=last_group, end_object=0,
                on_object=self._on_fetch_object)
        except Exception as e:
            logger.warning(f"msfts: {self.trackname}: repair of groups "
                           f"{first_group}-{last_group} not sent: {e}")
            self._end_repair(groups)
            return
        self.repairs += 1
        self._repairing[msg.request_id] = (groups, due)
        self.order.hold_until = max(self.order.hold_until or 0.0, due)
        asyncio.ensure_future(self._await_repair(msg.request_id))

    def _on_fetch_object(self, msg, size, recv_us, request_id) -> None:
        if request_id not in self._repairing:
            return
        if getattr(msg, "status", None) in (None, ObjectStatus.NORMAL):
            self.repaired += 1
        self._on_object(msg, size, recv_us, msg.group_id,
                        getattr(msg, 'subgroup_id', 0))

    async def _await_repair(self, request_id: int) -> None:
        groups, due = self._repairing[request_id]
        done = await self.session.await_fetch_done(
            request_id, timeout=max(0.0, due - _loop_time()))
        self._repairing.pop(request_id, None)
        if not done:
            self.repairs_expired += 1
            self.session.unregister_fetch_object_handler(request_id)
        self.order.hold_until = max(
            (d for _, d in self._repairing.values()), default=None)
        self._end_repair(groups)

    def _end_repair(self, groups: range) -> None:
        now = _loop_time()
        for g in groups:
            self._release(self.order.end_group(g, now))

    def _done(self, msg) -> None:
        if self.ended:
            return
        self.ended = True
        self.flush()
        if self.on_end is not None:
            self.on_end()

    def _unpackage(self, msg, group_id: int) -> Optional[bytes]:
        payload = bytes(msg.payload)
        try:
            check_payload(payload, self.packet_size)
        except MsftsError as e:
            self.objects_rejected += 1
            if self.objects_rejected <= 3:
                logger.warning(f"msfts: {self.trackname}: object "
                               f"{group_id}.{msg.object_id} rejected: {e}")
            self._release(self.order.push(group_id, msg.object_id, None,
                                          _loop_time()))
            return None
        return payload

    def _deliver(self, frame: LocFrame, group_id: int,
                 object_id: int) -> None:
        self._release(self.order.push(group_id, object_id, frame,
                                      _loop_time()))

    def flush(self) -> None:
        """Release everything held, in order (the track is ending)."""
        self._release(self.order.drain())

    def _release(self, released: List[tuple]) -> None:
        if self.on_frame:
            for g, o, frame in released:
                if frame is not None:
                    self.on_frame(frame, g, o)
        due = self.order.deadline
        if due == self._due:
            return
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._due = due
        if due is not None:
            self._timer = asyncio.get_running_loop().call_at(due,
                                                             self._on_timer)

    def _on_timer(self) -> None:
        self._timer = None
        self._due = None
        released = self.order.timeout(_loop_time())
        if released:
            logger.warning(f"msfts: {self.trackname}: no end of the open "
                           f"group in {self.order.hold_s:g} s; skipped "
                           f"ahead")
        self._release(released)


def _loop_time() -> float:
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        return time.monotonic()


__all__ = [
    "TS_PACKET", "M2TS_PACKET", "MIME_TYPE", "DEFAULT_PACKETS", "FLUSH_S",
    "HOLD_S", "MODES", "MODE_UNMODIFIED_PROGRAM",
    "MODE_UNMODIFIED_MULTIPLEX", "MODE_PER_PROGRAM", "MODE_ES_PACKETS",
    "MsftsError", "MsftsTrack", "TsObject", "TsPackager", "PacketOrder",
    "PcrTimestamps", "MsftsTrackPublisher", "MsftsTrackSubscriber",
    "check_payload",
    "catalog_track", "init_data_ref", "track_issues",
]
