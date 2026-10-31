"""Seeded fuzz of every receive codec: control message bodies at each
draft, subgroup and fetch streams, datagrams. Random and mutated-valid
input may raise only the library's parse errors, which the session maps
to PROTOCOL_VIOLATION, a stream reset or a dropped datagram.

AIOMOQT_FUZZ_ITERATIONS scales the run (per message type and draft);
AIOMOQT_FUZZ_SEED replays one."""
import collections
import dataclasses
import enum
import os
import random

import pytest

from aiomoqt.context import profile_for
from aiomoqt.messages.base import MOQTUnderflow
from aiomoqt.messages.data import (
    FetchHeader, FetchObject, MOQTObjectTooLarge, ObjectDatagram,
    ObjectDatagramStatus, ObjectHeader, SubgroupHeader)
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.types import DataStreamType, MOQTException, ObjectStatus
from aiomoqt.utils.buffer import Buffer, BufferReadError
from aiopquic.streamchain import StreamChain

DRAFTS = (14, 16, 18, 20, 22)
ITERATIONS = int(os.environ.get('AIOMOQT_FUZZ_ITERATIONS', '1000'))
SEED = int(os.environ.get('AIOMOQT_FUZZ_SEED', '0x5eed'), 0)
MAX_OBJECT = 1 << 20

# What a receive codec may raise: MOQTException (session close),
# MOQTObjectTooLarge (stream reset), or a short read.
PARSE_ERRORS = (MOQTException, MOQTObjectTooLarge, MOQTUnderflow,
                BufferReadError)

_BIG = (63, 64, 255, 256, 16383, 16384, (1 << 30) - 1, 1 << 30,
        (1 << 32) - 1, (1 << 62) - 1)
_BIG_VI64 = _BIG + ((1 << 63) - 1, 1 << 63, (1 << 64) - 1)


def _vint(prof, v):
    buf = Buffer(capacity=16, vi64=prof.vi64)
    buf.push_vint(v)
    return bytes(buf.data_slice(0, buf.tell()))


def _value(rng, prof):
    r = rng.random()
    if r < 0.5:
        return rng.randrange(8)
    if r < 0.8:
        return rng.choice(_BIG_VI64 if prof.vi64 else _BIG)
    return rng.randrange(1 << (64 if prof.vi64 else 62))


def random_body(rng, prof):
    """Raw bytes, or a run of plausible fields: varints of edge values,
    length-prefixed strings whose length may lie, single bytes."""
    if rng.random() < 0.3:
        n = rng.choice((0, 1, 2, 3, rng.randrange(64), rng.randrange(512)))
        return rng.randbytes(n)
    out = bytearray()
    for _ in range(rng.randrange(1, 16)):
        r = rng.random()
        if r < 0.5:
            out += _vint(prof, _value(rng, prof))
        elif r < 0.8:
            s = rng.randbytes(rng.randrange(8))
            n = len(s) if rng.random() < 0.6 else _value(rng, prof)
            out += _vint(prof, n) + s
        else:
            out += bytes([rng.choice((0, 1, 0x7F, 0x80, 0xC0, 0xFF))])
    return bytes(out)


def mutate(rng, raw, prof):
    data = bytearray(raw)
    for _ in range(rng.randrange(1, 4)):
        op = rng.randrange(6)
        pos = rng.randrange(len(data) + 1)
        if op == 0 and data:                     # flip a bit
            i = rng.randrange(len(data))
            data[i] ^= 1 << rng.randrange(8)
        elif op == 1:                            # truncate
            del data[pos:]
        elif op == 2:                            # extend
            data += rng.randbytes(rng.randrange(1, 16))
        elif op == 3:                            # insert a big varint
            data[pos:pos] = _vint(prof, _value(rng, prof))
        elif op == 4 and data:                   # overwrite with a varint
            v = _vint(prof, _value(rng, prof))
            data[pos:pos + len(v)] = v
        elif data:                               # corrupt a varint prefix
            i = rng.randrange(len(data))
            data[i] = rng.choice((0x3F, 0x40, 0x80, 0xBF, 0xC0, 0xFE, 0xFF))
    return bytes(data)


