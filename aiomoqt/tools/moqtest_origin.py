#!/usr/bin/env python3
"""moqtest ORIGIN — serves draft-afrind-moq-test tracks directly.

A moq-test track is a deterministic function of its 16-field namespace
tuple, so an origin can compute any group, object, or range on demand —
no relay, no cache, no fan-out. This makes the plain aiomoqt server-role
API the SUT for moxygen's moqtest_client (subscribe modes; publish mode
needs the ersatz-relay).

Parameter tuple (mirrors moxygen moqtest/Utils.cpp):
  [0]  "moq-test-00"
  [1]  forwarding preference 0-3 (subgroup-per-group / per-object /
       two-subgroups / datagram)
  [2]  start group        [3]  start object
  [4]  last group         [5]  last object in track
  [6]  objects per group  [7]  size of object 0
  [8]  size of objects >0 [9]  object frequency (ms between objects)
  [10] group increment    [11] object increment
  [12] send end-of-group markers (0/1)
  [13] integer extension id (<0 = off; wire id = 2*id)
  [14] variable extension id (<0 = off; wire id = 2*id+1)
  [15] publisher delivery timeout (ms)

Payload is 't' * size; publisher priority is 200 + (group % 2).
"""
import argparse
import asyncio
import logging
import random
import sys
from dataclasses import dataclass

from aiomoqt.server import MOQTServer
from aiomoqt.messages import ObjectDatagram
from aiomoqt.messages.data import FetchObject
from aiomoqt.types import (
    GroupOrder, MOQTMessageType, ObjectStatus, RequestErrorCode,
    SubscribeDoneCode, parse_draft_spec,
)
from aiomoqt.tools.moq_interop_relay import (
    _find_default_cert, _find_default_key,
)
from aiomoqt.utils.logger import get_logger, set_log_level

logger = get_logger(__name__)

FIELD0 = b"moq-test-00"
PRIORITY_BASE = 200
VAR_EXT_MAX = 20


@dataclass
class TestParams:
    fp: int
    start_group: int
    start_object: int
    last_group: int
    last_object: int
    objects_per_group: int
    size0: int
    size_gt0: int
    freq_ms: int
    g_inc: int
    o_inc: int
    markers: bool
    int_ext: int
    var_ext: int
    pub_timeout: int


def parse_namespace(ns) -> TestParams:
    fields = [f if isinstance(f, bytes) else str(f).encode() for f in ns]
    if len(fields) != 16 or fields[0] != FIELD0:
        raise ValueError("not a moq-test-00 namespace")
    v = [int(f) for f in fields[1:16]]
    p = TestParams(fp=v[0], start_group=v[1], start_object=v[2],
                   last_group=v[3], last_object=v[4],
                   objects_per_group=v[5], size0=v[6], size_gt0=v[7],
                   freq_ms=v[8], g_inc=v[9], o_inc=v[10],
                   markers=bool(v[11]), int_ext=v[12], var_ext=v[13],
                   pub_timeout=v[14])
    if not (0 <= p.fp <= 3) or p.g_inc < 1 or p.o_inc < 1:
        raise ValueError("invalid moq-test parameters")
    return p


def last_object_in_group(p: TestParams) -> int:
    if p.last_object <= p.start_object:
        return p.start_object
    steps = (p.last_object - p.start_object) // p.o_inc
    return p.start_object + steps * p.o_inc


def priority_for(group: int) -> int:
    return PRIORITY_BASE + (group % 2)


def make_extensions(p: TestParams):
    exts = {}
    if p.int_ext >= 0:
        exts[2 * p.int_ext] = random.getrandbits(32)
    if p.var_ext >= 0:
        exts[2 * p.var_ext + 1] = bytes(random.randrange(VAR_EXT_MAX) + 1)
    return exts or None


def _payload(p: TestParams, oid: int) -> bytes:
    return b"t" * (p.size0 if oid == p.start_object else p.size_gt0)


async def _pace(p: TestParams):
    if p.freq_ms:
        await asyncio.sleep(p.freq_ms / 1000)


def _object_ids(p: TestParams):
    return range(p.start_object, p.last_object + 1, p.o_inc)


def _group_ids(p: TestParams):
    return range(p.start_group, p.last_group + 1, p.g_inc)


async def serve_one_subgroup_per_group(session, alias, p) -> int:
    streams = 0
    last = last_object_in_group(p)
    for g in _group_ids(p):
        sid = await session.open_uni_stream()
        streams += 1
        hdr = session.subgroup_header(
            track_alias=alias, group_id=g, subgroup_id=0,
            publisher_priority=priority_for(g), extensions_present=True)
        await session.stream_write_drain(sid, hdr.serialize().data)
        for oid in _object_ids(p):
            if p.markers and oid == last:
                buf = hdr.end_group(object_id=oid)
                session.stream_write(sid, buf.data, end_stream=True)
            else:
                data = hdr.next_object_bytes(
                    payload=_payload(p, oid),
                    extensions=make_extensions(p), object_id=oid)
                await session.stream_write_drain(sid, data)
            await _pace(p)
        if not p.markers:
            session.stream_fin(sid)
    return streams


