"""LOCMAF codec: the Eyevinn golden corpus (vectors/locmaf, MIT) decoded,
re-serialized and re-encoded byte for byte, the receiver's must-reject
rules, round trips of our own CMAF chunks, and publisher numbering.

A missing or altered corpus fails these tests; it never skips them."""
import asyncio
import hashlib
import json
import os
import struct
from types import SimpleNamespace

import pytest

from aiomoqt.delivery import StreamMapping
from aiomoqt.media import LocmafTrackPublisher, LocmafTrackSubscriber
from aiomoqt.media.cmaf import CmafChunker, chunk_decode_time
from aiomoqt.media.locmaf import (
    LOCMAF_VERSION, GroupState, LocmafDecoder, LocmafEncoder, LocmafMalformed,
    LocmafSourceError, TrackContext, append_framed, parse_chunk, parse_object,
    reconstruct, serialize_object, split_framed,
)
from aiomoqt.utils.buffer import Buffer

VECTORS = os.path.join(os.path.dirname(__file__), 'vectors', 'locmaf')
CASES = (
    'uniform', 'varying-sizes', 'single-sample', 'negative-ctos',
    'first-sample-flags', 'per-sample-flags', 'list-grow-shrink',
    'bmdt-reanchor', 'cenc-subsamples', 'cbcs-omit', 'genboxes',
    'strict-cmf2-pair', 'rawboxes-file', 'event-only',
)


def _read(*parts) -> bytes:
    with open(os.path.join(VECTORS, *parts), 'rb') as f:
        return f.read()


def _json(*parts):
    return json.loads(_read(*parts))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _case(name):
    """(init, [(group, object, payload, canonical, effective json)])."""
    objects = []
    for f in sorted(os.listdir(os.path.join(VECTORS, name, 'objects'))):
        stem = f[:-len('.locmafobj')]
        g, o = int(stem[1:4]), int(stem[6:9])
        objects.append((g, o, _read(name, 'objects', f),
                        _read(name, 'canonical', stem + '.cmfc'),
                        _json(name, 'effective', stem + '.json')))
    return _read(name, 'init.mp4'), objects


def _projection(eff, raw):
    """The reference JSON shape of a decoded Object (Go omitempty)."""
    if eff is None:
        return {'rawBoxes': {'length': len(raw), 'sha256': _sha(raw)}}
    out = {'sampleCount': eff.sample_count, 'bmdt': eff.bmdt,
           'sampleDescriptionIndex': eff.sample_description_index,
           'durations': eff.durations, 'sizes': eff.sizes,
           'flags': eff.flags, 'ctos': eff.ctos}
    if eff.iv_size:
        out['perSampleIVSize'] = eff.iv_size
    if eff.ivs:
        out['ivs'] = eff.ivs.hex()
    if eff.subsample_counts is not None:
        out['hasSubsamples'] = True
        for key, v in (('subsampleCounts', eff.subsample_counts),
                       ('clearBytes', eff.clear_bytes),
                       ('protectedBytes', eff.protected_bytes)):
            if v:
                out[key] = v
    if eff.gen_boxes:
        out['genBoxes'] = [{'name': n.decode('latin-1'), 'payload': p.hex()}
                           for n, p in eff.gen_boxes]
    out['mdatLength'] = len(eff.mdat)
    out['mdatSha256'] = _sha(eff.mdat)
    return out


# -- corpus integrity --------------------------------------------------

def test_corpus_is_complete_pinned_and_licensed():
    top = _json('manifest.json')
    assert top['format'] == 'locmaf-golden-vectors/1'
    src = top['source']
    assert len(src['commit']) == 40 and int(src['commit'], 16) >= 0
    assert src['license'] == 'MIT'
    licence = _read(src['licenseFile']).decode()
    assert 'MIT License' in licence and 'Eyevinn Technology AB' in licence
    assert top['provenance']['locmafVersion'] == LOCMAF_VERSION == '0.3'
    assert tuple(c['name'] for c in top['cases']) == CASES
    assert sum(c['objects'] for c in top['cases']) == 38
    assert set(os.listdir(VECTORS)) == {'manifest.json', 'LICENSE', *CASES}
    for listed in top['cases']:
        name = listed['name']
        man = _json(name, 'manifest.json')
        assert man['name'] == name and man['locmafVersion'] == '0.3'
        for rel, digest in man['files'].items():
            assert _sha(_read(name, rel)) == digest, f"{name}/{rel}"
        on_disk = set()
        for root, _, files in os.walk(os.path.join(VECTORS, name)):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f),
                                      os.path.join(VECTORS, name))
                on_disk.add(rel.replace(os.sep, '/'))
        assert on_disk == set(man['files']) | {'manifest.json'}, name
        objects = [f for f in man['files'] if f.startswith('objects/')]
        assert len(objects) == man['objects'] == listed['objects'], name
        assert ('file.locmaf' in man['files']) == listed['framedFile'], name


