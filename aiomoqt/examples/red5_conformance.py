#!/usr/bin/env python3
"""Red5 MoQT conformance tests — targeted protocol compliance checks.

Tests the subscribe establishment flow against a Red5 relay to
identify non-conformant behavior.

Usage:
  python3 -m aiomoqt.examples.red5_conformance --host HOST --port PORT [--draft 14|16]
"""
import asyncio
import argparse
import logging
import time

from aiomoqt.client import MOQTClient
from aiomoqt.types import (
    MOQTMessageType, ParamType, FilterType, GroupOrder,
    MOQTException, MOQTRequestError,
    MOQT_VERSION_DRAFT14, MOQT_VERSION_DRAFT16,
)
from aiomoqt.messages import (
    Subscribe, SubscribeOk, SubscribeError,
    Publish, PublishOk, PublishError,
    PublishNamespace, PublishNamespaceOk,
    SubscribeNamespace, SubscribeNamespaceOk,
)
from aiomoqt.context import is_draft16_or_later
from aiomoqt.utils.logger import set_log_level, get_logger

logger = get_logger(__name__)


def parse_args():
    p = argparse.ArgumentParser(description='Red5 MoQT conformance tests')
    p.add_argument('--host', required=True)
    p.add_argument('--port', type=int, default=4433)
    p.add_argument('--namespace', default='live/stream1')
    p.add_argument('--trackname', default='video0')
    p.add_argument('--draft', type=int, default=14)
    p.add_argument('--debug', action='store_true')
    return p.parse_args()


class ConformanceResult:
    def __init__(self, name):
        self.name = name
        self.passed = None
        self.detail = ""

    def pass_(self, detail=""):
        self.passed = True
        self.detail = detail
        print(f"  [PASS] {self.name}: {detail}")

    def fail(self, detail=""):
        self.passed = False
        self.detail = detail
        print(f"  [FAIL] {self.name}: {detail}")

    def skip(self, detail=""):
        self.passed = None
        self.detail = detail
        print(f"  [SKIP] {self.name}: {detail}")


async def test_setup(host, port, draft):
    """Test 1: CLIENT_SETUP / SERVER_SETUP handshake."""
    r = ConformanceResult("setup")
    try:
        client = MOQTClient(host, port, path='',
                            use_quic=True, draft_version=draft)
        async with client.connect() as session:
            await session.client_session_init()
            r.pass_(f"ServerSetup received, version=0x{session._moqt_version:x}")
    except Exception as e:
        r.fail(str(e))
    return r


async def test_publish_namespace(host, port, draft, namespace):
    """Test 2: PUBLISH_NAMESPACE → must get PUBLISH_NAMESPACE_OK or error."""
    r = ConformanceResult("publish_namespace response")
    try:
        client = MOQTClient(host, port, path='',
                            use_quic=True, draft_version=draft)
        async with client.connect() as session:
            await session.client_session_init()
            resp = await session.publish_namespace(
                namespace_prefix=namespace,
                parameters={},
                wait_response=True,
            )
            if isinstance(resp, (PublishNamespaceOk,)):
                r.pass_(f"PUBLISH_NAMESPACE_OK received")
            else:
                r.fail(f"unexpected response: {type(resp).__name__}")
    except MOQTRequestError as e:
        r.pass_(f"PUBLISH_NAMESPACE_ERROR (valid): {e}")
    except asyncio.TimeoutError:
        r.fail("no response (timeout) — spec requires exactly one response")
    except Exception as e:
        r.fail(str(e))
    return r


async def test_publish_response(host, port, draft, namespace, trackname):
    """Test 3: PUBLISH(forward=0) → must get PUBLISH_OK or PUBLISH_ERROR.
    Spec §5.1: 'A subscriber MUST send exactly one PUBLISH_OK or
    PUBLISH_ERROR in response to a PUBLISH.'"""
    r = ConformanceResult("publish response (forward=0)")
    try:
        client = MOQTClient(host, port, path='',
                            use_quic=True, draft_version=draft)
        async with client.connect() as session:
            await session.client_session_init()
            await session.publish_namespace(
                namespace_prefix=namespace,
                parameters={},
                wait_response=True,
            )
            # Send PUBLISH(forward=0) and wait for response
            resp = await session.publish(
                namespace=namespace,
                track_name=trackname,
                forward=0,
                wait_response=True,
            )
            if isinstance(resp, PublishOk):
                r.pass_(f"PUBLISH_OK: forward={resp.forward}")
            elif isinstance(resp, PublishError):
                r.pass_(f"PUBLISH_ERROR (valid): {resp.error_code}")
            else:
                r.fail(f"unexpected: {type(resp).__name__}")
    except MOQTRequestError as e:
        r.pass_(f"error response (valid): {e}")
    except asyncio.TimeoutError:
        r.fail("no response (timeout) — spec MUST respond")
    except Exception as e:
        r.fail(str(e))
    return r


