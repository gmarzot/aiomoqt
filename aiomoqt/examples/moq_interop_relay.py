#!/usr/bin/env python3
"""EXPERIMENTAL — minimal MoQT interop relay (not a production relay).

This is a CONTROL-PLANE-ONLY skeleton intended solely for exercising
the 6 standard interop test cases at
https://github.com/englishm/moq-interop-runner. It has:

  * NO data forwarding (objects are never relayed)
  * NO multi-subscriber fan-out
  * NO authentication, authorization, or rate limiting
  * NO load handling, backpressure, or production hardening
  * An in-memory namespace table that is global to the process

Use moxygen, moq-rs, or another real relay for any actual workload.
This relay exists so aiomoqt's server-side primitives (MOQTServer,
the announce / subscribe handlers) have a working demonstrator and
so we can run the interop conformance suite against ourselves.

Routing model (cross-session, single relay instance):
  - PUBLISH_NAMESPACE: record the namespace tuple in `_announced`,
    respond with the protocol's default RequestOk (d16+) /
    PublishNamespaceOk (d14).
  - SUBSCRIBE: if the requested (namespace, track_name) namespace is in
    `_announced`, respond with SUBSCRIBE_OK. Otherwise send
    SUBSCRIBE_ERROR with TRACK_DOES_NOT_EXIST (d14 code 0x04,
    d16 code 0x10) so the conformance suite's subscribe-error test sees
    a spec-correct rejection.
  - PUBLISH_NAMESPACE_DONE: remove the namespace from `_announced`.

Run on UDP/4443 with the runner's /certs convention:

  python -m aiomoqt.examples.moq_interop_relay \\
      --bind 0.0.0.0 --port 4443 \\
      --cert /certs/cert.pem --key /certs/priv.key

Accepts both raw QUIC and WebTransport on the same port via two
parallel listeners (different UDP-port binds with separate ALPNs would
require a second port; for the runner we serve WT only by default and
add `--quic` to swap to raw QUIC).
"""

import argparse
import asyncio
import logging
import os
import sys

from aiomoqt.server import MOQTServer
from aiomoqt.types import (
    MOQTMessageType, RequestErrorCode, SubscribeErrorCode,
)
from aiomoqt.messages.request import RequestError
from aiomoqt.context import is_draft16_or_later
from aiomoqt.utils.logger import set_log_level, get_logger

logger = get_logger(__name__)


# Global cross-session announcement table. Maps namespace tuple (as a
# tuple of bytes) to a count of active publishers. Sub-tests within the
# same suite can re-announce / un-announce; the count lets that work.
_announced: dict[tuple, int] = {}


def _ns_tuple(namespace):
    """Normalize a namespace value (str / list / tuple of bytes-or-str)
    into a tuple of bytes for use as a dict key."""
    if isinstance(namespace, str):
        return tuple(s.encode() for s in namespace.split("/") if s)
    if isinstance(namespace, (list, tuple)):
        return tuple(
            s.encode() if isinstance(s, str) else bytes(s)
            for s in namespace
        )
    return tuple()


async def _on_publish_namespace(session, msg):
    """Record the namespace, ack with default OK path."""
    ns = _ns_tuple(msg.namespace)
    _announced[ns] = _announced.get(ns, 0) + 1
    logger.info(f"relay: announce ns={ns} -> count={_announced[ns]}")
    # Reuse the protocol's built-in OK helper. It emits RequestOk on
    # d16+ and PublishNamespaceOk on d14, matching peer expectation.
    session.publish_namepace_ok(msg)


async def _on_publish_namespace_done(session, msg):
    """Drop one publisher's hold on the namespace."""
    ns = _ns_tuple(msg.namespace) if msg.namespace else None
    if ns is None or ns not in _announced:
        logger.info(f"relay: publish_namespace_done for unknown ns={ns}")
        return
    _announced[ns] -= 1
    if _announced[ns] <= 0:
        del _announced[ns]
    logger.info(f"relay: namespace_done ns={ns} -> "
                f"{_announced.get(ns, 0)} remaining")


