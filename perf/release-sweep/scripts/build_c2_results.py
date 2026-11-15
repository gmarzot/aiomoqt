#!/usr/bin/env python3
"""Build result.built.json for C2 reps from the ttff_probe logs.

C2's on-disk result.json is the stale 2026-11-11 sweep record (publisher never
announced the namespace, subscriber got code=4); the logs in the same dir are the
2026-11-15 successful runs. Derive the summary from the logs.
"""
import glob, json, os

BLOCK = "runs/2026-11-11T02-33-51Z"

def pull(path):
    if not os.path.exists(path):
        return None
    for line in open(path, errors="replace"):
        if "TTFF-JSON:" in line:
            try:
                return json.loads(line.split("TTFF-JSON:", 1)[1].strip())
            except Exception:
                return None
    return None

def flat(rec):
    if not rec:
        return {}
    sub, fo = rec.get("subscribe_sent_unix"), rec.get("first_object_unix")
    ttff = rec.get("ttff_ms")
    if ttff is None and sub and fo:
        ttff = round((fo - sub) * 1000, 1)
    return dict(
        ttff_ms=ttff,
        subscribe_sent_unix=sub, first_object_unix=fo,
        first_object_bytes=rec.get("first_object_bytes"),
        first_group=rec.get("first_group"), first_object_id=rec.get("first_object_id"),
        burst_bytes_1s=rec.get("burst_bytes_1s"),
        burst_groups_1s=rec.get("burst_groups_1s"),
        bytes=rec.get("bytes"), objects=rec.get("objects"),
        stream_seconds=rec.get("stream_seconds"), error=rec.get("error"),
    )

for d in sorted(glob.glob(f"{BLOCK}/C2/rep-*")):
    late = pull(f"{d}/sub1.log")
    warm = pull(f"{d}/sub-warm.log")
    lf, wf = flat(late), flat(warm)
    if not lf and not wf:
        print(f"{os.path.basename(d)}: no probe records, skipped")
        continue
    ok = bool(lf.get("ttff_ms") is not None)
    started = ""
    if os.path.exists(f"{d}/t0_reference"):
        import datetime
        try:
            ts = float(open(f"{d}/t0_reference").read().strip())
            started = datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
    summary = dict(lf)
    summary["ttff_warm_ms"] = wf.get("ttff_ms")
    summary["ttff_basis"] = "SUBSCRIBE sent -> first object arrival, both stamped on the subscriber host"
    out = dict(point_id="C2", arm="base-070rc2", topology="C", transport="raw", draft=16,
               duration_s=lf.get("stream_seconds"),
               config=dict(late_join_at_s=30, warm_join_at_s=2, video="1080p",
                           publisher_flags="--video 1080p --pub-both"),
               summary=summary, status="ok" if ok else "failed", started_utc=started,
               hosts=dict(publisher="shenzhen", relay="shenzhen", subscriber="seawolf"),
               provenance="built from sub1.log/sub-warm.log TTFF-JSON 2026-11-15; "
                          "C2 result.json in this dir is the stale 2026-11-11 failure")
    json.dump(out, open(f"{d}/result.built.json", "w"), indent=1)
    print(f"{os.path.basename(d)}: late ttff={lf.get('ttff_ms')} ms warm={wf.get('ttff_ms')} "
          f"bytes={lf.get('bytes')} objects={lf.get('objects')} first_group={lf.get('first_group')} "
          f"burst1s={lf.get('burst_bytes_1s')} status={out['status']}")
