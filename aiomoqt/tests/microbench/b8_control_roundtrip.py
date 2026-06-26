"""B8 — MoQT CONTROL message round-trip microbench (d14/d16/d18).

Isolates the per-draft control-plane layout/codec cost: length-prefixed
vs delta-coded KVP params, uint8 vs varint param values, reply Request-ID
gating, and RFC9000 vs vi64 message bodies. No transport, no session —
just serialize() + deserialize() on representative control messages.

For each (message, draft) the bench:
  1. builds the message with realistic fields,
  2. serializes once and VERIFIES it deserializes back (key fields match)
     before timing — any combo that legitimately doesn't apply is skipped
     and annotated,
  3. times encode and decode separately with time_loop.

Output: an ENCODE table and a DECODE table (one row per message, one
column per draft, ns/op), then a SUMMARY with relative ratios (d18/d16,
d14/d16) and a one-line verdict computed from the measured numbers.

Run: python -m aiomoqt.tests.microbench.b8_control_roundtrip
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Callable, Optional

from aiomoqt.context import profile_for
from aiomoqt.messages import (
    Subscribe, SubscribeOk, Publish, PublishOk, RequestUpdate,
)
from aiomoqt.types import (
    MOQTMessageType, D16MessageType, GroupOrder, FilterType,
    ForwardingPreference, ContentExistsCode, ParamType,
)
from aiomoqt.utils.buffer import Buffer

from ._stats import time_loop

# The control serializers log per-param at INFO; silence them so the tight
# round-trip loop doesn't drown the table in log lines.
logging.disable(logging.WARNING)


DRAFTS = (14, 16, 18)


# ---------------------------------------------------------------------------
# Message construction — realistic fields per message.
# ---------------------------------------------------------------------------

def _make_subscribe() -> Subscribe:
    return Subscribe(
        request_id=7,
        track_namespace=(b'live', b'sports'),
        track_name=b'football',
        priority=128,
        group_order=GroupOrder.ASCENDING,
        forward=1,
        filter_type=FilterType.ABSOLUTE_RANGE,
        start_group=10, start_object=5, end_group=100,
        parameters={ParamType.DELIVERY_TIMEOUT: 5000},
    )


def _make_subscribe_ok() -> SubscribeOk:
    return SubscribeOk(
        request_id=7,
        track_alias=42,
        expires=300,
        group_order=GroupOrder.ASCENDING,
        content_exists=ContentExistsCode.EXISTS,
        largest_group_id=50,
        largest_object_id=200,
        parameters={ParamType.DELIVERY_TIMEOUT: 5000},
    )


def _make_publish() -> Publish:
    return Publish(
        request_id=7,
        track_namespace=(b'live', b'sports'),
        track_name=b'football',
        track_alias=42,
        group_order=GroupOrder.ASCENDING,
        content_exists=ContentExistsCode.EXISTS,
        largest_group_id=50,
        largest_object_id=200,
        forward=ForwardingPreference.SUBGROUP,
        parameters={ParamType.DELIVERY_TIMEOUT: 5000},
    )


def _make_publish_ok() -> PublishOk:
    return PublishOk(
        request_id=7,
        forward=ForwardingPreference.SUBGROUP,
        priority=128,
        group_order=GroupOrder.ASCENDING,
        filter_type=FilterType.ABSOLUTE_RANGE,
        start_group=10, start_object=5, end_group=100,
        parameters={},
    )


def _make_request_update() -> RequestUpdate:
    # FORWARD (0x10) is a uint8-valued param at d18 — exercises the uint8
    # param-value codec path there, the varint one at d14/d16.
    return RequestUpdate(
        request_id=7,
        existing_request_id=5,
        parameters={ParamType.FORWARD: 1},
    )


# (label, factory, type_id, is_reply)
#   is_reply -> d18 drops the in-band Request ID on the wire (demuxed from
#   the request stream), so request_id is absent there and excluded from the
#   field-match verification for d18 only.
MESSAGES = [
    ("Subscribe",     _make_subscribe,      MOQTMessageType.SUBSCRIBE,    False),
    ("SubscribeOk",   _make_subscribe_ok,   MOQTMessageType.SUBSCRIBE_OK, True),
    ("Publish",       _make_publish,        MOQTMessageType.PUBLISH,      False),
    ("PublishOk",     _make_publish_ok,     MOQTMessageType.PUBLISH_OK,   True),
    ("RequestUpdate", _make_request_update, D16MessageType.REQUEST_UPDATE, False),
]

# Fields verified for the round-trip (kept small + load-bearing per message).
VERIFY_FIELDS = {
    "Subscribe":     ("request_id", "track_namespace", "track_name", "filter_type"),
    "SubscribeOk":   ("track_alias", "expires", "largest_group_id"),
    "Publish":       ("request_id", "track_namespace", "track_alias"),
    "PublishOk":     ("forward", "priority", "filter_type"),
    "RequestUpdate": ("request_id", "parameters"),
}


# ---------------------------------------------------------------------------
# Round-trip primitives.
# ---------------------------------------------------------------------------

def _strip_header(raw: bytes, draft: int) -> bytes:
    """Strip the message Type (vi64 for d18, RFC9000 otherwise) + 16-bit
    Length prefix, returning the message body the deserializer consumes."""
    prof = profile_for(draft)
    head = Buffer(data=raw)
    head.vi64 = prof.vi64
    head.pull_vint()            # Type
    head.pull_uint16()          # Length
    return raw[head.tell():]


def _decode_body(cls, body: bytes, draft: int):
    prof = profile_for(draft)
    rbuf = Buffer(data=body, vi64=prof.vi64)
    return cls.deserialize(rbuf, prof=prof, buf_end=len(body))


def _verify(label: str, factory, is_reply: bool, draft: int) -> Optional[str]:
    """Build + serialize + deserialize once; assert key fields survive the
    round-trip. Returns None on success, or a skip/error reason string."""
    cls = type(factory())
    prof = profile_for(draft)
    msg = factory()
    raw = bytes(msg.serialize(prof=prof).data)
    body = _strip_header(raw, draft)
    out = _decode_body(cls, body, draft)
    for fname in VERIFY_FIELDS[label]:
        # Replies omit Request ID on the d18 wire — not a wire field there.
        if fname == "request_id" and is_reply and draft == 18:
            assert getattr(out, "request_id") is None, \
                f"{label} d18 reply should omit request_id on the wire"
            continue
        exp = getattr(msg, fname)
        got = getattr(out, fname)
        assert exp == got, f"{label} d{draft}: {fname} {exp!r} != {got!r}"
    return None


def _enc_fn(factory, prof) -> Callable[[], None]:
    msg = factory()

    def _fn():
        msg.serialize(prof=prof)
    return _fn


def _dec_fn(factory, draft) -> Callable[[], None]:
    cls = type(factory())
    prof = profile_for(draft)
    raw = bytes(factory().serialize(prof=prof).data)
    body = _strip_header(raw, draft)

    def _fn():
        rbuf = Buffer(data=body, vi64=prof.vi64)
        cls.deserialize(rbuf, prof=prof, buf_end=len(body))
    return _fn


def _time_ns(fn, duration, warmup) -> float:
    iters, elapsed = time_loop(fn, target_seconds=duration,
                               warmup_seconds=warmup)
    return (elapsed * 1e9) / iters if iters > 0 else 0.0


# ---------------------------------------------------------------------------
# Table + summary.
# ---------------------------------------------------------------------------

def _print_table(title: str, rows: dict) -> None:
    print(title)
    hdr = (f"  {'message':<16}"
           + "".join(f"{('d' + str(d) + ' ns/op'):>14}" for d in DRAFTS))
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for label, _f, _t, _r in MESSAGES:
        cells = []
        for d in DRAFTS:
            v = rows[label].get(d)
            cells.append("skip".rjust(14) if v is None
                         else f"{v:>14,.0f}")
        print(f"  {label:<16}" + "".join(cells))
    print()


def _ratios(rows: dict) -> tuple[float, float]:
    """Mean d18/d16 and d14/d16 across messages with all three present."""
    r18, r14 = [], []
    for label, *_ in MESSAGES:
        v14, v16, v18 = (rows[label].get(d) for d in (14, 16, 18))
        if v16:
            if v18:
                r18.append(v18 / v16)
            if v14:
                r14.append(v14 / v16)
    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0
    return mean(r18), mean(r14)


def main() -> int:
    p = argparse.ArgumentParser(
        description="MoQT control-message round-trip microbench (d14/d16/d18)")
    p.add_argument("--duration", type=float, default=2.0)
    p.add_argument("--warmup", type=float, default=0.3)
    args = p.parse_args()

    print(f"B8 — MoQT control round-trip   drafts={DRAFTS}  "
          f"duration={args.duration}s")
    print()

    enc: dict = {n: {} for n, *_ in MESSAGES}
    dec: dict = {n: {} for n, *_ in MESSAGES}
    skips: list[str] = []

    for label, factory, _type_id, is_reply in MESSAGES:
        for d in DRAFTS:
            prof = profile_for(d)
            try:
                _verify(label, factory, is_reply, d)
            except Exception as e:  # legitimately-N/A or codec gap
                enc[label][d] = None
                dec[label][d] = None
                skips.append(f"{label} d{d}: {e}")
                continue
            enc[label][d] = _time_ns(_enc_fn(factory, prof),
                                     args.duration, args.warmup)
            dec[label][d] = _time_ns(_dec_fn(factory, d),
                                     args.duration, args.warmup)

    _print_table("ENCODE (serialize)", enc)
    _print_table("DECODE (deserialize)", dec)

    enc18, enc14 = _ratios(enc)
    dec18, dec14 = _ratios(dec)
    print("SUMMARY  (relative to d16, mean of per-message ratios)")
    print(f"  encode   d18/d16 {enc18:5.2f}x   d14/d16 {enc14:5.2f}x")
    print(f"  decode   d18/d16 {dec18:5.2f}x   d14/d16 {dec14:5.2f}x")
    enc18_pct = (enc18 - 1.0) * 100.0
    dec18_pct = (dec18 - 1.0) * 100.0
    net = ((enc18 + dec18) / 2.0 - 1.0) * 100.0
    print(f"  -> d18 control encode {enc18_pct:+.0f}% / decode {dec18_pct:+.0f}% "
          f"vs d16; vi64 bodies + delta KVP + uint8 params net {net:+.0f}%.")

    if skips:
        print("\nskipped (message,draft) combos:")
        for s in skips:
            print(f"  - {s}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
