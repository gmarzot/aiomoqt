"""Request/response lifecycle coverage.

Exercises request-id allocation, the `_resolve_request` classification
(the fire-and-forget / duplicate / genuinely-unsolicited distinction),
`_await_response` success / error / timeout / race, and the
`publish` / `publish_namespace` send paths across `wait_response=True`
(blocking) vs the fire-and-forget default.

Regression coverage for the "unsolicited response for request_id=N:
RequestOk" warning: a fire-and-forget publish's correct, single ack from a
protocol-compliant peer (e.g. moxx) must be recognized as expected traffic
(DEBUG), never warned as unsolicited. Also fixes/guards the latent race
where a fast `publish(wait_response=True)` ack could be dropped because the
future was not pre-registered before the send.
"""
import asyncio
import logging
from collections import deque

import pytest

from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.messages.request import RequestOk, RequestError
from aiomoqt.types import MOQTRequestError
from aiomoqt.context import profile_for
from aiomoqt.utils.buffer import Buffer

_PLOG = "aiomoqt.protocol"


def _session(is_client=True):
    """Minimal session instance for exercising the request/response
    lifecycle in isolation. Bypasses __init__ (no QUIC/transport); sets
    only the attributes the tested methods touch and stubs _send_request
    to record (request_id, msg) rather than hit the wire."""
    s = object.__new__(_MOQTSessionMixin)
    s._next_request_id = 0 if is_client else 1
    s._sent_requests = deque(maxlen=1024)
    s._pending_requests = {}
    s._next_track_alias = 0
    s._track_aliases = {}
    s._loop = asyncio.get_running_loop()
    s._sent = []
    s._send_request = lambda rid, msg: s._sent.append((rid, msg))
    return s


@pytest.fixture
def plog(caplog):
    """Capture aiomoqt.protocol records despite propagate=False."""
    lg = logging.getLogger(_PLOG)
    lg.addHandler(caplog.handler)
    prev = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        yield caplog
    finally:
        lg.setLevel(prev)
        lg.removeHandler(caplog.handler)


def _levels(caplog, needle):
    return [r.levelno for r in caplog.records if needle in r.getMessage()]


# --- _allocate_request_id -------------------------------------------------

async def test_allocate_client_even_and_recorded():
    s = _session(is_client=True)
    ids = [s._allocate_request_id() for _ in range(3)]
    assert ids == [0, 2, 4]                       # client: even, +2
    assert list(s._sent_requests) == [0, 2, 4]    # each recorded as issued


async def test_allocate_server_odd():
    s = _session(is_client=False)
    assert [s._allocate_request_id() for _ in range(2)] == [1, 3]


# --- _resolve_request classification (the fix) ----------------------------

async def test_resolve_live_future_no_warning(plog):
    s = _session()
    rid = s._allocate_request_id()
    fut = s._loop.create_future()
    s._pending_requests[rid] = fut
    ok = RequestOk(request_id=rid)
    s._resolve_request(rid, ok)
    assert fut.done() and fut.result() is ok
    assert not any(r.levelno >= logging.WARNING for r in plog.records)


async def test_resolve_fire_and_forget_ack_is_debug_not_warning(plog):
    # THE regression: an ack for an id we issued but chose not to await
    # (fire-and-forget publish). A compliant peer sends exactly one OK.
    s = _session()
    rid = s._allocate_request_id()               # issued, no future
    s._resolve_request(rid, RequestOk(request_id=rid))
    assert logging.WARNING not in _levels(plog, f"request_id={rid}")
    assert logging.DEBUG in _levels(plog, f"request_id={rid}")


async def test_resolve_never_issued_id_warns(plog):
    s = _session()
    s._resolve_request(777, RequestOk(request_id=777))   # never allocated
    assert logging.WARNING in _levels(plog, "request_id=777")


async def test_resolve_duplicate_after_resolution_is_debug(plog):
    s = _session()
    rid = s._allocate_request_id()
    s._pending_requests[rid] = s._loop.create_future()
    s._resolve_request(rid, RequestOk(request_id=rid))   # first: resolves
    s._pending_requests.pop(rid, None)                   # awaiter pops
    plog.clear()
    s._resolve_request(rid, RequestOk(request_id=rid))   # duplicate/late
    assert logging.WARNING not in _levels(plog, f"request_id={rid}")
    assert logging.DEBUG in _levels(plog, f"request_id={rid}")


# --- _await_response: success / error / timeout / race --------------------

async def test_await_success_returns_ok():
    s = _session()
    rid = s._allocate_request_id()
    s._pending_requests[rid] = s._loop.create_future()
    ok = RequestOk(request_id=rid)
    s._loop.call_soon(s._resolve_request, rid, ok)
    resp = await s._await_response(rid, timeout=1.0)
    assert resp is ok
    assert rid not in s._pending_requests            # popped in finally


async def test_await_error_response_raises():
    s = _session()
    rid = s._allocate_request_id()
    s._pending_requests[rid] = s._loop.create_future()
    err = RequestError(request_id=rid, error_code=5,
                       reason="nope", retry_interval=0)
    s._loop.call_soon(s._resolve_request, rid, err)
    with pytest.raises(MOQTRequestError):
        await s._await_response(rid, timeout=1.0)
    assert rid not in s._pending_requests


