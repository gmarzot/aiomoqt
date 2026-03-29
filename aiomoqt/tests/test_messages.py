
import pytest
from conftest import moqt_message_serialization, moqt_test_id

from aiomoqt.types import *
from aiomoqt.messages import *

FETCH_TEST_CASES = [
    (
        Fetch,
        {
            "request_id": 42,
            "fetch_type": FetchType.FETCH,
            "namespace": (b"live", b"sports"),
            "track_name": b"football",
            "subscriber_priority": 1,
            "group_order": GroupOrder.ASCENDING,
            "start_group": 10,
            "start_object": 5,
            "end_group": 20,
            "end_object": 15,
            "parameters": {SetupParamType.AUTH_TOKEN: b'param0', ParamType.DELIVERY_TIMEOUT: 300}
        },
        MOQTMessageType.FETCH,
        False,
        "basic"
    ),
    (
        Fetch,
        {
            "request_id": 123,
            "fetch_type": FetchType.FETCH,
            "namespace": (b"vod", b"movies", b"action"),
            "track_name": b"stream1",
            "subscriber_priority": 3,
            "group_order": GroupOrder.ASCENDING,
            "start_group": 0,
            "start_object": 0,
            "end_group": 100,
            "end_object": 500,
            "parameters": {}
        },
        MOQTMessageType.FETCH,
        False,
        "empty_params"
    ),
    (
        Fetch,
        {
            "fetch_type": FetchType.JOINING_FETCH,
            "request_id": 789,
            "subscriber_priority": 255,
            "group_order": GroupOrder.DESCENDING,
            "joining_sub_id": 45,
            "pre_group_offset": 3,
            "parameters": {ParamType.GROUP_ORDER: GroupOrder.ASCENDING}
        },
        MOQTMessageType.FETCH,
        False,
        "joining_fetch"
    )
]

# Test cases for FetchCancel message (unchanged)
FETCH_CANCEL_TEST_CASES = [
    (
        FetchCancel,
        {
            "request_id": 42
        },
        MOQTMessageType.FETCH_CANCEL,
        False,
        "basic"
    ),
    (
        FetchCancel,
        {
            "request_id": 9999
        },
        MOQTMessageType.FETCH_CANCEL,
        False,
        "large_id"
    )
]

# Test cases for FetchOk message (unchanged)
FETCH_OK_TEST_CASES = [
    (
        FetchOk,
        {
            "request_id": 42,
            "group_order": 0,
            "end_of_track": 0,
            "largest_group_id": 50,
            "largest_object_id": 200,
            "parameters": {ParamType.AUTH_TOKEN: b"param1", ParamType.SUBSCRIBER_PRIORITY: 255}
        },
        MOQTMessageType.FETCH_OK,
        False,
        "basic"
    ),
    (
        FetchOk,
        {
            "request_id": 123,
            "group_order": 1,
            "end_of_track": 1,
            "largest_group_id": 100,
            "largest_object_id": 500,
            "parameters": {}
        },
        MOQTMessageType.FETCH_OK,
        False,
        "empty_params"
    )
]

# Test cases for FetchError message (unchanged)
FETCH_ERROR_TEST_CASES = [
    (
        FetchError,
        {
            "request_id": 42,
            "error_code": 404,
            "reason": "Not Found"
        },
        MOQTMessageType.FETCH_ERROR,
        False,
        "not_found"
    ),
    (
        FetchError,
        {
            "request_id": 123,
            "error_code": 500,
            "reason": "Internal Server Error"
        },
        MOQTMessageType.FETCH_ERROR,
        False,
        "server_error"
    )
]

# Test cases for ServerSetup message
SERVER_SETUP_TEST_CASES = [
    (
        ServerSetup,
        {
            "selected_version": 0xff0000A,
            "parameters": {
                SetupParamType.MAX_REQUEST_ID: 1000,
            }
        },
        MOQTMessageType.SERVER_SETUP,
        False,
        "basic"
    ),
    (
        ServerSetup,
        {
            "selected_version": 0xff00009,
            "parameters": {}
        },
        MOQTMessageType.SERVER_SETUP,
        False,
        "empty_params"
    )
]

