#!/usr/bin/env python3
"""Extract C2 late-join TTFF + catch-up bytes from ttff_probe TTFF-JSON lines."""
import glob, json, os, sys

BLOCK = "runs/2026-11-11T02-33-51Z"

def pull(path):
    if not os.path.exists(path):
        return None
    for line in open(path, errors="replace"):
        if "TTFF-JSON:" in line:
            try:
                return json.loads(line.split("TTFF-JSON:", 1)[1].strip())
            except Exception as e:
                return {"parse_error": str(e)}
    return None

def brief(d, label):
    if not d:
        return f"{label}: MISSING"
    ttff = d.get("ttff_ms")
    sub = d.get("subscribe_sent_unix")
    fo = d.get("first_object_unix")
    derived = round((fo - sub) * 1000, 1) if sub and fo else None
    return (f"{label}: ttff_ms={ttff} (derived={derived}) bytes={d.get('bytes')} "
            f"objects={d.get('objects')} first_group={d.get('first_group')} "
            f"first_object_bytes={d.get('first_object_bytes')} "
            f"burst_bytes_1s={d.get('burst_bytes_1s')} "
            f"burst_groups_1s={d.get('burst_groups_1s')} "
            f"stream_s={d.get('stream_seconds')} err={d.get('error')} "
            f"n_groups={len(d.get('groups') or [])}")

for rep in sorted(glob.glob(f"{BLOCK}/C2/rep-*")):
    print(f"--- {os.path.basename(rep)} ---")
    print(" ", brief(pull(f"{rep}/sub1.log"), "late(+30s)"))
    print(" ", brief(pull(f"{rep}/sub-warm.log"), "warm(+2s)"))
