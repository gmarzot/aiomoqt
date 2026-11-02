"""A server whose UDP port is taken must fail at serve() rather than log
"Listening" with no socket (the transport thread swallows EADDRINUSE on a
wildcard bind). A named host is left to aiopquic's own bind check."""
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


def _quiet():
    said = []
    patches = (mock.patch.object(server.logger, "info",
                                 lambda m, *a: said.append(m)),
               mock.patch.object(server.logger, "warning",
                                 lambda m, *a: said.append(m)))
    return said, patches


def test_busy_port_raises_and_free_port_passes():
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("0.0.0.0", 0))
    port = holder.getsockname()[1]
    try:
        with pytest.raises(OSError, match=f"UDP port {port} unavailable"):
            _check_udp_port_free(None, port)
    finally:
        holder.close()
    _check_udp_port_free("0.0.0.0", port)   # released: no raise


def test_port_zero_is_never_probed():
    _check_udp_port_free(None, 0)


@pytest.mark.parametrize("host", [
    "localhost", "127.0.0.1", "::1", "10.0.0.5", "host.invalid"])
def test_a_named_host_is_left_to_the_transport(host):
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    said, (p1, p2) = _quiet()
    try:
        with p1, p2:
            _check_udp_port_free(host, port)   # no probe, no notice
    finally:
        holder.close()
    assert said == []


def test_wildcard_bind_says_nothing():
    said, (p1, p2) = _quiet()
    with p1, p2:
        for host in (None, "", "::", "0.0.0.0"):
            _check_udp_port_free(host, _free_port())
    assert said == []


def test_an_ipv6_side_conflict_warns_only_when_ipv6_is_wanted():
    try:
        holder = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        holder.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        holder.bind(("::", 0))
    except OSError:
        pytest.skip("no IPv6 on this host")
    port = holder.getsockname()[1]
    said = []
    try:
        with mock.patch.object(server.logger, "warning",
                               lambda m, *a: said.append(m)):
            _check_udp_port_free(None, port)        # IPv4 free: no raise
            assert said == [f"MOQT server: UDP [::]:{port} is in use; IPv6 "
                            f"clients cannot reach this server"]
            said.clear()
            _check_udp_port_free("0.0.0.0", port)   # IPv4 only
            assert said == []
    finally:
        holder.close()
