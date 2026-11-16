"""pub_media --ts --msfts and sub_media --ts-out / --frame-log end to end
through the interop relay over raw QUIC and WebTransport, TS in and out
over UDP, and the flags' checks."""
import asyncio
import contextlib
import hashlib
import json
import os
import socket
import threading
import time

import jsonschema
import pytest

from aiomoqt.media.mpegts import TsDemuxer
from aiomoqt.media.udp import UdpInput, UdpOutput, parse_udp_url
from aiomoqt.tests import test_mpegts as mts
from aiomoqt.tests._certs import requires_certs
from aiomoqt.media import Catalog
from aiomoqt.media.broadcast import MediaSubscriber
from aiomoqt.media.msfts import TsPackager
from aiomoqt.media.timeline import MediaTimeline
from aiomoqt.tests.test_msfts import (
    ONE, P, PMT_PID, VIDEO_PID, _ffmpeg_mpeg2, changing_stream, gop_stream,
)
from aiomoqt.tests.test_pub_media_frindell import _argv
from aiomoqt.tests.test_pub_media_mtt import _paced_fifo
from aiomoqt.tests.test_pub_media_service import (
    _relay_end_of_track, _serve, _stop, _until, _url,
)
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tools import pub_media, sub_media

_PORT = 32010


async def _through_relay(monkeypatch, port, use_quic, pub_argv, sub_argv,
                         seconds=20):
    """pub_media and sub_media through the relay; sub_media's exit code."""
    url = _url(port, use_quic)
    ns = f'msfts/{port}'
    pub_args = _argv(monkeypatch, 'pub_media', url, '-k', '--draft', '18',
                     '-t', str(seconds), '--pub-ns', '-N', ns, '--stats',
                     '0', *pub_argv)
    sub_args = _argv(monkeypatch, 'sub_media', url, '-k', '--draft', '18',
                     '-N', ns, *sub_argv)
    srv, handle = await _serve(port, use_quic, 18)
    rc = None
    try:
        pub = asyncio.ensure_future(pub_media.run(pub_args))
        await _until(lambda: relay._announced or pub.done(), 10)
        sub = asyncio.ensure_future(sub_media.run(sub_args))
        await asyncio.wait_for(pub, seconds + 15)
        await asyncio.sleep(0.5)
        sub.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            rc = await sub
    finally:
        await _stop(srv, handle)
    return rc


def _check_suffix(data: bytes, out: bytes, min_groups: int) -> None:
    """The source from a group start to its end, byte for byte."""
    assert out, "no objects delivered"
    assert len(out) % P == 0
    assert data.endswith(out)
    # A group opens on its PSI run: SDT or PAT first.
    assert out[1:3] in (b'\x40\x00', b'\x40\x11')
    pats = sum(1 for i in range(0, len(out), P) if out[i + 1:i + 3] == b'\x40\x00')
    assert pats >= min_groups


@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_fifo_ts_round_trips_byte_for_byte(monkeypatch, tmp_path,
                                                 use_quic):
    data, _ = gop_stream(12, frames=10, lead=False)
    out = tmp_path / 'out.ts'
    rc = await _through_relay(
        monkeypatch, _PORT + (not use_quic), use_quic,
        ['--ts', _paced_fifo(tmp_path, data, 3), '--msfts'],
        ['--ts-out', str(out), '-t', '30'])
    assert rc == 0
    _check_suffix(data, out.read_bytes(), 4)


