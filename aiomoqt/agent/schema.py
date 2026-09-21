"""JSON Schema 2020-12 emission for the spec dataclasses.

The emitter reads the same `Choices`/`Range`/`Doc` markers the validator
in `spec.py` reads, so a constraint is declared once and the published
schema cannot drift from what `from_dict` actually enforces. A test
asserts the two agree.

`additionalProperties` is false because `from_dict` is strict: the schema
describes the strict behaviour, not the lenient escape hatch.

Two shapes. `json_schema()` is the nested form, with `$defs`/`$ref` for
nested specs — right for documentation and for validating a stored spec.
`tool_schema()` flattens chosen nested fields to top-level scalars,
because MCP tool arguments are flat; `from_flat()` reverses it.
"""
from __future__ import annotations

from dataclasses import MISSING, fields as _dc_fields, is_dataclass
from types import UnionType
from typing import (
    Annotated, Any, Dict, List, Optional, Tuple, Union, get_args, get_origin,
    get_type_hints,
)

from .errors import SpecError
from .spec import Choices, Doc, Range, spec_keys

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

_PRIMITIVES: Dict[Any, Dict[str, Any]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    bytes: {"type": "string", "contentEncoding": "base64"},
}


def _strip_annotated(hint: Any) -> Tuple[Any, Tuple[Any, ...]]:
    if get_origin(hint) is Annotated:
        args = get_args(hint)
        return args[0], args[1:]
    return hint, ()


def _is_optional(hint: Any) -> bool:
    hint, _ = _strip_annotated(hint)
    return (get_origin(hint) in (Union, UnionType)
            and type(None) in get_args(hint))


def _non_none(hint: Any) -> Any:
    """The bare type, with Annotated and Optional removed."""
    hint, _ = _strip_annotated(hint)
    if get_origin(hint) in (Union, UnionType):
        rest = [a for a in get_args(hint) if a is not type(None)]
        if len(rest) == 1:
            return _non_none(rest[0])
    return hint


def _field_schema(hint: Any, defs: Dict[str, Any]) -> Dict[str, Any]:
    _, metadata = _strip_annotated(hint)
    target = _non_none(hint)
    origin = get_origin(target)

    if is_dataclass(target):
        _collect(target, defs)
        node: Dict[str, Any] = {"$ref": f"#/$defs/{target.__name__}"}
    elif origin in (list, List):
        inner = (get_args(target) or (Any,))[0]
        node = {"type": "array", "items": _field_schema(inner, defs)}
    elif origin in (dict, Dict):
        node = {"type": "object"}
    elif target in _PRIMITIVES:
        node = dict(_PRIMITIVES[target])
    else:
        node = {}

    for meta in metadata:
        if isinstance(meta, Choices):
            node["enum"] = list(meta.values)
        elif isinstance(meta, Range):
            if meta.minimum is not None:
                node["minimum"] = meta.minimum
            if meta.maximum is not None:
                node["maximum"] = meta.maximum
        elif isinstance(meta, Doc):
            node["description"] = meta.text
    return node


def _collect(cls: type, defs: Dict[str, Any]) -> None:
    """Add `cls` to `defs` if absent. Recurses through nested specs."""
    if cls.__name__ in defs:
        return
    defs[cls.__name__] = {}  # placeholder breaks reference cycles
    hints = get_type_hints(cls, include_extras=True)
    props: Dict[str, Any] = {}
    required: List[str] = []
    for f in _dc_fields(cls):
        if f.name == "extra":
            continue
        hint = hints.get(f.name, Any)
        node = _field_schema(hint, defs)
        has_default = (f.default is not MISSING
                       or f.default_factory is not MISSING)  # type: ignore[misc]
        if f.default is not MISSING and f.default is not None:
            # A $ref node cannot carry siblings in strict 2020-12 readers.
            if "$ref" not in node:
                node["default"] = f.default
        if not has_default and not _is_optional(hint):
            required.append(f.name)
        props[f.name] = node

    schema: Dict[str, Any] = {"type": "object", "properties": props,
                              "additionalProperties": False}
    if required:
        schema["required"] = required
    doc = (cls.__doc__ or "").strip().split("\n\n")[0].replace("\n", " ")
    if doc:
        schema["description"] = doc
    constraints = getattr(cls, "schema_constraints", None)
    if callable(constraints):
        schema.update(constraints())
    defs[cls.__name__] = schema


def json_schema(cls: type) -> Dict[str, Any]:
    """Nested JSON Schema 2020-12 for a spec dataclass."""
    if not is_dataclass(cls):
        raise SpecError(f"json_schema: {cls!r} is not a dataclass")
    defs: Dict[str, Any] = {}
    _collect(cls, defs)
    root = dict(defs.pop(cls.__name__))
    root["$schema"] = SCHEMA_DIALECT
    root["title"] = cls.__name__
    if defs:
        root["$defs"] = defs
    return root


