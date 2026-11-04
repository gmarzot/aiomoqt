"""moq_interop_client moq-test-* cases: through aiomoqt-relay in one
process, and the verifier against hand-built deliveries."""
from types import SimpleNamespace

import pytest

from aiomoqt.tools import moq_interop_client as client
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tools.moqtest_origin import priority_for
from aiomoqt.types import ObjectStatus, SubscribeDoneCode

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 24100
_NAMES = list(client.MOQ_TEST_CASES)


async def _run_case(name, port, draft, use_quic):
    relay._announced.clear()
    relay._tracks.clear()
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    try:
        return await client.TEST_FUNCTIONS[name](
            "localhost", port, "/", use_quic, True, False,
            supported_drafts=draft)
    finally:
        await server.close_sessions()
        handle.close()
        relay._announced.clear()
        relay._tracks.clear()


def test_cases_run_by_default_under_moq_rs_names():
    assert _NAMES == [
        "moq-test-subgroup-per-group-eog", "moq-test-subgroup-per-object",
        "moq-test-two-subgroups-eog", "moq-test-datagram",
        "moq-test-datagram-eog", "moq-test-extensions",
        "moq-test-increments", "moq-test-forward-zero"]
    assert client.STANDARD_TESTS[-len(_NAMES):] == _NAMES
    assert all(name in client.TEST_FUNCTIONS for name in _NAMES)


@pytest.mark.parametrize("name, counts", [
    ("moq-test-subgroup-per-group-eog", (15, 3, 3)),
    ("moq-test-subgroup-per-object", (8, 0, 8)),
    ("moq-test-two-subgroups-eog", (18, 3, 6)),
    ("moq-test-datagram", (15, 0, 0)),
    ("moq-test-datagram-eog", (15, 3, 0)),
    ("moq-test-extensions", (15, 3, 3)),
    ("moq-test-increments", (9, 0, 3)),
    ("moq-test-forward-zero", (6, 0, 2)),
])
def test_expected_delivery(name, counts):
    p = client.parse_moq_test(("moq-test-00", *client.MOQ_TEST_CASES[name]))
    data, markers, streams = client._moq_test_expected(p)
    assert (len(data), len(markers), streams) == counts


@requires_certs
@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("name", _NAMES)
async def test_case_passes_through_the_relay(name, draft, use_quic):
    port = (_BASE_PORT + 2 * _NAMES.index(name) + (0 if use_quic else 1)
            + (0 if draft == 18 else 20))
    r = await _run_case(name, port, draft, use_quic)
    assert r.passed and not r.skipped, r.message
    assert set(r.sessions) == {"subscriber", "publisher"}
    if name == "moq-test-forward-zero":
        assert r.message == "0 objects with Forward State 0; Stream Count 0"
    else:
        data, markers, streams = client._moq_test_expected(client.parse_moq_test(
            ("moq-test-00", *client.MOQ_TEST_CASES[name])))
        assert r.message == (f"objects {len(data)}/{len(data)}, End of Group "
                             f"{len(markers)}/{len(markers)}, Stream Count "
                             f"{streams}/{streams}")


@requires_certs
@pytest.mark.asyncio
async def test_case_skips_below_draft_18():
    r = await _run_case("moq-test-datagram", _BASE_PORT + 50, 16, True)
    assert r.passed and r.skipped
    assert r.skip_reason == ("RENDEZVOUS_TIMEOUT is draft-18; "
                             "negotiated draft-16")


@requires_certs
@pytest.mark.asyncio
async def test_forward_zero_notes_a_relay_that_takes_the_track(monkeypatch):
    # Forward State 1 upstream is allowed; only delivery downstream fails.
    monkeypatch.setattr(relay, "_forwarding", lambda track: True)
    r = await _run_case("moq-test-forward-zero", _BASE_PORT + 51, 18, True)
    assert r.passed, r.message
    assert r.message == ("0 objects with Forward State 0; Stream Count 0; "
                         "relay took the PUBLISH at Forward State 1")


