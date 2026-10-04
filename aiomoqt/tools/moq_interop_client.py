#!/usr/bin/env python3
"""
MoQ Interop Test Client — implements the control-plane test cases and the
data-plane scenarios from https://github.com/englishm/moq-interop-runner

Output: TAP version 14 with YAML diagnostics, with an ISO-8601 `# date:`
header so published log files self-identify when they ran.

CLI interface matches TEST-CLIENT-INTERFACE.md spec, plus a `--compat`
flag for known-non-standard endpoints. Compat-tolerated outcomes are
annotated in TAP output (`# COMPAT` directive + `compat: true` YAML
field) so they remain visible — they are explicit acceptances of
intentional deviations, not silent passes.
"""

import argparse
import asyncio
import datetime
import json
import logging
import os
import secrets
import sys
import time
from dataclasses import dataclass, field

from aiomoqt.client import MOQTClient
from aiomoqt.context import is_draft16_or_later, profile_for
from aiomoqt.messages import SubgroupHeader
from aiomoqt.messages.base import MOQTMessage
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import (
    ParamType, FetchType, MOQTRequestError, MOQTMessageType,
    SubscribeErrorCode, RequestErrorCode, SubscribeDoneCode,
    FilterType, GroupOrder, MOQTDraft, ObjectStatus, parse_draft_spec,
)
from aiomoqt.utils.logger import set_log_level

try:
    from aiomoqt import __version__ as AIOMOQT_VERSION
except Exception:
    AIOMOQT_VERSION = "unknown"

# Spec-compliant "track not found" codes across drafts:
# d14 SubscribeErrorCode.TRACK_DOES_NOT_EXIST = 0x04
# d16 RequestErrorCode.DOES_NOT_EXIST         = 0x10
TRACK_NOT_FOUND_CODES = frozenset({
    int(SubscribeErrorCode.TRACK_DOES_NOT_EXIST),
    int(RequestErrorCode.DOES_NOT_EXIST),
})

# INTERNAL_ERROR (0x0) is not an acceptable answer to a well-formed
# request that names a non-existent track — the relay must be specific.
SUBSCRIBE_BENIGN_ERROR_CODES = frozenset({
    int(SubscribeErrorCode.UNAUTHORIZED),
    int(SubscribeErrorCode.TIMEOUT),
    int(SubscribeErrorCode.NOT_SUPPORTED),
    int(SubscribeErrorCode.TRACK_DOES_NOT_EXIST),
    int(SubscribeErrorCode.MALFORMED_AUTH_TOKEN),
    int(SubscribeErrorCode.EXPIRED_AUTH_TOKEN),
    int(RequestErrorCode.MALFORMED_AUTH_TOKEN),
    int(RequestErrorCode.EXPIRED_AUTH_TOKEN),
    int(RequestErrorCode.DOES_NOT_EXIST),
    int(RequestErrorCode.MALFORMED_TRACK),
    int(RequestErrorCode.UNAUTHORIZED),
    int(RequestErrorCode.NOT_SUPPORTED),
    int(RequestErrorCode.TIMEOUT),
})

# INTERNAL_ERROR (0x0) is a server-side fault, not a deliberate refusal,
# so it never satisfies "did the relay reject this request?". moq-rs /
# moq-dev / libquicr use 0 as their own not-found code, so honor it under
# that compat (libquicr returns code=0 for a FETCH of a nonexistent track).
INTERNAL_ERROR_CODE = 0


def _is_refusal(code: int, compat: frozenset) -> bool:
    """Whether a structured error code counts as a valid relay refusal for
    the subscribe / join / fetch probes: any code except INTERNAL_ERROR
    (0x0), which signals a server fault rather than a policy decision —
    unless the endpoint's compat declares 0 as its deliberate refusal
    code (moq-rs / moq-dev / libquicr)."""
    if code != INTERNAL_ERROR_CODE:
        return True
    return (_compat_active(compat, "moq-rs")
            or _compat_active(compat, "moq-dev")
            or _compat_active(compat, "libquicr"))


# ---------------------------------------------------------------------------
# Compatibility flags
# ---------------------------------------------------------------------------
# Compat tolerates *intentional* protocol differences at specific endpoints,
# not real errors. Each tolerance is annotated in the TAP output so a
# reader can see that strict spec was not met. Real failures (connection
# refused, decode error, transport reset) are never compat-passed.
KNOWN_COMPAT_IMPLS = frozenset({
    "moq-dev",            # cdn.moq.dev /anon — returns 404 for not-found
    "moq-rs",             # cloudflare moq-rs d14 — returns code=0 for not-found
    "moq-rs-d16",         # itzmanish/moq-rs draft-16 fork (CF endpoint)
    "libquicr",           # Cisco libquicr — accepts SUBSCRIBE to unknown tracks
    "lenient-extensions",  # tolerate truncated trailing extensions block
    "all",                # enable every known compat tolerance
})

# Endpoint-derived compat tolerances (host -> keys), applied on top of
# --compat. Empty by policy: a confirmed peer non-conformance fails the
# test; tolerances are explicit --compat opt-ins only.
RELAY_COMPAT = {}


def _compat_active(compat: frozenset, key: str) -> bool:
    return "all" in compat or key in compat


def _relay_compat(host: str) -> frozenset:
    """Compat tolerances auto-selected for a known relay endpoint (host
    substring match). Lets one client image recognize a quirky relay and
    self-enable the matching opt-in tolerance with no per-column config,
    since the moq-interop-runner passes only RELAY_URL."""
    h = (host or "").lower()
    keys = set()
    for needle, ks in RELAY_COMPAT.items():
        if needle in h:
            keys |= ks
    return frozenset(keys)


def _format_exc(e: BaseException) -> str:
    """Informative exception text — preserves the class name when str(e) is empty.

    asyncio.TimeoutError, ConnectionResetError, and several aiopquic
    exceptions stringify to '', which produced empty `Failed: ` lines
    in earlier interop reports. This ensures the cause is always
    visible in the log.
    """
    if e is None:
        return "<no exception>"
    cls = type(e).__name__
    msg = str(e)
    if not msg:
        return cls
    return f"{cls}: {msg}" if cls not in msg else msg


async def _probe_setup_ok(host, port, path, use_quic, tls_disable_verify,
                          debug, draft=None, timeout: float = 5.0) -> bool:
    """Probe whether SETUP completes for a given offer. draft=None offers
    the auto (multi-version) set; an int single-pins one draft (one ALPN)
    — what the preference-ordered probe uses, so each attempt is
    deterministic with no multi-offer server-choice vagary. A relay that
    refuses the offer — QUIC 376 no_application_protocol, or a stalled /
    timed-out WT SETUP — fails here. Returns True iff SETUP completes,
    False on any handshake/SETUP failure. Works for both transports."""
    client = _make_client(host, port, path, use_quic,
                          tls_disable_verify, debug, supported_drafts=draft)
    try:
        async with asyncio.timeout(timeout):
            async with client.connect() as session:
                await session.client_session_init()
                session.close()
        return True
    except Exception:
        return False


def _utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _unique_suffix():
    """Short hex timestamp for unique namespace/track per run."""
    return hex(int(time.time()) & 0xFFFF)[2:]


INTEROP_NAMESPACE = f"moq-test/{_unique_suffix()}/interop"
INTEROP_TRACK = f"track-{_unique_suffix()}"

# How long the announce-subscribe subscriber waits for SUBSCRIBE_OK before
# treating the subscription as held pending — a valid forward-and-wait relay
# model (e.g. moqtail) rather than a failure. Mirrors moq-rs's 5s.
_SUBSCRIBE_WAIT = 5.0

# PUBLISH_NAMESPACE parameters used by the standard tests.
# Default empty: anonymous tests should look anonymous. AUTH_TOKEN is
# added only when --auth-token / $AUTH_TOKEN is set explicitly. Some
# relays (notably cdn.moq.dev /anon) reject PUBLISH_NAMESPACE with
# any AUTH_TOKEN param on their anonymous scope.
_PUB_NS_PARAMS: dict = {}

