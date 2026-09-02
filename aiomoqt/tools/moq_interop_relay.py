#!/usr/bin/env python3
"""EXPERIMENTAL — minimal MoQT interop relay (not a production relay).

Forwards objects from an upstream publisher to downstream subscribers.
Still missing, deliberately:

  * NO authentication, authorization, or rate limiting
  * NO load handling, backpressure, or production hardening
  * NO group cache, so a late subscriber sees only what arrives next,
    and joining FETCH is not served
  * NO forward-state propagation (REQUEST_UPDATE Forward=0/1 upstream)
  * NO PUBLISH forwarding to SUBSCRIBE_TRACKS subscribers
  * Namespace tables are in-memory and global to the process

Use moxygen, moq-rs, or another real relay for any actual workload.
This relay exists so aiomoqt's server-side primitives have a working
demonstrator and so we can run a conformance suite against ourselves.

Routing model (cross-session, single relay instance):
  - PUBLISH_NAMESPACE: record the announcing session under the
    namespace tuple, respond with the protocol's default RequestOk
    (d16+) / PublishNamespaceOk (d14).
  - SUBSCRIBE: find publishers that announced the namespace or a prefix
    of it (§2.4 Namespace Prefix Matching), subscribe upstream once per
    Full Track Name, answer SUBSCRIBE_OK and fan the objects out to
    every downstream subscriber of that track. With no match, send
    SUBSCRIBE_ERROR / REQUEST_ERROR with TRACK_DOES_NOT_EXIST (d14 code
    0x04, d16+ code 0x10) so a conformance suite sees a spec-correct
    rejection.
  - PUBLISH_NAMESPACE_DONE: drop that session's hold on the namespace.

Run on UDP/4443 with the runner's /certs convention:

  python -m aiomoqt.tools.moq_interop_relay \\
      --bind 0.0.0.0 --port 4443 \\
      --cert /certs/cert.pem --key /certs/priv.key

Transports: WebTransport by default, raw QUIC with `--quic`, or BOTH
on one port with `--dual` (per-connection ALPN dispatch via aiopquic
serve_dispatch). `--quic-port N` remains as the legacy two-listener
arrangement (second raw-QUIC listener sharing the global namespace
table) for runners that expect distinct endpoints.
"""

import argparse
import asyncio
import logging
import os
import sys

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.types import (
    FilterType, GroupOrder, MOQTMessageType, ObjectStatus, RequestErrorCode,
    SubscribeErrorCode, parse_draft_spec,
)
from aiomoqt.messages import SubgroupHeader
from aiomoqt.messages.publish import PublishOk
from aiomoqt.messages.request import RequestError
from aiomoqt.track import SubscribedTrack
from aiomoqt.context import is_draft16_or_later
from aiomoqt.utils.logger import set_log_level, get_logger
from aiomoqt.utils.url import parse_relay_url

# Version confinement. The interop runner injects DRAFT (moq-interop-runner
# PR #95) — or older MOQT_DRAFT — to pin the relay to one draft; the client
# is pinned the same way, so negotiation lands on that draft. When neither is
# set (the open-relay context, where clients offer their full version list),
# advertise every supported draft so any client negotiates.
_RELAY_DRAFT_DEFAULT = (os.environ.get("DRAFT")
                        or os.environ.get("MOQT_DRAFT") or "14,16,18").strip()

logger = get_logger(__name__)


# Cross-session announcement table: namespace tuple -> the publisher
# sessions currently advertising it. A session may announce a namespace
# more than once (sub-tests re-announce), so each session is held with a
# refcount and drops out when it reaches zero or the session closes.
_announced: dict[tuple, dict] = {}

# Tracks being relayed upstream->downstream, keyed by (namespace, name).
_tracks: dict[tuple, "_RelayedTrack"] = {}

