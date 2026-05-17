# Data path analysis: aiomoqt object ↔ wire

State: after Phase 1–7 (perf-0.3.2 + perf-0.9.4), 2026-05-16.

This document traces a single MoQT subgroup-stream object through every
layer of the stack — in both directions — annotating each step with:

- **copies** (memcpy / allocation)
- **waits** (await, condvar, kernel block)
- **syscalls** (write, read, eventfd, sendmmsg, recvmmsg, epoll_wait, …)
- **Python ↔ C boundary** crossings
- **thread boundaries** (asyncio main thread ↔ picoquic worker thread)

Goal: identify every cost so optimization conversations can be
specific. Sections are independent — read TX or RX in isolation.

---

## 0. Process and thread model

```
                    ┌─────────────────────────────────────┐
                    │  python process                     │
                    │                                     │
   asyncio events ──┼──▶ asyncio main thread ────────────┐│
                    │     (Python + Cython,              ││
                    │      holds GIL)                    ││
                    │                                    ▼│
                    │   SPSC TX ring ←──────── push ────┐ │
                    │                                   │ │
                    │     ┌─────────────────────────────┘ │
                    │     │                               │
                    │     ▼                               │
                    │   picoquic worker pthread           │
                    │     (C only, releases GIL)          │
                    │                                     │
                    │   SPSC RX ring ────► push ────────┐ │
                    │                                   │ │
                    │   per-stream sc->tx ◀── push ─────│ │
                    │   per-stream sc->rx ────► pop ────│ │
                    │                                   │ │
                    │   wake_fd (eventfd / pipe) ───────┘ │
                    └─────────────────────────────────────┘
                                       │
                              UDP socket (cnx)
                                       │
                                    network
```

- **Two threads only.** Asyncio main + picoquic worker. No thread per
  connection, no thread per stream.
- **GIL released for the worker thread** for almost its entire body —
  the only times it grabs the GIL are when calling user callbacks
  (which happens nowhere in the data path) or when interacting with
  Python objects (also nowhere on the worker side).
- **SPSC rings** are lock-free, atomic head/tail, single-producer
  single-consumer per direction. Memory order: producer release on
  tail/head store; consumer acquire on the matched load.
- **Wake mechanism:** Linux eventfd (one per `TransportContext`); pipe
  fallback on macOS. Asyncio attaches via `loop.add_reader(eventfd,
  _on_eventfd)`.

The asyncio main thread can be the producer (TX events) and consumer
(RX events) simultaneously; the picoquic worker is the matching
counterparty on each ring.

---

# TX path: aiomoqt object → wire

## Layer 1 — aiomoqt publisher loop