STANDARD_TESTS = [
    "setup-only",
    "announce-only",
    "publish-namespace-done",
    "subscribe-error",
    "rendezvous-timeout",
    "announce-subscribe",
    "subscribe-before-announce",
    "data-subgroup-basic",
]

# Leading Track Namespace fields from --namespace-prefix.
_NS_PREFIX: tuple = ()


@dataclass
class TestResult:
    name: str
    passed: bool
    duration_ms: float = 0.0
    message: str = ""
    # Role from the test specification -> _session_meta() of that session.
    sessions: dict = field(default_factory=dict)
    # Revision of the prose specification implemented; 0 = unrecorded.
    spec_revision: int = 0
    expected: str = ""
    received: str = ""
    skipped: bool = False
    skip_reason: str = ""
    compat: bool = False        # True when result accepted under a --compat tolerance
    compat_note: str = ""       # Human-readable reason the deviation was tolerated
    wire_noncompliance_count: int = 0   # Spec-violating peer wire events tolerated during this test


class TAPReporter:
    """Emit TAP version 14 output with run-identifying headers."""

    def __init__(self, *, target_url: str = "",
                 aiomoqt_version: str = "",
                 compat: frozenset = frozenset()):
        self.results: list[TestResult] = []
        self.target_url = target_url
        self.aiomoqt_version = aiomoqt_version
        self.compat = compat
        self.started_at = _utc_now_iso()
        self.ended_at = ""
        self.notes: list[str] = []

    def add(self, result: TestResult):
        self.results.append(result)

    def finalize(self):
        if not self.ended_at:
            self.ended_at = _utc_now_iso()

    def report(self) -> str:
        self.finalize()
        lines = ["TAP version 14"]
        lines.append(f"# date: {self.started_at}")
        lines.append(f"# ended: {self.ended_at}")
        if self.target_url:
            lines.append(f"# target: {self.target_url}")
        if self.aiomoqt_version:
            lines.append(f"# version: aiomoqt/{self.aiomoqt_version}")
        if self.compat:
            lines.append(f"# compat: {','.join(sorted(self.compat))}")
        for note in self.notes:
            lines.append(f"# note: {note}")
        lines.append(f"1..{len(self.results)}")
        for i, r in enumerate(self.results, 1):
            status = "ok" if r.passed else "not ok"
            skip = f" # SKIP {r.skip_reason}" if r.skipped else ""
            tag = " # COMPAT" if r.compat and not r.skipped else ""
            lines.append(f"{status} {i} - {r.name}{skip}{tag}")
            lines.append("  ---")
            lines.append(f"  duration_ms: {round(r.duration_ms)}")
            if self.aiomoqt_version:
                lines.append(
                    f"  implementation_version: {_yaml_str(self.aiomoqt_version)}")
            if r.spec_revision:
                lines.append(f"  test_spec_revision: {r.spec_revision}")
            roles = {k: v for k, v in r.sessions.items() if v}
            if roles:
                lines.append("  sessions:")
                for role, meta in roles.items():
                    lines.append(f"    {role}:")
                    for k, v in meta.items():
                        lines.append(f"      {k}: {_yaml_str(v)}")
            if r.message:
                lines.append(f"  message: {_yaml_str(r.message)}")
            if r.expected:
                lines.append(f"  expected: {_yaml_str(r.expected)}")
            if r.received:
                lines.append(f"  received: {_yaml_str(r.received)}")
            if r.compat:
                lines.append("  compat: true")
                if r.compat_note:
                    lines.append(f"  compat_note: {_yaml_str(r.compat_note)}")
            if r.wire_noncompliance_count:
                lines.append(
                    f"  wire_noncompliance: {r.wire_noncompliance_count}"
                )
            lines.append("  ...")
        total_noncompliance = sum(
            r.wire_noncompliance_count for r in self.results
        )
        if total_noncompliance:
            lines.append(
                f"# wire_noncompliance_total: trailing_extensions="
                f"{total_noncompliance}"
            )
        return "\n".join(lines)


def _yaml_str(value) -> str:
    """A YAML double-quoted scalar (JSON string syntax is valid YAML)."""
    return json.dumps(str(value), ensure_ascii=False)


def _session_meta(session) -> dict:
    """TAP `sessions` fields for what this session actually negotiated."""
    try:
        hi = session.handshake_info
    except Exception:
        return {}
    wt = hi.get("transport") == "webtransport"
    meta = {"transport": "webtransport-h3" if wt else "quic"}
    if hi.get("draft"):
        meta["moqt_version"] = f"moqt-{hi['draft']}"
    if hi.get("alpn"):
        meta["alpn"] = hi["alpn"]
    if wt and hi.get("wt_protocol"):
        meta["webtransport_protocol"] = hi["wt_protocol"]
    initial = (hi.get("connection_ids") or {}).get("initial")
    if initial:
        meta["quic_initial_destination_connection_id"] = initial.hex()
    return meta


def _make_client(host: str, port: int, path: str, use_quic: bool,
                 tls_disable_verify: bool, debug: bool,
                 supported_drafts: int = None) -> MOQTClient:
    """Create a configured MOQTClient."""
    return MOQTClient(
        host, port,
        path=path,
        use_quic=use_quic,
        verify_tls=not tls_disable_verify,
        debug=debug,
        supported_drafts=supported_drafts,
    )


# ---------------------------------------------------------------------------
# Test implementations
# ---------------------------------------------------------------------------

async def test_setup_only(host, port, path, use_quic, tls_disable_verify,
                          debug, supported_drafts=None, compat=frozenset(),
                          timeout=5.0) -> TestResult:
    """Test 1: Connect, exchange SETUP, graceful close."""
    t0 = time.monotonic()
    sessions = {}
    client = _make_client(host, port, path, use_quic, tls_disable_verify, debug, supported_drafts=supported_drafts)
    try:
        async with asyncio.timeout(timeout):
            async with client.connect() as session:
                await session.client_session_init()
                sessions["client"] = _session_meta(session)
                session.close()
        return TestResult(
            name="setup-only", passed=True,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions,
            message="SERVER_SETUP received with compatible version",
        )
    except Exception as e:
        return TestResult(
            name="setup-only", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions,
            message=f"Failed: {_format_exc(e)}",
            expected="SERVER_SETUP received",
            received=_format_exc(e),
        )


async def test_announce_only(host, port, path, use_quic, tls_disable_verify,
                             debug, supported_drafts=None, compat=frozenset(),
                             timeout=5.0) -> TestResult:
    """Test 2: SETUP + PUBLISH_NAMESPACE + receive OK."""
    t0 = time.monotonic()
    sessions = {}
    client = _make_client(host, port, path, use_quic, tls_disable_verify, debug, supported_drafts=supported_drafts)
    try:
        async with asyncio.timeout(timeout):
            async with client.connect() as session:
                await session.client_session_init()
                sessions["publisher"] = _session_meta(session)

                await session.publish_namespace(
                    namespace=INTEROP_NAMESPACE,
                    parameters=_PUB_NS_PARAMS,
                    wait_response=True,
                )
                session.close()
        return TestResult(
            name="announce-only", passed=True,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions,
            message="PUBLISH_NAMESPACE_OK received",
        )
    except Exception as e:
        return TestResult(
            name="announce-only", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions,
            message=f"Failed: {_format_exc(e)}",
            expected="PUBLISH_NAMESPACE_OK",
            received=_format_exc(e),
        )


