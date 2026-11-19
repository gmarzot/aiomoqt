"""MoQT draft-18 framing inside moq-fabric data, as moqx writes it.

A NEW_STREAM's inner bytes are one d18 subgroup stream: header type 0x51
(subgroup 0) or 0x55, FIRST_OBJECT set, track alias = the track's
fullname hash, the priority byte present and every object carrying an
extension block, empty or not. A fabric datagram carries one d18 object
datagram. Encoding and decoding use aiomoqt's d18 codecs.
"""
from typing import Any, Dict, List, NamedTuple, Optional

from aiopquic.streamchain import StreamChain

from aiomoqt.context import profile_for
from aiomoqt.messages import (MOQTMalformedKVP, MOQTUnderflow, ObjectStatus,
                              SUBGROUP_ID_EXPLICIT, SUBGROUP_ID_FIRST_OBJ,
                              SUBGROUP_ID_ZERO)
from aiomoqt.messages.data import (MOQTObjectTooLarge, ObjectDatagram,
                                   ObjectHeader, SubgroupHeader)
from aiomoqt.moq_fabric.wire import DATAGRAM_HEADER_LEN, WireError
from aiomoqt.types import MOQTProtocolViolation
from aiomoqt.utils.buffer import Buffer, BufferReadError

P18 = profile_for(18)
# moqx drops larger objects.
MAX_OBJECT = 1 << 20
# A fabric datagram frame (header + inner) stays within aiopquic's
# 1200-byte datagram payload cap; moq-fabric peers accept 1280.
DATAGRAM_MAX_FRAME = 1200
DATAGRAM_MAX_INNER = DATAGRAM_MAX_FRAME - DATAGRAM_HEADER_LEN
DEFAULT_PRIORITY = 128

# Even types carry an int, odd types bytes.
Extensions = Optional[Dict[int, Any]]


class StreamObject(NamedTuple):
    """An object from an inner subgroup stream."""
    group_id: int
    subgroup_id: int
    object_id: int
    status: ObjectStatus
    extensions: Extensions
    payload: bytes
    priority: int


class DatagramObject(NamedTuple):
    """An object from an inner datagram (no subgroup)."""
    group_id: int
    object_id: int
    status: ObjectStatus
    extensions: Extensions
    payload: bytes
    priority: int
    end_of_group: bool


def is_subgroup_type(stream_type: int) -> bool:
    return ((stream_type & ~P18.subgroup_type_mask) == 0x10
            and (stream_type >> 1) & 0x03 != 3)


# -- encode ----------------------------------------------------------

class SubgroupWriter:
    """One subgroup's inner bytes; encoded once for every egress stream,
    since every stream of it starts at the subgroup's first object."""

    def __init__(self, fullname_hash: int, group_id: int, subgroup_id: int,
                 priority: int = DEFAULT_PRIORITY):
        self.group_id = group_id
        self.subgroup_id = subgroup_id
        self._h = SubgroupHeader(
            track_alias=fullname_hash, group_id=group_id,
            subgroup_id=subgroup_id, publisher_priority=priority,
            extensions_present=True,
            subgroup_id_mode=(SUBGROUP_ID_ZERO if subgroup_id == 0
                              else SUBGROUP_ID_EXPLICIT),
            first_object=True, prof=P18)
        self.header = bytes(self._h.serialize().data)
        self.last_object_id: Optional[int] = None

    def _next(self, object_id: int, payload: bytes, extensions: Extensions,
              status: ObjectStatus) -> bytes:
        if self.last_object_id is not None and object_id <= self.last_object_id:
            raise ValueError(f"object {object_id} after {self.last_object_id}"
                             f" in subgroup {self.group_id}/{self.subgroup_id}")
        data = self._h.next_object_bytes(payload, extensions, status,
                                         object_id)
        self.last_object_id = object_id
        return data

    def object(self, object_id: int, payload: bytes,
               extensions: Extensions = None) -> bytes:
        if len(payload) > MAX_OBJECT:
            raise WireError(f"object of {len(payload)} bytes over "
                            f"{MAX_OBJECT}")
        return self._next(object_id, payload, extensions,
                          ObjectStatus.NORMAL)

    def status(self, object_id: int, status: ObjectStatus) -> bytes:
        """A status object (END_OF_GROUP, END_OF_TRACK)."""
        return self._next(object_id, b"", None, status)