# Sessions this relay dialled OUT to. An origin does not announce to us,
# so it never lands in _announced; it is tried as a fallback when no
# inbound publisher covers a namespace. Without this the relay is only
# ever a server, and the whole client leg — SETUP, SUBSCRIBE and object
# receive as a client, upstream disconnect — goes untested.
_upstreams: list = []


async def _dial_upstream(url: str, draft) -> None:
    """Hold a session open to an upstream origin for the process's life."""
    ep = parse_relay_url(url)
    client = MOQTClient(ep.host, ep.port, path=ep.path,
                        use_quic=ep.use_quic, verify_tls=False,
                        supported_drafts=draft)
    while True:
        try:
            async with client.connect() as session:
                await session.client_session_init()
                _upstreams.append(session)
                logger.info(f"relay: upstream connected {url} "
                            f"draft={session.negotiated_draft}")
                try:
                    await session.async_closed()
                finally:
                    if session in _upstreams:
                        _upstreams.remove(session)
        except Exception as e:
            logger.info(f"relay: upstream {url} unavailable: {e}")
        logger.info(f"relay: retrying upstream {url} in 5s")
        await asyncio.sleep(5)


def _announced_match(ns: tuple) -> list[tuple]:
    """Announced namespaces covering `ns`, longest (most specific) first.

    Namespace Prefix Matching (transport §2.4): fields are compared
    sequentially and each must match exactly; an announcement with the
    same or fewer fields than the request qualifies. So announcing
    (foo) covers a SUBSCRIBE for (foo, bar), while (foobar) does not —
    which is why this compares tuple fields and never a joined string.
    """
    hits = [a for a in _announced
            if len(a) <= len(ns) and ns[:len(a)] == a]
    return sorted(hits, key=len, reverse=True)


def _publishers_for(ns: tuple) -> list:
    """Publisher sessions that announced `ns` or a prefix of it."""
    out, seen = [], set()
    for a in _announced_match(ns):
        for sess in _announced[a]:
            if id(sess) not in seen:
                seen.add(id(sess))
                out.append(sess)
    # An origin we dialled advertises nothing to us, so ask it last:
    # _establish_upstream tries candidates in turn and moves on when one
    # does not serve the track.
    for sess in _upstreams:
        if id(sess) not in seen:
            seen.add(id(sess))
            out.append(sess)
    return out


