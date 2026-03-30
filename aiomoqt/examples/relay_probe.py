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
import time
from datetime import datetime, timezone
from pathlib import Path

from aiomoqt.client import MOQTClient
from aiomoqt.types import (
    MOQT_VERSION_DRAFT14, MOQT_VERSION_DRAFT16, MOQTRequestError,
)
from aiomoqt.context import get_major_version
from aiomoqt.utils.url import parse_relay_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("relay-probe")

PROBE_TIMEOUT = int(os.getenv("PROBE_TIMEOUT", "8"))
PROBE_INTERVAL = int(os.getenv("PROBE_INTERVAL", "300"))
RELAYS_FILE = os.getenv("RELAYS_FILE", "/app/relays.json")
OUTPUT_FILE = os.getenv("OUTPUT_FILE", "/output/relay-status.json")

# Draft versions to probe, newest first
DRAFT_PROBES = [
    ("draft-16", MOQT_VERSION_DRAFT16),
    ("draft-14", MOQT_VERSION_DRAFT14),
]


async def probe_version(host, port, path, use_quic, draft_version,
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
            endpoint=path,
            use_quic=use_quic,
            verify_tls=verify_tls,
            draft_version=draft_version,
        )
        async with asyncio.timeout(PROBE_TIMEOUT):
            async with client.connect() as session:
                await session.client_session_init(timeout=PROBE_TIMEOUT - 1)
                v = session._moqt_version
                result["live"] = True
                result["version_hex"] = f"0x{v:08x}"
                result["draft"] = f"draft-{get_major_version(v)}"
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
    host, port, path, use_quic = relay.host, relay.port, relay.endpoint or "", relay.use_quic
    transport = "QUIC" if use_quic else "H3/WT"

    t0 = time.monotonic()
    drafts = []
    results = []

    for draft_name, draft_version in DRAFT_PROBES:
        r = await probe_version(
            host, port, path, use_quic, draft_version, verify_tls,
        )
        results.append(r)
        if r["live"]:
            drafts.append(draft_name)
            logger.info(f"  {url} [{transport}] {draft_name}: LIVE"
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

    once = os.getenv("PROBE_ONCE", "").lower() in ("1", "true", "yes")
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

        if once:
            break

        logger.info(f"Next probe in {PROBE_INTERVAL}s")
        await asyncio.sleep(PROBE_INTERVAL)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        prog="python -m aiomoqt.examples.relay_probe",
        description="MOQ relay probe — probes relays for liveness and draft version support.",
        epilog="""environment variables:
  RELAYS_FILE      path to relay list JSON (default: /app/relays.json)
  OUTPUT_FILE      status JSON output path (default: /output/relay-status.json)
  PROBE_TIMEOUT    per-probe timeout in seconds (default: 8)
  PROBE_INTERVAL   seconds between probe cycles (default: 300)
  PROBE_ONCE       set "1" for single run then exit (default: loop)

Reads RELAYS_FILE for relay definitions. If not found, uses built-in
defaults (OpenMoQ, Meta moxygen, Cloudflare). Each relay endpoint is
probed for draft-16 and draft-14 support. Results are written as JSON
to OUTPUT_FILE.

Relays may specify a per-relay "interval" (seconds) in relays.json
to override PROBE_INTERVAL for that relay. Cached results are used
between probes.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.parse_args()
    asyncio.run(run_loop())
