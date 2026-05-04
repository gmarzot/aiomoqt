import asyncio
from asyncio.futures import Future
from typing import Any, Optional, Tuple, Coroutine

from aiopquic.asyncio.server import serve as aiopquic_serve
from aiopquic.asyncio.webtransport import serve_webtransport
from aiopquic.quic.configuration import QuicConfiguration

from .protocol import MOQTPeer, MOQTSessionQuic, MOQTSessionWTServer
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
        debug: Optional[bool] = False,
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
        self._server_closed: Future[Tuple[int, str]] = self._loop.create_future()
        self._next_subscribe_id = 1

    def serve(self) -> Coroutine[Any, Any, Any]:
        """Start the MOQT server."""
        logger.info(f"Starting MOQT server on {self.host}:{self.port}")

        if self.use_quic:
            if self.draft_version is not None:
                alpn = [moqt_alpn_for_version(self.draft_version)]
            else:
                alpn = [MOQT_ALPN]
            cfg = QuicConfiguration(
                alpn_protocols=alpn, is_client=False,
                max_data=2**24, max_stream_data=2**24,
                max_datagram_frame_size=64 * 1024,
            )
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

        return serve_webtransport(
            self.host, self.port, self.path or "",
            handler=handler,
            cert_file=self.certificate, key_file=self.private_key,
            session_factory=factory,
        )

    async def closed(self) -> bool:
        if not self._server_closed.done():
            self._server_closed = await self._server_closed
        return True
