"""pub_media --annexb and --no-catalog: LOC H.264/H.265 as Annex B with
the parameter sets in every key frame (loc-04 §2.1.1, §2.1.4), catalog
codec avc3/hev1, no VIDEO_CONFIG; the default stays length-prefixed with
VIDEO_CONFIG. End to end through the interop relay."""
import asyncio
import struct
from types import SimpleNamespace as NS

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.media import Catalog, LocFrame
from aiomoqt.media.loc import (
    LOC_PROP_VIDEO_CONFIG, LocTrackPublisher, LocTrackSubscriber,
)
from aiomoqt.media.sources import (
    Mp4Reader, as_annexb, avcc_param_sets, hvcc_param_sets, split_annexb,
)
from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_pub_media_frindell import _argv
from aiomoqt.tests.test_pub_media_mtt import (
    _analyze, _encode, _paced_fifo, _pub_args, _with_relay,
)
from aiomoqt.tests.test_pub_media_ts_timing import _catalogs
from aiomoqt.tools import pub_media
from aiomoqt.tools.sub_media import _Writers

_BASE_PORT = 23600
_SC = b'\x00\x00\x00\x01'
_AVCC = (bytes([1, 0x64, 0, 0x1F, 0xFF, 0xE1, 0, 3]) + b'\x67\x64\x00'
         + bytes([1, 0, 2]) + b'\x68\xee')
_IDR = b'\x65\x88\x80\x00'
_P = b'\x41\x9a\x00'


def _lp(*nals):
    return b''.join(struct.pack('>I', len(n)) + n for n in nals)


def _h264_types(payload):
    return [n[0] & 0x1F for n in split_annexb(payload)]


def _h265_types(payload):
    return [(n[0] >> 1) & 0x3F for n in split_annexb(payload)]


def _is_lp(payload):
    pos = 0
    while pos + 4 <= len(payload):
        pos += 4 + int.from_bytes(payload[pos:pos + 4], 'big')
    return pos == len(payload) and payload[:4] != _SC


# -- packager ------------------------------------------------------------------

def test_annexb_key_frame_carries_parameter_sets():
    pub = LocTrackPublisher(None, 'ns', 'video', config=_AVCC,
                            annexb='h264')
    key = pub._package(_lp(_IDR), 0, 0)
    assert key == b'\x0a\x00' + avcc_param_sets(_AVCC) + _SC + _IDR
    assert _h264_types(key[2:]) == [7, 8, 5]
    assert pub._package(_lp(_P), 0, 1) == b'\x0a\x00' + _SC + _P
    exts = pub._object_extensions(LocFrame(_lp(_IDR), True, 1), True,
                                  NS(negotiated_draft=18))
    assert LOC_PROP_VIDEO_CONFIG not in exts
    assert pub.config_property is None        # nor as a Track Property


def test_annexb_keeps_in_band_parameter_sets_single():
    pub = LocTrackPublisher(None, 'ns', 'video', config=_AVCC,
                            annexb='h264')
    sps, pps = b'\x67\x64\x00', b'\x68\xee'
    out = pub._package(_lp(sps, pps, _IDR), 0, 0)
    assert _h264_types(out[2:]) == [7, 8, 5]


def test_default_is_length_prefixed_with_video_config():
    pub = LocTrackPublisher(None, 'ns', 'video', config=_AVCC)
    assert pub._package(_lp(_IDR), 0, 0) == b'\x0a\x00' + _lp(_IDR)
    exts = pub._object_extensions(LocFrame(_lp(_IDR), True, 1), True,
                                  NS(negotiated_draft=18))
    assert exts[LOC_PROP_VIDEO_CONFIG] == _AVCC


@pytest.mark.parametrize('kind,codec', [('audio', 'h264'), ('video', 'vp8')])
def test_annexb_refused_where_it_cannot_apply(kind, codec):
    with pytest.raises(ValueError):
        LocTrackPublisher(None, 'ns', 't', media_kind=kind, annexb=codec)


def test_hevc_parameter_sets_from_hvcc(tmp_path):
    video = Mp4Reader(_encode(tmp_path / 'h.mp4', 'mp4',
                              vcodec='libx265')).video
    assert _h265_types(hvcc_param_sets(video.config)) == [32, 33, 34]
    pub = LocTrackPublisher(None, 'ns', 'video', config=video.config,
                            annexb='h265')
    first = next(iter(video.samples()))
    out = pub._package(first.payload, 0, 0)[2:]
    types = _h265_types(out)
    assert types[:3] == [32, 33, 34] and types.count(33) == 1
    assert 16 <= types[-1] <= 21


# -- pub_media flags and catalog ------------------------------------------------

def test_catalog_codec_and_init(tmp_path, monkeypatch):
    clip = _encode(tmp_path / 'c.mp4', 'mp4', acodec='aac', seconds=1)
    video = Mp4Reader(clip).video
    assert video.codec_string.startswith('avc1.')
    for argv, codec, init in (
            ([], 'avc1', True), (['--annexb'], 'avc3', False),
            (['--no-catalog'], 'avc3', False)):
        args = _pub_args(monkeypatch, '--mp4', clip, *argv)
        entry = pub_media._build_catalog(args, video, None).find('video')
        assert entry.codec == codec + video.codec_string[4:], argv
        assert (entry.initRef is not None) == init, argv
        assert pub_media._annexb_family(args, video) == (
            'h264' if argv else None)
    assert pub_media._annexb_codec('hvc1.1.6.L93.B0') == 'hev1.1.6.L93.B0'