class _RelayedTrack:
    """One upstream subscription fanned out to N downstream subscribers.

    Transport §"Relays": a Full Track Name is matched exactly against
    existing upstream subscriptions, so a second downstream subscriber
    for the same track joins this fan-out instead of opening a second
    upstream SUBSCRIBE.

    Objects arrive on a synchronous callback but forwarding has to open
    streams, which is async — inbound objects go through a queue drained
    by one task per track, which also keeps them in arrival order.
    """

    def __init__(self, key):
        self.key = key
        self.upstream = None
        # Flow B: the publisher's PUBLISH, held until someone subscribes.
        self.pending_publish = None   # (session, Publish msg)
        self.downstream = []          # list of (session, track_alias)
        self.queue = asyncio.Queue()
        self.task = None
        # (id(session), group, subgroup) -> (stream_id, SubgroupHeader)
        self._streams = {}

    def on_stream_end(self, group_id, subgroup_id):
        """Upstream closed a subgroup stream: end ours the same way so
        the subscriber sees a clean group end rather than a reset."""
        self.queue.put_nowait(
            (group_id, subgroup_id or 0, None, None, None, None, "END"))

    def on_object(self, msg, size, ts, group_id, subgroup_id):
        """Upstream delivery callback (sync) — hand off to the drain."""
        gid = getattr(msg, "group_id", None)
        gid = gid if gid is not None else group_id
        # Forward the publisher's priority, never a substitute of our
        # own: a subscriber's scheduling depends on it.
        prio = getattr(msg, "publisher_priority", None)
        self.queue.put_nowait(
            (gid, subgroup_id or 0, msg.object_id,
             bytes(msg.payload), msg.extensions or None,
             128 if prio is None else prio,
             getattr(msg, "status", None),
             getattr(msg, "stream_flags", None)))

    def add_downstream(self, session, track_alias):
        self.downstream.append((session, track_alias))
        if self.task is None:
            self.task = asyncio.create_task(self._forward_loop())

    def drop_session(self, session):
        self.downstream = [(s, a) for (s, a) in self.downstream
                           if s is not session]
        for k in [k for k in self._streams if k[0] == id(session)]:
            self._streams.pop(k, None)

    async def _forward_loop(self):
        while True:
            gid, sgid, oid, payload, exts, prio, status, shape = \
                await self.queue.get()
            for session, alias in list(self.downstream):
                try:
                    await self._forward_one(
                        session, alias, gid, sgid, oid, payload, exts,
                        prio, status, shape)
                except Exception:
                    logger.debug("relay: forward failed, dropping subscriber",
                                 exc_info=True)
                    self.drop_session(session)

    async def _forward_one(self, session, alias, gid, sgid, oid,
                           payload, exts, prio, status=None, shape=None):
        """Write one object downstream, opening the (group, subgroup)
        stream on first sight. Group/subgroup identity is preserved from
        upstream so the downstream sees the publisher's structure."""
        skey = (id(session), gid, sgid)
        entry = self._streams.get(skey)
        if entry is None and status == "END":
            return
        if entry is None:
            stream_id = await session.open_uni_stream()
            # Mirror the upstream stream's shape: END_OF_GROUP,
            # FIRST_OBJECT and the subgroup-id mode are part of the
            # delivery semantics a subscriber checks, and re-encoding
            # with our own flags loses them.
            first_obj, eog, inherited = shape or (False, False, False)
            header = SubgroupHeader(
                track_alias=alias, group_id=gid, subgroup_id=sgid,
                publisher_priority=prio, extensions_present=True,
                end_of_group=eog, first_object=first_obj,
                prof=session._profile)
            session.stream_write(stream_id, header.serialize().data)
            entry = (stream_id, header)
            self._streams[skey] = entry
        stream_id, header = entry
        if status == "END":
            # Upstream closed the subgroup stream without a marker, so
            # the group end IS the FIN. Writing a marker here would be
            # inventing one the publisher never sent.
            session.stream_write(stream_id, b"", end_stream=True)
            self._streams.pop(skey, None)
            return
        if status in (ObjectStatus.END_OF_GROUP, ObjectStatus.END_OF_TRACK):
            # Upstream sent an explicit marker: forward it, then FIN.
            buf = header.end_group(
                object_id=header.next_object_id if oid is None else oid)
            session.stream_write(stream_id, buf.data, end_stream=True)
            self._streams.pop(skey, None)
            return
        buf = header.next_object(payload=payload, extensions=exts,
                                 object_id=oid)
        await session.stream_write_drain(stream_id, buf.data)


def _forget_session(session) -> None:
    """Drop everything a closing session owned: its announcements and
    its downstream subscriptions."""
    for ns in list(_announced):
        if _announced[ns].pop(session, None) is not None and \
                not _announced[ns]:
            del _announced[ns]
    for track in list(_tracks.values()):
        track.drop_session(session)


def _ns_tuple(namespace):
    """Normalize a namespace value (str / list / tuple of bytes-or-str)
    into a tuple of bytes for use as a dict key."""
    if isinstance(namespace, str):
        return tuple(s.encode() for s in namespace.split("/") if s)
    if isinstance(namespace, (list, tuple)):
        return tuple(
            s.encode() if isinstance(s, str) else bytes(s)
            for s in namespace
        )
    return tuple()


async def _on_publish_namespace(session, msg):
    """Record the announcing session, ack with default OK path."""
    ns = _ns_tuple(msg.namespace)
    holders = _announced.setdefault(ns, {})
    holders[session] = holders.get(session, 0) + 1
    logger.info(f"relay: announce ns={ns} -> {len(holders)} publisher(s)")
    # Reuse the protocol's built-in OK helper. It emits RequestOk on
    # d16+ and PublishNamespaceOk on d14, matching peer expectation.
    session.publish_namepace_ok(msg)


