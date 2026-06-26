#!/usr/bin/env python3
"""
MOQ relay probe — determines relay liveness and supported draft versions.

Each endpoint URL gets exactly 2 probes:
  - moqt:// URLs: raw QUIC with moqt-16 ALPN, then moq-00 ALPN
  - https:// URLs: H3/WebTransport with moqt-16, then moq-00

Each probe does a proper CLIENT_SETUP/SERVER_SETUP handshake and clean close.
No bare ALPN probes, no custom protocol classes, no half-open connections.

Outputs relay-status.json consumed by the landing page.
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from aiomoqt.client import MOQTClient
from aiomoqt.types import (
    MOQTRequestError,
    moqt_version_from_draft,
    parse_draft_spec,
)
from aiomoqt.utils.url import parse_relay_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("relay-probe")

# Runtime config. Values read from env at import time so existing
# container deployments keep working; the CLI parser in __main__
# rebinds these from argparse defaults when the module is invoked
# directly, giving CLI-over-env precedence.
PROBE_TIMEOUT = int(os.getenv("PROBE_TIMEOUT", "8"))
# PROBE_INTERVAL controls looping: 0 = probe once and exit, >0 = loop every
# N seconds.
PROBE_INTERVAL = int(os.getenv("PROBE_INTERVAL", "0"))
RELAYS_FILE = os.getenv("RELAYS_FILE", "/app/relays.json")
OUTPUT_FILE = os.getenv("OUTPUT_FILE", "/output/relay-status.json")

# Draft versions to probe, oldest first — d14 bare WT CONNECT must
# run before d16 (which sends wt-available-protocols) to avoid
# relay state issues on sequential probes to the same endpoint.
DRAFT_PROBES = [
    # Draft numbers (the public MoQTClient supported_drafts form).
    ("draft-14", 14),
    ("draft-16", 16),
    ("draft-18", 18),
]

# Set from --draft to probe a single draft instead of the full DRAFT_PROBES
# list (None = probe all of DRAFT_PROBES).
DRAFT_FILTER = None


async def probe_version(host, port, path, use_quic, supported_drafts,
                        verify_tls=False):
    """Single probe: connect, CLIENT_SETUP/SERVER_SETUP, return result.

    Returns dict with live, draft, version_hex, alpn, params, error.
    """
    result = {
        "live": False,
        "draft": None,
        "version_hex": None,
        "alpn": None,
        "params": {},
        "error": None,
    }
    try:
        client = MOQTClient(
            host, port,
            path=path,
            use_quic=use_quic,
            verify_tls=verify_tls,
            supported_drafts=supported_drafts,
        )
        async with asyncio.timeout(PROBE_TIMEOUT):
            async with client.connect() as session:
                await session.client_session_init(timeout=PROBE_TIMEOUT - 1)
                draft = session.negotiated_draft
                result["live"] = True
                # version_hex is a display-only wire form: translate the
                # negotiated draft to its IETF version code here.
                result["version_hex"] = f"0x{moqt_version_from_draft(draft):08x}"
                result["draft"] = f"draft-{draft}"
                result["alpn"] = client.configuration.alpn_protocols[0]
                # Capture setup params from server
                # Clean close
                session.close()
                await asyncio.sleep(0.1)
    except asyncio.TimeoutError:
        result["error"] = "timeout"
    except MOQTRequestError as e:
        result["error"] = f"request_error: {e.error_code} {e.reason}"
    except (OSError, ConnectionError) as e:
        result["error"] = f"connect: {e}"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


async def probe_endpoint(url, verify_tls=False):
    """Probe one URL for all supported drafts.

    Tries each draft version sequentially (newest first).
    Returns endpoint status with list of supported drafts.
    """
    relay = parse_relay_url(url)
    host, port, path, use_quic = relay.host, relay.port, relay.path or "", relay.use_quic
    transport = "QUIC" if use_quic else "H3/WT"

    t0 = time.monotonic()
    drafts = []
    results = []

    for draft_name, supported_drafts in (DRAFT_FILTER or DRAFT_PROBES):
        r = await probe_version(
            host, port, path, use_quic, supported_drafts, verify_tls,
        )
        results.append(r)
        if r["live"]:
            # For a multi-draft offer the label is the offered set; report
            # which draft the relay actually negotiated.
            negotiated = r["draft"] or draft_name
            drafts.append(negotiated)
            arrow = "" if negotiated == draft_name else f" -> {negotiated}"
            logger.info(f"  {url} [{transport}] {draft_name}{arrow}: LIVE"
                        f" (alpn={r['alpn']})")
        else:
            logger.info(f"  {url} [{transport}] {draft_name}: {r['error']}")

    latency_ms = round((time.monotonic() - t0) * 1000)
    any_live = any(r["live"] for r in results)

    return {
        "url": url,
        "transport": transport,
        "host": host,
        "port": port,
        "live": any_live,
        "drafts": drafts,
        "draft": drafts[0] if drafts else None,
        "latency_ms": latency_ms,
        "probes": results,
        "error": results[-1].get("error") if not any_live else None,
    }


async def probe_relay(relay):
    """Probe all endpoints for one relay."""
    endpoints = relay.get("endpoints", [])
    if not endpoints and "url" in relay:
        endpoints = [{"url": relay["url"]}]

    ep_results = []
    for ep in endpoints:
        url = ep if isinstance(ep, str) else ep.get("url", "")
        if not url:
            continue
        r = await probe_endpoint(url, verify_tls=ep.get("verify_tls", False)
                                 if isinstance(ep, dict) else False)
        ep_results.append(r)

    any_live = any(r["live"] for r in ep_results)
    all_drafts = set()
    for r in ep_results:
        all_drafts.update(r.get("drafts", []))

    return {
        "name": relay.get("name", relay.get("id", "unknown")),
        "live": any_live,
        "drafts": sorted(all_drafts),
        "endpoints": ep_results,
    }


async def probe_all(relays, last_probed, cached_results):
    """Probe relays in parallel, honoring per-relay interval."""
    now = time.monotonic()
    to_probe = []
    skipped = []
    for relay in relays:
        rid = relay.get("id", relay.get("name", "unknown"))
        interval = relay.get("interval", PROBE_INTERVAL)
        elapsed = now - last_probed.get(rid, 0)
        if elapsed >= interval:
            to_probe.append(relay)
        else:
            remaining = int(interval - elapsed)
            logger.debug(f"Skipping {rid}: next probe in {remaining}s")
            skipped.append(relay)

    if to_probe:
        logger.info(f"Probing {len(to_probe)} relays "
                     f"({len(skipped)} skipped)...")
        tasks = [probe_relay(relay) for relay in to_probe]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for relay, result in zip(to_probe, results):
            rid = relay.get("id", relay.get("name", "unknown"))
            last_probed[rid] = time.monotonic()
            if isinstance(result, Exception):
                cached_results[rid] = {
                    "name": rid, "live": False, "error": str(result),
                }
            else:
                cached_results[rid] = result
    else:
        logger.debug("All relays within their probe interval, skipping")

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "relays": {},
    }
    for relay in relays:
        rid = relay.get("id", relay.get("name", "unknown"))
        if rid in cached_results:
            report["relays"][rid] = cached_results[rid]

    up = sum(1 for r in report["relays"].values() if r.get("live"))
    logger.info(f"Done: {up}/{len(relays)} live")
    return report


async def run_loop():
    """Main loop — probe on interval, write JSON."""
    relays_path = Path(RELAYS_FILE)
    if relays_path.exists():
        relays = json.loads(relays_path.read_text())
    else:
        logger.warning(f"No relays file at {RELAYS_FILE}, using defaults")
        relays = [
            {"id": "openmoq", "name": "OpenMoQ", "endpoints": [
                {"url": "moqt://moqx-000.ci.openmoq.org:4433"},
                {"url": "https://moqx-000.ci.openmoq.org:4433/moq-relay"},
            ]},
            {"id": "moxygen", "name": "Moxygen/Meta", "interval": 120,
             "endpoints": [
                {"url": "moqt://fb.mvfst.net:9448"},
            ]},
            {"id": "cloudflare", "name": "Cloudflare", "endpoints": [
                {"url": "moqt://draft-14.cloudflare.mediaoverquic.com:443"},
            ]},
        ]

    last_probed = {}  # relay_id -> monotonic time of last probe
    cached_results = {}  # relay_id -> last probe result

    while True:
        report = await probe_all(relays, last_probed, cached_results)

        out = Path(OUTPUT_FILE)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, indent=2))
        tmp.rename(out)
        logger.info(f"Wrote {out}")

        if PROBE_INTERVAL <= 0:
            break  # interval 0 = probe once and exit

        logger.info(f"Next probe in {PROBE_INTERVAL}s")
        await asyncio.sleep(PROBE_INTERVAL)


def _parse_args():
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m aiomoqt.examples.relay_probe",
        description="MOQ relay probe — liveness and draft-version check.",
        epilog=(
            "Each CLI flag defaults from its matching environment "
            "variable (RELAYS_FILE, OUTPUT_FILE, PROBE_TIMEOUT, "
            "PROBE_INTERVAL), so container deployments "
            "that set env vars keep working unchanged. CLI flags "
            "override env. A per-relay 'interval' field in the "
            "relays file overrides --interval for that relay. "
            "If the relays file is missing, a small built-in list "
            "is used so the process always has something to probe."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-f", "--relays-file", default=RELAYS_FILE, metavar="PATH",
        help=f"input relay list JSON (env: RELAYS_FILE; "
             f"default: {RELAYS_FILE!r})")
    p.add_argument(
        "-o", "--output-file", default=OUTPUT_FILE, metavar="PATH",
        help=f"status JSON output path (env: OUTPUT_FILE; "
             f"default: {OUTPUT_FILE!r})")
    p.add_argument(
        "--timeout", type=int, default=PROBE_TIMEOUT, metavar="SEC",
        help=f"per-probe handshake timeout (env: PROBE_TIMEOUT; "
             f"default: {PROBE_TIMEOUT})")
    p.add_argument(
        "--interval", type=int, default=PROBE_INTERVAL, metavar="SEC",
        help=f"loop period: 0 = probe once and exit, >0 = loop every SEC "
             f"(env: PROBE_INTERVAL; default: {PROBE_INTERVAL}).")
    p.add_argument(
        "--draft", default=None, metavar="SPEC",
        help="draft(s) to probe: a single draft (--draft 18) or a list "
             "(--draft 18,16). By default each listed draft is probed "
             "INDIVIDUALLY, in order; with --offer the whole list is offered "
             "in ONE session. Default (no --draft) probes 14, 16, 18 each.")
    p.add_argument(
        "--offer", action="store_true", default=False,
        help="with a multi-draft --draft, offer the whole list in ONE "
             "session (ordered as given) and report the draft the relay "
             "negotiates, instead of probing each draft separately.")
    p.add_argument(
        "--url", default=None, metavar="URL",
        help="probe a single relay URL (e.g. moqt://host:port or "
             "https://host:port/path) and print one line per probed "
             "draft to stdout; bypasses --relays-file / --output-file. "
             "Intended for quick interactive liveness checks.")
    p.add_argument(
        "-d", "--debug", action="store_true", default=False,
        help="verbose handshake logging (DEBUG level on aiomoqt + "
             "relay-probe loggers). Off by default in --url mode so "
             "stdout stays clean for scripting.")
    return p.parse_args()


def _classify_error(err: str, transport: str = "") -> str:
    """Map a raw probe error to a short 'status - reason' conclusion for the
    quiet one-line output. The raw error is shown under --debug."""
    e = (err or "").lower()
    is_wt = "wt" in transport.lower() or "h3" in transport.lower()
    if "error_code=376" in e:  # TLS no_application_protocol (ALPN mismatch)
        # Over WT the offered ALPN is "h3"; 376 there means the relay isn't
        # an H3/WebTransport server. Over raw QUIC it's the moqt-NN ALPN, so
        # 376 means no shared draft.
        if is_wt:
            return "connection refused - h3/webtransport not supported"
        return "connection refused - no compatible draft/version"
    if "wt connect refused" in e:
        return "connection refused - draft not supported or wrong path"
    if "timeout" in e:
        return "no response - handshake timed out"
    if ("name or service not known" in e or "gaierror" in e
            or "getaddr" in e or "name resolution" in e):
        return "DNS lookup failed - host did not resolve"
    if "refused" in e:
        return "connection refused - nothing listening"
    if "during handshake" in e:
        return "connection refused - handshake failed"
    return err or "unreachable"


async def _probe_single_url(url, timeout, debug=False):
    """One-shot probe: print one human-readable line per (draft, transport)
    combo. No JSON, no file I/O. Exits with code 0 if any draft was LIVE,
    else 1 — easy to wire into shell scripts."""
    from aiomoqt.utils.logger import set_log_level
    # Default quiet: only the one-line result reaches stdout. Suppress the
    # aiomoqt + aiopquic connection logs (including handshake-failure
    # WARNING/ERROR) — a failed probe is still reported on the printed ✗ line
    # with its error code. --debug shows the full handshake.
    if debug:
        set_log_level(logging.DEBUG)
        logging.getLogger("relay-probe").setLevel(logging.DEBUG)
    else:
        set_log_level(logging.CRITICAL)
        logging.getLogger("relay-probe").setLevel(logging.WARNING)
        logging.getLogger("aiopquic").setLevel(logging.CRITICAL)
    global PROBE_TIMEOUT
    PROBE_TIMEOUT = timeout
    result = await probe_endpoint(url)
    transport = result["transport"]
    if result["live"]:
        drafts = ",".join(result["drafts"])
        print(f"{url}  {transport.lower():<8}"
              f"  {drafts}  ✓ ({result['latency_ms']}ms)")
        return 0
    err = result.get("error") or "unreachable"
    conclusion = _classify_error(err, transport)
    suffix = f"  ({err})" if (debug and conclusion != err) else ""
    print(f"{url}  {transport.lower():<8}  ✗ {conclusion}{suffix}")
    return 1


if __name__ == "__main__":
    args = _parse_args()
    if args.draft is not None:
        try:
            spec = parse_draft_spec(args.draft)
        except ValueError as e:
            print(f"relay_probe: bad --draft value: {e}", file=sys.stderr)
            sys.exit(2)
        if args.offer:
            # Offer the whole set in one session (ordered); report negotiated.
            DRAFT_FILTER = [(f"draft-{args.draft}", spec)]
        else:
            # Probe each listed draft individually, in the given order.
            drafts = spec if isinstance(spec, list) else [spec]
            DRAFT_FILTER = [(f"draft-{d}", d) for d in drafts]
    if args.url:
        # Single-URL mode: skip the relays-file / output-file machinery.
        rc = asyncio.run(_probe_single_url(
            args.url, args.timeout, debug=args.debug))
        raise SystemExit(rc)
    # CLI overrides env overrides hard default. Rebinding module-level
    # constants keeps existing references in probe_version / probe_all
    # / run_loop working with zero signature churn.
    RELAYS_FILE = args.relays_file
    OUTPUT_FILE = args.output_file
    PROBE_TIMEOUT = args.timeout
    PROBE_INTERVAL = args.interval
    asyncio.run(run_loop())
