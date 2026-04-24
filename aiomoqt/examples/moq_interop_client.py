#!/usr/bin/env python3
"""
MoQ Interop Test Client — implements the 6 standard test cases from
https://github.com/englishm/moq-interop-runner

Output: TAP version 14 with YAML diagnostics.
CLI interface matches TEST-CLIENT-INTERFACE.md spec.
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from aiomoqt.types import (
    ParamType, FetchType, MOQTRequestError,
    SubscribeErrorCode, RequestErrorCode,
)

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
from aiomoqt.client import MOQTClient
from aiomoqt.utils.logger import set_log_level


def _unique_suffix():
    """Short hex timestamp for unique namespace/track per run."""
    return hex(int(time.time()) & 0xFFFF)[2:]

INTEROP_NAMESPACE = f"moq-test/{_unique_suffix()}/interop"
INTEROP_TRACK = f"track-{_unique_suffix()}"

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


class TAPReporter:
    """Emit TAP version 14 output."""

    def __init__(self):
        self.results: list[TestResult] = []

    def add(self, result: TestResult):
        self.results.append(result)

    def report(self) -> str:
        lines = ["TAP version 14", f"1..{len(self.results)}"]
        for i, r in enumerate(self.results, 1):
            status = "ok" if r.passed else "not ok"
            skip = f" # SKIP {r.skip_reason}" if r.skipped else ""
            lines.append(f"{status} {i} - {r.name}{skip}")
            # YAML diagnostic block
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
            lines.append("  ...")
        return "\n".join(lines)


def _get_connection_id(session) -> str:
    """Extract QUIC connection ID from session for diagnostics."""
    try:
        quic = session._quic
        cid = quic._host_cids[0].cid if quic._host_cids else b""
        return cid.hex() if cid else "unknown"
    except Exception:
        return "unknown"


def _make_client(host: str, port: int, endpoint: str, use_quic: bool,
                 tls_disable_verify: bool, debug: bool,
                 draft_version: int = None) -> MOQTClient:
    """Create a configured MOQTClient."""
    return MOQTClient(
        host, port,
        endpoint=endpoint,
        use_quic=use_quic,
        verify_tls=not tls_disable_verify,
        debug=debug,
        draft_version=draft_version,
    )


# ---------------------------------------------------------------------------
# Test implementations
# ---------------------------------------------------------------------------

async def test_setup_only(host, port, endpoint, use_quic, tls_disable_verify,
                          debug, draft_version=None, timeout=2.0) -> TestResult:
    """Test 1: Connect, exchange SETUP, graceful close."""
    t0 = time.monotonic()
    client = _make_client(host, port, endpoint, use_quic, tls_disable_verify, debug, draft_version=draft_version)
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
            message=f"Failed: {e}",
            expected="SERVER_SETUP received",
            received=str(e),
        )


async def test_announce_only(host, port, endpoint, use_quic, tls_disable_verify,
                             debug, draft_version=None, timeout=2.0) -> TestResult:
    """Test 2: SETUP + PUBLISH_NAMESPACE + receive OK."""
    t0 = time.monotonic()
    client = _make_client(host, port, endpoint, use_quic, tls_disable_verify, debug, draft_version=draft_version)
    try:
        async with asyncio.timeout(timeout):
            async with client.connect() as session:
                await session.client_session_init()
                cid = _get_connection_id(session)

                await session.publish_namespace(
                    namespace=INTEROP_NAMESPACE,
                    parameters={ParamType.AUTH_TOKEN: b"interop-test"},
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
            message=f"Failed: {e}",
            expected="PUBLISH_NAMESPACE_OK",
            received=str(e),
        )


async def test_publish_namespace_done(host, port, endpoint, use_quic,
                                      tls_disable_verify, debug,
                                      draft_version=None, timeout=2.0) -> TestResult:
    """Test 3: SETUP + PUBLISH_NAMESPACE + OK + PUBLISH_NAMESPACE_DONE + close."""
    t0 = time.monotonic()
    client = _make_client(host, port, endpoint, use_quic, tls_disable_verify, debug, draft_version=draft_version)
    try:
        async with asyncio.timeout(timeout):
            async with client.connect() as session:
                await session.client_session_init()
                cid = _get_connection_id(session)

                response = await session.publish_namespace(
                    namespace=INTEROP_NAMESPACE,
                    parameters={ParamType.AUTH_TOKEN: b"interop-test"},
                    wait_response=True,
                )
                # Now send PUBLISH_NAMESPACE_DONE
                ns_tuple = session._make_namespace_tuple(INTEROP_NAMESPACE)
                session.publish_namespace_done(
                    namespace=ns_tuple,
                    request_id=response.request_id,
                )
                # Brief pause to let the message flush
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
            message=f"Failed: {e}",
            expected="PUBLISH_NAMESPACE_OK + PUBLISH_NAMESPACE_DONE",
            received=str(e),
        )


async def test_subscribe_error(host, port, endpoint, use_quic,
                               tls_disable_verify, debug,
                               draft_version=None, timeout=2.0) -> TestResult:
    """Test 4: SUBSCRIBE to non-existent track, expect SUBSCRIBE_ERROR."""
    t0 = time.monotonic()
    cid = "unknown"
    client = _make_client(host, port, endpoint, use_quic, tls_disable_verify, debug, draft_version=draft_version)
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
                    # If we get here, no error — unexpected
                    session.close()
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
                    spec_ok = int(e.error_code) in TRACK_NOT_FOUND_CODES
                    return TestResult(
                        name="subscribe-error",
                        passed=spec_ok,
                        duration_ms=(time.monotonic() - t0) * 1000,
                        connection_id=cid,
                        message=(
                            f"Error received (expected): "
                            f"code={e.error_code}"
                            if spec_ok else
                            f"Non-conformant: expected TRACK_DOES_NOT_EXIST "
                            f"(d14=0x04 / d16=0x10), got code={e.error_code}"
                        ),
                        expected="SUBSCRIBE_ERROR code=TRACK_DOES_NOT_EXIST",
                        received=(
                            "" if spec_ok
                            else f"SUBSCRIBE_ERROR code={e.error_code}"
                        ),
                    )
    except Exception as e:
        return TestResult(
            name="subscribe-error", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            message=f"Failed: {e}",
            expected="error response",
            received=str(e),
        )


async def test_announce_subscribe(host, port, endpoint, use_quic,
                                  tls_disable_verify, debug,
                                  draft_version=None, timeout=3.0) -> TestResult:
    """Test 5: Two connections — publisher announces, subscriber subscribes."""
    t0 = time.monotonic()
    pub_cid = "unknown"
    sub_cid = "unknown"

    try:
        async with asyncio.timeout(timeout):
            # Publisher connection
            pub_client = _make_client(host, port, endpoint, use_quic,
                                      tls_disable_verify, debug, draft_version=draft_version)
            sub_client = _make_client(host, port, endpoint, use_quic,
                                      tls_disable_verify, debug, draft_version=draft_version)

            async with pub_client.connect() as pub_session:
                await pub_session.client_session_init()
                pub_cid = _get_connection_id(pub_session)

                # Publisher announces namespace
                await pub_session.publish_namespace(
                    namespace=INTEROP_NAMESPACE,
                    parameters={ParamType.AUTH_TOKEN: b"interop-test"},
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
            message=f"Failed: {e}",
        )


async def test_subscribe_before_announce(host, port, endpoint, use_quic,
                                         tls_disable_verify, debug,
                                         draft_version=None, timeout=3.5) -> TestResult:
    """Test 6: Subscriber connects first, publisher 500ms later. Both outcomes valid."""
    t0 = time.monotonic()
    pub_cid = "unknown"
    sub_cid = "unknown"
    sub_response = None

    try:
        async with asyncio.timeout(timeout):
            sub_client = _make_client(host, port, endpoint, use_quic,
                                      tls_disable_verify, debug, draft_version=draft_version)
            pub_client = _make_client(host, port, endpoint, use_quic,
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
                        parameters={ParamType.AUTH_TOKEN: b"interop-test"},
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
        if sub_response is None:
            msg = "Timeout waiting for subscriber response"
            passed = False
        elif isinstance(sub_response, MOQTRequestError):
            spec_ok = int(sub_response.error_code) in SUBSCRIBE_BENIGN_ERROR_CODES
            passed = spec_ok
            msg = (
                f"Error received (valid: relay didn't buffer): "
                f"code={sub_response.error_code}"
                if spec_ok else
                f"Non-conformant error: expected a benign code "
                f"(TRACK_DOES_NOT_EXIST / UNAUTHORIZED / TIMEOUT / "
                f"NOT_SUPPORTED), got code={sub_response.error_code}"
            )
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
        )
    except Exception as e:
        return TestResult(
            name="subscribe-before-announce", passed=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            publisher_connection_id=pub_cid,
            subscriber_connection_id=sub_cid,
            message=f"Failed: {e}",
        )


async def test_fetch(host, port, endpoint, use_quic, tls_disable_verify,
                     debug, draft_version=None, timeout=3.0) -> TestResult:
    """FETCH probe: send a standalone FETCH; relay handles if it responds
    with FETCH_OK or structured FETCH_ERROR. Timeout/close = fail."""
    t0 = time.monotonic()
    client = _make_client(host, port, endpoint, use_quic,
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
            message=f"Failed: {e}",
            expected="FETCH_OK or FETCH_ERROR",
            received=str(e),
        )


async def test_join(host, port, endpoint, use_quic, tls_disable_verify,
                    debug, draft_version=None, timeout=3.0) -> TestResult:
    """JOIN probe: send SUBSCRIBE + JOINING_FETCH(RELATIVE, start=0).
    Relay handles if it responds (OK or structured error). Timeout = fail."""
    t0 = time.monotonic()
    client = _make_client(host, port, endpoint, use_quic,
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
            message=f"Failed: {e}",
            expected="SUBSCRIBE_OK + FETCH_OK or structured error",
            received=str(e),
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
    """Parse relay URL into (host, port, endpoint, use_quic)."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme == "moqt":
        use_quic = True
        port = parsed.port or 443
    elif scheme in ("https", "wss"):
        use_quic = False
        port = parsed.port or 443
    else:
        # Bare host:port — default to WebTransport
        use_quic = False
        port = parsed.port or 443

    host = parsed.hostname or url.split(":")[0]
    endpoint = parsed.path.lstrip("/")

    return host, port, endpoint, use_quic


