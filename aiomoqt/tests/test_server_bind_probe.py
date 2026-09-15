"""A server whose UDP port is taken must fail at serve() rather than log
"Listening" with no socket (the transport thread swallows EADDRINUSE),
and the widened-bind notice is a warning only for a named interface."""
import socket
from unittest import mock

import pytest

from aiomoqt import server
from aiomoqt.server import _check_udp_port_free


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_busy_port_raises_and_free_port_passes():
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    try:
        with pytest.raises(OSError, match=f"UDP port {port} unavailable"):
            _check_udp_port_free("127.0.0.1", port)
    finally:
        holder.close()
    _check_udp_port_free("0.0.0.0", port)   # released: no raise


def test_port_zero_is_never_probed():
    _check_udp_port_free("127.0.0.1", 0)


@pytest.mark.parametrize("host, loopback", [
    ("localhost", True), ("127.0.0.1", True), ("127.0.0.53", True),
    ("::1", True), ("10.0.0.5", False), ("stingray.local", False),
])
def test_widened_bind_warns_only_for_a_named_interface(host, loopback):
    # The transport binds every interface whatever we ask for. Saying so
    # is a warning only when the caller named an interface; on loopback
    # it is routine and must not shout on every local run.
    said = []
    with mock.patch.object(server.logger, "info",
                           lambda m, *a: said.append(("info", m))), \
         mock.patch.object(server.logger, "warning",
                           lambda m, *a: said.append(("warning", m))):
        _check_udp_port_free(host, _free_port())
    assert [lvl for lvl, _ in said] == ["info" if loopback else "warning"]
    assert host in said[0][1] and "0.0.0.0" in said[0][1]


def test_wildcard_bind_says_nothing():
    said = []
    with mock.patch.object(server.logger, "info",
                           lambda m, *a: said.append(m)), \
         mock.patch.object(server.logger, "warning",
                           lambda m, *a: said.append(m)):
        _check_udp_port_free("0.0.0.0", _free_port())
        _check_udp_port_free("", _free_port())
    assert said == []
