
import ssl
from typing import Optional, AsyncContextManager

import certifi
from qh3.quic.configuration import QuicConfiguration
from qh3.asyncio.client import connect
from qh3.h3.connection import H3_ALPN

from .protocol import *
from .types import moqt_alpn_for_version
from .context import set_moqt_ctx_version
from .utils.logger import *

logger = get_logger(__name__)

class MOQTClient(MOQTPeer):  # New connection manager class
    def __init__(
        self,
        host: str,
        port: int,
        endpoint: Optional[str] = None,
        use_quic: Optional[bool] = False,
        verify_tls: Optional[bool] = True,
        allow_optional_dgram: Optional[bool] = False,
        configuration: Optional[QuicConfiguration] = None,
        debug: Optional[bool] = False,
        quic_debug: Optional[bool] = False,
        keylog_filename: Optional[str] = None,
        draft_version: Optional[int] = None,
    ):
        super().__init__(allow_optional_dgram=allow_optional_dgram)
        self.host = host
        self.port = port
        self.endpoint = endpoint
        self.use_quic = use_quic
        self.debug = debug
        self.draft_version = draft_version

        # For raw QUIC, set version context now (ALPN carries the version).
        # For H3/WT, defer until wt-available-protocols negotiation completes.
        if draft_version is not None and use_quic:
            set_moqt_ctx_version(draft_version)

        logger.debug(f"MOQT: client session: {self} use_quic={use_quic} endpoint={endpoint}")

        if configuration is None:
            # Choose ALPN based on draft version
            if use_quic:
                if draft_version is not None:
                    alpn = [moqt_alpn_for_version(draft_version)]
                else:
                    alpn = [MOQT_ALPN]
            else:
                alpn = H3_ALPN
            verify_mode = ssl.CERT_REQUIRED if verify_tls else ssl.CERT_NONE
            configuration = QuicConfiguration(
                alpn_protocols=alpn,
                is_client=True,
                verify_mode=verify_mode,
                cafile=certifi.where() if verify_tls else None,
                max_data=2**24,
                max_stream_data=2**24,
                max_datagram_frame_size=64*1024,
            )
        keylog_file = open(keylog_filename, 'a') if keylog_filename else None
        configuration.secrets_log_file = keylog_file
        configuration.quic_logger = QuicDebugLogger() if quic_debug else None
        self.configuration = configuration

    def connect(self) -> AsyncContextManager[MOQTSession]:
        """Return a context manager that creates MOQTSessionProtocol instance."""
        logger.debug(f"MOQT: session connect: {self}")
        protocol = lambda *args, **kwargs: MOQTSession(*args, **kwargs, session=self)
        return connect(
            self.host,
            self.port,
            configuration=self.configuration,
            create_protocol=protocol
        )