def _flat_name(parent: str, child: str, taken: Dict[str, Any]) -> str:
    return f"{parent}_{child}" if child in taken else child


def tool_schema(cls: type,
                flatten: Optional[Dict[str, Tuple[str, ...]]] = None,
                ) -> Dict[str, Any]:
    """Flat input schema for an MCP tool.

    `flatten` lifts named subfields of a nested spec to the top level, so
    a model supplies `namespace` rather than `{"track": {...}}`. A lifted
    name that collides is prefixed with its parent.
    """
    if flatten is not None and not isinstance(flatten, dict):
        raise SpecError(f"tool_schema: flatten must be a mapping, got {flatten!r}")
    nested = json_schema(cls)
    defs = nested.get("$defs", {})
    props = dict(nested.get("properties", {}))
    required = list(nested.get("required", []))

    for parent, children in (flatten or {}).items():
        if not isinstance(children, (list, tuple)) or not children:
            raise SpecError(
                f"tool_schema: flatten[{parent!r}] must name at least one "
                f"field, got {children!r}")
        node = props.pop(parent, None)
        if node is None:
            raise SpecError(f"tool_schema: {cls.__name__} has no field {parent!r}")
        ref = node.get("$ref", "")
        target = defs.get(ref.rsplit("/", 1)[-1])
        if target is None:
            raise SpecError(f"tool_schema: {parent!r} is not a nested spec")
        parent_required = set(target.get("required", []))
        was_required = parent in required
        if was_required:
            required.remove(parent)
        for child in children:
            child_node = target.get("properties", {}).get(child)
            if child_node is None:
                raise SpecError(
                    f"tool_schema: {parent!r} has no field {child!r}")
            name = _flat_name(parent, child, props)
            props[name] = dict(child_node)
            if was_required and child in parent_required:
                required.append(name)

    out: Dict[str, Any] = {"$schema": SCHEMA_DIALECT, "type": "object",
                           "title": cls.__name__, "properties": props,
                           "additionalProperties": False}
    if required:
        out["required"] = required
    kept = _reachable_defs(props, defs)
    if kept:
        out["$defs"] = kept
    return out


def _reachable_defs(props: Dict[str, Any],
                    defs: Dict[str, Any]) -> Dict[str, Any]:
    """Definitions still reachable by $ref after flattening."""
    seen: Dict[str, Any] = {}
    pending = [n for p in props.values() for n in _refs(p)]
    while pending:
        name = pending.pop()
        if name in seen or name not in defs:
            continue
        seen[name] = defs[name]
        pending.extend(_refs(defs[name]))
    return seen


def _refs(node: Any) -> List[str]:
    """Every '#/$defs/NAME' target under `node`."""
    found: List[str] = []
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            found.append(ref.rsplit("/", 1)[-1])
        for value in node.values():
            found.extend(_refs(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_refs(value))
    return found


def from_flat(cls: type, args: Dict[str, Any],
              flatten: Optional[Dict[str, Tuple[str, ...]]] = None,
              ) -> Dict[str, Any]:
    """Rebuild a nested spec dict from flat tool arguments.

    The inverse of `tool_schema`'s flattening, so an MCP handler can hand
    the result straight to `from_dict`.
    """
    if not isinstance(args, dict):
        raise SpecError(f"from_flat: expected an object, got {args!r}")
    if flatten is not None and not isinstance(flatten, dict):
        raise SpecError(f"from_flat: flatten must be a mapping, got {flatten!r}")
    out = dict(args)
    for parent, children in (flatten or {}).items():
        if parent not in spec_keys(cls):
            raise SpecError(f"from_flat: {cls.__name__} has no field {parent!r}")
        if not isinstance(children, (list, tuple)) or not children:
            raise SpecError(
                f"from_flat: flatten[{parent!r}] must name at least one "
                f"field, got {children!r}")
        collected: Dict[str, Any] = {}
        for child in children:
            for candidate in (child, f"{parent}_{child}"):
                if candidate in out:
                    collected[child] = out.pop(candidate)
                    break
        if collected:
            existing = out.get(parent)
            if isinstance(existing, dict):
                collected.update(existing)
            out[parent] = collected
    unknown = [k for k in out if k not in spec_keys(cls)]
    if unknown:
        raise SpecError(
            f"from_flat: {cls.__name__} got unexpected argument"
            f"{'s' if len(unknown) > 1 else ''} "
            f"{', '.join(repr(k) for k in unknown)}")
    return out