async def test_publish_namespace_done(host, port, path, use_quic,
                                      tls_disable_verify, debug,
                                      supported_drafts=None,
                                      compat=frozenset(),
                                      timeout=5.0) -> TestResult:
    """Test 3: SETUP + PUBLISH_NAMESPACE + OK + PUBLISH_NAMESPACE_DONE + close."""
    t0 = time.monotonic()
    sessions = {}
    client = _make_client(host, port, path, use_quic, tls_disable_verify, debug, supported_drafts=supported_drafts)
    try:
        async with asyncio.timeout(timeout):
            async with client.connect() as session:
                await session.client_session_init()
                sessions["publisher"] = _session_meta(session)

                response = await session.publish_namespace(
                    namespace=INTEROP_NAMESPACE,
                    parameters=_PUB_NS_PARAMS,
                    wait_response=True,
                )
                ns_tuple = session._make_namespace_tuple(INTEROP_NAMESPACE)
                # d14/d16 send PUBLISH_NAMESPACE_DONE; d18 has no such
                # message and withdraws by resetting the announce's
                # request stream (§3.3.2).
                withdrew = session.publish_namespace_done(
                    namespace=ns_tuple,
                    request_id=response.request_id,
                )
                how = ("PUBLISH_NAMESPACE_DONE sent" if withdrew is not None
                       else "announce stream reset")
                await asyncio.sleep(0.1)
                session.close()

        return TestResult(
            name="publish-namespace-done", passed=True,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions,
            message=f"PUBLISH_NAMESPACE_OK received, {how}",
        )
    except Exception as e:
        return TestResult(
            name="publish-namespace-done", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions,
            message=f"Failed: {_format_exc(e)}",
            expected="PUBLISH_NAMESPACE_OK + PUBLISH_NAMESPACE_DONE",
            received=_format_exc(e),
        )


async def test_subscribe_error(host, port, path, use_quic,
                               tls_disable_verify, debug,
                               supported_drafts=None, compat=frozenset(),
                               timeout=5.0) -> TestResult:
    """Test 4: SUBSCRIBE to non-existent track, expect an error response.

    Interop policy (default): any structured error (SUBSCRIBE_ERROR /
    REQUEST_ERROR, any code) is accepted as a valid "track not found"
    rejection — the assertion is that the relay refused; the exact code
    is noted, not required (relays use non-spec codes, e.g. 404 for
    moq-dev, 0 for moq-rs d14). A timeout or transport error still fails.
    """
    moq_dev_compat = _compat_active(compat, "moq-dev")
    moq_rs_compat = _compat_active(compat, "moq-rs")
    libquicr_compat = _compat_active(compat, "libquicr")
    # Interop policy (default): any structured error answers the assertion
    # "did the relay refuse the bad request?" — yes. The exact code is
    # noted, not required (relays return non-spec codes like 404). A
    # timeout / transport error still fails.
    accept_any_error = True
    t0 = time.monotonic()
    sessions = {}
    client = _make_client(host, port, path, use_quic, tls_disable_verify, debug, supported_drafts=supported_drafts)
    try:
        async with asyncio.timeout(timeout):
            async with client.connect() as session:
                await session.client_session_init()
                sessions["subscriber"] = _session_meta(session)
                try:
                    await session.subscribe(
                        namespace="nonexistent/namespace",
                        track_name="test-track",
                        wait_response=True,
                    )
                    # If we get here, no error.
                    session.close()
                    if libquicr_compat:
                        return TestResult(
                            name="subscribe-error", passed=True,
                            duration_ms=(time.monotonic() - t0) * 1000,
                            sessions=sessions,
                            message=(
                                "SUBSCRIBE_OK for non-existent track accepted "
                                "(libquicr deferred-delivery policy)"
                            ),
                            expected="error response",
                            received="SUBSCRIBE_OK",
                            compat=True,
                            compat_note=(
                                "libquicr accepts SUBSCRIBE to any track "
                                "(deferred-delivery policy); does not "
                                "verify track existence at subscribe time"
                            ),
                        )
                    return TestResult(
                        name="subscribe-error", passed=False,
                        duration_ms=(time.monotonic() - t0) * 1000,
                        sessions=sessions,
                        message="Unexpected SUBSCRIBE_OK for non-existent track",
                        expected="error response",
                        received="SUBSCRIBE_OK",
                    )
                except MOQTRequestError as e:
                    session.close()
                    code = int(e.error_code)
                    spec_ok = code in TRACK_NOT_FOUND_CODES
                    if spec_ok:
                        return TestResult(
                            name="subscribe-error", passed=True,
                            duration_ms=(time.monotonic() - t0) * 1000,
                            sessions=sessions,
                            message=f"Error received (expected): code={e.error_code}",
                            expected="SUBSCRIBE_ERROR code=TRACK_DOES_NOT_EXIST",
                        )
                    if accept_any_error and _is_refusal(code, compat):
                        impl = "moq-rs" if moq_rs_compat else ("moq-dev" if moq_dev_compat else "relay")
                        return TestResult(
                            name="subscribe-error", passed=True,
                            duration_ms=(time.monotonic() - t0) * 1000,
                            sessions=sessions,
                            message=(
                                f"Non-spec error code={e.error_code} accepted "
                                f"as 'track not found'"
                            ),
                            expected="SUBSCRIBE_ERROR code=TRACK_DOES_NOT_EXIST (0x04/0x10)",
                            received=f"SUBSCRIBE_ERROR code={e.error_code}",
                            compat=True,
                            compat_note=(
                                f"{impl} returns non-spec error code "
                                f"{e.error_code} for not-found; spec expects "
                                f"0x04 (d14) or 0x10 (d16)"
                            ),
                        )
                    return TestResult(
                        name="subscribe-error", passed=False,
                        duration_ms=(time.monotonic() - t0) * 1000,
                        sessions=sessions,
                        message=(
                            f"Non-conformant: expected TRACK_DOES_NOT_EXIST "
                            f"(d14=0x04 / d16=0x10), got code={e.error_code}"
                        ),
                        expected="SUBSCRIBE_ERROR code=TRACK_DOES_NOT_EXIST",
                        received=f"SUBSCRIBE_ERROR code={e.error_code}",
                    )
    except Exception as e:
        return TestResult(
            name="subscribe-error", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions,
            message=f"Failed: {_format_exc(e)}",
            expected="error response",
            received=_format_exc(e),
        )


# d18 SUBSCRIBE parameter (§10.2.6).
_RENDEZVOUS_TIMEOUT = 0x04


async def test_rendezvous_timeout(host, port, path, use_quic,
                                  tls_disable_verify, debug,
                                  supported_drafts=None, compat=frozenset(),
                                  timeout=5.0) -> TestResult:
    """SUBSCRIBE with RENDEZVOUS_TIMEOUT 500 ms for a track nobody
    publishes; expect REQUEST_ERROR TIMEOUT within 2 s."""
    name = "rendezvous-timeout"
    t0 = time.monotonic()
    sessions = {}
    client = _make_client(host, port, path, use_quic, tls_disable_verify,
                          debug, supported_drafts=supported_drafts)
    try:
        async with asyncio.timeout(timeout):
            async with client.connect() as session:
                await session.client_session_init()
                sessions["subscriber"] = _session_meta(session)
                draft = session.negotiated_draft
                if draft < 18:
                    session.close()
                    return TestResult(
                        name=name, passed=True, skipped=True,
                        skip_reason=f"RENDEZVOUS_TIMEOUT is draft-18; "
                                    f"negotiated draft-{draft}",
                        duration_ms=(time.monotonic() - t0) * 1000,
                        sessions=sessions)
                try:
                    async with asyncio.timeout(2.0):
                        await session.subscribe(
                            namespace="nonexistent/rendezvous",
                            track_name="test-track",
                            parameters={_RENDEZVOUS_TIMEOUT: 500},
                            wait_response=True)
                    code = None
                except MOQTRequestError as e:
                    code = int(e.error_code)
                session.close()
    except Exception as e:
        return TestResult(
            name=name, passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions,
            message=f"Failed: {_format_exc(e)}",
            expected="REQUEST_ERROR TIMEOUT", received=_format_exc(e))
    if code == RequestErrorCode.TIMEOUT:
        passed, msg = True, "REQUEST_ERROR TIMEOUT received"
    elif code is None:
        passed, msg = False, "SUBSCRIBE_OK for a track nobody publishes"
    else:
        passed, msg = False, f"REQUEST_ERROR code={code:#x}, expected TIMEOUT"
    return TestResult(
        name=name, passed=passed,
        duration_ms=(time.monotonic() - t0) * 1000,
        sessions=sessions, message=msg,
        expected="REQUEST_ERROR TIMEOUT")


