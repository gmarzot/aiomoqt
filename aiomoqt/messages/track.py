from enum import IntEnum
from dataclasses import dataclass
from sortedcontainers import SortedDict
from typing import Optional, Dict, List, Tuple, Union
import time

from . import (MOQTUnderflow, MOQTMessage, ObjectStatus, DataStreamType,
               MOQT_DEFAULT_PRIORITY, BUF_SIZE,
               SUBGROUP_HEADER_BASE, SUBGROUP_ID_ZERO, SUBGROUP_ID_FIRST_OBJ, SUBGROUP_ID_EXPLICIT,
               OBJECT_DATAGRAM_BASE, OBJECT_DATAGRAM_STATUS_BASE)
from ..utils.buffer import Buffer, BufferReadError
from ..utils.logger import get_logger

logger = get_logger(__name__)



@dataclass
class Group:
    """MOQT Group data accumulator"""
    group_id: int
    objects: Optional[SortedDict[int, Buffer]]
    _max_obj_id: int = -1
    _last_update: int = 0

    def __post_init__(self):
        if self.objects is None:
            self.objects = SortedDict()

    def add_object(self, obj_id: int, buf: Buffer) -> None:
        """Add an object to the track's structure."""
        self.objects[obj_id] = buf
        if obj_id > self._max_obj_id:
            self._max_obj_id = obj_id

    @property
    def max_obj_id(self) -> int:
        return self._max_obj_id

    @property
    def last_update(self) -> float:
        return self._last_update



@dataclass
class Track:
    """Represents a MOQT track."""
    namespace: Tuple[bytes, ...]
    trackname: bytes
    groups: Optional[SortedDict[int, Group]]
    _max_grp_id: int = -1

    def __post_init__(self):
        if self.groups is None:
            self.groups = SortedDict()

    def group(self, grp_id: int) -> Group:
        group = self.groups.setdefault(grp_id, Group(grp_id))
        if grp_id > self._max_grp_id:
            self._max_grp_id = grp_id
        return group


