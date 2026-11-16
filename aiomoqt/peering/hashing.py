"""moq-peering track hashes and node ids.

city64 is Google CityHash64 v1.1 with every 32- and 64-bit fetch loaded
big-endian, as moq-peering's cityhash.h does (it matches libquicr), so it
differs from Google's for inputs longer than 3 bytes. The namespace hash
covers the tuples concatenated without delimiters.
"""
import secrets
from typing import NamedTuple, Sequence

_M = (1 << 64) - 1
_K0 = 0xc3a5c85c97cb3127
_K1 = 0xb492b66fbe98f273
_K2 = 0x9ae16a3b2f90404f
_KMUL = 0x9ddfea08eb382d69


def _f64(s: bytes, i: int) -> int:
    return int.from_bytes(s[i:i + 8], "big")


def _f32(s: bytes, i: int) -> int:
    return int.from_bytes(s[i:i + 4], "big")


def _rot(v: int, shift: int) -> int:
    return v if shift == 0 else ((v >> shift) | (v << (64 - shift))) & _M


def _mix(v: int) -> int:
    return v ^ (v >> 47)


def _swap(v: int) -> int:
    return int.from_bytes(v.to_bytes(8, "little"), "big")


def _hash16(u: int, v: int, mul: int = _KMUL) -> int:
    a = _mix(((u ^ v) * mul) & _M)
    b = _mix(((v ^ a) * mul) & _M)
    return (b * mul) & _M


def _len0to16(s: bytes) -> int:
    n = len(s)
    if n >= 8:
        mul = _K2 + n * 2
        a = (_f64(s, 0) + _K2) & _M
        b = _f64(s, n - 8)
        c = (_rot(b, 37) * mul + a) & _M
        d = ((_rot(a, 25) + b) * mul) & _M
        return _hash16(c, d, mul)
    if n >= 4:
        mul = _K2 + n * 2
        a = _f32(s, 0)
        return _hash16(n + (a << 3), _f32(s, n - 4), mul)
    if n > 0:
        y = s[0] + (s[n >> 1] << 8)
        z = n + (s[n - 1] << 2)
        return (_mix(((y * _K2) ^ (z * _K0)) & _M) * _K2) & _M
    return _K2


def _len17to32(s: bytes) -> int:
    n = len(s)
    mul = _K2 + n * 2
    a = (_f64(s, 0) * _K1) & _M
    b = _f64(s, 8)
    c = (_f64(s, n - 8) * mul) & _M
    d = (_f64(s, n - 16) * _K2) & _M
    return _hash16((_rot((a + b) & _M, 43) + _rot(c, 30) + d) & _M,
                   (a + _rot((b + _K2) & _M, 18) + c) & _M, mul)


def _weak32(s: bytes, i: int, a: int, b: int):
    w, x, y, z = _f64(s, i), _f64(s, i + 8), _f64(s, i + 16), _f64(s, i + 24)
    a = (a + w) & _M
    b = _rot((b + a + z) & _M, 21)
    c = a
    a = (a + x + y) & _M
    b = (b + _rot(a, 44)) & _M
    return (a + z) & _M, (b + c) & _M


def _len33to64(s: bytes) -> int:
    n = len(s)
    mul = _K2 + n * 2
    a = (_f64(s, 0) * _K2) & _M
    b = _f64(s, 8)
    c = _f64(s, n - 24)
    d = _f64(s, n - 32)
    e = (_f64(s, 16) * _K2) & _M
    f = (_f64(s, 24) * 9) & _M
    g = _f64(s, n - 8)
    h = (_f64(s, n - 16) * mul) & _M
    u = (_rot((a + g) & _M, 43) + (_rot(b, 30) + c) * 9) & _M
    v = ((((a + g) & _M) ^ d) + f + 1) & _M
    w = (_swap(((u + v) * mul) & _M) + h) & _M
    x = (_rot((e + f) & _M, 42) + c) & _M
    y = ((_swap(((v + w) * mul) & _M) + g) * mul) & _M
    z = (e + f + c) & _M
    a = (_swap(((x + z) * mul + y) & _M) + b) & _M
    b = (_mix(((z + a) * mul + d + h) & _M) * mul) & _M
    return (b + x) & _M


