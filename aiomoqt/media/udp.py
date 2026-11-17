"""MPEG-TS over UDP for pub_media --ts and sub_media --ts-out.

`udp://[@][HOST]:PORT` receives: it binds HOST, or every address when
HOST is empty, and joins HOST when it is an IPv4 multicast group.
`udp://HOST:PORT` sends there, at most seven packets a datagram (1316
bytes for 188-byte packets, as SRT and RIST carry them).
"""
from __future__ import annotations

import asyncio
import ipaddress
import select
import socket
import struct
from typing import Optional, Tuple
from urllib.parse import urlsplit

DATAGRAM_PACKETS = 7
_RECV_BYTES = 65536
_RCVBUF = 8 << 20
# How often a blocking read checks whether the input was closed.
_POLL_S = 0.25


def is_udp_url(spec: Optional[str]) -> bool:
    return bool(spec) and spec.startswith('udp://')


def parse_udp_url(url: str, *, listen: bool) -> Tuple[str, int]:
    """(host, port) of a udp:// URL; raise ValueError for one that is
    not `udp://[@][HOST]:PORT`, or names no HOST to send to."""
    u = urlsplit(url)
    if u.scheme != 'udp' or u.path not in ('', '/') or u.query or u.fragment:
        raise ValueError(f"{url!r}: expected udp://[@][HOST]:PORT")
    if u.password is not None or u.username not in (None, ''):
        raise ValueError(f"{url!r}: expected udp://[@][HOST]:PORT")
    try:
        port = u.port
    except ValueError as e:
        raise ValueError(f"{url!r}: {e}") from None
    if port is None or port == 0 and not listen:
        raise ValueError(f"{url!r}: no port")
    host = u.hostname or ''
    if not listen and (not host or u.username is not None):
        raise ValueError(f"{url!r}: expected udp://HOST:PORT")
    return host, port


def _multicast(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_multicast
    except ValueError:
        return False


class UdpInput:
    """A TS byte stream from UDP datagrams. read1() blocks for the next
    datagrams and returns every one queued (b'' once closed); read()
    waits up to `timeout` s on the event loop (None on timeout), its
    reader registered once and drained into a queue as datagrams land."""

    def __init__(self, url: str):
        host, port = parse_udp_url(url, listen=True)
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _RCVBUF)
            except OSError:
                pass
            group = _multicast(host)
            sock.bind(('' if group else host, port))
            if group:
                if family != socket.AF_INET:
                    raise ValueError(f"{url!r}: IPv6 multicast is not "
                                     f"supported")
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                struct.pack('4s4s', socket.inet_aton(host),
                                            socket.inet_aton('0.0.0.0')))
            sock.setblocking(False)
        except BaseException:
            sock.close()
            raise
        self.sock = sock
        self.address = sock.getsockname()
        self.closed = False
        self._queue: list = []
        self._queued = 0
        # read() holds at most what the socket itself would.
        self._cap = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        self._waiter: Optional[asyncio.Future] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _drain(self) -> bytes:
        parts = []
        size = 0
        while size < _RECV_BYTES:
            try:
                data = self.sock.recv(_RECV_BYTES)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                if self.closed:
                    break
                raise
            parts.append(data)
            size += len(data)
        return b''.join(parts)

    def read1(self, n: int = _RECV_BYTES) -> bytes:
        while not self.closed:
            try:
                ready, _, _ = select.select([self.sock], [], [], _POLL_S)
            except (OSError, ValueError):
                break
            if ready:
                data = self._drain()
                if data:
                    return data
        return b''

    def _readable(self) -> None:
        data = self._drain()
        if data:
            self._queue.append(data)
            self._queued += len(data)
            if self._queued > self._cap:
                # The reader is behind: leave the rest to the socket.
                self._unregister()
        self._wake()

    def _wake(self) -> None:
        w = self._waiter
        if w is not None and not w.done():
            w.set_result(None)

    def _unregister(self) -> None:
        loop, self._loop = self._loop, None
        if loop is not None and not loop.is_closed():
            loop.remove_reader(self.sock.fileno())

    def _take(self) -> bytes:
        data = b''.join(self._queue)
        self._queue.clear()
        self._queued = 0
        return data

    async def read(self, timeout: Optional[float] = None) -> Optional[bytes]:
        if self.closed:
            return b''
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
            self._loop.add_reader(self.sock.fileno(), self._readable)
        if self._queue:
            return self._take()
        self._waiter = self._loop.create_future()
        expiry = (None if timeout is None
                  else self._loop.call_later(timeout, self._wake))
        try:
            await self._waiter
        finally:
            self._waiter = None
            if expiry is not None:
                expiry.cancel()
        if self._queue:
            return self._take()
        return b'' if self.closed else None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._unregister()
            self.sock.close()
            self._wake()


class UdpOutput:
    """TS packets to `udp://HOST:PORT`, a datagram per seven packets of
    each write (fewer at its end). Unconnected, so a receiver that is not
    there yet costs nothing."""

    def __init__(self, url: str, packet_size: int = 188):
        host, port = parse_udp_url(url, listen=False)
        info = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
        self.sock = socket.socket(info[0], socket.SOCK_DGRAM)
        self._addr = info[4]
        self._step = DATAGRAM_PACKETS * packet_size
        self.dropped = 0

    def write(self, payload) -> None:
        step = self._step
        for i in range(0, len(payload), step):
            try:
                self.sock.sendto(payload[i:i + step], self._addr)
            except OSError:
                self.dropped += 1

    def close(self) -> None:
        self.sock.close()