# -- corpus round trips ------------------------------------------------

@pytest.mark.parametrize('name', CASES)
def test_corpus_objects_rebuild_the_canonical_chunks(name):
    init, objects = _case(name)
    ctx = TrackContext.from_init(init)
    states = {}
    for g, o, payload, canonical, effective in objects:
        state = states.setdefault(g, GroupState())
        obj = parse_object(payload)
        chunk, eff = reconstruct(obj, state, ctx, o)
        assert chunk == canonical, f"{name} g{g} o{o}"
        assert _projection(eff, chunk) == effective, f"{name} g{g} o{o}"
        assert serialize_object(obj) == payload, f"{name} g{g} o{o}"


@pytest.mark.parametrize('name', CASES)
def test_corpus_canonical_chunks_encode_to_the_objects(name):
    init, objects = _case(name)
    encoder = LocmafEncoder(TrackContext.from_init(init))
    states = {}
    for g, o, payload, canonical, _ in objects:
        state = states.setdefault(g, GroupState())
        assert encoder.encode(canonical, state, o) == payload, f"{name} g{g} o{o}"
    assert encoder.raw_fallbacks == (1 if name == 'rawboxes-file' else 0)


@pytest.mark.parametrize('name', CASES)
def test_corpus_track_decoder_never_rejects(name):
    init, objects = _case(name)
    decoder = LocmafDecoder(init)
    for g, o, payload, canonical, _ in objects:
        assert decoder.push(g, o, payload) == canonical, f"{name} g{g} o{o}"


def test_corpus_framed_file_splits_and_rejoins():
    framed = _read('rawboxes-file', 'file.locmaf')
    _, objects = _case('rawboxes-file')
    payloads = [p for _, _, p, _, _ in objects]
    assert split_framed(framed) == payloads
    out = bytearray()
    for p in payloads:
        append_framed(out, p)
    assert bytes(out) == framed
    with pytest.raises(LocmafMalformed):
        split_framed(framed[:-1])


# -- receiver rules ----------------------------------------------------

def _uniform():
    init, objects = _case('uniform')
    return init, [p for _, _, p, _, _ in objects], [c for *_, c, _ in objects]


def test_delta_first_in_a_group_is_rejected():
    init, payloads, canonical = _uniform()
    decoder = LocmafDecoder(init)
    with pytest.raises(LocmafMalformed, match='reference'):
        decoder.push(0, 1, payloads[1])
    assert decoder.push(1, 0, payloads[0]) == canonical[0]


def test_delta_after_an_object_id_gap_is_rejected_until_a_full_header():
    init, payloads, canonical = _uniform()
    decoder = LocmafDecoder(init)
    decoder.push(0, 0, payloads[0])
    with pytest.raises(LocmafMalformed, match='gap'):
        decoder.push(0, 2, payloads[2])
    with pytest.raises(LocmafMalformed):
        decoder.push(0, 3, payloads[2])
    assert decoder.push(0, 4, payloads[0]) == canonical[0]


def _vi(*values) -> bytes:
    buf = Buffer(capacity=16)
    for v in values:
        buf.push_uint_vi64(v)
    return buf.data


def _object(element, props, mdat=b'') -> bytes:
    return _vi(element, len(props)) + props + mdat


_FULL = _vi(6, 200, 10, 0, 14, 4)         # uniform o000's header fields
_MDAT = b'\x01' * 800
_BOX = struct.pack('>I4s', 8, b'free')

