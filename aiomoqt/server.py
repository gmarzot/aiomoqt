import asyncio
from asyncio.futures import Future
from typing import Any, List, Optional, Tuple, Coroutine

from aiopquic.asyncio.server import serve as aiopquic_serve
from aiopquic.asyncio.webtransport import serve_webtransport
from aiopquic.quic.configuration import QuicConfiguration

from .protocol import (DEFAULT_TX_MAX_INFLIGHT_BYTES, MOQTPeer,
                        MOQTSessionQuic, MOQTSessionWTServer)
from .types import (moqt_alpn_for_version, moqt_version_from_draft,
                    MOQTDraft)
from .utils.logger import *

logger = get_logger(__name__)


class MOQTServer(MOQTPeer):
    """Server-side session manager."""
    def __init__(
        self,
        host: str,
        port: int,
        certificate: str,
        private_key: str,
        path: Optional[str] = None,
        use_quic: Optional[bool] = False,
        draft_version: Optional[int] = None,
        supported_drafts: Optional[List[int]] = None,
        debug: Optional[bool] = False,
        tx_max_inflight_bytes: Optional[int] = DEFAULT_TX_MAX_INFLIGHT_BYTES,
        tx_max_queued_bytes: Optional[int] = None,
        congestion_control_algorithm: Optional[str] = None,
        keep_alive_interval: Optional[float] = None,
        socket_buffer_size: Optional[int] = None,
    ):
        super().__init__(tx_max_inflight_bytes=tx_max_inflight_bytes)
        self.host = host
        self.port = port
        self.path = path
        self.use_quic = use_quic
        # Two-attribute discipline (symmetric with MOQTClient): drafts
        # as short ints. A single-draft server is the Phase 0 scope;
        # selecting among multiple client-offered drafts is a fast-follow
        # that needs the aiopquic server-side ALPN/WT-protocol selector.
        if draft_version is not None:
            moqt_version_from_draft(draft_version)  # validate
            self.supported_drafts = (draft_version,)
        elif supported_drafts is not None:
            for _d in supported_drafts:
                moqt_version_from_draft(_d)  # validate each
            self.supported_drafts = tuple(supported_drafts)
        else:
            # d18 is beta: opt in explicitly (draft_version=18, or
            # supported_drafts=[18, 16, 14]). The no-args default offers the
            # stable set only, so an auto session never negotiates onto the
            # beta d18 wire; d14 stays in the offer for d14-only peers.
            self.supported_drafts = (
                MOQTDraft.DRAFT_16, MOQTDraft.DRAFT_14)
        self.draft_version = draft_version
        self.certificate = certificate
        self.private_key = private_key
        self.debug = debug
        # Aggregate TX gate budget (QuicConfiguration.tx_max_queued_bytes):
        # bounds publisher run-ahead across ALL streams; steady-state
        # added latency ≈ value / drain rate. None = honor the aiopquic
        # default (4 MiB); 0 = disable.
        self.tx_max_queued_bytes = tx_max_queued_bytes
        self.congestion_control_algorithm = congestion_control_algorithm
        # QUIC keep-alive interval (seconds). None = disabled. PING
        # frames hold a flow-controlled, consumer-stalled connection
        # open past the idle timeout instead of dropping it.
        self.keep_alive_interval = keep_alive_interval
        # UDP SO_RCVBUF/SO_SNDBUF request (bytes). None = aiopquic
        # default (64 MiB, kernel-clamped to rmem_max/wmem_max).
        self.socket_buffer_size = socket_buffer_size
        self._loop = asyncio.get_running_loop()
        self._server_closed: Future[Tuple[int, str]] = self._loop.create_future()
        self._next_subscribe_id = 1

    def serve(self) -> Coroutine[Any, Any, Any]:
        """Start the MOQT server."""
        logger.info(f"Starting MOQT server on {self.host}:{self.port}")

        if self.use_quic:
            alpn = [moqt_alpn_for_version(d)
                    for d in sorted(self.supported_drafts, reverse=True)]
            cfg = QuicConfiguration(
                alpn_protocols=alpn, is_client=False,
                max_data=2**24, max_stream_data=2**24,
                max_datagram_frame_size=64 * 1024,
            )
            # None defers to the aiopquic default (bbr1) — loss-based
            # CCs collapse on GIL-induced loss blips and can't recover.
            if self.congestion_control_algorithm is not None:
                cfg.congestion_control_algorithm = (
                    self.congestion_control_algorithm)
            if self.tx_max_queued_bytes is not None:
                cfg.tx_max_queued_bytes = self.tx_max_queued_bytes
            if self.keep_alive_interval is not None:
                cfg.keep_alive_interval = self.keep_alive_interval
            if self.socket_buffer_size is not None:
                cfg.socket_buffer_size = self.socket_buffer_size
            cfg.load_cert_chain(self.certificate, self.private_key)
            protocol = lambda *a, **kw: MOQTSessionQuic(*a, **kw, session=self)
            return aiopquic_serve(
                self.host, self.port,
                configuration=cfg,
                create_protocol=protocol,
            )

        # WebTransport mode (use_quic=False): serve_webtransport with
        # MOQTSessionWTServer as the session factory.
        def factory(transport, state):
            return MOQTSessionWTServer(transport, state, session=self)

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
        if self.tx_max_queued_bytes is not None:
            wt_cfg.tx_max_queued_bytes = self.tx_max_queued_bytes
        if self.keep_alive_interval is not None:
            wt_cfg.keep_alive_interval = self.keep_alive_interval
        if self.socket_buffer_size is not None:
            wt_cfg.socket_buffer_size = self.socket_buffer_size

        # Advertise the supported drafts as WT subprotocols (newest first)
        # so picowt selects the highest mutual against the client's
        # WT-Available-Protocols. Drafts >= 15 negotiate the version this
        # way ("moqt-NN"); d14 uses the legacy in-band CLIENT_SETUP path.
        wt_supported = [f"moqt-{d}"
                        for d in sorted(self.supported_drafts, reverse=True)
                        if d >= 15] or None
        return serve_webtransport(
            self.host, self.port, self.path or "",
            handler=handler,
            cert_file=self.certificate, key_file=self.private_key,
            session_factory=factory,
            configuration=wt_cfg,
            wt_supported_protocols=wt_supported,
        )

    async def closed(self) -> bool:
        if not self._server_closed.done():
            self._server_closed = await self._server_closed
        return True
