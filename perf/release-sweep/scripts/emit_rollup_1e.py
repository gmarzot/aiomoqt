#!/usr/bin/env python3
"""Regenerate the Phase 1e block of rollup.csv from result.json files.

The previously committed 1e rows are field-shifted (status carried 1.0, mbps
carried 3.3), so this rewrites them in place rather than appending. Non-1e rows
are preserved byte-for-byte by column.
"""
import csv, glob, json, os, sys

BLOCK = "runs/2026-11-11T02-33-51Z"
RUN = "2026-11-11T02-33-51Z"
CSV = "baselines/base-070rc2/rollup.csv"
# Every point this phase owns. Filtering on the point id rather than on the run
# id is what makes the rewrite idempotent: an earlier generator emitted these
# five points with an empty ``run`` column, so a ``run != RUN`` filter kept them
# and every regeneration doubled the block.
POINTS_1E = {"M1", "M2", "C0", "C1", "C2"}

def g(summary, *keys, default=""):
    for k in keys:
        v = summary.get(k)
        if v not in (None, "", "null"):
            return v
    return default

def started_utc(d):
    import datetime
    p = f"{d}/t0_reference"
    if os.path.exists(p):
        try:
            ts = float(open(p).read().strip())
            return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
    if os.path.exists(f"{d}/result.json"):
        r = json.load(open(f"{d}/result.json"))
        if r.get("started_utc"):
            return r["started_utc"]
        if r.get("started_epoch"):
            import datetime
            return datetime.datetime.utcfromtimestamp(r["started_epoch"]).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ""

def main():
    header = next(csv.reader(open(CSV, newline="")))
    with open(CSV, newline="") as f:
        old = [r for r in csv.DictReader(f) if r["point_id"] not in POINTS_1E]
    print(f"kept {len(old)} non-1e rows")

    new = []
    for path in sorted(glob.glob(f"{BLOCK}/*/rep-*/result*.json")):
        d = os.path.dirname(path)
        if path.endswith("result.built.json") and os.path.exists(f"{d}/result.json"):
            try:
                if (json.load(open(f"{d}/result.json")).get("summary") or {}).get("mbps"):
                    continue
            except Exception:
                pass
        point, rep = d.split("/")[-2], d.split("/")[-1].replace("rep-", "")
        # The rest of rollup.csv numbers reps 1,2,3; rep-NN dirs would otherwise
        # introduce a second convention in the same column.
        rep = str(int(rep)) if rep.isdigit() else rep
        r = json.load(open(path))
        s = r.get("summary") or {}
        cfg = r.get("config") or {}
        row = {k: "" for k in header}
        # C2 reps carry the whole probe record under summary.ttff; only scalars
        # belong in a CSV cell, and a subscriber that got code=4 is a failed rep.
        probe = s.get("ttff") if isinstance(s.get("ttff"), dict) else {}
        status = r.get("status") or g(s, "result", default="ok")
        if probe.get("error"):
            status = "failed"
        ttff = g(s, "ttff_ms")
        if not ttff and probe.get("ttff_ms") is not None:
            ttff = probe["ttff_ms"]
        row.update(
            run=RUN, point_id=point, arm=r.get("arm", "base-070rc2"),
            topology=r.get("topology", ""), transport=r.get("transport", "raw"),
            rep=rep, status=status,
            started_utc=started_utc(d), duration_s=g(r, "duration_s"),
            mbps=g(s, "mbps", "mbps_mean_intervals", "bitrate_mbps"),
            obj_s=g(s, "obj_s"), grp_s=g(s, "grp_s"),
            p50=g(s, "p50"), p95=g(s, "p95"), p99=g(s, "p99"),
            jitter_ms=g(s, "jitter_ms"), lost=g(s, "lost", "lost_pct"),
            ooo=g(s, "ooo"), interval_cv=g(s, "interval_cv"),
            cpu_pub=g(s, "cpu_pub"), cpu_sub=g(s, "cpu_sub"),
            rss_max_mb=g(s, "rss_max_mb"), clock_offset_us=g(s, "clock_offset_us"),
            ttff_ms=g(s, "ttff_ms"), burst_bytes_1s=g(s, "burst_bytes_1s"),
            burst_groups_1s=g(s, "burst_groups_1s"), first_group=g(s, "first_group"),
            peak_subs=g(s, "peak_subs"), target_subs=g(s, "target_subs", default=cfg.get("slots", "")),
            joins=g(s, "joins"), churns=g(s, "churns"), exodus=g(s, "exodus"),
            fails=g(s, "fails"), died=g(s, "died"),
            delivery_frac=g(s, "delivery_frac"), peak_frac=g(s, "peak_frac"),
        )
        new.append(row)

    best = {}
    for r in new:
        k = (r["point_id"], r["rep"])
        if k not in best or (r["mbps"] and not best[k]["mbps"]):
            best[k] = r
    new = sorted(best.values(),
                 key=lambda r: (r["point_id"], int(r["rep"]) if r["rep"].isdigit() else 0))
    with open(CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, lineterminator="\r\n")
        w.writeheader()
        for r in old + new:
            w.writerow({k: r.get(k, "") for k in header})
    print(f"wrote {len(old) + len(new)} rows ({len(new)} from block {RUN})")
    for r in new:
        print(" ", r["point_id"], r["rep"], "mbps=", r["mbps"], "peak=", r["peak_subs"],
              "ttff=", r["ttff_ms"], "status=", r["status"])

if __name__ == "__main__":
    main()
