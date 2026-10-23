"""moq-interop-runner data-plane scenarios: our interop client against our
interop relay, in one process."""
from types import SimpleNamespace

import pytest

from aiomoqt.tools import moq_interop_client as client
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.types import ObjectStatus, RequestErrorCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 15700


async def _run(port, draft, use_quic):
    relay._announced.clear()
    relay._tracks.clear()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    try:
        return await client.test_data_subgroup_basic(
            "localhost", port, "/", use_quic, True, False,
            supported_drafts=draft)
    finally:
        handle.close()
        relay._announced.clear()
        relay._tracks.clear()


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", [14, 16, 18, 20, 22])
async def test_canonical_case_through_the_relay(draft, use_quic):
    port = _BASE_PORT + 2 * draft + (0 if use_quic else 1)
    r = await _run(port, draft, use_quic)
    assert r.passed, r.message
    assert r.message.startswith("15 objects received"), r.message
    assert r.spec_revision == client.DATA_SUBGROUP_BASIC_REVISION
    transport = "quic" if use_quic else "webtransport-h3"
    for role in ("publisher", "subscriber"):
        assert r.sessions[role]["moqt_version"] == f"moqt-{draft}"
        assert r.sessions[role]["transport"] == transport


@requires_certs
@pytest.mark.asyncio
async def test_an_object_the_relay_drops_fails_the_case(monkeypatch):
    forward = relay._RelayedTrack._forward_one

    async def drop_1_3(self, session, alias, gid, sgid, oid, *args, **kw):
        if (gid, oid) != (1, 3):
            await forward(self, session, alias, gid, sgid, oid, *args, **kw)

    monkeypatch.setattr(relay._RelayedTrack, "_forward_one", drop_1_3)
    r = await _run(_BASE_PORT + 1, 18, True)
    assert not r.passed
    assert r.message.startswith("missing object 1.3 "), r.message


def _canonical():
    return [(g, 0, o, 128, ObjectStatus.NORMAL, b"t" * (64 if o == 0 else 32))
            for g in range(3) for o in range(5)]


def test_verifier_accepts_the_canonical_set():
    assert client._data_problems(_canonical()) == []


def test_verifier_ignores_status_objects():
    eog = (0, 0, 5, 128, ObjectStatus.END_OF_GROUP, b"")
    assert client._data_problems(_canonical() + [eog]) == []


def test_verifier_reports_each_deviation():
    objs = _canonical()
    del objs[5]                                                 # 1.0
    objs[1] = (0, None, 1, 128, ObjectStatus.NORMAL, b"t" * 32)  # datagram
    objs[2] = (0, 0, 2, 200, ObjectStatus.NORMAL, b"t" * 32)
    objs[3] = (0, 0, 3, 128, ObjectStatus.NORMAL, b"t" * 31)
    objs[4] = (0, 0, 4, 128, ObjectStatus.NORMAL, b"u" * 32)
    objs += [objs[0], (3, 0, 0, 128, ObjectStatus.NORMAL, b"t" * 64)]
    assert client._data_problems(objs) == [
        "object 0.1 on subgroup None, expected 0",
        "object 0.2 priority 200, expected 128",
        "object 0.3 payload 31 bytes, expected 32",
        "object 0.4 payload bytes are not all 0x74",
        "duplicate object 0.0",
        "unexpected object 3.0",
        "missing object 1.0",
    ]


def test_tap_yaml_block():
    rep = client.TAPReporter(aiomoqt_version="1.2.3")
    rep.add(client.TestResult(
        name="data-subgroup-basic", passed=False, duration_ms=12.6,
        message='missing object 1.3: "x"', spec_revision=1,
        sessions={"publisher": {"moqt_version": "moqt-18",
                                "transport": "quic"},
                  "subscriber": {}}))
    lines = rep.report().splitlines()
    i = lines.index("not ok 1 - data-subgroup-basic")
    assert lines[i + 1:] == [
        "  ---",
        "  duration_ms: 13",
        '  implementation_version: "1.2.3"',
        "  test_spec_revision: 1",
        "  sessions:",
        "    publisher:",
        '      moqt_version: "moqt-18"',
        '      transport: "quic"',
        '  message: "missing object 1.3: \\"x\\""',
        "  ...",
    ]


class _FakeSession:
    """Just enough of a session for _DataPublisher."""
    negotiated_draft = 18

    def __init__(self):
        self.cancel, self.done, self._sid = {}, [], 3

    def subscribe_ok(self, request_msg):
        return SimpleNamespace(track_alias=0)

    def register_request_cancel_handler(self, request_id, callback):
        self.cancel[request_id] = callback

    async def open_uni_stream(self):
        self._sid += 4
        return self._sid

    def stream_write(self, sid, data, end_stream=False):
        pass

    async def stream_write_drain(self, sid, data):
        pass

    def subscribe_done(self, **kw):
        self.done.append(kw)


async def _publish(cancel_first):
    pub = client._DataPublisher((b"ns",), b"track")
    s = _FakeSession()
    await pub.on_subscribe(s, SimpleNamespace(
        track_namespace=(b"ns", *(f.encode() for f in client.DATA_SUBGROUP_BASIC_CASE)),
        track_name=b"track", request_id=7,
        forward=None))
    if cancel_first:
        s.cancel[7](7)
    return pub, s, await pub.send(s)


@pytest.mark.asyncio
async def test_publisher_sends_publish_done_with_its_stream_count():
    pub, s, streams = await _publish(cancel_first=False)
    assert streams == 3 and pub.done_sent
    assert [d["stream_count"] for d in s.done] == [3]


