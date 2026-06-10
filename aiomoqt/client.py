from contextlib import asynccontextmanager
from typing import Optional

from aiopquic.asyncio.client import connect as aiopquic_connect
from aiopquic._binding._transport import TransportContext
from aiopquic.quic.configuration import QuicConfiguration

from .protocol import MOQTPeer, MOQTSessionQuic, MOQTSessionWTClient
from .types import moqt_alpn_for_version, MOQT_ALPN
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
        draft_version: Optional[int] = None,
        libquicr_compat: Optional[bool] = False,
        congestion_control_algorithm: Optional[str] = None,
        tx_max_queued_bytes: Optional[int] = None,
    ):
        super().__init__(allow_optional_dgram=allow_optional_dgram,
                         libquicr_compat=libquicr_compat)
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
        # Public API contract: draft_version is the IETF draft number as
        # an integer (e.g. 14, 16). Internal state stores the full IETF
        # version code (e.g. 0xff000010) — what goes on the wire, what
        # ALPN encodes, what CLIENT_SETUP carries. The conversion is
        # done here at the API boundary so internal code never has to
        # think about which form it's holding.
        if draft_version is not None:
            from .types import moqt_version_from_draft
            draft_version = moqt_version_from_draft(draft_version)
        self.draft_version = draft_version
        self.keylog_filename = keylog_filename
        self.configuration = configuration
        self.congestion_control_algorithm = congestion_control_algorithm
        # Aggregate TX gate budget (QuicConfiguration.tx_max_queued_bytes):
        # bounds publisher run-ahead across ALL streams; steady-state
        # added latency ≈ value / drain rate. None = honor the aiopquic
        # default (8 MiB); 0 = disable.
        self.tx_max_queued_bytes = tx_max_queued_bytes

        if draft_version is not None:
            set_moqt_ctx_version(draft_version)

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
                if self.draft_version is not None:
                    alpn = [moqt_alpn_for_version(self.draft_version)]
                else:
                    alpn = [MOQT_ALPN]
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
            rx_ring_cap=wt_cfg.max_stream_data,
            initial_max_data=wt_cfg.max_data,
            initial_max_streams_uni=wt_cfg.max_streams_uni,
            initial_max_streams_bidi=wt_cfg.max_streams_bidi,
            idle_timeout_ms=int(wt_cfg.idle_timeout * 1000),
            congestion_control_algorithm=(
                wt_cfg.congestion_control_algorithm),
        )
        # MoQT version negotiation over WebTransport (per moq-transport-16
        # §3.1): drafts >= 15 carry the version in WT-Available-Protocols
        # ("moqt-NN") rather than CLIENT_SETUP's versions array. d14 and
        # earlier use the legacy in-band CLIENT_SETUP path and advertise
        # nothing here.
        wt_protocols = None
        if self.draft_version is not None:
            draft_major = self.draft_version & 0xFF
            if draft_major >= 15:
                wt_protocols = [f"moqt-{draft_major:d}"]
        session = MOQTSessionWTClient(
            transport,
            self.host, self.port, self.path or "",
            sni=self.host,
            wt_available_protocols=wt_protocols,
            session=self,
        )
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
