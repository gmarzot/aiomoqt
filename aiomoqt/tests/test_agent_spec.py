"""Agent spec objects — round-trip, validation, and schema agreement.

The schema/validator agreement table is the point of this module. The
JSON Schema emitter and `from_dict` read the same Annotated markers but
are separate code paths, so they can drift; `test_schema_matches_validator`
is what keeps the published schema an honest description of what the
library actually accepts.
"""
import pytest

from aiomoqt.agent import (
    MAPPINGS, ON_FULL, Priority, PublishSpec, SpecError, StartAt,
    SubscribeSpec, TrackRef,
)
from aiomoqt.agent.schema import (
    SCHEMA_DIALECT, from_flat, json_schema, tool_schema,
)

jsonschema = pytest.importorskip("jsonschema")

SPECS = (TrackRef, StartAt, Priority, SubscribeSpec, PublishSpec)
FLAT = {"track": ("namespace", "name", "relay")}

# (payload, accepted) — every entry must get the same verdict from the
# emitted schema and from from_dict.
_SUBSCRIBE_CASES = (
    ({"track": {"namespace": "a"}}, True),
    ({"track": {"namespace": "a", "name": "t"}, "buffer": 512}, True),
    ({"track": {"namespace": "a"}, "on_full": "block", "decode": "json"}, True),
    ({"track": {"namespace": "a"}, "priority": {"subscriber": 0}}, True),
    ({"track": {"namespace": "a"}, "priority": {"subscriber": 255}}, True),
    ({"track": {"namespace": "a"},
      "start_at": {"mode": "group", "group": 7}}, True),
    ({"track": {"namespace": "a"},
      "start_at": {"mode": "range", "group": 1, "end_group": 9}}, True),
    ({"track": {"namespace": "a"}, "forward": False}, True),
    ({}, False),                                                # no track
    ({"track": {}}, False),                                     # no namespace
    ({"track": {"namespace": "a"}, "on_full": "explode"}, False),
    ({"track": {"namespace": "a"}, "decode": "pickle"}, False),
    ({"track": {"namespace": "a"}, "priority": {"subscriber": 256}}, False),
    ({"track": {"namespace": "a"}, "priority": {"subscriber": -1}}, False),
    ({"track": {"namespace": "a"}, "start_att": {}}, False),    # unknown key
    ({"track": {"namespace": "a", "bogus": 1}}, False),         # unknown nested
    ({"track": {"namespace": "a"}, "buffer": 0}, False),        # below minimum
    ({"track": {"namespace": "a"}, "buffer": 65537}, False),    # above maximum
)


def _accepts(cls, payload):
    try:
        cls.from_dict(payload)
        return True
    except SpecError:
        return False


@pytest.mark.parametrize("payload,accepted", _SUBSCRIBE_CASES)
def test_schema_matches_validator(payload, accepted):
    """The published schema and from_dict agree, case for case."""
    validator = jsonschema.validators.Draft202012Validator(
        json_schema(SubscribeSpec))
    assert validator.is_valid(payload) is accepted
    assert _accepts(SubscribeSpec, payload) is accepted


@pytest.mark.parametrize("cls", SPECS)
def test_emitted_schema_is_valid_2020_12(cls):
    schema = json_schema(cls)
    jsonschema.validators.Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == SCHEMA_DIALECT
    assert schema["additionalProperties"] is False


def test_round_trip_identity():
    spec = SubscribeSpec(
        track=TrackRef("live/cam-1", "video", relay="moqt://r.example:4433"),
        start_at=StartAt.at(120, 3),
        priority=Priority(subscriber=8, publisher=16,
                          group_order="descending", delivery_timeout_ms=500),
        forward=False, buffer=1024, on_full="block", decode="json",
        timeout_s=5.0)
    once = spec.to_dict()
    assert SubscribeSpec.from_dict(once).to_dict() == once


def test_defaults_omitted_and_restored():
    """An all-default nested spec is dropped, and comes back on parse."""
    spec = PublishSpec(track=TrackRef("ns", "t"))
    assert "priority" not in spec.to_dict()
    assert PublishSpec.from_dict(spec.to_dict()).priority == Priority()


def test_unknown_field_suggests_a_near_match():
    with pytest.raises(SpecError, match="did you mean 'start_at'"):
        SubscribeSpec.from_dict({"track": {"namespace": "a"}, "start_att": {}})


def test_lenient_keeps_unknown_fields():
    spec = SubscribeSpec.from_dict(
        {"track": {"namespace": "a"}, "future_key": 1}, lenient=True)
    assert spec.extra == {"future_key": 1}
    assert spec.to_dict()["future_key"] == 1


