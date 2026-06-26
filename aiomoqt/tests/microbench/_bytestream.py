"""Pre-encoded MoQT wire bytes for parser microbenches.

Builds subgroup stream and fetch stream byte sequences using the
real SubgroupHeader / FetchHeader serializers, so the parser sees
spec-conformant wire bytes. Returns a single bytes object that
the bench can chunk and feed.
"""
from __future__ import annotations

from aiomoqt.messages import ObjectStatus
from aiomoqt.context import profile_for
from aiomoqt.messages.track import (
    SubgroupHeader, FetchHeader, FetchObject,
)


def make_subgroup_stream(
    n_objects: int,
    payload_size: int,
    *,
    track_alias: int = 1,
    group_id: int = 0,
    extensions: bool = False,
    draft: int = 16,
) -> bytes:
    """Build a complete subgroup stream: header + n_objects.

    payload_size is the length of each object's payload. `draft` selects
    the wire codec (vi64 for d18, RFC9000 for d14/d16) so the corpus is
    valid wire for that draft. Returns the concatenated bytes ready to
    feed to the parser.
    """
    sg = SubgroupHeader(
        track_alias=track_alias,
        group_id=group_id,
        subgroup_id=0,
        extensions_present=extensions,
        prof=profile_for(draft),
    )
    out = bytearray()
    out += bytes(sg.serialize().data)
    payload = b"x" * payload_size
    for _ in range(n_objects):
        out += bytes(sg.next_object(payload=payload).data)
    return bytes(out)


def make_fetch_stream(n_objects: int, payload_size: int,
                      *, request_id: int = 0) -> bytes:
    """Build a fetch stream: FETCH_HEADER + n FetchObjects."""
    fh = FetchHeader(request_id=request_id)
    out = bytearray()
    out += bytes(fh.serialize().data)
    payload = b"x" * payload_size
    for i in range(n_objects):
        fo = FetchObject(
            group_id=0, subgroup_id=0, object_id=i,
            publisher_priority=128, extensions=None,
            status=ObjectStatus.NORMAL, payload=payload,
        )
        out += bytes(fo.serialize(prof=profile_for(16)).data)
    return bytes(out)


def chunked(data: bytes, chunk_size: int) -> list[bytes]:
    """Split data into roughly chunk_size pieces. Last chunk may be short."""
    if chunk_size <= 0 or chunk_size >= len(data):
        return [data]
    return [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
