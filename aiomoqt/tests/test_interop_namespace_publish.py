"""moq-interop-runner namespace and direct-PUBLISH cases: our interop
client against our interop relay, in one process."""
import pytest

from aiomoqt.tools import moq_interop_client as client
from aiomoqt.tools import moq_interop_relay as relay

from aiomoqt.tests._certs import CERT, KEY, requires_certs

_BASE_PORT = 19100
_DRAFTS = [14, 16, 18, 20, 22]
_TRANSPORTS = pytest.mark.parametrize("use_quic", [True, False],
                                      ids=["quic", "wt"])
_LIFECYCLE = "subscribe-namespace-lifecycle"
_CASES = [_LIFECYCLE, "namespace-discovery", "publish-track-only",
          "publish-track-subscribe"]


@pytest.fixture(autouse=True)
def _clean_tables():
    tables = (relay._announced, relay._tracks, relay._announce_requests,
              relay._ns_subs, relay._track_subs)
    for t in tables:
        t.clear()
    yield
    for t in tables:
        t.clear()


def _port(case, draft, use_quic):
    return (_BASE_PORT + 20 * _CASES.index(case) + 2 * (draft - 14)
            + (not use_quic))


async def _run(case, port, draft, use_quic, **kw):
    server = relay._build_server("localhost", port, CERT, KEY,
                                 use_quic=use_quic, draft=draft)
    handle = await server.serve()
    try:
        return await client.TEST_FUNCTIONS[case](
            "localhost", port, "/", use_quic, True, False,
            supported_drafts=draft, **kw)
    finally:
        handle.close()


def _assert_sessions(r, draft, use_quic, roles):
    transport = "quic" if use_quic else "webtransport-h3"
    for role in roles:
        assert r.sessions[role]["moqt_version"] == f"moqt-{draft}"
        assert r.sessions[role]["transport"] == transport


def test_the_standard_run_includes_every_case():
    assert set(_CASES) <= set(client.STANDARD_TESTS)
    assert set(client.STANDARD_TESTS) <= set(client.TEST_FUNCTIONS)


@requires_certs
@_TRANSPORTS
@pytest.mark.parametrize("draft", [16, 18, 20, 22])
async def test_namespace_lifecycle_through_the_relay(draft, use_quic):
    r = await _run(_LIFECYCLE, _port(_LIFECYCLE, draft, use_quic), draft,
                   use_quic)
    assert r.passed and not r.skipped, r.message
    assert r.spec_revision == client.SUBSCRIBE_NAMESPACE_LIFECYCLE_REVISION
    _assert_sessions(r, draft, use_quic, ("subscriber", "publisher"))


@requires_certs
@_TRANSPORTS
async def test_namespace_lifecycle_skips_draft_14(use_quic):
    r = await _run(_LIFECYCLE, _port(_LIFECYCLE, 14, use_quic), 14, use_quic)
    assert r.skipped and r.passed
    assert r.skip_reason == "NAMESPACE is draft-16; negotiated draft-14"


@requires_certs
@_TRANSPORTS
@pytest.mark.parametrize("draft", _DRAFTS)
@pytest.mark.parametrize("case", _CASES[1:])
async def test_case_through_the_relay(case, draft, use_quic):
    r = await _run(case, _port(case, draft, use_quic), draft, use_quic)
    assert r.passed and not r.skipped, r.message
    roles = (("publisher",) if case == "publish-track-only"
             else ("publisher", "subscriber"))
    _assert_sessions(r, draft, use_quic, roles)


@requires_certs
async def test_lifecycle_fails_without_namespace(monkeypatch):
    monkeypatch.setattr(client, "_LIFECYCLE_STEP_S", 0.5)
    monkeypatch.setattr(relay, "_announce_namespace", lambda ns: None)
    r = await _run(_LIFECYCLE, _BASE_PORT + 100, 18, True)
    assert not r.passed
    assert r.message == "no NAMESPACE within 0.5s"


@requires_certs
async def test_lifecycle_fails_without_namespace_done(monkeypatch):
    monkeypatch.setattr(client, "_LIFECYCLE_STEP_S", 0.5)
    monkeypatch.setattr(relay, "_retract_namespace", lambda ns: None)
    r = await _run(_LIFECYCLE, _BASE_PORT + 101, 18, True)
    assert not r.passed
    assert r.message == "NAMESPACE received, no NAMESPACE_DONE within 0.5s"


@requires_certs
async def test_publish_track_only_fails_when_publish_goes_unanswered(
        monkeypatch):
    async def ignore(session, msg):
        pass

    monkeypatch.setattr(relay, "_on_publish", ignore)
    r = await _run("publish-track-only", _BASE_PORT + 102, 18, True,
                   timeout=1.0)
    assert not r.passed
    assert r.message == "Failed: TimeoutError"


@requires_certs
async def test_publish_track_subscribe_fails_when_objects_are_dropped(
        monkeypatch):
    async def drop(self, *args, **kw):
        pass

    monkeypatch.setattr(client, "_DATA_DELIVERY_S", 1.0)
    monkeypatch.setattr(relay._RelayedTrack, "_forward_one", drop)
    r = await _run("publish-track-subscribe", _BASE_PORT + 103, 18, True)
    assert not r.passed
    assert r.message == "0 of 5 objects within 1s"
