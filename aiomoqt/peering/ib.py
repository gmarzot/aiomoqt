"""Information base for the tracks this node originates, and the path
selection transit shares.

Who subscribes to each track, which session reaches each subscriber node
best, and the one SUBSCRIBE_NODE_SET per egress session that follows from
that; plus the epoch/seq rules a subscribe advertisement is accepted by.
Sans-I/O: reconcile() returns the SNS changes to send.
"""
from collections import defaultdict
from typing import (Any, Callable, Dict, Iterable, List, NamedTuple,
                    Optional, Sequence, Tuple)

from aiomoqt.peering.hashing import track_hashes
from aiomoqt.peering.wire import NodeInfo, NodeSet, SubscribeInfo

DEFAULT_TTL_MS = 5000


# -- subscribe acceptance ---------------------------------------------

def seq_newer(new: int, old: int) -> bool:
    """16-bit serial comparison: new follows old by 1..32767."""
    return 1 <= (new - old) % 65536 <= 32767


def subscribe_outcome(prev: Optional[SubscribeInfo],
                      new: SubscribeInfo) -> str:
    """'learned', 'updated', 'duplicate' or 'stale'. An advertisement is
    taken only within the stored epoch and at a newer seq; another epoch
    is stale while the old record is held."""
    if prev is None:
        return "learned"
    if prev == new:
        return "duplicate"
    if prev.epoch != new.epoch or not seq_newer(new.seq, prev.seq):
        return "stale"
    return "updated"


# -- origin tracks ----------------------------------------------------

class OriginTrack:
    """A track this node publishes: its name and the SNS it holds open on
    each session."""

    def __init__(self, namespace: Tuple[bytes, ...], name: bytes,
                 priority: int = 128, ttl_ms: int = DEFAULT_TTL_MS):
        self.namespace = namespace
        self.name = name
        self.fullname_hash = track_hashes(namespace, name).fullname
        self.priority = priority
        self.ttl_ms = ttl_ms
        self.sns: Dict[int, NodeSet] = {}


class SnsChange(NamedTuple):
    session_id: int
    sns: NodeSet
    withdraw: bool


# Extra cost of egressing on a session (its metric above the node's).
SessionCost = Callable[[Any], int]


def path_rank(info: NodeInfo, extra: int = 0) -> Tuple[int, int, int]:
    """§6.1 order: lowest metric sum, then fewest hops, then lowest srtt
    sum."""
    return info.sum_metric() + extra, len(info.path), info.sum_srtt()


def loops_through(info: NodeInfo, node_id: int) -> bool:
    return any(p.node_id == node_id for p in info.path)


def best_route(node_id: int, sessions: Iterable,
               current: Optional[int] = None, *,
               cost: Optional[SessionCost] = None,
               local_id: Optional[int] = None):
    """The session advertising node_id with the best path rank, or None;
    a full tie keeps `current`, then the lowest session id. A path
    through `local_id` is a loop and never chosen."""
    ranked = []
    for s in sessions:
        rec = s.nodes.get(node_id)
        if rec is None or (local_id is not None
                           and loops_through(rec.body, local_id)):
            continue
        rank = path_rank(rec.body, cost(s) if cost is not None else 0)
        ranked.append((rank, s.id != current, s.id, s))
    return min(ranked, key=lambda r: r[:3])[3] if ranked else None


def best_session(node_id: int, carriers: Sequence, sessions: Iterable,
                 current: Optional[int] = None, *,
                 cost: Optional[SessionCost] = None,
                 local_id: Optional[int] = None):
    """best_route(); a node no session advertised goes to the first
    session its subscribe came on."""
    s = best_route(node_id, sessions, current, cost=cost, local_id=local_id)
    if s is not None:
        return s
    return min(carriers, key=lambda s: s.id) if carriers else None


def subscribers(fullname_hash: int, sessions: Iterable, local_id: int
                ) -> Dict[int, Tuple[List, int]]:
    """Subscriber node -> (sessions its forwarding subscribe came on, its
    priority), from every session's accepted subscribes."""
    out: Dict[int, Tuple[List, int]] = {}
    for s in sessions:
        for (h, source), rec in s.subscribes.items():
            if h != fullname_hash or source == local_id:
                continue
            if not rec.body.forward:
                continue
            carriers, priority = out.setdefault(source,
                                                ([], rec.body.priority))
            carriers.append(s)
    return out


def reconcile(track: OriginTrack, sessions: Sequence, local_id: int,
              next_sns_id: Callable[[Any], int], *,
              cost: Optional[SessionCost] = None) -> List[SnsChange]:
    """Bring track.sns in line with the subscribers seen on `sessions`
    (the connected ones): an SNS_ADV for each new or changed set, an
    SNS_WD for each session left with none. A set keeps its id while it
    lives."""
    sessions = list(sessions)
    current = {n: sid for sid, sns in track.sns.items() for n in sns.nodes}
    want: Dict[int, set] = defaultdict(set)
    priority: Dict[int, int] = {}
    by_id = {s.id: s for s in sessions}
    for node_id, (carriers, prio) in subscribers(
            track.fullname_hash, sessions, local_id).items():
        s = best_session(node_id, carriers, sessions, current.get(node_id),
                         cost=cost, local_id=local_id)
        if s is not None:
            want[s.id].add(node_id)
            priority.setdefault(s.id, prio)
    changes: List[SnsChange] = []
    for sid in [sid for sid in track.sns if sid not in want]:
        changes.append(SnsChange(sid, track.sns.pop(sid), True))
    for sid, nodes in want.items():
        held = track.sns.get(sid)
        members = tuple(sorted(nodes))
        if held is None:
            sns = NodeSet(next_sns_id(by_id[sid]), priority[sid], members)
        elif held.nodes == members:
            continue
        else:
            sns = NodeSet(held.sns_id, held.priority, members)
        track.sns[sid] = sns
        changes.append(SnsChange(sid, sns, False))
    return changes
