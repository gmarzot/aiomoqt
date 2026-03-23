"""MoQT URL parsing utility.

Scheme determines transport:
  moqt://host:port           -> raw QUIC (ALPN: moq-00)
  https://host:port/endpoint -> H3/WebTransport

Default ports: moqt:// -> 4443, https:// -> 4433
"""
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


MOQT_DEFAULT_PORT = 4443
HTTPS_DEFAULT_PORT = 4433


@dataclass
class MOQTRelay:
    """Parsed MoQT relay connection parameters."""
    host: str
    port: int
    use_quic: bool  # True = raw QUIC, False = H3/WebTransport
    endpoint: Optional[str]  # WebTransport endpoint path (None for raw QUIC)

    def __str__(self):
        if self.use_quic:
            return f"moqt://{self.host}:{self.port}"
        ep = f"/{self.endpoint}" if self.endpoint else ""
        return f"https://{self.host}:{self.port}{ep}"

    @property
    def transport_name(self) -> str:
        return "QUIC" if self.use_quic else "WebTransport"


def parse_relay_url(url: str, force_quic: bool = False,
                    default_endpoint: str = "moq") -> MOQTRelay:
    """Parse a relay URL into connection parameters.

    Args:
        url: Relay URL or bare hostname. Supported forms:
            - "moqt://host:port"         -> raw QUIC
            - "https://host:port/path"   -> H3/WebTransport
            - "host:port"               -> H3/WebTransport (default)
            - "host"                    -> H3/WebTransport, default port
        force_quic: Override to use raw QUIC even for https:// URLs.
        default_endpoint: WebTransport endpoint when not in URL (default: "moq").

    Returns:
        MOQTRelay with parsed connection parameters.
    """
    # Bare hostname or host:port (no scheme)
    if "://" not in url:
        # Check for host:port
        if ":" in url:
            host, port_str = url.rsplit(":", 1)
            try:
                port = int(port_str)
                return MOQTRelay(
                    host=host,
                    port=port,
                    use_quic=force_quic,
                    endpoint=None if force_quic else default_endpoint,
                )
            except ValueError:
                pass
        # Plain hostname
        return MOQTRelay(
            host=url,
            port=MOQT_DEFAULT_PORT if force_quic else HTTPS_DEFAULT_PORT,
            use_quic=force_quic,
            endpoint=None if force_quic else default_endpoint,
        )

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme == "moqt":
        return MOQTRelay(
            host=parsed.hostname or "localhost",
            port=parsed.port or MOQT_DEFAULT_PORT,
            use_quic=True,
            endpoint=None,
        )
    elif scheme == "https":
        endpoint = parsed.path.strip("/") or default_endpoint
        return MOQTRelay(
            host=parsed.hostname or "localhost",
            port=parsed.port or HTTPS_DEFAULT_PORT,
            use_quic=force_quic,
            endpoint=None if force_quic else endpoint,
        )
    else:
        raise ValueError(f"unsupported scheme: {scheme}:// (use moqt:// or https://)")
