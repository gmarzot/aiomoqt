import asyncio
import errno
import socket
import weakref
from asyncio.futures import Future
from typing import Any, Coroutine, Iterable, List, Optional, Tuple, Union

from aiopquic.asyncio.server import serve as aiopquic_serve
from aiopquic.asyncio.dispatch import serve_dispatch
from aiopquic.asyncio.webtransport import serve_webtransport
from aiopquic.quic.configuration import QuicConfiguration

from .protocol import (DEFAULT_TX_MAX_INFLIGHT_BYTES, MOQTPeer,
                        MOQTSessionQuic, MOQTSessionWTServer, USER_AGENT)
from .messages.data import DEFAULT_MAX_OBJECT_BYTES
from .types import (SessionCloseCode, moqt_alpn_for_version,
                    normalize_supported_drafts)
from .utils.logger import *

logger = get_logger(__name__)


_WILDCARDS = (None, "", "::", "0.0.0.0")

# Seconds between checks for new sessions to close while close_sessions waits.
_CLOSE_POLL_S = 0.05


def _cnx_ended(session) -> Optional[asyncio.Event]:
    """Set once the session's connection has ended: raw QUIC's own close,
    WT's connection close (the peer drops it)."""
    return (getattr(session, '_cnx_closed', None)
            or getattr(session, '_closed', None))


def _listen_desc(host: Optional[str]) -> str:
    if host in (None, "", "::"):
        return "all interfaces"
    return "all IPv4 interfaces" if host == "0.0.0.0" else host


def _check_udp_port_free(host: Optional[str], port: int) -> None:
    """Fail before the transport thread swallows EADDRINUSE on a wildcard
    bind; aiopquic checks a specific host itself."""
    if port == 0 or host not in _WILDCARDS:
        return
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("0.0.0.0", port))
    except OSError as e:
        raise OSError(f"UDP port {port} unavailable: {e.strerror}") from e
    finally:
        probe.close()
    if host != "0.0.0.0":
        _warn_if_ipv6_port_taken(port)


def _warn_if_ipv6_port_taken(port: int) -> None:
    """The transport may also serve [::]:port. A conflict there leaves IPv6
    clients out while IPv4 still serves, so it warns; a host without IPv6
    says nothing."""
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    except OSError:
        return
    try:
        probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        probe.bind(("::", port))
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            logger.warning(f"MOQT server: UDP [::]:{port} is in use; IPv6 "
                           f"clients cannot reach this server")
    finally:
        probe.close()