# Test cases for ClientSetup message
CLIENT_SETUP_TEST_CASES = [
    (
        ClientSetup,
        {
            "versions": [1, 2],
            "parameters": {
                SetupParamType.MAX_REQUEST_ID: 100,
                SetupParamType.PATH: b"/path/to/endpoint",
                SetupParamType.IMPLEMENTATION: b"aiomoqt-1.0/dev",
                SetupParamType.AUTH_TOKEN: b"token123",
                SetupParamType.MAX_AUTH_TOKEN_CACHE_SIZE: 1024,
                SetupParamType.GREASE_1_PARAM: b'\xBA\xAD\xF0\x0D',
                SetupParamType.GREASE_2_PARAM: 0xDEADBEEF,
            }
        },
        MOQTMessageType.CLIENT_SETUP,
        False,
        "basic"
    ),
    (
        ClientSetup,
        {
            "versions": [0xff0000e],
            "parameters": {}
        },
        MOQTMessageType.CLIENT_SETUP,
        False,
        "empty_params"
    )
]

# Test cases for GoAway message
GOAWAY_TEST_CASES = [
    (
        GoAway,
        {
            "new_session_uri": "https://example.com/newsession"
        },
        MOQTMessageType.GOAWAY,
        False,
        "basic"
    ),
    (
        GoAway,
        {
            "new_session_uri": ""
        },
        MOQTMessageType.GOAWAY,
        False,
        "empty_uri"
    )
]

