import ssl
from typing import Any, Optional, Tuple, Coroutine

import asyncio
from asyncio.futures import Future
from qh3.quic.configuration import QuicConfiguration
from qh3.asyncio.server import QuicServer, serve as qh3_serve
from qh3.h3.connection import H3_ALPN

from .protocol import MOQTPeer, MOQTSession, MOQTSessionQuic
from .types import moqt_alpn_for_version, MOQT_ALPN
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
        congestion_control_algorithm: Optional[str] = 'reno',
        configuration: Optional[QuicConfiguration] = None,
        keylog_filename: Optional[str] = None,
        debug: Optional[bool] = False,
        quic_debug: Optional[bool] = False,
    ):
        super().__init__()
        self.host = host
        self.port = port
        self.path = path
        self.use_quic = use_quic
        self.draft_version = draft_version
        self.certificate = certificate
        self.private_key = private_key
        self.debug = debug
        self._loop = asyncio.get_running_loop()
        self._server_closed:Future[Tuple[int,str]] = self._loop.create_future()
        self._next_subscribe_id = 1  # prime subscribe id generator

        keylog_file = open(keylog_filename, 'a') if keylog_filename else None

        if configuration is None:
            if use_quic:
                if draft_version is not None:
                    alpn = [moqt_alpn_for_version(draft_version)]
                else:
                    alpn = [MOQT_ALPN]
            else:
                alpn = H3_ALPN
            configuration = QuicConfiguration(
                is_client=False,
                alpn_protocols=alpn,
                verify_mode=ssl.CERT_NONE,
                certificate=certificate,
                private_key=private_key,
                max_data=2**24,
                max_stream_data=2**24,
                max_datagram_frame_size=64*1024,
                quic_logger=QuicDebugLogger() if quic_debug else None,
                secrets_log_file=keylog_file if keylog_file else None
            )
        # load SSL certificate and key
        configuration.load_cert_chain(certificate, private_key)
        self.configuration = configuration

    def serve(self) -> Coroutine[Any, Any, QuicServer]:
        """Start the MOQT server."""
        logger.info(f"Starting MOQT server on {self.host}:{self.port}")

        if self.use_quic and MOQTSessionQuic is not None:
            # Phase D-partial: raw QUIC via aiopquic
            from aiopquic.asyncio.server import serve as aiopquic_serve
            from aiopquic.quic.configuration import (
                QuicConfiguration as AioPQuicConfiguration,
            )
            cfg = AioPQuicConfiguration(
                alpn_protocols=self.configuration.alpn_protocols,
                is_client=False,
                max_data=self.configuration.max_data,
                max_stream_data=self.configuration.max_stream_data,
                max_datagram_frame_size=self.configuration.max_datagram_frame_size,
            )
            cfg.load_cert_chain(self.certificate, self.private_key)
            protocol = lambda *a, **kw: MOQTSessionQuic(*a, **kw, session=self)
            return aiopquic_serve(
                self.host,
                self.port,
                configuration=cfg,
                create_protocol=protocol,
            )

        protocol = lambda *args, **kwargs: MOQTSession(*args, **kwargs, session=self)
        return qh3_serve(
            self.host,
            self.port,
            configuration=self.configuration,
            create_protocol=protocol,
        )
        
    async def closed(self) -> bool:
        if not self._server_closed.done():
            self._server_closed = await self._server_closed
        return True