@dataclass
class SubgroupHeader(MOQTMessage):
    """Draft-14 SUBGROUP_HEADER (types 0x10-0x1D).

    Type byte encodes flags from base 0x10:
      Bit 0 (0x01): Extensions present in objects
      Bits 1-2:     Subgroup ID mode (0=zero, 1=first_obj_id, 2=explicit)
      Bit 3 (0x08): Contains end of group

    Wire: Type (i), Track Alias (i), Group ID (i), [Subgroup ID (i)], Publisher Priority (8)
    Subgroup ID field only present when mode == SUBGROUP_ID_EXPLICIT.
    """
    track_alias: int
    group_id: int
    subgroup_id: Optional[int] = 0
    publisher_priority: int = MOQT_DEFAULT_PRIORITY
    extensions_present: bool = False
    end_of_group: bool = False
    subgroup_id_mode: int = SUBGROUP_ID_EXPLICIT

    def __post_init__(self):
        self.type = SUBGROUP_HEADER_BASE
        self._last_object_id = None  # runtime state for delta decoding

    def _compute_type(self) -> int:
        """Compute wire type byte from flags."""
        type_val = SUBGROUP_HEADER_BASE
        if self.extensions_present:
            type_val |= 0x01
        type_val |= (self.subgroup_id_mode & 0x03) << 1
        if self.end_of_group:
            type_val |= 0x08
        return type_val

    def serialize(self) -> Buffer:
        buf = Buffer(capacity=BUF_SIZE)
        buf.push_uint_var(self._compute_type())
        buf.push_uint_var(self.track_alias)
        buf.push_uint_var(self.group_id)
        if self.subgroup_id_mode == SUBGROUP_ID_EXPLICIT:
            buf.push_uint_var(self.subgroup_id or 0)
        buf.push_uint8(self.publisher_priority)
        return buf

    def next_object(self, payload: bytes = b'',
                    extensions: Optional[Dict] = None,
                    status: ObjectStatus = ObjectStatus.NORMAL,
                    object_id: Optional[int] = None) -> Buffer:
        """Create and serialize the next object in this subgroup.

        Handles object_id assignment, delta encoding, and extensions_present
        flag automatically. Tracks state across calls.

        Args:
            object_id: Explicit object_id. If None, auto-increments from last.

        Returns: Buffer ready to send on the stream.
        """
        if object_id is not None:
            obj_id = object_id
        else:
            obj_id = 0 if self._last_object_id is None else self._last_object_id + 1
        obj = ObjectHeader(
            object_id=obj_id,
            extensions=extensions,
            status=status,
            payload=payload
        )
        buf = obj.serialize(
            extensions_present=self.extensions_present,
            prev_object_id=self._last_object_id
        )
        self._last_object_id = obj_id
        # Resolve subgroup_id for FIRST_OBJ mode
        if self.subgroup_id_mode == SUBGROUP_ID_FIRST_OBJ and self.subgroup_id is None:
            self.subgroup_id = obj_id
        return buf

    def end_group(self, extensions: Optional[Dict] = None,
                  object_id: Optional[int] = None) -> Buffer:
        """Create and serialize an END_OF_GROUP status object.

        Args:
            object_id: Explicit object_id for the status object. If None, auto-increments.

        Returns: Buffer ready to send on the stream (typically with end_stream=True).
        """
        return self.next_object(
            payload=b'',
            extensions=extensions,
            status=ObjectStatus.END_OF_GROUP,
            object_id=object_id,
        )

    @property
    def next_object_id(self) -> int:
        """The object_id that will be assigned to the next object."""
        return 0 if self._last_object_id is None else self._last_object_id + 1

    @classmethod
    def deserialize(cls, buf: Buffer, type_val: int) -> 'SubgroupHeader':
        """Deserialize SubgroupHeader from wire, given the already-read type byte."""
        extensions_present = bool(type_val & 0x01)
        subgroup_id_mode = (type_val >> 1) & 0x03
        end_of_group = bool(type_val & 0x08)

        track_alias = buf.pull_uint_var()
        group_id = buf.pull_uint_var()

        if subgroup_id_mode == SUBGROUP_ID_EXPLICIT:
            subgroup_id = buf.pull_uint_var()
        elif subgroup_id_mode == SUBGROUP_ID_ZERO:
            subgroup_id = 0
        else:  # SUBGROUP_ID_FIRST_OBJ — resolved when first object arrives
            subgroup_id = None

        publisher_priority = buf.pull_uint8()

        return cls(
            track_alias=track_alias,
            group_id=group_id,
            subgroup_id=subgroup_id,
            publisher_priority=publisher_priority,
            extensions_present=extensions_present,
            end_of_group=end_of_group,
            subgroup_id_mode=subgroup_id_mode,
        )


