"""LOC property numbering: the loc-04 registry, the timestamp ids we
emit for older receivers, and the receive-side preference order.

LOC carries no version on the wire, so these are the only mechanism
keeping us readable across loc-01/02/04 deployments — assert them
directly rather than through a loopback round-trip.
"""
from types import SimpleNamespace

from aiomoqt.media import (
    LocTrackPublisher, LocTrackSubscriber,
    LOC_PROP_TIMESTAMP, LOC_PROP_TIMESCALE,
    LOC_PROP_VIDEO_CONFIG, LOC_PROP_AUDIO_CONFIG,
    LOC_PROP_VIDEO_FRAME_MARKING, LOC_PROP_AUDIO_LEVEL,
    LOC02_PROP_TIMESTAMP, LOC01_PROP_CAPTURE_TS,
)
from aiomoqt.media.loc import LocFrame

_AVCC = b"\x01\x64\x00\x1f\xff\xe1"
_ASC = b"\x12\x10"  # AAC-LC 44.1kHz stereo


def _pub(**kw):
    return LocTrackPublisher(None, "loc/ns", "track", **kw)


def _sub():
    return LocTrackSubscriber(None, "loc/ns", "track")


def _obj(exts, payload=b"f", object_id=0, group_id=0):
    return SimpleNamespace(extensions=exts, payload=payload,
                           object_id=object_id, group_id=group_id)


def _session(draft):
    return SimpleNamespace(negotiated_draft=draft)


def test_registry_matches_loc04():
    # draft-ietf-moq-loc-04 §6.1 Table 1. Parity carries the value
    # shape: even ids are a bare vi64, odd ids length-prefixed bytes.
    assert (LOC_PROP_TIMESCALE, LOC_PROP_VIDEO_FRAME_MARKING,
            LOC_PROP_AUDIO_LEVEL, LOC_PROP_VIDEO_CONFIG,
            LOC_PROP_AUDIO_CONFIG, LOC_PROP_TIMESTAMP) == (
        0x08, 0x09, 0x0C, 0x0D, 0x0F, 0x10)
    for vi64_id in (LOC_PROP_TIMESTAMP, LOC_PROP_TIMESCALE,
                    LOC_PROP_AUDIO_LEVEL):
        assert vi64_id % 2 == 0
    for bytes_id in (LOC_PROP_VIDEO_CONFIG, LOC_PROP_AUDIO_CONFIG,
                     LOC_PROP_VIDEO_FRAME_MARKING):
        assert bytes_id % 2 == 1


def test_timestamp_dual_emit_below_d18():
    exts = _pub()._object_extensions(LocFrame(b"f", timestamp=4242), False,
                                     _session(16))
    assert exts[LOC_PROP_TIMESTAMP] == 4242
    assert exts[LOC02_PROP_TIMESTAMP] == 4242
    assert LOC01_PROP_CAPTURE_TS not in exts


def test_timestamp_loc01_compat_adds_third_id_below_d18():
    exts = _pub(loc01_compat=True)._object_extensions(
        LocFrame(b"f", timestamp=4242), False, _session(16))
    assert all(exts[p] == 4242 for p in (LOC_PROP_TIMESTAMP,
                                         LOC02_PROP_TIMESTAMP,
                                         LOC01_PROP_CAPTURE_TS))


def test_d18_emits_only_the_loc04_timestamp_id():
    # MOQT d18 §15.8 gives 0x06 (SUBGROUP_DELIVERY_TIMEOUT) and 0x02
    # (OBJECT_DELIVERY_TIMEOUT) Track scope; an Object Property under
    # either is refused, so neither may be emitted at d18 even with
    # loc01_compat asked for.
    for pub in (_pub(), _pub(loc01_compat=True)):
        exts = pub._object_extensions(LocFrame(b"f", timestamp=4242), False,
                                      _session(18))
        assert exts[LOC_PROP_TIMESTAMP] == 4242
        assert LOC02_PROP_TIMESTAMP not in exts
        assert LOC01_PROP_CAPTURE_TS not in exts


def test_config_property_follows_media_kind():
    video = _pub(config=_AVCC)._object_extensions(
        LocFrame(b"f"), True, _session(16))
    assert video[LOC_PROP_VIDEO_CONFIG] == _AVCC
    assert LOC_PROP_AUDIO_CONFIG not in video

    audio = _pub(config=_ASC, media_kind="audio")._object_extensions(
        LocFrame(b"f"), True)
    assert audio[LOC_PROP_AUDIO_CONFIG] == _ASC
    assert LOC_PROP_VIDEO_CONFIG not in audio


def test_config_and_timescale_only_at_group_start():
    pub = _pub(config=_AVCC, timescale=90000)
    mid = pub._object_extensions(LocFrame(b"f"), False)
    assert LOC_PROP_VIDEO_CONFIG not in mid
    assert LOC_PROP_TIMESCALE not in mid


def test_receive_prefers_newest_timestamp_id():
    got = []
    sub = _sub()
    sub.on_frame = lambda f, g, o: got.append(f)
    sub._on_object(_obj({LOC_PROP_TIMESTAMP: 300,
                         LOC02_PROP_TIMESTAMP: 200,
                         LOC01_PROP_CAPTURE_TS: 100}), 1, 0, 0, 0)
    assert got[0].timestamp == 300


def test_receive_falls_back_through_older_ids():
    for exts, want in (({LOC02_PROP_TIMESTAMP: 200}, 200),
                       ({LOC01_PROP_CAPTURE_TS: 100}, 100),
                       ({}, None)):
        got = []
        sub = _sub()
        sub.on_frame = lambda f, g, o: got.append(f)
        sub._on_object(_obj(exts), 1, 0, 0, 0)
        assert got[0].timestamp == want


def test_loc01_audio_level_is_not_read_as_a_timestamp():
    # loc-01 assigned 6 to Audio Level; loc-02 registered 0x06 as
    # TIMESTAMP. A publisher sending 0x02 is on loc-01 numbering, so its
    # 0x06 is a level and must survive as an extension, not be consumed.
    got = []
    sub = _sub()
    sub.on_frame = lambda f, g, o: got.append(f)
    sub._on_object(_obj({LOC01_PROP_CAPTURE_TS: 5_000_000,
                         LOC02_PROP_TIMESTAMP: 42}), 1, 0, 0, 0)
    assert got[0].timestamp == 5_000_000
    assert got[0].extensions == {LOC02_PROP_TIMESTAMP: 42}


def test_receive_captures_audio_config():
    sub = _sub()
    sub._on_object(_obj({LOC_PROP_AUDIO_CONFIG: _ASC}), 1, 0, 0, 0)
    assert sub.config == _ASC


def test_consumed_properties_are_not_passed_through():
    got = []
    sub = _sub()
    sub.on_frame = lambda f, g, o: got.append(f)
    sub._on_object(_obj({LOC_PROP_TIMESTAMP: 300,
                         LOC02_PROP_TIMESTAMP: 300,
                         LOC01_PROP_CAPTURE_TS: 300,
                         LOC_PROP_TIMESCALE: 90000,
                         LOC_PROP_VIDEO_CONFIG: _AVCC,
                         LOC_PROP_AUDIO_LEVEL: 7}), 1, 0, 0, 0)
    assert got[0].extensions == {LOC_PROP_AUDIO_LEVEL: 7}
    assert sub.timescale == 90000