# Control message test cases (use generic conftest helper)
TEST_CASES = [
    # (class, params, type_id, needs_len, test_id)
    (
        PublishNamespace,
        {
            'namespace': (b'vivohcast', b'net', b'live'),
            'parameters': {
                ParamType.AUTH_TOKEN: b'auth-token-123',
                ParamType.GREASE_1_PARAM: b'\xBA\xAD\xF0\x0D',
                ParamType.GREASE_2_PARAM: 1234,
            }
        },
        MOQTMessageType.PUBLISH_NAMESPACE,
        False
    ),
    (
        PublishNamespaceOk,
        {
            'request_id': 0,
        },
        MOQTMessageType.PUBLISH_NAMESPACE_OK,
        False
    ),
    (
        PublishNamespaceError,
        {
            'request_id': 0,
            'error_code': 404,
            'reason': 'Not found'
        },
        MOQTMessageType.PUBLISH_NAMESPACE_ERROR,
        False
    ),
    (
        PublishNamespaceDone,
        {
            'namespace': (b'vivohcast', b'net', b'live')
        },
        MOQTMessageType.PUBLISH_NAMESPACE_DONE,
        False,
    ),
    (
        PublishNamespaceCancel,
        {
            'namespace': (b'vivohcast', b'net', b'live'),
            'error_code': 503,
            'reason': 'Service unavailable'
        },
        MOQTMessageType.PUBLISH_NAMESPACE_CANCEL,
        False,
    ),
    (
        SubscribeNamespace,
        {
            'request_id': 0,
            'namespace_prefix': (b'vivohcast', b'net'),
            'parameters': {
                ParamType.AUTH_TOKEN: b'auth-token-456',
                ParamType.GREASE_2_PARAM: 1111111111
            }
        },
        MOQTMessageType.SUBSCRIBE_NAMESPACE,
        False,
    ),
    (
        SubscribeNamespaceOk,
        {
            'request_id': 0,
        },
        MOQTMessageType.SUBSCRIBE_NAMESPACE_OK,
        False,
    ),
    (
        SubscribeNamespaceError,
        {
            'request_id': 0,
            'error_code': 400,
            'reason': 'Bad request'
        },
        MOQTMessageType.SUBSCRIBE_NAMESPACE_ERROR,
        False,
    ),
    (
        UnsubscribeNamespace,
        {
            'namespace_prefix': (b'vivohcast', b'net')
        },
        MOQTMessageType.UNSUBSCRIBE_NAMESPACE,
        False,
    ),
    (
        FetchHeader,
        {
            'request_id': 42
        },
        DataStreamType.FETCH_HEADER,
        False,
    ),
    (
        FetchObject,
        {
            'group_id': 1,
            'subgroup_id': 2,
            'object_id': 3,
            'publisher_priority': 56,
            'extensions': {0: 4207849484, 1: b'\xfa\xce\xb0\x0c'},
            'payload': b'Sample Payload'
        },
        None,
        False,
    ),
    (
        FetchObject,
        {
            'group_id': 1,
            'subgroup_id': 2,
            'object_id': 3,
            'publisher_priority': 56,
            'extensions': {},
            'payload': b''
        },
        None,
        False,
    ),
    # PUBLISH control messages (draft-14)
    (
        Publish,
        {
            'request_id': 1,
            'track_namespace': (b'live', b'sports'),
            'track_name': b'football',
            'track_alias': 42,
            'group_order': GroupOrder.ASCENDING,
            'content_exists': ContentExistsCode.NO_CONTENT,
            'forward': ForwardingPreference.SUBGROUP,
            'parameters': {}
        },
        MOQTMessageType.PUBLISH,
        False,
    ),
    (
        Publish,
        {
            'request_id': 2,
            'track_namespace': (b'vod',),
            'track_name': b'movie1',
            'track_alias': 99,
            'group_order': GroupOrder.DESCENDING,
            'content_exists': ContentExistsCode.EXISTS,
            'largest_group_id': 50,
            'largest_object_id': 200,
            'forward': ForwardingPreference.DATAGRAM,
            'parameters': {ParamType.DELIVERY_TIMEOUT: 5000}
        },
        MOQTMessageType.PUBLISH,
        False,
    ),
    (
        PublishOk,
        {
            'request_id': 1,
            'forward': ForwardingPreference.SUBGROUP,
            'priority': 128,
            'group_order': GroupOrder.ASCENDING,
            'filter_type': FilterType.LATEST_OBJECT,
            'parameters': {}
        },
        MOQTMessageType.PUBLISH_OK,
        False,
    ),
    (
        PublishOk,
        {
            'request_id': 2,
            'forward': ForwardingPreference.DATAGRAM,
            'priority': 255,
            'group_order': GroupOrder.DESCENDING,
            'filter_type': FilterType.ABSOLUTE_RANGE,
            'start_group': 10,
            'start_object': 5,
            'end_group': 100,
            'parameters': {}
        },
        MOQTMessageType.PUBLISH_OK,
        False,
    ),
    (
        PublishError,
        {
            'request_id': 1,
            'error_code': PublishErrorCode.UNINTERESTED,
            'reason': 'Not interested'
        },
        MOQTMessageType.PUBLISH_ERROR,
        False,
    ),
]

TEST_CASES.extend(FETCH_TEST_CASES)
TEST_CASES.extend(FETCH_CANCEL_TEST_CASES)
TEST_CASES.extend(FETCH_OK_TEST_CASES)
TEST_CASES.extend(FETCH_ERROR_TEST_CASES)
TEST_CASES.extend(SERVER_SETUP_TEST_CASES)
TEST_CASES.extend(CLIENT_SETUP_TEST_CASES)
TEST_CASES.extend(GOAWAY_TEST_CASES)

@pytest.mark.parametrize(
    "cls,params,type_id,needs_len",
    [case[:4] for case in TEST_CASES],
    ids=[moqt_test_id(case) for case in TEST_CASES]
)
def test_moqt_messages(cls, params, type_id, needs_len):
    """Test all MOQT message classes through parameterized testing."""
    assert moqt_message_serialization(cls, params, type_id, needs_len)


