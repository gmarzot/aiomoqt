"""moq-test-* scenarios (draft-afrind-moq-test): our interop client against
aiomoqt-relay, in one process, plus the scoreboard on its own."""
from types import SimpleNamespace

import pytest

from aiomoqt.messages import RequestError
from aiomoqt.tools import moq_interop_client as client
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tools.moqtest_origin import parse_namespace
from aiomoqt.types import (ObjectStatus, ParamType, RequestErrorCode,
                           SubscribeDoneCode)

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 15800

# moq-rs moq-test-client names: the matrix compares the same scenarios.
_NAMES = [
    "moq-test-subgroup-per-group-eog", "moq-test-subgroup-per-object",
    "moq-test-two-subgroups-eog", "moq-test-datagram",
    "moq-test-datagram-eog", "moq-test-extensions", "moq-test-increments",
    "moq-test-forward-zero",
]


async def _serve(port, draft, use_quic):
    relay._announced.clear()
    relay._tracks.clear()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    return await server.serve()


async def _run(name, port, draft, use_quic):
    return await client.run_moq_test_case(
        name, "localhost", port, "/", use_quic, True, False,
        supported_drafts=draft)


def test_the_cases_are_the_standard_run():
    assert list(client.MOQ_TEST_CASES) == _NAMES
    assert client.STANDARD_TESTS[-len(_NAMES):] == _NAMES
    for name in _NAMES:
        assert name in client.TEST_FUNCTIONS


@pytest.mark.parametrize("name", _NAMES)
def test_tuple_parses_as_moq_test(name):
    case = client.MOQ_TEST_CASES[name]
    p = parse_namespace(case.fields())
    assert (p.fp, p.start_group, p.start_object, p.last_group,
            p.last_object, p.objects_per_group, p.size0, p.size_gt0,
            p.freq_ms, p.g_inc, p.o_inc, p.markers, p.int_ext, p.var_ext,
            p.pub_timeout) == (
        case.fp, case.start_group, case.start_object, case.last_group,
        case.last_object, case.objects_per_group, case.size0,
        case.size_rest, case.freq_ms, case.g_inc, case.o_inc, case.eog,
        case.int_ext, case.var_ext, 0)
    # An End of Group marker sits on the object series.
    if case.eog:
        assert case.last_object in case.objects()
    # Field 6 bounds the data objects in a group.
    data = len(case.objects()) - (1 if case.eog else 0)
    assert data <= case.objects_per_group


def test_expected_sets():
    c = client.MOQ_TEST_CASES
    assert c["moq-test-increments"].expected_data() == {
        (g, o) for g in (10, 17, 24) for o in (3, 5, 7)}
    two = c["moq-test-two-subgroups-eog"]
    assert two.expected_markers() == {(0, 6), (1, 6), (2, 6)}
    assert [two.subgroup(o) for o in two.objects()] == [0, 1, 0, 1, 0, 1, 0]
    assert [c[n].stream_count() for n in _NAMES] == [3, 8, 6, 0, 0, 3, 3, 0]
    assert c["moq-test-extensions"].extension_ids() == {2, 5}


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", [18, 20, 22])
async def test_every_case_through_the_relay(draft, use_quic):
    port = _BASE_PORT + 2 * draft + (0 if use_quic else 1)
    handle = await _serve(port, draft, use_quic)
    try:
        for name in _NAMES:
            r = await _run(name, port, draft, use_quic)
            assert r.passed and not r.skipped, f"{name}: {r.message}"
            for role in ("publisher", "subscriber"):
                assert r.sessions[role]["moqt_version"] == f"moqt-{draft}"
    finally:
        handle.close()


@requires_certs
@pytest.mark.asyncio
async def test_skipped_below_draft_18():
    handle = await _serve(_BASE_PORT + 1, 16, True)
    try:
        r = await _run("moq-test-datagram", _BASE_PORT + 1, 16, True)
    finally:
        handle.close()
    assert r.skipped and r.passed
    assert "negotiated draft-16" in r.skip_reason


@requires_certs
@pytest.mark.asyncio
async def test_an_object_the_relay_drops_fails_the_case(monkeypatch):
    forward = relay._RelayedTrack._forward_one

    async def drop_1_3(self, session, alias, gid, sgid, oid, *args, **kw):
        if (gid, oid) != (1, 3):
            await forward(self, session, alias, gid, sgid, oid, *args, **kw)

    monkeypatch.setattr(relay._RelayedTrack, "_forward_one", drop_1_3)
    handle = await _serve(_BASE_PORT + 2, 18, True)
    try:
        r = await _run("moq-test-subgroup-per-group-eog", _BASE_PORT + 2,
                       18, True)
    finally:
        handle.close()
    assert not r.passed
    assert r.message.startswith("missing 1 objects (first 5): (1, 3)"), \
        r.message


@requires_certs
@pytest.mark.asyncio
async def test_a_refused_publish_is_a_prompt_skip(monkeypatch):
    async def refuse(session, msg):
        session._send_reply(msg.request_id, RequestError(
            request_id=msg.request_id,
            error_code=int(RequestErrorCode.NOT_SUPPORTED),
            retry_interval=0, reason="PUBLISH is not supported"))

    monkeypatch.setattr(relay, "_on_publish", refuse)
    handle = await _serve(_BASE_PORT + 3, 18, True)
    try:
        r = await _run("moq-test-datagram", _BASE_PORT + 3, 18, True)
    finally:
        handle.close()
    assert r.skipped and r.skip_reason == "relay does not support PUBLISH"
    # The relay still holds the SUBSCRIBE; its rendezvous window is 5 s.
    assert r.duration_ms < 2000, r.duration_ms


