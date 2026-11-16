"""Text and JSON views of a FabricNode: log fields, the report, the
interval line.

Every event carries t_ms (monotonic, from the node's start) and t_unix_ns
(wall clock at the same instant); the report's clock block gives both
origins, so reports from several processes on one host line up.
"""
import json
import os
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Sequence

from aiomoqt.peering.hashing import libstdcxx_hash, node_id_format
from aiomoqt.peering.wire import (AnnounceInfo, Connect, ConnectResponse,
                                  DataHeader, NodeInfo, NodeSet, NodeType,
                                  SubscribeInfo)


def text(raw: bytes) -> str:
    return bytes(raw).decode("utf-8", "backslashreplace")


def track_text(namespace: Sequence[bytes], name: bytes = b"") -> str:
    path = "/".join(text(t) for t in namespace)
    return f"{path}/{text(name)}" if name else path


def hash_text(value: int) -> str:
    return f"0x{value:016x}"


def node_type_text(value: int) -> str:
    try:
        return NodeType(value).name.lower()
    except ValueError:
        return str(value)


def _path_text(node: NodeInfo) -> str:
    return ",".join(f"{node_id_format(p.node_id)}({p.srtt_us}us,{p.metric})"
                    for p in node.path)


def describe(body: Any) -> str:
    """One log line's worth of a message body."""
    if isinstance(body, NodeInfo):
        out = (f"node={node_id_format(body.node_id)} contact={body.contact!r} "
               f"type={node_type_text(body.node_type)} metric={body.metric}")
        return out + (f" path=[{_path_text(body)}]" if body.path else "")
    if isinstance(body, Connect):
        return f"mode={body.mode} {describe(body.node)}"
    if isinstance(body, ConnectResponse):
        if body.node is None:
            return f"code={body.code}"
        return f"code={body.code} {describe(body.node)}"
    if isinstance(body, AnnounceInfo):
        kind = "namespace" if body.is_namespace else "track"
        return (f"source={node_id_format(body.source)} {kind} "
                f"{track_text(body.namespace, body.name)} "
                f"hash={hash_text(body.fullname_hash)}")
    if isinstance(body, SubscribeInfo):
        return (f"source={node_id_format(body.source)} "
                f"{track_text(body.namespace, body.name)} epoch={body.epoch} "
                f"seq={body.seq} forward={body.forward} "
                f"priority={body.priority} "
                f"hash={hash_text(body.fullname_hash)}")
    if isinstance(body, NodeSet):
        nodes = ",".join(node_id_format(n) for n in body.nodes)
        return f"sns={body.sns_id} priority={body.priority} nodes=[{nodes}]"
    return repr(body)


# -- JSON ------------------------------------------------------------

def _ms(node, mono_ns: int) -> float:
    return round((mono_ns - node.start_mono_ns) / 1e6, 3)


def node_json(info: NodeInfo) -> Dict[str, Any]:
    return {
        "node": node_id_format(info.node_id),
        "node_int": info.node_id,
        "contact": info.contact,
        "type": node_type_text(info.node_type),
        "metric": info.metric,
        "path": [{"node": node_id_format(p.node_id), "srtt_us": p.srtt_us,
                  "metric": p.metric} for p in info.path],
        "sum_metric": info.sum_metric(),
        "sum_srtt_us": info.sum_srtt(),
        # The library derives its node id as std::hash(label).
        "id_is_label_hash": libstdcxx_hash(info.contact.encode(
            "utf-8", "surrogateescape")) == info.node_id,
    }


def announce_json(a: AnnounceInfo) -> Dict[str, Any]:
    return {
        "source": node_id_format(a.source),
        "ns": [text(t) for t in a.namespace],
        "name": text(a.name),
        "namespace": a.is_namespace,
        "hash": hash_text(a.fullname_hash),
    }


def subscribe_json(s: SubscribeInfo) -> Dict[str, Any]:
    return {
        "source": node_id_format(s.source),
        "ns": [text(t) for t in s.namespace],
        "name": text(s.name),
        "epoch": s.epoch,
        "seq": s.seq,
        "forward": s.forward,
        "priority": s.priority,
        "delivery_timeout_ms": s.delivery_timeout_ms,
        "expires_ms": s.expires_ms,
        "new_group_request_id": s.new_group_request_id,
        "hash": hash_text(s.fullname_hash),
        "namespace_hash": hash_text(s.namespace_hash),
        "name_hash": hash_text(s.name_hash),
    }


def sns_json(sns: NodeSet) -> Dict[str, Any]:
    return {"sns": sns.sns_id, "priority": sns.priority,
            "nodes": [node_id_format(n) for n in sns.nodes]}


def event_fields(ev) -> Dict[str, Any]:
    """Kind-specific fields of a SessionEvent."""
    body = ev.body
    fields: Dict[str, Any] = {}
    if isinstance(body, NodeInfo):
        fields.update(node_json(body))
    elif isinstance(body, ConnectResponse):
        fields["code"] = body.code
    elif isinstance(body, AnnounceInfo):
        fields.update(announce_json(body))
    elif isinstance(body, SubscribeInfo):
        fields.update(subscribe_json(body))
    elif isinstance(body, NodeSet):
        fields.update(sns_json(body))
    elif isinstance(body, DataHeader):
        fields.update(hash=hash_text(body.fullname_hash), sns=body.sns_id)
    if ev.hash_ok is not None:
        fields["hash_ok"] = ev.hash_ok
    if ev.note:
        fields["note"] = ev.note
    return fields


def _stamped(node, rec, fields: Dict[str, Any]) -> Dict[str, Any]:
    fields["t_ms"] = _ms(node, rec.mono_ns)
    fields["t_unix_ns"] = rec.unix_ns
    return fields