async def _on_subscribe(session, msg):
    """Accept SUBSCRIBE for announced namespaces, error otherwise."""
    ns = _ns_tuple(msg.track_namespace)
    if ns in _announced:
        logger.info(f"relay: subscribe ns={ns} track={msg.track_name} "
                    f"-> SUBSCRIBE_OK")
        session.subscribe_ok(request_msg=msg)
        return
    logger.info(f"relay: subscribe ns={ns} track={msg.track_name} "
                f"-> ERROR (not announced)")
    # On d16+ the universal REQUEST_ERROR (0x05) carries the not-found
    # code (0x10). On d14 the legacy SUBSCRIBE_ERROR (0x05) carries
    # TRACK_DOES_NOT_EXIST (0x04). Send the right shape per version.
    if is_draft16_or_later():
        err = RequestError(
            request_id=msg.request_id,
            error_code=int(RequestErrorCode.DOES_NOT_EXIST),
            retry_interval=0,
            reason="track does not exist",
        )
        logger.info(f"MOQT send: {err}")
        session.send_control_message(err.serialize())
    else:
        session.subscribe_error(
            request_id=msg.request_id,
            error_code=int(SubscribeErrorCode.TRACK_DOES_NOT_EXIST),
            reason="track does not exist",
        )


def _find_default_cert():
    candidates = [
        '/certs/cert.pem',
        os.path.join(os.path.dirname(__file__),
                     '..', '..', 'certs', 'cert.pem'),
        os.path.expanduser('~/.local/share/moqt/cert.pem'),
    ]
    for c in candidates:
        if os.path.exists(c):
            return os.path.realpath(c)
    return None


def _find_default_key(cert_path):
    if not cert_path:
        return None
    for name in ('priv.key', 'key.pem'):
        candidate = os.path.join(os.path.dirname(cert_path), name)
        if os.path.exists(candidate):
            return candidate
    return None


def parse_args():
    parser = argparse.ArgumentParser(
        description="MoQT interop-runner relay (control-plane only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--bind", type=str, default="0.0.0.0",
                        help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=4443,
                        help="UDP listen port (default: 4443)")
    parser.add_argument("--cert", type=str, default=None,
                        help="TLS cert PEM "
                             "(default: /certs/cert.pem)")
    parser.add_argument("--key", type=str, default=None,
                        help="TLS key PEM "
                             "(default: /certs/priv.key)")
    parser.add_argument("--quic", action="store_true",
                        help="Serve raw QUIC instead of WebTransport")
    parser.add_argument("--draft", type=int, default=16,
                        help="MoQT draft version (default: 16)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    return parser.parse_args()


async def main():
    args = parse_args()
    log_level = logging.DEBUG if args.debug else logging.INFO
    set_log_level(log_level)
    logging.basicConfig(
        level=log_level, stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cert = args.cert or _find_default_cert()
    key = args.key or _find_default_key(cert)
    if not cert or not key:
        print("Error: TLS cert/key required. Use --cert/--key or place "
              "cert.pem+priv.key at /certs/", file=sys.stderr)
        sys.exit(2)

    server = MOQTServer(
        host=args.bind, port=args.port,
        certificate=cert, private_key=key,
        path="/",
        use_quic=args.quic,
        draft_version=args.draft,
    )
    server.register_handler(
        MOQTMessageType.PUBLISH_NAMESPACE, _on_publish_namespace)
    server.register_handler(
        MOQTMessageType.PUBLISH_NAMESPACE_DONE, _on_publish_namespace_done)
    server.register_handler(
        MOQTMessageType.SUBSCRIBE, _on_subscribe)

    quic_server = await server.serve()

    transport = "raw QUIC" if args.quic else "H3/WebTransport"
    print(
        "=" * 64
        + "\n EXPERIMENTAL aiomoqt interop relay — control-plane only.\n"
        " NOT a production relay: no data forwarding, no auth,\n"
        " no scale handling. Use moxygen / moq-rs for real workloads.\n"
        + "=" * 64,
        file=sys.stderr,
    )
    print(f"Listening on {args.bind}:{args.port} "
          f"({transport}, draft-{args.draft})", file=sys.stderr)

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        quic_server.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