async def _serve_forwarded_subscribe(session, msg):
    """Publisher-side handler: serve the relay's forwarded SUBSCRIBE so
    forward-and-wait relays (e.g. moqtail) complete the downstream
    SUBSCRIBE_OK. Mirrors loopback_bench's _on_subscribe and moq-rs's
    serve loop — answer SUBSCRIBE_OK and generate the track. Eager relays
    that ack the subscriber directly never forward, so this stays unused
    for them."""
    track = PublishedTrack(
        session,
        namespace=INTEROP_NAMESPACE,
        trackname=INTEROP_TRACK,
        object_size=64, group_size=10, num_subgroups=1, rate=50,
    )
    track._quiet = True
    track._stats_header_printed = True
    ok = session.subscribe_ok(request_msg=msg)
    track.track_alias = ok.track_alias
    track._generating = True
    await track.generate(session, ok.track_alias)


async def test_announce_subscribe(host, port, path, use_quic,
                                  tls_disable_verify, debug,
                                  supported_drafts=None, compat=frozenset(),
                                  timeout=10.0) -> TestResult:
    """Test 5: Two connections — publisher announces, subscriber subscribes."""
    t0 = time.monotonic()
    sessions = {}

    try:
        async with asyncio.timeout(timeout):
            # Publisher connection. Serve the relay's forwarded SUBSCRIBE
            # so forward-and-wait relays (moqtail) complete the OK.
            pub_client = _make_client(host, port, path, use_quic,
                                      tls_disable_verify, debug, supported_drafts=supported_drafts)
            pub_client.register_handler(
                MOQTMessageType.SUBSCRIBE, _serve_forwarded_subscribe)
            sub_client = _make_client(host, port, path, use_quic,
                                      tls_disable_verify, debug, supported_drafts=supported_drafts)

            async with pub_client.connect() as pub_session:
                await pub_session.client_session_init()
                sessions["publisher"] = _session_meta(pub_session)

                await pub_session.publish_namespace(
                    namespace=INTEROP_NAMESPACE,
                    parameters=_PUB_NS_PARAMS,
                    wait_response=True,
                )

                # Subscriber connection
                async with sub_client.connect() as sub_session:
                    await sub_session.client_session_init()
                    sessions["subscriber"] = _session_meta(sub_session)

                    pending_hold = False
                    try:
                        async with asyncio.timeout(_SUBSCRIBE_WAIT):
                            await sub_session.subscribe(
                                namespace=INTEROP_NAMESPACE,
                                track_name=INTEROP_TRACK,
                                wait_response=True,
                            )
                        msg = "SUBSCRIBE_OK received — relay routed subscription"
                        passed = True
                    except asyncio.TimeoutError:
                        # Forward-and-wait relays (e.g. moqtail) hold the
                        # subscription pending until the upstream publisher
                        # serves data — no eager SUBSCRIBE_OK arrives. A held
                        # subscription is a valid relay model, not a failure
                        # (moq-rs treats it the same). Annotated so the pass
                        # stays visible as a non-eager outcome. A real
                        # SUBSCRIBE_ERROR (below) is still a fail.
                        msg = ("relay holds subscription pending "
                               "(forward-and-wait); no SUBSCRIBE_OK within "
                               f"{_SUBSCRIBE_WAIT:.0f}s")
                        passed = True
                        pending_hold = True
                    except MOQTRequestError as e:
                        msg = f"SUBSCRIBE_ERROR: upstream subscribe failed: {e.reason}"
                        passed = False

                    sub_session.close()
                pub_session.close()

        return TestResult(
            name="announce-subscribe", passed=passed,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions,
            message=msg,
            compat=pending_hold,
            compat_note=(
                "relay held the subscription pending without an eager "
                "SUBSCRIBE_OK; accepted as a valid forward-and-wait relay "
                "model (matches moq-rs)") if pending_hold else "",
        )
    except Exception as e:
        return TestResult(
            name="announce-subscribe", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions,
            message=f"Failed: {_format_exc(e)}",
            received=_format_exc(e),
        )


async def test_namespace_discovery(host, port, path, use_quic,
                                   tls_disable_verify, debug,
                                   supported_drafts=None,
                                   compat=frozenset(),
                                   timeout=15.0) -> TestResult:
    """Test 7: A subscriber knowing only the namespace learns the trackname.

    d14/d16 answer SUBSCRIBE_NAMESPACE with a PUBLISH per track. d18
    reports namespaces first (NAMESPACE) and answers a second request,
    SUBSCRIBE_TRACKS, with the PUBLISH — and moved SUBSCRIBE_NAMESPACE
    from 0x11 to 0x50. SubscribedTrack walks whichever shape the
    negotiated draft requires, so this asserts the outcome, not the
    message sequence, and holds across every draft.
    """
    t0 = time.monotonic()
    sessions = {}
    try:
        async with asyncio.timeout(timeout):
            pub_client = _make_client(host, port, path, use_quic,
                                      tls_disable_verify, debug,
                                      supported_drafts=supported_drafts)
            pub_client.register_handler(
                MOQTMessageType.SUBSCRIBE, _serve_forwarded_subscribe)
            sub_client = _make_client(host, port, path, use_quic,
                                      tls_disable_verify, debug,
                                      supported_drafts=supported_drafts)

            async with pub_client.connect() as pub_session:
                await pub_session.client_session_init()
                sessions["publisher"] = _session_meta(pub_session)
                track = PublishedTrack(
                    pub_session,
                    namespace=INTEROP_NAMESPACE,
                    trackname=INTEROP_TRACK,
                    object_size=64, group_size=10, num_subgroups=1, rate=50,
                )
                track._quiet = True
                track._stats_header_printed = True
                # Announce the namespace as well as the track: d18
                # enumerates namespaces, and a relay learns a namespace
                # exists from PUBLISH_NAMESPACE, not from PUBLISH.
                # d14/d16 need the PUBLISH for the fused response.
                await track.publish(announce_namespace=True,
                                    publish_track=True)

                async with sub_client.connect() as sub_session:
                    await sub_session.client_session_init()
                    sessions["subscriber"] = _session_meta(sub_session)
                    discovered = SubscribedTrack(
                        sub_session, INTEROP_NAMESPACE)
                    discovered._quiet = True
                    await discovered.subscribe(timeout=timeout / 2)
                    found = discovered.trackname
                    passed = found == INTEROP_TRACK
                    msg = (f"discovered '{found}'" if passed else
                           f"discovered '{found}', expected "
                           f"'{INTEROP_TRACK}'")
                    sub_session.close()
                pub_session.close()

        return TestResult(
            name="namespace-discovery", passed=passed,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions,
            message=msg,
            expected=f"PUBLISH announcing '{INTEROP_TRACK}'",
            received=str(found),
        )
    except Exception as e:
        return TestResult(
            name="namespace-discovery", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions,
            message=f"Failed: {_format_exc(e)}",
            expected=f"PUBLISH announcing '{INTEROP_TRACK}'",
            received=_format_exc(e),
        )