def _params(name):
    return client.parse_moq_test(("moq-test-00", *client.MOQ_TEST_CASES[name]))


def _delivery(name):
    """What a correct relay delivers for `name`, in generator order."""
    p = _params(name)
    data, markers, _ = client._moq_test_expected(p)
    exts = client._moq_test_extension_ids(p)
    out = []
    for g, o in sorted(data | markers):
        eog = (g, o) in markers
        size = p.size0 if o == p.start_object else p.size_gt0
        out.append((g, client._moq_test_subgroup(p, o), o, priority_for(g),
                    ObjectStatus.END_OF_GROUP if eog else None,
                    b"" if eog else b"t" * size,
                    frozenset() if eog else exts, False))
    return out


def _done(stream_count, status=SubscribeDoneCode.TRACK_ENDED):
    return SimpleNamespace(status_code=status, stream_count=stream_count)


def _problems(name, objects, done=None, ended=None, reset=0):
    p = _params(name)
    forward_zero = name == "moq-test-forward-zero"
    streams = 0 if forward_zero else client._moq_test_expected(p)[2]
    return client._moq_test_problems(
        p, objects, _done(streams) if done is None else done,
        streams if ended is None else ended, reset, forward_zero)


@pytest.mark.parametrize("name", _NAMES[:-1])
def test_verifier_accepts_a_correct_delivery(name):
    assert _problems(name, _delivery(name)) == []


def test_verifier_reports_each_deviation():
    name = "moq-test-extensions"
    objs = _delivery(name)
    bad = [o for o in objs if (o[0], o[2]) not in {(0, 1), (1, 5)}]
    g, sg, o, prio, st, payload, exts, flag = bad[0]
    bad[0] = (g, 1, o, prio + 1, st, payload[:-1], frozenset({2}), flag)
    assert _problems(name, bad, done=_done(2, 0x0), reset=1) == [
        "object 0.0 on subgroup 1, expected 0",
        f"object 0.0 priority {prio + 1}, expected {prio}",
        "object 0.0: 63-byte payload, expected 64 bytes of b't'",
        "object 0.0 extensions [2], expected [2, 5]",
        "missing 1 object(s): 0.1",
        "missing End of Group at 1.5",
        "downstream PUBLISH_DONE status 0, expected TRACK_ENDED",
        "downstream Stream Count 2, expected 3",
        "1 downstream stream(s) reset",
    ]


def test_verifier_checks_per_stream_order_and_datagram_flags():
    objs = _delivery("moq-test-subgroup-per-group-eog")
    objs[0], objs[1] = objs[1], objs[0]
    assert _problems("moq-test-subgroup-per-group-eog", objs) == [
        "object 0.0 after 0.1 on subgroup 0"]
    dgrams = _delivery("moq-test-datagram")
    dgrams[0] = (*dgrams[0][:7], True)
    assert _problems("moq-test-datagram", dgrams) == [
        "datagram 0.0 sets the End of Group type bit"]


def test_verifier_forward_zero():
    name = "moq-test-forward-zero"
    assert _problems(name, []) == []
    assert _problems(name, _delivery(name)[:2]) == [
        "2 object(s) delivered with Forward State 0"]
    assert _problems(name, [], done=_done(2)) == [
        "downstream Stream Count 2, expected 0"]
    assert client._moq_test_problems(_params(name), [], None, 0, 0, True) == [
        "no downstream PUBLISH_DONE"]


@pytest.mark.asyncio
async def test_a_failed_case_reports_and_the_run_continues(monkeypatch):
    async def _fails(*args, **kw):
        return client.TestResult(name="moq-test-datagram", passed=False,
                                 message="missing 1 object(s): 2.4")

    monkeypatch.setitem(client.TEST_FUNCTIONS, "moq-test-datagram", _fails)
    rep = await client.run_tests(
        ["moq-test-datagram", "moq-test-forward-zero"], "localhost", 1, "/",
        True, True, False, supported_drafts=18)
    assert [r.name for r in rep.results] == [
        "moq-test-datagram", "moq-test-forward-zero"]
    assert not rep.results[0].passed