# ---- Draft-14 SubgroupHeader tests (type variants 0x10-0x1D) ----

def test_subgroup_header_explicit():
    """SubgroupHeader with explicit subgroup_id (mode 2), no extensions, no end_of_group."""
    sg = SubgroupHeader(
        track_alias=123, group_id=456, subgroup_id=789,
        publisher_priority=10, subgroup_id_mode=SUBGROUP_ID_EXPLICIT,
    )
    buf = sg.serialize()
    buf_len = buf.tell()
    buf.seek(0)
    type_val = buf.pull_uint_var()
    assert type_val == 0x14  # base 0x10 | (2 << 1) = 0x14
    new_sg = SubgroupHeader.deserialize(buf, type_val)
    assert new_sg.track_alias == 123
    assert new_sg.group_id == 456
    assert new_sg.subgroup_id == 789
    assert new_sg.publisher_priority == 10
    assert new_sg.extensions_present is False
    assert new_sg.end_of_group is False
    assert new_sg.subgroup_id_mode == SUBGROUP_ID_EXPLICIT


def test_subgroup_header_zero():
    """SubgroupHeader with zero subgroup_id (mode 0)."""
    sg = SubgroupHeader(
        track_alias=1, group_id=2, subgroup_id=0,
        publisher_priority=128, subgroup_id_mode=SUBGROUP_ID_ZERO,
    )
    buf = sg.serialize()
    buf_len = buf.tell()
    buf.seek(0)
    type_val = buf.pull_uint_var()
    assert type_val == 0x10  # base, mode=0, no flags
    new_sg = SubgroupHeader.deserialize(buf, type_val)
    assert new_sg.subgroup_id == 0
    assert new_sg.subgroup_id_mode == SUBGROUP_ID_ZERO


def test_subgroup_header_with_extensions_and_eog():
    """SubgroupHeader with extensions + end_of_group + explicit subgroup_id."""
    sg = SubgroupHeader(
        track_alias=10, group_id=20, subgroup_id=30,
        publisher_priority=5, subgroup_id_mode=SUBGROUP_ID_EXPLICIT,
        extensions_present=True, end_of_group=True,
    )
    buf = sg.serialize()
    buf.seek(0)
    type_val = buf.pull_uint_var()
    # 0x10 | 0x01 (ext) | (2 << 1) (explicit) | 0x08 (eog) = 0x1D
    assert type_val == 0x1D
    new_sg = SubgroupHeader.deserialize(buf, type_val)
    assert new_sg.extensions_present is True
    assert new_sg.end_of_group is True
    assert new_sg.subgroup_id == 30


def test_subgroup_header_first_obj_mode():
    """SubgroupHeader with first_obj_id mode (subgroup_id resolved later)."""
    sg = SubgroupHeader(
        track_alias=1, group_id=2,
        publisher_priority=128, subgroup_id_mode=SUBGROUP_ID_FIRST_OBJ,
    )
    buf = sg.serialize()
    buf.seek(0)
    type_val = buf.pull_uint_var()
    assert type_val == 0x12  # base | (1 << 1)
    new_sg = SubgroupHeader.deserialize(buf, type_val)
    assert new_sg.subgroup_id is None  # resolved when first object arrives
    assert new_sg.subgroup_id_mode == SUBGROUP_ID_FIRST_OBJ


# ---- Draft-14 ObjectHeader tests (delta encoding + conditional extensions) ----

def test_object_header_first_object_with_extensions():
    """First object in subgroup: delta == absolute object_id, extensions present."""
    obj = ObjectHeader(
        object_id=5,
        extensions={0x20: 1234},
        status=ObjectStatus.NORMAL,
        payload=b'Hello World'
    )
    buf = obj.serialize(extensions_present=True, prev_object_id=None)
    buf_len = buf.tell()
    buf.seek(0)
    new_obj = ObjectHeader.deserialize(buf, buf_len, extensions_present=True, prev_object_id=None)
    assert new_obj.object_id == 5
    assert new_obj.extensions[0x20] == 1234
    assert new_obj.payload == b'Hello World'


