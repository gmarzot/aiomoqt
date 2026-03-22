# Draft-14 Gap Analysis: aiomoqt vs draft-ietf-moq-transport-14

Spec source: https://raw.githubusercontent.com/moq-wg/moq-transport/refs/tags/draft-ietf-moq-transport-14/draft-ietf-moq-transport.md

## CRITICAL: Wrong Message Type IDs

| Message | Implementation | Spec (draft-14) |
|---------|---------------|-----------------|
| PUBLISH | **0x1B** | **0x1D** |
| PUBLISH_OK | **0x1C** | **0x1E** |
| PUBLISH_ERROR | **0x1D** | **0x1F** |
| TRACK_STATUS_REQUEST | 0x0D (name wrong) | **TRACK_STATUS** = 0x0D |
| TRACK_STATUS | 0x0E (name wrong) | **TRACK_STATUS_OK** = 0x0E |
| TRACK_STATUS_ERROR | **MISSING** | **0x0F** |

## Wire Format Bugs

### SUBSCRIBE (0x03) — `Forward` field missing from deserialize

Spec field order:
```
Request ID (i), Track Namespace (tuple), Track Name (b),
Subscriber Priority (8), Group Order (8), Forward (8), Filter Type (i),
[Start Location], [End Group], Parameters
```

Implementation serialize has `forward` but **deserialize skips it**
(subscribe.py line 179 jumps from group_order to filter_type).

### SUBSCRIBE_OK (0x04) — missing `Track Alias` field

Spec:
```
Request ID (i), Track Alias (i), Expires (i), Group Order (8),
Content Exists (8), [Largest Location], Parameters
```

Implementation is missing Track Alias entirely.

### SUBSCRIBE_ERROR (0x05) — stale `track_alias` field

Spec: `Request ID, Error Code, Error Reason` (3 fields only)
Implementation still has `track_alias` (removed in draft-14).

### SUBSCRIBE_UPDATE (0x02) — missing fields

Spec:
```
Request ID (i), Subscription Request ID (i), Start Location,
End Group (i), Subscriber Priority (8), Forward (8), Parameters
```

Implementation is missing **Subscription Request ID** and **Forward (8)**.

### TRACK_STATUS (0x0D) — completely wrong format

Spec says "identical to SUBSCRIBE message format".
Implementation has: `request_id, namespace, track_name, status_code, last_group_id, last_object_id`.

### TRACK_STATUS_OK (0x0E) — completely wrong format

Spec says "identical to SUBSCRIBE_OK message format".
Implementation has custom format with status_code, last_group/object fields.

### TRACK_STATUS_ERROR (0x0F) — not implemented

Spec says "identical to SUBSCRIBE_ERROR message format".

### PUBLISH (0x1D) — wrong type ID and wire format

Spec:
```
Request ID (i), Track Namespace (tuple), Track Name (b),
Track Alias (i), Group Order (8), Content Exists (8),
[Largest Location], Forward (8), Parameters
```

Implementation has type ID 0x1B and incomplete fields.

### PUBLISH_OK (0x1E) — wrong type ID and wire format

Spec:
```
Request ID (i), Forward (8), Subscriber Priority (8),
Group Order (8), Filter Type (i), [Start Location],
[End Group (i)], Parameters
```

Implementation has type ID 0x1C and incomplete fields.

### PUBLISH_ERROR (0x1F) — wrong type ID

Spec: `Request ID, Error Code, Error Reason`
Implementation has type ID 0x1D (should be 0x1F).

### FETCH (0x16) — missing Absolute Joining fetch type

Spec has 3 fetch types:
- Standalone (0x1)
- Relative Joining (0x2)
- **Absolute Joining (0x3)** — MISSING from implementation

FetchType enum only has FETCH=0x01 and JOINING_FETCH=0x02.

### FETCH_OK (0x18) — field naming

