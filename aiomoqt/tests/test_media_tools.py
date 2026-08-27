"""pub_media/sub_media tool logic: catalog built from an mp4, playable
Annex-B/.wav writers. Transport paths are covered by test_broadcast."""
import struct
import wave
from types import SimpleNamespace

from aiomoqt.media import Catalog, LocFrame
from aiomoqt.media.sources import Mp4AvcReader, avcc_param_sets
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
