# CAT auth flows

How Common Access Tokens move through MoQT sessions on aiomoqt-relay and moqx. Both verify
the same token format, so they can share a key. The relay is the only enforcer: it checks the
SETUP token, then (from d16) each request against that token's grants or a token on the
request. Publishers and catalogs enforce nothing.

## Session-only

Each client started with `--auth-key KID:SECRET` mints its own token: every action, any
namespace and track, no expiry. Suited to test rigs and trusted clients: whoever holds the
secret can mint any token, and only a key change revokes one.

```mermaid
sequenceDiagram
    participant P as Publisher<br/>--auth-key
    participant R as Relay<br/>--auth-key, same key
    participant S as Subscriber<br/>--auth-key

    P->>R: CLIENT_SETUP + token (all actions, no exp)
    R->>R: verify MAC, token type and expiry, keep grants
    R-->>P: SERVER_SETUP
    P->>R: PUBLISH_NAMESPACE live/event
    R-->>P: OK (session grants)

    S->>R: CLIENT_SETUP + token
    alt token verifies and grants CLIENT_SETUP
        R-->>S: SERVER_SETUP
    else missing, wrong key or wrong token type
        R--xS: close UNAUTHORIZED (0x2)
    else expired
        R--xS: close EXPIRED_AUTH_TOKEN (0x18)
    else unparseable
        R--xS: close MALFORMED_AUTH_TOKEN (0x16)
    end
    S->>R: SUBSCRIBE catalog, video, audio
    R->>P: SUBSCRIBE (upstream)
    P-->>R: objects
    R-->>S: objects
```

## Narrow sessions, track tokens named by the catalog

An issuer holds the key and scopes each token. The publisher's session covers its namespace;
a viewer's session covers only the catalog. A catalog track with `authInfo` tells the viewer
to get a token for it before subscribing; the relay never reads the catalog, it only checks
the token the viewer attaches.

```mermaid
sequenceDiagram
    participant I as Token issuer<br/>holds the key
    participant P as Publisher
    participant R as Relay
    participant A as Viewer A
    participant B as Viewer B

    I-->>P: session token: CLIENT_SETUP,<br/>PUBLISH_NAMESPACE, PUBLISH on live/event
    P->>R: CLIENT_SETUP + session token
    R-->>P: SERVER_SETUP
    P->>R: PUBLISH_NAMESPACE live/event
    R-->>P: OK (session grants)

    I-->>A: session token: CLIENT_SETUP,<br/>SUBSCRIBE and FETCH on live/event catalog
    A->>R: CLIENT_SETUP + session token
    R-->>A: SERVER_SETUP
    A->>R: SUBSCRIBE + joining FETCH catalog
    R->>P: SUBSCRIBE catalog
    P-->>R: catalog
    R-->>A: catalog
    Note over A: video has authInfo:<br/>a token is required
    A->>I: request a video token (login, entitlement)
    I-->>A: track token: SUBSCRIBE on live/event video
    A->>R: SUBSCRIBE video + track token
    R->>R: session grants miss, request token covers
    R->>P: SUBSCRIBE video
    P-->>R: video objects
    R-->>A: video objects

    Note over B: same catalog-only session,<br/>no video token
    B->>R: SUBSCRIBE video
    R-->>B: refused, UNAUTHORIZED (1), session stays up
    Note over R,B: refused although video already flows to A
```

A bearer token works for whoever presents it: a token placed in the catalog itself protects
the track only as far as reading the catalog is restricted.

## The relay's check on each request

Checked from d16: SUBSCRIBE, FETCH, SUBSCRIBE_NAMESPACE, SUBSCRIBE_TRACKS, TRACK_STATUS, PUBLISH
and PUBLISH_NAMESPACE. A token covers a request when it verifies, has not expired, and grants
the action on the request's namespace and track.

```mermaid
flowchart LR
    Q[request] --> G{"SETUP grants<br/>cover it?"}
    G -- yes --> OK[accept]
    G -- no --> T{"token on the request<br/>covers it?"}
    T -- yes --> OK
    T -- no --> NO["refuse, UNAUTHORIZED (1)<br/>session stays up"]
```

- A joining FETCH is checked against the track of the subscription it joins.
- REQUEST_UPDATE is not checked; at d14 only SETUP is.
- A running subscription is not re-checked when its token expires.
- moqx also accepts a request its `anonymous_claim` covers, and ignores request tokens under
  `allow_request_token_override: false`; aiomoqt-relay has neither setting.

aiomoqt's tools mint session tokens only; `MediaSubscriber` keeps `authInfo` but does not act
on it. From the library, pass a track token as a request parameter:

```python
from aiomoqt.cat import ACTION_SUBSCRIBE, CatAuth
from aiomoqt.types import ParamType

issuer = CatAuth("cat-dev", secret)  # runs at the issuer, not in the viewer
token = issuer.mint([ACTION_SUBSCRIBE], namespace="live/event", track="video")
await session.subscribe("live/event", "video",
                        parameters={ParamType.AUTH_TOKEN: token}, wait_response=True)
```
