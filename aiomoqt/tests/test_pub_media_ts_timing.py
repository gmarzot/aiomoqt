"""pub_media timing and catalog for moq-test-tracks: --ts-cadence mapping,
measured or explicit catalog framerate and bitrate, --init-data
placement of decoder config, and --no-catalog, end to end through the
interop relay into sub_media --analyze."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from aiomoqt.media.catalog import Catalog
from aiomoqt.media.loc import (
    LOC_PROP_AUDIO_CONFIG, LOC_PROP_VIDEO_CONFIG, LocTrackSubscriber,
)
from aiomoqt.media.mpegts import TsCadence, TsDemuxer, TsRates, TsUnit
from aiomoqt.media.sources import Mp4Reader
from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_pub_media_frindell import _argv
from aiomoqt.tests.test_pub_media_mtt import (
    _analyze, _encode, _paced_fifo, _pub_args, _with_relay,
)
from aiomoqt.tools import pub_media
from aiomoqt.utils.timing import (
    OBJECT_TIMESTAMP, TIMESCALE, TIMESTAMP_MAPPING,
)

_BASE_PORT = 19500
_CONFIG = (LOC_PROP_VIDEO_CONFIG, LOC_PROP_AUDIO_CONFIG)


@pytest.fixture
def received(monkeypatch):
    """Per track: (group, object, property ids) of each media object, and
    the Track Properties of its subscription."""
    from aiomoqt.utils.media_stats import MediaAnalysis
    seen = {'objects': {}, 'track': {}}
    orig = MediaAnalysis.on_arrival

    def record(self, name, msg, recv_us, group_id, subgroup_id, frame):
        if frame is not None:
            seen['objects'].setdefault(name, []).append(
                (group_id, msg.object_id, set(msg.extensions or ())))
        return orig(self, name, msg, recv_us, group_id, subgroup_id, frame)
    monkeypatch.setattr(MediaAnalysis, 'on_arrival', record)
    take = LocTrackSubscriber.take_track_properties

    def track_props(self, msg):
        take(self, msg)
        seen['track'][self.trackname] = dict(self.properties or {})
    monkeypatch.setattr(LocTrackSubscriber, 'take_track_properties',
                        track_props)
    return seen


@pytest.fixture(scope='module')
def h264_aac(tmp_path_factory):
    return _encode(tmp_path_factory.mktemp('tst') / 'clip.mp4', 'mp4',
                   acodec='aac', layout='stereo')


def _ts(tmp_path, acodec='libopus', seconds=6):
    data = open(_encode(tmp_path / 'clip.ts', 'mpegts', acodec=acodec,
                        seconds=seconds), 'rb').read()
    return _paced_fifo(tmp_path, data, seconds)


def _catalogs(err: str):
    """Every catalog sub_media --show-catalog printed."""
    out, dec, i = [], json.JSONDecoder(), err.find('{')
    while i != -1:
        try:
            obj, end = dec.raw_decode(err, i)
        except ValueError:
            i = err.find('{', i + 1)
            continue
        if isinstance(obj, dict) and 'tracks' in obj:
            out.append(obj)
        i = err.find('{', end)
    return out


# -- --ts-cadence ---------------------------------------------------------

@requires_certs
@pytest.mark.parametrize('cadence,use_quic', [
    (True, True), (True, False), (False, True)])
async def test_ts_cadence_maps_timing(tmp_path, monkeypatch, received,
                                      cadence, use_quic):
    port = _BASE_PORT + 2 * cadence + (not use_quic)
    flags = (['--ts-cadence', 'fps=30,gop=30'] if cadence
             else ['--timestamp-mapping'])
    rows = await _analyze(monkeypatch, tmp_path, port,
                          ['--ts', _ts(tmp_path), *flags], use_quic=use_quic)
    for name, scale in (('video', 30000), ('audio', 48000)):
        props = received['track'][name]
        objects = received['objects'][name]
        assert len(objects) > 20 and rows[name]['ts_malformed'] == '0'
        assert rows[name]['ts_source'] == 'frindell'
        stamped = [o for o in objects if OBJECT_TIMESTAMP in o[2]]
        if cadence:
            assert props[TIMESCALE] == scale and TIMESTAMP_MAPPING in props
            assert stamped == []              # on schedule throughout
            assert 0 <= float(rows[name]['lat_p50_ms']) < 1000
        else:
            assert props[TIMESCALE] == 1_000_000
            assert TIMESTAMP_MAPPING not in props
            assert len(stamped) == len(objects)


def test_cadence_stamps_only_the_frame_off_schedule():
    cadence = TsCadence.parse('fps=30,gop=10')
    wall0 = 1_767_225_600_000_000
    clock = NS(to_us=lambda pts: wall0 + pts * 1000 // 90)
    first = [TsUnit('video', b'k', True, 0)]
    dmx = NS(to_us=clock.to_us)
    m = pub_media._ts_timestamps(NS(ts_cadence=cadence), dmx, first,
                                 'video', 0)
    assert m.mapped and m.timescale == 30000
    pts = [i * 3000 for i in range(10)]
    pts[6] += 900                            # 10 ms late
    stamped = [i for i, p in enumerate(pts)
               if m.object_properties(0, i, clock.to_us(p))]
    assert stamped == [6]
    explicit = pub_media._ts_timestamps(NS(ts_cadence=None), dmx, first,
                                        'video', wall0)
    assert not explicit.mapped


@pytest.mark.parametrize('argv,why', [
    (['--ts-cadence', 'fps=30,gop=60'], 'applies to --ts only'),
    (['--ts', '-', '--ts-cadence', 'fps=30'], 'fps=N[/D],gop=M'),
    (['--mp4', 'x.mp4', '--packaging', 'cmaf', '--no-catalog'],
     'LOC packaging only'),
    (['--mp4', 'x.mp4', '--packaging', 'cmaf', '--init-data', 'track'],
     'LOC packaging only'),
    (['--no-catalog', '--init-data', 'catalog'], 'needs a catalog'),
    (['--catalog-bitrate', '0'], 'must be positive'),
])
def test_flags_refused_where_they_cannot_apply(monkeypatch, capsys, argv,
                                               why):
    with pytest.raises(SystemExit):
        _pub_args(monkeypatch, *argv)
    assert why in capsys.readouterr().err


def test_ts_cadence_implies_timestamp_mapping(monkeypatch):
    args = _pub_args(monkeypatch, '--ts', '-', '--ts-cadence',
                     'fps=30000/1001,gop=60')
    assert args.timestamp_mapping and args.ts_cadence.timescale == 30000


# -- catalog framerate and bitrate ------------------------------------------

def _units(path):
    d = TsDemuxer()
    data = open(path, 'rb').read()
    units = []
    for i in range(0, len(data), 1316):
        units += d.feed(data[i:i + 1316])
    return d, units + d.close()


async def test_ts_catalog_measures_framerate_and_bitrate(tmp_path,
                                                         monkeypatch):
    ts = _encode(tmp_path / 'c.ts', 'mpegts', acodec='aac', seconds=4)
    mp4 = Mp4Reader(_encode(tmp_path / 'c.mp4', 'mp4', acodec='aac',
                            seconds=4))
    d, units = _units(ts)
    args = _pub_args(monkeypatch, '--ts', ts)
    cat = pub_media._build_catalog(args, pub_media._LiveVideo(d.video),
                                   pub_media._LiveAudio(d))
    v, a = cat.find('video'), cat.find('audio')
    assert (v.framerate, v.bitrate, a.bitrate) == (None, None, None)
    rates = TsRates()
    for u in units:
        rates.add(u)
    published = []

    async def publish(c):
        published.append(c.to_json())
    pub = NS(catalog_track=NS(catalog=cat, state=None,
                              publish_catalog=publish))
    await pub_media._apply_rates(rates, args, pub)
    # As the mp4 of the same encode reports it (CMAF catalog values).
    assert v.framerate == mp4.video.fps == 30.0
    for entry, src in ((v, mp4.video), (a, mp4.audio)):
        assert abs(entry.bitrate - src.avg_bitrate) < 0.15 * src.avg_bitrate
    assert Catalog.from_json(cat.to_json()).find('video').bitrate == v.bitrate


def test_explicit_bitrate_and_cadence_framerate(monkeypatch, tmp_path):
    ts = _encode(tmp_path / 'c.ts', 'mpegts', acodec='aac', seconds=1)
    d, _ = _units(ts)
    args = _pub_args(monkeypatch, '--ts', ts, '--catalog-bitrate', '2500000')
    video = pub_media._LiveVideo(d.video)
    video.fps = TsCadence.parse('fps=30,gop=60').framerate
    cat = pub_media._build_catalog(args, video, pub_media._LiveAudio(d))
    assert (cat.find('video').framerate, cat.find('video').bitrate) == (
        30.0, 2_500_000)
    assert cat.find('audio').bitrate is None          # measured later


@requires_certs
async def test_ts_catalog_reaches_subscribers(tmp_path, monkeypatch,
                                              capsys):
    await _analyze(monkeypatch, tmp_path, _BASE_PORT + 10,
                   ['--ts', _ts(tmp_path), '--ts-cadence', 'fps=30,gop=30',
                    '--catalog-bitrate', '900000'],
                   sub_argv=['--show-catalog'])
    video = _catalogs(capsys.readouterr().err)[0]['tracks'][0]
    assert (video['name'], video['framerate'], video['bitrate']) == (
        'video', 30.0, 900_000)


# -- --init-data -------------------------------------------------------------

_INIT_TYPES = {None: 'inline', 'catalog': 'inline',
               'track': 'track-property', 'object': 'object-property'}


@requires_certs
@pytest.mark.parametrize('mode,source,use_quic', [
    (None, 'mp4', True), ('catalog', 'mp4', True), ('track', 'mp4', True),
    ('track', 'mp4', False), ('object', 'mp4', True), ('track', 'ts', True),
    ('catalog', 'ts', False)])
async def test_init_data_placement(tmp_path, monkeypatch, capsys, received,
                                   h264_aac, mode, source, use_quic):
    port = (_BASE_PORT + 20
            + [(None, 'mp4', True), ('catalog', 'mp4', True),
               ('track', 'mp4', True), ('track', 'mp4', False),
               ('object', 'mp4', True), ('track', 'ts', True),
               ('catalog', 'ts', False)].index((mode, source, use_quic)))
    pub = (['--mp4', h264_aac, '--loop'] if source == 'mp4'
           else ['--ts', _ts(tmp_path, acodec='aac')])
    await _analyze(monkeypatch, tmp_path, port,
                   [*pub, *(['--init-data', mode] if mode else [])],
                   use_quic=use_quic, sub_argv=['--show-catalog'])
    catalog = _catalogs(capsys.readouterr().err)[0]
    types = {i['id']: i['type'] for i in catalog['initDataList']}
    assert set(types.values()) == {_INIT_TYPES[mode]}, types
    if mode in ('track', 'object'):
        assert {i['data'] for i in catalog['initDataList']} == {'0x0D', '0x0F'}
    for name, prop in (('video', LOC_PROP_VIDEO_CONFIG),
                       ('audio', LOC_PROP_AUDIO_CONFIG)):
        objects = received['objects'][name]
        assert len(objects) > 10
        on = {(g, o) for g, o, props in objects if prop in props}
        firsts = {(g, o) for g, o, _ in objects if o == 0}
        assert on == (firsts if mode in (None, 'object') else set()), name
        assert not any(set(_CONFIG) - {prop} & props
                       for _, _, props in objects)
        track = received['track'][name]
        if mode == 'track':
            assert isinstance(track.get(prop), (bytes, bytearray)), name
        else:
            assert not set(_CONFIG) & set(track), name


# -- --no-catalog --------------------------------------------------------------

@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_no_catalog_publishes_no_catalog_track(monkeypatch, h264_aac,
                                                     use_quic):
    port = _BASE_PORT + 40 + (not use_quic)
    url = f"{'moqt' if use_quic else 'https'}://localhost:{port}/"
    pub_args = _argv(monkeypatch, 'pub_media', url, '-k', '--draft', '18',
                     '-t', '2', '--stats', '0', '--pub-both', '-N',
                     'tst/nocat', '--mp4', h264_aac, '--loop',
                     '--no-catalog')

    async def body(relay):
        pub = asyncio.ensure_future(pub_media.run(pub_args))
        names = set()
        for _ in range(30):
            await asyncio.sleep(0.05)
            names |= {str(k) for k in relay._tracks}
        await asyncio.wait_for(pub, 15)
        return names
    names = await _with_relay(port, use_quic, body)
    assert any('video' in n for n in names) and any('audio' in n
                                                    for n in names)
    assert not any('catalog' in n for n in names), names
