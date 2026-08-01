"""MSF catalog model — round-trip, delta engine, validation.

Fixture JSON is lifted from draft-ietf-moq-msf-01 §5.6 examples.
"""
import json

import pytest

from aiomoqt.media import (
    Catalog, CatalogTrack, DeltaOp, InitData, CatalogError,
)

_AV_CATALOG = {
    "version": "1",
    "generatedAt": 1746104606044,
    "tracks": [
        {
            "name": "video", "packaging": "loc", "isLive": True,
            "role": "video", "renderGroup": 1,
            "codec": "avc3.42E01E", "width": 1280, "height": 720,
            "framerate": 30, "bitrate": 1500000,
        },
        {
            "name": "audio", "packaging": "loc", "isLive": True,
            "role": "audio", "renderGroup": 1,
            "codec": "opus", "samplerate": 48000,
            "channelConfig": "2", "bitrate": 32000,
            "buffers": {"target": 2000},
        },
    ],
}

# §5.6.4 — delta adding a declared track and cloning another.
_DELTA_ADD_CLONE = {
    "generatedAt": 1746104606050,
    "deltaUpdate": [
        {"op": "add", "tracks": [
            {"name": "slides", "isLive": True, "role": "video",
             "codec": "av01.0.08M.10.0.110.09", "width": 1920,
             "height": 1080, "framerate": 15, "bitrate": 750000,
             "renderGroup": 1, "packaging": "loc"},
        ]},
        {"op": "clone", "tracks": [
            {"parentName": "video", "name": "video-540",
             "width": 960, "height": 540, "bitrate": 600000},
        ]},
    ],
}

# §5.6.5 — delta removing tracks.
_DELTA_REMOVE = {
    "generatedAt": 1746104606060,
    "deltaUpdate": [
        {"op": "remove", "tracks": [{"name": "video"}, {"name": "slides"}]},
    ],
}


def _catalog():
    return Catalog.from_dict(_AV_CATALOG)


def test_round_trip_identity():
    cat = _catalog()
    again = Catalog.from_json(cat.to_json())
    assert again.to_dict() == cat.to_dict() == _AV_CATALOG


def test_unknown_fields_tolerated_and_preserved():
    doc = dict(_AV_CATALOG, customRoot="x")
    doc["tracks"] = [dict(_AV_CATALOG["tracks"][0], customTrack=7)]
    cat = Catalog.from_dict(doc)
    assert cat.extra == {"customRoot": "x"}
    assert cat.tracks[0].extra == {"customTrack": 7}
    out = cat.to_dict()
    assert out["customRoot"] == "x" and out["tracks"][0]["customTrack"] == 7


def test_version_gate():
    assert Catalog.from_dict(dict(_AV_CATALOG, version=1)).version == "1"
    with pytest.raises(CatalogError, match="version"):
        Catalog.from_dict(dict(_AV_CATALOG, version="99"))
    assert Catalog.from_dict(dict(_AV_CATALOG, version="99"),
                             lenient=True).version == "99"
    with pytest.raises(CatalogError, match="version"):
        Catalog.from_dict({"tracks": []})  # missing version, independent


def test_init_data_list_emitted_after_tracks():
    cat = _catalog()
    cat.initDataList = [InitData.from_bytes("v0", b"\x01\x64avcC")]
    cat.tracks[0].initRef = "v0"
    keys = list(json.loads(cat.to_json()))
    assert keys.index("initDataList") > keys.index("tracks")
    assert keys[-1] == "initDataList"


def test_resolve_init():
    cat = _catalog()
    cat.initDataList = [InitData.from_bytes("v0", b"extradata")]
    cat.tracks[0].initRef = "v0"
    assert cat.resolve_init(cat.tracks[0]) == b"extradata"
    assert cat.resolve_init(cat.tracks[1]) is None
    cat.tracks[0].initRef = "missing"
    with pytest.raises(CatalogError, match="initRef"):
        cat.resolve_init(cat.tracks[0])


def test_delta_add_and_clone():
    cat = _catalog()
    cat.apply(Catalog.from_dict(_DELTA_ADD_CLONE))
    assert [t.name for t in cat.tracks] == [
        "video", "audio", "slides", "video-540"]
    clone = cat.find("video-540")
    # Inherited from parent, overridden where redefined, no parent refs.
    assert clone.codec == "avc3.42E01E" and clone.framerate == 30
    assert (clone.width, clone.height, clone.bitrate) == (960, 540, 600000)
    assert clone.parentName is None
    assert cat.generatedAt == 1746104606050


def test_delta_remove():
    cat = _catalog()
    cat.apply(Catalog.from_dict(_DELTA_ADD_CLONE))
    cat.apply(Catalog.from_dict(_DELTA_REMOVE))
    assert [t.name for t in cat.tracks] == ["audio", "video-540"]


def test_delta_ops_apply_sequentially():
    # Clone may reference a track added by an earlier op in the SAME
    # update (§5.3: each op applies to the previous op's result).
    cat = _catalog()
    update = Catalog.delta([
        DeltaOp("add", [CatalogTrack(name="t1", packaging="loc",
                                     isLive=True, bitrate=1)]),
        DeltaOp("clone", [CatalogTrack(parentName="t1", name="t2")]),
    ])
    cat.apply(update)
    assert cat.find("t2").bitrate == 1


def test_delta_errors():
    cat = _catalog()
    with pytest.raises(CatalogError, match="already declared"):
        cat.apply(Catalog.delta(
            [DeltaOp("add", [CatalogTrack(name="video")])]))
    with pytest.raises(CatalogError, match="unknown track"):
        cat.apply(Catalog.delta(
            [DeltaOp("remove", [CatalogTrack(name="nope")])]))
    with pytest.raises(CatalogError, match="new track name"):
        cat.apply(Catalog.delta(
            [DeltaOp("clone", [CatalogTrack(parentName="video")])]))
    with pytest.raises(CatalogError, match="unknown delta op"):
        cat.apply(Catalog.delta([DeltaOp("mutate", [])]))
    with pytest.raises(CatalogError, match="delta update"):
        cat.apply(_catalog())  # independent catalog is not a delta


def test_delta_emission_shape():
    update = Catalog.delta(
        [DeltaOp("remove", [CatalogTrack(name="x")])], generatedAt=5)
    out = update.to_dict()
    assert "version" not in out and "tracks" not in out
    assert out["deltaUpdate"][0] == {"op": "remove",
                                     "tracks": [{"name": "x"}]}


def test_validate_clean_and_dirty():
    assert _catalog().validate() == []
    cat = _catalog()
    cat.tracks[0].codec = None
    cat.tracks[0].targetLatency = 2000
    cat.tracks[0].buffers = {"target": 1000}
    cat.tracks[1].samplerate = None
    issues = cat.validate()
    assert any("requires codec" in i for i in issues)
    assert any("exclusive" in i for i in issues)
    assert any("samplerate" in i for i in issues)


def test_find_ambiguity():
    cat = _catalog()
    cat.tracks.append(CatalogTrack(name="video", namespace="other/ns"))
    with pytest.raises(CatalogError, match="ambiguous"):
        cat.find("video")
    assert cat.find("video", "other/ns").namespace == "other/ns"