def encode_datagram(fullname_hash: int, group_id: int, object_id: int,
                    payload: bytes = b"", extensions: Extensions = None,
                    priority: int = DEFAULT_PRIORITY,
                    end_of_group: bool = False,
                    status: ObjectStatus = ObjectStatus.NORMAL) -> bytes:
    data = bytes(ObjectDatagram(
        track_alias=fullname_hash, group_id=group_id, object_id=object_id,
        publisher_priority=priority, extensions=extensions,
        payload=payload, end_of_group=end_of_group,
        status=status).serialize(prof=P18).data)
    if len(data) > DATAGRAM_MAX_INNER:
        raise WireError(f"datagram of {len(data)} bytes over "
                        f"{DATAGRAM_MAX_INNER}")
    return data


# -- decode ----------------------------------------------------------

class SubgroupReader:
    """Decodes one inner subgroup stream from its chunks.

    Tolerates what moqx sends: an extension block on every object,
    FIRST_OBJECT set on a stream that starts mid-subgroup, statuses 0, 3
    and 4. Raises WireError on a non-subgroup stream type (a FETCH header,
    say), an object over max_object, or a malformed object.
    """

    def __init__(self, max_object: int = MAX_OBJECT):
        self.max_object = max_object
        self.header: Optional[SubgroupHeader] = None
        self._chain = StreamChain(vi64=True)
        self._need = 1

    @property
    def residual(self) -> int:
        """Bytes held that do not yet make a whole object."""
        return self._chain.capacity - self._chain.tell()

    def feed(self, data) -> List[StreamObject]:
        chain = self._chain
        chain.extend(data)
        out: List[StreamObject] = []
        if chain.capacity < self._need:
            return out
        self._need = 1
        while chain.capacity > chain.tell():
            chain.save()
            try:
                if self.header is None:
                    self._read_header(chain)
                else:
                    out.append(self._read_object(chain, self.header))
            except MOQTUnderflow as e:
                chain.rollback()
                self._need = e.needed
                break
            except BufferReadError:
                chain.rollback()
                self._need = chain.capacity + 1
                break
            except WireError:
                raise
            except (MOQTObjectTooLarge, MOQTMalformedKVP,
                    MOQTProtocolViolation, ValueError) as e:
                raise WireError(f"inner object: {e}") from None
            chain.commit()
        return out

    def _read_header(self, chain) -> None:
        stream_type = chain.pull_uint_vi64()
        if not is_subgroup_type(stream_type):
            raise WireError(f"inner stream type 0x{stream_type:x} is not "
                            f"a d18 subgroup")
        self.header = SubgroupHeader.deserialize(chain, stream_type,
                                                 prof=P18)

    def _read_object(self, chain, h: SubgroupHeader) -> StreamObject:
        obj = ObjectHeader(object_id=0)
        obj.deserialize_into(chain, chain.capacity, h.extensions_present,
                             h._last_object_id, True, P18.params_delta_coded,
                             self.max_object)
        h._last_object_id = obj.object_id
        if h.subgroup_id_mode == SUBGROUP_ID_FIRST_OBJ and h.subgroup_id is None:
            h.subgroup_id = obj.object_id
        return StreamObject(h.group_id, h.subgroup_id or 0, obj.object_id,
                            obj.status or ObjectStatus.NORMAL, obj.extensions,
                            bytes(obj.payload), h.publisher_priority)

    def finish(self) -> None:
        """At FIN: WireError if the stream ended inside its header or an
        object."""
        if self.residual:
            raise WireError(f"stream ended with {self.residual} bytes of a "
                            f"partial {'object' if self.header else 'header'}")
        if self.header is None:
            raise WireError("stream ended before its subgroup header")


def decode_datagram(data) -> DatagramObject:
    raw = bytes(data)
    buf = Buffer(data=raw, vi64=True)
    try:
        dtype = buf.pull_uint_vi64()
        if dtype > 0x2F or 0x10 <= dtype < 0x20:
            raise WireError(f"inner datagram type 0x{dtype:x}")
        d = ObjectDatagram.deserialize(buf, len(raw), dtype, prof=P18)
    except (BufferReadError, MOQTMalformedKVP, MOQTProtocolViolation,
            ValueError) as e:
        if isinstance(e, WireError):
            raise
        raise WireError(f"inner datagram: {e}") from None
    return DatagramObject(d.group_id, d.object_id, d.status, d.extensions,
                          bytes(d.payload), d.publisher_priority,
                          d.end_of_group)
