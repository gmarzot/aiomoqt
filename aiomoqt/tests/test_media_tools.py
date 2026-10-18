"""pub_media/sub_media tool logic: catalog built from an mp4, playable
Annex-B/.wav writers. Transport paths are covered by test_broadcast."""
import struct
import sys
import wave
from types import SimpleNamespace

import pytest

from aiomoqt.media import Catalog, LocFrame
from aiomoqt.media.cmaf import CmafChunker
from aiomoqt.media.sources import Mp4AvcReader, avcc_param_sets
from aiomoqt.tools import pub_media, sub_media
from aiomoqt.tools.pub_media import _build_catalog
from aiomoqt.tools.sub_media import _Writers
from aiomoqt.tests.test_media_sources import _mp4, _AVCC


def test_build_catalog_from_mp4(tmp_path):
    path = tmp_path / "t.mp4"
    path.write_bytes(_mp4())
    reader = Mp4AvcReader(str(path))
    args = SimpleNamespace(no_audio=False, packaging='loc')
    cat = _build_catalog(args, reader, None)
    assert cat.validate() == []
    video = cat.find("video")
    assert video.codec == "avc1.64001F"
    assert (video.width, video.height) == (640, 360)
    assert cat.resolve_init(video) == _AVCC
    audio = cat.find("audio")
    assert audio.codec == "pcm-s16" and audio.channelConfig == "2"


def test_writers_produce_playable_files(tmp_path):
    avcc = bytes([1, 0x64, 0, 0x1F, 0xFF,
                  0xE1, 0, 3]) + b'\x67\x64\x00' + bytes([1, 0, 2]) + b'\x68\xee'
    cat = Catalog.from_dict({
        "version": "1",
        "tracks": [
            {"name": "video", "packaging": "loc", "isLive": True,
             "role": "video", "codec": "avc1.640028", "bitrate": 1},
            {"name": "audio", "packaging": "loc", "isLive": True,
             "role": "audio", "codec": "pcm-s16", "samplerate": 8000,
             "channelConfig": "1", "bitrate": 1},
        ]})
    sub = SimpleNamespace(
        catalog=cat, tracks={"video": SimpleNamespace(config=avcc)})
    w = _Writers(str(tmp_path), sub)
    # length-prefixed AVC sample: one 4-byte NAL
    lp = struct.pack('>I', 4) + b'\x65\x88\x80\x00'
    w.on_frame("video", LocFrame(payload=lp, key_frame=True), 0, 0)
    w.on_frame("audio", LocFrame(payload=b'\x00\x01' * 80,
                                 key_frame=True), 0, 0)
    w.close()
    h264 = (tmp_path / "video.h264").read_bytes()
    # SPS+PPS injected before the keyframe, prefixes → start codes.
    assert h264.startswith(avcc_param_sets(avcc))
    assert h264.endswith(b'\x00\x00\x00\x01\x65\x88\x80\x00')
    with wave.open(str(tmp_path / "audio.wav")) as f:
        assert (f.getnchannels(), f.getframerate(),
                f.getnframes()) == (1, 8000, 80)
    assert w.counts == {"video": 1, "audio": 1}


def test_build_catalog_for_locmaf(tmp_path):
    path = tmp_path / "t.mp4"
    path.write_bytes(_mp4())
    reader = Mp4AvcReader(str(path))
    chunker = CmafChunker(reader)
    args = SimpleNamespace(no_audio=True, packaging='locmaf',
                           target_latency=500)
    cat = _build_catalog(args, reader, None, {'video': chunker})
    assert cat.validate() == []
    video = cat.find("video")
    assert (video.packaging, video.locmafVersion) == ('locmaf', '0.3')
    assert cat.resolve_init(video) == chunker.init_segment()


def _pub_args(monkeypatch, *argv):
    monkeypatch.setattr(sys, 'argv', ['pub_media', 'moqt://h:4433/', *argv])
    return pub_media.parse_args()


@pytest.mark.parametrize('argv', [
    ('--packaging', 'locmaf'),
    ('--packaging', 'locmaf', '--mp4', 'x.mp4', '-D'),
    ('--packaging', 'locmaf', '--mp4', 'x.mp4', '--loc-codecstring'),
    ('--packaging', 'locmaf', '--mp4', 'x.mp4', '--loc-bare'),
    ('--packaging', 'cmaf', '--mp4', 'x.mp4', '--loc-bare'),
], ids=['no-source', 'datagram', 'loc-codecstring', 'loc-bare-locmaf',
        'loc-bare-cmaf'])
def test_pub_media_locmaf_flag_conflicts(monkeypatch, argv):
    with pytest.raises(SystemExit):
        _pub_args(monkeypatch, *argv)


def test_pub_media_input_takes_locmaf(monkeypatch):
    args = _pub_args(monkeypatch, '--input', 'x.m3u8', '--packaging', 'locmaf')
    assert args.packaging == 'locmaf'


def test_sub_media_packaging_filter(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['sub_media', 'moqt://h:4433/',
                                      '--packaging', 'locmaf'])
    args = sub_media.parse_args()
    entries = [SimpleNamespace(name=n, packaging=p) for n, p in (
        ('video', 'cmaf'), ('video_locmaf', 'locmaf'), ('audio', 'loc'))]
    assert [e.name for e in entries if sub_media._wanted(args, e)] == [
        'video_locmaf']
    args.packaging = None
    assert all(sub_media._wanted(args, e) for e in entries)


def test_writers_name_mp4_files_by_track_when_roles_are_shared(tmp_path):
    cat = Catalog.from_dict({"version": "1", "tracks": [
        {"name": n, "packaging": p, "isLive": True, "role": r,
         "locmafVersion": "0.3" if p == 'locmaf' else None}
        for n, p, r in (('video', 'cmaf', 'video'),
                        ('video_locmaf', 'locmaf', 'video'),
                        ('audio_locmaf', 'locmaf', 'audio'))]})
    w = _Writers(str(tmp_path), SimpleNamespace(catalog=cat, tracks={}))
    for name in ('video', 'video_locmaf', 'audio_locmaf'):
        w.on_frame(name, LocFrame(payload=name.encode()), 0, 0)
    w.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        'audio.mp4', 'video.mp4', 'video_locmaf.mp4']
    assert (tmp_path / 'video_locmaf.mp4').read_bytes() == b'video_locmaf'
