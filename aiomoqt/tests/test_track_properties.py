"""Track Properties (§12.5-12.7): restricted values are checked, also
inside Immutable Properties, and unknown types pass. A d18 REQUEST_OK
carries them only as a TRACK_STATUS reply (§10.5)."""
import asyncio

import pytest

from aiomoqt.context import profile_for
from aiomoqt.messages import MOQTMessage
from aiomoqt.messages.request import RequestOk
from aiomoqt.types import MOQTProtocolViolation, SessionCloseCode
from aiomoqt.utils.buffer import Buffer

from aiomoqt.tests.test_control_fragmentation import (
    _control_session, _feed_reply,
)

D18 = profile_for(18)


def _kvps(props) -> bytes:
    """Key-Value-Pairs as an Immutable Properties value carries them."""
    buf = Buffer(capacity=256, vi64=True)
    MOQTMessage._extensions_encode(buf, props, with_length=False, delta=True)
    return bytes(buf.data_slice(0, buf.tell()))


def _decode(raw: bytes) -> RequestOk:
    buf = Buffer(data=raw, vi64=True)
    buf.pull_vint()
    length = buf.pull_uint16()
    return RequestOk.deserialize(buf, prof=D18, buf_end=buf.tell() + length)


def test_d18_request_ok_round_trips_track_properties():
    props = {0x22: 2, 0x9D: b"x"}
    raw = bytes(RequestOk(parameters={}, track_properties=props)
                .serialize(prof=D18).data)
    assert _decode(raw).track_properties == props


@pytest.mark.parametrize("props", [
    {0x22: 3},
    {0x22: 0},
    {0x30: 2},
    {0x0B: _kvps({0x22: 3})},
    {0x0B: _kvps({0x0B: b""})},
    {0x0B: b"\x80"},
], ids=["group-order-3", "group-order-0", "dynamic-groups-2",
        "immutable-group-order-3", "immutable-in-immutable",
        "immutable-unparsable"])
def test_invalid_track_properties_are_a_protocol_violation(props):
    with pytest.raises(MOQTProtocolViolation):
        MOQTMessage._check_track_properties(props, prof=D18)


def test_unknown_track_properties_pass():
    MOQTMessage._check_track_properties(
        {0x00: 0, 0x01: b"", 0x22: 1, 0x9D: b"x", 0x11C: 5,
         0x0B: _kvps({0x30: 1, 0x9D: b"y"})}, prof=D18)


def _ok_with(s, props) -> bytes:
    return bytes(RequestOk(parameters={}, track_properties=props)
                 .serialize(prof=s._profile).data)


async def _settle():
    for _ in range(3):
        await asyncio.sleep(0)


async def test_track_properties_close_a_reply_that_is_not_track_status():
    s = _control_session(18)
    _feed_reply(s, _ok_with(s, {0x22: 1}))
    await _settle()
    assert s._closed[0][0] == SessionCloseCode.PROTOCOL_VIOLATION


async def test_a_track_status_reply_keeps_its_track_properties():
    s = _control_session(18)
    s._track_status_requests.add(7)
    reply = asyncio.get_running_loop().create_future()
    s._pending_requests[7] = reply
    _feed_reply(s, _ok_with(s, {0x22: 2, 0x9D: b"x"}))
    got = await asyncio.wait_for(reply, 1)
    assert got.track_properties == {0x22: 2, 0x9D: b"x"}
    assert s._closed == [] and 7 not in s._track_status_requests


async def test_an_invalid_track_status_property_closes_the_session():
    s = _control_session(18)
    s._track_status_requests.add(7)
    _feed_reply(s, _ok_with(s, {0x0B: _kvps({0x22: 3})}))
    await _settle()
    assert s._closed[0][0] == SessionCloseCode.PROTOCOL_VIOLATION