@pytest.mark.parametrize("raw,field,expected", (
    ({"track": {"namespace": "a"}, "buffer": "512"}, "buffer", 512),
    ({"track": {"namespace": "a"}, "forward": "false"}, "forward", False),
    ({"track": {"namespace": "a"}, "timeout_s": 5}, "timeout_s", 5.0),
))
def test_listed_coercions(raw, field, expected):
    """Models emit "512" and "false"; the accepted set is deliberately small."""
    assert getattr(SubscribeSpec.from_dict(raw), field) == expected


@pytest.mark.parametrize("raw", (
    {"track": {"namespace": "a"}, "buffer": "many"},
    {"track": {"namespace": "a"}, "buffer": True},
    {"track": {"namespace": "a"}, "forward": "yes"},
    {"track": {"namespace": "a"}, "namespace": 7},
))
def test_coercion_stays_closed(raw):
    with pytest.raises(SpecError):
        SubscribeSpec.from_dict(raw)


@pytest.mark.parametrize("text,namespace,name,relay", (
    ("ns/track", "ns", "track", None),
    ("a/b/c", "a/b", "c", None),
    ("moqt://r.example:4433/live/cam-1/video",
     "live/cam-1", "video", "moqt://r.example:4433"),
    ("https://r.example/ns/t", "ns", "t", "https://r.example"),
))
def test_trackref_parse(text, namespace, name, relay):
    ref = TrackRef.parse(text)
    assert (ref.namespace, ref.name, ref.relay) == (namespace, name, relay)


@pytest.mark.parametrize("text", ("", "moqt://r.example:4433"))
def test_trackref_parse_rejects(text):
    with pytest.raises(SpecError):
        TrackRef.parse(text)


@pytest.mark.parametrize("build", (
    lambda: StartAt(mode="group"),                  # group mode needs a group
    lambda: StartAt(mode="range", group=1),         # range needs both bounds
    lambda: StartAt.range(9, 2),                    # inverted bounds
    lambda: StartAt(mode="latest", group=3),        # unused field set
    lambda: StartAt(mode="next_group", object=0),
))
def test_startat_invariants(build):
    with pytest.raises(SpecError):
        build()


@pytest.mark.xfail(strict=True, reason=(
    "Choices is enforced in from_dict, not in __post_init__, so direct "
    "construction accepts an invalid enum and then serializes to JSON the "
    "published schema rejects"))
def test_direct_construction_validates_enums():
    with pytest.raises(SpecError):
        StartAt(mode="sideways")


def test_startat_constructors():
    assert StartAt.latest().mode == "latest"
    assert StartAt.next_group().mode == "next_group"
    assert StartAt.at(4).group == 4
    assert StartAt.range(1, 2).end_group == 2


def test_tool_schema_flattens_and_keeps_live_defs():
    flat = tool_schema(SubscribeSpec, flatten=FLAT)
    jsonschema.validators.Draft202012Validator.check_schema(flat)
    assert "track" not in flat["properties"]
    assert {"namespace", "name", "relay"} <= set(flat["properties"])
    assert flat["required"] == ["namespace"]
    # TrackRef was flattened away; the specs still referenced are kept.
    assert set(flat.get("$defs", {})) == {"StartAt", "Priority"}


def test_from_flat_round_trips_through_from_dict():
    args = {"namespace": "live/cam-1", "name": "video", "buffer": 512}
    spec = SubscribeSpec.from_dict(from_flat(SubscribeSpec, args, flatten=FLAT))
    assert spec.track == TrackRef("live/cam-1", "video")
    assert spec.buffer == 512


def test_from_flat_rejects_unknown_argument():
    with pytest.raises(SpecError, match="bogus"):
        from_flat(SubscribeSpec, {"namespace": "a", "bogus": 1}, flatten=FLAT)


@pytest.mark.parametrize("flatten", (
    {"nope": ("a",)},          # no such field
    {"buffer": ("a",)},        # not a nested spec
    {"track": ("nope",)},      # no such subfield
))
def test_tool_schema_rejects_bad_flatten(flatten):
    with pytest.raises(SpecError):
        tool_schema(SubscribeSpec, flatten=flatten)


def test_enum_markers_reach_the_schema():
    props = json_schema(SubscribeSpec)["properties"]
    assert props["on_full"]["enum"] == list(ON_FULL)
    assert json_schema(PublishSpec)["properties"]["mapping"]["enum"] == list(MAPPINGS)
    priority = json_schema(Priority)["properties"]["subscriber"]
    assert (priority["minimum"], priority["maximum"]) == (0, 255)
    assert "description" in priority


def test_agent_package_does_not_import_the_mcp_extra():
    """aiomoqt.agent must import with the mcp SDK absent."""
    import sys

    import aiomoqt.agent  # noqa: F401
    assert not [m for m in sys.modules if m == "mcp" or m.startswith("mcp.")]