# -- valid seeds -------------------------------------------------------

def _guess(f):
    """A plausible value for a field left None by default; None keeps the
    default."""
    if f.default_factory is not dataclasses.MISSING or \
            f.default not in (None, dataclasses.MISSING):
        return None
    if isinstance(f.type, type) and issubclass(f.type, enum.Enum):
        return f.type(1)
    t = str(f.type)
    if 'Filter' in t:
        return None
    if 'Dict' in t:
        return {}
    if 'Tuple' in t or 'tuple' in t:
        return (b"ns", b"x")
    if 'bytes' in t:
        return b"name"
    if 'str' in t:
        return "why"
    return 1


def _seed_kwargs():
    from aiomoqt.tests import test_messages as tm
    cases = collections.defaultdict(list)
    for name in dir(tm):
        if name.endswith('TEST_CASES'):
            for case in getattr(tm, name):
                cases[case[0]].append(case[1])
    return cases


_CASES = None


def control_seeds(cls, prof):
    """Bodies (type and length stripped) of encodings of cls that parse
    back at this draft."""
    global _CASES
    if _CASES is None:
        _CASES = _seed_kwargs()
    kwargs = list(_CASES.get(cls, ()))
    guessed = {}
    for f in dataclasses.fields(cls):
        if f.init and f.name != 'type':
            v = _guess(f)
            if v is not None:
                guessed[f.name] = v
    kwargs.append(guessed)
    seeds = []
    for kw in kwargs:
        try:
            raw = cls(**kw).serialize(prof=prof)
        except Exception:
            continue
        raw = bytes(raw.data_slice(0, raw.tell())
                    if hasattr(raw, 'data_slice') else raw)
        buf = Buffer(data=raw, vi64=prof.vi64)
        buf.pull_vint()
        n = buf.pull_uint16()
        body = raw[buf.tell():buf.tell() + n]
        try:
            parse_control(cls, body, prof)
        except MOQTException:
            continue
        seeds.append(body)
    return seeds


# -- receive paths, as protocol.py drives them --------------------------

def parse_control(cls, body, prof):
    """Decode as the control ingest does, then render the message for the
    log line every received message gets."""
    buf = Buffer(data=body, vi64=prof.vi64)
    cls.deserialize(buf, prof=prof, buf_end=len(body)).describe(prof)


def parse_stream(raw, prof, fused=True):
    """A data stream through the ingest parsers: header, then objects
    until the bytes run out. fused: on a StreamChain, as received, which
    takes the Cython object parser; else on a Buffer (the Python one)."""
    if fused:
        chain = StreamChain()
        chain.extend(raw)
        chain.vi64 = prof.vi64
    else:
        chain = Buffer(data=raw, vi64=prof.vi64)
    stream_type = chain.pull_vint()
    mask = prof.subgroup_type_mask
    if ((stream_type & ~mask) == 0x10
            and ((stream_type >> 1) & 0x03) != 3):
        hdr = SubgroupHeader.deserialize(chain, type_val=stream_type,
                                         prof=prof)
        obj = ObjectHeader(object_id=0)
        while chain.tell() < chain.capacity:
            obj.deserialize_into(chain, chain.capacity,
                                 hdr.extensions_present, hdr._last_object_id,
                                 hdr._vi64, hdr._kvp_delta, MAX_OBJECT)
            hdr._last_object_id = obj.object_id
    elif stream_type == DataStreamType.FETCH_HEADER:
        FetchHeader.deserialize(chain)
        prior = None
        while chain.tell() < chain.capacity:
            prior = FetchObject.deserialize(chain, prior=prior, prof=prof,
                                            max_payload=MAX_OBJECT)


def parse_datagram(raw, prof):
    buf = Buffer(data=raw, vi64=prof.vi64)
    dtype = buf.pull_vint()
    if prof.merged_datagram_layout or dtype <= 0x07:
        ObjectDatagram.deserialize(buf, buf.capacity, type_val=dtype,
                                   prof=prof)
    else:
        ObjectDatagramStatus.deserialize(buf, type_val=dtype, prof=prof)