@pytest.mark.asyncio
async def test_an_update_handled_before_the_reply_is_the_newer_forward():
    """moqx answers PUBLISH with Forward State 0, then a REQUEST_UPDATE to 1
    on the same stream; the update's handler can run first."""
    pub = client._MoqTestPublisher(client.MOQ_TEST_CASES["moq-test-datagram"])
    sent = []
    session = SimpleNamespace(_send_reply=lambda rid, msg: sent.append(rid))
    await pub._on_request_update(session, SimpleNamespace(
        request_id=4, parameters={ParamType.FORWARD: 1}))
    pub.take_reply(SimpleNamespace(forward=None,
                                   parameters={ParamType.FORWARD: 0}))
    assert pub.forward == 1 and pub.replied.is_set() and sent == [4]

    pub = client._MoqTestPublisher(client.MOQ_TEST_CASES["moq-test-datagram"])
    pub.take_reply(SimpleNamespace(forward=None,
                                   parameters={ParamType.FORWARD: 0}))
    assert pub.forward == 0


def _obj(gid, oid, payload=b"", status=ObjectStatus.NORMAL, priority=128,
         extensions=None, end_of_group=False):
    return SimpleNamespace(group_id=gid, object_id=oid, payload=payload,
                           status=status, publisher_priority=priority,
                           extensions=extensions, end_of_group=end_of_group)


def _feed(board, case, skip=(), extra=None):
    """The tuple's whole object set, as a conformant relay delivers it."""
    for g in case.groups():
        for o in case.objects():
            if (g, o) in skip:
                continue
            sg = case.subgroup(o)
            if case.is_marker(o):
                board.on_object(_obj(g, o, status=ObjectStatus.END_OF_GROUP),
                                0, 0, g, sg)
            else:
                ext = {i: b"x" if i % 2 else 7 for i in case.extension_ids()}
                board.on_object(_obj(g, o, b"t" * case.size(o),
                                     extensions=ext or None), 0, 0, g, sg)
    for _ in range(case.stream_count()):
        board.on_stream_end(0, 0)
    board.on_done(SimpleNamespace(status_code=SubscribeDoneCode.TRACK_ENDED,
                                  stream_count=case.stream_count()))


@pytest.mark.parametrize("name", _NAMES)
def test_scoreboard_accepts_the_tuples_set(name):
    case = client.MOQ_TEST_CASES[name]
    board = client._MoqTestScoreboard(case)
    if not case.forward_zero:
        _feed(board, case)
    else:
        board.on_done(SimpleNamespace(
            status_code=SubscribeDoneCode.TRACK_ENDED, stream_count=0))
    assert board.verdict() == []


def test_scoreboard_reports_each_deviation():
    case = client.MOQ_TEST_CASES["moq-test-extensions"]
    board = client._MoqTestScoreboard(case)
    board.on_object(_obj(0, 0, b"t" * 64, priority=200,
                         extensions={2: 1, 5: b"x"}), 0, 0, 0, 0)
    board.on_object(_obj(0, 1, b"t" * 32, extensions={2: 1}), 0, 0, 0, 0)
    board.on_object(_obj(0, 2, b"t" * 32, extensions={2: 1, 5: b"x"}),
                    0, 0, 0, 1)
    board.on_object(_obj(0, 3, b"t" * 31, extensions={2: 1, 5: b"x"}),
                    0, 0, 0, 0)
    board.on_object(_obj(0, 5, status=ObjectStatus.END_OF_TRACK), 0, 0, 0, 0)
    board.on_object(_obj(0, 4, b"t" * 32, extensions={2: 1, 5: b"x"}),
                    0, 0, 0, 0)
    board.on_object(_obj(0, 4, b"t" * 32, extensions={2: 1, 5: b"x"}),
                    0, 0, 0, 0)
    board.on_done(SimpleNamespace(status_code=SubscribeDoneCode.TRACK_ENDED,
                                  stream_count=2))
    problems = board.verdict()
    assert problems[:6] == [
        "object (0, 0) priority 200, expected 128",
        "object (0, 1) extensions [2], expected [2, 5]",
        "object (0, 2) on subgroup 1, expected 0",
        "object (0, 3) payload 31 bytes, expected 32",
        "status 4 at (0, 5), expected END_OF_GROUP",
        "duplicate object (0, 4)",
    ]
    assert problems[6].startswith("missing 10 objects (first 5): (1, 0)")
    assert "missing End of Group marker at (0, 5)" in problems
    assert problems[-1] == "PUBLISH_DONE Stream Count 2, expected 3"


def test_scoreboard_forward_zero_fails_on_data():
    case = client.MOQ_TEST_CASES["moq-test-forward-zero"]
    board = client._MoqTestScoreboard(case)
    board.on_object(_obj(0, 0, b"t" * 64), 0, 0, 0, 0)
    board.on_done(SimpleNamespace(status_code=SubscribeDoneCode.TRACK_ENDED,
                                  stream_count=1))
    assert board.verdict() == [
        "FORWARD=0, but 1 objects were forwarded",
        "PUBLISH_DONE Stream Count 1, expected 0",
    ]


def test_scoreboard_datagram_checks():
    case = client.MOQ_TEST_CASES["moq-test-datagram-eog"]
    board = client._MoqTestScoreboard(case)
    board.on_object(_obj(0, 0, b"t" * 64, end_of_group=True), 0, 0, None,
                    None)
    board.on_object(_obj(0, 1, b"t" * 32), 0, 0, 0, 0)
    assert board.problems == [
        "datagram (0, 0) has the end-of-group type bit",
        "object (0, 1) arrived on subgroup 0, expected a datagram",
    ]