@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_playout_and_analysis(monkeypatch, capsys, tmp_path, use_quic):
    data = _ffmpeg_mpeg2(tmp_path / 'cbr.ts', seconds=4)
    out, log = tmp_path / 'out.ts', tmp_path / 'pcr.jsonl'
    rc = await _through_relay(
        monkeypatch, _PORT + 8 + (not use_quic), use_quic,
        ['--ts', _paced_fifo(tmp_path, data, 4), '--msfts'],
        ['--ts-out', str(out), '--ts-playout', '300', '--analyze', '-i', '1',
         '--arrival-log', str(log), '-t', '30'])
    assert rc == 0
    with open(os.path.join(os.path.dirname(__file__), 'vectors',
                           'ts_analysis_schema.json')) as fh:
        schema = json.load(fh)
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()
             if ln.startswith('{')]
    for line in lines:
        jsonschema.validate(line, schema)
    summary = lines[-1]
    assert summary['type'] == 'summary' and len(lines) >= 3
    assert summary['track'] == 'program-1'
    assert summary['ts_packets'] > 0
    assert summary['ts_packets'] == sum(x['ts_packets'] for x in lines[:-1])
    assert summary['cc_errors_total'] == 0
    assert summary['pcr']['pid'] == 256 and summary['pcr']['count'] > 50
    assert summary['playout']['buffer_ms'] == 300
    assert summary['playout']['late'] == 0
    assert summary['e2e_ms']['source'] == 'media-timeline'
    # Played out on the PCR, the packets are the source's, in order.
    played = out.read_bytes()
    assert played and data.endswith(played)
    pcrs = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert len(pcrs) == summary['pcr']['count']
    assert all(r['release_ns'] - r['arrival_ns'] >= 0 for r in pcrs)


@requires_certs
async def test_udp_in_and_out(monkeypatch, tmp_path):
    data = _ffmpeg_mpeg2(tmp_path / 'cbr.ts', seconds=5)
    port_in, port_out = _PORT + 2, _PORT + 3
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(('127.0.0.1', port_out))
    rx.settimeout(0.2)
    got, stop = [], threading.Event()

    def receive():
        while not stop.is_set():
            try:
                got.append(rx.recv(65536))
            except socket.timeout:
                pass

    def send():
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        step = 7 * P
        chunks = [data[i:i + step] for i in range(0, len(data), step)]
        time.sleep(0.3)
        t0 = time.monotonic()
        for k, chunk in enumerate(chunks):
            delay = t0 + 4 * k / len(chunks) - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            tx.sendto(chunk, ('127.0.0.1', port_in))
        tx.close()
    threads = [threading.Thread(target=f, daemon=True) for f in (receive,
                                                                 send)]
    for t in threads:
        t.start()
    try:
        rc = await _through_relay(
            monkeypatch, _PORT + 4, True,
            ['--ts', f'udp://@:{port_in}', '--msfts'],
            ['--ts-out', f'udp://127.0.0.1:{port_out}', '-t', '30'],
            seconds=6)
    finally:
        await asyncio.sleep(0.3)
        stop.set()
        threads[0].join()
        rx.close()
    assert rc == 0
    assert got and all(len(d) % P == 0 and len(d) <= 7 * P for d in got)
    out = b''.join(got)
    # UDP has no end of stream: the run ends on -t, so the output is a
    # contiguous run of the source from a group start.
    assert out and out[1:3] in (b'\x40\x00', b'\x40\x11')
    assert out in data and len(out) > len(data) // 3


@requires_certs
async def test_sub_media_writes_the_track_without_ts_out(monkeypatch,
                                                         tmp_path):
    data, _ = gop_stream(8, frames=10, lead=False)
    out = tmp_path / 'media'
    await _through_relay(
        monkeypatch, _PORT + 5, False,
        ['--ts', _paced_fifo(tmp_path, data, 2), '--msfts', '-T', 'feed'],
        ['--out', str(out), '-t', '30'])
    _check_suffix(data, (out / 'feed.ts').read_bytes(), 2)


