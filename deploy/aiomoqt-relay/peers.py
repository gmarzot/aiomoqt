#!/usr/bin/env python3
"""aiomoqt-relay peer log, for /private/peers/.

Every INTERVAL_S, from the relay's status.json: each live session's IP, role,
transport and draft, and each namespace and track an IP publishes or
subscribes to, into a SQLite database. Every REPORT_S, a JSON summary.
Detail older than RETAIN_DAYS is dropped; daily totals are kept.
Sessions shorter than INTERVAL_S can be missed.
"""
import json
import os
import sqlite3
import sys
import time

STATUS = os.environ.get("PEERS_STATUS", "/status/status.json")
DB = os.environ.get("PEERS_DB", "/peers/peers.db")
REPORT = os.environ.get("PEERS_REPORT", "/report/peers.json")
INTERVAL_S = 5
REPORT_S = 60
RETAIN_DAYS = 90
# Namespaces listed per IP and role, and IPs per namespace.
SAMPLE = 20
# IPs in the report, most recently seen first.
MAX_IPS = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  relay REAL, sid INTEGER, ip TEXT, role TEXT, transport TEXT, draft INTEGER,
  since REAL, last REAL, PRIMARY KEY (relay, sid));
CREATE TABLE IF NOT EXISTS seen (
  ip TEXT, role TEXT, namespace TEXT, track TEXT, first REAL, last REAL,
  PRIMARY KEY (ip, role, namespace, track));
CREATE TABLE IF NOT EXISTS daily (
  day TEXT, ip TEXT, role TEXT, namespace TEXT,
  PRIMARY KEY (day, ip, role, namespace));
CREATE TABLE IF NOT EXISTS days (
  day TEXT PRIMARY KEY, ips INTEGER, pub_ips INTEGER, sub_ips INTEGER,
  sessions INTEGER, namespaces INTEGER);
