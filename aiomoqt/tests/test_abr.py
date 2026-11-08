"""ABR ladder helpers: rung specs, frame rates, Annex B to LOC, the
ladder's catalog. No encoder needed."""
import struct

import pytest

from aiomoqt.media.abr import (
    DelayMeter, Rung, frame_rate, ladder_catalog, loc_from_annexb,
    quantiles_ms, rungs_below,
)
from aiomoqt.media.catalog import Catalog, CatalogTrack, InitData
from aiomoqt.media.sources import avcc_from_sps_pps

SPS = bytes([0x67, 0x64, 0x00, 0x1F, 0xAC, 0xD9, 0x40])
SPS_BASELINE = bytes([0x67, 0x42, 0xC0, 0x1E, 0x8C])
PPS = bytes([0x68, 0xEB, 0xE3, 0xCB])
SEI = bytes([0x06, 0x05, 0x01, 0x80])
IDR = bytes([0x65, 0x88, 0x84, 0x00, 0x33])
P_SLICE = bytes([0x41, 0x9A, 0x02, 0x10])


def _lp(*nals) -> bytes:
    return b''.join(struct.pack('>I', len(n)) + n for n in nals)


def test_rung_parse():
    assert Rung.parse('1280x720:3.75M') == Rung(1280, 720, 3_750_000)
    assert Rung.parse('640x360:900k') == Rung(640, 360, 900_000)
    assert Rung.parse('320x180:300000') == Rung(320, 180, 300_000)
    assert Rung(1280, 720, 1).name == 'video_720p'


@pytest.mark.parametrize('spec', ['1280x720', '1281x720:1M', '1280x720:0',
                                  'axb:1M', '1280x720:fast', '0x720:1M'])
def test_rung_parse_refuses(spec):
    with pytest.raises(ValueError):
        Rung.parse(spec)


def test_default_rungs_below_the_source():
    assert [r.height for r in rungs_below(2160)] == [1440, 1080, 720, 540,
                                                     360, 180]
    assert [r.height for r in rungs_below(720)] == [540, 360, 180]
    assert rungs_below(180) == []


@pytest.mark.parametrize('fps, expect', [
    (60, (60, 1)), (30, (30, 1)), (25, (25, 1)), (29.97, (30000, 1001)),
    (59.94, (60000, 1001)), (23.976, (24000, 1001))])
def test_frame_rate(fps, expect):
    assert frame_rate(fps) == expect


@pytest.mark.parametrize('fps', [0, -30, None])
def test_frame_rate_refuses(fps):
    with pytest.raises(ValueError):
        frame_rate(fps)


def test_loc_from_annexb_splits_out_parameter_sets():
    au = (b'\x00\x00\x00\x01\x09\xf0'
          + b'\x00\x00\x00\x01' + SPS + b'\x00\x00\x01' + PPS
          + b'\x00\x00\x01' + SEI + b'\x00\x00\x00\x01' + IDR
          + b'\x00\x00\x01\x0c\xff\xff')
    payload, sps, pps = loc_from_annexb(au)
    assert (sps, pps) == (SPS, PPS)
    assert payload == _lp(SEI, IDR)


def test_loc_from_annexb_delta_frame():
    assert loc_from_annexb(b'\x00\x00\x00\x01' + P_SLICE) == (
        _lp(P_SLICE), None, None)


def _source_catalog() -> Catalog:
    return Catalog(
        tracks=[
            CatalogTrack(name='video', packaging='loc', isLive=True,
                         role='video', renderGroup=1, codec='avc1.640033',
                         width=3840, height=2160, framerate=60,
                         bitrate=23_000_000, initRef='v0'),
            CatalogTrack(name='audio', packaging='loc', isLive=True,
                         role='audio', renderGroup=1, codec='mp4a.40.2',
                         samplerate=48000, channelConfig='2',
                         bitrate=128_000, initRef='a0')],
        initDataList=[InitData.from_bytes('v0', b'\x01src'),
                      InitData.from_bytes('a0', b'\x12\x10')])


def test_ladder_catalog_adds_each_rung_to_the_video_alt_group():
    src = _source_catalog()
    rungs = [Rung(1280, 720, 3_750_000), Rung(640, 360, 900_000)]
    configs = [avcc_from_sps_pps(SPS, PPS),
               avcc_from_sps_pps(SPS_BASELINE, PPS)]
    cat = ladder_catalog(src, 'video', rungs, configs)
    assert [t.name for t in cat.tracks] == ['video', 'video_720p',
                                            'video_360p', 'audio']
    video, r720, r360 = cat.tracks[:3]
    assert video.altGroup is not None
    assert r720.altGroup == r360.altGroup == video.altGroup
    assert (r720.width, r720.height, r720.bitrate, r720.label) == (
        1280, 720, 3_750_000, '720p')
    assert (r720.codec, r360.codec) == ('avc1.64001F', 'avc1.42C01E')
    assert (r720.framerate, r720.renderGroup, r720.isLive) == (60, 1, True)
    assert cat.resolve_init(r720) == configs[0]
    assert cat.resolve_init(r360) == configs[1]
    assert cat.resolve_init(video) == b'\x01src'
    assert src.find('video_720p') is None
    assert src.find('video').altGroup is None
    assert cat.validate() == []
    again = Catalog.from_json(cat.to_json())
    assert [t.name for t in again.tracks] == [t.name for t in cat.tracks]


def test_ladder_catalog_keeps_the_alt_group_and_free_init_ids():
    src = _source_catalog()
    src.find('video').altGroup = 3
    src.initDataList.append(InitData.from_bytes('abr0', b'taken'))
    config = avcc_from_sps_pps(SPS, PPS)
    cat = ladder_catalog(src, 'video', [Rung(640, 360, 900_000)], [config])
    rung = cat.find('video_360p')
    assert rung.altGroup == 3
    assert rung.initRef != 'abr0'
    assert cat.resolve_init(rung) == config


def test_ladder_catalog_needs_the_video_track():
    with pytest.raises(ValueError):
        ladder_catalog(_source_catalog(), 'nope', [Rung(640, 360, 1)],
                       [avcc_from_sps_pps(SPS, PPS)])


def test_delay_meter_and_quantiles():
    meter = DelayMeter()
    for ms in range(1, 101):
        meter.add(ms * 1_000_000)
    assert quantiles_ms(meter.take()) == (51.0, 100.0, 100.0)
    assert meter.take() == []
    assert quantiles_ms([]) is None
