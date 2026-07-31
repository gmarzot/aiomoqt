#!/usr/bin/env python3
"""Synthetic test for _process_data_stream buffer reassembly.

Simulates high-throughput object delivery by splitting serialized
objects at various boundaries and feeding them through a mock
stream queue, then verifying all objects parse correctly.
"""
import asyncio
import time
from aiomoqt.utils.buffer import Buffer, BufferReadError
from aiomoqt.messages.track import SubgroupHeader, ObjectHeader
from aiomoqt.types import MOQT_TIMESTAMP_EXT, ObjectStatus
import aiomoqt.types as types

# Set version context
types._moqt_ctx_version = 0xff00000e


def build_stream_data(num_objects: int = 100, object_size: int = 4096) -> bytes:
    """Build a complete stream: SubgroupHeader + N objects."""
    header = SubgroupHeader(
        track_alias=0, group_id=0, subgroup_id=0,
        publisher_priority=255, extensions_present=True,
    )
    hdr_buf = header.serialize()
    stream = bytearray(hdr_buf.data)

    for i in range(num_objects):
        ext = {MOQT_TIMESTAMP_EXT: int(time.time() * 1000)}
        payload = (f"| 0.{i} |".encode() + b'X' * object_size)[:object_size]
        obj_buf = header.next_object(payload=payload, extensions=ext)
        stream += obj_buf.data

    return bytes(stream)


def split_at_sizes(data: bytes, chunk_sizes: list) -> list:
    """Split data into chunks of specified sizes."""
    chunks = []
    pos = 0
    for size in chunk_sizes:
        if pos >= len(data):
            break
        end = min(pos + size, len(data))
        chunks.append(data[pos:end])
        pos = end
    if pos < len(data):
        chunks.append(data[pos:])
    return chunks


def split_uniform(data: bytes, chunk_size: int) -> list:
    """Split data into uniform chunks (simulating MTU-sized delivery)."""
    return [data[i:i+chunk_size] for i in range(0, len(data), chunk_size)]


async def parse_stream_chunks(chunks: list, expect_objects: int) -> dict:
    """Parse chunks through the same logic as _process_data_stream.

    Returns dict with results.
    """
    re_buf = Buffer(capacity=(1024 * 1024 * 32))
    cur_pos = 0
    needed = 0
    parsed_objects = []
    group_id = None
    subgroup_id = None
    prev_object_id = None
    data_stream = {}  # mock _data_streams

    from aiomoqt.messages.track import (
        SubgroupHeader, ObjectHeader, FetchHeader,
        SUBGROUP_HEADER_BASE, SUBGROUP_ID_EXPLICIT, SUBGROUP_ID_FIRST_OBJ,
    )
    from aiomoqt.types import ObjectStatus
    from aiomoqt.messages.base import MOQTUnderflow

    stream_id = 15  # mock

    for chunk_idx, chunk in enumerate(chunks):
        msg_buf = Buffer(data=chunk)
        cur_pos = msg_buf.tell()  # 0 for all chunks
        msg_len = msg_buf.capacity

        if needed > 0:
            if msg_len < needed:
                needed -= msg_len
                re_buf.push_bytes(msg_buf.data_slice(cur_pos, msg_len))
                continue
            # Enough data — append and reprocess
            re_buf.push_bytes(msg_buf.data_slice(cur_pos, msg_buf.capacity))
            msg_len = re_buf.tell()
            msg_buf = re_buf
            msg_buf.seek(0)
            cur_pos = 0
            needed = 0

        while cur_pos < msg_len:
            try:
                pos = msg_buf.tell()
                # Parse: same logic as _moqt_handle_data_stream
                if data_stream.get(stream_id) is None:
                    stream_type = msg_buf.pull_uint_var()
                    if 0x10 <= stream_type <= 0x1D and ((stream_type >> 1) & 0x03) != 3:
                        msg_header = SubgroupHeader.deserialize(msg_buf, type_val=stream_type)
                        data_stream[stream_id] = msg_header
                        group_id = msg_header.group_id
                        subgroup_id = msg_header.subgroup_id
                    else:
                        raise ValueError(f"Unexpected stream type: 0x{stream_type:x}")
                else:
                    sg_header = data_stream[stream_id]
                    msg_header = ObjectHeader.deserialize(
                        msg_buf, msg_len,
                        extensions_present=sg_header.extensions_present,
                        prev_object_id=sg_header._last_object_id
                    )
                    sg_header._last_object_id = msg_header.object_id

                    if msg_header.status != ObjectStatus.NORMAL:
                        parsed_objects.append(('status', msg_header.object_id, msg_header.status))
                    else:
                        parsed_objects.append(('object', msg_header.object_id, len(msg_header.payload)))

                cur_pos = msg_buf.tell()

            except MOQTUnderflow as e:
                # e.needed is absolute buffer position; convert to bytes beyond msg_len
                needed = e.needed - msg_len
                if needed <= 0:
                    needed = 1
                break
            except BufferReadError:
                needed = 1  # don't know how much, just get more
                break
            except Exception as e:
                return {
                    'success': False,
                    'error': str(e),
                    'error_type': type(e).__name__,
                    'chunk_idx': chunk_idx,
                    'parsed': len(parsed_objects),
                    'cur_pos': cur_pos,
                    'msg_len': msg_len,
                    'buf_tell': msg_buf.tell(),
                }

        if needed > 0:
            have = msg_len - cur_pos
            saved_bytes = msg_buf.data_slice(cur_pos, msg_len)
            if have < needed:
                needed -= have
            re_buf.seek(0)
            re_buf.push_bytes(saved_bytes)

    return {
        'success': True,
        'parsed': len(parsed_objects),
        'expected': expect_objects,
        'match': len(parsed_objects) == expect_objects,
        'objects': parsed_objects,
    }