[aiomoqt/track.py:328:`_generate_subgroup`](aiomoqt/track.py#L328) is
one asyncio coroutine per parallel subgroup stream. On the rate-limited
hot path it loops:

```python
seq_info = f"{group_id}.{cur_obj_id}".encode()        # alloc + format
payload  = (seq_info + b'|' + pad)[:self.object_size] # alloc + slice
extensions = {MOQT_TIMESTAMP_EXT: int(time.time()*1e6)}  # dict + int
data = header.next_object_bytes(payload=payload, ...) # Cython call
await session.stream_write_drain(stream_id, data)     # async send
```

**Costs per iteration (rate-capped):**

| event | cost |
|---|---|
| `f"{group_id}.{cur_obj_id}".encode()` | small str format + bytes alloc |
| `(seq_info + b'|' + pad)[:size]` | bytes concat + slice — **1 alloc, 1 memcpy of `pad`** (the long part) |
| `int(time.time() * 1e6)` | C function (`time.time`) + int box |
| `extensions = {...}` | dict alloc |
| `header.next_object_bytes(...)` | crosses into Cython (Layer 2) |
| `await stream_write_drain(...)` | inline-fast-path send (Layer 3) |

Most iterations also do `await asyncio.sleep(...)` for rate pacing
when `--rate > 0` — that's the **only** wait in the steady state at
rate-capped points.

At **max rate (`-r 0`)** the rate sleep is replaced with
`await asyncio.sleep(0)` every 64 objects — a yield-without-timer.

## Layer 2 — encode_object_subgroup (Cython)

[aiomoqt/messages/track.py:`SubgroupHeader.next_object_bytes`](aiomoqt/messages/track.py)
is a thin Python wrapper that computes `delta = obj_id - prev - 1`
and calls
[aiopquic/_binding/_streamchain.pyx:`encode_object_subgroup`](src/aiopquic/_binding/_streamchain.pyx).

**`encode_object_subgroup` (Cython cpdef):**

1. Compute total size in bytes via `_varint_size` per field (no allocation).
2. `out = PyBytes_FromStringAndSize(NULL, total_size)` — **1 alloc** of
   the destination bytes object.
3. Get writable pointer via `PyBytes_AsString(out)`.
4. Write fields inline using `_varint_write` (no Python call) and
   `memcpy` for payload + extension byte-values:
   - delta varint (1–8 bytes)
   - ext_block_len varint
   - per extension: id varint, then value-varint or len+bytes
   - payload_len varint
   - **`memcpy(p, payload, payload_len)`** ← the payload copy
   - OR status varint (when payload is empty)
5. Return `out`.

**Per object on hot path:**

| event | count | notes |
|---|---|---|
| Python → Cython boundary | 1 | one `cpdef` call |
| allocation | 1 | the output bytes |
| memcpy | 1 | payload bytes into output |
| memcpy | small | extension byte-values (typically none for timestamp-only) |
| varint encode | ~3 | delta, ext_block_len, payload_len — all inlined |
| Cython → Python boundary | 1 | return |

No waits, no syscalls, no thread boundary.

## Layer 3 — `stream_write_drain` (Python)

[aiomoqt/protocol.py:1201:`stream_write_drain`](aiomoqt/protocol.py#L1201)
is the async backpressure-aware wrapper around the QUIC layer.
Phase-7 form:

```python
get_event = getattr(self._quic, 'get_tx_drain_event', None)
event = get_event(stream_id) if get_event else None
while True:
    if not self._session_writable(): return
    if event is not None: event.clear()
    try:
        self._quic.send_stream_data(stream_id, data, end_stream)
        return
    except BufferError:
        if event is not None: await event.wait()
        else:                 await asyncio.sleep(0.0001)
    except (AssertionError, AttributeError) as e:
        logger.debug(...); return
```

**Steady-state hot path (sc->tx not full):**

| event | cost |
|---|---|
| `getattr` lookup | small, once per call |
| `event.clear()` | asyncio Event clear — atomic flag write |
| `self._quic.send_stream_data(...)` | Python → Cython (Layer 4) |
| return | — |

No await fires; no Event.wait, no sleep.

**Slow path (sc->tx full → BufferError):** see *§Phase-7 backpressure
sub-path* below. On steady-state at rate-capped operating points the
slow path **does not fire**.

## Layer 4 — `QuicConnection.send_stream_data` (Python, aiopquic)

[aiopquic/quic/connection.py:355:`send_stream_data`](src/aiopquic/quic/connection.py#L355)
looks up the per-stream wrapper context and calls into Cython:

```python
sc = self._get_or_create_stream_ctx(stream_id)        # dict.get
rc = self._transport.tx_send_atomic(                  # → Cython
    stream_id, data if data is not None else b"",
    end_stream, self._cnx_ptr, sc, _STREAM_RING_CAP)
if rc == 1: raise BufferError("TX event ring full ...")
if rc == 2: raise BufferError("per-stream send ring full ...")
if rc < 0:  raise MemoryError(...)
```

**Per call:**

| event | cost |
|---|---|
| `_get_or_create_stream_ctx(stream_id)` | dict lookup; on first call: allocates a new `aiopquic_stream_ctx_t` via Cython call (rare) |
| Python → Cython boundary | 1 (the `tx_send_atomic` call) |
| Cython → Python boundary | 1 (return) |
| return-code branch | 3 cheap int compares |

## Layer 5 — `tx_send_atomic` (Cython, aiopquic)

[aiopquic/_binding/_transport.pyx:814:`tx_send_atomic`](src/aiopquic/_binding/_transport.pyx#L814)
holds the GIL through four atomic steps. Outline:

```cython
# PRE-CHECK 1: TX event ring has room for the MARK_ACTIVE event
if spsc_ring_count(self._ctx.tx_ring) >= self._ctx.tx_ring.capacity:
    return 1

# COMMIT: push payload bytes into per-stream sc->tx ring
rc = aiopquic_stream_ctx_send_data(sc, data_ptr, data_len,
                                   stream_ring_cap, end_stream)
if rc == 0: return 2
if rc < 0:  return -1

# Push MARK_ACTIVE event onto SPSC TX ring (pre-checked above)
memset(&entry, 0, sizeof(entry))
entry.event_type = SPSC_EVT_TX_MARK_ACTIVE
entry.stream_id = stream_id
entry.cnx = <void*>cnx_ptr
entry.stream_ctx = <void*>sc
spsc_ring_push(self._ctx.tx_ring, &entry, NULL, 0)

# Coalesced wake (0 → 1 transition fires picoquic_wake_up_network_thread)
if aiopquic_tx_wake_set_pending(self._ctx) == 0:
    picoquic_wake_up_network_thread(self._thread_ctx)
return 0
```

`aiopquic_stream_ctx_send_data` (in [c/stream_ctx.h](src/aiopquic/_binding/c/stream_ctx.h)) is
the **all-or-nothing** push:

1. Compute free bytes via atomic loads of tail/head.
2. If `free_bytes < len`: **atomically set `tx_drain_pending = 1`**
   (the Phase-7 backpressure arm), return 0.
3. Else: `aiopquic_stream_buf_push(sc->tx, data, len)` —
   **1–2 memcpys** into the ring (2 if the write wraps the ring tail).
4. Optionally set FIN.
5. Return 1.

The wake step calls `aiopquic_tx_wake_set_pending` (atomic exchange).
If the prior value was 0, this caller emits the wake; if it was 1
(another producer already armed), this caller skips the syscall. The
worker clears the flag back to 0 on entry to the wake handler **before**
draining, so any push that loses the wake race is still drained or
fires a fresh wake on the next iteration.

**Per call on hot path (sc->tx has room):**

| event | count | notes |
|---|---|---|
| Cython entry boundary | 1 | from Python |
| atomic load (sc->tx tail/head) | 2 | relaxed/acquire |
| memcpy into sc->tx | 1 (sometimes 2 on wrap) | the payload bytes |
| atomic store (sc->tx tail) | 1 | release |
| atomic store (`hash_enabled` check + ptr arith) | 0 if disabled | gated off by default |
| spsc_ring_count + ring push for MARK_ACTIVE | 1 push (atomic stores on tx_ring head/tail) | no data bytes |
| atomic exchange (tx_wake_pending) | 1 | acq-rel |
| `picoquic_wake_up_network_thread` syscall | 0 or 1 | **skipped** if a wake is already pending |
| return | — | |

On the hot path with continuous writes, the wake_up is heavily
coalesced — typically 1 wake per N ring pushes.

`picoquic_wake_up_network_thread` is in picoquic; it issues a
**`pthread_cond_signal`** (or platform equivalent) on the worker's
sleep condvar. That's the **one cross-thread cost** in the hot path.

## Layer 6 — picoquic worker thread

The picoquic worker is a dedicated pthread started by
`picoquic_start_network_thread`. Its main loop alternates between:

- Waiting on its condvar / a poll timeout for "wake_up" or "incoming
  packet";
- Processing pending events;
- Calling `aiopquic_loop_cb` (in
  [c/callback.h](src/aiopquic/_binding/c/callback.h)) on
  `picoquic_packet_loop_wake_up`;
- Calling `aiopquic_stream_cb` per active stream when
  `picoquic_callback_prepare_to_send` fires.

When woken, the worker first **clears** `tx_wake_pending` (release
store) so subsequent pushes will fire a fresh wake, then drains the
TX SPSC ring:

```c
// from c/callback.h:548-570
case picoquic_packet_loop_wake_up:
    __atomic_store_n(&ctx->tx_wake_pending, 0, __ATOMIC_RELEASE);
    while (1) {
        spsc_entry_t* entry = spsc_ring_peek(ctx->tx_ring);
        if (!entry) break;
        ...
        switch (entry->event_type) {
            case SPSC_EVT_TX_MARK_ACTIVE:
                picoquic_mark_active_stream(cnx, entry->stream_id,
                                             1, entry->stream_ctx);
                ...
                break;
            // (other TX op events: TX_STREAM_DATA, TX_STREAM_FIN,
            //  TX_DATAGRAM, TX_CLOSE, WT TX ops, etc.)
        }
    }
```

`picoquic_mark_active_stream` flips the cnx into the "due for
sending" list. The packet loop then picks it up and fires
`prepare_to_send` callbacks per stream until either:

- The packet's frame budget is consumed, OR
- The stream's sc->tx is empty, OR
- Congestion control / flow control blocks further frames.

[c/callback.h:328-376:`prepare_to_send`](src/aiopquic/_binding/c/callback.h):

```c
aiopquic_stream_ctx_t* sc = (aiopquic_stream_ctx_t*)stream_ctx;
aiopquic_stream_buf_t* sb = sc->tx;
uint32_t avail = aiopquic_stream_buf_used(sb);
uint32_t to_send = (avail < want) ? avail : want;
int fin_after = aiopquic_stream_buf_fin_pending(sb);
int is_fin = (fin_after && to_send == avail) ? 1 : 0;
int is_still_active = (avail > to_send) ? 1 : 0;
uint8_t* buf = picoquic_provide_stream_data_buffer(
    bytes, to_send, is_fin, is_still_active);
if (buf && to_send > 0) {
    aiopquic_stream_buf_pop(sb, buf, to_send);    // memcpy out of ring
    ctx->worker_prepare_to_send_pulled_bytes += to_send;
    // Phase-7: notify Python writer that drained
    uint32_t expected = 1;
    if (atomic_compare_exchange_strong_explicit(
            &sc->tx_drain_pending, &expected, 0,
            memory_order_acq_rel, memory_order_relaxed)) {
        spsc_entry_t drain = {SPSC_EVT_STREAM_TX_DRAINED, ...};
        spsc_ring_push(ctx->rx_ring, &drain, NULL, 0);
        aiopquic_notify_rx(ctx);
    }
}
```

**Worker side, per object's worth of bytes:**

| event | count | notes |
|---|---|---|
| atomic loads (sc->tx tail/head) | 2 | |
| `picoquic_provide_stream_data_buffer` call | 1 | picoquic gives a destination in its frame buffer |
| **memcpy out of sc->tx** | 1 (sometimes 2 on wrap) | from ring into picoquic's STREAM frame body |
| TLS encrypt + packet build | 1 logical copy (in-place where possible) | inside picoquic |
| Phase-7 CAS (tx_drain_pending) | 1 | only fires the SPSC push if 1→0 succeeds |

When the packet is fully assembled picoquic calls **`sendmmsg`** on
the UDP socket. With `send_length_max = 65535` (set in our packet
loop config), the kernel does GSO segmentation — many QUIC packets in
**one syscall**.

**Single TX syscall per packet train**, not per QUIC packet.

## TX path summary (per object, rate-capped hot path)

| layer | copies | allocs | waits | syscalls | Py↔C | thread switch |
|---|---:|---:|---:|---:|---:|---:|
| 1 publisher loop | 1 (pad slice) | 2 (bytes, dict) | rate sleep only | — | — | — |
| 2 encode | 1 (payload→output) | 1 (output bytes) | — | — | 1 (out) | — |
| 3 stream_write_drain | — | — | — | — | — | — |
| 4 send_stream_data | — | — | — | — | 1 (out) | — |
| 5 tx_send_atomic | 1 (output→sc->tx) | — | — | 0 or 1 (wake — coalesced) | — | 1 (wake) |
| 6 worker drain | 1 (sc->tx→frame) | — | — | — | — | — |
| 6 picoquic build | 1 logical (encrypt) | — | — | 1 (sendmmsg per packet-train) | — | — |
| **total** | **~4 memcpies** | **~3 small allocs** | rate sleep only | **~1 wake syscall** (coalesced) + 1 sendmmsg per train | **2 crossings** | **1 wake** (coalesced) |

---

## TX Phase-7 backpressure sub-path

Fires only when `aiopquic_stream_ctx_send_data` finds sc->tx full.
Most operating points never trigger this; saturation max-rate runs
trigger it constantly. Sequence per fill→drain cycle:

```
Producer (asyncio thread)              Worker thread
───────────────────────                ─────────────
send_stream_data
  → tx_send_atomic
    → aiopquic_stream_ctx_send_data
      free_bytes < len:
        store_release tx_drain_pending = 1
        return 0
  → BufferError raised
stream_write_drain catches:
  await event.wait()                   prepare_to_send
                                         pop bytes from sc->tx
                                         CAS tx_drain_pending 1→0:
                                           spsc_ring_push(STREAM_TX_DRAINED)
                                           aiopquic_notify_rx(ctx)
                                             write(eventfd, 1)
                                 ──────────────────────────────────
                                 asyncio main thread wakes (epoll)
_on_eventfd → _process_events
  next_event → _drain_and_convert
    → drain_rx_callback → _handle_raw_event
      evt_type == _EVT_STREAM_TX_DRAINED:
        event.set()
event.wait() resumes
→ loop, send_stream_data retries
```

**Cost per fill→drain cycle (NOT per object):**

| event | count |
|---|---|
| atomic store (tx_drain_pending = 1) | 1 |
| BufferError raise + catch | 1 |
| asyncio Event.wait | 1 await |
| (worker) CAS tx_drain_pending 1→0 | 1 |
| (worker) spsc_ring_push drain event | 1 |
| (worker) `write(eventfd, 1)` syscall | 1 |
| asyncio epoll_wait returns | 1 |
| `_process_events` + drain + handler | 1 batch |
| `event.set()` | 1 atomic |
| Event.wait() resumes | 1 wake |
| retry send_stream_data | 1 (then succeeds) |

**This is the single biggest win in Phase 7** — pre-Phase-7, the
publisher polled `await asyncio.sleep(0.0001)` every 100µs, which the
profile showed as ~5 s of `events.__init__` + `call_later` + `heappop`
overhead per 30 s saturation run. Post-Phase-7: 0.18 s `locks.set` +
0.33 s `locks.wait` — ~25× cheaper, and **only fires once per ring
fill→drain cycle, not once per 100µs of saturation**.

---

# RX path: wire → on_object callback

## Layer 1 — UDP socket + picoquic ingress (worker thread)

The picoquic worker thread is blocked on its socket / wake condvar
when idle. On packet arrival:

1. **`recvmmsg`** drains the UDP socket — multiple datagrams per
   syscall. Kernel copies packet bytes into a user-space buffer
   (1 copy).
2. picoquic parses the long/short header, looks up the connection,
   feeds packet through its TLS state (decrypt — in place where
   possible).
3. picoquic parses QUIC frames. For STREAM frames it invokes our
   stream callback with the frame's plaintext payload.

## Layer 2 — `aiopquic_stream_cb` for stream_data (C)

[c/callback.h:399+:`aiopquic_stream_cb`](src/aiopquic/_binding/c/callback.h)
fires for every STREAM frame body. For PULL-model streams (the
default), the callback **pushes bytes into the per-stream sc->rx
ring** rather than into the global RX SPSC ring:

```c
if (sc->rx) {
    aiopquic_stream_buf_push(sc->rx, bytes, length);
}
spsc_entry_t entry = {0};
entry.event_type = is_fin ? SPSC_EVT_STREAM_FIN : SPSC_EVT_STREAM_DATA;
entry.stream_id = stream_id;
entry.cnx = cnx;
entry.stream_ctx = sc;            // pointer; data is in sc->rx
spsc_ring_push(ctx->rx_ring, &entry, NULL, 0);
aiopquic_notify_rx(ctx);          // write(eventfd, 1) — coalesced
```

The **bytes never enter the global RX ring** — only an entry that
points at the stream context. Receivers pop the entry and then pop
bytes from sc->rx.

**Per stream-frame arrival on the worker thread:**

| event | count |
|---|---|
| memcpy into sc->rx | 1 (sometimes 2 on wrap) |
| atomic store (sc->rx tail) | 1 |
| spsc_ring_push event entry | 1 |
| `aiopquic_notify_rx` | atomic exchange + (0 or 1) `write(eventfd)` |

The `write(eventfd, 1)` is the single cross-thread wake. Coalesced
via `rx_notify_pending` — if asyncio hasn't drained yet, subsequent
worker pushes skip the syscall.

## Layer 3 — asyncio wakes via eventfd

Asyncio attaches the worker's eventfd as a reader:

```python
# aiopquic/asyncio/protocol.py:42
self._loop.add_reader(eventfd, self._on_eventfd)
```

When the worker writes the eventfd, asyncio's `epoll_wait` returns;
the loop dispatches `_on_eventfd`:

```python
# aiopquic/asyncio/protocol.py:54-56
def _on_eventfd(self) -> None:
    self._process_events()

def _process_events(self) -> None:
    event = self._quic.next_event()
    while event is not None:
        ...
        self.quic_event_received(event)
        event = self._quic.next_event()
```

For aiomoqt's MoQT subclass, `quic_event_received` is overridden
([aiomoqt/protocol.py:898](aiomoqt/protocol.py#L898)).

## Layer 4 — `_drain_and_convert` + `drain_rx_callback` (Cython + Python)

[aiopquic/quic/connection.py:206:`_drain_and_convert`](src/aiopquic/quic/connection.py#L206):

```python
def _drain_and_convert(self) -> None:
    if self._transport is None: return
    self._transport.drain_rx_callback(self._handle_raw_event)
```

[_transport.pyx:`drain_rx_callback`](src/aiopquic/_binding/_transport.pyx)
(Cython) loops:

```cython
for i in range(max_events):
    entry = spsc_ring_peek(self._ctx.rx_ring)
    if entry is NULL: break

    # Resolve the bytes payload:
    if entry.event_type in (SPSC_EVT_STREAM_DATA, SPSC_EVT_STREAM_FIN):
        sc = <aiopquic_stream_ctx_t*>entry.stream_ctx
        avail = aiopquic_stream_buf_used(sc.rx)
        if avail > 0:
            buf = malloc(avail)                       # ← per-event alloc
            aiopquic_stream_buf_pop(sc.rx, buf, avail) # ← memcpy out
            data = memoryview(StreamChunk._wrap(buf, avail))
            aiopquic_stream_ctx_rx_consumed_add(sc, avail)  # atomic flow-control advance

    # Skip orphan/coalesced same-stream events
    if data is None and is_stream_data: spsc_ring_pop; continue

    handler(entry.event_type, entry.stream_id, data, entry.is_fin,
            entry.error_code, <uintptr_t>entry.cnx, <uintptr_t>entry.stream_ctx)
    spsc_ring_pop(self._ctx.rx_ring)
    count += 1

aiopquic_clear_rx(self._ctx)   # drain eventfd + re-arm rx_notify_pending
```

`aiopquic_clear_rx` does the **`read(eventfd)`** to drain the wake
counter, then re-arms `rx_notify_pending = 0` (release store) so the
next worker push fires a fresh wake.

[connection.py:`_handle_raw_event`](src/aiopquic/quic/connection.py)
turns each entry into a typed QUIC event and appends to `self._events`:

```python
if evt_type in (_EVT_STREAM_DATA, _EVT_STREAM_FIN):
    self._events.append(StreamDataReceived(
        stream_id=stream_id,
        data=data if data is not None else memoryview(b""),
        end_stream=(evt_type == _EVT_STREAM_FIN),
    ))
elif evt_type == _EVT_STREAM_TX_DRAINED:
    event = self._stream_tx_drain_events.get(stream_id)
    if event is None:
        event = asyncio.Event()
        self._stream_tx_drain_events[stream_id] = event
    event.set()
# ... other event types
```

**Per stream-data event:**

| event | count |
|---|---|
| `spsc_ring_peek` (atomic load) | 1 |
| atomic load (sc->rx tail/head) | 2 |
| **malloc** sc->rx-sized buffer | 1 ← RX-only allocation cost |
| **memcpy** out of sc->rx | 1 (sometimes 2 on wrap) |
| atomic add (sc->rx_consumed) — flow control advance | 1 |
| StreamChunk wrap + `memoryview` construction | 1 small alloc (memoryview is a view; no extra copy) |
| Cython → Python boundary (`handler` call) | 1 |
| `StreamDataReceived` dataclass alloc | 1 |
| list `.append` to `self._events` | 1 |
| `spsc_ring_pop` (atomic store) | 1 |

**After the batch:**

| event | count |
|---|---|
| `read(eventfd)` | 1 syscall |
| `rx_notify_pending = 0` release store | 1 |
| optional re-arm `write(eventfd)` if ring non-empty | 0 or 1 syscall |

## Layer 5 — `quic_event_received` → `_on_stream_data`

[aiomoqt/protocol.py:898:`quic_event_received`](aiomoqt/protocol.py#L898)
dispatches each `StreamDataReceived` to
[`_on_stream_data`](aiomoqt/protocol.py#L483) (Phase-5 inline,
synchronous):

```python
def _on_stream_data(self, stream_id, data, end_stream):
    if stream_id in self._stream_torn_down: return
    state = self._data_streams.get(stream_id)
    if state is None:
        state = _DataStreamState(chain=StreamChain())
        self._data_streams[stream_id] = state
    if data and len(data) > 0:
        state.chain.extend(data)    # by reference — no copy
    if state.chain.capacity > 0:
        self._drain_stream(stream_id, state)
    if end_stream and stream_id in self._data_streams:
        self._cleanup_stream(stream_id)
```

`state.chain.extend(data)` is a Cython call into StreamChain. It
calls `PyObject_GetBuffer` to pin the memoryview, appends a `_Chunk`
to the chain's deque. **Zero payload copy** — the chain holds the
memoryview by reference, parse-pulls walk across chunk boundaries.

## Layer 6 — parse loop in `_drain_stream`

[aiomoqt/protocol.py:503:`_drain_stream`](aiomoqt/protocol.py#L503)
loops parse-commit until the chain underflows:

```python
while chain.capacity > 0:
    chain.save()
    try:
        msg_obj = self._moqt_handle_data_stream(stream_id, chain, chain.capacity)
    except MOQTStreamReject as e: self._reject_stream(...); return
    except MOQTUnderflow: chain.rollback(); return
    except BufferReadError: chain.rollback(); return
    except Exception as e: # forensic log + reject
    consumed = chain.tell()
    state.bytes_total += consumed
    chain.commit()
    # ... dispatch by msg_obj type:
    if isinstance(msg_obj, ObjectHeader): ... on_object_received(...)
    elif isinstance(msg_obj, SubgroupHeader): ...
    ...
```

`_moqt_handle_data_stream` dispatches to the per-stream parser. For
a subgroup stream, each iteration after admission calls
`obj.deserialize_into(chain, len, extensions_present, prev_object_id)`,
which routes through Cython:

```python
try:
    delta, exts, status, payload = chain.parse_object_subgroup(
        extensions_present, MOQTMessage.EXTENSIONS_LEN_LIMIT)
except AttributeError:
    # legacy Buffer fallback (tests/microbench)
    ...
self.object_id = delta if prev is None else prev + delta + 1
self.extensions = exts
self.status = ObjectStatus(status)
self.payload = payload
```

## Layer 7 — `parse_object_subgroup` (Cython)

[_streamchain.pyx:`parse_object_subgroup`](src/aiopquic/_binding/_streamchain.pyx)
walks the chain's chunks via the chain's own pull_uint_var / pull_bytes
helpers — entirely in Cython:

1. pull_uint_var → delta
2. if extensions_present:
   - pull_uint_var → exts_len
   - if exts_len > limit: raise RuntimeError (framer desync detection)
   - while pos < exts_end: pull ext_id, pull value (varint or
     `pull_bytes(value_len)`) → exts dict
3. pull_uint_var → payload_len
4. if 0: pull_uint_var → status; payload = b""
5. else: `pull_bytes(payload_len)` — **1 PyBytes alloc + 1 memcpy
   of payload from chain into a fresh bytes object**
6. return tuple (delta, exts, status, payload)

If any pull underflows (chunks don't have enough bytes yet),
`StreamUnderflow` raises and the caller's `chain.rollback()` resets
the cursor to the saved anchor.

**Per object on hot path:**

| event | count |
|---|---|
| Python → Cython boundary | 1 |
| pull_uint_var calls (internal) | 3–5 (delta, ext_len, ext_id?, value?, payload_len) |
| **pull_bytes for payload** — 1 PyBytes alloc + 1 memcpy | 1 |
| extensions dict + ext entries | 1 small dict, 1–2 int values per object (timestamp only) |
| return tuple alloc | 1 |
| Cython → Python boundary | 1 |

## Layer 8 — `on_object` callback (Python)

For sub_bench:
[aiomoqt/examples/sub_bench.py:68:`on_object`](aiomoqt/examples/sub_bench.py#L68)
does latency math, RFC-3550 jitter, loss detection, periodic report
emission. **All Python; ~3 µs per object on the profile.**

## RX path summary (per object, rate-capped hot path)

| layer | copies | allocs | waits | syscalls | Py↔C | thread switch |
|---|---:|---:|---:|---:|---:|---:|
| worker recvmmsg | 1 (kernel→user) | — | — | 1 recvmmsg per batch | — | — |
| picoquic decrypt | 0–1 (in-place) | — | — | — | — | — |
| stream_cb → sc->rx | 1 (frame→ring) | — | — | 1 eventfd write (coalesced) | — | 1 wake (coalesced) |
| asyncio epoll wakes | — | — | (was epoll_wait) | — | — | — |
| _drain_and_convert + drain_rx_callback | 1 (sc->rx→buf) | 1 (malloc) + 1 (StreamChunk) | — | 1 read(eventfd) per batch | 1 in, N callbacks out | — |
| _handle_raw_event | — | 1 (StreamDataReceived) per event | — | — | — | — |
| _on_stream_data + chain.extend | 0 (by ref) | — | — | — | 1 (chain.extend) | — |
| _drain_stream + parse_object_subgroup | 1 (chain→PyBytes payload) | 1 (PyBytes) + 1 (dict) + 1 (tuple) per object | — | — | 1 in, 1 out per object | — |
| on_object | — | small (lat list, jitter scalars) | — | — | — | — |
| **total** | **~4 memcpies** | **~5 small allocs** | epoll wait between batches | 1 recvmmsg per batch + 1 eventfd write/read pair per batch | **~3 crossings** | **1 wake** (coalesced) |

---

# Where time goes — by operating point

Measured on Ryzen WSL2, dev tree post-Phase-7, against local moqx
relay (raw QUIC, draft 16). Numbers are publisher-side or
subscriber-side wallclock self-time as indicated.

## Rate-capped 10K obj/s × 2 streams × 1 KiB (166 Mbps target)

**Publisher CPU (30 s wallclock):**
| function | self time | calls | per-call cost |
|---|---:|---:|---:|
| epoll.poll (idle) | 17.71 s | — | — |
| `connection.send_stream_data` | 3.95 s | 290 K | ~14 µs |
| `_generate_subgroup` (loop body) | 1.87 s | 280 K | ~7 µs |
| `next_object_bytes` | 0.41 s | 283 K | ~1.4 µs |
| `_extensions_encode` | — | — | gone (was 1.32 s pre-Phase-6) |
| `serialize` (legacy) | — | — | gone (was 1.22 s pre-Phase-6) |
| `stream_write_drain` | 0.24 s | 285 K | ~0.8 µs |

**Subscriber CPU (22 s wallclock):**
| function | self time |
|---|---:|
| epoll.poll (idle) | 10.43 s |
| `_drain_stream` | 1.18 s |
| `on_object` (bench) | 1.56 s |
| `deserialize_into` | 0.69 s |
| `_moqt_handle_data_stream` | 0.56 s |
| `_drain_and_convert` | 0.44 s |
| `_handle_raw_event` | 0.20 s |

## Saturation 32 KiB × 8 streams × max rate (~750 Mbps)

**Publisher CPU (30 s wallclock, with cProfile overhead):**
| function | self time | note |
|---|---:|---|
| epoll.poll (idle) | **20.81 s (69%)** | publisher has CPU headroom |
| `send_stream_data` | 1.75 s | |
| `_drain_stream` (sub side, not shown) | — | |
| `_generate_subgroup` | 0.78 s | |
| `stream_write_drain` | 0.54 s | |
| `next_object_bytes` | 0.26 s | |
| **`locks.wait`** (Event.wait) | **0.33 s** | Phase-7 backpressure |
| **`locks.set`** (Event.set) | **0.18 s** | Phase-7 backpressure |

Note that pre-Phase-7 the saturation profile showed `sleep` 1.43 s +
`call_at` 0.81 s + `call_later` 0.80 s + `events.__init__` 0.86 s +
`heappop` 0.49 s + `events.__lt__` 0.71 s = **~5.1 s** of asyncio
timer overhead. Post-Phase-7 that block is **~0.5 s** (Event
operations). The ~4.6 s freed is why epoll idle went from 28% → 69%.

---

# Bottlenecks by regime

| regime | wall | evidence |
|---|---|---|
| 1 KiB × 2 streams × 1K obj/s (16 Mbps) | nothing — both ends >95% idle | epoll dominates |
| 1 KiB × 2 streams × 10K obj/s (166 Mbps) | per-object Python orchestration (RX side) | _drain_stream + deserialize_into + handle_data_stream sum to ~2.4 s of 22 s |
| 32 KiB × 8 streams × max rate (~750 Mbps) | **not Python — but exactly *where* needs measurement** | see "What we can and can't attribute" below |
| 32 KiB × many streams × max rate (multi-Gbps target, not currently reached) | unverified; suspects: relay forwarding, pub-side picoquic worker | requires multi-process pub or relay profiling to disambiguate |

## What we can and can't attribute at saturation

**What the profiles show:**
- Publisher Python: **69% idle in epoll**, 31% active CPU
- Subscriber Python: **46% idle in epoll**, 54% active CPU
- Both ends have measurable CPU headroom
- 0% packet loss, sustained 744 Mbps over 60 s
- Publisher latency p99 ≈ 455 ms (sustained queue at the publisher's sc->tx)

**What "epoll idle" actually means concretely:**

Publisher idle = blocked in `await event.wait()` because sc->tx was
full, waiting for the picoquic worker to drain bytes. So
publisher-side Python is *not* CPU-bound.

Subscriber idle = blocked in `epoll_wait` because the eventfd hasn't
fired — i.e., the picoquic worker thread hasn't pushed more
stream-data events. So subscriber-side Python is *not* CPU-bound
either.

**What this rules out:**
- The aiomoqt encode/parse hot path (Layer 2 / Layer 7) is **not** the wall.
- The drain/dispatch hot path (Layer 4-5 on RX) is **not** the wall.
- The asyncio scheduler is **not** the wall (we measured 0.5 s of
  Event ops per 30 s; trivial vs the 9-15 s of idle).

**What it does NOT directly prove:**

I previously said "the C picoquic worker is the wall." That overstated
what the publisher-side profile shows. The publisher's epoll idle just
says "we're waiting for sc->tx to drain." The chain of possible reasons:

```
pub Python (await) ← pub sc->tx FULL ← pub picoquic worker pulls slowly ← ???

The "???" could be any of:
  a) pub picoquic worker is CPU-bound on TLS encrypt + packet framing
  b) pub kernel UDP send buffer is full (relay not draining)
  c) pub QUIC flow control: relay hasn't extended MAX_STREAM_DATA
  d) relay (moxygen) is forwarding-rate-limited
  e) sub picoquic worker is CPU-bound on decrypt + parse
  f) sub Python isn't advancing rx_consumed → relay sub-side blocked
  g) network loopback (unlikely — kernel loopback is GB/s class)
```

We can't disambiguate without **measuring the C-side** — none of which
shows up in cProfile of the Python process.

**How to actually pin this down (concrete experiments):**

1. **`pidstat` per process during saturation.** Shows CPU% per process.
   If `moqx` is at 100% one core, it's (d). If `pub_bench` is at
   100% across both threads (asyncio + worker), it's (a). If `moqx`
   is at 30% and pub is at 30%, it's something else (b/c/g).
   Trivial to run: `pidstat -p $(pgrep -d, moqx),$(pgrep -d, pub_bench)
   -u 5` during a sustained run.

2. **`py-spy dump --pid <PID>` on the publisher.** Captures all
   threads including the C worker thread. We'll see whether the
   worker is stuck in `sendmmsg`, in `picoquic_*` encrypt routines,
   or idle.

3. **Direct pub→sub without moqx.** Run pub_bench against the
   in-process loopback test server (if one exists; if not, the
   sub_bench can be wrapped as a server). Eliminates (d). If
   throughput jumps significantly, the relay was the wall.

4. **Disable TLS — picoquic has a NULL cipher mode for tests.**
   If throughput jumps, encryption was a significant cost.

5. **Increase initial_max_data + initial_max_stream_data per cnx in
   the relay config.** If throughput jumps, QUIC flow control at the
   relay was throttling.

**Current honest assessment:** at 744 Mbps single-pub-single-sub
through moqx, the wall is **somewhere in the C/relay/network path,
not in our Python**. The most likely candidates by intuition (not
measurement) are (d) relay forwarding rate and (a) pub-side picoquic
worker — both happen to be C-only paths that wouldn't show up in our
cProfile work. The right next step before more aiopquic perf work is
running experiments 1 and 2 above.

---

# Remaining optimization opportunities

Re-ranked after the honesty pass on bottlenecks above. The wall at
saturation is currently NOT in our Python — so the highest-value items
are the ones that move the wall when it does come back to us (higher
rates, more streams, multi-connection scale), not necessarily what
gains us the most at 744 Mbps single-pub.

## A. RX payload zero-copy (highest impact when Python returns to the wall)

**Where:**
[_transport.pyx:`drain_rx_callback`](src/aiopquic/_binding/_transport.pyx),
lines that do:
```cython
buf = malloc(<size_t>length)
aiopquic_stream_buf_pop(rx_sb, <uint8_t*>buf, <uint32_t>length)
data = memoryview(StreamChunk._wrap(buf, length))
aiopquic_stream_ctx_rx_consumed_add(sc, <uint64_t>length)
```

**What's wasted:**
- The picoquic worker just pushed bytes into sc->rx (memcpy #1).
- drain_rx_callback malloc's a private buffer and pops bytes into it
  (memcpy #2).
- Eventually `parse_object_subgroup.pull_bytes(payload_len)` allocates
  a PyBytes and memcpies again (memcpy #3).

That's **3 copies of every payload byte** between worker write and
Python-visible bytes. At 32 KiB × 3000 obj/s = ~96 MB/s of memory
bandwidth burned in copies. Worse on bigger object sizes.

**Two zero-copy shapes:**

### Shape A: chunks reference sc->rx directly
- `aiopquic_stream_buf_pop` is replaced by `aiopquic_stream_buf_peek`
  that returns up to 2 spans (handles ring wrap).
- StreamChunk wraps those spans as a memoryview INTO sc->rx itself.
- Python holds the memoryview; the chain extends from it.
- When the StreamChunk is GC'd, advance `rx_consumed` and the
  picoquic worker can extend MAX_STREAM_DATA.

**Hard part:** the bytes are still IN sc->rx until released. If the
worker thread writes new bytes into sc->rx while Python still holds
the memoryview, the consumer reads garbage. **Must coordinate:**
- sc->rx capacity ≥ "max in-flight bytes Python holds"
- worker checks that the slot it's about to write isn't pinned
- OR: ring size is provisioned to be larger than any Python-held window

`initial_max_stream_data` already sizes sc->rx per stream. The
existing flow-control machinery uses rx_consumed to extend
MAX_STREAM_DATA, but the **peer is only told it can send more after
Python releases the chunk**. That serializes: peer must wait for us
to GC before sending the next window. Latency hit if Python is slow
to release.

**Estimated win:** −1 memcpy + −1 malloc per stream-data event.
At 750 Mbps × 32 KiB obj/s: ~3000 mallocs/sec eliminated + ~96 MB/s
memory-bandwidth savings. Probably ~5–10% on the RX CPU at this rate;
larger at higher rates.

### Shape B: PyBytes destination passed back into the pop
- Track stream "wants to be parsed" via inline parse from
  `drain_rx_callback` (skip the StreamDataReceived → handler hop).
- Pop directly into a PyBytes the parser owns.
- Cuts the malloc-and-copy out of the drain path but doesn't eliminate
  the PyBytes alloc in pull_bytes.

**Less complete than A but simpler to implement.** Saves the malloc
in drain but keeps one of the memcpies.

### Recommended order
Shape B first (smaller change, ~half the win), validate; then Shape A
if RX is back to being the wall.

## B. TX send_data_vec — eliminate the encode-output intermediate bytes

**Where:**
TX path Layer 2 (`encode_object_subgroup`) currently returns ONE bytes
blob containing `[delta][ext_block][payload_len][payload]`. Layer 5
(`tx_send_atomic`) then memcpies that blob into sc->tx.

The payload was already a `bytes` (handed in by the caller). We
allocate a NEW bytes (output) and memcpy the payload into it — purely
so the call into aiopquic is "one bytes object."

**Better shape:** new aiopquic API:
```c
static inline int aiopquic_stream_ctx_send_data_vec(
    aiopquic_stream_ctx_t* sc,
    const struct iovec* iov, int iovcnt,
    uint32_t capacity, uint8_t set_fin);
```
- Cython side: `tx_send_atomic_vec(stream_id, list_of_bytes, …)`
- Python side: aiomoqt builds `[header_bytes, payload]` and calls
  the vec version. The header is freshly encoded (varints +
  extensions); payload is the input bytes passed through.
- aiopquic_stream_ctx_send_data_vec: precheck total free in sc->tx,
  then memcpy each chunk in.

**Code changes:**
1. New Cython: `encode_object_subgroup_header(delta, exts, status,
   payload_len, extensions_present) -> bytes` (just the header bytes,
   does not memcpy the payload).
2. New aiomoqt method: `SubgroupHeader.next_object_header_bytes(...)`
   returns header bytes only.
3. New aiopquic Cython: `tx_send_atomic_vec` that accepts a list of
   bytes-like objects.
4. New C: `aiopquic_stream_ctx_send_data_vec` does the all-or-nothing
   precheck across the sum, then per-chunk push.
5. New aiomoqt protocol: `stream_write_drain_vec(stream_id, chunks)`.
6. Publisher loop calls the vec version:
   ```python
   header = header.next_object_header_bytes(extensions, status, len(payload))
   await session.stream_write_drain_vec(stream_id, [header, payload])
   ```

**Estimated win:**
- −1 PyBytes alloc (the output blob from encode)
- −1 memcpy (payload into output blob)

At 32 KiB × 3000 obj/s: ~96 MB/s memory bandwidth saved on TX.
~5–8% pub-side CPU saving at saturation.

Caveat: at saturation, pub Python is 69% idle. This is a win
that "moves the wall forward" — it won't immediately raise the
744 Mbps number because the wall isn't Python TX right now.

## C. drain_rx_callback malloc → slab (subset of A)

**Where:** same site as A, the `malloc(<size_t>length)` per event.

**Standalone (without zero-copy):** keep the copy, but allocate from
a free-list of size buckets per TransportContext. ~10–20% faster than
libc malloc for variable-size allocs of this shape on modern glibc;
much less than ptmalloc's free-list contention pain on free-threaded
builds.

**Why this is a smaller win than A:** memcpy time and malloc time are
both costs; only malloc is removed. The memcpy stays. A eliminates
both.

**When to do C standalone:** if A's lifetime-management complexity is
too risky to land in a release. C is a "safe partial" of A.

## D. Inline parse from `drain_rx_callback`

**Where:** [_transport.pyx](src/aiopquic/_binding/_transport.pyx) +
[connection.py](src/aiopquic/quic/connection.py) +
[aiomoqt/protocol.py](aiomoqt/protocol.py)

**Current shape:**
- drain_rx_callback (Cython) → handler callback into Python per entry
- handler builds StreamDataReceived (one dataclass alloc per event)
- handler appends to self._events list
- `next_event` pops from self._events (one at a time)
- quic_event_received dispatches to _on_stream_data per event
- _on_stream_data → state.chain.extend → _drain_stream → parse

That's **N Cython→Python crossings + N dataclass allocs + N events
list pushes + N events list pops** per drain batch — when the parse
itself could happen inline.

**Better shape:** drain_rx_callback (or a sibling) emits parsed
objects directly to a per-stream parser callback, bypassing the
StreamDataReceived hop entirely for the hot subgroup-stream path.

This is more invasive: the QUIC event abstraction is generic
(StreamDataReceived covers ALL stream data, not just MoQT subgroup).
Either:
- Add a per-stream "parse-callback" mode at QuicConnection. When a
  stream is admitted as a subgroup stream, register the callback;
  drain_rx_callback then calls the callback directly with parsed
  objects instead of materializing StreamDataReceived.
- Or just collapse the abstraction at the aiomoqt layer: parse
  in `_handle_raw_event` itself, append parsed object to a per-stream
  ready queue, dispatch from there.

**Estimated win at saturation:**
- −1 dataclass alloc per event (StreamDataReceived)
- −1 list append + pop pair
- ~1 fewer Python frame on the hot path per event

Maybe 5–10% on RX CPU. Subtle.

## E. send_length_max + sendmmsg batching audit

**Where:** `picoquic_packet_loop_param_t` exposed in
[_transport.pyx:159](src/aiopquic/_binding/_transport.pyx#L159)
and set in start() at line 1041.

**Current:** `send_length_max = 65535`. That's the per-segment max
for one GSO `sendmsg`. Multiple GSO sends within one packet-loop
iteration would still be separate syscalls.

**Audit:** does picoquic do `sendmmsg` (note the extra m — vectorized)
to batch multiple destinations in one syscall? Look at picoquic
sources. If not, that's a kernel-side improvement.

Estimated win: untested, but each syscall is ~1 µs; saving 10 of
them per pps would add up at high pps.

## F. picoquic packet loop multi-thread (`is_port_shared`)

**Where:** picoquic's `picoquic_packet_loop_v3` supports
`is_port_shared` — multiple loop threads on the same UDP port via
SO_REUSEPORT. Different connections land on different threads via
kernel hashing.

**Helps:** multi-connection scenarios (many publishers fanning into
one relay; relay handling many subscribers).

**Does NOT help our current single-pub-single-sub scenario.** The
single connection lives on one worker thread, period.

**Win:** linear with thread count for connection-parallel workloads.

## G. NULL-cipher / encryption cost audit

**Where:** picoquic configuration. picoquic has a test/dev mode where
the TLS layer uses a NULL cipher for stress testing.

**Test:** run saturation pub/sub with NULL cipher, see what the
throughput ceiling is.

**Why it matters:** if turning off TLS gets us from 744 Mbps to
~2 Gbps, then encryption is the wall at saturation (suspect (a) in
the bottleneck table — pub picoquic worker CPU-bound on TLS). That
points future optimization at: (i) AES-NI usage in picotls, (ii)
session-key caching, (iii) potentially hardware offload. If the
ceiling doesn't move, encryption isn't the wall.

This is an **experiment to disambiguate the bottleneck**, not an
optimization itself. Encryption is not optional in production.

## H. Move aiopquic↔aiomoqt Cython relayering

See [project_aiopquic_aiomoqt_layering memory](file:///home/gmarzot/.claude/projects/-home-gmarzot-Projects-moq-aiomoqt/memory/project_aiopquic_aiomoqt_layering.md).
Architectural cleanup; no expected perf delta. Pre-req for items A/B/C
landing cleanly (gives aiomoqt its own Cython surface to add helpers
without polluting aiopquic).

## I. uvloop

Single-digit % gain on the current path per prior analysis. Worth
trying once the bigger items land, since the relative gain might
grow as Python overhead shrinks.

## Recommended order (after disambiguating bottleneck)

1. **First: run the experiments in §"What we can and can't attribute"**.
   Whichever wall they expose determines whether to chase A/B/D
   (Python-Cython-C path) or push deeper into picoquic/relay.
2. If experiments show C/relay wall: defer Python-side perf, move to
   relay scaling work (out of aiopquic scope) or NULL-cipher
   investigation (G).
3. If experiments show our Python comes back as the wall at some
   operating point: B (TX send_data_vec) is the highest-leverage
   single change for current 0.9.4. A (RX zero-copy) is the
   next-cycle big move; C (slab) is the safer interim.
4. H (relayering) before A/B/C-vec changes if we want to keep
   aiopquic free of MoQT semantics long-term.
5. D, E, F, I as polish.

---

# Appendix: file/line index

| component | file:line |
|---|---|
| aiomoqt publisher loop | [aiomoqt/track.py:328:_generate_subgroup](aiomoqt/track.py#L328) |
| next_object_bytes wrapper | [aiomoqt/messages/track.py](aiomoqt/messages/track.py) |
| encode_object_subgroup | [src/aiopquic/_binding/_streamchain.pyx](src/aiopquic/_binding/_streamchain.pyx) |
| stream_write_drain | [aiomoqt/protocol.py:1201](aiomoqt/protocol.py#L1201) |
| QuicConnection.send_stream_data | [src/aiopquic/quic/connection.py:355](src/aiopquic/quic/connection.py#L355) |
| tx_send_atomic (Cython) | [src/aiopquic/_binding/_transport.pyx:814](src/aiopquic/_binding/_transport.pyx#L814) |
| aiopquic_stream_ctx_send_data | [src/aiopquic/_binding/c/stream_ctx.h](src/aiopquic/_binding/c/stream_ctx.h) |
| aiopquic_stream_buf_push | [src/aiopquic/_binding/c/stream_buf.h:107](src/aiopquic/_binding/c/stream_buf.h#L107) |
| MARK_ACTIVE + wake | [src/aiopquic/_binding/_transport.pyx:884](src/aiopquic/_binding/_transport.pyx#L884) |
| worker loop callback | [src/aiopquic/_binding/c/callback.h:542](src/aiopquic/_binding/c/callback.h#L542) |
| prepare_to_send | [src/aiopquic/_binding/c/callback.h:326](src/aiopquic/_binding/c/callback.h#L326) |
| aiopquic_stream_buf_pop | [src/aiopquic/_binding/c/stream_buf.h:132](src/aiopquic/_binding/c/stream_buf.h#L132) |
| aiopquic_notify_rx | [src/aiopquic/_binding/c/callback.h:217](src/aiopquic/_binding/c/callback.h#L217) |
| aiopquic_clear_rx | [src/aiopquic/_binding/c/callback.h:243](src/aiopquic/_binding/c/callback.h#L243) |
| asyncio _on_eventfd | [src/aiopquic/asyncio/protocol.py:54](src/aiopquic/asyncio/protocol.py#L54) |
| drain_rx_callback | [src/aiopquic/_binding/_transport.pyx](src/aiopquic/_binding/_transport.pyx) |
| _drain_and_convert | [src/aiopquic/quic/connection.py:206](src/aiopquic/quic/connection.py#L206) |
| _handle_raw_event | [src/aiopquic/quic/connection.py](src/aiopquic/quic/connection.py) |
| quic_event_received (aiomoqt) | [aiomoqt/protocol.py:898](aiomoqt/protocol.py#L898) |
| _on_stream_data | [aiomoqt/protocol.py:468](aiomoqt/protocol.py#L468) |
| _drain_stream | [aiomoqt/protocol.py:503](aiomoqt/protocol.py#L503) |
| _moqt_handle_data_stream | [aiomoqt/protocol.py:726](aiomoqt/protocol.py#L726) |
| ObjectHeader.deserialize_into | [aiomoqt/messages/track.py:285](aiomoqt/messages/track.py#L285) |
| parse_object_subgroup | [src/aiopquic/_binding/_streamchain.pyx](src/aiopquic/_binding/_streamchain.pyx) |
| sub_bench on_object | [aiomoqt/examples/sub_bench.py:68](aiomoqt/examples/sub_bench.py#L68) |