Spec: `Request ID, Group Order(8), End Of Track(8), End Location, Parameters`
Implementation uses `largest_group_id/largest_object_id` naming for End Location.
Wire format is functionally equivalent (two varints) but naming is wrong.

## Enum Value Bugs

### SubscribeErrorCode — all values wrong

| Code | Spec (draft-14) | Implementation |
|------|-----------------|----------------|
| 0x0 | INTERNAL_ERROR | INTERNAL_ERROR |
| 0x1 | UNAUTHORIZED | INVALID_RANGE |
| 0x2 | TIMEOUT | RETRY_TRACK_ALIAS |
| 0x3 | NOT_SUPPORTED | TRACK_DOES_NOT_EXIST |
| 0x4 | TRACK_DOES_NOT_EXIST | UNAUTHORIZED |
| 0x5 | INVALID_RANGE | TIMEOUT |
| 0x10 | MALFORMED_AUTH_TOKEN | (missing) |
| 0x12 | EXPIRED_AUTH_TOKEN | (missing) |

### SubscribeDoneCode (PUBLISH_DONE status) — shifted by 1

| Code | Spec (draft-14) | Implementation |
|------|-----------------|----------------|
| 0x0 | INTERNAL_ERROR | UNSUBSCRIBED (not in spec) |
| 0x1 | UNAUTHORIZED | INTERNAL_ERROR |
| 0x2 | TRACK_ENDED | UNAUTHORIZED |
| 0x3 | SUBSCRIPTION_ENDED | TRACK_ENDED |
| 0x4 | GOING_AWAY | SUBSCRIPTION_ENDED |
| 0x5 | EXPIRED | GOING_AWAY |
| 0x6 | TOO_FAR_BEHIND | EXPIRED |
| 0x7 | MALFORMED_TRACK | TOO_FAR_BEHIND |

### ObjectStatus — wrong values