class MOQTServer(MOQTPeer):
    """Server-side session manager."""
    def __init__(
        self,
        host: Optional[str],
        port: int,
        certificate: str,
        private_key: str,
        path: Optional[str] = None,
        use_quic: Optional[bool] = False,
        supported_drafts: Optional[Union[int, List[int]]] = None,
        debug: Optional[bool] = False,
        tx_max_inflight_bytes: Optional[int] = DEFAULT_TX_MAX_INFLIGHT_BYTES,
        tx_max_queued_bytes: Optional[int] = None,
        congestion_control_algorithm: Optional[str] = "bbr",
        congestion_control_options: Optional[str] = None,
        flow_control_window: Optional[int] = None,
        keep_alive_interval: Optional[float] = None,
        socket_buffer_size: Optional[int] = None,
        stream_ring_cap: Optional[int] = None,
        max_connections: Optional[int] = None,
        implementation: Optional[str] = USER_AGENT,
        quiet_peers: Iterable[str] = (),
        max_filter_ranges: int = 0,
        max_object_bytes: int = DEFAULT_MAX_OBJECT_BYTES,
    ):
        super().__init__(tx_max_inflight_bytes=tx_max_inflight_bytes,
                         implementation=implementation,
                         max_object_bytes=max_object_bytes)
        self.host = host
        self.port = port
        self.path = path
        self.use_quic = use_quic
        # Public API: supported_drafts is the set of IETF draft numbers
        # (e.g. 16, or [16, 14]) this server accepts; None means "accept
        # every supported draft". Stored as a non-empty list of draft
        # numbers, newest first; the wire forms (ALPN / IETF version
        # code) are only translated when building ALPN or (de)serializing
        # SETUP. The peer's chosen ALPN selects the negotiated_draft.
        self.supported_drafts = normalize_supported_drafts(supported_drafts)
        self.certificate = certificate
        self.private_key = private_key
        self.debug = debug
        # Aggregate TX gate budget (QuicConfiguration.tx_max_queued_bytes):
        # bounds publisher run-ahead across ALL streams; steady-state
        # added latency ≈ value / drain rate. None = honor the aiopquic
        # default (4 MiB); 0 = disable.
        self.tx_max_queued_bytes = tx_max_queued_bytes
        self.congestion_control_algorithm = congestion_control_algorithm
        # Algorithm option string, e.g. "Q0.02" for bbr.
        self.congestion_control_options = congestion_control_options
        # QUIC flow-control window, per stream and per connection; None = 16 MiB.
        self.flow_control_window = flow_control_window
        # QUIC keep-alive interval (seconds). None = disabled. PING
        # frames hold a flow-controlled, consumer-stalled connection
        # open past the idle timeout instead of dropping it.
        self.keep_alive_interval = keep_alive_interval
        # UDP SO_RCVBUF/SO_SNDBUF request (bytes). None = aiopquic
        # default (64 MiB, kernel-clamped to rmem_max/wmem_max).
        self.socket_buffer_size = socket_buffer_size
        # Per-stream TX ring (QuicConfiguration.stream_ring_cap), allocated
        # whole per stream: memory scales with it times open streams. Must
        # be >= the largest object and above tx_max_inflight_bytes. None =
        # aiopquic default (4 MiB).
        self.stream_ring_cap = stream_ring_cap
        # Most connections held at once (aiopquic >= 0.5.1). None =
        # aiopquic default (256).
        self.max_connections = max_connections
        # Sessions whose peer's SETUP names an implementation starting
        # with one of these log below WARNING at DEBUG (a health probe's
        # sessions, say).
        self.quiet_peers = tuple(quiet_peers)
        # MAX_FILTER_RANGES sent at d20+ (§9.1.6). Nonzero only where the
        # application applies Range Filters; 0 refuses them all.
        self.max_filter_ranges = int(max_filter_ranges)
        self._loop = asyncio.get_running_loop()
        self._server_closed: Future[Tuple[int, str]] = self._loop.create_future()
        self._next_subscribe_id = 1
        self._live: weakref.WeakSet = weakref.WeakSet()
        # Close reason for sessions arriving from now on; None accepts them.
        self._refusal: Optional[str] = None
        # Sessions this server closed, until their connections end.
        self._closing: weakref.WeakSet = weakref.WeakSet()

    def serve(self) -> Coroutine[Any, Any, Any]:
        """Start the MOQT server."""
        logger.info(f"Starting MOQT server on {_listen_desc(self.host)}, "
                    f"UDP port {self.port}")
        _check_udp_port_free(self.host, self.port)

        if self.use_quic:
            # Accept one ALPN per supported draft, newest first. A single
            # supported draft yields a single ALPN (as the explicit-draft
            # path did); the auto set accepts every supported draft so a
            # cross-draft client can negotiate its version via QUIC ALPN.
            alpn = [moqt_alpn_for_version(d) for d in self.supported_drafts]
            cfg = QuicConfiguration(
                alpn_protocols=alpn, is_client=False,
                max_data=2**24, max_stream_data=2**24,
                max_datagram_frame_size=64 * 1024,
            )
            # None defers to the aiopquic default.
            if self.congestion_control_algorithm is not None:
                cfg.congestion_control_algorithm = (
                    self.congestion_control_algorithm)
            if self.congestion_control_options is not None:
                cfg.congestion_control_options = self.congestion_control_options
            if self.flow_control_window is not None:
                cfg.max_data = cfg.max_stream_data = self.flow_control_window
            if self.tx_max_queued_bytes is not None:
                cfg.tx_max_queued_bytes = self.tx_max_queued_bytes
            if self.keep_alive_interval is not None:
                cfg.keep_alive_interval = self.keep_alive_interval
            if self.socket_buffer_size is not None:
                cfg.socket_buffer_size = self.socket_buffer_size
            if self.stream_ring_cap is not None:
                cfg.stream_ring_cap = self.stream_ring_cap
            if self.max_connections is not None:
                cfg.max_connections = self.max_connections
            cfg.load_cert_chain(self.certificate, self.private_key)
            def protocol(*a, **kw):
                return self._adopt(MOQTSessionQuic(*a, **kw, session=self))
            return aiopquic_serve(
                self.host, self.port,
                configuration=cfg,
                create_protocol=protocol,
            )

        # WebTransport mode (use_quic=False): serve_webtransport with
        # MOQTSessionWTServer as the session factory.
        def factory(transport, state):
            return self._adopt(
                MOQTSessionWTServer(transport, state, session=self))

        async def handler(_session):
            pass  # session lifetime owned by the dispatcher

        # Build a QuicConfiguration so FC sizing (max_data,
        # max_stream_data, max_streams_uni/bidi) and the CC algorithm
        # actually reach picoquic's transport params. Without this,
        # serve_webtransport runs with picoquic defaults (1 MiB
        # MAX_DATA, default MAX_STREAM_DATA), which caps loopback
        # throughput and distorts FC-driven backpressure measurements.
        # ALPN, cert, key are owned by serve_webtransport itself
        # (alpn="h3" + cert_file/key_file kwargs) so we don't set them
        # on the cfg here.
        wt_cfg = QuicConfiguration(
            is_client=False,
            max_data=2**24, max_stream_data=2**24,
            max_datagram_frame_size=64 * 1024,
        )
        if self.congestion_control_algorithm is not None:
            wt_cfg.congestion_control_algorithm = (
                self.congestion_control_algorithm)
        if self.congestion_control_options is not None:
            wt_cfg.congestion_control_options = self.congestion_control_options
        if self.flow_control_window is not None:
            wt_cfg.max_data = wt_cfg.max_stream_data = self.flow_control_window
        if self.tx_max_queued_bytes is not None:
            wt_cfg.tx_max_queued_bytes = self.tx_max_queued_bytes
        if self.keep_alive_interval is not None:
            wt_cfg.keep_alive_interval = self.keep_alive_interval
        if self.socket_buffer_size is not None:
            wt_cfg.socket_buffer_size = self.socket_buffer_size
        if self.stream_ring_cap is not None:
            wt_cfg.stream_ring_cap = self.stream_ring_cap
        if self.max_connections is not None:
            wt_cfg.max_connections = self.max_connections

        # WT-Protocol allowlist in supported_drafts order, as for raw-QUIC
        # ALPN: aiopquic selects the first one the client offered and
        # refuses an offer with no match. Drafts >= 15 negotiate this way
        # ("moqt-NN"); d14 uses the legacy in-band CLIENT_SETUP path.
        wt_supported = [f"moqt-{d}"
                        for d in self.supported_drafts
                        if d >= 15] or None
        return serve_webtransport(
            self.host, self.port, self.path or "",
            handler=handler,
            cert_file=self.certificate, key_file=self.private_key,
            session_factory=factory,
            configuration=wt_cfg,
            wt_supported_protocols=wt_supported,
        )

    def serve_dual(self) -> Coroutine[Any, Any, Any]:
        """Serve raw QUIC and H3/WebTransport MoQT on ONE UDP port,
        routed per connection by negotiated ALPN (aiopquic
        serve_dispatch). Raw connections negotiate their draft via the
        per-draft MoQT ALPNs; WT connections via WT-Protocol. Replaces
        the two-listener --quic-port arrangement."""
        logger.info(
            f"Starting dual-stack MOQT server on {_listen_desc(self.host)}, "
            f"UDP port {self.port}")
        _check_udp_port_free(self.host, self.port)
        cfg = QuicConfiguration(
            alpn_protocols=[moqt_alpn_for_version(d)
                            for d in self.supported_drafts],
            is_client=False,
            max_data=2**24, max_stream_data=2**24,
            max_datagram_frame_size=64 * 1024,
            certificate_file=self.certificate,
            private_key_file=self.private_key,
        )
        if self.congestion_control_algorithm is not None:
            cfg.congestion_control_algorithm = (
                self.congestion_control_algorithm)
        if self.congestion_control_options is not None:
            cfg.congestion_control_options = self.congestion_control_options
        if self.flow_control_window is not None:
            cfg.max_data = cfg.max_stream_data = self.flow_control_window
        if self.tx_max_queued_bytes is not None:
            cfg.tx_max_queued_bytes = self.tx_max_queued_bytes
        if self.keep_alive_interval is not None:
            cfg.keep_alive_interval = self.keep_alive_interval
        if self.socket_buffer_size is not None:
            cfg.socket_buffer_size = self.socket_buffer_size
        if self.stream_ring_cap is not None:
            cfg.stream_ring_cap = self.stream_ring_cap
        if self.max_connections is not None:
            cfg.max_connections = self.max_connections

        def wt_factory(transport, state):
            return self._adopt(
                MOQTSessionWTServer(transport, state, session=self))

        async def wt_handler(_session):
            pass  # session lifetime owned by the dispatcher

        # Server preference order, as in serve().
        wt_supported = [f"moqt-{d}" for d in self.supported_drafts
                        if d >= 15] or None
        return serve_dispatch(
            self.host, self.port,
            configuration=cfg,
            create_protocol=lambda *a, **kw: self._adopt(MOQTSessionQuic(
                *a, **kw, session=self)),
            wt_path=self.path or "",
            wt_handler=wt_handler,
            wt_session_factory=wt_factory,
            wt_supported_protocols=wt_supported,
        )

    def _adopt(self, session):
        self._live.add(session)
        if self._refusal is not None:
            self._loop.call_soon(self.refuse, session)
        return session

    def refuse_sessions(self, reason: str = "server shutting down") -> None:
        """Close every session that arrives from now on (NO_ERROR) before
        its setup completes."""
        self._refusal = reason

    def refuse(self, session: Any) -> None:
        """Close `session` (NO_ERROR) unless already closed; a SETUP it
        has in flight no longer completes."""
        if session._moqt_session_closed.done():
            return
        self._closing.add(session)
        session._close_session(SessionCloseCode.NO_ERROR,
                               self._refusal or "server shutting down")

    async def close_sessions(self, timeout: float = 1.0) -> None:
        """Close every live session (NO_ERROR), refuse any that arrive
        from now on, and wait up to `timeout` for these closes to leave.
        Call before closing the serve() handle, which stops the transport
        without telling the peers."""
        if self._refusal is None:
            self._refusal = "server shutting down"
        for s in list(self._live):
            if not s._moqt_session_closed.done():
                self._closing.add(s)
                s.close(SessionCloseCode.NO_ERROR, "server shutting down")
        deadline = self._loop.time() + timeout
        while True:
            left = deadline - self._loop.time()
            pending = [ev for ev in map(_cnx_ended, list(self._closing))
                       if ev is not None and not ev.is_set()]
            if not pending or left <= 0:
                return
            # Short rounds pick up sessions refused meanwhile.
            waits = [asyncio.ensure_future(ev.wait()) for ev in pending]
            _, unfinished = await asyncio.wait(
                waits, timeout=min(left, _CLOSE_POLL_S),
                return_when=asyncio.FIRST_COMPLETED)
            for w in unfinished:
                w.cancel()

    async def closed(self) -> bool:
        if not self._server_closed.done():
            self._server_closed = await self._server_closed
        return True
