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
        draft_version: Optional[int] = None,
        libquicr_compat: Optional[bool] = False,
    ):
        super().__init__(allow_optional_dgram=allow_optional_dgram,
                         libquicr_compat=libquicr_compat)
        self.host = host
        self.port = port
        self.path = path
        self.use_quic = use_quic
        self.verify_tls = verify_tls
        self.debug = debug
        self.draft_version = draft_version
        self.keylog_filename = keylog_filename
        self.configuration = configuration

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
                )
            protocol = lambda *a, **kw: MOQTSessionQuic(*a, **kw, session=self)
            return aiopquic_connect(
                self.host, self.port,
                configuration=cfg,
                create_protocol=protocol,
            )

        return self._connect_wt()

    @asynccontextmanager
    async def _connect_wt(self):
        transport = TransportContext()
        transport.start(
            is_client=True, alpn="h3",
            max_datagram_frame_size=64 * 1024,
        )
        session = MOQTSessionWTClient(
            transport,
            self.host, self.port, self.path or "",
            sni=self.host,
            session=self,
        )
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
