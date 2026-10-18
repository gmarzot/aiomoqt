"""Relay RENDEZVOUS_TIMEOUT (d18 §10.2.6): a SUBSCRIBE for a track with no
publisher is held up to the requested time, then answered TIMEOUT."""
import asyncio
import contextlib
import time

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.track import PublishedTrack
from aiomoqt.tools import moq_interop_client as interop_client
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.types import MOQTMessageType, MOQTRequestError, RequestErrorCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

_BASE_PORT = 15810


def _client(port, draft):
    return MOQTClient("localhost", port, path="/", use_quic=True,
                      verify_tls=False, supported_drafts=draft)


async def _announce_after(port, draft, delay):
    """Publisher announcing the namespace after `delay`, serving the
    relay's SUBSCRIBE."""
    await asyncio.sleep(delay)
    pub = _client(port, draft)

    async def on_subscribe(session, msg):
        session.subscribe_ok(request_msg=msg)
    pub.register_handler(MOQTMessageType.SUBSCRIBE, on_subscribe)
    async with pub.connect() as session:
        await session.client_session_init()
        await session.publish_namespace(namespace="rv/ns", parameters={},
                                        wait_response=True)
        await asyncio.sleep(5)


async def _subscribe(port, draft, params, publish_after=None):
    """(error code or None for SUBSCRIBE_OK, seconds to the answer)."""
    relay._announced.clear()
    relay._tracks.clear()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=True, draft=draft)
    handle = await server.serve()
    pub = None
    try:
        if publish_after is not None:
            pub = asyncio.create_task(
                _announce_after(port, draft, publish_after))
        async with _client(port, draft).connect() as session:
            await session.client_session_init()
            t0 = time.monotonic()
            try:
                await session.subscribe(namespace="rv/ns", track_name="t",
                                        parameters=params,
                                        wait_response=True)
                code = None
            except MOQTRequestError as e:
                code = int(e.error_code)
            return code, time.monotonic() - t0
    finally:
        if pub is not None:
            pub.cancel()
        handle.close()
        relay._announced.clear()
        relay._tracks.clear()


@pytest.mark.asyncio
async def test_no_publisher_within_the_timeout_is_answered_timeout():
    code, waited = await _subscribe(_BASE_PORT, 18,
                                    {relay.RENDEZVOUS_TIMEOUT: 500})
    assert code == RequestErrorCode.TIMEOUT
    assert 0.45 <= waited < 3.0, waited


@pytest.mark.asyncio
async def test_a_publisher_arriving_in_time_is_subscribed():
    code, waited = await _subscribe(_BASE_PORT + 1, 18,
                                    {relay.RENDEZVOUS_TIMEOUT: 3000},
                                    publish_after=0.3)
    assert code is None
    assert waited < 2.5, waited


@pytest.mark.asyncio
async def test_without_the_parameter_the_answer_is_immediate():
    code, waited = await _subscribe(_BASE_PORT + 2, 18, {})
    assert code == RequestErrorCode.DOES_NOT_EXIST
    assert waited < 0.4, waited


@pytest.mark.asyncio
async def test_parameter_0x04_before_d18_is_not_a_rendezvous():
    code, waited = await _subscribe(_BASE_PORT + 3, 16,
                                    {relay.RENDEZVOUS_TIMEOUT: 500})
    assert code == RequestErrorCode.DOES_NOT_EXIST
    assert waited < 0.4, waited


async def _client_case(port, draft):
    relay._announced.clear()
    relay._tracks.clear()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=True, draft=draft)
    handle = await server.serve()
    try:
        return await interop_client.test_rendezvous_timeout(
            "localhost", port, "/", True, True, False,
            supported_drafts=draft)
    finally:
        handle.close()


@pytest.mark.asyncio
async def test_interop_client_case_passes_at_d18():
    r = await _client_case(_BASE_PORT + 4, 18)
    assert r.passed and not r.skipped, r.message
    assert r.sessions["subscriber"]["moqt_version"] == "moqt-18"


@pytest.mark.asyncio
async def test_interop_client_case_skips_before_d18():
    r = await _client_case(_BASE_PORT + 5, 16)
    assert r.skipped
    assert "draft-16" in r.skip_reason



@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_held_subscriber_has_the_publish_answered_forwarding(
        use_quic, monkeypatch):
    # A PUBLISH arriving for a held SUBSCRIBE starts forwarding in its
    # PUBLISH_OK, with no REQUEST_UPDATE a publisher may not support.
    port = _BASE_PORT + 6 + (not use_quic)
    names = ("video", "audio")
    got = {name: 0 for name in names}
    answered, raised = {}, []
    answer, resume = relay._answer_publish, relay._resume_parked_publish

    def answer_spy(track, forward=None):
        pending = track.pending_publish is not None
        answer(track, forward)
        if pending and track.pending_publish is None:
            answered[bytes(track.key[1])] = track.parked is None

    def resume_spy(track):
        if track.parked is not None:
            raised.append(track.key)
        resume(track)
    monkeypatch.setattr(relay, "_answer_publish", answer_spy)
    monkeypatch.setattr(relay, "_resume_parked_publish", resume_spy)
    relay._announced.clear()
    relay._tracks.clear()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=18)
    handle = await server.serve()
    try:
        async with contextlib.AsyncExitStack() as stack:
            async def _session():
                client = MOQTClient("localhost", port, path="/",
                                    use_quic=use_quic, verify_tls=False,
                                    supported_drafts=18)
                s = await stack.enter_async_context(client.connect())
                await s.client_session_init()
                return s

            def _count(name):
                def on_object(*_args):
                    got[name] += 1
                return on_object

            rx = await _session()
            held = [asyncio.create_task(rx.subscribe(
                "rv/held", name, forward=1,
                parameters={relay.RENDEZVOUS_TIMEOUT: 5000},
                on_object=_count(name), wait_response=True))
                for name in names]
            await asyncio.sleep(0.3)
            pub = await _session()
            for name in names:
                track = PublishedTrack(pub, namespace="rv/held",
                                       trackname=name, object_size=100,
                                       group_size=5, rate=200)
                await track.publish(announce_namespace=False,
                                    publish_track=True)
            await asyncio.gather(*held)
            for _ in range(150):
                if all(got.values()):
                    break
                await asyncio.sleep(0.02)
    finally:
        handle.close()
        relay._announced.clear()
        relay._tracks.clear()
    assert all(got.values()), got
    assert answered == {name.encode(): True for name in names}, answered
    assert raised == []
