
import pytest
from conftest import moqt_message_serialization, moqt_test_id

from aiomoqt.types import *
from aiomoqt.messages import *

FETCH_TEST_CASES = [
    (
        Fetch,
        {
            "subscribe_id": 42,
            "fetch_type": FetchType.FETCH,
            "namespace": (b"live", b"sports"),
            "track_name": b"football",
            "subscriber_priority": 1,
            "group_order": GroupOrder.ASCENDING,
            "start_group": 10,
            "start_object": 5,
            "end_group": 20,
            "end_object": 15,
            "parameters": {0x0: b'param0', 0x01: b"param1"}
        },
        MOQTMessageType.FETCH,
        False,
        "basic"
    ),
    (
        Fetch,
        {
            "subscribe_id": 123,
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
            "subscribe_id": 789,
            "subscriber_priority": 255,
            "group_order": GroupOrder.DESCENDING,
            "joining_sub_id": 45,
            "pre_group_offset": 3,
            "parameters": {3: b"param3"}
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
            "subscribe_id": 42
        },
        MOQTMessageType.FETCH_CANCEL,
        False,
        "basic"
    ),
    (
        FetchCancel,
        {
            "subscribe_id": 9999
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
            "subscribe_id": 42,
            "group_order": 0,
            "end_of_track": 0,
            "largest_group_id": 50,
            "largest_object_id": 200,
            "parameters": {1: b"param1", 2: b"param2"}
        },
        MOQTMessageType.FETCH_OK,
        False,
        "basic"
    ),
    (
        FetchOk,
        {
            "subscribe_id": 123,
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
            "subscribe_id": 42,
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
            "subscribe_id": 123,
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
                SetupParamType.ENDPOINT_PATH: b"/path/to/endpoint"
            }
        },
        MOQTMessageType.CLIENT_SETUP,
        False,
        "basic"
    ),
    (
        ClientSetup,
        {
            "versions": [0xff00009],
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

# Define test cases for parameterized testing
TEST_CASES = [
    # (class, params, type_id, needs_len, test_id)
    (
        PublishNamespace,
        {
            'namespace': (b'vivohcast', b'net', b'live'),
            'parameters': {
                ParamType.AUTHORIZATION_TOKEN: b'auth-token-123',
                ParamType.GREASE_1_PARAM: b'\xDE\xAD\xBE\xEF'
            }
        },
        MOQTMessageType.PUBLISH_NAMESPACE,
        False
    ),
    (
        PublishNamespaceOk,
        {
            'namespace': (b'vivohcast', b'net', b'live')
        },
        MOQTMessageType.PUBLISH_NAMESPACE_OK,
        False
    ),
    (
        PublishNamespaceError,
        {
            'namespace': (b'vivohcast', b'net', b'live'),
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
            'namespace_prefix': (b'vivohcast', b'net'),
            'parameters': {
                1: b'param1',
                2: b'param2'
            }
        },
        MOQTMessageType.SUBSCRIBE_NAMESPACE,
        False,
    ),
    (
        SubscribeNamespaceOk,
        {
            'namespace_prefix': (b'vivohcast', b'net')
        },
        MOQTMessageType.SUBSCRIBE_NAMESPACE_OK,
        False,
    ),
    (
        SubscribeNamespaceError,
        {
            'namespace_prefix': (b'vivohcast', b'net'),
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
        SubgroupHeader, 
        {
            'track_alias': 123,
            'group_id': 456,
            'subgroup_id': 789,
            'publisher_priority': 10
        },
        DataStreamType.SUBGROUP_HEADER,
        False,
    ),
    (
        ObjectHeader,
        {
            'object_id': 1,
            'extensions': {0: 4207849484, 1: b'\xfa\xce\xb0\x0c'},
            'status': ObjectStatus.NORMAL,
            'payload': b'Hello World'
        },
        None,
        True,
    ),
    (
        FetchHeader,
        {
            'subscribe_id': 42
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
    (
        ObjectDatagram,
        {
            'track_alias': 123,
            'group_id': 456,
            'object_id': 789,
            'publisher_priority': 255,
            'extensions': {0: 4207849484, 1: b'\xfa\xce\xb0\x0c'},
            'payload': b'Hello World'
        },
        DatagramType.OBJECT_DATAGRAM,
        True,
    ),
    (
        ObjectDatagramStatus,
        {
            'track_alias': 123,
            'group_id': 456,
            'object_id': 789,
            'publisher_priority': 0,
            'extensions': {0: 4207849484, 1: b'\xfa\xce\xb0\x0c'},
            'status': ObjectStatus.DOES_NOT_EXIST
        },
        DatagramType.OBJECT_DATAGRAM_STATUS,
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


def test_subgroup_header():
    params = {
        'track_alias': 123,
        'group_id': 456,
        'subgroup_id': 789,
        'publisher_priority': 10
    }
    assert moqt_message_serialization(SubgroupHeader, params, DataStreamType.SUBGROUP_HEADER)

def test_object_header():
    params = {
        'object_id': 1,
        'extensions': {0: 4207849484, 1: b'\xfa\xce\xb0\x0c'},
        'status': ObjectStatus.NORMAL,
        'payload': b'Hello World'
    }
    assert moqt_message_serialization(ObjectHeader, params, needs_len=True)

def test_fetch_header():
    params = {
        'subscribe_id': 42
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

def test_object_datagram():
    params = {
        'track_alias': 123,
        'group_id': 456,
        'object_id': 789,
        'publisher_priority': 255,
        'extensions': {0: 4207849484, 1: b'\xfa\xce\xb0\x0c'},
        'payload': b'Hello World'
    }
    assert moqt_message_serialization(ObjectDatagram, params, DatagramType.OBJECT_DATAGRAM)

def test_object_datagram_status():
    params = {
        'track_alias': 123,
        'group_id': 456,
        'object_id': 789,
        'publisher_priority': 0,
        'extensions': {},
        'status': ObjectStatus.DOES_NOT_EXIST
    }
    assert moqt_message_serialization(ObjectDatagramStatus, params, DatagramType.OBJECT_DATAGRAM_STATUS)

# Example usage for each message class
def test_subgroup_header():
    params = {
        'track_alias': 123,
        'group_id': 456,
        'subgroup_id': 789,
        'publisher_priority': 10
    }
    assert moqt_message_serialization(SubgroupHeader, params, DataStreamType.SUBGROUP_HEADER)

def test_object_header():
    params = {
        'object_id': 1,
        'extensions': {0: 4207849484, 1: b'\xfa\xce\xb0\x0c'},
        'status': ObjectStatus.NORMAL,
        'payload': b''
    }
    assert moqt_message_serialization(ObjectHeader, params, needs_len=True)

def test_fetch_header():
    params = {
        'subscribe_id': 42
    }
    assert moqt_message_serialization(FetchHeader, params, DataStreamType.FETCH_HEADER)

def test_fetch_object():
    params = {
        'group_id': 1,
        'subgroup_id': 2,
        'object_id': 3,
        'publisher_priority': 56,
        'extensions': {0: 4207849484, 1: b'\xfa\xce\xb0\x0c'},
        'payload': b'Sample payload'
    }
    assert moqt_message_serialization(FetchObject, params)

def test_object_datagram():
    params = {
        'track_alias': 123,
        'group_id': 456,
        'object_id': 789,
        'publisher_priority': 255,
        'extensions': {0: 4207849484, 1: b'\xfa\xce\xb0\x0c'},
        'payload': b'Hello World'
    }
    assert moqt_message_serialization(ObjectDatagram, params, DatagramType.OBJECT_DATAGRAM, needs_len=True)

def test_object_datagram_status():
    params = {
        'track_alias': 123,
        'group_id': 456,
        'object_id': 789,
        'publisher_priority': 0,
        'extensions': {10000: 4207849484, 100000001: b'\xfa\xce\xb0\x0c'},
        'status': ObjectStatus.END_OF_GROUP
    }
    assert moqt_message_serialization(ObjectDatagramStatus, params, DatagramType.OBJECT_DATAGRAM_STATUS)

def test_ObjectHeader():
    data_bytes = b'\xfa\xce\xb0\x0c'
    obj = ObjectHeader(
        object_id = 1,
        status = ObjectStatus.NORMAL,
        extensions = {
            0: 4207849484,
            1: data_bytes,
        },
        payload = b'Hello World'
    )

    obj_buf  = obj.serialize()
    obj_len = obj_buf.tell()
    obj_buf.seek(0)
    new_obj = ObjectHeader.deserialize(obj_buf, obj_len)
    
    assert obj.object_id == new_obj.object_id
    assert obj.status == new_obj.status
    if obj.extensions is None:
        assert new_obj.extensions is None
    else:
        assert len(obj.extensions) == len(new_obj.extensions)
        
    assert len(obj.payload) == len(new_obj.payload)