def test_object_header_delta_encoding():
    """Subsequent object: delta = obj_id - prev_id - 1."""
    obj = ObjectHeader(
        object_id=10,
        status=ObjectStatus.NORMAL,
        payload=b'data'
    )
    # Sequential: prev=9, so delta = 10 - 9 - 1 = 0
    buf = obj.serialize(extensions_present=False, prev_object_id=9)
    buf_len = buf.tell()
    buf.seek(0)
    new_obj = ObjectHeader.deserialize(buf, buf_len, extensions_present=False, prev_object_id=9)
    assert new_obj.object_id == 10
    assert new_obj.extensions is None
    assert new_obj.payload == b'data'


def test_object_header_no_extensions():
    """Object without extensions (subgroup header extensions_present=False)."""
    obj = ObjectHeader(
        object_id=0,
        status=ObjectStatus.END_OF_GROUP,
        payload=b''
    )
    buf = obj.serialize(extensions_present=False)
    buf_len = buf.tell()
    buf.seek(0)
    new_obj = ObjectHeader.deserialize(buf, buf_len, extensions_present=False)
    assert new_obj.object_id == 0
    assert new_obj.extensions is None
    assert new_obj.status == ObjectStatus.END_OF_GROUP


# ---- Draft-14 ObjectDatagram tests (type variants 0x00-0x07) ----

def test_object_datagram_basic():
    """ObjectDatagram with object_id, no extensions, no end_of_group."""
    dg = ObjectDatagram(
        track_alias=123, group_id=456, object_id=789,
        publisher_priority=255, payload=b'Hello World'
    )
    buf = dg.serialize()
    buf_len = buf.tell()
    buf.seek(0)
    type_val = buf.pull_uint_var()
    assert type_val == 0x00  # no flags
    new_dg = ObjectDatagram.deserialize(buf, buf_len, type_val)
    assert new_dg.track_alias == 123
    assert new_dg.group_id == 456
    assert new_dg.object_id == 789
    assert new_dg.payload == b'Hello World'
    assert new_dg.end_of_group is False


def test_object_datagram_with_extensions():
    """ObjectDatagram with extensions."""
    dg = ObjectDatagram(
        track_alias=1, group_id=2, object_id=3,
        publisher_priority=128,
        extensions={MOQT_TIMESTAMP_EXT: 1234567890},
        payload=b'payload'
    )
    buf = dg.serialize()
    buf_len = buf.tell()
    buf.seek(0)
    type_val = buf.pull_uint_var()
    assert type_val & 0x01  # extensions bit set
    new_dg = ObjectDatagram.deserialize(buf, buf_len, type_val)
    assert new_dg.extensions[MOQT_TIMESTAMP_EXT] == 1234567890


def test_object_datagram_no_object_id():
    """ObjectDatagram with object_id=0 (no_object_id flag set)."""
    dg = ObjectDatagram(
        track_alias=1, group_id=2, object_id=0,
        publisher_priority=128, payload=b'first'
    )
    buf = dg.serialize()
    buf_len = buf.tell()
    buf.seek(0)
    type_val = buf.pull_uint_var()
    assert type_val & 0x04  # no_object_id bit set
    new_dg = ObjectDatagram.deserialize(buf, buf_len, type_val)
    assert new_dg.object_id == 0


