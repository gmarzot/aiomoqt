"""moq-fabric CityHash, track hashes and node ids.

Vectors from moq-fabric f10e8bc5's cityhash.h, name.h and node_info.h, and libstdc++ std::hash<std::string>, compiled with g++ 11.4 -std=c++20.
"""
import pytest

from aiomoqt.moq_fabric.hashing import (city64, fresh_node_id, hash_combine,
                                     libstdcxx_hash, namespace_hash,
                                     node_id_format, node_id_parse,
                                     track_hashes)


def _digits64():
    return ("0123456789" * 6 + "0123").encode()


def _range(n):
    return bytes(i & 0xFF for i in range(n))


# One per length class: 0, 1-3, 4-7, 8-16, 17-32, 33-64, >64.
CITY = [
    (b"", 0x9ae16a3b2f90404f),
    (b"a", 0xb3454265b6df75e3),
    (b"ab", 0xaa8d6e5242ada51e),
    (b"abc", 0x24a5b3a074e7f369),
    (b"abcd", 0x411d0dd0ed2ab4b3),
    (b"abcdefg", 0xdd0a47e06cf3e279),
    (b"abcdefgh", 0xd00f9e3a5dfe6d74),
    (b"live", 0xa8f78c85310182c0),
    (b"video", 0x8dfd2b64f2d8463b),
    (b"moq-test", 0x17ce9e94508fb908),
    (b"aiomoqt", 0x1984c282901e2f9c),
    (b"0123456789abcdef", 0x78df54107f532c96),
    (b"0123456789abcdefg", 0xfb8f0f7d62966878),
    (b"0123456789abcdef" * 2, 0xb84788856a9d6f27),
    (b"0123456789abcdef" * 2 + b"X", 0x257ec515bd87e3e0),
    (_digits64(), 0xfe230c7808b22a98),
    (_range(65), 0xcbf87324301e340e),
    (_range(127), 0x1da495ffd4ec3cf7),
    (_range(128), 0xa0823c81ce5e74f0),
    (_range(129), 0x79617f28f34aa0cc),
    (_range(200), 0xe7e4ca75ba00026e),
    (_range(1000), 0x06f57090edf3ec91),
]

# (namespace tuples, name) -> (namespace, name, fullname)
TRACKS = [
    ((b"moq-test", b"live"), b"video",
     (0x18b469fe11acd643, 0x8dfd2b64f2d8463b, 0x773c11b5d5e8b10e)),
    ((b"aiomoqt",), b"track0",
     (0x1984c282901e2f9c, 0x0255566faeb3872f, 0xe38286231db8048f)),
    ((b"edge-left",), b"live",
     (0x8734d7a6d084d1f9, 0xa8f78c85310182c0, 0xbabade7ef004e919)),
    ((b"demo",), b"",
     (0xc75b2d610612d894, 0x9ae16a3b2f90404f, 0xf7260bfde8a2eee9)),
    ((b"aiomoqt", b"probe"), b"",
     (0x599fc53333edde89, 0x9ae16a3b2f90404f, 0xb380d272feff0cff)),
    ((), b"",
     (0x9ae16a3b2f90404f, 0x9ae16a3b2f90404f, 0xe99333bb9d359ac0)),
]

STD_HASH = [
    ("", 0x553e93901e462a6e),
    ("a", 0x454ddee488c1ed6b),
    ("relay-a", 0xb97e0ef037b17f89),
    ("edge-left", 0x6960f93214b94f14),
    ("moqx-1", 0x02c55e68e4401df4),
    ("abcdefgh", 0x783db3e38db898bb),
    ("abcdefghi", 0xb4ec9257851c8aaf),
    ("aiomoqt-peer", 0xcd011e5eafd72fd6),
    ("relay.m10x.org:33435", 0x3cd6db474e620182),
]


@pytest.mark.parametrize("data,want", CITY, ids=lambda v: None
                         if isinstance(v, int) else f"len{len(v)}")
def test_city64(data, want):
    assert city64(data) == want


@pytest.mark.parametrize("namespace,name,want", TRACKS)
def test_track_hashes(namespace, name, want):
    assert track_hashes(namespace, name) == want


# Tuple boundaries and order change the namespace hash.
CHAINED = [
    ((b"ab", b"c"), (0x9256a7fadf777e8b, 0xc17efdbaecff6802, 0x1a1f6a4d9c2062dd)),
    ((b"a", b"bc"), (0x7567a6ea518f00e2, 0xc17efdbaecff6802, 0x3083762ed52f4aa8)),
    ((b"a", b"b"), (0x7b9a7e9bab37fb01, 0xc17efdbaecff6802, 0x08a753b461eee941)),
    ((b"b", b"a"), (0xf01eaa9a9027bb83, 0xc17efdbaecff6802, 0xd75258df32d6e6a7)),
    ((b"a", b"b", b"c"), (0x38f801782951d438, 0xc17efdbaecff6802, 0xf86933086c3ab984)),
]


@pytest.mark.parametrize("namespace,want", CHAINED)
def test_namespace_hash_chains_tuples(namespace, want):
    assert track_hashes(namespace, b"x") == want


def test_namespace_hash_chain_step():
    ab = namespace_hash((b"a", b"b"))
    assert namespace_hash((b"a", b"b", b"c")) == city64(ab.to_bytes(8, "big") + b"c")
    assert namespace_hash((b"a",)) == city64(b"a")


def test_hash_combine():
    assert hash_combine(hash_combine(0, 0x1234), (1 << 64) - 1) \
        == 0x40000028b22c084d


@pytest.mark.parametrize("label,want", STD_HASH)
def test_libstdcxx_hash(label, want):
    assert libstdcxx_hash(label.encode()) == want


@pytest.mark.parametrize("text,value", [
    ("12:34", 51539607586),
    ("1.2:34", 281483566645282),
    ("100.2:9001.2001", 28147506850891729),
    ("123456:789.100", 530239534202980),
])
def test_node_id_parse(text, value):
    assert node_id_parse(text) == value


@pytest.mark.parametrize("value,text", [
    (347364508091815901, "1234.5678:100.6109"),
    ((1 << 64) - 1, "65535.65535:65535.65535"),
    (51539607586, "0.12:0.34"),
])
def test_node_id_format(value, text):
    assert node_id_format(value) == text
    assert node_id_parse(text) == value


@pytest.mark.parametrize("text", [
    "1234", "1.70000:1.2", "1.2.3:4", "x:1", "4294967296:0", ":", "1:",
    "-1:1", "１:1",
])
def test_node_id_parse_rejects(text):
    with pytest.raises(ValueError):
        node_id_parse(text)


def test_fresh_node_id_differs_per_call():
    ids = {fresh_node_id("aiomoqt-peer") for _ in range(64)}
    assert len(ids) == 64 and 0 not in ids
