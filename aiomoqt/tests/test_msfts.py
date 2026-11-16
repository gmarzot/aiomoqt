"""MSFTS (draft-gregoire-moq-msfts-02): object and catalog vectors, valid
and malformed; the publisher's packager against a stream built in-test
from ISO 13818-1; the subscriber's reordering; and two mpeg2ts tracks on
one session through the relay over raw QUIC and WebTransport."""
import asyncio
import base64
import json
import shutil
import struct
import subprocess
import time

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.media import (
    Catalog, CatalogTrack, InitData, MediaPublisher, MediaSubscriber,
)
from aiomoqt.media import msfts
from aiomoqt.media.msfts import (
    MsftsError, MsftsTrack, MsftsTrackPublisher, MsftsTrackSubscriber,
    PacketOrder, TsPackager, check_payload,
)
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tools import moq_interop_relay as relay

P = 188
PMT_PID = 0x1000
VIDEO_PID = 0x100
AUDIO_PID = 0x101
SDT_PID = 0x11
NULL_PID = 0x1FFF


# -- reference muxer, from ISO 13818-1 §2.4.3 and §2.4.4 ------------------

def ts(pid, payload=b'', *, pusi=False, cc=0, rai=False, pcr=None,
       disc=False):
    """One TS packet. An adaptation field carries the flags and PCR and
    stuffs a short payload."""
    flags = (0x80 if disc else 0) | (0x40 if rai else 0) | (
        0x10 if pcr is not None else 0)
    af = b''
    if flags or len(payload) < 184:
        body = b''
        if flags or len(payload) < 183:
            body = bytes([flags])
            if pcr is not None:
                base, ext = divmod(pcr, 300)
                body += bytes([(base >> 25) & 0xFF, (base >> 17) & 0xFF,
                               (base >> 9) & 0xFF, (base >> 1) & 0xFF,
                               ((base & 1) << 7) | 0x7E | (ext >> 8),
                               ext & 0xFF])
        af_len = 183 - len(payload)
        assert af_len >= len(body)
        af = bytes([af_len]) + body + b'\xff' * (af_len - len(body))
    afc = (0x20 if af else 0) | (0x10 if payload else 0)
    pkt = bytes([0x47, (0x40 if pusi else 0) | (pid >> 8), pid & 0xFF,
                 afc | (cc & 0xF)]) + af + payload
    assert len(pkt) == P
    return pkt


def section(table_id, ext, body, version=0):
    inner = struct.pack('>HBBB', ext, 0xC1 | (version << 1), 0, 0) + body
    head = bytes([table_id]) + struct.pack('>H', 0xB000 | (len(inner) + 4))
    return head + inner + b'\x00\x00\x00\x00'


def psi(pid, sec, cc=0):
    data = b'\x00' + sec
    return ts(pid, data + b'\xff' * (184 - len(data)), pusi=True, cc=cc)


def pat(programs=((1, PMT_PID),), cc=0, version=0):
    body = b''.join(struct.pack('>HH', n, 0xE000 | p) for n, p in programs)
    return psi(0, section(0x00, 1, body, version), cc)


def pmt(streams=((0x02, VIDEO_PID), (0x03, AUDIO_PID)), pcr_pid=VIDEO_PID,
        cc=0, version=0, pid=PMT_PID, program=1):
    body = struct.pack('>HH', 0xE000 | pcr_pid, 0xF000)
    for stype, spid in streams:
        body += bytes([stype]) + struct.pack('>HH', 0xE000 | spid, 0xF000)
    return psi(pid, section(0x02, program, body, version), cc)


def sdt(cc=0):
    return psi(SDT_PID, section(0x42, 1, b'\xff\x01\xff'), cc)


def null():
    return ts(NULL_PID, b'\xff' * 184)


def pes_head(stream_id, pts, length=0):
    b = bytes([0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF,
               0x01 | ((pts >> 14) & 0xFE), (pts >> 7) & 0xFF,
               0x01 | ((pts << 1) & 0xFE)])
    return (b'\x00\x00\x01' + bytes([stream_id]) + struct.pack('>H', length)
            + bytes([0x80, 0x80, 5]) + b)