async def test_subscribe_before_announce(host, port, path, use_quic,
                                         tls_disable_verify, debug,
                                         supported_drafts=None,
                                         compat=frozenset(),
                                         timeout=7.0) -> TestResult:
    """Test 6: Subscriber connects first, publisher 500ms later. Both outcomes valid."""
    moq_dev_compat = _compat_active(compat, "moq-dev")
    moq_rs_compat = _compat_active(compat, "moq-rs")
    # Interop policy (default): any structured error answers the assertion
    # "did the relay refuse the bad request?" — yes. The exact code is
    # noted, not required (relays return non-spec codes like 404). A
    # timeout / transport error still fails.
    accept_any_error = True
    t0 = time.monotonic()
    sessions = {}
    sub_response = None

    try:
        async with asyncio.timeout(timeout):
            sub_client = _make_client(host, port, path, use_quic,
                                      tls_disable_verify, debug, supported_drafts=supported_drafts)
            pub_client = _make_client(host, port, path, use_quic,
                                      tls_disable_verify, debug, supported_drafts=supported_drafts)

            async with sub_client.connect() as sub_session:
                await sub_session.client_session_init()
                sessions["subscriber"] = _session_meta(sub_session)

                # Subscriber sends SUBSCRIBE before publisher announces
                sub_task = asyncio.create_task(
                    sub_session.subscribe(
                        namespace=INTEROP_NAMESPACE,
                        track_name=INTEROP_TRACK,
                        wait_response=True,
                    )
                )

                # Wait 500ms then publisher connects and announces
                await asyncio.sleep(0.5)

                async with pub_client.connect() as pub_session:
                    await pub_session.client_session_init()
                    sessions["publisher"] = _session_meta(pub_session)

                    await pub_session.publish_namespace(
                        namespace=INTEROP_NAMESPACE,
                        parameters=_PUB_NS_PARAMS,
                        wait_response=True,
                    )

                    # Wait for subscriber response (may have already arrived)
                    try:
                        async with asyncio.timeout(2.0):
                            sub_response = await sub_task
                    except MOQTRequestError as e:
                        sub_response = e  # error is a valid outcome
                    except asyncio.TimeoutError:
                        sub_response = None

                    pub_session.close()
                sub_session.close()

        # Valid outcomes: SUBSCRIBE_OK (relay buffered pre-announce sub)
        # OR a structured SUBSCRIBE_ERROR with a spec-defined code
        # (relay chose not to buffer — represented by codes like
        # TRACK_DOES_NOT_EXIST or UNINTERESTED). INTERNAL_ERROR (0x0)
        # is rejected: it signals a server-side failure, not a policy
        # choice, and should not pass a conformance test.
        compat_used = False
        compat_reason = ""
        if sub_response is None:
            msg = "Timeout waiting for subscriber response"
            passed = False
        elif isinstance(sub_response, MOQTRequestError):
            code = int(sub_response.error_code)
            spec_ok = code in SUBSCRIBE_BENIGN_ERROR_CODES
            if spec_ok:
                msg = f"Error received (valid: relay didn't buffer): code={sub_response.error_code}"
                passed = True
            elif accept_any_error and _is_refusal(code, compat):
                impl = "moq-rs" if moq_rs_compat else ("moq-dev" if moq_dev_compat else "relay")
                msg = (
                    f"Non-spec error code={sub_response.error_code} accepted "
                    f"as benign 'did not buffer'"
                )
                passed = True
                compat_used = True
                compat_reason = (
                    f"{impl} returns non-spec error code "
                    f"{sub_response.error_code} for not-found; spec "
                    f"expects a benign code"
                )
            else:
                msg = (
                    f"Non-conformant error: expected a benign code "
                    f"(TRACK_DOES_NOT_EXIST / UNAUTHORIZED / TIMEOUT / "
                    f"NOT_SUPPORTED), got code={sub_response.error_code}"
                )
                passed = False
        else:
            msg = ("SUBSCRIBE_OK received after delayed announce "
                   "(relay buffered)")
            passed = True

        return TestResult(
            name="subscribe-before-announce", passed=passed,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions,
            message=msg,
            compat=compat_used,
            compat_note=compat_reason,
        )
    except Exception as e:
        return TestResult(
            name="subscribe-before-announce", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions,
            message=f"Failed: {_format_exc(e)}",
            received=_format_exc(e),
        )


# ---------------------------------------------------------------------------
# Data plane (docs/tests/data-plane/)
# ---------------------------------------------------------------------------

# data-subgroup-basic canonical case: one subgroup per group.
DATA_SUBGROUP_BASIC_REVISION = 1
_DATA_GROUPS = 3
_DATA_OBJECTS = 5
_DATA_PRIORITY = 128
_DATA_FILL = b"t"
_DATA_GAP_S = 0.002
_DATA_PHASE_S = 5.0
_DATA_DELIVERY_S = 10.0
# PUBLISH_DONE Stream Count from a publisher that cannot count its streams.
_UNKNOWN_STREAM_COUNT = (1 << 62) - 1


def _run_id() -> str:
    """Run-unique namespace field: 128 random bits, lowercase hex."""
    return secrets.token_hex(16)


def _data_object_size(object_id: int) -> int:
    return 64 if object_id == 0 else 32


def _as_bytes(value) -> bytes:
    return value.encode() if isinstance(value, str) else bytes(value)


def _is_normal(status) -> bool:
    return status is None or status == ObjectStatus.NORMAL


class _DataPublisher:
    """Publisher role: accepts the relay's SUBSCRIBE for one Full Track
    Name and sends the canonical case on it."""

    def __init__(self, namespace: tuple, track_name: bytes):
        self.namespace = namespace
        self.track_name = track_name
        self.request = None
        self.track_alias = None
        self.forward = None
        # The relay ended the upstream subscription (STOP_SENDING, reset
        # or UNSUBSCRIBE): there is no request stream left for PUBLISH_DONE.
        self.cancelled = False
        self.done_sent = False
        self.subscribed = asyncio.Event()

    async def on_subscribe(self, session, msg):
        matches = (tuple(_as_bytes(f) for f in msg.track_namespace) == self.namespace
                   and _as_bytes(msg.track_name) == self.track_name)
        if not matches or self.request is not None:
            code = (RequestErrorCode.DOES_NOT_EXIST
                    if is_draft16_or_later(session.negotiated_draft)
                    else SubscribeErrorCode.TRACK_DOES_NOT_EXIST)
            session.subscribe_error(request_id=msg.request_id,
                                    error_code=int(code),
                                    reason="track does not exist")
            return
        ok = session.subscribe_ok(request_msg=msg)
        self.request = msg
        self.track_alias = ok.track_alias
        self.forward = getattr(msg, "forward", None)
        session.register_request_cancel_handler(
            msg.request_id, lambda _rid: setattr(self, "cancelled", True))
        self.subscribed.set()

    async def send(self, session) -> int:
        """One subgroup stream per group, ended by FIN with no end-of-group
        signal, then PUBLISH_DONE. Returns the number of streams opened."""
        prof = profile_for(session.negotiated_draft)
        streams = 0
        for group_id in range(_DATA_GROUPS):
            stream_id = await session.open_uni_stream()
            streams += 1
            header = SubgroupHeader(
                track_alias=self.track_alias, group_id=group_id,
                subgroup_id=0, publisher_priority=_DATA_PRIORITY, prof=prof)
            session.stream_write(stream_id, header.serialize().data)
            for object_id in range(_DATA_OBJECTS):
                if group_id or object_id:
                    await asyncio.sleep(_DATA_GAP_S)
                buf = header.next_object(
                    payload=_DATA_FILL * _data_object_size(object_id),
                    object_id=object_id)
                await session.stream_write_drain(stream_id, buf.data)
            session.stream_write(stream_id, b"", end_stream=True)
        if not self.cancelled:
            session.subscribe_done(
                request_id=self.request.request_id,
                status_code=SubscribeDoneCode.TRACK_ENDED,
                stream_count=streams, reason="track ended")
            self.done_sent = True
        return streams