@dataclass
class ObjectHeader(MOQTMessage):
    """Draft-14 object within a subgroup stream.

    Wire: Object ID Delta (i), [Ext Headers Len (i) + Ext headers (...)],
          Object Payload Length (i), [Object Status (i)], Object Payload (..)

    Delta encoding: first obj ID = delta; subsequent = prev_id + delta + 1.
    Extensions only present if the SubgroupHeader type has extensions_present.
    Object Status only if payload_length == 0.
    """
    object_id: int
    extensions: Optional[Dict[int, Union[bytes, int]]] = None
    status: Optional[ObjectStatus] = ObjectStatus.NORMAL
    payload: bytes = b''

    def serialize(self, extensions_present: bool = True, prev_object_id: Optional[int] = None) -> Buffer:
        """Serialize for stream transmission.

        Args:
            extensions_present: Whether subgroup header has extensions flag set.
            prev_object_id: Previous object's ID for delta encoding (None = first object).
        """
        payload_len = len(self.payload)
        buf = Buffer(capacity=(BUF_SIZE + payload_len))

        # Delta encoding
        if prev_object_id is None:
            delta = self.object_id
        else:
            delta = self.object_id - prev_object_id - 1
        buf.push_uint_var(delta)

        # Extensions conditional on subgroup header flag
        # Per spec: extensions MUST NOT be present on non-NORMAL status objects
        if extensions_present and self.status == ObjectStatus.NORMAL:
            MOQTMessage._extensions_encode(buf, self.extensions)
        elif extensions_present:
            MOQTMessage._extensions_encode(buf, None)  # empty extensions

        if self.status == ObjectStatus.NORMAL and self.payload:
            buf.push_uint_var(payload_len)
            buf.push_bytes(self.payload)
        else:
            buf.push_uint_var(0)  # Zero length
            buf.push_uint_var(self.status)  # Status code

        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, buf_len: int,
                    extensions_present: bool = True,
                    prev_object_id: Optional[int] = None) -> 'ObjectHeader':
        """Deserialize from stream transmission.

        Args:
            buf_len: Total buffer length for underflow detection.
            extensions_present: Whether subgroup header has extensions flag set.
            prev_object_id: Previous object's ID for delta decoding (None = first object).
        """
        delta = buf.pull_uint_var()

        # Resolve actual object_id from delta
        if prev_object_id is None:
            object_id = delta
        else:
            object_id = prev_object_id + delta + 1

        # Extensions conditional on subgroup header flag
        extensions = None
        if extensions_present:
            extensions = MOQTMessage._extensions_decode(buf)

        # Get payload or status
        payload_len = buf.pull_uint_var()
        remaining = buf_len - buf.tell()
        if payload_len == 0:  # Zero length means status code follows
            status = ObjectStatus(buf.pull_uint_var())
            payload = b''
        elif payload_len > remaining:
            raise MOQTUnderflow(buf.tell(), buf.tell() + payload_len)
        else:
            status = ObjectStatus.NORMAL
            try:
                payload = buf.pull_bytes(payload_len)
            except BufferReadError:
                raise MOQTUnderflow(buf.tell(), buf.tell() + payload_len)

        return cls(
            object_id=object_id,
            extensions=extensions,
            status=status,
            payload=payload
        )


@dataclass
class FetchHeader(MOQTMessage):
    """MOQT fetch stream header.

    Carries runtime state _prior_obj used by the receive path to resolve
    delta-encoded FetchObject fields per d16 spec §10.4.4. Not part of
    the wire format.
    """
    request_id: int

    def __post_init__(self):
        self._prior_obj: Optional['FetchObject'] = None

    def serialize(self) -> bytes:
        buf = Buffer(capacity=BUF_SIZE)
        buf.push_uint_var(DataStreamType.FETCH_HEADER)

        buf.push_uint_var(self.request_id)

        return buf

    @classmethod
    def deserialize(cls, buf: Buffer) -> 'FetchHeader':
        request_id = buf.pull_uint_var()
        return cls(request_id=request_id)

# Draft-16 FetchObject Serialization Flag bits (spec §10.4.4)
# The lower 2 bits encode subgroup ID mode
FETCH_FLAG_SUBGROUP_MASK = 0x03
FETCH_FLAG_SG_ZERO = 0x00          # subgroup_id = 0
FETCH_FLAG_SG_PRIOR = 0x01         # subgroup_id = prior object's subgroup
FETCH_FLAG_SG_PRIOR_PLUS = 0x02    # subgroup_id = prior + 1
FETCH_FLAG_SG_PRESENT = 0x03       # subgroup_id field present

FETCH_FLAG_OBJECT_ID_PRESENT = 0x04   # else: prior + 1
FETCH_FLAG_GROUP_ID_PRESENT = 0x08    # else: prior group_id
FETCH_FLAG_PRIORITY_PRESENT = 0x10    # else: prior priority
FETCH_FLAG_EXTENSIONS_PRESENT = 0x20  # else: no extensions
FETCH_FLAG_DATAGRAM = 0x40            # ignore subgroup bits

# Special flag values for end-of-range markers
FETCH_FLAGS_END_NON_EXISTENT = 0x8C  # End of Non-Existent Range
FETCH_FLAGS_END_UNKNOWN = 0x10C      # End of Unknown Range