@requires_certs
@pytest.mark.parametrize('sub_argv,why', [
    ([], "no mpeg2ts track in the catalog (tracks: audio loc)"),
    (['-T', 'audio'], "track 'audio' is loc, not mpeg2ts"),
])
async def test_ts_out_refuses_loc_tracks(monkeypatch, capsys, tmp_path,
                                         sub_argv, why):
    port = _PORT + 6
    url = _url(port, True)
    pub_args = _argv(monkeypatch, 'pub_media', url, '-k', '--draft', '18',
                     '-t', '10', '--pub-ns', '-N', 'msfts/loc', '--stats', '0')
    sub_args = _argv(monkeypatch, 'sub_media', url, '-k', '--draft', '18',
                     '-N', 'msfts/loc', '--ts-out', str(tmp_path / 'x.ts'),
                     *sub_argv)
    srv, handle = await _serve(port, True, 18)
    pub = asyncio.ensure_future(pub_media.run(pub_args))
    try:
        await _until(lambda: relay._announced or pub.done(), 10)
        with pytest.raises(SystemExit) as e:
            await sub_media.run(sub_args)
    finally:
        pub.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pub
        await _stop(srv, handle)
    assert e.value.code == 2
    assert f"--ts-out: {why}" in capsys.readouterr().err
    assert not (tmp_path / 'x.ts').exists()


def _h264_aac_ts(frames=150):
    """H.264 and AAC, every access unit distinct: 30 fps, a key frame
    every 15 frames."""
    out, vcc, acc = [mts.av_stream()], 0, 0
    for k in range(frames):
        nals = ([mts._SPS, mts._PPS, mts.idr(400)[:-2] + bytes([k, 1])]
                if k % 15 == 0 else [b'\x41\x9a' + bytes([k, 2]) * 60])
        pkts, vcc = mts.packetize(mts.VIDEO_PID,
                                  mts.video_pes(nals, 90_000 + k * 3000), vcc)
        out += pkts
        if k % 2 == 0:
            frame = bytes([k]) * 40
            pkts, acc = mts.packetize(mts.AUDIO_PID,
                                      mts.audio_pes([frame], 90_000 + k * 3000),
                                      acc)
            out += pkts
    return b''.join(out)


@requires_certs
async def test_frame_log_hashes_match_the_demuxer(monkeypatch, tmp_path):
    data = _h264_aac_ts()
    want = {}
    dmx = TsDemuxer()
    for u in dmx.feed(data) + dmx.close():
        want.setdefault(u.track, set()).add(hashlib.sha1(u.payload).hexdigest())
    log = tmp_path / 'frames.jsonl'
    t0 = time.time_ns()
    await _through_relay(
        monkeypatch, _PORT + 7, True,
        ['--ts', _paced_fifo(tmp_path, data, 4)],
        ['--analyze', '-t', '30', '-i', '1', '--frame-log', str(log)])
    lines = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert lines, "no frames logged"
    seen = {}
    for e in lines:
        assert set(e) == {'track', 'group', 'object', 'key', 'size', 'sha1',
                          'arrival_ns'}
        assert e['sha1'] in want[e['track']], e
        assert e['key'] == (e['object'] == 0)
        assert t0 < e['arrival_ns'] < time.time_ns()
        seen.setdefault(e['track'], set()).add(e['sha1'])
    assert len(seen.get('video', ())) >= 60
    assert len(seen.get('audio', ())) >= 30


# -- flags ----------------------------------------------------------------

def _pub(monkeypatch, *argv):
    return _argv(monkeypatch, 'pub_media', 'moqt://localhost:1/', *argv)


def _sub(monkeypatch, *argv):
    return _argv(monkeypatch, 'sub_media', 'moqt://localhost:1/', *argv)