class _DataSubscriber:
    """Subscriber role: records every object, every stream end and the
    PUBLISH_DONE."""

    def __init__(self):
        # (group, subgroup, object, priority, status, payload)
        self.objects = []
        self.streams_ended = 0
        self.streams_reset = 0
        self.done = None
        self._changed = asyncio.Event()

    def on_object(self, msg, size, ts, group_id, subgroup_id):
        # The object header is reused across a stream: copy what is checked.
        gid = getattr(msg, "group_id", None)
        self.objects.append((
            group_id if gid is None else gid, subgroup_id, msg.object_id,
            getattr(msg, "publisher_priority", None),
            getattr(msg, "status", None), bytes(msg.payload or b"")))

    def on_stream_end(self, group_id, subgroup_id, clean=True, reset_code=0):
        self.streams_ended += 1
        if not clean:
            self.streams_reset += 1
        self._changed.set()

    def on_done(self, msg):
        self.done = msg
        self._changed.set()

    def _drained(self) -> bool:
        if self.done is None:
            return False
        count = self.done.stream_count
        return (count != _UNKNOWN_STREAM_COUNT
                and self.streams_ended >= (count or 0))

    async def drain(self, deadline: float) -> None:
        """Until PUBLISH_DONE has arrived and Stream Count streams have
        ended; with the unknown-count sentinel, until the deadline."""
        loop = asyncio.get_running_loop()
        while not self._drained():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            self._changed.clear()
            try:
                async with asyncio.timeout(remaining):
                    await self._changed.wait()
            except TimeoutError:
                return


def _data_problems(objects) -> list:
    """Deviations from the canonical object set: bad objects in arrival
    order, then missing objects in location order."""
    problems, seen = [], set()
    for gid, sgid, oid, prio, status, payload in objects:
        if not _is_normal(status):
            continue
        loc = f"{gid}.{oid}"
        if (gid, oid) in seen:
            problems.append(f"duplicate object {loc}")
            continue
        seen.add((gid, oid))
        if gid not in range(_DATA_GROUPS) or oid not in range(_DATA_OBJECTS):
            problems.append(f"unexpected object {loc}")
            continue
        if sgid != 0:
            problems.append(f"object {loc} on subgroup {sgid}, expected 0")
        if prio != _DATA_PRIORITY:
            problems.append(
                f"object {loc} priority {prio}, expected {_DATA_PRIORITY}")
        size = _data_object_size(oid)
        if len(payload) != size:
            problems.append(f"object {loc} payload {len(payload)} bytes, "
                            f"expected {size}")
        elif payload != _DATA_FILL * size:
            problems.append(f"object {loc} payload bytes are not all 0x74")
    for gid in range(_DATA_GROUPS):
        for oid in range(_DATA_OBJECTS):
            if (gid, oid) not in seen:
                problems.append(f"missing object {gid}.{oid}")
    return problems


async def test_data_subgroup_basic(host, port, path, use_quic,
                                   tls_disable_verify, debug,
                                   supported_drafts=None,
                                   compat=frozenset()) -> TestResult:
    """data-subgroup-basic: three groups of five objects, one subgroup
    stream per group, relayed from publisher to subscriber and checked
    object by object."""
    name = "data-subgroup-basic"
    t0 = time.monotonic()
    loop = asyncio.get_running_loop()
    sessions = {}
    fields = (*_NS_PREFIX, "moq-interop", name, _run_id())
    namespace = "/".join(fields)
    pub = _DataPublisher(tuple(f.encode() for f in fields), b"track")
    sub = _DataSubscriber()
    pub_client = _make_client(host, port, path, use_quic, tls_disable_verify,
                              debug, supported_drafts=supported_drafts)
    pub_client.register_handler(MOQTMessageType.SUBSCRIBE, pub.on_subscribe)
    sub_client = _make_client(host, port, path, use_quic, tls_disable_verify,
                              debug, supported_drafts=supported_drafts)

    def result(passed: bool, message: str) -> TestResult:
        return TestResult(
            name=name, passed=passed,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions, spec_revision=DATA_SUBGROUP_BASIC_REVISION,
            message=message)

    step = "publisher SETUP"
    try:
        async with asyncio.timeout(_DATA_PHASE_S) as phase:
            async with pub_client.connect() as pub_session:
                await pub_session.client_session_init()
                sessions["publisher"] = _session_meta(pub_session)

                step = "PUBLISH_NAMESPACE"
                phase.reschedule(loop.time() + _DATA_PHASE_S)
                await pub_session.publish_namespace(
                    namespace=namespace, parameters=_PUB_NS_PARAMS,
                    wait_response=True)

                step = "subscriber SETUP"
                phase.reschedule(loop.time() + _DATA_PHASE_S)
                async with sub_client.connect() as sub_session:
                    await sub_session.client_session_init()
                    sessions["subscriber"] = _session_meta(sub_session)

                    # FORWARD, SUBSCRIBER_PRIORITY and SUBSCRIPTION_FILTER
                    # are omitted; d14 carries them as fixed fields.
                    step = "SUBSCRIBE"
                    phase.reschedule(loop.time() + _DATA_PHASE_S)
                    d16 = is_draft16_or_later(sub_session.negotiated_draft)
                    track = SubscribedTrack(
                        sub_session, namespace, "track",
                        on_object=sub.on_object, on_done=sub.on_done)
                    await track.subscribe(
                        forward=None, group_order=GroupOrder.ASCENDING,
                        filter_type=None if d16 else FilterType.LATEST_OBJECT)
                    sub_session.register_stream_end_handler(
                        track.track_alias, sub.on_stream_end)

                    step = "upstream SUBSCRIBE"
                    await pub.subscribed.wait()
                    if pub.forward == 0:
                        return result(False, "relay subscribed upstream "
                                             "with Forward State 0")

                    step = "object delivery"
                    deadline = loop.time() + _DATA_DELIVERY_S
                    phase.reschedule(deadline + 1.0)
                    sent = await pub.send(pub_session)
                    await sub.drain(deadline)
                    sub_session.close()
                pub_session.close()
    except TimeoutError:
        return result(False, f"timed out waiting for {step}")
    except MOQTRequestError as e:
        return result(False, f"{step} refused: code={e.error_code} {e.reason}")
    except Exception as e:
        return result(False, f"{step} failed: {_format_exc(e)}")

    problems = _data_problems(sub.objects)
    done = sub.done
    if done is None:
        problems.insert(0, "no downstream PUBLISH_DONE")
    if not pub.done_sent:
        problems.insert(0, "relay cancelled the upstream subscription "
                           "before PUBLISH_DONE")
        count = "none"
    else:
        count = ("unknown" if done.stream_count == _UNKNOWN_STREAM_COUNT
                 else done.stream_count)
        if done.status_code != SubscribeDoneCode.TRACK_ENDED:
            problems.insert(0, f"downstream PUBLISH_DONE status "
                               f"{done.status_code}, expected TRACK_ENDED")
        if count != "unknown" and sub.streams_ended < (count or 0):
            problems.append(f"{sub.streams_ended} of {count} downstream "
                            f"streams ended")
    received = sum(1 for o in sub.objects if _is_normal(o[4]))
    summary = (f"{received} objects received; Stream Count upstream {sent}, "
               f"downstream {count}")
    if sub.streams_reset:
        summary += f"; {sub.streams_reset} downstream stream(s) reset"
    if problems:
        return result(False, f"{problems[0]} ({len(problems)} problem(s)); "
                             f"{summary}")
    return result(True, summary)


async def test_fetch(host, port, path, use_quic, tls_disable_verify,
                     debug, supported_drafts=None, compat=frozenset(),
                     timeout=6.0) -> TestResult:
    """FETCH probe: send a standalone FETCH; relay handles if it responds
    with FETCH_OK or structured FETCH_ERROR. Timeout/close = fail."""
    t0 = time.monotonic()
    sessions = {}
    client = _make_client(host, port, path, use_quic,
                          tls_disable_verify, debug,
                          supported_drafts=supported_drafts)
    try:
        async with asyncio.timeout(timeout):
            async with client.connect() as session:
                await session.client_session_init()
                sessions["subscriber"] = _session_meta(session)
                spec_ok = True
                try:
                    await session.fetch(
                        namespace=INTEROP_NAMESPACE,
                        track_name=INTEROP_TRACK,
                        start_group=0, start_object=0,
                        end_group=0, end_object=0,
                        wait_response=True,
                    )
                    msg = "FETCH_OK received"
                except MOQTRequestError as e:
                    # Any structured FETCH_ERROR means the relay answered
                    # the FETCH (it didn't time out / close). Mirror the
                    # join probe: accept it as a refusal via _is_refusal
                    # (INTERNAL_ERROR 0x0 only under the endpoint's compat,
                    # e.g. libquicr returns code=0 for a nonexistent track),
                    # annotating spec vs non-spec codes. A timeout/close
                    # still fails via the outer except.
                    code = int(e.error_code)
                    spec_ok = _is_refusal(code, compat)
                    benign = code in SUBSCRIBE_BENIGN_ERROR_CODES
                    if benign:
                        msg = f"FETCH_ERROR (valid): code={e.error_code}"
                    elif spec_ok:
                        msg = (
                            f"FETCH_ERROR accepted (non-spec "
                            f"code={e.error_code})"
                        )
                    else:
                        msg = (
                            f"Non-conformant FETCH_ERROR code={e.error_code} "
                            f"(expected benign code or FETCH_OK)"
                        )
                session.close()
        return TestResult(
            name="fetch", passed=spec_ok,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions, message=msg,
        )
    except Exception as e:
        return TestResult(
            name="fetch", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions,
            message=f"Failed: {_format_exc(e)}",
            expected="FETCH_OK or FETCH_ERROR",
            received=_format_exc(e),
        )