@dataclass
class FetchObject(MOQTMessage):
    """Object within a fetch stream.

    Two wire formats:

    Draft-14 (spec §10.4.4): explicit fields
        Group ID (i), Subgroup ID (i), Object ID (i), Publisher Priority (8),
        Extension Headers Length (i), [Extensions], Payload Length (i),
        [Status (i)], Payload (..)

    Draft-16 (spec §10.4.4): Serialization Flags + conditional fields
        Serialization Flags (i),
        [Group ID (i),] [Subgroup ID (i),] [Object ID (i),]
        [Priority (8),] [Extensions (..),]
        Payload Length (i), [Payload (..)]

    For d16, the encoder produces fully-explicit objects (all flag bits
    set) by default. Decoder honours flags including delta references to
    the prior object on the same stream.
    """
    group_id: int = 0
    subgroup_id: int = 0
    object_id: int = 0
    publisher_priority: int = MOQT_DEFAULT_PRIORITY
    extensions: Optional[Dict[int, bytes]] = None
    status: ObjectStatus = ObjectStatus.NORMAL
    payload: bytes = b''
    # d16 only: end-of-range marker flag (0x8C or 0x10C)
    end_of_range: Optional[int] = None

    def serialize(self) -> Buffer:
        from ..context import is_draft16_or_later
        if is_draft16_or_later():
            return self._serialize_d16()
        return self._serialize_d14()

    def _serialize_d14(self) -> Buffer:
        buf = Buffer(capacity=BUF_SIZE + len(self.payload))
        buf.push_uint_var(self.group_id)
        buf.push_uint_var(self.subgroup_id)
        buf.push_uint_var(self.object_id)
        buf.push_uint8(self.publisher_priority)
        MOQTMessage._extensions_encode(buf, self.extensions)
        if self.status == ObjectStatus.NORMAL and len(self.payload) > 0:
            buf.push_uint_var(len(self.payload))
            buf.push_bytes(self.payload)
        else:
            buf.push_uint_var(0)
            buf.push_uint_var(self.status)
        return buf

    def _serialize_d16(self) -> Buffer:
        """Encode with d16 Serialization Flags. Default: all-explicit."""
        buf = Buffer(capacity=BUF_SIZE + len(self.payload))

        # End-of-range markers carry only group_id + object_id
        if self.end_of_range is not None:
            buf.push_uint_var(self.end_of_range)
            buf.push_uint_var(self.group_id)
            buf.push_uint_var(self.object_id)
            return buf

        # Default: emit fully-explicit object (no delta refs).
        # Subgroup mode = 0x03 (present), object/group/priority present.
        flags = (FETCH_FLAG_SG_PRESENT
                 | FETCH_FLAG_OBJECT_ID_PRESENT
                 | FETCH_FLAG_GROUP_ID_PRESENT
                 | FETCH_FLAG_PRIORITY_PRESENT)
        if self.extensions:
            flags |= FETCH_FLAG_EXTENSIONS_PRESENT

        buf.push_uint_var(flags)
        buf.push_uint_var(self.group_id)
        buf.push_uint_var(self.subgroup_id)
        buf.push_uint_var(self.object_id)
        buf.push_uint8(self.publisher_priority)
        if flags & FETCH_FLAG_EXTENSIONS_PRESENT:
            # d16 fetch object extensions: with explicit length prefix
            MOQTMessage._extensions_encode(buf, self.extensions)

        # Payload length and payload
        if self.status == ObjectStatus.NORMAL and len(self.payload) > 0:
            buf.push_uint_var(len(self.payload))
            buf.push_bytes(self.payload)
        else:
            buf.push_uint_var(0)
            buf.push_uint_var(self.status)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer,
                    prior: Optional['FetchObject'] = None) -> 'FetchObject':
        from ..context import is_draft16_or_later
        if is_draft16_or_later():
            return cls._deserialize_d16(buf, prior)
        return cls._deserialize_d14(buf)

    @classmethod
    def _deserialize_d14(cls, buf: Buffer) -> 'FetchObject':
        group_id = buf.pull_uint_var()
        subgroup_id = buf.pull_uint_var()
        object_id = buf.pull_uint_var()
        publisher_priority = buf.pull_uint8()
        extensions = MOQTMessage._extensions_decode(buf)
        payload_len = buf.pull_uint_var()
        if payload_len == 0:
            status = ObjectStatus(buf.pull_uint_var())
            payload = b''
        else:
            status = ObjectStatus.NORMAL
            payload = buf.pull_bytes(payload_len)
        return cls(
            group_id=group_id,
            subgroup_id=subgroup_id,
            object_id=object_id,
            publisher_priority=publisher_priority,
            extensions=extensions,
            status=status,
            payload=payload,
        )

    @classmethod
    def _deserialize_d16(cls, buf: Buffer,
                         prior: Optional['FetchObject']) -> 'FetchObject':
        flags = buf.pull_uint_var()

        # End-of-range markers
        if flags in (FETCH_FLAGS_END_NON_EXISTENT, FETCH_FLAGS_END_UNKNOWN):
            group_id = buf.pull_uint_var()
            object_id = buf.pull_uint_var()
            return cls(
                group_id=group_id,
                object_id=object_id,
                end_of_range=flags,
                payload=b'',
            )

        if flags >= 0x80:
            raise ValueError(
                f"FetchObject: invalid serialization flags 0x{flags:x}")

        if prior is None and (
                flags & FETCH_FLAG_OBJECT_ID_PRESENT == 0
                or flags & FETCH_FLAG_GROUP_ID_PRESENT == 0):
            raise ValueError(
                "FetchObject: first object on stream must have "
                "Group ID and Object ID present (flags 0x08 | 0x04)")

        # Group ID
        if flags & FETCH_FLAG_GROUP_ID_PRESENT:
            group_id = buf.pull_uint_var()
        else:
            group_id = prior.group_id

        # Subgroup ID — derived from low 2 bits unless 0x40 is set
        sg_mode = flags & FETCH_FLAG_SUBGROUP_MASK
        if flags & FETCH_FLAG_DATAGRAM:
            subgroup_id = 0  # ignored for datagram-pref objects
        elif sg_mode == FETCH_FLAG_SG_ZERO:
            subgroup_id = 0
        elif sg_mode == FETCH_FLAG_SG_PRIOR:
            subgroup_id = prior.subgroup_id if prior else 0
        elif sg_mode == FETCH_FLAG_SG_PRIOR_PLUS:
            subgroup_id = (prior.subgroup_id + 1) if prior else 1
        else:  # FETCH_FLAG_SG_PRESENT
            subgroup_id = buf.pull_uint_var()

        # Object ID
        if flags & FETCH_FLAG_OBJECT_ID_PRESENT:
            object_id = buf.pull_uint_var()
        else:
            object_id = prior.object_id + 1

        # Priority
        if flags & FETCH_FLAG_PRIORITY_PRESENT:
            publisher_priority = buf.pull_uint8()
        else:
            publisher_priority = (prior.publisher_priority
                                  if prior else MOQT_DEFAULT_PRIORITY)

        # Extensions
        if flags & FETCH_FLAG_EXTENSIONS_PRESENT:
            extensions = MOQTMessage._extensions_decode(buf)
        else:
            extensions = None

        # Payload length and payload (with optional Status)
        payload_len = buf.pull_uint_var()
        if payload_len == 0:
            status = ObjectStatus(buf.pull_uint_var())
            payload = b''
        else:
            status = ObjectStatus.NORMAL
            payload = buf.pull_bytes(payload_len)

        return cls(
            group_id=group_id,
            subgroup_id=subgroup_id,
            object_id=object_id,
            publisher_priority=publisher_priority,
            extensions=extensions,
            status=status,
            payload=payload,
        )