class Mux:
    """Packets with per-PID continuity counters."""

    def __init__(self):
        self.cc = {}
        self.out = []

    def put(self, pid, chunks, **kw):
        first = True
        for chunk in chunks:
            cc = self.cc.get(pid, 0)
            self.out.append(ts(pid, chunk, pusi=first and kw.get('pusi', True),
                               cc=cc, **{k: v for k, v in kw.items()
                                         if k != 'pusi' and first}))
            if chunk:   # an adaptation-only packet keeps the counter
                self.cc[pid] = (cc + 1) & 0xF
            first = False

    def raw(self, pkt):
        self.out.append(pkt)

    def pes(self, pid, es, pts, *, key=False, rai=None, pcr=None,
            stream_id=0xE0):
        data = pes_head(stream_id, pts) + es
        chunks = [data[i:i + 184] for i in range(0, len(data), 184)]
        if rai or pcr is not None:
            # The first packet carries the adaptation field.
            room = 184 - 2 - (6 if pcr is not None else 0)
            chunks = [data[:room]] + [data[i:i + 184]
                                      for i in range(room, len(data), 184)]
        self.put(pid, chunks, rai=bool(rai), pcr=pcr)

    def data(self):
        return b''.join(self.out)


MPEG2_I = b'\x00\x00\x01\xb3' + b'\x14' * 8 + b'\x00\x00\x01\x00\x00\x0f'
MPEG2_P = b'\x00\x00\x01\x00\x00\x17'