def test_annexb_refused_off_loc(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _pub_args(monkeypatch, '--mp4', 'x.mp4', '--packaging', 'cmaf',
                  '--annexb')
    assert 'LOC packaging only' in capsys.readouterr().err


# -- sub_media ------------------------------------------------------------------

def test_sub_media_writes_annexb_payload_verbatim(tmp_path):
    cat = Catalog.from_dict({"version": "1", "tracks": [
        {"name": "video", "packaging": "loc", "isLive": True,
         "role": "video", "codec": "avc3.64001F", "bitrate": 1}]})
    w = _Writers(str(tmp_path), NS(catalog=cat,
                                   tracks={"video": NS(config=None)}))
    payload = avcc_param_sets(_AVCC) + _SC + _IDR
    w.on_frame("video", LocFrame(payload=payload, key_frame=True), 0, 0)
    w.close()
    assert (tmp_path / "video.h264").read_bytes() == payload
    assert as_annexb(_lp(_IDR)) == _SC + _IDR


# -- end to end ----------------------------------------------------------------

@pytest.fixture
def payloads(monkeypatch):
    """Per track: (object id, property ids, payload) of each media object."""
    from aiomoqt.utils.media_stats import MediaAnalysis
    seen = {}
    orig = MediaAnalysis.on_arrival

    def record(self, name, msg, recv_us, group_id, subgroup_id, frame):
        if frame is not None:
            seen.setdefault(name, []).append(
                (msg.object_id, set(msg.extensions or ()), frame.payload))
        return orig(self, name, msg, recv_us, group_id, subgroup_id, frame)
    monkeypatch.setattr(MediaAnalysis, 'on_arrival', record)
    return seen


def _assert_annexb(objects, types, param_sets):
    keys = [p for o, _, p in objects if o == 0]
    assert len(objects) > 20 and len(keys) >= 1
    for oid, props, payload in objects:
        assert LOC_PROP_VIDEO_CONFIG not in props
        assert payload[:4] == _SC
        if oid == 0:
            assert types(payload)[:len(param_sets)] == param_sets
        else:
            assert not set(param_sets) & set(types(payload))


_E2E = [('mp4', 'libx264', True), ('ts', 'libx264', False),
        ('mp4', 'libx265', True), ('ts', 'libx265', True)]


@requires_certs
@pytest.mark.parametrize('source,vcodec,use_quic', _E2E)
async def test_annexb_end_to_end(tmp_path, monkeypatch, capsys, payloads,
                                 source, vcodec, use_quic):
    port = _BASE_PORT + _E2E.index((source, vcodec, use_quic))
    if source == 'mp4':
        pub = ['--mp4', _encode(tmp_path / 'c.mp4', 'mp4', vcodec=vcodec,
                                acodec='aac'), '--loop']
    else:
        data = open(_encode(tmp_path / 'c.ts', 'mpegts', vcodec=vcodec,
                            acodec='aac', seconds=6), 'rb').read()
        pub = ['--ts', _paced_fifo(tmp_path, data, 6)]
    rows = await _analyze(monkeypatch, tmp_path, port, [*pub, '--annexb'],
                          use_quic=use_quic, sub_argv=['--show-catalog'])
    assert int(rows['video']['objects']) > 20
    video = _catalogs(capsys.readouterr().err)[0]['tracks'][0]
    hevc = vcodec == 'libx265'
    assert video['codec'].startswith('hev1.' if hevc else 'avc3.')
    assert 'initRef' not in video
    _assert_annexb(payloads['video'], _h265_types if hevc else _h264_types,
                   [32, 33, 34] if hevc else [7, 8])


@requires_certs
async def test_default_end_to_end_unchanged(tmp_path, monkeypatch, capsys,
                                            payloads):
    clip = _encode(tmp_path / 'c.mp4', 'mp4', acodec='aac')
    await _analyze(monkeypatch, tmp_path, _BASE_PORT + 10,
                   ['--mp4', clip, '--loop'], sub_argv=['--show-catalog'])
    video = _catalogs(capsys.readouterr().err)[0]['tracks'][0]
    assert video['codec'].startswith('avc1.') and video['initRef'] == 'v0'
    objects = payloads['video']
    assert len(objects) > 20
    for oid, props, payload in objects:
        assert (LOC_PROP_VIDEO_CONFIG in props) == (oid == 0)
        assert _is_lp(payload)


@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_no_catalog_carries_parameter_sets(tmp_path, monkeypatch,
                                                 use_quic):
    port = _BASE_PORT + 12 + (not use_quic)
    url = f"{'moqt' if use_quic else 'https'}://localhost:{port}/"
    ns = f'annexb/{port}'
    clip = _encode(tmp_path / 'c.mp4', 'mp4', acodec='aac')
    pub_args = _argv(monkeypatch, 'pub_media', url, '-k', '--draft', '18',
                     '-t', '4', '--stats', '0', '--pub-ns', '-N', ns,
                     '--mp4', clip, '--loop', '--no-catalog')
    got = []

    async def body(relay):
        pub = asyncio.ensure_future(pub_media.run(pub_args))
        try:
            for _ in range(100):
                if relay._announced or pub.done():
                    break
                await asyncio.sleep(0.05)
            assert relay._announced, "publisher never announced"
            client = MOQTClient('localhost', port, path='/',
                                use_quic=use_quic, verify_tls=False,
                                supported_drafts=18)
            async with client.connect() as session:
                await session.client_session_init()
                sub = LocTrackSubscriber(
                    session, ns, 'video',
                    on_frame=lambda f, g, o: got.append(
                        (o, set(f.extensions or ()), f.payload)))
                await sub.subscribe()
                for _ in range(100):
                    if len(got) > 30:
                        break
                    await asyncio.sleep(0.05)
            await asyncio.wait_for(pub, 15)
        finally:
            if not pub.done():
                pub.cancel()
        return sub
    sub = await _with_relay(port, use_quic, body)
    assert sub.config is None
    _assert_annexb(got, _h264_types, [7, 8])