@pytest.mark.asyncio
async def test_a_cancel_after_publish_done_is_not_a_failure():
    # Ending the request stream once PUBLISH_DONE is out is cleanup.
    pub, s, _ = await _publish(cancel_first=False)
    s.cancel[7](7)
    assert pub.done_sent


@pytest.mark.asyncio
async def test_publisher_skips_publish_done_once_the_relay_cancels():
    # A relay that ends the upstream subscription leaves no request
    # stream for PUBLISH_DONE; the case reports that instead of crashing.
    pub, s, streams = await _publish(cancel_first=True)
    assert streams == 3 and not pub.done_sent and s.done == []


@requires_certs
@pytest.mark.asyncio
async def test_a_relay_that_withholds_publish_done_fails_the_case(monkeypatch):
    # libquicr never forwards PUBLISH_DONE downstream.
    monkeypatch.setattr(relay._RelayedTrack, "_enqueue_terminal",
                        lambda self, *args: None)
    monkeypatch.setattr(client, "_DATA_DELIVERY_S", 1.0)
    r = await _run(_BASE_PORT + 3, 18, True)
    assert not r.passed
    assert r.message.startswith("no downstream PUBLISH_DONE"), r.message


@pytest.mark.asyncio
async def test_a_test_that_raises_fails_and_the_run_continues(monkeypatch):
    async def _raises(*args, **kw):
        raise AttributeError("boom")

    async def _passes(*args, **kw):
        return client.TestResult(name="passes", passed=True)

    monkeypatch.setattr(client, "TEST_FUNCTIONS",
                        {"raises": _raises, "passes": _passes})
    rep = await client.run_tests(["raises", "passes"], "localhost", 1, "/",
                                 True, True, False, supported_drafts=18)
    assert [(r.name, r.passed) for r in rep.results] == [
        ("raises", False), ("passes", True)]
    assert "AttributeError" in rep.results[0].message


@requires_certs
@pytest.mark.asyncio
async def test_the_case_rides_below_a_moq_test_routing_prefix(monkeypatch):
    # PUBLISH_NAMESPACE carries ("moq-test", name, run id); the
    # upstream SUBSCRIBE carries that prefix plus the 16-field MoQT Test tuple.
    seen = []
    on_subscribe = client._DataPublisher.on_subscribe

    async def spy(self, session, msg):
        seen.append(tuple(bytes(f) for f in msg.track_namespace))
        await on_subscribe(self, session, msg)
    monkeypatch.setattr(client._DataPublisher, "on_subscribe", spy)
    announced = []
    on_ns = relay._on_publish_namespace

    async def ns_spy(session, msg):
        announced.append(relay._ns_tuple(msg.namespace))
        return await on_ns(session, msg)
    monkeypatch.setattr(relay, "_on_publish_namespace", ns_spy)
    r = await _run(_BASE_PORT + 3, 18, True)
    assert r.passed, r.message
    (prefix,) = announced
    assert prefix[:2] == (b"moq-test", b"data-subgroup-basic") and len(prefix) == 3
    (ns,) = seen
    assert ns[:3] == prefix and len(ns) == 19
    assert ns[3:] == tuple(f.encode() for f in client.DATA_SUBGROUP_BASIC_CASE)


@pytest.mark.parametrize("suffix", [
    ("moq-test-00", "1", "0", "0", "2", "4", "5", "64", "32", "2", "1", "1",
     "0", "-1", "-1", "0"),                       # one subgroup per object
    ("moq-test-00", "0", "0", "0", "2", "4", "5", "64", "32", "2", "1", "1",
     "1", "-1", "-1", "0"),                       # End of Group markers
    ("not-moq-test",),
])
@pytest.mark.asyncio
async def test_a_case_this_publisher_cannot_serve_is_refused(suffix):
    prefix = (b"moq-test", b"data-subgroup-basic", b"run")
    pub = client._DataPublisher(prefix, b"track")
    refused = []
    session = SimpleNamespace(
        subscribe_error=lambda **kw: refused.append(kw),
        subscribe_ok=lambda **kw: pytest.fail("accepted"))
    msg = SimpleNamespace(request_id=4, track_name=b"track",
                          track_namespace=(*prefix, *(f.encode() for f in suffix)))
    await pub.on_subscribe(session, msg)
    assert refused and refused[0]["request_id"] == 4
    assert pub.request is None


async def _run_case(case, port, draft, use_quic):
    relay._announced.clear()
    relay._tracks.clear()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    try:
        return await case("localhost", port, "/", use_quic, True, False,
                          supported_drafts=draft)
    finally:
        await server.close_sessions()
        handle.close()
        relay._announced.clear()
        relay._tracks.clear()


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", [14, 16, 18, 20, 22])
@pytest.mark.parametrize("name", ["fetch", "join"])
async def test_history_cases_deliver_objects(name, draft, use_quic):
    port = (_BASE_PORT + 100 + 2 * draft + (0 if use_quic else 1)
            + (0 if name == "fetch" else 50))
    r = await _run_case(client.TEST_FUNCTIONS[name], port, draft, use_quic)
    assert r.passed, r.message
    n = int(r.message.split()[0])
    assert n > 0, r.message


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["fetch", "join"])
async def test_history_case_without_objects_fails(monkeypatch, name):
    # A structured refusal fails: the case needs objects.
    async def refuse(session, msg):
        session.fetch_error(msg.request_id, RequestErrorCode.DOES_NOT_EXIST,
                            "no history")

    monkeypatch.setattr(relay, "_on_fetch", refuse)
    port = _BASE_PORT + 99 + (0 if name == "fetch" else 1)
    r = await _run_case(client.TEST_FUNCTIONS[name], port, 18, True)
    assert not r.passed, r.message