def test_flag_defaults(monkeypatch):
    a = _pub(monkeypatch, '--ts', 'udp://@:5000', '--msfts')
    assert (a.ts, a.msfts, a.msfts_packets, a.msfts_timestamps) == (
        'udp://@:5000', True, 7, 'timeline')
    a = _pub(monkeypatch, '--ts', '-', '--msfts', '--msfts-packets', '348')
    assert a.msfts_packets == 348
    s = _sub(monkeypatch)
    assert s.ts_out is None and s.frame_log is None
    assert (s.ts_playout, s.ts_late, s.break_ms, s.arrival_log) == (
        None, 'drop', 200, None)
    s = _sub(monkeypatch, '--ts-out', 'x.ts', '--ts-playout', '300',
             '--ts-late', 'emit', '--break-ms', '100', '--analyze',
             '--arrival-log', 'a.jsonl')
    assert (s.ts_playout, s.ts_late, s.break_ms, s.arrival_log) == (
        300, 'emit', 100, 'a.jsonl')
    s = _sub(monkeypatch, '--ts-out', 'udp://10.0.0.1:6000', '--frame-log',
             'f.jsonl')
    assert (s.ts_out, s.frame_log) == ('udp://10.0.0.1:6000', 'f.jsonl')


@pytest.mark.parametrize('argv', [
    ['--msfts'],
    ['--ts', '-', '--msfts-packets', '8'],
    ['--ts', '-', '--msfts-timestamps', 'object'],
    ['--ts', '-', '--msfts', '--msfts-timestamps', 'pcr'],
    ['--ts', '-', '--msfts', '--msfts-packets', '0'],
    ['--ts', '-', '--msfts', '--msfts-packets', '5001'],
    ['--ts', '-', '--msfts', '--ts-cadence', 'fps=25,gop=25'],
    ['--ts', '-', '--msfts', '--timestamp-mapping'],
    ['--ts', '-', '--msfts', '-D'],
    ['--ts', '-', '--msfts', '--no-catalog'],
    ['--ts', '-', '--msfts', '--annexb'],
    ['--ts', '-', '--msfts', '--video-subgroups', 'object'],
    ['--ts', 'udp://@:port'],
    ['--ts', 'udp://@:5000?pkt_size=1316'],
    ['--ts', 'udp://@'],
])
def test_pub_media_refuses(monkeypatch, argv):
    with pytest.raises(SystemExit) as e:
        _pub(monkeypatch, *argv)
    assert e.value.code == 2


@pytest.mark.parametrize('argv', [
    ['--ts-playout', '0', '--ts-out', 'x.ts'],
    ['--ts-playout', '300'],
    ['--break-ms', '0'],
    ['--ts-late', 'skip'],
    ['--ts-out', 'x.ts', '--packaging', 'loc'],
    ['--ts-out', '-', '--pipe', 'video'],
    ['--ts-out', 'udp://@:6000'],
    ['--ts-out', 'udp://host'],
])
def test_sub_media_refuses(monkeypatch, argv):
    with pytest.raises(SystemExit) as e:
        _sub(monkeypatch, *argv)
    assert e.value.code == 2


@pytest.mark.parametrize('url,listen,want', [
    ('udp://@:5000', True, ('', 5000)),
    ('udp://:5000', True, ('', 5000)),
    ('udp://@239.1.1.1:5000', True, ('239.1.1.1', 5000)),
    ('udp://127.0.0.1:5000', True, ('127.0.0.1', 5000)),
    ('udp://[::1]:5000', False, ('::1', 5000)),
    ('udp://10.1.2.3:6000/', False, ('10.1.2.3', 6000)),
])
def test_udp_urls(url, listen, want):
    assert parse_udp_url(url, listen=listen) == want


@pytest.mark.parametrize('url,listen', [
    ('udp://@:5000', False), ('udp://:5000', False), ('udp://h', False),
    ('udp://h:0', False), ('udp://h:5000/x', True), ('udp://u@h:1', True),
    ('udp://h:70000', True), ('tcp://h:1', True),
])
def test_bad_udp_urls(url, listen):
    with pytest.raises(ValueError):
        parse_udp_url(url, listen=listen)


