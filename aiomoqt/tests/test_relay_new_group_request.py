"""aiomoqt-relay forwards NEW_GROUP_REQUEST (d18 §10.2.13, d22 §9.20.19):
in its upstream SUBSCRIBE when it holds no subscription, else in a
REQUEST_UPDATE when the value is 0 or above the Largest Group, the track
declares DYNAMIC_GROUPS=1 and no equal or larger request is outstanding."""
import asyncio
from types import SimpleNamespace

import pytest

from aiomoqt.context import profile_for
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import ParamType

from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_relay_send_lanes import _relay, relay_tables

assert relay_tables  # fixture, used by name

_BASE_PORT = 29300
_NS = ("ngr", "live")
_NGR = ParamType.NEW_GROUP_REQUEST
_DYNAMIC = {int(ParamType.DYNAMIC_GROUPS): 1}


class _Origin(PublishedTrack):
    """A publisher recording the NEW_GROUP_REQUESTs it receives."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []

    def _take_timeouts(self, sub, params, prof):
        if params and _NGR in params:
            self.requests.append(params[_NGR])
        super()._take_timeouts(sub, params, prof)


# -- decisions against a fake upstream ------------------------------------

class _Upstream:
    def __init__(self):
        self._profile = profile_for(18)
        self.sent = []

    def request_update(self, rid, forward=None, parameters=None,
                       wait_response=False, **_):
        self.sent.append((rid, parameters[_NGR]))


def _track(dynamic=True, largest=(4, 2)):
    track = relay._RelayedTrack(("ns", "t"))
    up = _Upstream()
    up._moqt_session_closed = asyncio.get_running_loop().create_future()
    track.upstream = SimpleNamespace(session=up, request_id=7)
    track.properties = dict(_DYNAMIC) if dynamic else {}
    track.note_largest(largest)
    return track, up


async def test_zero_or_above_largest_goes_upstream_once_outstanding():
    track, up = _track()
    relay._forward_new_group(track, {_NGR: 0})
    # Two viewers asking while the first is outstanding: one request.
    relay._forward_new_group(track, {_NGR: 0})
    assert up.sent == [(7, 0)]
    relay._forward_new_group(track, {_NGR: 6})
    relay._forward_new_group(track, {_NGR: 6})
    relay._forward_new_group(track, {_NGR: 5})
    assert up.sent == [(7, 0), (7, 6)]
    # Outstanding until the Largest Group increases.
    track.note_largest((4, 9))
    relay._forward_new_group(track, {_NGR: 6})
    assert len(up.sent) == 2
    track.note_largest((5, 0))
    relay._forward_new_group(track, {_NGR: 6})
    assert up.sent[-1] == (7, 6) and len(up.sent) == 3


@pytest.mark.parametrize("value", [1, 4])
async def test_at_or_below_largest_is_not_forwarded(value):
    track, up = _track()
    relay._forward_new_group(track, {_NGR: value})
    assert up.sent == []


async def test_without_dynamic_groups_nothing_is_forwarded():
    track, up = _track(dynamic=False)
    relay._forward_new_group(track, {_NGR: 0})
    relay._forward_new_group(track, {_NGR: 9})
    assert up.sent == []


async def test_no_parameter_sends_nothing():
    track, up = _track()
    relay._forward_new_group(track, {})
    relay._forward_new_group(track, None)
    assert up.sent == []


# -- over the wire ---------------------------------------------------------

async def _wait(pred, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end and not pred():
        await asyncio.sleep(0.02)
    return pred()


class _Viewer:
    def __init__(self, session):
        self.session = session
        self.objects = {}

    def counter(self, name):
        self.objects[name] = 0

        def on_object(msg, *_):
            self.objects[name] += 1
        return on_object

    async def subscribe(self, name, ngr=None):
        if ngr is None:
            track = SubscribedTrack(self.session, _NS, name,
                                    on_object=self.counter(name))
            await track.subscribe(timeout=5)
            return track.request_id
        ok = await self.session.subscribe(
            namespace=_NS, track_name=name, wait_response=True,
            parameters={_NGR: ngr}, on_object=self.counter(name))
        return ok.request_id


@requires_certs
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_established_tracks_forward_in_request_update(
        use_quic, draft, relay_tables):
    port = _BASE_PORT + 4 * (draft == 22) + 2 * (not use_quic)
    async with _relay(port, use_quic, draft) as connect:
        pub = await connect()
        # Long groups: the Largest Group holds still for the test.
        origins = {name: _Origin(pub, namespace=_NS, trackname=name,
                                 object_size=500, group_size=10_000,
                                 rate=20, track_properties=props)
                   for name, props in (("video", _DYNAMIC),
                                       ("audio", _DYNAMIC),
                                       ("static", None))}
        for o in origins.values():
            # Numbering starts at group 7, so 0 < values <= Largest exist.
            o._largest = (6, 0)
            o.attach()
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        a, b, c = [_Viewer(await connect()) for _ in range(3)]
        for name in origins:
            await a.subscribe(name)
        key = {n: (relay._ns_tuple(_NS), n.encode()) for n in origins}
        assert await _wait(lambda: all(
            relay._tracks[k].largest is not None for k in key.values()))
        largest = relay._tracks[key["video"]].largest[0]
        assert largest == 7
        assert all(o.requests == [] for o in origins.values())

        # Two viewers send 0 to video: one request goes upstream.
        await b.subscribe("video", ngr=0)
        c_rid = await c.subscribe("video", ngr=0)
        # Above the Largest Group on audio; any value on a track without
        # DYNAMIC_GROUPS stays here.
        await b.subscribe("audio", ngr=largest + 3)
        await b.subscribe("static", ngr=0)
        assert await _wait(lambda: origins["video"].requests
                           and origins["audio"].requests)
        await asyncio.sleep(0.3)
        assert origins["video"].requests == [0]
        assert origins["audio"].requests == [largest + 3]
        assert origins["static"].requests == []

        # REQUEST_UPDATE: at or below the Largest Group stays here; above
        # the outstanding request goes up.
        for value in (3, largest, largest + 5):
            c.session.request_update(c_rid, parameters={_NGR: value})
        assert await _wait(lambda: len(origins["video"].requests) == 2)
        await asyncio.sleep(0.3)
        assert origins["video"].requests == [0, largest + 5]
        assert relay._tracks[key["video"]].largest[0] == largest

    for v in (a, b):
        assert all(n > 0 for n in v.objects.values()), v.objects


@requires_certs
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_the_first_subscribe_carries_it_upstream(
        use_quic, draft, relay_tables):
    port = _BASE_PORT + 50 + 4 * (draft == 22) + 2 * (not use_quic)
    async with _relay(port, use_quic, draft) as connect:
        pub = await connect()
        origins = {name: _Origin(pub, namespace=_NS, trackname=name,
                                 object_size=500, group_size=10_000,
                                 rate=20, track_properties=props)
                   for name, props in (("video", _DYNAMIC),
                                       ("audio", None))}
        for o in origins.values():
            o.attach()
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        a, b = _Viewer(await connect()), _Viewer(await connect())
        # No upstream subscription yet: the upstream SUBSCRIBE carries it,
        # with or without DYNAMIC_GROUPS (only a REQUEST_UPDATE needs it).
        await a.subscribe("video", ngr=0)
        await a.subscribe("audio", ngr=4)
        await b.subscribe("video")
        assert await _wait(lambda: all(o.requests
                                       for o in origins.values()))
        await asyncio.sleep(0.3)
        assert origins["video"].requests == [0]
        assert origins["audio"].requests == [4]
        assert await _wait(lambda: all(a.objects.values())
                           and b.objects["video"])