def session_json(node, s) -> Dict[str, Any]:
    remote = s.remote
    reader = s.reader
    return {
        "session": s.id,
        "origin": "dialed" if s.dialer else "accepted",
        "peer": s.peer,
        "remote_node": node_id_format(remote.node_id) if remote else None,
        "remote_node_int": remote.node_id if remote else None,
        "contact": remote.contact if remote else None,
        "remote_type": node_type_text(remote.node_type) if remote else None,
        "remote_metric": remote.metric if remote else None,
        "state": s.state,
        "connect_ms": s.connect_ms,
        "srtt_us": s.srtt_us,
        "t_up_ms": _ms(node, s.start_mono_ns),
        "t_up_unix_ns": s.start_unix_ns,
        "t_closed_ms": (_ms(node, s.closed_stamp[0]) if s.closed_stamp
                        else None),
        "close_reason": s.close_reason,
        "rx_msgs": dict(s.rx_msgs),
        "tx_msgs": dict(s.tx_msgs),
        "hash_mismatches": s.hash_mismatches,
        "malformed": reader.malformed,
        "unknown": reader.unknown,
        "bad_version": reader.bad_version,
        "oversize": reader.oversize,
        "last_error": reader.last_error,
        "duplicates": s.duplicates,
        "stale_subscribes": s.stale,
        "self_echoes": s.self_echoes,
        "bad_data": s.bad_data,
    }


def _best_paths(node) -> List[Dict[str, Any]]:
    """Each learned node once, on the session with the best path: lowest
    metric sum, then fewest hops, then lowest srtt sum."""
    best: Dict[int, tuple] = {}
    seen: Dict[int, List[int]] = {}
    for s, rec in node.learned_nodes():
        info = rec.body
        seen.setdefault(info.node_id, []).append(s.id)
        rank = (info.sum_metric(), len(info.path), info.sum_srtt())
        cur = best.get(info.node_id)
        if cur is None or rank < cur[0]:
            best[info.node_id] = (rank, s, rec)
    out = []
    for node_id, (_, s, rec) in sorted(best.items()):
        fields = node_json(rec.body)
        fields["via_session"] = s.id
        fields["sessions"] = seen[node_id]
        fields["updates"] = rec.updates
        out.append(_stamped(node, rec, fields))
    return out


def build_report(node) -> Dict[str, Any]:
    local = node.local
    for s in node.connected():
        node.sample_srtt(s)
    announces = []
    for s, rec in node.learned_announces():
        fields = announce_json(rec.body)
        fields.update(session=s.id, hash_ok=rec.hash_ok)
        announces.append(_stamped(node, rec, fields))
    subscribes = []
    for s, rec in node.learned_subscribes():
        fields = subscribe_json(rec.body)
        fields.update(session=s.id, hash_ok=rec.hash_ok)
        subscribes.append(_stamped(node, rec, fields))
    sns_in = []
    for s, rec in node.learned_sns():
        fields = sns_json(rec.body)
        fields["session"] = s.id
        sns_in.append(_stamped(node, rec, fields))
    data_in = []
    for s, full_hash, flow in node.data_flows():
        data_in.append({
            "session": s.id, "hash": hash_text(full_hash), "sns": flow.sns,
            "streams": flow.streams, "datagrams": flow.datagrams,
            "bytes": flow.bytes, "t_ms": _ms(node, flow.first_mono_ns),
            "t_unix_ns": flow.first_unix_ns})
    sessions = [session_json(node, s) for s in node.sessions.values()]
    return {
        "node": {
            "id": node_id_format(local.node_id),
            "id_int": local.node_id,
            "contact": local.contact,
            "type": node_type_text(local.node_type),
            "metric": local.metric,
            "listen": node.listen_text(),
            "peers": [f"{h}:{p}" for h, p in node.peers],
        },
        "clock": {"start_mono_ns": node.start_mono_ns,
                  "start_unix_ns": node.start_unix_ns,
                  "t_ms": _ms(node, node.clock()[0])},
        "counters": {
            "sessions_up": len(node.connected()),
            "hash_mismatches": sum(s["hash_mismatches"] for s in sessions),
            "malformed": sum(s["malformed"] for s in sessions),
            "unknown": sum(s["unknown"] for s in sessions),
            "dial_failures": dict(node.dial_failures),
            "events_dropped": node.events_dropped,
        },
        "originated": {
            "announces": [announce_json(a) for a in node.originated_announces()],
            "subscribes": [subscribe_json(s)
                           for s in node.originated_subscribes()],
        },
        "sessions": sessions,
        "nodes": _best_paths(node),
        "announces": announces,
        "subscribes": subscribes,
        "sns_in": sns_in,
        "data_in": data_in,
        "events": list(node.events),
    }


def write_report(path: str, report: Dict[str, Any]) -> None:
    """Write the report atomically (a reader never sees half a file)."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".peer-report-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(report, f, indent=1)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def summary_line(node, elapsed: Optional[float] = None) -> str:
    def count(items: Iterable) -> int:
        return sum(1 for _ in items)
    head = f"[{elapsed:7.1f}s] " if elapsed is not None else ""
    mismatches = sum(s.hash_mismatches for s in node.sessions.values())
    return (f"{head}sessions {len(node.connected())}/{len(node.peers) or '-'} "
            f"nodes {len({rec.body.node_id for _, rec in node.learned_nodes()})} "
            f"announces {count(node.learned_announces())} "
            f"subscribes {count(node.learned_subscribes())} "
            f"sns {count(node.learned_sns())} "
            f"hash_mismatches {mismatches}")