async def test_udp_input_reads_queued_datagrams_and_closes():
    rx = UdpInput('udp://127.0.0.1:0')
    port = rx.address[1]
    tx = UdpOutput(f'udp://127.0.0.1:{port}')
    tx.write(b'\x47' * (P * 10))
    await asyncio.sleep(0.05)
    data = await rx.read(1.0)
    assert data == b'\x47' * (P * 10)
    assert await rx.read(0.05) is None
    tx.write(b'\x47' * P)
    assert rx.read1() == b'\x47' * P
    threading.Timer(0.1, rx.close).start()
    assert rx.read1() == b''
    tx.close()


# -- wall-clock references --------------------------------------------------

def test_media_timeline_keeps_its_window():
    t = MediaTimeline(window_s=2)
    t.add(0, 0, 0, 10_000)
    t.add(1000, 1, 0, 11_000)
    t.add(2000, 2, 0, 12_000)
    assert json.loads(t.add(3000, 3, 0, 13_000)) == [
        [1000, [1, 0], 11_000], [2000, [2, 0], 12_000],
        [3000, [3, 0], 13_000]]


@pytest.mark.parametrize('mode,tracks', [
    ('timeline', ['program-1', 'program-1.timeline']),
    ('object', ['program-1']), ('both', ['program-1', 'program-1.timeline']),
    ('none', ['program-1'])])
def test_catalog_by_timestamp_mode(monkeypatch, mode, tracks):
    data, _ = gop_stream(2, lead=False)
    p = TsPackager()
    p.feed(data, 0)
    args = _pub(monkeypatch, '--ts', '-', '--msfts', '--msfts-timestamps',
                mode)
    cat = pub_media._MsftsRun(args, p, None).catalog()
    assert [t.name for t in cat.tracks] == tracks
    if len(tracks) == 2:
        t = cat.tracks[1].to_dict()
        assert t == {'name': 'program-1.timeline',
                     'packaging': 'mediatimeline', 'isLive': True,
                     'depends': ['program-1'],
                     'mimeType': 'application/json'}
    assert cat.validate() == ['program-1: video track requires bitrate']


@requires_certs
@pytest.mark.parametrize('mode,source', [
    ('timeline', 'media-timeline'), ('object', 'loc-timestamp'),
    ('both', 'loc-timestamp'), ('none', None)])
async def test_end_to_end_delay_by_timestamp_mode(monkeypatch, capsys,
                                                  tmp_path, mode, source):
    data, _ = gop_stream(10, frames=10, lead=False)
    await _through_relay(
        monkeypatch, _PORT + 10, True,
        ['--ts', _paced_fifo(tmp_path, data, 2), '--msfts',
         '--msfts-timestamps', mode],
        ['--analyze', '-i', '1', '-t', '30'])
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()
             if ln.startswith('{')]
    summary = lines[-1]
    assert summary['ts_packets'] > 0
    if source is None:
        assert summary['e2e_ms'] is None
    else:
        assert summary['e2e_ms']['source'] == source
        assert summary['e2e_ms']['samples'] > 0
        assert -1000 < summary['e2e_ms']['p50'] < 1000


# -- a PSI change: the receiver follows the next track ---------------------

@pytest.fixture
def catalogs(monkeypatch):
    """Every catalog sub_media applies, as dicts."""
    seen = []
    orig = MediaSubscriber._on_catalog_object

    def spy(self, msg, size, ts, group_id, subgroup_id):
        before = self.catalog
        orig(self, msg, size, ts, group_id, subgroup_id)
        if self.catalog is not None and self.catalog is not before:
            seen.append(self.catalog.to_dict())
    monkeypatch.setattr(MediaSubscriber, '_on_catalog_object', spy)
    return seen


_TWO = ((1, PMT_PID), (2, PMT_PID + 1))


def _tables(packets: bytes) -> bytes:
    """Packets with their continuity counters cleared: an unchanged
    table keeps the packets it first came in."""
    out = bytearray(packets)
    for i in range(3, len(out), P):
        out[i] &= 0xF0
    return bytes(out)