MALFORMED = {
    'unknown element': lambda: _vi(5) + _FULL,
    'truncated integer': lambda: _object(2, _vi(6, 200, 14) + b'\xc0\x01'),
    'property overrun': lambda: _object(2, _FULL + _vi(3, 10, 1)),
    'duplicate field': lambda: _object(2, _FULL + _vi(14, 4), _MDAT),
    'field 27 in a full header': lambda: _object(2, _FULL + _vi(27, 1, 4), _MDAT),
    'field 10 in a delta header': lambda: _object(3, _vi(10, 3200), _MDAT),
    'CENC field on a clear track': lambda: _object(2, _FULL + _vi(16, 8), _MDAT),
    'sizes not matching the mdat': lambda: _object(2, _FULL, _MDAT[:-1]),
    'rawBoxes after a genBox': lambda: _vi(1, 8) + b'prft\0\0\0\0' + _vi(4) + _BOX,
    'size escape in rawBoxes': lambda: _vi(4) + struct.pack('>I4sQ', 1, b'free', 16),
}


@pytest.mark.parametrize('case', MALFORMED)
def test_malformed_object_is_rejected_and_clears_the_reference(case):
    init, payloads, canonical = _uniform()
    decoder = LocmafDecoder(init)
    assert decoder.push(0, 0, payloads[0]) == canonical[0]
    with pytest.raises(LocmafMalformed):
        decoder.push(0, 1, MALFORMED[case]())
    # Without the reset, this valid next delta would decode.
    with pytest.raises(LocmafMalformed, match='reference'):
        decoder.push(0, 1, payloads[1])


# -- our own chunks ----------------------------------------------------

_AVC1 = struct.pack('>I4s', 86, b'avc1') + b'\0' * 78


def _chunker():
    return CmafChunker(SimpleNamespace(timescale=90000, width=64, height=64,
                                       sample_entry_bytes=_AVC1))


def _samples(chunker, keys, jump_at=None):
    out = []
    for i, key in enumerate(keys):
        dts = 9_000_000 if i == jump_at else None
        out.append(chunker.chunk(bytes([i + 1]) * (300 + 7 * i), 3000, key,
                                 decode_time=dts))
    return out


def test_own_chunks_round_trip_with_deltas_and_a_decode_time_break():
    ck = _chunker()
    init = ck.init_segment()
    ctx = TrackContext.from_init(init)
    chunks = _samples(ck, [True, False, False, False], jump_at=2)
    encoder, decoder, state = LocmafEncoder(ctx), LocmafDecoder(init), GroupState()
    objects = [encoder.encode(c, state, i) for i, c in enumerate(chunks)]
    # full, delta, full (decode-time jump), delta
    assert [o[0] for o in objects] == [2, 3, 2, 3]
    assert len(objects[1]) < len(chunks[1]) - 80
    for i, (chunk, obj) in enumerate(zip(chunks, objects)):
        rebuilt = decoder.push(0, i, obj)
        src, out = parse_chunk(chunk, ctx), parse_chunk(rebuilt, ctx)
        assert (out.bmdt, out.durations, out.sizes, out.flags, out.mdat) == (
            src.bmdt, src.durations, src.sizes, src.flags, src.mdat)
        assert chunk_decode_time(rebuilt) == chunk_decode_time(chunk)
    assert encoder.raw_fallbacks == 0


def test_interleaved_groups_decode():
    ck = _chunker()
    init = ck.init_segment()
    encoder = LocmafEncoder(TrackContext.from_init(init))
    chunks = _samples(ck, [True, False, True, False])
    groups = {0: GroupState(), 1: GroupState()}
    sent = [(g, o, encoder.encode(chunks[2 * g + o], groups[g], o))
            for g in (0, 1) for o in (0, 1)]
    in_order = LocmafDecoder(init)
    expect = {(g, o): in_order.push(g, o, p) for g, o, p in sent}
    interleaved = LocmafDecoder(init)
    for g, o, p in (sent[0], sent[2], sent[1], sent[3]):
        assert interleaved.push(g, o, p) == expect[(g, o)]


def test_chunk_outside_the_model_goes_out_as_raw_boxes_and_resets():
    ck = _chunker()
    init = ck.init_segment()
    encoder = LocmafEncoder(TrackContext.from_init(init))
    chunks = _samples(ck, [True, False, False])
    odd = chunks[1] + chunks[1]                    # two moofs
    state = GroupState()
    objects = [encoder.encode(chunks[0], state, 0),
               encoder.encode(odd, state, 1),
               encoder.encode(chunks[2], state, 2)]
    assert [o[0] for o in objects] == [2, 4, 2]
    assert encoder.raw_fallbacks == 1
    decoder = LocmafDecoder(init)
    assert [decoder.push(0, i, o) for i, o in enumerate(objects)][1] == odd
    with pytest.raises(LocmafSourceError):
        encoder.encode(b'\x00\x00\x00\x01', state, 3)