async def _on_publish_namespace_done(session, msg):
    """Drop one publisher's hold on the namespace."""
    ns = _ns_tuple(msg.namespace) if msg.namespace else None
    holders = _announced.get(ns) if ns is not None else None
    if not holders or session not in holders:
        logger.info(f"relay: publish_namespace_done for unknown ns={ns}")
        return
    holders[session] -= 1
    if holders[session] <= 0:
        del holders[session]
    if not holders:
        del _announced[ns]
    logger.info(f"relay: namespace_done ns={ns} -> "
                f"{len(_announced.get(ns, {}))} publisher(s)")


async def _establish_upstream(ns, track_name):
    """Subscribe upstream once per Full Track Name and return the
    fan-out, or None when no publisher accepts.

    Transport §"Relays": with no Established upstream subscription for
    the Track, the relay subscribes to each publisher that announced the
    subscription's namespace or a prefix of it.
    """
    key = (ns, track_name)
    track = _tracks.get(key)
    if track is not None and track.upstream is not None:
        return track
    for pub in _publishers_for(ns):
        track = _RelayedTrack(key)
        name = (track_name.decode() if isinstance(track_name, bytes)
                else track_name)
        upstream = SubscribedTrack(
            pub, "/".join(x.decode() for x in ns), name,
            on_object=track.on_object)
        try:
            await upstream.subscribe(timeout=10.0)
        except Exception as e:
            logger.info(f"relay: upstream subscribe failed on {ns}: {e}")
            continue
        track.upstream = upstream
        pub.register_stream_end_handler(upstream.track_alias,
                                        track.on_stream_end)
        _tracks[key] = track
        logger.info(f"relay: upstream established ns={ns} track={name} "
                    f"alias={upstream.track_alias}")
        return track
    return None


async def _on_publish(session, msg):
    """Flow B: a publisher offers a track with no prior
    PUBLISH_NAMESPACE (transport 0x1D, present in d14/d16/d18 alike).

    The PUBLISH_OK is held until a subscriber arrives. This relay keeps
    no group cache, so accepting data before anyone wants it would
    publish the track into a void and leave a later subscriber with a
    partial track. Holding the reply keeps the publisher parked instead;
    its transaction timeout is far longer than the wait.
    """
    ns = _ns_tuple(msg.track_namespace)
    key = (ns, msg.track_name)
    track = _tracks.get(key)
    if track is None:
        track = _RelayedTrack(key)
        _tracks[key] = track
    track.pending_publish = (session, msg)
    logger.info(f"relay: publish ns={ns} track={msg.track_name} "
                f"alias={msg.track_alias} — holding PUBLISH_OK for a "
                f"subscriber")
    if track.downstream:
        _accept_publish(track)


def _accept_publish(track) -> None:
    """Answer a held PUBLISH with forward=1 and start taking objects."""
    if track.pending_publish is None or track.upstream is not None:
        return
    session, msg = track.pending_publish
    session._track_aliases[msg.track_alias] = msg.request_id
    session.register_object_handler(msg.track_alias, track.on_object)
    session.register_stream_end_handler(msg.track_alias, track.on_stream_end)
    ok = PublishOk(
        request_id=msg.request_id, forward=1, priority=128,
        group_order=GroupOrder.ASCENDING,
        filter_type=FilterType.LATEST_OBJECT, parameters={})
    logger.info(f"relay: PUBLISH_OK forward=1 alias={msg.track_alias}")
    session._send_reply(msg.request_id, ok)
    track.upstream = session
    track.pending_publish = None


