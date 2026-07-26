"""aiomoqt load_sim — MoQT relay load SIMULATOR (not a bench).

Simulates a realistic population against a relay: a matrix of
publishers and churning per-track audiences, for exercising relay
session handling and analytics/dashboards. It reports what it did
(populations, join/leave rates, delivery sanity) — it does not
measure throughput ceilings or latency floors; use pub_bench /
sub_bench / adaptive_bench for that.

Publishers (namespaces x tracks) run on bitrate profiles with a
start/duration timeline; per-track subscriber GROUPS evolve each second:

  join   — ramp toward the group target at join_rate subs/s, tapering as
           the group fills
  churn  — fractional random departures per second, escalating as the
           stream ages (churn_rate * (1 + progress))
  exodus — accelerating departures in the final leave_early seconds of
           the publisher's scheduled duration

Departed capacity is refilled by the join engine while the stream is
live, so populations breathe rather than monotonically drain.

Process layout: one process per publisher track; subscriber groups are
sharded into host processes (--subs-per-proc each) that maintain a
dynamic set of one-connection-per-subscriber slots under command-queue
control. The relay sees each subscriber as a distinct QUIC session.

Scenario file (JSON):

  {
    "relay": "moqt://localhost:4444",          // optional; -r overrides
    "publishers": [
      {"namespace": "conf-main", "track": "keynote",
       "profile": "1080p_high",                // or bitrate_kbps [+ fps]
       "start_delay": 0, "duration": 600, "streams": 1}
    ],
    "subscribers": [                           // >1 group per track is fine
      {"namespace": "conf-main", "track": "keynote", "subs": 50,
       "join_rate": 10, "join_delay": 0, "churn_rate": 0.02,
       "leave_early": 60, "leave_rate": 5}
    ]
  }

Split roles across hosts by running the same scenario with --role pub on
the publisher box and --role sub elsewhere (start within a few seconds
of each other; the subscribe retry window absorbs the skew).

`python -m aiomoqt.examples.load_sim --example` prints a demo scenario.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import signal
import sys
import time
from collections import deque
from typing import Any, Dict, List

from aiomoqt.client import MOQTClient
from aiomoqt.track import SubscribedTrack, TrackState
from aiomoqt.types import FilterType
from aiomoqt.utils.url import parse_relay_url
from aiomoqt.utils.format import fmt_bps

from aiomoqt.examples._bench_workers import (
    _RollingStats,
    _bridge_stop_event,
    _post,
    _setup_quiet_logging,
    _try_install_uvloop,
    pub_worker_entry,
    MP_CTX,
    SUBSCRIBE_RETRY_WINDOW_S,
    SUBSCRIBE_EACH_TIMEOUT_S,
    STATS_INTERVAL_S,
    STOP_POLL_S,
)

TICK_S = 1.0
CMD_POLL_S = 0.1
# Join commands in flight count against the group target until the host
# reports an outcome; expire unanswered ones so a wedged shard can't
# block the join engine forever.
PENDING_JOIN_TTL_S = SUBSCRIBE_RETRY_WINDOW_S + 10.0

# Bitrate ladder (bits/sec, fps). Matches common media rungs; publishers
# may instead give explicit bitrate_kbps [+ fps] or object_size + rate.
PROFILES = {
    "4k":         (15_300_000, 30),
    "1080p_high": (6_200_000, 30),
    "1080p":      (4_600_000, 30),
    "720p_high":  (2_600_000, 30),
    "720p":       (1_900_000, 30),
    "480p":       (1_100_000, 30),
    "360p":       (660_000, 30),
    "mobile":     (450_000, 15),
}

EXAMPLE_SCENARIO = {
    "_comment": "load_sim demo — 3 namespaces, 6 tracks, ~200 peak subs",
    "relay": "moqt://localhost:4444",
    "publishers": [
        {"namespace": "conf-main", "track": "keynote",
         "profile": "1080p_high", "start_delay": 0, "duration": 480},
        {"namespace": "conf-main", "track": "panel",
         "profile": "720p_high", "start_delay": 10, "duration": 420},
        {"namespace": "conf-aux", "track": "qna",
         "profile": "480p", "start_delay": 45, "duration": 240},
        {"namespace": "conf-aux", "track": "field",
         "profile": "mobile", "start_delay": 30, "duration": 300},
        {"namespace": "security", "track": "cam-1",
         "profile": "720p", "start_delay": 5, "duration": 460},
        {"namespace": "security", "track": "cam-2",
         "profile": "360p", "start_delay": 20, "duration": 380},
    ],
    "subscribers": [
        {"namespace": "conf-main", "track": "keynote", "subs": 60,
         "join_rate": 8, "join_delay": 0, "churn_rate": 0.02,
         "leave_early": 60, "leave_rate": 6},
        {"namespace": "conf-main", "track": "keynote", "subs": 25,
         "join_rate": 4, "join_delay": 15, "churn_rate": 0.05,
         "leave_early": 90, "leave_rate": 4},
        {"namespace": "conf-main", "track": "panel", "subs": 30,
         "join_rate": 5, "join_delay": 5, "churn_rate": 0.02,
         "leave_early": 45, "leave_rate": 4},
        {"namespace": "conf-aux", "track": "qna", "subs": 25,
         "join_rate": 6, "join_delay": 5, "churn_rate": 0.03,
         "leave_early": 30, "leave_rate": 5},
        {"namespace": "conf-aux", "track": "field", "subs": 15,
         "join_rate": 3, "join_delay": 5, "churn_rate": 0.04,
         "leave_early": 30, "leave_rate": 3},
        {"namespace": "security", "track": "cam-1", "subs": 20,
         "join_rate": 4, "join_delay": 0, "churn_rate": 0.01,
         "leave_early": 20, "leave_rate": 3},
        {"namespace": "security", "track": "cam-2", "subs": 10,
         "join_rate": 2, "join_delay": 10, "churn_rate": 0.04,
         "leave_early": 20, "leave_rate": 2},
    ],
}


# ---------------------------------------------------------------------------
# Subscriber group host worker — one process, dynamic slot set.
# Commands on cmd_queue:  ('add', n) | ('remove', n) | ('stop',)
# Events on events_queue (all tagged group/shard):
#   {'kind':'evt','ev': 'joined'|'join_failed'|'left'|'ended'|'died', ...}
#   {'kind':'group_stats', 'live', 'rx_bytes', 'rx_objs',
#    'lat_p90_ms', 'total_bytes', ...}   every STATS_INTERVAL_S
# ---------------------------------------------------------------------------

async def _slot_task(cfg, relay, slot, events_q, group, shard,
                     start_delay: float):
    """One subscriber lifetime: connect, subscribe (retrying while the
    publisher's announce propagates), receive until told to leave or the
    track ends, close. No self-heal — the controller's join engine is
    the refill path, so every death is visible as a leave+join on the
    relay, which is the point of a dashboard load test."""
    stop_ev: asyncio.Event = slot["stop"]
    stats: _RollingStats = slot["stats"]
    if start_delay > 0:
        try:
            await asyncio.wait_for(stop_ev.wait(), timeout=start_delay)
            return
        except asyncio.TimeoutError:
            pass

    def _on_object(msg, size_bytes, recv_time_ms, *_args, **_kw):
        stats.on_object(msg, size_bytes, recv_time_ms)

    outcome = "died"
    try:
        client = MOQTClient(
            relay.host, relay.port,
            path=relay.path or "",
            use_quic=relay.use_quic,
            verify_tls=not cfg.get("insecure", False),
            supported_drafts=cfg.get("draft"),
            keep_alive_interval=cfg.get("keep_alive_interval"),
        )
        async with client.connect() as session:
            await session.client_session_init()
            deadline = time.monotonic() + SUBSCRIBE_RETRY_WINDOW_S
            subscribed = False
            track = None
            while time.monotonic() < deadline and not stop_ev.is_set():
                track = SubscribedTrack(
                    session, cfg["namespace"], trackname=cfg["trackname"],
                    on_object=_on_object,
                )
                try:
                    await track.subscribe(
                        timeout=SUBSCRIBE_EACH_TIMEOUT_S,
                        filter_type=FilterType(
                            cfg.get("sub_filter", FilterType.LATEST_OBJECT)),
                    )
                    subscribed = True
                    break
                except Exception as e:
                    m = str(e)
                    if ("code=4" in m or "does not exist" in m
                            or "no such namespace" in m):
                        await asyncio.sleep(0.3)
                        continue
                    break
            if not subscribed:
                _post(events_q, {"kind": "evt", "ev": "join_failed",
                                 "group": group, "shard": shard})
                session.close()
                return
            slot["live"] = True
            _post(events_q, {"kind": "evt", "ev": "joined",
                             "group": group, "shard": shard})
            while not stop_ev.is_set():
                if getattr(track, "state", None) == TrackState.CLOSED:
                    outcome = ("ended" if getattr(track, "completed", False)
                               else "died")
                    break
                await asyncio.sleep(STOP_POLL_S)
            else:
                outcome = "left"
            session.close()
    except Exception:
        if not slot["live"]:
            outcome = "join_failed"
    finally:
        was_live = slot["live"]
        slot["live"] = False
        slot["done"] = True
        if was_live or outcome == "died":
            _post(events_q, {"kind": "evt", "ev": outcome,
                             "group": group, "shard": shard})


async def _group_host_task(cfg, mp_stop_event, cmd_q, events_q):
    group = cfg["group"]
    shard = cfg["shard"]
    stagger = float(cfg.get("stagger", 0.05))
    relay = parse_relay_url(cfg["relay_url"],
                            force_quic=cfg.get("force_quic", False))
    stop_ev = _bridge_stop_event(mp_stop_event)
    slots: Dict[int, Dict[str, Any]] = {}
    next_sid = 0

    def _spawn(delay: float):
        nonlocal next_sid
        sid = next_sid
        next_sid += 1
        slot = {"stop": asyncio.Event(), "stats": _RollingStats(),
                "live": False, "done": False, "task": None}
        slot["task"] = asyncio.create_task(
            _slot_task(cfg, relay, slot, events_q, group, shard, delay))
        slots[sid] = slot

    async def _cmd_loop():
        while not stop_ev.is_set():
            try:
                cmd = cmd_q.get_nowait()
            except Exception:
                await asyncio.sleep(CMD_POLL_S)
                continue
            if cmd[0] == "add":
                for i in range(int(cmd[1])):
                    _spawn(i * stagger)
            elif cmd[0] == "remove":
                # Live slots first (a visible leave), then pending joins.
                want = int(cmd[1])
                victims = [s for s in slots.values()
                           if s["live"] and not s["stop"].is_set()]
                if len(victims) < want:
                    victims += [s for s in slots.values()
                                if not s["live"] and not s["done"]
                                and not s["stop"].is_set()]
                for s in victims[:want]:
                    s["stop"].set()
            elif cmd[0] == "stop":
                stop_ev.set()

    reaped_bytes = 0
    reaped_objs = 0

    async def _stats_loop():
        nonlocal reaped_bytes, reaped_objs
        while not stop_ev.is_set():
            await asyncio.sleep(STATS_INTERVAL_S)
            # Reap finished slots, folding their cumulative totals into
            # the group's running counters so departed subscribers keep
            # counting toward total_bytes.
            for sid in [k for k, s in slots.items() if s["done"]]:
                snap = slots[sid]["stats"].snapshot()
                reaped_bytes += snap["total_bytes"]
                reaped_objs += snap["total_objs"]
                del slots[sid]
            rx_bytes = rx_objs = live = 0
            total_bytes = reaped_bytes
            worst_p90 = 0.0
            for s in slots.values():
                snap = s["stats"].snapshot()
                rx_bytes += snap["rx_bytes"]
                rx_objs += snap["rx_objs"]
                total_bytes += snap["total_bytes"]
                worst_p90 = max(worst_p90, snap["lat_p90_ms"])
                if s["live"]:
                    live += 1
            _post(events_q, {
                "kind": "group_stats", "group": group, "shard": shard,
                "live": live, "rx_bytes": rx_bytes, "rx_objs": rx_objs,
                "lat_p90_ms": worst_p90, "total_bytes": total_bytes,
                "t": time.monotonic(),
            })

    cmd_task = asyncio.create_task(_cmd_loop())
    stats_task = asyncio.create_task(_stats_loop())
    await stop_ev.wait()
    for s in slots.values():
        s["stop"].set()
    tasks = [s["task"] for s in slots.values() if s["task"]]
    if tasks:
        await asyncio.wait(tasks, timeout=5.0)
    for t in (cmd_task, stats_task, *tasks):
        t.cancel()
    for t in (cmd_task, stats_task, *tasks):
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass


def group_host_entry(cfg, mp_stop_event, cmd_q, events_q):
    """Process entrypoint for a subscriber group shard."""
    _setup_quiet_logging(cfg.get("logdir"),
                         f"grp-{cfg['group']}-{cfg['shard']}",
                         cfg.get("debug", False))
    if not cfg.get("no_uvloop", False):
        _try_install_uvloop()
    try:
        asyncio.run(_group_host_task(cfg, mp_stop_event, cmd_q, events_q))
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Scenario model
# ---------------------------------------------------------------------------

class PubSpec:
    def __init__(self, cfg: dict, dscale: float):
        self.namespace = cfg["namespace"]
        self.track = cfg["track"]
        self.key = f"{self.namespace}/{self.track}"
        if "profile" in cfg:
            if cfg["profile"] not in PROFILES:
                raise ValueError(f"unknown profile {cfg['profile']!r}; "
                                 f"choose from {sorted(PROFILES)}")
            bps, fps = PROFILES[cfg["profile"]]
        else:
            bps = int(cfg["bitrate_kbps"]) * 1000
            fps = int(cfg.get("fps", 30))
        self.bps = bps
        self.fps = float(cfg.get("rate", fps))
        self.object_size = int(cfg.get("object_size",
                                       max(64, round(bps / 8 / self.fps))))
        gop_s = float(cfg.get("gop_s", 2.0))
        self.group_size = max(1, int(self.fps * gop_s))
        self.streams = int(cfg.get("streams", 1))
        self.start_delay = float(cfg.get("start_delay", 0)) * dscale
        self.duration = float(cfg.get("duration", 300)) * dscale

    def active_at(self, t: float) -> bool:
        return self.start_delay <= t < self.start_delay + self.duration


class GroupSpec:
    def __init__(self, gid: int, cfg: dict, pub: PubSpec):
        self.gid = gid
        self.pub = pub
        self.key = pub.key
        self.max_subs = int(cfg["subs"])
        self.join_rate = max(1, int(cfg.get("join_rate", 10)))
        self.join_delay = float(cfg.get("join_delay", 0))
        self.churn_rate = float(cfg.get("churn_rate", 0.02))
        self.leave_early = float(cfg.get("leave_early", 0))
        self.leave_rate = int(cfg.get("leave_rate", 0))


class PubRT:
    def __init__(self, spec: PubSpec):
        self.spec = spec
        self.proc = None
        self.stop_ev = None
        self.events_q = None
        self.rate_q = None
        self.published = False
        self.ended = False


class ShardRT:
    def __init__(self, idx: int, cap: int, proc, stop_ev, cmd_q):
        self.idx = idx
        self.cap = cap
        self.proc = proc
        self.stop_ev = stop_ev
        self.cmd_q = cmd_q
        self.live = 0
        self.pending: deque = deque()   # monotonic timestamps of sent adds
        self.stats: dict = {}

    def occupancy(self) -> int:
        return self.live + len(self.pending)


class GroupRT:
    def __init__(self, spec: GroupSpec):
        self.spec = spec
        self.shards: List[ShardRT] = []
        self.joins = self.churns = self.exodus = 0
        self.fails = self.deaths = self.ended = 0
        self.iv = {"join": 0, "leave": 0, "fail": 0}
        self.max_live = 0

    def live(self) -> int:
        return sum(s.live for s in self.shards)

    def pending(self) -> int:
        now = time.monotonic()
        for s in self.shards:
            while s.pending and now - s.pending[0] > PENDING_JOIN_TTL_S:
                s.pending.popleft()
        return sum(len(s.pending) for s in self.shards)

    def send_adds(self, n: int):
        for _ in range(n):
            shard = min((s for s in self.shards if s.occupancy() < s.cap),
                        key=lambda s: s.occupancy(), default=None)
            if shard is None:
                return
            shard.cmd_q.put(("add", 1))
            shard.pending.append(time.monotonic())

    def send_removes(self, n: int):
        while n > 0:
            shard = max(self.shards, key=lambda s: s.live)
            if shard.live <= 0:
                return
            take = min(n, shard.live)
            shard.cmd_q.put(("remove", take))
            n -= take


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

def _load_scenario(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _tick_group(g: GroupRT, t: float, rng: random.Random):
    """One second of population evolution for one group."""
    spec = g.spec
    pub = spec.pub
    if not pub.active_at(t):
        return
    stream_time = t - pub.start_delay
    remaining = pub.duration - stream_time
    live = g.live()

    in_exodus = spec.leave_early > 0 and remaining <= spec.leave_early
    if (stream_time >= spec.join_delay and not in_exodus
            and live + g.pending() < spec.max_subs):
        fill = live / spec.max_subs
        eff = int(spec.join_rate * (1 - fill * 0.5)) + rng.randint(-2, 2)
        eff = max(1, eff)
        n = min(eff, spec.max_subs - live - g.pending())
        if n > 0:
            g.send_adds(n)
            g.joins += n

    if live > 0 and stream_time > 10:
        progress = stream_time / pub.duration
        eff = spec.churn_rate * (1 + progress)
        n = int(live * eff)
        if rng.random() < live * eff - n:
            n += 1
        if n > 0:
            g.send_removes(min(n, live))
            g.churns += n

    if spec.leave_early > 0 and 0 < remaining <= spec.leave_early:
        urgency = 1 + (spec.leave_early - remaining) / spec.leave_early
        n = min(int(spec.leave_rate * urgency), g.live())
        if n > 0:
            g.send_removes(n)
            g.exodus += n


def _drain_sub_events(events_q, groups: Dict[int, GroupRT]):
    while True:
        try:
            msg = events_q.get_nowait()
        except Exception:
            return
        g = groups.get(msg.get("group"))
        if g is None:
            continue
        shard = g.shards[msg["shard"]]
        kind = msg["kind"]
        if kind == "group_stats":
            shard.live = msg["live"]
            shard.stats = msg
        elif kind == "evt":
            ev = msg["ev"]
            if ev == "joined":
                if shard.pending:
                    shard.pending.popleft()
                shard.live += 1
                g.iv["join"] += 1
                g.max_live = max(g.max_live, g.live())
            elif ev == "join_failed":
                if shard.pending:
                    shard.pending.popleft()
                g.fails += 1
                g.iv["fail"] += 1
            elif ev in ("left", "ended", "died"):
                shard.live = max(0, shard.live - 1)
                g.iv["leave"] += 1
                if ev == "died":
                    g.deaths += 1
                elif ev == "ended":
                    g.ended += 1


def _predict_egress(pubs: List[PubSpec], groups: List[GroupSpec]):
    per_track: Dict[str, int] = {}
    for gs in groups:
        per_track[gs.key] = per_track.get(gs.key, 0) + gs.max_subs
    total = sum(per_track.get(p.key, 0) * p.bps for p in pubs)
    return total, per_track


def main():
    p = argparse.ArgumentParser(
        prog="python -m aiomoqt.examples.load_sim",
        description="aiomoqt load_sim — MoQT load simulator "
                    "(namespaces x tracks x churning audience)")
    p.add_argument("scenario", nargs="?", help="Scenario JSON path")
    p.add_argument("--example", action="store_true",
                   help="Print a demo scenario JSON and exit")
    p.add_argument("-r", "--relay-url", default=None,
                   help="Relay URL (overrides scenario 'relay')")
    p.add_argument("--role", choices=("both", "pub", "sub"), default="both",
                   help="Run publishers, subscribers, or both (default)")
    p.add_argument("-q", "--quic", action="store_true",
                   help="Raw QUIC even for https:// URLs")
    p.add_argument("-k", "--insecure", action="store_true",
                   help="Skip TLS verification")
    p.add_argument("--draft", type=int, default=None,
                   help="MoQT draft version: 14, 16, or 18")
    p.add_argument("-D", "--duration-scale", type=float, default=1.0,
                   help="Multiply publisher start/duration timelines")
    p.add_argument("--subs-per-proc", type=int, default=100,
                   help="Max subscriber slots per host process "
                        "(default: 100)")
    p.add_argument("--stagger", type=float, default=0.05,
                   help="Seconds between slot opens within a host "
                        "(default: 0.05)")
    p.add_argument("-i", "--interval", type=float, default=5.0,
                   help="Report interval seconds (default: 5)")
    p.add_argument("--report", default=None, metavar="PATH",
                   help="Write per-interval per-track CSV to PATH")
    p.add_argument("--seed", type=int, default=None,
                   help="RNG seed for reproducible churn")
    p.add_argument("--pub-ns", action="store_true",
                   help="Publishers use PUB_NS only (default: PUB_NS + "
                        "PUBLISH, the moxygen flow)")
    p.add_argument("--logdir", default=None,
                   help="Per-process debug log directory")
    p.add_argument("-d", "--debug", action="store_true")
    p.add_argument("--uvloop", dest="use_uvloop", action="store_true",
                   help="Install uvloop in workers")
    args = p.parse_args()

    if args.example:
        print(json.dumps(EXAMPLE_SCENARIO, indent=2))
        return 0
    if not args.scenario:
        p.error("scenario path required (or --example)")

    scenario = _load_scenario(args.scenario)
    relay_url = args.relay_url or scenario.get("relay")
    if not relay_url:
        p.error("no relay URL: pass -r or set 'relay' in the scenario")

    rng = random.Random(args.seed)
    pub_specs = [PubSpec(c, args.duration_scale)
                 for c in scenario.get("publishers", [])]
    by_key = {ps.key: ps for ps in pub_specs}
    group_specs = []
    for i, c in enumerate(scenario.get("subscribers", [])):
        key = f"{c['namespace']}/{c['track']}"
        if key not in by_key:
            p.error(f"subscriber group {i} references unknown track {key}")
        group_specs.append(GroupSpec(i, c, by_key[key]))

    total_bps, per_track_subs = _predict_egress(pub_specs, group_specs)
    horizon = max((ps.start_delay + ps.duration for ps in pub_specs),
                  default=0)
    peak_subs = sum(g.max_subs for g in group_specs)

    print("─" * 72)
    print("  aiomoqt load_sim")
    print("─" * 72)
    print(f"  relay:       {relay_url}   role={args.role}")
    print(f"  tracks:      {len(pub_specs)} across "
          f"{len({ps.namespace for ps in pub_specs})} namespaces")
    print(f"  subscribers: {peak_subs} peak target across "
          f"{len(group_specs)} groups")
    print(f"  timeline:    {horizon:.0f}s "
          f"(duration-scale {args.duration_scale:g})")
    print(f"  worst-case egress if all peaks align: "
          f"{fmt_bps(total_bps)}")
    for ps in pub_specs:
        subs = per_track_subs.get(ps.key, 0)
        print(f"    {ps.key:<28} {fmt_bps(ps.bps):>10} x {subs:>4} subs"
              f"  T+{ps.start_delay:.0f}s..{ps.start_delay + ps.duration:.0f}s"
              f"  obj={ps.object_size}B @ {ps.fps:g}/s")
    print("─" * 72)

    common = dict(
        relay_url=relay_url, force_quic=args.quic, insecure=args.insecure,
        draft=args.draft, debug=args.debug, logdir=args.logdir,
        no_uvloop=not args.use_uvloop,
    )

    pubs = {ps.key: PubRT(ps) for ps in pub_specs} \
        if args.role in ("both", "pub") else {}
    groups: Dict[int, GroupRT] = {}
    sub_events_q = MP_CTX.Queue(maxsize=100000)
    if args.role in ("both", "sub"):
        for gs in group_specs:
            grt = GroupRT(gs)
            nshards = max(1, math.ceil(gs.max_subs / args.subs_per_proc))
            cap = math.ceil(gs.max_subs / nshards)
            for si in range(nshards):
                stop_ev = MP_CTX.Event()
                cmd_q = MP_CTX.Queue(maxsize=10000)
                cfg = dict(common, group=gs.gid, shard=si,
                           namespace=gs.pub.namespace,
                           trackname=gs.pub.track,
                           stagger=args.stagger)
                proc = MP_CTX.Process(
                    target=group_host_entry,
                    args=(cfg, stop_ev, cmd_q, sub_events_q),
                    daemon=True)
                proc.start()
                grt.shards.append(ShardRT(si, cap, proc, stop_ev, cmd_q))
            groups[gs.gid] = grt

    csv_f = None
    if args.report:
        csv_f = open(args.report, "w")
        csv_f.write("t,namespace,track,live,target,rx_mbps,p90_ms,"
                    "joins,leaves,fails\n")

    start = time.monotonic()
    last_report = start
    stopping = False

    def _on_sigint(_sig, _frm):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _on_sigint)

    def _pub_rate_ops(ps: PubSpec) -> float:
        return ps.fps

    try:
        while True:
            now = time.monotonic()
            t = now - start

            # --- publisher timeline ---
            for prt in pubs.values():
                ps = prt.spec
                if (prt.proc is None and not prt.ended
                        and t >= ps.start_delay):
                    prt.stop_ev = MP_CTX.Event()
                    prt.events_q = MP_CTX.Queue(maxsize=1000)
                    prt.rate_q = MP_CTX.Queue(maxsize=8)
                    cfg = dict(common, namespace=ps.namespace,
                               trackname=ps.track,
                               object_size=ps.object_size,
                               group_size=ps.group_size,
                               num_subgroups=ps.streams,
                               initial_rate_ops=_pub_rate_ops(ps),
                               pub_ns=args.pub_ns,
                               pub_both=not args.pub_ns)
                    prt.proc = MP_CTX.Process(
                        target=pub_worker_entry,
                        args=(cfg, prt.stop_ev, prt.rate_q, prt.events_q),
                        daemon=True)
                    prt.proc.start()
                    print(f"  T+{t:>5.0f}s  pub UP    {ps.key} "
                          f"({fmt_bps(ps.bps)})")
                if prt.proc is not None and not prt.ended:
                    while True:
                        try:
                            msg = prt.events_q.get_nowait()
                        except Exception:
                            break
                        kind = msg.get("kind")
                        # pub_stats only flows after a successful
                        # publish, so it confirms too — a single missed
                        # pub_health can't flag a healthy publisher.
                        if kind == "pub_stats" or (
                                kind == "pub_health"
                                and msg.get("state") == "published"):
                            prt.published = True
                    if t >= ps.start_delay + ps.duration:
                        prt.stop_ev.set()
                        prt.ended = True
                        print(f"  T+{t:>5.0f}s  pub DOWN  {ps.key}")

            # --- subscriber population ---
            _drain_sub_events(sub_events_q, groups)
            if not stopping:
                for grt in groups.values():
                    _tick_group(grt, t, rng)

            # --- reporting ---
            if now - last_report >= args.interval:
                last_report = now
                live_total = sum(g.live() for g in groups.values())
                rx_bps = sum(s.stats.get("rx_bytes", 0) * 8
                             for g in groups.values() for s in g.shards)
                p90 = max((s.stats.get("lat_p90_ms", 0.0)
                           for g in groups.values() for s in g.shards),
                          default=0.0)
                pubs_up = sum(1 for prt in pubs.values()
                              if prt.proc is not None and not prt.ended)
                iv = {"join": 0, "leave": 0, "fail": 0}
                for g in groups.values():
                    for k in iv:
                        iv[k] += g.iv[k]
                        g.iv[k] = 0
                line = (f"  T+{t:>5.0f}s  subs={live_total:<5} "
                        f"(+{iv['join']}/-{iv['leave']}"
                        f"/x{iv['fail']})  rx={fmt_bps(rx_bps):<10} "
                        f"p90={p90:.0f}ms")
                if pubs:
                    line += f"  pubs={pubs_up}/{len(pubs)}"
                print(line)
                if csv_f:
                    per_track: Dict[str, list] = {}
                    for g in groups.values():
                        row = per_track.setdefault(
                            g.spec.key, [0, 0, 0, 0.0])
                        row[0] += g.live()
                        row[1] += g.spec.max_subs
                        row[2] += sum(s.stats.get("rx_bytes", 0)
                                      for s in g.shards)
                        row[3] = max(row[3],
                                     max((s.stats.get("lat_p90_ms", 0.0)
                                          for s in g.shards), default=0.0))
                    for key, row in per_track.items():
                        ns, tr = key.split("/", 1)
                        csv_f.write(f"{t:.0f},{ns},{tr},{row[0]},{row[1]},"
                                    f"{row[2] * 8 / 1e6:.2f},"
                                    f"{row[3]:.1f},,,\n")
                    csv_f.flush()

            # --- end conditions ---
            pubs_done = all(prt.ended for prt in pubs.values()) if pubs \
                else t >= horizon
            subs_done = (not groups
                         or (pubs_done
                             and sum(g.live() for g in groups.values()) == 0
                             and t >= horizon + 5))
            if stopping or (pubs_done and subs_done):
                break
            time.sleep(max(0.0, TICK_S - (time.monotonic() - now)))
    finally:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        print("  Shutting down...")
        for prt in pubs.values():
            if prt.proc is not None and not prt.ended:
                prt.stop_ev.set()
        for grt in groups.values():
            for s in grt.shards:
                try:
                    s.cmd_q.put(("stop",))
                except Exception:
                    pass
                s.stop_ev.set()
        deadline = time.monotonic() + 8.0
        procs = [prt.proc for prt in pubs.values() if prt.proc] + \
                [s.proc for g in groups.values() for s in g.shards]
        for proc in procs:
            proc.join(timeout=max(0.1, deadline - time.monotonic()))
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=1.0)
        for q in [sub_events_q]:
            q.cancel_join_thread()
        if csv_f:
            csv_f.close()

    # --- summary + verdict ---
    print("─" * 72)
    failed = False
    for grt in groups.values():
        gs = grt.spec
        total_rx = sum(s.stats.get("total_bytes", 0) for s in grt.shards)
        note = ""
        if grt.max_live == 0:
            note = "  << NEVER JOINED"
            failed = True
        elif grt.max_live < gs.max_subs * 0.5:
            note = "  << BELOW 50% OF TARGET"
            failed = True
        print(f"  {gs.key:<28} peak {grt.max_live:>4}/{gs.max_subs:<4} "
              f"joins={grt.joins:<5} churn={grt.churns:<5} "
              f"exodus={grt.exodus:<4} fails={grt.fails:<4} "
              f"died={grt.deaths:<4} rx={total_rx / 1e6:.0f}MB{note}")
    if pubs:
        for prt in pubs.values():
            if not prt.published:
                print(f"  {prt.spec.key:<28} << PUBLISHER NEVER CONFIRMED")
                failed = True
    print(f"  {'RESULT: FAIL' if failed else 'RESULT: OK'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
