"""MoQT URL parsing utility.

Scheme determines transport:
  moqt://host:port           -> raw QUIC (ALPN: moq-00)
  https://host:port/path     -> H3/WebTransport

Default ports: moqt:// -> 443, https:// -> 443
"""
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


MOQT_DEFAULT_PORT = 443
HTTPS_DEFAULT_PORT = 443


def normalize_wt_path(path: Optional[str]) -> Optional[str]:
    """Normalize a WT :path: strip trailing '/', ensure leading '/'."""
    if not path:
        return path
    path = path.rstrip("/")
    return path if path.startswith("/") else "/" + path


def moqt_setup_path(path: Optional[str]) -> str:
    """SETUP PATH for native QUIC (§10.3.1.2): the URI's path-abempty,
    plus "?query" if any. A bare segment gains its leading '/'."""
    path = path or ""
    if path and path[0] not in "/?":
        path = "/" + path
    return path


@dataclass
class MOQTRelay:
    """Parsed MoQT relay connection parameters."""
    host: str
    port: int
    use_quic: bool  # True = raw QUIC, False = H3/WebTransport
    # URL path: SETUP PATH on raw QUIC (path and query), :path on WT.
    path: Optional[str]

    def __str__(self):
        if self.use_quic:
            port_s = "" if self.port == MOQT_DEFAULT_PORT else f":{self.port}"
            return f"moqt://{self.host}{port_s}{self.path or ''}"
        # self.path already starts with "/" when present; don't double-add
        ep = self.path if self.path else ""
        port_s = "" if self.port == HTTPS_DEFAULT_PORT else f":{self.port}"
        return f"https://{self.host}{port_s}{ep}"

    @property
    def transport_name(self) -> str:
        return "QUIC" if self.use_quic else "WebTransport"


def parse_relay_url(url: str, force_quic: bool = False,
                    default_path: str = "") -> MOQTRelay:
    """Parse a relay URL into connection parameters.

    Args:
        url: Relay URL or bare hostname. Supported forms:
            - "moqt://host:port"         -> raw QUIC
            - "https://host:port/path"   -> H3/WebTransport
            - "host:port"               -> H3/WebTransport (default)
            - "host"                    -> H3/WebTransport, default port
        force_quic: Override to use raw QUIC even for https:// URLs.
        default_path: MoQT path when not in URL.
            Defaults to "" (= root path "/"). Callers that need a
            specific path (e.g. "moq-relay") must pass it explicitly.

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
                    path=None if force_quic else default_path,
                )
            except ValueError:
                pass
        # Plain hostname
        return MOQTRelay(
            host=url,
            port=MOQT_DEFAULT_PORT if force_quic else HTTPS_DEFAULT_PORT,
            use_quic=force_quic,
            path=None if force_quic else default_path,
        )

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme in ("moqt", "https") and not parsed.hostname:
        # MoQT §3.1.1 and RFC 9110 §4.2.2: the host MUST NOT be empty.
        raise ValueError(f"{url}: empty host")

    if scheme == "moqt":
        query = f"?{parsed.query}" if parsed.query else ""
        return MOQTRelay(
            host=parsed.hostname,
            port=parsed.port or MOQT_DEFAULT_PORT,
            use_quic=True,
            path=parsed.path + query,
        )
    elif scheme == "https":
        path = normalize_wt_path(parsed.path or default_path)
        return MOQTRelay(
            host=parsed.hostname,
            port=parsed.port or HTTPS_DEFAULT_PORT,
            use_quic=force_quic,
            path=None if force_quic else path,
        )
    else:
        raise ValueError(f"unsupported scheme: {scheme}:// (use moqt:// or https://)")