def test_object_datagram_end_of_group():
    """ObjectDatagram with end_of_group flag."""
    dg = ObjectDatagram(
        track_alias=1, group_id=2, object_id=99,
        publisher_priority=128, payload=b'last',
        end_of_group=True,
    )
    buf = dg.serialize()
    buf_len = buf.tell()
    buf.seek(0)
    type_val = buf.pull_uint_var()
    assert type_val & 0x02  # end_of_group bit set
    new_dg = ObjectDatagram.deserialize(buf, buf_len, type_val)
    assert new_dg.end_of_group is True


# ---- Draft-14 ObjectDatagramStatus tests (type variants 0x20-0x21) ----

def test_object_datagram_status_basic():
    """ObjectDatagramStatus without extensions."""
    ds = ObjectDatagramStatus(
        track_alias=123, group_id=456, object_id=789,
        publisher_priority=0, status=ObjectStatus.END_OF_GROUP
    )
    buf = ds.serialize()
    buf_len = buf.tell()
    buf.seek(0)
    type_val = buf.pull_uint_var()
    assert type_val == 0x20
    new_ds = ObjectDatagramStatus.deserialize(buf, type_val)
    assert new_ds.track_alias == 123
    assert new_ds.object_id == 789
    assert new_ds.status == ObjectStatus.END_OF_GROUP


def test_object_datagram_status_with_extensions():
    """ObjectDatagramStatus with extensions."""
    ds = ObjectDatagramStatus(
        track_alias=1, group_id=2, object_id=3,
        publisher_priority=0,
        extensions={0x20: 999},
        status=ObjectStatus.DOES_NOT_EXIST
    )
    buf = ds.serialize()
    buf.seek(0)
    type_val = buf.pull_uint_var()
    assert type_val == 0x21  # extensions bit set
    new_ds = ObjectDatagramStatus.deserialize(buf, type_val)
    assert new_ds.extensions[0x20] == 999
    assert new_ds.status == ObjectStatus.DOES_NOT_EXIST


def test_fetch_header():
    params = {
        'request_id': 42
    }
    assert moqt_message_serialization(FetchHeader, params, DataStreamType.FETCH_HEADER)

def test_fetch_object():
    params = {
        'group_id': 1,
        'subgroup_id': 2,
        'object_id': 3,
        'publisher_priority': 56,
        'extensions': {},
        'payload': b'Sample payload'
    }
    assert moqt_message_serialization(FetchObject, params)


# ========================================================================
# Draft-16 message serialization tests
# ========================================================================

from conftest import moqt_message_serialization_versioned
from aiomoqt.types import MOQT_VERSION_DRAFT14, MOQT_VERSION_DRAFT16
from aiomoqt.messages import (RequestOk, RequestError, RequestUpdate,
                               Namespace, NamespaceDone)
from aiomoqt.types import D16MessageType


class TestDraft16Setup:
    """Draft-16 CLIENT_SETUP/SERVER_SETUP: no version fields, delta-encoded params."""

    def test_client_setup_d16(self):
        assert moqt_message_serialization_versioned(
            ClientSetup,
            {'versions': [], 'parameters': {
                SetupParamType.PATH: b'/moq',
                SetupParamType.MAX_REQUEST_ID: 10000,
                SetupParamType.IMPLEMENTATION: b'test-1.0',
            }},
            type_id=MOQTMessageType.CLIENT_SETUP,
            version=MOQT_VERSION_DRAFT16,
        )

    def test_server_setup_d16(self):
        assert moqt_message_serialization_versioned(
            ServerSetup,
            {'selected_version': None, 'parameters': {
                SetupParamType.MAX_REQUEST_ID: 100,
            }},
            type_id=MOQTMessageType.SERVER_SETUP,
            version=MOQT_VERSION_DRAFT16,
        )

    def test_client_setup_d14_still_works(self):
        """Ensure d14 round-trip is unbroken."""
        assert moqt_message_serialization_versioned(
            ClientSetup,
            {'versions': [0xff00000e], 'parameters': {
                SetupParamType.MAX_REQUEST_ID: 100,
            }},
            type_id=MOQTMessageType.CLIENT_SETUP,
            version=MOQT_VERSION_DRAFT14,
        )


