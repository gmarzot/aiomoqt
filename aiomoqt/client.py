from contextlib import asynccontextmanager
from typing import List, Optional, Union

from aiopquic.asyncio.client import connect as aiopquic_connect
from aiopquic._binding._transport import TransportContext
from aiopquic.quic.configuration import QuicConfiguration

from .protocol import (
    MOQTPeer, MOQTSessionQuic, MOQTSessionWTClient,
    DEFAULT_TX_MAX_INFLIGHT_BYTES,
)
from .types import moqt_alpn_for_version, normalize_supported_drafts
from .context import set_moqt_ctx_version
from .utils.logger import *

logger = get_logger(__name__)


class MOQTClient(MOQTPeer):
    def __init__(
        self,
        host: str,
        port: int,
        path: Optional[str] = None,
        use_quic: Optional[bool] = False,
        verify_tls: Optional[bool] = True,
        allow_optional_dgram: Optional[bool] = False,
        configuration: Optional[QuicConfiguration] = None,
        debug: Optional[bool] = False,
        keylog_filename: Optional[str] = None,
        quic_debug_log: Optional[str] = None,
        supported_drafts: Optional[Union[int, List[int]]] = None,
        libquicr_compat: Optional[bool] = False,
        congestion_control_algorithm: Optional[str] = None,
        tx_max_inflight_bytes: Optional[int] = DEFAULT_TX_MAX_INFLIGHT_BYTES,
        tx_max_queued_bytes: Optional[int] = None,
        keep_alive_interval: Optional[float] = None,
        socket_buffer_size: Optional[int] = None,
    ):
        super().__init__(allow_optional_dgram=allow_optional_dgram,
                         libquicr_compat=libquicr_compat,
                         tx_max_inflight_bytes=tx_max_inflight_bytes)
        from .utils.url import normalize_wt_path
        self.host = host
        self.port = port
        self.path = normalize_wt_path(path)
        self.use_quic = use_quic
        self.verify_tls = verify_tls
        self.debug = debug
        # Path to picoquic text log file. When set, aiopquic enables
        # picoquic_set_log_level(1) + picoquic_set_textlog on the
        # transport so QUIC-layer events (packets, ACKs, RTT, CC) go
        # to that file. Spiritual replacement of qh3-era
        # QuicDebugLogger; only meaningful for the raw QUIC path (and
        # for the WT path's underlying QUIC transport).
        self.quic_debug_log = quic_debug_log
        # Public API contract: supported_drafts is the set of IETF draft
        # numbers (e.g. 16, or [16, 14]) this client offers; None means
        # "offer every supported version" (the auto path). Internal state
        # stores a non-empty list of full IETF version codes (e.g.
        # 0xff000010), newest first — what ALPN encodes and CLIENT_SETUP
        # carries. The normalization is done here at the API boundary so
        # internal code only ever sees wire-version codes. Version
        # negotiation (QUIC ALPN / H3-WT) selects one of these as the
        # session's negotiated_version.
        self.supported_drafts = normalize_supported_drafts(supported_drafts)
        self.keylog_filename = keylog_filename
        self.configuration = configuration
        self.congestion_control_algorithm = congestion_control_algorithm
        # Aggregate TX gate budget (QuicConfiguration.tx_max_queued_bytes):
        # bounds publisher run-ahead across ALL streams; steady-state
        # added latency ≈ value / drain rate. None = honor the aiopquic
        # default (4 MiB); 0 = disable.
        self.tx_max_queued_bytes = tx_max_queued_bytes
        # QUIC keep-alive interval (seconds). None = disabled. Sends
        # PING frames so a flow-controlled connection whose consumer
        # stalls isn't dropped by the idle timeout.
        self.keep_alive_interval = keep_alive_interval
        # UDP SO_RCVBUF/SO_SNDBUF request (bytes). None = aiopquic
        # default (64 MiB, kernel-clamped to rmem_max/wmem_max).
        self.socket_buffer_size = socket_buffer_size

        # A single offered draft pins the encoding context now (matches
        # the historical explicit-draft path). With several drafts (or
        # the auto offer) the context is set from the negotiated version
        # once ALPN / WT negotiation resolves it.
        if len(self.supported_drafts) == 1:
            set_moqt_ctx_version(self.supported_drafts[0])

        logger.debug(
            f"MOQT: client session: {self} use_quic={use_quic} path={path}")

    def connect(self):
        """Return an async context manager that yields a MOQT session.

        Raw QUIC mode (use_quic=True) uses aiopquic.connect.
        WebTransport mode (use_quic=False) uses aiopquic
        connect_webtransport with MOQTSessionWTClient as the session.
        """
        logger.debug(f"MOQT: session connect: {self}")

        if self.use_quic:
            if self.configuration is not None:
                cfg = self.configuration
                if cfg.server_name is None:
                    cfg.server_name = self.host
            else:
                # Offer one ALPN per supported draft, newest first, so a
                # d16-only peer negotiates moqt-16 and a d14 peer falls
                # back to moq-00. The session sets its version from
                # whichever ALPN the peer selects (version-from-ALPN). A
                # single offered draft yields a single ALPN; the auto
                # offer (supported_drafts=None) yields every version —
                # without that, auto offered only moq-00 (d14) and
                # d16-only relays closed the connection (376) before
                # SERVER_SETUP.
                alpn = [moqt_alpn_for_version(v)
                        for v in self.supported_drafts]
                cfg = QuicConfiguration(
                    alpn_protocols=alpn, is_client=True,
                    max_data=2**24, max_stream_data=2**24,
                    max_datagram_frame_size=64 * 1024,
                    server_name=self.host,
                    secrets_log_file=self.keylog_filename,
                )
            # None defers to the aiopquic default (bbr1).
            if self.congestion_control_algorithm is not None:
                cfg.congestion_control_algorithm = (
                    self.congestion_control_algorithm)
            if self.tx_max_queued_bytes is not None:
                cfg.tx_max_queued_bytes = self.tx_max_queued_bytes
            if self.keep_alive_interval is not None:
                cfg.keep_alive_interval = self.keep_alive_interval
            if self.socket_buffer_size is not None:
                cfg.socket_buffer_size = self.socket_buffer_size
            protocol = lambda *a, **kw: MOQTSessionQuic(*a, **kw, session=self)
            # quic_debug_log not wired on raw-QUIC path yet: aiopquic
            # 0.3.1's `connect()` helper doesn't forward debug_log to
            # TransportContext.start. Tracked for aiopquic 0.3.2.
            if self.quic_debug_log is not None:
                logger.warning(
                    "MOQT: quic_debug_log requested for raw-QUIC client; "
                    "not yet plumbed in aiopquic.connect (use WT or wait "
                    "for aiopquic 0.3.2)")
            return aiopquic_connect(
                self.host, self.port,
                configuration=cfg,
                create_protocol=protocol,
            )

        return self._connect_wt()

    @asynccontextmanager
    async def _connect_wt(self):
        # Config source-of-truth: self.configuration when set, else
        # the matching defaults the QUIC branch uses. Everything the
        # connection's behavior depends on must be forwarded here —
        # FC sizing, stream caps, idle timeout, and the CC algorithm
        # all reach picoquic only through transport.start().
        wt_cfg = self.configuration if self.configuration is not None \
            else QuicConfiguration(
                is_client=True,
                max_data=2**24, max_stream_data=2**24,
                max_datagram_frame_size=64 * 1024,
            )
        # None defers to the aiopquic default (bbr1).
        if self.congestion_control_algorithm is not None:
            wt_cfg.congestion_control_algorithm = (
                self.congestion_control_algorithm)
        if self.tx_max_queued_bytes is not None:
            wt_cfg.tx_max_queued_bytes = self.tx_max_queued_bytes
        if wt_cfg.event_ring_capacity is not None:
            transport = TransportContext(
                ring_capacity=wt_cfg.event_ring_capacity)
        else:
            transport = TransportContext()
        transport.start(
            is_client=True, alpn="h3",
            max_datagram_frame_size=64 * 1024,
            debug_log=self.quic_debug_log,
            rx_data_ring_cap=wt_cfg.max_stream_data,
            initial_max_data=wt_cfg.max_data,
            initial_max_streams_uni=wt_cfg.max_streams_uni,
            initial_max_streams_bidi=wt_cfg.max_streams_bidi,
            idle_timeout_ms=int(wt_cfg.idle_timeout * 1000),
            congestion_control_algorithm=(
                wt_cfg.congestion_control_algorithm),
            keep_alive_interval_ms=int(
                (self.keep_alive_interval or 0) * 1000),
            socket_buffer_size=(self.socket_buffer_size or 0),
            qlog_dir=wt_cfg.qlog_dir,
        )
        # MoQT draft over WebTransport: the ALPN is "h3", so unlike raw
        # QUIC it carries no MoQT version — we resolve it here. We pick
        # the newest offered draft (supported_drafts is sorted newest
        # first); for a single offered draft that is that draft, and for
        # the auto offer it is the latest supported draft (matching the
        # historical behavior). Drafts >= 15 advertise the version in
        # WT-Available-Protocols ("moqt-NN", per moq-transport-16 §3.1);
        # d14 uses the legacy in-band CLIENT_SETUP path. Either way the
        # encoding context is set to match so the peer's ServerSetup
        # (and every message after) parses under the right draft.
        wt_draft = self.supported_drafts[0]
        set_moqt_ctx_version(wt_draft)
        wt_protocols = None
        if wt_draft >= 15:
            wt_protocols = [f"moqt-{wt_draft:d}"]
        session = MOQTSessionWTClient(
            transport,
            self.host, self.port, self.path or "",
            sni=self.host,
            wt_available_protocols=wt_protocols,
            session=self,
        )
        # Stamp the resolved MoQT draft: over WT the ALPN is "h3" so the
        # session never learns it from ProtocolNegotiated (as raw QUIC
        # does). Without this the d16 ServerSetup handler reads the d14
        # default from negotiated_draft and reverts the context.
        session.negotiated_draft = wt_draft
        # Stamp the aggregate TX gate budget and per-stream ring cap
        # on the WT session (the client constructs the session
        # directly rather than via connect_webtransport, so the
        # configuration hand-off there doesn't apply).
        session.tx_max_queued_bytes = wt_cfg.tx_max_queued_bytes
        session.stream_ring_cap = wt_cfg.stream_ring_cap
        try:
            await session.open(timeout=10.0)
            yield session
        finally:
            try:
                if not session.session_closed:
                    session.close(0, b"")
            except Exception:
                pass
            try:
                transport.stop()
            except Exception:
                pass