async def test_join(host, port, path, use_quic, tls_disable_verify,
                    debug, supported_drafts=None, compat=frozenset(),
                    timeout=6.0) -> TestResult:
    """JOIN probe: send SUBSCRIBE + JOINING_FETCH(RELATIVE, start=0).
    Relay handles if it responds (OK or structured error). Timeout = fail."""
    t0 = time.monotonic()
    sessions = {}
    client = _make_client(host, port, path, use_quic,
                          tls_disable_verify, debug,
                          supported_drafts=supported_drafts)
    try:
        async with asyncio.timeout(timeout):
            async with client.connect() as session:
                await session.client_session_init()
                sessions["subscriber"] = _session_meta(session)
                spec_ok = True
                try:
                    await session.join(
                        namespace=INTEROP_NAMESPACE,
                        track_name=INTEROP_TRACK,
                        fetch_type=FetchType.RELATIVE_JOINING,
                        joining_start=0,
                        wait_response=True,
                    )
                    msg = "SUBSCRIBE_OK + FETCH_OK received"
                except MOQTRequestError as e:
                    # Any structured error answers the JOIN probe (the relay
                    # responded and refused); spec code noted, not required.
                    # A timeout still fails (outer except).
                    spec_ok = _is_refusal(int(e.error_code), compat)
                    benign = (int(e.error_code)
                              in SUBSCRIBE_BENIGN_ERROR_CODES)
                    msg = (
                        f"structured error (valid): code={e.error_code}"
                        if benign else
                        f"structured error accepted (non-spec "
                        f"code={e.error_code})"
                    )
                session.close()
        return TestResult(
            name="join", passed=spec_ok,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions, message=msg,
        )
    except Exception as e:
        return TestResult(
            name="join", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            sessions=sessions,
            message=f"Failed: {_format_exc(e)}",
            expected="SUBSCRIBE_OK + FETCH_OK or structured error",
            received=_format_exc(e),
        )


# ---------------------------------------------------------------------------
# Test dispatch
# ---------------------------------------------------------------------------

TEST_FUNCTIONS = {
    "setup-only": test_setup_only,
    "announce-only": test_announce_only,
    "publish-namespace-done": test_publish_namespace_done,
    "subscribe-error": test_subscribe_error,
    "rendezvous-timeout": test_rendezvous_timeout,
    "announce-subscribe": test_announce_subscribe,
    "subscribe-before-announce": test_subscribe_before_announce,
    "data-subgroup-basic": test_data_subgroup_basic,
    "namespace-discovery": test_namespace_discovery,
    "fetch": test_fetch,
    "join": test_join,
}


def parse_relay_url(url: str):
    """Parse relay URL into (host, port, path, use_quic). Thin tuple
    wrapper around aiomoqt.utils.url.parse_relay_url (which normalizes
    the WT :path)."""
    from aiomoqt.utils.url import parse_relay_url as _parse
    r = _parse(url)
    return r.host, r.port, r.path or "", r.use_quic


def parse_compat(raw: str) -> frozenset:
    """Parse comma-separated --compat / $COMPAT value into a normalized set."""
    if not raw:
        return frozenset()
    parts = {p.strip().lower() for p in raw.split(",") if p.strip()}
    unknown = parts - KNOWN_COMPAT_IMPLS
    if unknown:
        print(
            f"# warning: unknown --compat values ignored: "
            f"{','.join(sorted(unknown))} "
            f"(known: {','.join(sorted(KNOWN_COMPAT_IMPLS))})",
            file=sys.stderr,
        )
    return frozenset(parts & KNOWN_COMPAT_IMPLS)


def parse_args():
    parser = argparse.ArgumentParser(
        description="MoQ Interop Test Client (aiomoqt)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment variables: RELAY_URL, TESTCASE, MOQT_DRAFT, "
            "TLS_DISABLE_VERIFY, VERBOSE, COMPAT, "
            "NAMESPACE_PREFIX, AUTH_TOKEN"
        ),
    )
    parser.add_argument("-r", "--relay", type=str,
                        default=os.environ.get("RELAY_URL", "https://localhost"),
                        help="Relay URL (default: $RELAY_URL or https://localhost)")
    parser.add_argument("-t", "--test", type=str,
                        default=os.environ.get("TESTCASE", None),
                        help="Run specific test case")
    parser.add_argument("-l", "--list", action="store_true",
                        help="List available test cases")
    parser.add_argument("-v", "--verbose", action="store_true",
                        default=os.environ.get("VERBOSE", "").lower() in ("1", "true"),
                        help="Verbose output")
    parser.add_argument("--tls-disable-verify", action="store_true",
                        default=os.environ.get("TLS_DISABLE_VERIFY", "").lower() in ("1", "true"),
                        help="Skip TLS certificate verification")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging to stderr")
    # DRAFT is what the interop runner injects; MOQT_DRAFT is accepted
    # too so a harness setting it does not silently fall back to probing.
    draft_env = next((os.environ[k] for k in ("DRAFT", "MOQT_DRAFT")
                      if os.environ.get(k, "").strip()), None)
    parser.add_argument(
        "--draft", type=parse_draft_spec,
        default=parse_draft_spec(draft_env) if draft_env else None,
        help="MoQT draft, e.g. 18 or draft-18 (single = strict pin) or a "
             "comma list 18,16,14 (preference-ordered probe: pin the first "
             "whose SETUP completes) (env: DRAFT, MOQT_DRAFT). Default: "
             "probe 18,16,14")
    parser.add_argument(
        "--compat", type=str, default=os.environ.get("COMPAT", ""),
        help=(
            "Comma-separated list of compatibility tolerances for known "
            "non-standard endpoints. Known: "
            + ",".join(sorted(KNOWN_COMPAT_IMPLS))
            + ". Tolerated outcomes are annotated with '# COMPAT' in TAP "
              "output and 'compat: true' in the YAML block."
        ),
    )
    parser.add_argument(
        "--namespace-prefix", type=str,
        default=os.environ.get("NAMESPACE_PREFIX", ""),
        help=(
            "Prefix segment(s) prepended to the test namespace. "
            "Required for relays that scope publish/subscribe "
            "permission by namespace prefix (e.g. cdn.moq.dev /anon "
            "permits only 'anon/*'). Default empty."
        ),
    )
    parser.add_argument(
        "--auth-token", type=str,
        default=os.environ.get("AUTH_TOKEN", ""),
        help=(
            "Send the given token as the AUTH_TOKEN parameter on "
            "PUBLISH_NAMESPACE messages. Default empty (no AUTH_TOKEN "
            "sent); some anonymous-scope relays reject any AUTH_TOKEN."
        ),
    )
    return parser.parse_args()


