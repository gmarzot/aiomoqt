"""The ersatz relay answers a refused SUBSCRIBE with the publisher's own
error code and reason, encoded in the subscriber's draft."""
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.types import MOQTMessageType, MOQTRequestError, RequestErrorCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_PORT = 17520
_NS = "codes/live"
# Track -> (the code the d14 publisher refuses with, its d18 wire value).
_REFUSALS = {"a": (RequestErrorCode.INVALID_RANGE, 0x11),
             "b": (RequestErrorCode.UNAUTHORIZED, 0x1)}


def _clear_relay():
    for table in (relay._announced, relay._announce_requests, relay._tracks):
        table.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()


@pytest.fixture(autouse=True)
def _clean_relay():
    _clear_relay()
    yield
    _clear_relay()


async def _refuse(session, msg):
    name = msg.track_name.decode()
    session.subscribe_error(msg.request_id, error_code=_REFUSALS[name][0],
                            reason=f"refused {name}")


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_d14_refusal_reaches_a_d18_subscriber_in_its_codes(use_quic):
    # d14 INVALID_RANGE is 0x5 and d18's is 0x11: the relay passes on what
    # the code means, not the number, and one track's refusal is not
    # another's.
    port = _PORT + (not use_quic)
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=[18, 14])
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def connect(draft, handlers=()):
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=draft)
                for msg_type, handler in handlers:
                    client.register_handler(msg_type, handler)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s

            pub = await connect(14, [(MOQTMessageType.SUBSCRIBE, _refuse)])
            await pub.publish_namespace(namespace=_NS, wait_response=True)
            sub = await connect(18)
            assert (pub.negotiated_draft, sub.negotiated_draft) == (14, 18)
            for name, (_, wire) in _REFUSALS.items():
                with pytest.raises(MOQTRequestError) as refused:
                    await sub.subscribe(_NS, name, wait_response=True)
                assert (refused.value.error_code, refused.value.reason) == (
                    wire, f"refused {name}")
    finally:
        handle.close()