async def serve_one_subgroup_per_object(session, alias, p) -> int:
    streams = 0
    last = last_object_in_group(p)
    for g in _group_ids(p):
        for oid in _object_ids(p):
            sid = await session.open_uni_stream()
            streams += 1
            hdr = session.subgroup_header(
                track_alias=alias, group_id=g, subgroup_id=oid,
                publisher_priority=priority_for(g),
                extensions_present=True)
            await session.stream_write_drain(sid, hdr.serialize().data)
            if p.markers and oid == last:
                buf = hdr.end_group(object_id=oid)
                session.stream_write(sid, buf.data, end_stream=True)
            else:
                data = hdr.next_object_bytes(
                    payload=_payload(p, oid),
                    extensions=make_extensions(p), object_id=oid)
                await session.stream_write_drain(sid, data)
                session.stream_fin(sid)
            await _pace(p)
    return streams


async def serve_two_subgroups_per_group(session, alias, p) -> int:
    streams = 0
    last = last_object_in_group(p)
    both = p.objects_per_group > 1 and p.o_inc % 2 == 1
    for g in _group_ids(p):
        sids = [None, None]
        hdrs = [None, None]
        for sg in (0, 1):
            if p.start_object % 2 == sg or both:
                sids[sg] = await session.open_uni_stream()
                streams += 1
                hdrs[sg] = session.subgroup_header(
                    track_alias=alias, group_id=g, subgroup_id=sg,
                    publisher_priority=priority_for(g),
                    extensions_present=True)
                await session.stream_write_drain(
                    sids[sg], hdrs[sg].serialize().data)
        for oid in _object_ids(p):
            sg = oid % 2
            if p.markers and oid == last:
                buf = hdrs[sg].end_group(object_id=oid)
                session.stream_write(sids[sg], buf.data, end_stream=True)
                if sids[1 - sg] is not None:
                    session.stream_fin(sids[1 - sg])
            else:
                data = hdrs[sg].next_object_bytes(
                    payload=_payload(p, oid),
                    extensions=make_extensions(p), object_id=oid)
                await session.stream_write_drain(sids[sg], data)
            await _pace(p)
        if not p.markers:
            for sid in sids:
                if sid is not None:
                    session.stream_fin(sid)
    return streams


async def serve_datagrams(session, alias, p) -> int:
    prof = session._profile
    last = last_object_in_group(p)
    for g in _group_ids(p):
        for oid in _object_ids(p):
            if p.markers and oid == last:
                # END_OF_GROUP status datagram: no payload, and
                # extensions are illegal on a non-NORMAL status.
                obj = ObjectDatagram(
                    track_alias=alias, group_id=g, object_id=oid,
                    publisher_priority=priority_for(g),
                    status=ObjectStatus.END_OF_GROUP, payload=b"")
            else:
                obj = ObjectDatagram(
                    track_alias=alias, group_id=g, object_id=oid,
                    publisher_priority=priority_for(g),
                    extensions=make_extensions(p),
                    payload=_payload(p, oid))
            await session.dgram_write_drain(obj.serialize(prof=prof))
            await _pace(p)
    return 0


_SERVERS = {
    0: serve_one_subgroup_per_group,
    1: serve_one_subgroup_per_object,
    2: serve_two_subgroups_per_group,
    3: serve_datagrams,
}


async def _serve_track(session, alias, request_id, p: TestParams):
    try:
        streams = await _SERVERS[p.fp](session, alias, p)
    except asyncio.CancelledError:
        return
    except Exception:
        logger.error("origin: track generation failed", exc_info=True)
        return
    session.subscribe_done(
        request_id=request_id,
        status_code=SubscribeDoneCode.TRACK_ENDED,
        stream_count=streams, reason="track ended")


def _fetch_subgroup(p: TestParams, oid: int) -> int:
    if p.fp == 0:
        return 0
    if p.fp == 1:
        return oid
    if p.fp == 2:
        return oid % 2
    return 0                      # datagram track: subgroup field omitted


def _fetch_window(p: TestParams, rs_g, rs_o, re_g, re_o):
    """Intersect the requested [start..end] with the track's actual
    range. Returns (first_g, first_o, last_g, last_o) or None if empty."""
    fg = max(rs_g, p.start_group)
    lg = min(re_g if re_g else p.last_group, p.last_group)
    if fg > lg:
        return None
    return fg, rs_o, lg, re_o