async def test_subscribe_namespace_response(host, port, draft, namespace):
    """Test 4: SUBSCRIBE_NAMESPACE → must get OK/ERROR response."""
    r = ConformanceResult("subscribe_namespace response")
    try:
        client = MOQTClient(host, port, path='',
                            use_quic=True, draft_version=draft)
        async with client.connect() as session:
            await session.client_session_init()
            resp = await session.subscribe_namespace(
                namespace_prefix=namespace,
                parameters={},
                wait_response=True,
            )
            r.pass_(f"response received: {type(resp).__name__}")
    except MOQTRequestError as e:
        r.pass_(f"error response (valid): {e}")
    except asyncio.TimeoutError:
        r.fail("no response (timeout)")
    except Exception as e:
        r.fail(str(e))
    return r


async def test_subscribe_namespace_publish_forwarding(
        host, port, draft, namespace, trackname):
    """Test 5: After SUBSCRIBE_NAMESPACE, relay should forward
    PUBLISH messages for tracks in the namespace.
    Spec §6.1: 'The recipient will send any relevant NAMESPACE,
    NAMESPACE_DONE or PUBLISH messages for that namespace.'"""
    r = ConformanceResult("subscribe_namespace forwards PUBLISH")
    try:
        client = MOQTClient(host, port, path='',
                            use_quic=True, draft_version=draft)
        async with client.connect() as session:
            await session.client_session_init()
            await session.subscribe_namespace(
                namespace_prefix=namespace,
                parameters={},
                wait_response=True,
            )
            # Wait for PUBLISH announcement
            try:
                pub = await session.await_publish(timeout=5)
                ns = '/'.join(
                    p.decode() if isinstance(p, bytes) else p
                    for p in pub.track_namespace)
                tn = (pub.track_name.decode()
                      if isinstance(pub.track_name, bytes)
                      else pub.track_name)
                r.pass_(f"PUBLISH received: {ns}/{tn} "
                        f"forward={pub.forward}")
            except asyncio.TimeoutError:
                r.fail("no PUBLISH received within 5s — "
                       "relay should forward track announcements")
    except Exception as e:
        r.fail(str(e))
    return r


async def test_subscribe_response(host, port, draft, namespace, trackname):
    """Test 6: SUBSCRIBE → must get SUBSCRIBE_OK or SUBSCRIBE_ERROR.
    Spec §5.1: 'A publisher MUST send exactly one SUBSCRIBE_OK or
    SUBSCRIBE_ERROR in response to a SUBSCRIBE.'"""
    r = ConformanceResult("subscribe response")
    try:
        client = MOQTClient(host, port, path='',
                            use_quic=True, draft_version=draft)
        async with client.connect() as session:
            await session.client_session_init()
            resp = await session.subscribe(
                namespace=namespace,
                track_name=trackname,
                forward=1,
                filter_type=FilterType.LATEST_OBJECT,
                parameters={},
                wait_response=True,
            )
            if isinstance(resp, SubscribeOk):
                r.pass_(f"SUBSCRIBE_OK: content_exists="
                        f"{resp.content_exists} "
                        f"largest=({resp.largest_group_id},"
                        f"{resp.largest_object_id})")
            else:
                r.fail(f"unexpected: {type(resp).__name__}")
    except MOQTRequestError as e:
        r.pass_(f"SUBSCRIBE_ERROR (valid): {e}")
    except asyncio.TimeoutError:
        r.fail("no response (timeout) — spec MUST respond")
    except Exception as e:
        r.fail(str(e))
    return r