def test_init_with_two_traks_is_refused():
    init = _chunker().init_segment()
    moov = init.index(b'moov') - 4
    trak = init.index(b'trak') - 4
    size = struct.unpack_from('>I', init, trak)[0]
    doubled = (init[:moov]
               + struct.pack('>I', struct.unpack_from('>I', init, moov)[0] + size)
               + init[moov + 4:trak + size] + init[trak:trak + size]
               + init[trak + size:])
    with pytest.raises(LocmafSourceError, match='one trak'):
        TrackContext.from_init(doubled)


# -- tracks ------------------------------------------------------------

class _Out:
    def __init__(self):
        self.written = []
        self.resets = 0

    async def write(self, group_id, object_id, payload, group_start=False,
                    extensions=None):
        self.written.append((group_id, object_id, payload))

    def reset_group(self):
        self.resets += 1


async def _publish(track, frames, pause_at=()):
    """Feed (chunk, key) frames through produce(); at an index in
    pause_at the peer's Forward State drops to 0 for that frame."""
    sub = track._subs[0]
    sub.forward, sub.generating = 1, True
    out = _Out()
    task = asyncio.ensure_future(track.produce(out))
    for i, (chunk, key) in enumerate(frames):
        sub.forward = 0 if i in pause_at else 1
        await track.send_frame(chunk, key_frame=key, timestamp=i)
        for _ in range(5):
            await asyncio.sleep(0)
    await track.finish()
    await asyncio.wait_for(task, 1.0)
    return out


async def test_publisher_numbers_groups_full_then_delta():
    ck = _chunker()
    keys = [True, False, False, True, False]
    track = LocmafTrackPublisher(SimpleNamespace(), 'ns', 'video',
                                 init=ck.init_segment())
    out = await _publish(track, list(zip(_samples(ck, keys), keys)))
    assert [(g, o, p[0]) for g, o, p in out.written] == [
        (0, 0, 2), (0, 1, 3), (0, 2, 3), (1, 0, 2), (1, 1, 3)]
    assert track.raw_fallbacks == 0
    assert track._total_bytes == sum(len(p) for *_, p in out.written)


async def test_publisher_resumes_after_a_pause_with_a_full_header():
    ck = _chunker()
    keys = [True, False, False, False, True, False]
    track = LocmafTrackPublisher(SimpleNamespace(), 'ns', 'video',
                                 init=ck.init_segment())
    out = await _publish(track, list(zip(_samples(ck, keys), keys)),
                         pause_at={2})
    # frame 2 dropped at Forward State 0, frame 3 waits for a key frame
    assert [(g, o, p[0]) for g, o, p in out.written] == [
        (0, 0, 2), (0, 1, 3), (1, 0, 2), (1, 1, 3)]
    assert out.resets >= 1


@pytest.mark.parametrize('mapping', [StreamMapping.DATAGRAM,
                                     StreamMapping.PER_OBJECT])
def test_publisher_needs_per_group_streams(mapping):
    with pytest.raises(ValueError):
        LocmafTrackPublisher(SimpleNamespace(), 'ns', 'video',
                             init=_chunker().init_segment(), mapping=mapping)


def test_subscriber_rebuilds_chunks_and_counts_rejections():
    ck = _chunker()
    init = ck.init_segment()
    encoder = LocmafEncoder(TrackContext.from_init(init))
    state = GroupState()
    chunks = _samples(ck, [True, False, False])
    objects = [encoder.encode(c, state, i) for i, c in enumerate(chunks)]
    frames, arrivals = [], []
    sub = LocmafTrackSubscriber(
        SimpleNamespace(), 'ns', 'video', init=init,
        on_frame=lambda f, g, o: frames.append((g, o, f.payload)),
        on_arrival=lambda m, rx, g, sg, f: arrivals.append(f is None))

    def deliver(g, o, payload):
        msg = SimpleNamespace(group_id=g, object_id=o, payload=payload,
                              status=None, extensions={})
        sub._on_object(msg, len(payload), 0, g, 0)

    deliver(5, 1, objects[1])                        # joined mid-group
    deliver(6, 0, objects[0])
    deliver(6, 1, objects[1])
    assert sub.objects_rejected == 1
    assert arrivals == [True, False, False]
    ref = LocmafDecoder(init)
    assert frames == [(6, 0, ref.push(6, 0, objects[0])),
                      (6, 1, ref.push(6, 1, objects[1]))]