@requires_certs
@pytest.mark.parametrize('second,name,program,use_quic', [
    (((2, PMT_PID),), 'program-2', 2, True),
    (((2, PMT_PID),), 'program-2', 2, False),
    (_TWO, 'mux-2', None, True),
], ids=['program-number-quic', 'program-number-wt', 'pat-gains-a-program'])
async def test_the_receiver_follows_a_track_change(
        monkeypatch, capsys, tmp_path, catalogs, second, name, program,
        use_quic):
    ended = _relay_end_of_track(monkeypatch)
    data, marks = changing_stream([(5, ONE, VIDEO_PID),
                                   (5, second, VIDEO_PID)], frames=10)
    out = tmp_path / 'out.ts'
    rc = await _through_relay(
        monkeypatch, _PORT + 11 + (not use_quic) + 2 * (program is None),
        use_quic, ['--ts', _paced_fifo(tmp_path, data, 4), '--msfts'],
        ['--ts-out', str(out), '--analyze', '-i', '1', '-t', '30'])
    assert rc == 0
    assert {'program-1', 'program-1.timeline'} <= ended
    err = capsys.readouterr()
    assert f'switched to {name}' in err.err
    summary = [json.loads(ln) for ln in err.out.splitlines()
               if ln.startswith('{')][-1]
    assert summary['switches'] == 1 and summary['cc_errors_total'] == 0
    # The source up to the changed PAT, then from the next track's first
    # random access point: only the span between them is missing.
    got = out.read_bytes()
    (_, change), (start2, _) = marks
    tail = data[start2 * P:]
    assert got.endswith(tail)
    head = got[:-len(tail)]
    assert head and data[:change * P].endswith(head)
    first = [c for c in catalogs if c['tracks'][0]['name'] == 'program-1']
    last = catalogs[-1]
    assert first and [t['name'] for t in first[0]['tracks']] == [
        'program-1', 'program-1.timeline']
    assert first[0]['tracks'][0]['mpeg2tsProgramNumber'] == 1
    assert [t['name'] for t in last['tracks']] == [name, f'{name}.timeline']
    assert last['tracks'][0].get('mpeg2tsProgramNumber') == program
    # Its PAT and PMT: the changed ones.
    cat = Catalog.from_dict(last)
    assert _tables(cat.resolve_init(cat.tracks[0])) == _tables(
        data[change * P:(change + 2) * P])


@requires_certs
async def test_an_es_and_pcr_pid_change_keeps_the_track(
        monkeypatch, capsys, tmp_path, catalogs):
    data, marks = changing_stream([(5, ONE, VIDEO_PID),
                                   (5, ONE, VIDEO_PID + 0x10)], frames=10)
    out = tmp_path / 'out.ts'
    rc = await _through_relay(
        monkeypatch, _PORT + 14, True,
        ['--ts', _paced_fifo(tmp_path, data, 4), '--msfts'],
        ['--ts-out', str(out), '--ts-playout', '200', '--analyze', '-i', '1',
         '-t', '30'])
    assert rc == 0
    err = capsys.readouterr()
    assert 'switched' not in err.err
    summary = [json.loads(ln) for ln in err.out.splitlines()
               if ln.startswith('{')][-1]
    assert summary['switches'] == 0
    # The PMT moved the PCR; playout followed it to the end.
    assert summary['pcr']['pid'] == VIDEO_PID + 0x10
    assert summary['playout']['late'] == 0
    assert summary['playout']['breaks'] == 0
    got = out.read_bytes()
    assert got and data.endswith(got)
    # One track throughout, its declared fields as first published; the
    # initialization data follows the PMT.
    names = {t['name'] for c in catalogs for t in c['tracks']}
    assert names == {'program-1', 'program-1.timeline'}
    assert {c['tracks'][0]['mpeg2tsPcrPid'] for c in catalogs} == {VIDEO_PID}
    cat = Catalog.from_dict(catalogs[-1])
    (_, change), _ = marks
    assert _tables(cat.resolve_init(cat.tracks[0])) == _tables(
        data[change * P:(change + 2) * P])
