import inspect
import os
import subprocess
from dataclasses import fields

import pytest

from aiomoqt.messages import MOQTMessageType
from aiomoqt.context import get_major_version


def pytest_configure(config):
    """Generate a self-signed loopback cert if absent so the
    test_loopback_* suites run on a fresh clone without a manual step.

    Runs at configure time (before collection) so the modules'
    cert-existence skipif sees the freshly written certs. Best-effort:
    if openssl is missing or fails, the certs stay absent and those
    suites skip exactly as before — no hard dependency, no failure."""
    certs_dir = os.path.realpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "certs"))
    cert = os.path.join(certs_dir, "cert.pem")
    key = os.path.join(certs_dir, "key.pem")
    if os.path.exists(cert) and os.path.exists(key):
        return
    os.makedirs(certs_dir, exist_ok=True)
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-days", "3650", "-keyout", key, "-out", cert,
             "-subj", "/CN=localhost",
             "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
            check=True, capture_output=True)
    except Exception:
        pass  # openssl absent/failed → loopback suites skip as before


def _serialize(obj, draft):
    """obj.serialize(), threading draft= only for classes whose
    serialize takes it (control messages + FetchObject). The
    draft-invariant data classes have no draft parameter."""
    if 'draft' in inspect.signature(type(obj).serialize).parameters:
        return obj.serialize(draft=draft)
    return obj.serialize()


def _deserialize(cls, *args, draft, **kwargs):
    """Mirror of _serialize for cls.deserialize()."""
    if 'draft' in inspect.signature(cls.deserialize).parameters:
        return cls.deserialize(*args, draft=draft, **kwargs)
    return cls.deserialize(*args, **kwargs)


def moqt_test_id(case):
    """
    Generate a test ID from a test case tuple.
    Supports different test case formats.
    """
    if not isinstance(case, (list, tuple)):
        return str(case)

    cls = case[0]
    if not hasattr(cls, "__name__"):
        return str(cls)

    # Check if a variant name is provided (position 4)
    if len(case) > 4 and case[4]:
        return f"{cls.__name__}_{case[4]}"
    return cls.__name__

def moqt_message_serialization(cls, params, type_id=None, needs_len=False,
                               draft=14):
    """
    Test MOQT message class serialization/deserialization

    Args:
        cls: MOQT message class
        params: Dictionary of parameters to initialize the class
        type_id: Expected type id (if any)
        needs_len: Whether deserialize needs the buffer length
        draft: Draft number to (de)serialize under (default 14).
    """
    obj = cls(**params)
    buf = _serialize(obj, draft)

    buf_len = buf.tell()
    print(f"moqt_message_serialization: {cls.__name__} {buf_len}")
    buf.seek(0)

    if type_id is not None:
        id = buf.pull_uint_var()
        assert id == type_id

    # Check/strip type for typed messages
    if isinstance(type_id, MOQTMessageType):
        msg_len = buf.pull_uint16()
        buf_end = buf.tell() + msg_len
        new_obj = _deserialize(cls, buf, draft=draft, buf_end=buf_end)
    elif needs_len:
        new_obj = _deserialize(cls, buf, buf_len, draft=draft)
    else:
        new_obj = _deserialize(cls, buf, draft=draft)

    # Compare all fields from the dataclass
    for field in fields(cls):
        original_value = getattr(obj, field.name)
        new_value = getattr(new_obj, field.name)
        print(f"moqt_message_serialization: original: {original_value}  new: {new_value}")
        if isinstance(original_value, dict) or isinstance(new_value, dict):
            # Empty extensions block on the wire decodes to None — treat
            # {} and None as equivalent ("no extensions").
            if not original_value and not new_value:
                continue
            assert original_value is not None and new_value is not None, \
                f"`{field.name}`: {original_value!r} vs {new_value!r}"
            assert original_value.keys() == new_value.keys(), f"`{field.name}` keys don't match"
            for key in original_value:
                assert original_value[key] == new_value[key], f"`{field.name}` values don't match for key {key}"

        elif isinstance(original_value, tuple):
            # Handle tuples of bytes (like namespace)
            assert isinstance(new_value, tuple), f"'{field.name}' expected tuple but got {type(new_value)}"
            assert len(original_value) == len(new_value), f"'{field.name}' tuples have different lengths"

            for orig_item, new_item in zip(original_value, new_value):
                assert orig_item == new_item, f"'{field.name}'  tuple : {orig_item} != {new_item}"

        else:
            assert original_value == new_value, f"'{field.name}' doesn't match after deserialization"

    return True


def moqt_message_serialization_versioned(cls, params, type_id=None,
                                          needs_len=False, version=None,
                                          skip_fields=None):
    """Test serialization round-trip at a specific draft version.

    Args:
        version: MOQT version code (e.g. MOQT_VERSION_DRAFT16) or draft
                 number; normalized to a draft number for the codec.
        skip_fields: set of field names to skip comparison (e.g. fields
                     that are None on wire in d16 but set in the input)
    """
    draft = get_major_version(version) if version is not None else 14
    obj = cls(**params)
    buf = _serialize(obj, draft)
    buf_len = buf.tell()
    buf.seek(0)

    if type_id is not None:
        id = buf.pull_uint_var()
        assert id == type_id
        # All control messages have uint16 length after type
        msg_len = buf.pull_uint16()
        buf_end = buf.tell() + msg_len
        new_obj = _deserialize(cls, buf, draft=draft, buf_end=buf_end)
    elif needs_len:
        new_obj = _deserialize(cls, buf, buf_len, draft=draft)
    else:
        new_obj = _deserialize(cls, buf, draft=draft)

    skip = skip_fields or set()
    for field in fields(cls):
        if field.name in skip:
            continue
        original_value = getattr(obj, field.name)
        new_value = getattr(new_obj, field.name)
        if original_value is None and new_value is None:
            continue
        if isinstance(original_value, dict):
            assert (original_value or {}).keys() == (new_value or {}).keys(), \
                f"`{field.name}` keys don't match"
            for key in (original_value or {}):
                assert original_value[key] == new_value[key], \
                    f"`{field.name}` values don't match for key {key}"
        elif isinstance(original_value, tuple):
            assert isinstance(new_value, tuple)
            assert len(original_value) == len(new_value)
            for a, b in zip(original_value, new_value):
                assert a == b, f"'{field.name}' tuple mismatch"
        else:
            assert original_value == new_value, \
                f"'{field.name}' doesn't match: {original_value} != {new_value}"
    return True
