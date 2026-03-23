import ssl
from typing import Any, Optional, Tuple, Coroutine

import asyncio
from asyncio.futures import Future
from qh3.quic.configuration import QuicConfiguration
from qh3.asyncio.server import QuicServer, serve
from qh3.h3.connection import H3_ALPN

from .protocol import MOQTPeer, MOQTSession
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
        endpoint: Optional[str] = "moq",
        congestion_control_algorithm: Optional[str] = 'reno',
        configuration: Optional[QuicConfiguration] = None,
        keylog_filename: Optional[str] = None,
        debug: Optional[bool] = False,
        quic_debug: Optional[bool] = False,
    ):
        super().__init__()
        self.host = host
        self.port = port
        self.endpoint = endpoint
        self.debug = debug
        self._loop = asyncio.get_running_loop()
        self._server_closed:Future[Tuple[int,str]] = self._loop.create_future()
        self._next_subscribe_id = 1  # prime subscribe id generator

        keylog_file = open(keylog_filename, 'a') if keylog_filename else None

        if configuration is None:
            configuration = QuicConfiguration(
                is_client=False,
                alpn_protocols=H3_ALPN,
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
        protocol = lambda *args, **kwargs: MOQTSession(*args, **kwargs, session=self)
        return serve(
            self.host,
            self.port,
            configuration=self.configuration,
            create_protocol=protocol,
        )
        
    async def closed(self) -> bool:
        if not self._server_closed.done():
            self._server_closed = await self._server_closed
        return True