@dataclass
class ObjectDatagram(MOQTMessage):
    """Draft-14 object datagram (types 0x00-0x07).

    Type byte encodes flags from base 0x00:
      Bit 0 (0x01): Extensions present
      Bit 1 (0x02): End of group
      Bit 2 (0x04): No object ID (object_id = 0 when absent)

    Wire: Type (i), Track Alias (i), Group ID (i), [Object ID (i)],
          Publisher Priority (8), [Ext Headers ...], Object Payload (..)
    Payload is rest-of-datagram (no length field).
    """
    track_alias: int
    group_id: int
    object_id: int = 0
    publisher_priority: int = MOQT_DEFAULT_PRIORITY
    extensions: Optional[Dict[int, bytes]] = None
    payload: bytes = b''
    end_of_group: bool = False

    def __post_init__(self):
        self.type = OBJECT_DATAGRAM_BASE

    def serialize(self) -> Buffer:
        has_extensions = self.extensions is not None and len(self.extensions) > 0
        no_object_id = (self.object_id == 0)

        type_val = OBJECT_DATAGRAM_BASE
        if has_extensions:
            type_val |= 0x01
        if self.end_of_group:
            type_val |= 0x02
        if no_object_id:
            type_val |= 0x04

        payload_len = 0 if self.payload is None else len(self.payload)
        buf = Buffer(capacity=BUF_SIZE + payload_len)
        buf.push_uint_var(type_val)
        buf.push_uint_var(self.track_alias)
        buf.push_uint_var(self.group_id)
        if not no_object_id:
            buf.push_uint_var(self.object_id)
        buf.push_uint8(self.publisher_priority)
        if has_extensions:
            MOQTMessage._extensions_encode(buf, self.extensions)
        if payload_len > 0:
            buf.push_bytes(self.payload)
        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, buf_len: int, type_val: int = 0x00) -> 'ObjectDatagram':
        """Deserialize ObjectDatagram, given the already-read type byte."""
        extensions_present = bool(type_val & 0x01)
        end_of_group = bool(type_val & 0x02)
        no_object_id = bool(type_val & 0x04)

        track_alias = buf.pull_uint_var()
        group_id = buf.pull_uint_var()
        object_id = 0 if no_object_id else buf.pull_uint_var()
        publisher_priority = buf.pull_uint8()

        extensions = None
        if extensions_present:
            extensions = MOQTMessage._extensions_decode(buf)

        # Payload is rest of datagram — no length field
        payload = buf.pull_bytes(buf_len - buf.tell())

        return cls(
            track_alias=track_alias,
            group_id=group_id,
            object_id=object_id,
            publisher_priority=publisher_priority,
            extensions=extensions,
            payload=payload,
            end_of_group=end_of_group,
        )