def _fetch_objects(p: TestParams, win, descending: bool = False):
    """Yield FetchObjects for the window — 't'*size payloads,
    end-of-group markers where asked (never on a datagram track:
    FetchObject markers carry no datagram flag). Group order follows
    `descending` (§10.2.8); objects within a group are always
    ascending."""
    fg, fo, lg, lo = win
    last_in_track = last_object_in_group(p)
    markers = p.markers and p.fp != 3
    groups = range(fg, lg + 1, p.g_inc)
    for g in (reversed(groups) if descending else groups):
        first_o = fo if g == fg else p.start_object
        last_o = lo if (g == lg and lo) else p.last_object
        for oid in range(first_o, last_o + 1, p.o_inc):
            sg = _fetch_subgroup(p, oid)
            if markers and oid >= last_in_track:
                yield FetchObject(group_id=g, subgroup_id=sg, object_id=oid,
                                  publisher_priority=priority_for(g),
                                  status=ObjectStatus.END_OF_GROUP,
                                  payload=b"")
            else:
                yield FetchObject(
                    group_id=g, subgroup_id=sg, object_id=oid,
                    publisher_priority=priority_for(g),
                    extensions=make_extensions(p), payload=_payload(p, oid))


async def _on_fetch(session, msg):
    # Requested order governs the response (omitted = Ascending, §10.2.8).
    order = (GroupOrder.DESCENDING
             if getattr(msg, 'group_order', None) == GroupOrder.DESCENDING
             else GroupOrder.ASCENDING)
    try:
        p = parse_namespace(msg.namespace)
    except ValueError as e:
        session.fetch_error(request_id=msg.request_id,
                            error_code=int(RequestErrorCode.DOES_NOT_EXIST),
                            reason=str(e))
        return
    win = _fetch_window(p, msg.start_group or 0, msg.start_object or 0,
                        msg.end_group or 0, msg.end_object or 0)
    if win is None:
        # §10.13: FETCH_OK's End Location must be known before it is sent.
        session.fetch_ok(request_id=msg.request_id,
                         largest_group_id=0, largest_object_id=0)
        await session.serve_fetch(msg.request_id, ())
        return
    fg, fo, lg, lo = win
    session.fetch_ok(request_id=msg.request_id, largest_group_id=lg,
                     largest_object_id=last_object_in_group(p),
                     group_order=order)
    # FETCH_OK is terminal on the request stream (§3.3.2); the objects
    # ride a separate uni stream. FIN the request stream so the peer
    # stops waiting on it.
    _fin = session._bidi_streams.get(msg.request_id)
    if _fin is not None:
        session.stream_fin(_fin)
    logger.info(f"origin: FETCH fp={p.fp} window={fg}.{fo}..{lg} "
                f"order={int(order)}")
    objs = list(_fetch_objects(
        p, win, descending=(order == GroupOrder.DESCENDING)))
    task = asyncio.create_task(session.serve_fetch(
        msg.request_id, objs, group_order=order))
    session.register_request_cancel_handler(
        msg.request_id, lambda rid: task.cancel())


async def _on_subscribe(session, msg):
    try:
        p = parse_namespace(msg.track_namespace)
    except ValueError as e:
        logger.info(f"origin: subscribe rejected: {e}")
        session.subscribe_error(
            request_id=msg.request_id,
            error_code=int(RequestErrorCode.DOES_NOT_EXIST),
            reason=str(e))
        return
    ok = session.subscribe_ok(request_msg=msg)
    logger.info(f"origin: serving fp={p.fp} groups="
                f"{p.start_group}..{p.last_group} objects="
                f"{p.start_object}..{p.last_object} alias={ok.track_alias}")
    task = asyncio.create_task(
        _serve_track(session, ok.track_alias, msg.request_id, p))
    session.register_request_cancel_handler(
        msg.request_id, lambda rid: task.cancel())


def _build_server(bind, port, cert, key, use_quic, draft):
    server = MOQTServer(host=bind, port=port, certificate=cert,
                        private_key=key, path="/", use_quic=use_quic,
                        supported_drafts=draft)
    server.register_handler(MOQTMessageType.SUBSCRIBE, _on_subscribe)
    server.register_handler(MOQTMessageType.FETCH, _on_fetch)
    return server


def parse_args():
    ap = argparse.ArgumentParser(description="moqtest origin (SUT)")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=4443)
    ap.add_argument("--cert")
    ap.add_argument("--key")
    ap.add_argument("--quic", action="store_true",
                    help="raw QUIC instead of H3/WebTransport")
    ap.add_argument("--dual", action="store_true",
                    help="both transports on one port")
    ap.add_argument("--draft", type=parse_draft_spec, default=None)
    ap.add_argument("--debug", action="store_true")
    return ap.parse_args()


async def main():
    args = parse_args()
    set_log_level(logging.DEBUG if args.debug else logging.INFO)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    cert = args.cert or _find_default_cert()
    key = args.key or _find_default_key(cert)
    if not cert or not key:
        print("Error: TLS cert/key required", file=sys.stderr)
        sys.exit(2)
    server = _build_server(args.bind, args.port, cert, key,
                           args.quic, args.draft)
    mode = ("dual" if args.dual else
            "raw QUIC" if args.quic else "H3/WebTransport")
    print(f"moqtest-origin: {mode} on {args.bind}:{args.port}",
          file=sys.stderr)
    # serve()/serve_dual() bind and return a handle; hold the process
    # open until cancelled so the listener stays up.
    handle = await (server.serve_dual() if args.dual else server.serve())
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        handle.close()


def cli():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