async def run_tests(tests: list[str], host: str, port: int, path: str,
                    use_quic: bool, tls_disable_verify: bool,
                    debug: bool, supported_drafts: int = None,
                    compat: frozenset = frozenset(),
                    reporter: TAPReporter = None) -> TAPReporter:
    if reporter is None:
        reporter = TAPReporter(compat=compat)
    # Per-test namespace slot. Stricter relays (e.g. itzmanish/moq-rs)
    # cache PUBLISH_NAMESPACE_DONE state, so a later test that
    # reannounces the same namespace gets `code=0 reason=done` back.
    # Append the test name as a final segment so each test owns its
    # own announce slot.
    global INTEROP_NAMESPACE
    base_namespace = INTEROP_NAMESPACE
    # Auto-draft fallback (raw QUIC + WebTransport): probe the auto offer
    # once; if SETUP fails, pin draft-14 so a draft-14-only relay that
    # refuses the multi-version offer (moq-rs / xquic) — over raw QUIC by
    # rejecting the ALPN, or over WT by stalling the d16 SETUP — still
    # negotiates draft-14 instead of failing every case. Explicit --draft
    # is untouched; the probe is a no-op against relays that negotiate
    # auto cleanly (it just adds one connect).
    effective_draft = supported_drafts
    if isinstance(supported_drafts, (list, tuple)):
        # Preference-ordered probe (e.g. --draft 18,16,14 / DRAFT=18,16,14):
        # pin the FIRST draft whose single-ALPN SETUP completes. Each attempt
        # is a deterministic single-ALPN offer, with no multi-offer
        # server-choice vagary.
        effective_draft = None
        for d in supported_drafts:
            if await _probe_setup_ok(host, port, path, use_quic,
                                     tls_disable_verify, debug, draft=d):
                effective_draft = d
                reporter.notes.append(f"preference probe pinned draft-{d}")
                break
        if effective_draft is None:
            effective_draft = supported_drafts[-1]
            reporter.notes.append(
                "preference probe: no offered draft reached SETUP; "
                f"trying draft-{effective_draft}")
    elif supported_drafts is None and not await _probe_setup_ok(
            host, port, path, use_quic, tls_disable_verify, debug):
        effective_draft = 14
        reporter.notes.append(
            "auto multi-version handshake failed; pinned draft-14")
    for test_name in tests:
        fn = TEST_FUNCTIONS.get(test_name)
        if fn is None:
            reporter.add(TestResult(
                name=test_name, passed=True, skipped=True,
                skip_reason="Unknown test case",
            ))
            continue
        INTEROP_NAMESPACE = f"{base_namespace}/{test_name}"
        nc_before = MOQTMessage._trailing_extensions_truncation_count
        result = await fn(host, port, path, use_quic, tls_disable_verify,
                          debug, supported_drafts=effective_draft, compat=compat)
        nc_delta = (
            MOQTMessage._trailing_extensions_truncation_count - nc_before
        )
        result.wire_noncompliance_count = nc_delta
        # If the test owed its pass to wire tolerance (and wasn't
        # already annotated by a per-test compat policy), surface the
        # acceptance the same way moq-dev tolerances are surfaced.
        if (
            nc_delta > 0
            and result.passed
            and not result.compat
            and MOQTMessage._tolerate_trailing_extensions
        ):
            result.compat = True
            result.compat_note = (
                f"peer sent {nc_delta} non-compliant trailing "
                f"extensions block(s); tolerated via "
                f"--compat lenient-extensions"
            )
        reporter.add(result)
        # Print progress to stderr if verbose
        status = "PASS" if result.passed else "FAIL"
        tag = " (COMPAT)" if result.compat else ""
        print(f"  [{status}{tag}] {result.name}: {result.message}", file=sys.stderr)
    return reporter


def main():
    args = parse_args()

    if args.list:
        for name in TEST_FUNCTIONS:
            print(name)
        sys.exit(0)

    # A pinned draft we cannot speak is "not supported" (exit 127), not a
    # failure of every case; drafts we cannot speak drop out of a probe list.
    if args.draft is not None:
        speaks = sorted(int(d) for d in MOQTDraft)
        wanted = args.draft if isinstance(args.draft, list) else [args.draft]
        usable = [d for d in wanted if d in speaks]
        if not usable:
            print("TAP version 14")
            print(f"1..0 # SKIP draft {','.join(map(str, wanted))} not "
                  f"supported; aiomoqt speaks {speaks}")
            sys.exit(127)
        if isinstance(args.draft, list):
            args.draft = usable

    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.WARNING
    set_log_level(log_level)
    logging.basicConfig(level=log_level, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")

    host, port, path, use_quic = parse_relay_url(args.relay)
    compat = parse_compat(args.compat)
    # Auto-select endpoint-keyed tolerances (e.g. moq-rs-d16's truncated
    # trailing-extensions) so the runner needs no per-column COMPAT. Explicit
    # --compat still adds to whatever the endpoint contributes.
    relay_compat = _relay_compat(host)
    if relay_compat - compat:
        print(
            f"# Compat (auto for {host}): "
            f"{','.join(sorted(relay_compat - compat))}",
            file=sys.stderr,
        )
    compat = compat | relay_compat

    # Plumb the wire-tolerance flag into the deserializer module
    # before any session is created. Without this the parser stays
    # strict and a malformed extensions block raises through the
    # control-message dispatcher (default policy).
    if _compat_active(compat, "lenient-extensions"):
        MOQTMessage._tolerate_trailing_extensions = True

    # Apply namespace prefix and auth-token, if set, before the
    # tests capture INTEROP_NAMESPACE / _PUB_NS_PARAMS from module
    # scope. Both flags default off so anonymous tests look anonymous.
    global INTEROP_NAMESPACE, _PUB_NS_PARAMS, _NS_PREFIX
    prefix = args.namespace_prefix.strip("/")
    if prefix:
        INTEROP_NAMESPACE = f"{prefix}/{INTEROP_NAMESPACE}"
        _NS_PREFIX = tuple(prefix.split("/"))
    if args.auth_token:
        _PUB_NS_PARAMS = {
            ParamType.AUTH_TOKEN: args.auth_token.encode(),
        }

    # Public API takes the draft NUMBER (14, 16, ...). MOQTClient
    # normalizes to the wire form internally; pass args.draft through.
    # No explicit draft (the moq-interop-runner invokes us with RELAY_URL
    # but no DRAFT): probe newest first and pin the first draft whose SETUP
    # completes. An explicit --draft/DRAFT single int pins strictly; an
    # explicit list sets a custom probe order.
    supported_drafts = args.draft if args.draft is not None else [18, 16, 14]

    if args.verbose:
        transport = "QUIC" if use_quic else "WebTransport"
        draft_str = f" draft-{args.draft}" if args.draft else ""
        print(f"# Relay: {args.relay} ({host}:{port}/{path} via {transport}{draft_str})",
              file=sys.stderr)
        print(f"# Namespace: {INTEROP_NAMESPACE}", file=sys.stderr)
        if args.auth_token:
            print("# Auth: AUTH_TOKEN set", file=sys.stderr)
        if compat:
            print(f"# Compat: {','.join(sorted(compat))}", file=sys.stderr)

    reporter = TAPReporter(
        target_url=args.relay,
        aiomoqt_version=AIOMOQT_VERSION,
        compat=compat,
    )

    # Select tests
    if args.test:
        if args.test not in TEST_FUNCTIONS:
            print(reporter.report())  # emits headers + 1..0
            print(f"not ok 1 - {args.test} # SKIP unsupported test case")
            sys.exit(127)
        tests = [args.test]
    else:
        tests = STANDARD_TESTS

    asyncio.run(
        run_tests(tests, host, port, path, use_quic,
                  args.tls_disable_verify, args.debug,
                  supported_drafts=supported_drafts,
                  compat=compat,
                  reporter=reporter)
    )

    # TAP output to stdout
    print(reporter.report())

    # Exit code
    all_passed = all(r.passed for r in reporter.results)
    sys.exit(0 if all_passed else 1)


def cli():
    """Console entry point (moq-interop-client)."""
    main()


if __name__ == "__main__":
    cli()
