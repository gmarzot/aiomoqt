"""TLS cert lookup, and a self-signed localhost cert for loopback tools."""
import os
import shutil
import subprocess
import tempfile
from typing import Optional, Sequence, Tuple

from aiomoqt.utils.logger import get_logger

logger = get_logger(__name__)

SERVER_CERTS_DIR = "/certs"
SERVER_KEY_NAMES = ("priv.key", "key.pem")
REPO_CERTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "certs")
SAN = "DNS:localhost,IP:127.0.0.1,IP:::1"
DAYS = 3650


def user_certs_dir() -> str:
    return os.path.expanduser("~/.local/share/moqt")


def server_cert_dirs() -> Tuple[str, ...]:
    """aiomoqt-relay's search: the /certs container mount first."""
    return (SERVER_CERTS_DIR, REPO_CERTS_DIR, user_certs_dir())


def find_cert(dirs: Optional[Sequence[str]] = None) -> Optional[str]:
    """First existing cert.pem in `dirs`, resolved; default: the source
    tree's certs/, then ~/.local/share/moqt."""
    if dirs is None:
        dirs = (REPO_CERTS_DIR, user_certs_dir())
    for d in dirs:
        cert = os.path.join(d, "cert.pem")
        if os.path.exists(cert):
            return os.path.realpath(cert)
    return None


def find_key(cert: Optional[str],
             names: Sequence[str] = ("key.pem",)) -> Optional[str]:
    """First of `names` that exists beside `cert`."""
    if not cert:
        return None
    for name in names:
        key = os.path.join(os.path.dirname(cert), name)
        if os.path.exists(key):
            return key
    return None


def cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "aiomoqt")


def _openssl(cert: str, key: str) -> bool:
    exe = shutil.which("openssl")
    if not exe:
        return False
    try:
        subprocess.run(
            [exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-days", str(DAYS), "-keyout", key, "-out", cert,
             "-subj", "/CN=localhost", "-addext", f"subjectAltName={SAN}"],
            check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def _cryptography(cert: str, key: str) -> bool:
    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return False
    pkey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    san = x509.SubjectAlternativeName([
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ])
    crt = (x509.CertificateBuilder()
           .subject_name(name).issuer_name(name)
           .public_key(pkey.public_key())
           .serial_number(x509.random_serial_number())
           .not_valid_before(now - datetime.timedelta(minutes=5))
           .not_valid_after(now + datetime.timedelta(days=DAYS))
           .add_extension(san, critical=False)
           .sign(pkey, hashes.SHA256()))
    with open(key, "wb") as f:
        f.write(pkey.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    with open(cert, "wb") as f:
        f.write(crt.public_bytes(serialization.Encoding.PEM))
    return True


def generate_self_signed(cert: str, key: str) -> None:
    """Write a self-signed localhost cert and its key (mode 0600) to the
    given paths: the openssl CLI if present, else the cryptography
    package. Each file is replaced atomically."""
    d = os.path.dirname(os.path.abspath(cert))
    os.makedirs(d, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=d) as tmp:
        tcert = os.path.join(tmp, "cert.pem")
        tkey = os.path.join(tmp, "key.pem")
        if not (_openssl(tcert, tkey) or _cryptography(tcert, tkey)):
            raise RuntimeError(
                "cannot generate a self-signed TLS cert: install the openssl "
                "CLI or the 'cryptography' package, or pass --cert/--key")
        os.chmod(tkey, 0o600)
        os.replace(tkey, key)
        os.replace(tcert, cert)


def ensure_self_signed() -> Tuple[str, str]:
    """(cert, key) for a self-signed localhost cert, generated once and
    reused from $XDG_CACHE_HOME/aiomoqt (default ~/.cache/aiomoqt)."""
    d = cache_dir()
    cert = os.path.join(d, "cert.pem")
    key = os.path.join(d, "key.pem")
    if not (os.path.exists(cert) and os.path.exists(key)):
        os.makedirs(d, mode=0o700, exist_ok=True)
        generate_self_signed(cert, key)
        logger.info(f"generated a self-signed localhost cert: {cert}")
    return cert, key
