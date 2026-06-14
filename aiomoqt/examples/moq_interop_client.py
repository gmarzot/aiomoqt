#!/usr/bin/env python3
"""
MoQ Interop Test Client — implements the 6 standard test cases from
https://github.com/englishm/moq-interop-runner

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
import logging
import os
import sys
import time
from dataclasses import dataclass

from aiomoqt.client import MOQTClient
from aiomoqt.messages.base import MOQTMessage
from aiomoqt.types import (
    ParamType, FetchType, MOQTRequestError,
    SubscribeErrorCode, RequestErrorCode,
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


def _compat_active(compat: frozenset, key: str) -> bool:
    return "all" in compat or key in compat


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
    "announce-subscribe",
    "subscribe-before-announce",
]


@dataclass
class TestResult:
    name: str
    passed: bool
    duration_ms: float = 0.0
    message: str = ""
    connection_id: str = ""
    publisher_connection_id: str = ""
    subscriber_connection_id: str = ""
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
        lines.append(f"1..{len(self.results)}")
        for i, r in enumerate(self.results, 1):
            status = "ok" if r.passed else "not ok"
            skip = f" # SKIP {r.skip_reason}" if r.skipped else ""
            tag = " # COMPAT" if r.compat and not r.skipped else ""
            lines.append(f"{status} {i} - {r.name}{skip}{tag}")
            lines.append("  ---")
            lines.append(f"  duration_ms: {r.duration_ms:.1f}")
            if r.connection_id:
                lines.append(f"  connection_id: {r.connection_id}")
            if r.publisher_connection_id:
                lines.append(f"  publisher_connection_id: {r.publisher_connection_id}")
            if r.subscriber_connection_id:
                lines.append(f"  subscriber_connection_id: {r.subscriber_connection_id}")
            if r.message:
                lines.append(f"  message: {r.message}")
            if r.expected:
                lines.append(f"  expected: {r.expected}")
            if r.received:
                lines.append(f"  received: {r.received}")
            if r.compat:
                lines.append("  compat: true")
                if r.compat_note:
                    lines.append(f"  compat_note: {r.compat_note}")
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


def _get_connection_id(session) -> str:
    """Extract QUIC connection ID from session for diagnostics."""
    try:
        quic = session._quic
        cid = quic._host_cids[0].cid if quic._host_cids else b""
        return cid.hex() if cid else "unknown"
    except Exception:
        return "unknown"


def _make_client(host: str, port: int, path: str, use_quic: bool,
                 tls_disable_verify: bool, debug: bool,
                 draft_version: int = None) -> MOQTClient:
    """Create a configured MOQTClient."""
    return MOQTClient(
        host, port,
        path=path,
        use_quic=use_quic,
        verify_tls=not tls_disable_verify,
        debug=debug,
        draft_version=draft_version,
    )


# ---------------------------------------------------------------------------
# Test implementations
# ---------------------------------------------------------------------------

async def test_setup_only(host, port, path, use_quic, tls_disable_verify,
                          debug, draft_version=None, compat=frozenset(),
                          timeout=5.0) -> TestResult:
    """Test 1: Connect, exchange SETUP, graceful close."""
    t0 = time.monotonic()
    client = _make_client(host, port, path, use_quic, tls_disable_verify, debug, draft_version=draft_version)
    try:
        async with asyncio.timeout(timeout):
            async with client.connect() as session:
                await session.client_session_init()
                cid = _get_connection_id(session)
                session.close()
        return TestResult(
            name="setup-only", passed=True,
            duration_ms=(time.monotonic() - t0) * 1000,
            connection_id=cid,
            message="SERVER_SETUP received with compatible version",
        )
    except Exception as e:
        return TestResult(
            name="setup-only", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            message=f"Failed: {_format_exc(e)}",
            expected="SERVER_SETUP received",
            received=_format_exc(e),
        )


async def test_announce_only(host, port, path, use_quic, tls_disable_verify,
                             debug, draft_version=None, compat=frozenset(),
                             timeout=5.0) -> TestResult:
    """Test 2: SETUP + PUBLISH_NAMESPACE + receive OK."""
    t0 = time.monotonic()
    client = _make_client(host, port, path, use_quic, tls_disable_verify, debug, draft_version=draft_version)
    try:
        async with asyncio.timeout(timeout):
            async with client.connect() as session:
                await session.client_session_init()
                cid = _get_connection_id(session)

                await session.publish_namespace(
                    namespace=INTEROP_NAMESPACE,
                    parameters=_PUB_NS_PARAMS,
                    wait_response=True,
                )
                session.close()
        return TestResult(
            name="announce-only", passed=True,
            duration_ms=(time.monotonic() - t0) * 1000,
            connection_id=cid,
            message="PUBLISH_NAMESPACE_OK received",
        )
    except Exception as e:
        return TestResult(
            name="announce-only", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            message=f"Failed: {_format_exc(e)}",
            expected="PUBLISH_NAMESPACE_OK",
            received=_format_exc(e),
        )


async def test_publish_namespace_done(host, port, path, use_quic,
                                      tls_disable_verify, debug,
                                      draft_version=None,
                                      compat=frozenset(),
                                      timeout=5.0) -> TestResult:
    """Test 3: SETUP + PUBLISH_NAMESPACE + OK + PUBLISH_NAMESPACE_DONE + close."""
    t0 = time.monotonic()
    client = _make_client(host, port, path, use_quic, tls_disable_verify, debug, draft_version=draft_version)
    try:
        async with asyncio.timeout(timeout):
            async with client.connect() as session:
                await session.client_session_init()
                cid = _get_connection_id(session)

                response = await session.publish_namespace(
                    namespace=INTEROP_NAMESPACE,
                    parameters=_PUB_NS_PARAMS,
                    wait_response=True,
                )
                ns_tuple = session._make_namespace_tuple(INTEROP_NAMESPACE)
                session.publish_namespace_done(
                    namespace=ns_tuple,
                    request_id=response.request_id,
                )
                await asyncio.sleep(0.1)
                session.close()

        return TestResult(
            name="publish-namespace-done", passed=True,
            duration_ms=(time.monotonic() - t0) * 1000,
            connection_id=cid,
            message="PUBLISH_NAMESPACE_OK received, PUBLISH_NAMESPACE_DONE sent",
        )
    except Exception as e:
        return TestResult(
            name="publish-namespace-done", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            message=f"Failed: {_format_exc(e)}",
            expected="PUBLISH_NAMESPACE_OK + PUBLISH_NAMESPACE_DONE",
            received=_format_exc(e),
        )


async def test_subscribe_error(host, port, path, use_quic,
                               tls_disable_verify, debug,
                               draft_version=None, compat=frozenset(),
                               timeout=5.0) -> TestResult:
    """Test 4: SUBSCRIBE to non-existent track, expect SUBSCRIBE_ERROR.

    Compat (`moq-dev` / `moq-rs`): the relay returns its own non-spec
    "not found" code (404 for moq-dev, 0 for moq-rs d14). We accept
    *any* structured error as a valid "track not found" signal with
    annotation. A timeout or transport error still fails.
    """
    moq_dev_compat = _compat_active(compat, "moq-dev")
    moq_rs_compat = _compat_active(compat, "moq-rs")
    libquicr_compat = _compat_active(compat, "libquicr")
    accept_any_error = moq_dev_compat or moq_rs_compat
    t0 = time.monotonic()
    cid = "unknown"
    client = _make_client(host, port, path, use_quic, tls_disable_verify, debug, draft_version=draft_version)
    try:
        async with asyncio.timeout(timeout):
            async with client.connect() as session:
                await session.client_session_init()
                cid = _get_connection_id(session)
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
                            connection_id=cid,
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
                        connection_id=cid,
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
                            connection_id=cid,
                            message=f"Error received (expected): code={e.error_code}",
                            expected="SUBSCRIBE_ERROR code=TRACK_DOES_NOT_EXIST",
                        )
                    if accept_any_error:
                        impl = "moq-rs" if moq_rs_compat else "moq-dev"
                        return TestResult(
                            name="subscribe-error", passed=True,
                            duration_ms=(time.monotonic() - t0) * 1000,
                            connection_id=cid,
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
                        connection_id=cid,
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
            message=f"Failed: {_format_exc(e)}",
            expected="error response",
            received=_format_exc(e),
        )


async def test_announce_subscribe(host, port, path, use_quic,
                                  tls_disable_verify, debug,
                                  draft_version=None, compat=frozenset(),
                                  timeout=6.0) -> TestResult:
    """Test 5: Two connections — publisher announces, subscriber subscribes."""
    t0 = time.monotonic()
    pub_cid = "unknown"
    sub_cid = "unknown"

    try:
        async with asyncio.timeout(timeout):
            # Publisher connection
            pub_client = _make_client(host, port, path, use_quic,
                                      tls_disable_verify, debug, draft_version=draft_version)
            sub_client = _make_client(host, port, path, use_quic,
                                      tls_disable_verify, debug, draft_version=draft_version)

            async with pub_client.connect() as pub_session:
                await pub_session.client_session_init()
                pub_cid = _get_connection_id(pub_session)

                await pub_session.publish_namespace(
                    namespace=INTEROP_NAMESPACE,
                    parameters=_PUB_NS_PARAMS,
                    wait_response=True,
                )

                # Subscriber connection
                async with sub_client.connect() as sub_session:
                    await sub_session.client_session_init()
                    sub_cid = _get_connection_id(sub_session)

                    try:
                        await sub_session.subscribe(
                            namespace=INTEROP_NAMESPACE,
                            track_name=INTEROP_TRACK,
                            wait_response=True,
                        )
                        msg = "SUBSCRIBE_OK received — relay routed subscription"
                        passed = True
                    except MOQTRequestError as e:
                        msg = f"SUBSCRIBE_ERROR: upstream subscribe failed: {e.reason}"
                        passed = False

                    sub_session.close()
                pub_session.close()

        return TestResult(
            name="announce-subscribe", passed=passed,
            duration_ms=(time.monotonic() - t0) * 1000,
            publisher_connection_id=pub_cid,
            subscriber_connection_id=sub_cid,
            message=msg,
        )
    except Exception as e:
        return TestResult(
            name="announce-subscribe", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            publisher_connection_id=pub_cid,
            subscriber_connection_id=sub_cid,
            message=f"Failed: {_format_exc(e)}",
            received=_format_exc(e),
        )


async def test_subscribe_before_announce(host, port, path, use_quic,
                                         tls_disable_verify, debug,
                                         draft_version=None,
                                         compat=frozenset(),
                                         timeout=7.0) -> TestResult:
    """Test 6: Subscriber connects first, publisher 500ms later. Both outcomes valid."""
    moq_dev_compat = _compat_active(compat, "moq-dev")
    moq_rs_compat = _compat_active(compat, "moq-rs")
    accept_any_error = moq_dev_compat or moq_rs_compat
    t0 = time.monotonic()
    pub_cid = "unknown"
    sub_cid = "unknown"
    sub_response = None

    try:
        async with asyncio.timeout(timeout):
            sub_client = _make_client(host, port, path, use_quic,
                                      tls_disable_verify, debug, draft_version=draft_version)
            pub_client = _make_client(host, port, path, use_quic,
                                      tls_disable_verify, debug, draft_version=draft_version)

            async with sub_client.connect() as sub_session:
                await sub_session.client_session_init()
                sub_cid = _get_connection_id(sub_session)

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
                    pub_cid = _get_connection_id(pub_session)

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
            elif accept_any_error:
                impl = "moq-rs" if moq_rs_compat else "moq-dev"
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
            publisher_connection_id=pub_cid,
            subscriber_connection_id=sub_cid,
            message=msg,
            compat=compat_used,
            compat_note=compat_reason,
        )
    except Exception as e:
        return TestResult(
            name="subscribe-before-announce", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            publisher_connection_id=pub_cid,
            subscriber_connection_id=sub_cid,
            message=f"Failed: {_format_exc(e)}",
            received=_format_exc(e),
        )


async def test_fetch(host, port, path, use_quic, tls_disable_verify,
                     debug, draft_version=None, compat=frozenset(),
                     timeout=6.0) -> TestResult:
    """FETCH probe: send a standalone FETCH; relay handles if it responds
    with FETCH_OK or structured FETCH_ERROR. Timeout/close = fail."""
    t0 = time.monotonic()
    client = _make_client(host, port, path, use_quic,
                          tls_disable_verify, debug,
                          draft_version=draft_version)
    try:
        async with asyncio.timeout(timeout):
            async with client.connect() as session:
                await session.client_session_init()
                cid = _get_connection_id(session)
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
                    spec_ok = int(e.error_code) in SUBSCRIBE_BENIGN_ERROR_CODES
                    msg = (
                        f"FETCH_ERROR (valid): code={e.error_code}"
                        if spec_ok else
                        f"Non-conformant FETCH_ERROR code={e.error_code} "
                        f"(expected benign code or FETCH_OK)"
                    )
                session.close()
        return TestResult(
            name="fetch", passed=spec_ok,
            duration_ms=(time.monotonic() - t0) * 1000,
            connection_id=cid, message=msg,
        )
    except Exception as e:
        return TestResult(
            name="fetch", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            message=f"Failed: {_format_exc(e)}",
            expected="FETCH_OK or FETCH_ERROR",
            received=_format_exc(e),
        )


async def test_join(host, port, path, use_quic, tls_disable_verify,
                    debug, draft_version=None, compat=frozenset(),
                    timeout=6.0) -> TestResult:
    """JOIN probe: send SUBSCRIBE + JOINING_FETCH(RELATIVE, start=0).
    Relay handles if it responds (OK or structured error). Timeout = fail."""
    t0 = time.monotonic()
    client = _make_client(host, port, path, use_quic,
                          tls_disable_verify, debug,
                          draft_version=draft_version)
    try:
        async with asyncio.timeout(timeout):
            async with client.connect() as session:
                await session.client_session_init()
                cid = _get_connection_id(session)
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
                    spec_ok = int(e.error_code) in SUBSCRIBE_BENIGN_ERROR_CODES
                    msg = (
                        f"structured error (valid): code={e.error_code}"
                        if spec_ok else
                        f"Non-conformant error: code={e.error_code}"
                    )
                session.close()
        return TestResult(
            name="join", passed=spec_ok,
            duration_ms=(time.monotonic() - t0) * 1000,
            connection_id=cid, message=msg,
        )
    except Exception as e:
        return TestResult(
            name="join", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
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
    "announce-subscribe": test_announce_subscribe,
    "subscribe-before-announce": test_subscribe_before_announce,
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
            "Environment variables: RELAY_URL, TESTCASE, "
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
    parser.add_argument("--draft", type=int, default=None,
                        help="MoQT draft version (14 or 16, default: auto/14)")
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
                    debug: bool, draft_version: int = None,
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
                          debug, draft_version=draft_version, compat=compat)
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

    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.WARNING
    set_log_level(log_level)
    logging.basicConfig(level=log_level, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")

    host, port, path, use_quic = parse_relay_url(args.relay)
    compat = parse_compat(args.compat)

    # Plumb the wire-tolerance flag into the deserializer module
    # before any session is created. Without this the parser stays
    # strict and a malformed extensions block raises through the
    # control-message dispatcher (default policy).
    if _compat_active(compat, "lenient-extensions"):
        MOQTMessage._tolerate_trailing_extensions = True

    # Apply namespace prefix and auth-token, if set, before the
    # tests capture INTEROP_NAMESPACE / _PUB_NS_PARAMS from module
    # scope. Both flags default off so anonymous tests look anonymous.
    global INTEROP_NAMESPACE, _PUB_NS_PARAMS
    prefix = args.namespace_prefix.strip("/")
    if prefix:
        INTEROP_NAMESPACE = f"{prefix}/{INTEROP_NAMESPACE}"
    if args.auth_token:
        _PUB_NS_PARAMS = {
            ParamType.AUTH_TOKEN: args.auth_token.encode(),
        }

    # Public API takes the draft NUMBER (14, 16, ...). MOQTClient
    # normalizes to the wire form internally; pass args.draft through.
    draft_version = args.draft if args.draft else None

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
                  draft_version=draft_version,
                  compat=compat,
                  reporter=reporter)
    )

    # TAP output to stdout
    print(reporter.report())

    # Exit code
    all_passed = all(r.passed for r in reporter.results)
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