def parse_args():
    parser = argparse.ArgumentParser(
        description="MoQ Interop Test Client (aiomoqt)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Environment variables: RELAY_URL, TESTCASE, TLS_DISABLE_VERIFY, VERBOSE",
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
    return parser.parse_args()


async def run_tests(tests: list[str], host: str, port: int, endpoint: str,
                    use_quic: bool, tls_disable_verify: bool,
                    debug: bool, draft_version: int = None) -> TAPReporter:
    reporter = TAPReporter()
    for test_name in tests:
        fn = TEST_FUNCTIONS.get(test_name)
        if fn is None:
            reporter.add(TestResult(
                name=test_name, passed=True, skipped=True,
                skip_reason="Unknown test case",
            ))
            continue
        result = await fn(host, port, endpoint, use_quic, tls_disable_verify,
                          debug, draft_version=draft_version)
        reporter.add(result)
        # Print progress to stderr if verbose
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {result.name}: {result.message}", file=sys.stderr)
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

    host, port, endpoint, use_quic = parse_relay_url(args.relay)

    # Resolve draft version
    draft_version = None
    if args.draft:
        draft_version = 0xff000000 + args.draft

    if args.verbose:
        transport = "QUIC" if use_quic else "WebTransport"
        draft_str = f" draft-{args.draft}" if args.draft else ""
        print(f"# Relay: {args.relay} ({host}:{port}/{endpoint} via {transport}{draft_str})",
              file=sys.stderr)

    # Select tests
    if args.test:
        if args.test not in TEST_FUNCTIONS:
            print("TAP version 14")
            print("1..1")
            print(f"not ok 1 - {args.test} # SKIP unsupported test case")
            sys.exit(127)
        tests = [args.test]
    else:
        tests = STANDARD_TESTS

    reporter = asyncio.run(
        run_tests(tests, host, port, endpoint, use_quic,
                  args.tls_disable_verify, args.debug,
                  draft_version=draft_version)
    )

    # TAP output to stdout
    print(reporter.report())

    # Exit code
    all_passed = all(r.passed for r in reporter.results)
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
