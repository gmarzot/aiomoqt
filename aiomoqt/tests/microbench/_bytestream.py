"""Pre-encoded MoQT wire bytes for parser microbenches.

Builds subgroup stream and fetch stream byte sequences using the
real SubgroupHeader / FetchHeader serializers, so the parser sees
spec-conformant wire bytes. Returns a single bytes object that
the bench can chunk and feed.
"""
from __future__ import annotations

from aiomoqt.context import set_moqt_ctx_version
from aiomoqt.messages import ObjectStatus
from aiomoqt.messages.track import (
    SubgroupHeader, FetchHeader, FetchObject,
)
from aiomoqt.types import MOQT_VERSION_DRAFT16


def make_subgroup_stream(
    n_objects: int,
    payload_size: int,
    *,
    track_alias: int = 1,
    group_id: int = 0,
    extensions: bool = False,
) -> bytes:
    """Build a complete subgroup stream: header + n_objects.

    payload_size is the length of each object's payload. Returns the
    concatenated bytes ready to feed to the parser.
    """
    set_moqt_ctx_version(MOQT_VERSION_DRAFT16)
    sg = SubgroupHeader(
        track_alias=track_alias,
        group_id=group_id,
        subgroup_id=0,
        extensions_present=extensions,
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
    set_moqt_ctx_version(MOQT_VERSION_DRAFT16)
    fh = FetchHeader(request_id=request_id)
    out = bytearray()
    out += bytes(fh.serialize().data)
    payload = b"x" * payload_size
    prev = None
    for i in range(n_objects):
        fo = FetchObject(
            group_id=0, subgroup_id=0, object_id=i,
            publisher_priority=128, extensions=None,
            status=ObjectStatus.NORMAL, payload=payload,
        )
        out += bytes(fo.serialize(prev_obj=prev).data)
        prev = fo
    return bytes(out)


def chunked(data: bytes, chunk_size: int) -> list[bytes]:
    """Split data into roughly chunk_size pieces. Last chunk may be short."""
    if chunk_size <= 0 or chunk_size >= len(data):
        return [data]
    return [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