async def test_complete_objects():
    """Each chunk contains exactly one complete object."""
    print("Test 1: Complete objects per chunk")
    data = build_stream_data(num_objects=10, object_size=1024)
    # Don't split — one big chunk
    result = await parse_stream_chunks([data], 10)
    print(f"  Result: {result['success']} parsed={result['parsed']} expected={result['expected']}")
    assert result['match'], f"Mismatch: {result}"


async def test_mtu_split():
    """Objects split at MTU boundaries (1200 bytes) — the high-rate scenario."""
    print("Test 2: MTU-split (1200 byte chunks), 10 objects × 1024B payload")
    data = build_stream_data(num_objects=10, object_size=1024)
    chunks = split_uniform(data, 1200)
    print(f"  Stream: {len(data)} bytes, {len(chunks)} chunks")
    result = await parse_stream_chunks(chunks, 10)
    print(f"  Result: {result['success']} parsed={result['parsed']} expected={result['expected']}")
    if not result['success']:
        print(f"  Error: {result['error_type']}: {result['error']}")
        print(f"  Failed at chunk {result['chunk_idx']}, parsed {result['parsed']} objects")
    assert result['match'], f"Mismatch: {result}"


async def test_mtu_split_large():
    """Large objects split across many MTU chunks."""
    print("Test 3: MTU-split (1200 byte chunks), 50 objects × 4096B payload")
    data = build_stream_data(num_objects=50, object_size=4096)
    chunks = split_uniform(data, 1200)
    print(f"  Stream: {len(data)} bytes, {len(chunks)} chunks")
    result = await parse_stream_chunks(chunks, 50)
    print(f"  Result: {result['success']} parsed={result['parsed']} expected={result['expected']}")
    if not result['success']:
        print(f"  Error: {result['error_type']}: {result['error']}")
        print(f"  Failed at chunk {result['chunk_idx']}, parsed {result['parsed']} objects")
    assert result['match'], f"Mismatch: {result}"


async def test_mtu_split_64k():
    """64KB objects split across many MTU chunks — stress test."""
    print("Test 4: MTU-split (1200 byte chunks), 20 objects × 65536B payload")
    data = build_stream_data(num_objects=20, object_size=65536)
    chunks = split_uniform(data, 1200)
    print(f"  Stream: {len(data)} bytes, {len(chunks)} chunks")
    result = await parse_stream_chunks(chunks, 20)
    print(f"  Result: {result['success']} parsed={result['parsed']} expected={result['expected']}")
    if not result['success']:
        print(f"  Error: {result['error_type']}: {result['error']}")
        print(f"  Failed at chunk {result['chunk_idx']}, parsed {result['parsed']} objects")
    assert result['match'], f"Mismatch: {result}"


async def test_tiny_chunks():
    """Pathological case: very small chunks (100 bytes)."""
    print("Test 5: Tiny chunks (100 bytes), 10 objects × 4096B payload")
    data = build_stream_data(num_objects=10, object_size=4096)
    chunks = split_uniform(data, 100)
    print(f"  Stream: {len(data)} bytes, {len(chunks)} chunks")
    result = await parse_stream_chunks(chunks, 10)
    print(f"  Result: {result['success']} parsed={result['parsed']} expected={result['expected']}")
    if not result['success']:
        print(f"  Error: {result['error_type']}: {result['error']}")
        print(f"  Failed at chunk {result['chunk_idx']}, parsed {result['parsed']} objects")
    assert result['match'], f"Mismatch: {result}"


async def test_split_in_header():
    """Split right in the middle of an object header."""
    print("Test 6: Split at object header boundary")
    data = build_stream_data(num_objects=5, object_size=1024)
    # Split at 5 (after subgroup header), then 8 (mid-object-header), then rest
    chunks = split_at_sizes(data, [5, 8, 50, 1200, 1200, 1200, 1200, 1200])
    print(f"  Stream: {len(data)} bytes, {len(chunks)} chunks")
    result = await parse_stream_chunks(chunks, 5)
    if result['success']:
        print(f"  Result: {result['success']} parsed={result['parsed']} expected={result['expected']}")
        assert result['match'], f"Mismatch: {result}"
    else:
        print(f"  Error: {result['error_type']}: {result['error']}")
        print(f"  Failed at chunk {result['chunk_idx']}, parsed {result['parsed']} objects")
        assert False, f"Parse error: {result}"


async def main():
    tests = [
        test_complete_objects,
        test_mtu_split,
        test_mtu_split_large,
        test_mtu_split_64k,
        test_tiny_chunks,
        test_split_in_header,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            await test()
            passed += 1
        except AssertionError as e:
            print(f"  FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{'='*40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*40}")


if __name__ == "__main__":
    asyncio.run(main())