| Code | Spec (draft-14) | Implementation |
|------|-----------------|----------------|
| 0x0 | Normal | NORMAL |
| 0x1 | Object Does Not Exist | DOES_NOT_EXIST |
| 0x3 | End of Group | END_OF_GROUP |
| 0x4 | End of Track | END_OF_TRACK_AND_GROUP (wrong name) |
| 0x5 | (not defined) | END_OF_TRACK (doesn't exist in spec) |

Note: spec skips 0x2 intentionally. Implementation's 0x04 should be END_OF_TRACK, not END_OF_TRACK_AND_GROUP.

### FilterType — missing NEXT_GROUP_START

| Code | Spec (draft-14) | Implementation |
|------|-----------------|----------------|
| 0x1 | Next Group Start | (MISSING) |
| 0x2 | Largest Object | LATEST_OBJECT |
| 0x3 | AbsoluteStart | ABSOLUTE_START |
| 0x4 | AbsoluteRange | ABSOLUTE_RANGE |

### ForwardingPreference — stale TRACK value

Spec (draft-14) only has Subgroup and Datagram.
Implementation still has TRACK=0x0 (removed in draft-14).

### SessionCloseCode — many missing codes

Missing from implementation:
- INVALID_REQUEST_ID (0x4)
- DUPLICATE_TRACK_ALIAS (0x5)
- KEY_VALUE_FORMATTING_ERROR (0x6)
- TOO_MANY_REQUESTS (0x7)
- INVALID_PATH (0x8)
- MALFORMED_PATH (0x9)
- AUTH_TOKEN_CACHE_OVERFLOW (0x13)
- DUPLICATE_AUTH_TOKEN_ALIAS (0x14)
- VERSION_NEGOTIATION_FAILED (0x15)
- MALFORMED_AUTH_TOKEN (0x16)
- UNKNOWN_AUTH_TOKEN_ALIAS (0x17)
- EXPIRED_AUTH_TOKEN (0x18)
- INVALID_AUTHORITY (0x19)
- MALFORMED_AUTHORITY (0x1A)

### SUBSCRIBE_NAMESPACE_ERROR codes — missing values

Missing: NAMESPACE_PREFIX_UNKNOWN (0x4), NAMESPACE_PREFIX_OVERLAP (0x5)

### FETCH_ERROR codes — missing values

Missing: NO_OBJECTS (0x6), INVALID_JOINING_REQUEST_ID (0x7),
UNKNOWN_STATUS_IN_RANGE (0x8), MALFORMED_TRACK (0x9)

## Data Stream Format Changes (major)

### SUBGROUP_HEADER type changed

- Spec: Type is a **range 0x10-0x1D** (12 variants encoding flags for
  end-of-group, extensions-present, subgroup-ID encoding)
- Implementation: Single type `DataStreamType.SUBGROUP_HEADER = 0x04`

The 12 type variants encode:

| Type | Subgroup ID | Extensions | End of Group |
|------|-------------|------------|--------------|
| 0x10 | 0 | No | No |
| 0x11 | 0 | Yes | No |
| 0x12 | First Obj ID | No | No |
| 0x13 | First Obj ID | Yes | No |
| 0x14 | Explicit | No | No |
| 0x15 | Explicit | Yes | No |
| 0x18 | 0 | No | Yes |
| 0x19 | 0 | Yes | Yes |
| 0x1A | First Obj ID | No | Yes |
| 0x1B | First Obj ID | Yes | Yes |
| 0x1C | Explicit | No | Yes |
| 0x1D | Explicit | Yes | Yes |

### OBJECT_DATAGRAM type changed

- Spec: Type is a **range 0x00-0x07, 0x20-0x21** (10 variants encoding flags)
- Implementation: `DatagramType.OBJECT_DATAGRAM = 0x01`, `OBJECT_DATAGRAM_STATUS = 0x02`

The 10 type variants encode:

| Type | End of Group | Extensions | Object ID | Status/Payload |
|------|-------------|------------|-----------|----------------|
| 0x00 | No | No | Yes | Payload |
| 0x01 | No | Yes | Yes | Payload |
| 0x02 | Yes | No | Yes | Payload |
| 0x03 | Yes | Yes | Yes | Payload |
| 0x04 | No | No | No | Payload |
| 0x05 | No | Yes | No | Payload |
| 0x06 | Yes | No | No | Payload |
| 0x07 | Yes | Yes | No | Payload |
| 0x20 | No | No | Yes | Status |
| 0x21 | No | Yes | Yes | Status |

### Object ID in subgroups is now a delta

- Spec: Object ID Delta (added to previous Object ID + 1)
- Implementation: Absolute Object ID

### FETCH_HEADER type ID

- Spec: 0x05 (matches implementation)

## What's Correct

These messages match the spec wire format:
- CLIENT_SETUP (0x20)
- SERVER_SETUP (0x21)
- GOAWAY (0x10)
- MAX_REQUEST_ID (0x15)
- REQUESTS_BLOCKED (0x1A)
- UNSUBSCRIBE (0x0A)
- PUBLISH_NAMESPACE (0x06)
- PUBLISH_NAMESPACE_OK (0x07)
- PUBLISH_NAMESPACE_ERROR (0x08)
- PUBLISH_NAMESPACE_DONE (0x09)
- PUBLISH_NAMESPACE_CANCEL (0x0C)
- SUBSCRIBE_NAMESPACE (0x11)
- SUBSCRIBE_NAMESPACE_OK (0x12)
- SUBSCRIBE_NAMESPACE_ERROR (0x13)
- UNSUBSCRIBE_NAMESPACE (0x14)
- FETCH_CANCEL (0x17)
- FETCH_ERROR (0x19) — wire format correct, error code enum incomplete

## Version Note

Implementation has `MOQT_CUR_VERSION = 0xff00000e` (draft-14).
Spec says draft-14 version number is `0xff00000E` (matches).

Setup parameter IMPLEMENTATION = 0x07 has comment noting draft-14 bug
(spec says 0x05, colliding with AUTHORITY; draft-15 fixes to 0x07). This
is correct forward-looking behavior.