def city64(data: bytes) -> int:
    """moq-peering's CityHash64 of data."""
    s = bytes(data)
    n = len(s)
    if n <= 16:
        return _len0to16(s)
    if n <= 32:
        return _len17to32(s)
    if n <= 64:
        return _len33to64(s)
    x = _f64(s, n - 40)
    y = (_f64(s, n - 16) + _f64(s, n - 56)) & _M
    z = _hash16((_f64(s, n - 48) + n) & _M, _f64(s, n - 24))
    v = _weak32(s, n - 64, n, z)
    w = _weak32(s, n - 32, (y + _K1) & _M, x)
    x = (x * _K1 + _f64(s, 0)) & _M
    remaining = (n - 1) & ~63
    i = 0
    while True:
        x = (_rot((x + y + v[0] + _f64(s, i + 8)) & _M, 37) * _K1) & _M
        y = (_rot((y + v[1] + _f64(s, i + 48)) & _M, 42) * _K1) & _M
        x ^= w[1]
        y = (y + v[0] + _f64(s, i + 40)) & _M
        z = (_rot((z + w[0]) & _M, 33) * _K1) & _M
        v = _weak32(s, i, (v[1] * _K1) & _M, (x + w[0]) & _M)
        w = _weak32(s, i + 32, (z + w[1]) & _M, (y + _f64(s, i + 16)) & _M)
        z, x = x, z
        i += 64
        remaining -= 64
        if remaining == 0:
            break
    return _hash16((_hash16(v[0], w[0]) + _mix(y) * _K1 + z) & _M,
                   (_hash16(v[1], w[1]) + x) & _M)


def hash_combine(seed: int, value: int) -> int:
    return seed ^ ((value + 0x9e3779b9 + (seed << 6) + (value >> 2)) & _M)


class TrackHashes(NamedTuple):
    namespace: int
    name: int
    fullname: int


def track_hashes(namespace: Sequence[bytes], name: bytes = b"") -> TrackHashes:
    """The three hashes moq-peering carries for a track; a namespace
    announce has an empty name."""
    ns = city64(b"".join(bytes(t) for t in namespace))
    nm = city64(name)
    return TrackHashes(ns, nm, hash_combine(hash_combine(0, ns), nm))


# -- node ids --------------------------------------------------------

def _id_half(text: str) -> int:
    parts = text.split(".")
    if len(parts) > 2 or not all(p.isascii() and p.isdigit() for p in parts):
        raise ValueError(f"node id half {text!r}: a uint32 or u16.u16")
    values = [int(p) for p in parts]
    if len(values) == 1:
        if values[0] > 0xFFFFFFFF:
            raise ValueError(f"node id half {text!r} over 32 bits")
        return values[0]
    if max(values) > 0xFFFF:
        raise ValueError(f"node id half {text!r}: dotted value over 65535")
    return values[0] << 16 | values[1]


def node_id_parse(text: str) -> int:
    """'high:low', each side a uint32 or u16.u16, as NodeId().Value()."""
    high, sep, low = text.partition(":")
    if not sep:
        raise ValueError(f"node id {text!r} has no ':'")
    return _id_half(high) << 32 | _id_half(low)


def node_id_format(node_id: int) -> str:
    """'a.b:c.d', four 16-bit groups high to low."""
    return (f"{node_id >> 48 & 0xFFFF}.{node_id >> 32 & 0xFFFF}:"
            f"{node_id >> 16 & 0xFFFF}.{node_id & 0xFFFF}")


def fresh_node_id(label: str) -> int:
    """A new non-zero id per call: city64 of label:random64. Peers keep
    a vanished node's subscribes for 30 s, so a restart must not reuse
    its id."""
    while True:
        node_id = city64(f"{label}:{secrets.randbits(64)}".encode())
        if node_id:
            return node_id


def libstdcxx_hash(data: bytes) -> int:
    """libstdc++ std::hash<std::string> on 64-bit targets (_Hash_bytes,
    seed 0xc70f6907): the library derives a node id from its label this
    way."""
    mul = 0xc6a4a7935bd1e995
    s = bytes(data)
    n = len(s)
    h = 0xc70f6907 ^ ((n * mul) & _M)
    aligned = n & ~7
    for i in range(0, aligned, 8):
        k = (_mix((int.from_bytes(s[i:i + 8], "little") * mul) & _M) * mul) & _M
        h = ((h ^ k) * mul) & _M
    if n & 7:
        h = ((h ^ int.from_bytes(s[aligned:], "little")) * mul) & _M
    h = (_mix(h) * mul) & _M
    return _mix(h)
