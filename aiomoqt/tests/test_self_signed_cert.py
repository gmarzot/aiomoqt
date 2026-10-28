"""Self-signed cert generation, and the tools that use or refuse it."""
import os
import re
import subprocess
import sys

import pytest

from aiomoqt.tests._procs import ENV, REPO, free_udp_port, objects_received
from aiomoqt.utils import certs


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Empty HOME and XDG_CACHE_HOME; returns the expected cache dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "aiomoqt"


def _sans(cert):
    from cryptography import x509
    crt = x509.load_pem_x509_certificate(open(cert, "rb").read())
    ext = crt.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    return ({str(n.value) for n in ext.value if isinstance(n, x509.DNSName)}
            | {str(n.value) for n in ext.value
               if isinstance(n, x509.IPAddress)})


def _check_pair(cert, key):
    assert os.stat(key).st_mode & 0o777 == 0o600
    pytest.importorskip("cryptography")
    assert _sans(cert) == {"localhost", "127.0.0.1", "::1"}


def test_ensure_self_signed_generates_once(fresh_home):
    cert, key = certs.ensure_self_signed()
    assert os.path.dirname(cert) == str(fresh_home)
    assert os.path.dirname(key) == str(fresh_home)
    mtimes = (os.stat(cert).st_mtime_ns, os.stat(key).st_mtime_ns)
    assert certs.ensure_self_signed() == (cert, key)
    assert (os.stat(cert).st_mtime_ns, os.stat(key).st_mtime_ns) == mtimes
    _check_pair(cert, key)


def test_cache_defaults_to_home(fresh_home, monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert certs.cache_dir() == os.path.join(os.environ["HOME"], ".cache",
                                             "aiomoqt")


def test_cryptography_fallback_without_openssl(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert certs.shutil.which("openssl") is None
    cert, key = str(tmp_path / "c.pem"), str(tmp_path / "k.pem")
    certs.generate_self_signed(cert, key)
    _check_pair(cert, key)


def test_no_generator_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setitem(sys.modules, "cryptography", None)
    with pytest.raises(RuntimeError, match="openssl.*cryptography"):
        certs.generate_self_signed(str(tmp_path / "c.pem"),
                                   str(tmp_path / "k.pem"))
    assert not os.path.exists(tmp_path / "c.pem")


def _run_fresh(tool, *args, timeout=60):
    """Run a tool with no cert in the source tree or the default dirs."""
    if os.path.exists(os.path.join(certs.SERVER_CERTS_DIR, "cert.pem")):
        pytest.skip("host has /certs/cert.pem")
    launcher = ("import sys, runpy\n"
                "import aiomoqt.utils.certs as c\n"
                "c.REPO_CERTS_DIR = '/nonexistent'\n"
                f"sys.argv = {[tool, *map(str, args)]!r}\n"
                f"runpy.run_module('aiomoqt.tools.{tool}', "
                "run_name='__main__', alter_sys=True)\n")
    env = {**ENV, "HOME": os.environ["HOME"],
           "XDG_CACHE_HOME": os.environ["XDG_CACHE_HOME"]}
    p = subprocess.run([sys.executable, "-c", launcher], capture_output=True,
                       text=True, timeout=timeout, cwd=os.environ["HOME"],
                       env=env)
    return p.returncode, p.stdout + p.stderr


@pytest.mark.parametrize("transport", ["-Q", "-W"])
def test_loopback_bench_makes_its_own_cert(fresh_home, transport):
    rc, out = _run_fresh("loopback_bench", transport, "-p", free_udp_port(),
                         "-t", 2, "-s", 1200, "--log-level", "info")
    assert rc == 0, out
    assert objects_received(out) > 0, out
    assert f"self-signed localhost cert: {fresh_home}/cert.pem" in out, out
    mtime = os.stat(fresh_home / "cert.pem").st_mtime_ns
    rc, out = _run_fresh("loopback_bench", transport, "-p", free_udp_port(),
                         "-t", 2, "-s", 1200, "--log-level", "info")
    assert rc == 0, out
    assert objects_received(out) > 0, out
    assert "self-signed" not in out, out
    assert os.stat(fresh_home / "cert.pem").st_mtime_ns == mtime


def test_adaptive_bench_loopback_makes_its_own_cert(fresh_home):
    rc, out = _run_fresh("adaptive_bench", "--loopback-port",
                         free_udp_port(), "--interval", 1, "-t", 3)
    assert rc == 0, out
    m = re.search(r"High-water:\s+([\d.]+)(\w*bps)", out)
    assert m and float(m.group(1)) > 0, out
    assert (fresh_home / "cert.pem").exists()


@pytest.mark.parametrize("tool", ["moq_interop_relay", "moqtest_origin",
                                  "pub_server"])
def test_servers_do_not_self_sign(fresh_home, tool):
    rc, out = _run_fresh(tool, "--port", free_udp_port(), timeout=30)
    if tool != "pub_server":
        assert rc != 0, out
    assert re.search(r"TLS (cert/key|certificate) required", out), out
    assert not fresh_home.exists()


def test_find_cert_and_key(tmp_path):
    d = tmp_path / "certs"
    d.mkdir()
    (d / "cert.pem").write_text("")
    (d / "priv.key").write_text("")
    assert certs.find_cert([str(tmp_path / "none"), str(d)]) == str(
        (d / "cert.pem").resolve())
    assert certs.find_key(str(d / "cert.pem")) is None
    assert certs.find_key(str(d / "cert.pem"), certs.SERVER_KEY_NAMES) == str(
        d / "priv.key")


def test_source_tree_dir_is_the_repo_certs():
    assert os.path.realpath(certs.REPO_CERTS_DIR) == str(REPO / "certs")
