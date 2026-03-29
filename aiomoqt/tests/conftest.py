from dataclasses import fields
from aiomoqt.messages import MOQTMessageType
from aiomoqt.context import set_moqt_ctx_version, get_moqt_ctx_version
from aiomoqt.types import MOQT_VERSION_DRAFT14, MOQT_VERSION_DRAFT16


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

def moqt_message_serialization(cls, params, type_id=None, needs_len=False):
    """
    Test MOQT message class serialization/deserialization
    
    Args:
        cls: MOQT message class
        params: Dictionary of parameters to initialize the class
        type_id: Expected type id (if any)
        needs_len: Whether deserialize needs the buffer length
    """
    obj = cls(**params)
    buf = obj.serialize()

    buf_len = buf.tell()
    print(f"moqt_message_serialization: {cls.__name__} {buf_len}")
    buf.seek(0)
    
    if type_id is not None:
        id = buf.pull_uint_var()
        assert id == type_id

    # Check/strip type for typed messages
    if isinstance(type_id, MOQTMessageType):
        msg_len = buf.pull_uint16()
        
    if needs_len:
        new_obj = cls.deserialize(buf, buf_len)
    else:
        new_obj = cls.deserialize(buf)
    
    # Compare all fields from the dataclass
    for field in fields(cls):            
        original_value = getattr(obj, field.name)
        new_value = getattr(new_obj, field.name)
        print(f"moqt_message_serialization: original: {original_value}  new: {new_value}")
        if isinstance(original_value, dict):
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
        version: MOQT version code (e.g. MOQT_VERSION_DRAFT16)
        skip_fields: set of field names to skip comparison (e.g. fields
                     that are None on wire in d16 but set in the input)
    """
    prev = get_moqt_ctx_version()
    if version is not None:
        set_moqt_ctx_version(version)
    try:
        obj = cls(**params)
        buf = obj.serialize()
        buf_len = buf.tell()
        buf.seek(0)

        if type_id is not None:
            id = buf.pull_uint_var()
            assert id == type_id
            # All control messages have uint16 length after type
            msg_len = buf.pull_uint16()

        if needs_len:
            new_obj = cls.deserialize(buf, buf_len)
        else:
            new_obj = cls.deserialize(buf)

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
    finally:
        set_moqt_ctx_version(prev)