async def _on_subscribe(session, msg):
    """Relay a SUBSCRIBE: establish upstream, then fan out downstream."""
    ns = _ns_tuple(msg.track_namespace)

    # Flow B first: a track already offered by PUBLISH is served from
    # that offer, no upstream SUBSCRIBE needed.
    track = _tracks.get((ns, msg.track_name))
    if track is not None and (track.pending_publish or track.upstream):
        ok = session.subscribe_ok(request_msg=msg)
        track.add_downstream(session, ok.track_alias)
        _accept_publish(track)
        logger.info(f"relay: subscribe ns={ns} track={msg.track_name} "
                    f"-> SUBSCRIBE_OK (published track, fanout="
                    f"{len(track.downstream)})")
        return

    # A dialled origin announces nothing, so an empty announcement table
    # is not proof the track is unavailable.
    if _announced_match(ns) or _upstreams:
        track = await _establish_upstream(ns, msg.track_name)
        if track is not None:
            ok = session.subscribe_ok(request_msg=msg)
            track.add_downstream(session, ok.track_alias)
            logger.info(f"relay: subscribe ns={ns} track={msg.track_name} "
                        f"-> SUBSCRIBE_OK (fanout="
                        f"{len(track.downstream)})")
            return
        # Announced but no publisher served it. Answer honestly rather
        # than acking a track that will never deliver.
        logger.info(f"relay: subscribe ns={ns} track={msg.track_name} "
                    f"-> ERROR (no upstream)")
    logger.info(f"relay: subscribe ns={ns} track={msg.track_name} "
                f"-> ERROR (not announced)")
    # On d16+ the universal REQUEST_ERROR (0x05) carries the not-found
    # code (0x10). On d14 the legacy SUBSCRIBE_ERROR (0x05) carries
    # TRACK_DOES_NOT_EXIST (0x04). Send the right shape per version.
    if is_draft16_or_later(session.negotiated_draft):
        err = RequestError(
            request_id=msg.request_id,
            error_code=int(RequestErrorCode.DOES_NOT_EXIST),
            retry_interval=0,
            reason="track does not exist",
        )
        logger.info(f"MOQT send: {err}")
        session.send_control_message(err)
    else:
        session.subscribe_error(
            request_id=msg.request_id,
            error_code=int(SubscribeErrorCode.TRACK_DOES_NOT_EXIST),
            reason="track does not exist",
        )


def _find_default_cert():
    candidates = [
        '/certs/cert.pem',
        os.path.join(os.path.dirname(__file__),
                     '..', '..', 'certs', 'cert.pem'),
        os.path.expanduser('~/.local/share/moqt/cert.pem'),
    ]
    for c in candidates:
        if os.path.exists(c):
            return os.path.realpath(c)
    return None


def _find_default_key(cert_path):
    if not cert_path:
        return None
    for name in ('priv.key', 'key.pem'):
        candidate = os.path.join(os.path.dirname(cert_path), name)
        if os.path.exists(candidate):
            return candidate
    return None


def parse_args():
    parser = argparse.ArgumentParser(
        description="MoQT interop-runner relay (control-plane only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--bind", type=str, default="0.0.0.0",
                        help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=4443,
                        help="UDP listen port (default: 4443)")
    parser.add_argument("--cert", type=str, default=None,
                        help="TLS cert PEM "
                             "(default: /certs/cert.pem)")
    parser.add_argument("--key", type=str, default=None,
                        help="TLS key PEM "
                             "(default: /certs/priv.key)")
    parser.add_argument("--quic", action="store_true",
                        help="Serve raw QUIC instead of WebTransport")
    parser.add_argument("--dual", action="store_true",
                        help="Serve raw QUIC AND H3/WebTransport on "
                             "--port (single-port ALPN dispatch); "
                             "excludes --quic/--quic-port")
    parser.add_argument("--quic-port", type=int, default=None,
                        help="Also serve raw QUIC on this port via a "
                             "second listener in the same process "
                             "(shares the namespace table) — lets one "
                             "instance back both remote-webtransport "
                             "(--port) and remote-quic (--quic-port)")
    parser.add_argument("--draft", type=parse_draft_spec,
                        default=parse_draft_spec(_RELAY_DRAFT_DEFAULT),
                        help="MoQT draft(s) to serve: a single draft confines "
                             "negotiation to it; a list offers all of them. "
                             "Default from $DRAFT / $MOQT_DRAFT, else 14,16,18.")
    parser.add_argument("--upstream", action="append", default=[],
                        metavar="URL",
                        help="Origin to dial for tracks no inbound "
                             "publisher serves, e.g. "
                             "moqt://host:4433/ or https://host:443/moq. "
                             "Repeat for several. Without it the relay "
                             "only serves publishers that connect to it.")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    return parser.parse_args()