@dataclass
class ObjectDatagramStatus(MOQTMessage):
    """Draft-14 object datagram status (types 0x20-0x21).

    Type byte encodes flags from base 0x20:
      Bit 0 (0x01): Extensions present

    Wire: Type (i), Track Alias (i), Group ID (i), Object ID (i),
          Publisher Priority (8), [Ext Headers ...], Object Status (i)
    Object ID always present. No payload.
    """
    track_alias: int
    group_id: int
    object_id: int
    publisher_priority: int = MOQT_DEFAULT_PRIORITY
    extensions: Optional[Dict[int, bytes]] = None
    status: ObjectStatus = ObjectStatus.NORMAL

    def __post_init__(self):
        self.type = OBJECT_DATAGRAM_STATUS_BASE

    def serialize(self) -> Buffer:
        has_extensions = self.extensions is not None and len(self.extensions) > 0
        type_val = OBJECT_DATAGRAM_STATUS_BASE
        if has_extensions:
            type_val |= 0x01

        buf = Buffer(capacity=BUF_SIZE)
        buf.push_uint_var(type_val)
        buf.push_uint_var(self.track_alias)
        buf.push_uint_var(self.group_id)
        buf.push_uint_var(self.object_id)
        buf.push_uint8(self.publisher_priority)
        if has_extensions:
            MOQTMessage._extensions_encode(buf, self.extensions)
        buf.push_uint_var(self.status)

        return buf

    @classmethod
    def deserialize(cls, buf: Buffer, type_val: int = 0x20) -> 'ObjectDatagramStatus':
        """Deserialize ObjectDatagramStatus, given the already-read type byte."""
        extensions_present = bool(type_val & 0x01)

        track_alias = buf.pull_uint_var()
        group_id = buf.pull_uint_var()
        object_id = buf.pull_uint_var()
        publisher_priority = buf.pull_uint8()

        extensions = None
        if extensions_present:
            extensions = MOQTMessage._extensions_decode(buf)

        status = ObjectStatus(buf.pull_uint_var())
        return cls(
            track_alias=track_alias,
            group_id=group_id,
            object_id=object_id,
            publisher_priority=publisher_priority,
            extensions=extensions,
            status=status
        )