def gop_stream(gops=3, frames=6, *, rai=True, lead=True, video=MPEG2_I,
               other=MPEG2_P, streams=((0x02, VIDEO_PID), (0x03, AUDIO_PID)),
               programs=((1, PMT_PID),), size=900):
    """A CBR-like stream: each GOP opens with SDT, PAT, PMT, a null and a
    PCR-only packet, then a key frame; audio and nulls between frames.
    Returns (bytes, packet index of each GOP's SDT, of its key frame)."""
    m = Mux()
    if lead:
        m.pes(VIDEO_PID, other + b'\x55' * size, 0)      # before any PSI
        m.raw(null())
    starts = []
    raps = []
    pts = 90_000
    for g in range(gops):
        starts.append(len(m.out))
        m.raw(sdt(m.cc.get(SDT_PID, 0)))
        m.cc[SDT_PID] = (m.cc.get(SDT_PID, 0) + 1) & 0xF
        m.raw(pat(programs, m.cc.get(0, 0)))
        m.cc[0] = (m.cc.get(0, 0) + 1) & 0xF
        for n, pmt_pid in programs:
            m.raw(pmt(streams, cc=m.cc.get(pmt_pid, 0), pid=pmt_pid,
                      program=n))
            m.cc[pmt_pid] = (m.cc.get(pmt_pid, 0) + 1) & 0xF
        m.raw(null())
        m.put(VIDEO_PID, [b''], pcr=pts * 300, pusi=False)  # PCR only
        raps.append(len(m.out))
        for f in range(frames):
            es = (video if f == 0 else other) + bytes([g, f]) * (size // 2)
            m.pes(VIDEO_PID, es, pts, rai=rai and f == 0,
                  pcr=pts * 300 if f == 0 else None)
            m.pes(AUDIO_PID, b'\xff\xfd' + bytes([f]) * 300, pts,
                  stream_id=0xC0)
            m.raw(null())
            pts += 3600
    return m.data(), (starts, raps)


def objects_of(data, groups, n):
    """The objects unmodified carriage makes: from each group start, n
    packets or through the key frame's first packet when that is longer,
    then runs of n packets, the last short."""
    starts, raps = groups
    out = []
    bounds = [s * P for s in starts] + [len(data)]
    for a, b, rap in zip(bounds, bounds[1:], raps):
        i = a
        size = max(n * P, (rap + 1) * P - a)
        while i < b:
            out.append((data[i:min(i + size, b)], i == a))
            i += size
            size = n * P
    return out


def pack(data, n=7, step=1316, packager=None):
    p = packager or TsPackager(n)
    out = []
    for i in range(0, len(data), step):
        out += p.feed(data[i:i + step], i)
    return p, out + p.close()


# -- object payload (§5.1) ------------------------------------------------

def test_whole_packets_pass():
    check_payload(null() * 7)
    check_payload(null())
    m2ts = b'\x12\x34\x56\x78' + null()
    check_payload(m2ts * 3, 192)


@pytest.mark.parametrize('payload,size,why', [
    (b'', 188, 'whole number'),
    (null() * 7 + b'\x47', 188, 'whole number'),
    (null()[:-1], 188, 'whole number'),
    (null() * 2 + b'\x00' + null()[1:], 188, 'packet 2 lacks'),
    (null() * 2, 192, 'whole number'),
    ((b'\x00' * 4 + null()) * 2, 188, 'whole number'),
    (b'\x47' + b'\x00' * 191, 192, 'packet 0 lacks'),
])
def test_malformed_objects_are_refused(payload, size, why):
    with pytest.raises(MsftsError, match=why):
        check_payload(payload, size)


# -- catalog (§6, §7) -----------------------------------------------------

# draft-gregoire-moq-msfts-02 §7.1, verbatim.
DRAFT_7_1 = '''{
  "version": "draft-01",
  "generatedAt": 1746104606044,
  "tracks": [
    {
      "name": "program-1-ts",
      "namespace": "live.example.com/channel/1",
      "packaging": "mpeg2ts",
      "mpeg2tsMode": "unmodified-program",
      "isLive": true,
      "targetLatency": 1000,
      "role": "video",
      "mimeType": "video/mp2t",
      "bitrate": 6000000,
      "mpeg2tsPacketSize": 188,
      "mpeg2tsProgramNumber": 1,
      "mpeg2tsPcrPid": 257,
      "mpeg2tsRandomAccess": true
    }
  ]
}'''

_TRACK = {"name": "t", "packaging": "mpeg2ts", "isLive": True,
          "mimeType": "video/mp2t", "mpeg2tsPacketSize": 188}

# The MSFTS fields of the §7.2, §7.3, §7.5 and §7.7 track objects.
DRAFT_TRACKS = [
    {**_TRACK, "mpeg2tsMode": "unmodified-program", "role": "video",
     "bitrate": 12000000, "mpeg2tsPacketSize": 192,
     "mpeg2tsProgramNumber": 1, "mpeg2tsTimestampMode": "arrival-time",
     "mpeg2tsRandomAccess": True},
    {**_TRACK, "mpeg2tsMode": "per-program", "role": "video",
     "bitrate": 6000000, "mpeg2tsProgramNumber": 1, "mpeg2tsPcrPid": 257,
     "mpeg2tsMuxRate": 6500000, "mpeg2tsRandomAccess": True},
    {**_TRACK, "mpeg2tsMode": "unmodified-multiplex", "bitrate": 20000000},
    {**_TRACK, "mpeg2tsMode": "es-packets", "role": "audio",
     "bitrate": 128000, "mpeg2tsProgramNumber": 1, "mpeg2tsPcrPid": 257,
     "mpeg2tsMuxRate": 6000000, "mpeg2tsEsPid": 258,
     "mpeg2tsRandomAccess": True},
    {**_TRACK, "mpeg2tsMode": "es-packets", "role": "pat",
     "mpeg2tsProgramNumber": 1, "mpeg2tsMuxRate": 6000000,
     "mpeg2tsEsPid": 0},
]


def _psi_init():
    return pat() + pmt()


# moq2ts MsftsMuxer::catalogJson for `--unmodified` on a live pipe: Qt
# sorts each object's keys; the initDataList follows the tracks.
MOQ2TS = ('{"generatedAt":1763300000000,"tracks":[{"accessibleWindowTime":'
          '300000,"initRef":"init-program-1","isLive":true,"mimeType":'
          '"video/mp2t","mpeg2tsMode":"unmodified-program",'
          '"mpeg2tsPacketSize":188,"mpeg2tsPcrPid":256,'
          '"mpeg2tsProgramNumber":1,"mpeg2tsRandomAccess":true,"name":'
          '"program-1","packaging":"mpeg2ts","role":"video","targetLatency":'
          '1000},{"depends":["program-1"],"isLive":true,"mimeType":'
          '"application/json","name":"program-1.timeline","packaging":'
          '"mediatimeline"}],"version":"draft-01","initDataList":[{"id":'
          '"init-program-1","type":"inline","data":"'
          + base64.b64encode(_psi_init()).decode() + '"}]}')


def test_draft_example_parses_clean():
    cat = Catalog.from_json(DRAFT_7_1)
    t = cat.tracks[0]
    info = MsftsTrack.from_entry(t)
    assert (info.mode, info.packet_size, info.program_number, info.pcr_pid,
            info.random_access) == ('unmodified-program', 188, 1, 257, True)
    assert cat.validate() == []
    assert json.loads(cat.to_json()) == json.loads(DRAFT_7_1)


@pytest.mark.parametrize('track', DRAFT_TRACKS,
                         ids=['7.2', '7.3', '7.5', '7.7-audio', '7.7-pat'])
def test_draft_track_objects_are_valid(track):
    t = CatalogTrack.from_dict(track)
    MsftsTrack.from_entry(t)
    assert msfts.track_issues(t) == []


def test_moq2ts_catalog_parses_and_resolves_its_psi():
    cat = Catalog.from_json(MOQ2TS)
    t = cat.find('program-1')
    info = MsftsTrack.from_entry(t)
    assert info.mode == 'unmodified-program' and info.pcr_pid == 256
    init = cat.resolve_init(t)
    assert init == _psi_init()
    check_payload(init)
    assert cat.validate() == ['program-1: video track requires bitrate']


def test_our_catalog_has_moq2ts_fields():
    data, _ = gop_stream(1)
    p, _ = pack(data)
    entry = p.catalog_track('program-1', bitrate=6_000_000,
                            target_latency=1000)
    ours = entry.to_dict()
    theirs = Catalog.from_json(MOQ2TS).find('program-1').to_dict()
    theirs.pop('accessibleWindowTime')
    assert ours.pop('bitrate') == 6_000_000
    assert ours == theirs


_BAD = [
    ({"mpeg2tsMode": "transparent"}, "mpeg2tsMode"),
    ({"mpeg2tsMode": None}, "mpeg2tsMode"),
    ({"mpeg2tsPacketSize": 190}, "mpeg2tsPacketSize"),
    ({"mpeg2tsPacketSize": "188"}, "mpeg2tsPacketSize"),
    ({"mpeg2tsPacketSize": True}, "mpeg2tsPacketSize"),
    ({"mpeg2tsPacketSize": None}, "mpeg2tsPacketSize"),
    ({"mpeg2tsMode": "es-packets"}, "without mpeg2tsEsPid"),
    ({"mpeg2tsEsPid": 256}, "mpeg2tsEsPid on"),
    ({"mpeg2tsPcrPid": 0x2000}, "mpeg2tsPcrPid"),
    ({"mpeg2tsPcrPid": -1}, "mpeg2tsPcrPid"),
    ({"mpeg2tsScte35Pid": "500"}, "mpeg2tsScte35Pid"),
    ({"mpeg2tsProgramNumber": 0}, "mpeg2tsProgramNumber"),
    ({"mpeg2tsProgramNumber": 65536}, "mpeg2tsProgramNumber"),
    ({"mpeg2tsMuxRate": 0}, "mpeg2tsMuxRate"),
    ({"mpeg2tsMode": "per-program", "mpeg2tsMuxRate": "fast"},
     "mpeg2tsMuxRate"),
    ({"mpeg2tsSiPids": [17, 17]}, "mpeg2tsSiPids"),
    ({"mpeg2tsSiPids": 17}, "mpeg2tsSiPids"),
    ({"mpeg2tsRandomAccess": "yes"}, "mpeg2tsRandomAccess"),
    ({"mpeg2tsMode": "unmodified-program", "mpeg2tsPacketSize": 192,
      "mpeg2tsTimestampMode": 1}, "mpeg2tsTimestampMode"),
    ({"packaging": "loc"}, "packaging"),
]


@pytest.mark.parametrize('fields,why', _BAD)
def test_malformed_track_is_rejected(fields, why):
    d = {**_TRACK, "mpeg2tsMode": "unmodified-program", **fields}
    d = {k: v for k, v in d.items() if v is not None}
    t = CatalogTrack.from_dict(d)
    with pytest.raises(MsftsError, match=why):
        MsftsTrack.from_entry(t)
    assert any(why in i for i in msfts.track_issues(t))


@pytest.mark.parametrize('fields,why', [
    ({"mpeg2tsTimestampMode": "opaque"}, "188-octet"),
    ({"mpeg2tsMuxRate": 6000000}, "mpeg2tsMuxRate on"),
    ({"mpeg2tsMode": "unmodified-multiplex", "mpeg2tsSiPids": [17]},
     "mpeg2tsSiPids on"),
    ({"mpeg2tsMode": "unmodified-multiplex", "mpeg2tsRandomAccess": True},
     "no reference program"),
    ({"mpeg2tsMode": "unmodified-multiplex", "altGroup": 1}, "altGroup"),
])
def test_publisher_rule_breaks_are_reported_not_rejected(fields, why):
    t = CatalogTrack.from_dict({**_TRACK, "mpeg2tsMode": "unmodified-program",
                                **fields})
    MsftsTrack.from_entry(t)
    issues = msfts.track_issues(t)
    assert len(issues) == 1 and why in issues[0]


@pytest.mark.parametrize('data,why', [
    (b'\x47' * 100, 'whole number'),
    (null() + b'\x00' * 188, 'packet 1 lacks'),
])
def test_init_data_must_be_whole_packets(data, why):
    t = CatalogTrack.from_dict({**_TRACK, "mpeg2tsMode": "unmodified-program",
                                "initRef": "i", "bitrate": 1})
    cat = Catalog(tracks=[t], initDataList=[InitData.from_bytes('i', data)])
    assert any(why in i for i in cat.validate())
    cat.initDataList = [InitData(id='i', data='not base64!')]
    assert any('initialization data' in i for i in cat.validate())


# -- packager ---------------------------------------------------------------

@pytest.mark.parametrize('n', [1, 7, 20])
@pytest.mark.parametrize('step', [1316, 188 * 3 + 50, 65536])
def test_objects_are_runs_of_packets_from_each_group_start(n, step):
    data, groups = gop_stream(4)
    p, objs = pack(data, n, step)
    want = objects_of(data, groups, n)
    assert [(o.payload, o.key) for o in objs] == want
    assert p.groups == 4
    # The lead-in before the first SDT is dropped, nothing else.
    assert p.dropped == groups[0][0]
    assert data.endswith(b''.join(o.payload for o in objs))


def test_each_group_opens_with_its_psi_run_then_the_key_frame():
    data, starts = gop_stream(3)
    _, objs = pack(data)
    for o in objs:
        if o.key:
            pids = [((o.payload[i + 1] & 0x1F) << 8) | o.payload[i + 2]
                    for i in range(0, len(o.payload), P)]
            assert pids == [SDT_PID, 0, PMT_PID, NULL_PID, VIDEO_PID,
                            VIDEO_PID, VIDEO_PID]


@pytest.mark.parametrize('stype,key,other', [
    (0x02, MPEG2_I, MPEG2_P),
    (0x02, b'\x00\x00\x01\xb8' + b'\x00' * 4, MPEG2_P),
    (0x1B, b'\x00\x00\x00\x01\x09\xf0\x00\x00\x00\x01\x67\x64',
     b'\x00\x00\x00\x01\x09\xf0\x00\x00\x00\x01\x41\x9a'),
    (0x1B, b'\x00\x00\x01\x65\x88', b'\x00\x00\x01\x41\x9a'),
    (0x24, b'\x00\x00\x00\x01\x40\x01', b'\x00\x00\x00\x01\x02\x01'),
    (0x24, b'\x00\x00\x01\x26\x01', b'\x00\x00\x01\x02\x01'),
], ids=['mpeg2-seq', 'mpeg2-gop', 'h264-sps', 'h264-idr', 'h265-vps',
        'h265-idr'])
def test_key_frames_are_found_without_the_indicator(stype, key, other):
    data, groups = gop_stream(3, rai=False, video=key, other=other,
                              streams=((stype, VIDEO_PID),
                                       (0x0F, AUDIO_PID)))
    p, objs = pack(data)
    assert p.groups == 3
    assert [(o.payload, o.key) for o in objs] == objects_of(data, groups, 7)


def test_a_frame_without_indicator_or_key_syntax_opens_no_group():
    data, _ = gop_stream(3, rai=False, video=MPEG2_P)
    p, objs = pack(data)
    assert p.groups == 0 and objs == []
    assert p.ready


def test_a_slow_feed_flushes_a_partial_object_on_time():
    data, _ = gop_stream(1, lead=False)
    p = TsPackager(7, flush_s=0.005)
    head = data[:10 * P]
    objs = p.feed(head, 1_000)
    assert [len(o.payload) // P for o in objs] == [7]
    assert p.flush_due() == 1_000 + 5_000
    late = p.flush(7_000)
    assert [(len(o.payload) // P, o.key, o.at_us) for o in late] == [
        (3, False, 1_000)]
    assert p.flush_due() is None
    more = p.feed(data[10 * P:12 * P], 9_000)
    assert more == [] and p.flush_due() == 14_000


def test_a_flush_leaves_the_psi_run_for_its_group():
    data, groups = gop_stream(3, lead=False)
    p = TsPackager(7)
    objs = []
    t = 0
    for i in range(0, len(data), 7 * P):     # a burst every 20 ms
        t += 20_000
        objs += p.feed(data[i:i + 7 * P], t)
        due = p.flush_due()
        if due is not None:
            objs += p.flush(due)
    objs += p.close()
    assert sum(o.key for o in objs) == 3
    assert b''.join(o.payload for o in objs) == data
    heads = [o.payload for o in objs if o.key]
    starts, raps = groups
    for head, start, rap in zip(heads, starts, raps):
        assert head.startswith(data[start * P:(rap + 1) * P])
    assert all(len(o.payload) <= 7 * P for o in objs)


def test_elementary_data_after_the_psi_ends_the_run():
    m = Mux()
    m.raw(pat())
    m.raw(pmt())
    m.pes(VIDEO_PID, MPEG2_I + b'\x01' * 300, 1000, rai=True)
    m.pes(VIDEO_PID, MPEG2_P + b'\x02' * 300, 4600)
    m.raw(pat(cc=1))
    m.pes(AUDIO_PID, b'\xff\xfd' + b'\x03' * 100, 4600, stream_id=0xC0)
    gop = len(m.out)
    m.pes(VIDEO_PID, MPEG2_I + b'\x04' * 300, 8200, rai=True)
    data = m.data()
    _, objs = pack(data)
    heads = [o.payload for o in objs if o.key]
    assert len(heads) == 2 and heads[1].startswith(data[gop * P:(gop + 1) * P])


def test_object_stamp_is_its_first_packet_arrival():
    data, _ = gop_stream(1, lead=False)
    p = TsPackager(7)
    a = p.feed(data[:5 * P], 100)
    b = p.feed(data[5 * P:19 * P], 200)
    assert a == [] and [o.at_us for o in b] == [100, 200]


def test_bytes_between_packets_are_skipped():
    data, (starts, _) = gop_stream(2)
    cut = starts[1] * P + 3 * P
    dirty = data[:cut] + b'\x00\x47\x12' + data[cut:]
    p, objs = pack(dirty)
    assert p.resyncs == 3
    assert b''.join(o.payload for o in objs) == data[starts[0] * P:]


def test_psi_and_mux_rate_for_the_catalog():
    data, _ = gop_stream(1, lead=False)
    p, _ = pack(data[:6 * P])
    assert p.ready and p.mode == 'unmodified-program'
    assert (p.program_number, p.pcr_pid, p.rap_pid) == (1, VIDEO_PID,
                                                        VIDEO_PID)
    assert p.streams == [(0x02, VIDEO_PID), (0x03, AUDIO_PID)]
    assert p.init_data == data[P:3 * P]
    assert not p.psi_changed
    m = Mux()
    m.raw(pmt(((0x02, VIDEO_PID),), version=1))
    p.feed(m.data(), 0)
    assert p.psi_changed and p.init_data == data[P:2 * P] + m.data()


def test_mux_rate_is_measured_between_pcrs_a_second_apart():
    p = TsPackager()
    m = Mux()
    m.raw(pat())
    m.raw(pmt())
    m.put(VIDEO_PID, [b''], pcr=0, pusi=False)
    for _ in range(999):
        m.raw(null())
    m.put(VIDEO_PID, [b''], pcr=27_000_000, pusi=False)
    p.feed(m.data(), 0)
    assert p.bitrate == 1000 * P * 8 and p.bitrate_changed


def test_a_multiplex_names_no_reference_program():
    progs = ((1, PMT_PID), (2, PMT_PID + 1))
    data, groups = gop_stream(2, programs=progs)
    starts = groups[0]
    p, objs = pack(data)
    assert p.mode == 'unmodified-multiplex' and p.groups == 2
    d = p.catalog_track('mux-1').to_dict()
    for key in ('mpeg2tsProgramNumber', 'mpeg2tsPcrPid',
                'mpeg2tsRandomAccess', 'role'):
        assert key not in d
    assert d['mpeg2tsMode'] == 'unmodified-multiplex'
    assert d['initRef'] == 'init-mux-1'
    assert p.init_data == data[(starts[0] + 1) * P:(starts[0] + 3) * P]
    assert [(o.payload, o.key) for o in objs] == objects_of(data, groups, 7)


def test_long_psi_run_makes_object_0_longer():
    data, groups = gop_stream(2, programs=tuple(
        (n, PMT_PID + n) for n in range(1, 10)))
    _, objs = pack(data)
    first = [o for o in objs if o.key]
    assert len(first[0].payload) // P == 9 + 5
    assert [(o.payload, o.key) for o in objs] == objects_of(data, groups, 7)


def test_audio_only_groups_a_second_apart():
    m = Mux()
    m.raw(pat())
    m.raw(pmt(((0x03, AUDIO_PID),), pcr_pid=AUDIO_PID))
    starts = []
    for k in range(100):                      # 24 ms frames, 2.4 s
        pts = 1000 + k * 2160
        if k in (0, 42, 84):
            starts.append(len(m.out))
        m.pes(AUDIO_PID, b'\xff\xfd' + b'\x11' * 400, pts, stream_id=0xC0)
    data = m.data()
    p, objs = pack(data)
    assert p.catalog_track('program-1').role == 'audio'
    assert p.groups == 3
    keys = [i for i, o in enumerate(objs) if o.key]
    assert len(keys) == 3
    # PSI before the first frame leads the first group.
    assert b''.join(o.payload for o in objs) == data


def _ffmpeg_mpeg2(path, seconds=3):
    """The target tracks' shape at low resolution: MPEG-2 video and mp2
    in a CBR mux with null padding, PCR every 20 ms, PAT/PMT every
    100 ms, a key frame every second."""
    if shutil.which('ffmpeg') is None:
        pytest.skip('ffmpeg not installed')
    subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
         '-i', f'testsrc2=size=320x180:rate=25:duration={seconds}', '-f',
         'lavfi', '-i', f'sine=f=1000:sample_rate=48000:duration={seconds}',
         '-c:v', 'mpeg2video', '-b:v', '1500k', '-maxrate', '1500k',
         '-minrate', '1500k', '-bufsize', '1000k', '-g', '25', '-bf', '0',
         '-c:a', 'mp2', '-b:a', '192k', '-muxrate', '2000000',
         '-mpegts_service_id', '1', '-pat_period', '0.1', '-pcr_period', '20',
         '-f', 'mpegts', str(path)], check=True)
    return path.read_bytes()


def test_ffmpeg_mpeg2_cbr_stream(tmp_path):
    data = _ffmpeg_mpeg2(tmp_path / 'cbr.ts')
    p, objs = pack(data)
    assert p.groups == 3 and p.dropped == 0
    assert b''.join(o.payload for o in objs) == data
    assert p.bitrate == 2_000_000
    assert p.streams[0][0] == 0x02 and p.rap_pid == p.streams[0][1]
    nulls = sum(1 for i in range(0, len(data), P)
                if data[i + 1] & 0x1F == 0x1F and data[i + 2] == 0xFF)
    assert nulls > 0
    for o in objs:
        if o.key:
            assert b'\x00\x00\x01\xb3' in o.payload
            assert o.payload[1:3] in (b'\x40\x00', b'\x40\x11')


# -- subscriber order ---------------------------------------------------

def test_order_releases_in_group_and_object_order():
    o = PacketOrder(hold_s=0.5)
    assert o.push(0, 0, 'a', 0) == [(0, 0, 'a')]
    assert o.push(1, 0, 'c', 0.01) == []            # next group waits
    assert o.push(0, 1, 'b', 0.02) == [(0, 1, 'b')]
    assert o.deadline == 0.52                       # progress restarts it
    assert o.end_group(0, 0.03) == [(1, 0, 'c')]
    assert o.deadline is None
    assert o.push(1, 1, 'd', 0.04) == [(1, 1, 'd')]
    assert o.push(0, 2, 'x', 0.05) == [] and o.late == 1
    assert o.end_group(1, 0.06) == []
    assert o.push(3, 0, 'e', 0.07) == [(3, 0, 'e')]  # a group may be skipped


def test_order_skips_an_open_group_after_the_hold():
    o = PacketOrder(hold_s=0.5)
    o.push(5, 0, 'a', 0)
    assert o.push(6, 0, 'b', 1.0) == [] and o.deadline == 1.5
    assert o.timeout(1.4) == []
    assert o.timeout(1.5) == [(6, 0, 'b')] and o.skipped == 1
    assert o.push(5, 1, 'late', 1.6) == [] and o.late == 1


def test_order_holds_a_gap_inside_a_group():
    o = PacketOrder(hold_s=0.5)
    o.push(0, 0, 'a', 0)
    assert o.push(0, 2, 'c', 0.1) == []
    assert o.push(0, 1, 'b', 0.2) == [(0, 1, 'b'), (0, 2, 'c')]
    assert o.push(0, 5, 'f', 0.3) == []
    assert o.timeout(0.8) == [(0, 5, 'f')]


def test_order_end_before_the_group_starts_and_rejected_objects():
    o = PacketOrder()
    o.push(0, 0, 'a', 0)
    assert o.end_group(1, 0.1) == []        # group 1 ended before it began
    o.push(2, 0, 'c', 0.2)
    assert o.push(1, 0, None, 0.3) == []    # rejected: held as a gap
    assert o.end_group(0, 0.4) == [(1, 0, None), (2, 0, 'c')]


def test_order_starts_mid_group_and_drains_in_order():
    o = PacketOrder()
    assert o.push(4, 3, 'x', 0) == [(4, 3, 'x')]
    o.push(5, 1, 'b', 0)
    o.push(5, 0, 'a', 0)
    o.push(4, 9, 'y', 0)
    assert o.drain() == [(4, 9, 'y'), (5, 0, 'a'), (5, 1, 'b')]
    assert o.held == 0 and o.deadline is None


# -- two tracks on one session, through the relay -------------------------

_PORT = 32000


async def _serve(port, use_quic):
    relay._announced.clear()
    relay._tracks.clear()
    srv = relay._build_server('localhost', port, CERT, KEY,
                              use_quic=use_quic, draft=18)
    for _ in range(40):
        try:
            return srv, await srv.serve()
        except OSError:
            await asyncio.sleep(0.05)
    return srv, await srv.serve()


def _client(port, use_quic):
    return MOQTClient('localhost', port, path='/', use_quic=use_quic,
                      verify_tls=False, supported_drafts=[18])


@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_two_mpeg2ts_tracks_on_one_session(use_quic):
    port = _PORT + (not use_quic)
    ns = f'msfts/two/{port}'
    streams = {}
    catalog = Catalog(generatedAt=0, tracks=[], initDataList=[])
    for name, frames in (('program-1', 6), ('program-2', 9)):
        data, (starts, _) = gop_stream(4, frames=frames)
        p, objs = pack(data)
        streams[name] = (data, starts, objs)
        entry = p.catalog_track(name, bitrate=1_000_000)
        catalog.tracks.append(entry)
        catalog.initDataList.append(InitData.from_bytes(entry.initRef,
                                                        p.init_data))
    assert catalog.validate() == []
    got = {name: [] for name in streams}
    srv, handle = await _serve(port, use_quic)
    try:
        async with _client(port, use_quic).connect() as pub_s, \
                _client(port, use_quic).connect() as sub_s:
            await pub_s.client_session_init()
            await sub_s.client_session_init()
            pub = MediaPublisher(pub_s, ns, catalog)
            tracks = {n: pub.add_track(MsftsTrackPublisher(pub_s, ns, n))
                      for n in streams}
            await pub.start(announce_namespace=True, publish_track=False)
            sub = MediaSubscriber(
                sub_s, ns, on_frame=lambda n, f, g, o: got[n].append(
                    (g, o, f.payload)))
            await sub.start(timeout=10)
            assert set(sub.tracks) == set(streams)
            assert all(isinstance(t, MsftsTrackSubscriber)
                       for t in sub.tracks.values())
            for name, track in tracks.items():
                deadline = time.monotonic() + 5
                while track.demand[0] == 0:
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.02)
            for k in range(max(len(s[2]) for s in streams.values())):
                for name, (_, _, objs) in streams.items():
                    if k < len(objs):
                        await tracks[name].send_frame(
                            objs[k].payload, key_frame=objs[k].key,
                            timestamp=objs[k].at_us)
                await asyncio.sleep(0)
            for track in tracks.values():
                await track.finish()
            deadline = time.monotonic() + 10
            while any(len(got[n]) < len(streams[n][2]) for n in streams):
                assert time.monotonic() < deadline, {
                    n: len(v) for n, v in got.items()}
                await asyncio.sleep(0.05)
    finally:
        await srv.close_sessions()
        handle.close()
    for name, (data, starts, objs) in streams.items():
        rx = got[name]
        assert rx, name
        assert [(g, o) for g, o, _ in rx] == sorted((g, o) for g, o, _ in rx)
        assert b''.join(p for _, _, p in rx) == data[starts[0] * P:], name
        assert sum(1 for _, o, _ in rx if o == 0) == 4