def stream_seeds(prof):
    seeds = []
    for mode in (0, 1, 2):
        hdr = SubgroupHeader(track_alias=3, group_id=7, subgroup_id=2,
                             subgroup_id_mode=mode, extensions_present=True,
                             prof=prof)
        raw = bytearray(hdr.serialize().data)
        for _ in range(3):
            raw += hdr.next_object_bytes(b"payload",
                                         extensions={2: 9, 5: b"ab"})
        raw += bytes(hdr.end_group().data)
        seeds.append(bytes(raw))
    fh = FetchHeader(request_id=4)
    raw = bytearray(fh.serialize(prof=prof).data)
    prior = None
    for oid in range(3):
        obj = FetchObject(group_id=1, subgroup_id=0, object_id=oid,
                          publisher_priority=5, extensions={2: 1},
                          payload=b"xyz")
        raw += bytes(obj.serialize(prof=prof, prior=prior).data)
        prior = obj
    seeds.append(bytes(raw))
    return seeds


def datagram_seeds(prof):
    seeds = [bytes(ObjectDatagram(track_alias=1, group_id=2, object_id=3,
                                  extensions={2: 4, 7: b"q"},
                                  payload=b"data").serialize(prof=prof).data)]
    if prof.merged_datagram_layout:
        seeds.append(bytes(ObjectDatagram(
            track_alias=1, group_id=2, object_id=3,
            status=ObjectStatus.END_OF_GROUP).serialize(prof=prof).data))
    else:
        seeds.append(bytes(ObjectDatagramStatus(
            track_alias=1, group_id=2, object_id=3, extensions={2: 4},
            status=ObjectStatus.END_OF_GROUP).serialize(prof=prof).data))
    return seeds


# -- the fuzz loops ----------------------------------------------------

def _run(label, parse, seeds, prof, rng):
    for i in range(ITERATIONS):
        if seeds and i % 2:
            raw = mutate(rng, rng.choice(seeds), prof)
        else:
            raw = random_body(rng, prof)
        try:
            parse(raw, prof)
        except PARSE_ERRORS:
            pass
        except Exception as e:
            pytest.fail(
                f"{label} draft-{prof.draft}: {type(e).__name__}: {e} "
                f"input={raw.hex()} (AIOMOQT_FUZZ_SEED={SEED:#x})")


def _rng(*key):
    return random.Random(f"{SEED}:{':'.join(map(str, key))}")


@pytest.mark.parametrize("draft", DRAFTS)
def test_control_messages(draft):
    prof = profile_for(draft)
    registry = _MOQTSessionMixin.CONTROL_REGISTRY[draft]
    for code, (cls, _) in sorted(registry.items()):
        seeds = control_seeds(cls, prof)
        _run(f"{cls.__name__}(0x{int(code):x})",
             lambda raw, p, c=cls: parse_control(c, raw, p),
             seeds, prof, _rng('control', draft, int(code)))


@pytest.mark.parametrize("draft", DRAFTS)
def test_control_seeds_parse(draft):
    """The mutation seeds are valid: every type has one that round-trips."""
    prof = profile_for(draft)
    for code, (cls, _) in _MOQTSessionMixin.CONTROL_REGISTRY[draft].items():
        seeds = control_seeds(cls, prof)
        assert seeds, f"no seed for {cls.__name__}"
        for body in seeds:
            parse_control(cls, body, prof)


@pytest.mark.parametrize("fused", [True, False], ids=["cython", "python"])
@pytest.mark.parametrize("draft", DRAFTS)
def test_data_streams(draft, fused):
    prof = profile_for(draft)
    seeds = stream_seeds(prof)
    for raw in seeds:
        parse_stream(raw, prof, fused)
    _run(f"data stream ({'cython' if fused else 'python'})",
         lambda raw, p: parse_stream(raw, p, fused), seeds, prof,
         _rng('stream', draft, fused))


@pytest.mark.parametrize("draft", DRAFTS)
def test_datagrams(draft):
    prof = profile_for(draft)
    seeds = datagram_seeds(prof)
    for raw in seeds:
        parse_datagram(raw, prof)
    _run("datagram", parse_datagram, seeds, prof, _rng('dgram', draft))