CREATE INDEX IF NOT EXISTS sessions_last ON sessions (last);
CREATE INDEX IF NOT EXISTS seen_last ON seen (last);
"""


def day_of(t):
    return time.strftime("%Y-%m-%d", time.gmtime(t))


def record(db, st, now):
    """One status sample: live sessions, and what their IPs publish and subscribe."""
    relay = (st.get("relay") or {}).get("started") or 0
    day = day_of(now)
    live = {s["id"]: s for s in st.get("sessions") or [] if s.get("addr")}
    ips = {s["addr"] for s in live.values()}
    daily = set()
    for s in live.values():
        db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (relay, sid) "
            "DO UPDATE SET last = excluded.last, role = excluded.role",
            (relay, s["id"], s["addr"], s.get("role") or "", s.get("transport") or "",
             s.get("draft"), s.get("since") or now, now))
        daily.add((day, s["addr"], s.get("role") or "", ""))
    seen = set()
    # A track's origin counts while a session from that IP is live; its
    # subscribers are listed by session.
    for n in st.get("namespaces") or []:
        if n.get("origin_addr") in ips:
            seen.add((n["origin_addr"], "pub", n["namespace"], ""))
    for t in st.get("tracks") or []:
        if t.get("origin_addr") in ips:
            seen.add((t["origin_addr"], "pub", t["namespace"], t["name"]))
        for x in t.get("subscriber_list") or []:
            s = live.get(x.get("session"))
            if s is not None:
                seen.add((s["addr"], "sub", t["namespace"], t["name"]))
    db.executemany(
        "INSERT INTO seen VALUES (?,?,?,?,?,?) ON CONFLICT (ip, role, namespace, track) "
        "DO UPDATE SET last = excluded.last", [(*k, now, now) for k in seen])
    daily.update((day, ip, role, ns) for ip, role, ns, _ in seen)
    db.executemany("INSERT OR IGNORE INTO daily VALUES (?,?,?,?)", daily)


def prune(db, now):
    cut = now - RETAIN_DAYS * 86400
    db.execute("DELETE FROM sessions WHERE last < ?", (cut,))
    db.execute("DELETE FROM seen WHERE last < ?", (cut,))
    db.execute("DELETE FROM daily WHERE day < ?", (day_of(cut),))


def summarize(db, now):
    db.execute("""
        INSERT OR REPLACE INTO days
        SELECT d.day, COUNT(DISTINCT d.ip),
               COUNT(DISTINCT CASE WHEN d.role = 'pub' THEN d.ip END),
               COUNT(DISTINCT CASE WHEN d.role = 'sub' THEN d.ip END),
               (SELECT COUNT(*) FROM sessions s
                WHERE date(s.since, 'unixepoch') = d.day),
               COUNT(DISTINCT CASE WHEN d.role = 'pub' AND d.namespace != ''
                                   THEN d.namespace END)
        FROM daily d GROUP BY d.day""")
    days = [dict(zip(("day", "ips", "pub_ips", "sub_ips", "sessions", "namespaces"), r))
            for r in db.execute("SELECT * FROM days ORDER BY day DESC")]

    peers = {}
    for ip, first, last, n, roles, transports, drafts in db.execute(
            "SELECT ip, MIN(since), MAX(last), COUNT(*), GROUP_CONCAT(DISTINCT role), "
            "GROUP_CONCAT(DISTINCT transport), GROUP_CONCAT(DISTINCT draft) "
            "FROM sessions GROUP BY ip"):
        peers[ip] = {"ip": ip, "first": first, "last": last, "sessions": n,
                     "roles": sorted(filter(None, (roles or "").split(","))),
                     "transports": sorted(filter(None, (transports or "").split(","))),
                     "drafts": sorted({int(d) for d in (drafts or "").split(",") if d}),
                     "pub": {"n": 0, "sample": []}, "sub": {"n": 0, "sample": []}}
    names = {}
    for ip, role, ns, tracks, first, last in db.execute(
            "SELECT ip, role, namespace, COUNT(NULLIF(track, '')), MIN(first), MAX(last) "
            "FROM seen GROUP BY ip, role, namespace ORDER BY MAX(last) DESC"):
        p = peers.setdefault(ip, {"ip": ip, "first": first, "last": last, "sessions": 0,
                                  "roles": [], "transports": [], "drafts": [],
                                  "pub": {"n": 0, "sample": []}, "sub": {"n": 0, "sample": []}})
        side = p[role]
        side["n"] += 1
        if len(side["sample"]) < SAMPLE:
            side["sample"].append({"namespace": ns, "tracks": tracks, "last": last})
        e = names.setdefault(ns, {"namespace": ns, "first": first, "last": last,
                                  "publishers": [], "tracks": 0, "subscribers": 0,
                                  "sub_sample": []})
        e["first"], e["last"] = min(e["first"], first), max(e["last"], last)
        if role == "pub":
            e["tracks"] = max(e["tracks"], tracks)
            if len(e["publishers"]) < SAMPLE:
                e["publishers"].append(ip)
        else:
            e["subscribers"] += 1
            if len(e["sub_sample"]) < SAMPLE:
                e["sub_sample"].append(ip)
    ip_list = sorted(peers.values(), key=lambda p: -p["last"])
    return {"generated": now, "retain_days": RETAIN_DAYS, "interval_s": INTERVAL_S,
            "ip_count": len(ip_list), "days": days, "ips": ip_list[:MAX_IPS],
            "namespaces": sorted(names.values(), key=lambda e: -e["last"])}


def write_report(report):
    tmp = REPORT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(report, f, separators=(",", ":"))
    os.replace(tmp, REPORT)


def main():
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA)
    mtime = None
    reported = 0.0
    while True:
        now = time.time()
        try:
            m = os.stat(STATUS).st_mtime
            if m != mtime:
                with open(STATUS) as f:
                    st = json.load(f)
                mtime = m
                record(db, st, now)
                db.commit()
            if now - reported >= REPORT_S:
                prune(db, now)
                write_report(summarize(db, now))
                db.commit()
                reported = now
        except (OSError, ValueError, sqlite3.Error) as e:
            print(f"peers: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