async def test_await_timeout_raises():
    s = _session()
    rid = s._allocate_request_id()               # nothing ever resolves it
    with pytest.raises(MOQTRequestError):
        await s._await_response(rid, timeout=0.05)


async def test_await_resolved_before_await_no_race(plog):
    # Ack arrives BEFORE the caller awaits: the pre-registered future must
    # already hold the result (else it would time out).
    s = _session()
    rid = s._allocate_request_id()
    s._pending_requests[rid] = s._loop.create_future()
    ok = RequestOk(request_id=rid)
    s._resolve_request(rid, ok)                  # resolve synchronously
    resp = await s._await_response(rid, timeout=1.0)
    assert resp is ok
    assert logging.WARNING not in _levels(plog, f"request_id={rid}")


# --- publish_namespace / publish: wait_response vs fire-and-forget --------

async def test_publish_namespace_fire_and_forget_no_future(plog):
    s = _session()
    msg = s.publish_namespace("live/x")          # default wait_response=False
    assert type(msg).__name__ == "PublishNamespace"
    rid = msg.request_id
    assert rid not in s._pending_requests        # no future registered
    assert (rid, msg) in s._sent                 # but actually sent
    s._resolve_request(rid, RequestOk(request_id=rid))   # peer's correct ack
    assert logging.WARNING not in _levels(plog, f"request_id={rid}")


async def test_publish_namespace_wait_response_preregisters():
    s = _session()
    coro = s.publish_namespace("live/x", wait_response=True)
    rid = s._sent_requests[-1]
    assert rid in s._pending_requests            # future pre-registered
    coro.close()                                 # don't actually await


async def test_publish_fire_and_forget_ack_not_warned(plog):
    s = _session()
    msg = s.publish("live/x", "track")           # default fire-and-forget
    rid = msg.request_id
    assert rid not in s._pending_requests
    s._resolve_request(rid, RequestOk(request_id=rid))
    assert logging.WARNING not in _levels(plog, f"request_id={rid}")


async def test_publish_wait_response_preregisters_before_send():
    # The fix: publish now pre-registers the future when wait_response=True
    # (it previously did not — a latent loopback race).
    s = _session()
    coro = s.publish("live/x", "track", wait_response=True)
    rid = s._sent_requests[-1]
    assert rid in s._pending_requests
    coro.close()


async def test_publish_wait_response_resolves_even_if_ack_first():
    # Race: the ack resolves the request before the caller awaits the
    # returned coroutine — pre-registration makes this safe.
    s = _session()
    coro = s.publish("live/x", "track", wait_response=True)
    rid = s._sent_requests[-1]
    s._resolve_request(rid, RequestOk(request_id=rid))   # ack before await
    resp = await coro
    assert resp.request_id == rid


# --- per-draft correlation: where the Request ID lives + how it correlates
# The resolution flow must bridge a real wire difference: d14/d16 carry the
# Request ID in-band in the reply; d18 omits it and correlates the reply to
# its request by the request *stream* it arrives on.

def _drive_session(draft):
    s = _session()
    s.negotiated_draft = draft
    s._profile = profile_for(draft)
    s._control_msg_overrides = {}
    s._tasks = set()
    return s


@pytest.mark.parametrize("draft", [14, 16, 18])
def test_request_ok_request_id_on_wire_per_draft(draft):
    prof = profile_for(draft)
    wire = bytes(RequestOk(request_id=7, parameters={}).serialize(prof=prof).data)
    payload = wire[3:]  # skip type(1B varint) + length(2B) header
    got = RequestOk.deserialize(Buffer(data=payload), prof=prof,
                                buf_end=len(payload))
    if prof.reply_has_request_id:      # d14 / d16 — in-band
        assert got.request_id == 7
    else:                              # d18 — omitted, stream-correlated
        assert got.request_id is None


@pytest.mark.parametrize("draft", [14, 16, 18])
async def test_response_resolves_correct_future_per_draft(draft):
    # End-to-end through the real dispatch path: a peer RequestOk resolves
    # the matching pending future via the in-band id (d14/d16) or the
    # stream-injected id (d18).
    s = _drive_session(draft)
    rid = s._allocate_request_id()
    fut = s._loop.create_future()
    s._pending_requests[rid] = fut
    wire = bytes(RequestOk(request_id=rid, parameters={}).serialize(
        prof=s._profile).data)
    stream_rid = None if s._profile.reply_has_request_id else rid
    s._moqt_handle_control_message(Buffer(data=wire), request_id=stream_rid)
    await asyncio.sleep(0)  # let the scheduled handler task run
    assert fut.done() and fut.result().request_id == rid


async def test_d18_reply_without_stream_binding_cannot_correlate(plog):
    # d18 correlation depends entirely on the stream→id binding. With no
    # binding (request_id=None), the reply carries no id and cannot resolve
    # any request — documents the d18 contract.
    s = _drive_session(18)
    rid = s._allocate_request_id()
    fut = s._loop.create_future()
    s._pending_requests[rid] = fut
    wire = bytes(RequestOk(request_id=rid, parameters={}).serialize(
        prof=s._profile).data)
    s._moqt_handle_control_message(Buffer(data=wire), request_id=None)
    await asyncio.sleep(0)
    assert not fut.done()   # unresolved — correlation impossible
