"""moq-interop-runner data-plane scenarios: our interop client against our
interop relay, in one process."""
import pytest

from aiomoqt.tools import moq_interop_client as client
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.types import ObjectStatus

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
@pytest.mark.parametrize("draft", [14, 16, 18])
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