class TestDraft16Subscribe:
    """Draft-16 SUBSCRIBE/SUBSCRIBE_OK: fixed fields moved to params."""

    def test_subscribe_d16(self):
        assert moqt_message_serialization_versioned(
            Subscribe,
            {
                'request_id': 0,
                'track_namespace': (b'live', b'sports'),
                'track_name': b'football',
                'priority': 128,
                'group_order': GroupOrder.ASCENDING,
                'forward': 1,
                'filter_type': FilterType.LATEST_OBJECT,
                'parameters': {},
            },
            type_id=MOQTMessageType.SUBSCRIBE,
            version=MOQT_VERSION_DRAFT16,
        )

    def test_subscribe_ok_d16(self):
        assert moqt_message_serialization_versioned(
            SubscribeOk,
            {
                'request_id': 0,
                'track_alias': 42,
                'expires': 300,
                'group_order': GroupOrder.ASCENDING,
                'content_exists': ContentExistsCode.EXISTS,
                'largest_group_id': 50,
                'largest_object_id': 200,
                'parameters': {},
                'track_extensions': {},
            },
            type_id=MOQTMessageType.SUBSCRIBE_OK,
            version=MOQT_VERSION_DRAFT16,
            skip_fields={'content_exists', 'track_extensions'},
        )

    def test_subscribe_d16_with_filter(self):
        """SUBSCRIBE with ABSOLUTE_RANGE filter in params."""
        assert moqt_message_serialization_versioned(
            Subscribe,
            {
                'request_id': 2,
                'track_namespace': (b'vod',),
                'track_name': b'movie',
                'priority': 255,
                'group_order': GroupOrder.DESCENDING,
                'forward': 2,
                'filter_type': FilterType.ABSOLUTE_RANGE,
                'start_group': 10,
                'start_object': 5,
                'end_group': 100,
                'parameters': {},
            },
            type_id=MOQTMessageType.SUBSCRIBE,
            version=MOQT_VERSION_DRAFT16,
        )


class TestDraft16Publish:
    """Draft-16 PUBLISH/PUBLISH_OK: fixed fields moved to params + track extensions."""

    def test_publish_d16_no_content(self):
        assert moqt_message_serialization_versioned(
            Publish,
            {
                'request_id': 1,
                'track_namespace': (b'live', b'sports'),
                'track_name': b'football',
                'track_alias': 42,
                'group_order': GroupOrder.ASCENDING,
                'content_exists': ContentExistsCode.NO_CONTENT,
                'forward': ForwardingPreference.SUBGROUP,
                'parameters': {},
                'track_extensions': {},
            },
            type_id=MOQTMessageType.PUBLISH,
            version=MOQT_VERSION_DRAFT16,
            skip_fields={'content_exists', 'track_extensions'},
        )

    def test_publish_d16_with_content(self):
        assert moqt_message_serialization_versioned(
            Publish,
            {
                'request_id': 2,
                'track_namespace': (b'vod',),
                'track_name': b'movie1',
                'track_alias': 99,
                'group_order': GroupOrder.DESCENDING,
                'content_exists': ContentExistsCode.EXISTS,
                'largest_group_id': 50,
                'largest_object_id': 200,
                'forward': ForwardingPreference.DATAGRAM,
                'parameters': {},
                'track_extensions': {},
            },
            type_id=MOQTMessageType.PUBLISH,
            version=MOQT_VERSION_DRAFT16,
            skip_fields={'content_exists', 'track_extensions'},
        )

    def test_publish_ok_d16(self):
        assert moqt_message_serialization_versioned(
            PublishOk,
            {
                'request_id': 1,
                'forward': ForwardingPreference.SUBGROUP,
                'priority': 128,
                'group_order': GroupOrder.ASCENDING,
                'filter_type': FilterType.LATEST_OBJECT,
                'parameters': {},
            },
            type_id=MOQTMessageType.PUBLISH_OK,
            version=MOQT_VERSION_DRAFT16,
        )