def _build_server(bind, port, cert, key, use_quic, draft):
    """Construct a MOQTServer with the relay's control-plane handlers."""
    server = MOQTServer(
        host=bind, port=port,
        certificate=cert, private_key=key,
        path="/",
        use_quic=use_quic,
        supported_drafts=draft,
    )
    server.register_handler(
        MOQTMessageType.PUBLISH_NAMESPACE, _on_publish_namespace)
    server.register_handler(
        MOQTMessageType.PUBLISH_NAMESPACE_DONE, _on_publish_namespace_done)
    server.register_handler(
        MOQTMessageType.SUBSCRIBE, _on_subscribe)
    server.register_handler(
        MOQTMessageType.PUBLISH, _on_publish)
    return server


async def main():
    args = parse_args()
    log_level = logging.DEBUG if args.debug else logging.INFO
    set_log_level(log_level)
    logging.basicConfig(
        level=log_level, stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cert = args.cert or _find_default_cert()
    key = args.key or _find_default_key(cert)
    if not cert or not key:
        print("Error: TLS cert/key required. Use --cert/--key or place "
              "cert.pem+priv.key at /certs/", file=sys.stderr)
        sys.exit(2)

    if args.dual and (args.quic or args.quic_port is not None):
        print("Error: --dual excludes --quic/--quic-port (one port "
              "serves both transports)", file=sys.stderr)
        sys.exit(2)

    # Primary listener: WebTransport unless --quic; both with --dual.
    listeners = [(_build_server(args.bind, args.port, cert, key,
                                args.quic, args.draft),
                  args.port,
                  "dual raw QUIC + H3/WebTransport" if args.dual
                  else ("raw QUIC" if args.quic else "H3/WebTransport"))]

    # Optional second listener: raw QUIC on --quic-port, sharing the
    # global namespace table. One process then backs both a
    # remote-webtransport and a remote-quic interop endpoint.
    if args.quic_port is not None:
        if args.quic_port == args.port:
            print("Error: --quic-port must differ from --port (two UDP "
                  "binds can't share one port; same-port dual-ALPN would "
                  "need aiopquic support)", file=sys.stderr)
            sys.exit(2)
        listeners.append(
            (_build_server(args.bind, args.quic_port, cert, key,
                           True, args.draft),
             args.quic_port, "raw QUIC"))

    handles = [await (server.serve_dual() if args.dual else server.serve())
               for server, _port, _label in listeners]

    # Dial origins after the listeners are up: an origin may itself be
    # waiting on us, and a failed dial retries rather than aborting.
    upstream_tasks = [asyncio.create_task(_dial_upstream(url, args.draft))
                      for url in args.upstream]
    for url in args.upstream:
        print(f"  upstream origin: {url}")

    print(
        "=" * 64
        + "\n EXPERIMENTAL aiomoqt interop relay.\n"
        " NOT a production relay: no group cache, no auth,\n"
        " no scale handling. Use moxygen / moq-rs for real workloads.\n"
        + "=" * 64,
        file=sys.stderr,
    )
    for _server, port, label in listeners:
        print(f"Listening on {args.bind}:{port} "
              f"({label}, draft-{args.draft})", file=sys.stderr)

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        for h in handles:
            h.close()


def cli():
    """Console entry point (moq-interop-relay)."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Interrupted.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