async def test_subscribe_data_delivery(host, port, draft,
                                        namespace, trackname):
    """Test 7: After SUBSCRIBE_OK with content_exists=1, data
    should arrive on a uni stream."""
    r = ConformanceResult("subscribe data delivery")
    objects = []

    def on_obj(msg, size, ts, gid, sid):
        objects.append((gid, msg.object_id, size))

    try:
        client = MOQTClient(host, port, path='',
                            use_quic=True, draft_version=draft)
        async with client.connect() as session:
            await session.client_session_init()
            session.on_object_received = on_obj

            resp = await session.subscribe(
                namespace=namespace,
                track_name=trackname,
                forward=1,
                filter_type=FilterType.LATEST_OBJECT,
                parameters={},
                wait_response=True,
            )
            if not isinstance(resp, SubscribeOk):
                r.skip(f"subscribe failed: {type(resp).__name__}")
                return r

            # Wait for data
            await asyncio.sleep(5)

            if objects:
                r.pass_(f"{len(objects)} objects received, "
                        f"first: g={objects[0][0]} o={objects[0][1]} "
                        f"size={objects[0][2]}B")
            else:
                if resp.content_exists == 1:
                    r.fail("SUBSCRIBE_OK says content_exists=1 "
                           "but no data arrived in 5s")
                else:
                    r.skip("content_exists=0, no data expected")
    except MOQTRequestError as e:
        r.skip(f"subscribe error: {e}")
    except Exception as e:
        r.fail(str(e))
    return r


async def test_track_alias_established(host, port, draft,
                                        namespace, trackname):
    """Test 8: Data streams should use a track_alias that was
    established via PUBLISH or SUBSCRIBE_OK. Receiving data on
    an unestablished alias is non-conformant."""
    r = ConformanceResult("track_alias established before data")
    unestablished = []

    def on_obj(msg, size, ts, gid, sid):
        pass  # data callback

    try:
        client = MOQTClient(host, port, path='',
                            use_quic=True, draft_version=draft)
        async with client.connect() as session:
            await session.client_session_init()
            session.on_object_received = on_obj

            # Subscribe
            resp = await session.subscribe(
                namespace=namespace,
                track_name=trackname,
                forward=1,
                filter_type=FilterType.LATEST_OBJECT,
                parameters={},
                wait_response=True,
            )
            if not isinstance(resp, SubscribeOk):
                r.skip("subscribe failed")
                return r

            # Collect warnings about unknown track_alias
            import io
            import logging as _logging
            log_capture = io.StringIO()
            handler = _logging.StreamHandler(log_capture)
            handler.setLevel(_logging.WARNING)
            _logging.getLogger('aiomoqt.protocol').addHandler(handler)

            await asyncio.sleep(3)

            _logging.getLogger('aiomoqt.protocol').removeHandler(handler)
            warnings = log_capture.getvalue()

            if "unknown track_alias" in warnings:
                count = warnings.count("unknown track_alias")
                r.fail(f"{count} data streams with unestablished "
                       f"track_alias (spec §10.4.2)")
            else:
                r.pass_("all data streams used established aliases")
    except Exception as e:
        r.fail(str(e))
    return r


async def main():
    args = parse_args()
    log_level = logging.DEBUG if args.debug else logging.WARNING
    set_log_level(log_level)

    draft_s = f"d{args.draft}"
    print(f"\nRed5 MoQT Conformance Tests")
    print(f"  target: {args.host}:{args.port} ({draft_s} QUIC)")
    print(f"  namespace: {args.namespace}")
    print(f"  track: {args.trackname}")
    print(f"  {'─' * 60}")

    results = []
    results.append(await test_setup(
        args.host, args.port, args.draft))
    results.append(await test_publish_namespace(
        args.host, args.port, args.draft, args.namespace))
    results.append(await test_publish_response(
        args.host, args.port, args.draft,
        args.namespace, args.trackname))
    results.append(await test_subscribe_namespace_response(
        args.host, args.port, args.draft, args.namespace))
    results.append(await test_subscribe_namespace_publish_forwarding(
        args.host, args.port, args.draft,
        args.namespace, args.trackname))
    results.append(await test_subscribe_response(
        args.host, args.port, args.draft,
        args.namespace, args.trackname))
    results.append(await test_subscribe_data_delivery(
        args.host, args.port, args.draft,
        args.namespace, args.trackname))
    results.append(await test_track_alias_established(
        args.host, args.port, args.draft,
        args.namespace, args.trackname))

    print(f"\n  {'─' * 60}")
    passed = sum(1 for r in results if r.passed is True)
    failed = sum(1 for r in results if r.passed is False)
    skipped = sum(1 for r in results if r.passed is None)
    print(f"  Results: {passed} passed, {failed} failed, "
          f"{skipped} skipped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Interrupted.")