class TestDraft16Fetch:
    """Draft-16 FETCH/FETCH_OK: priority/group_order moved to params."""

    def test_fetch_d16(self):
        assert moqt_message_serialization_versioned(
            Fetch,
            {
                'request_id': 42,
                'fetch_type': FetchType.FETCH,
                'namespace': (b'live', b'sports'),
                'track_name': b'football',
                'subscriber_priority': 1,
                'group_order': GroupOrder.ASCENDING,
                'start_group': 10,
                'start_object': 5,
                'end_group': 20,
                'end_object': 15,
                'parameters': {},
            },
            type_id=MOQTMessageType.FETCH,
            version=MOQT_VERSION_DRAFT16,
        )

    def test_fetch_ok_d16(self):
        assert moqt_message_serialization_versioned(
            FetchOk,
            {
                'request_id': 42,
                'group_order': GroupOrder.ASCENDING,
                'end_of_track': 0,
                'largest_group_id': 50,
                'largest_object_id': 200,
                'parameters': {},
                'track_extensions': {},
            },
            type_id=MOQTMessageType.FETCH_OK,
            version=MOQT_VERSION_DRAFT16,
            skip_fields={'track_extensions'},
        )


class TestDraft16Namespace:
    """Draft-16 namespace messages: RequestID-based done/cancel."""

    def test_publish_namespace_done_d16(self):
        assert moqt_message_serialization_versioned(
            PublishNamespaceDone,
            {'request_id': 42},
            type_id=MOQTMessageType.PUBLISH_NAMESPACE_DONE,
            version=MOQT_VERSION_DRAFT16,
            skip_fields={'namespace'},
        )

    def test_publish_namespace_cancel_d16(self):
        assert moqt_message_serialization_versioned(
            PublishNamespaceCancel,
            {'request_id': 7, 'error_code': 0x10, 'reason': 'gone'},
            type_id=MOQTMessageType.PUBLISH_NAMESPACE_CANCEL,
            version=MOQT_VERSION_DRAFT16,
            skip_fields={'namespace'},
        )


class TestDraft16NewMessages:
    """Draft-16 new message types: RequestOk, RequestError, RequestUpdate,
    Namespace, NamespaceDone."""

    def test_request_ok(self):
        assert moqt_message_serialization_versioned(
            RequestOk,
            {'request_id': 5, 'parameters': {ParamType.EXPIRES: 300}},
            type_id=D16MessageType.REQUEST_OK,
            version=MOQT_VERSION_DRAFT16,
        )

    def test_request_error(self):
        assert moqt_message_serialization_versioned(
            RequestError,
            {'request_id': 5, 'error_code': 0x10,
             'retry_interval': 1000, 'reason': 'does not exist'},
            type_id=D16MessageType.REQUEST_ERROR,
            version=MOQT_VERSION_DRAFT16,
        )

    def test_request_update(self):
        assert moqt_message_serialization_versioned(
            RequestUpdate,
            {'request_id': 10, 'existing_request_id': 5,
             'parameters': {ParamType.FORWARD: 1}},
            type_id=D16MessageType.REQUEST_UPDATE,
            version=MOQT_VERSION_DRAFT16,
        )

    def test_namespace(self):
        assert moqt_message_serialization_versioned(
            Namespace,
            {'namespace_suffix': (b'live', b'sports')},
            type_id=D16MessageType.NAMESPACE,
            version=MOQT_VERSION_DRAFT16,
        )

    def test_namespace_done(self):
        assert moqt_message_serialization_versioned(
            NamespaceDone,
            {'namespace_suffix': (b'live', b'sports')},
            type_id=D16MessageType.NAMESPACE_DONE,
            version=MOQT_VERSION_DRAFT16,
        )
