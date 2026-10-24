"""Relay cache bound with non-consecutive group IDs: CACHE_GROUPS counts
groups held, so a publisher numbering groups by timestamp keeps its
previous groups cached for fills and FETCH. Two tracks per session."""
from aiomoqt.messages import SubgroupHeader
from aiomoqt.messages.params import D20ParamType, LocationFilter
from aiomoqt.tests import test_relay_fill as fill
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.track import PublishedTrack
from aiomoqt.types import ParamType, RequestErrorCode

from aiomoqt.tests._certs import requires_certs

pytestmark = requires_certs

_PORT = 19960
_NS = "relay/sparse"
_SIZES = {"meta": 120, "info": 60}
_BASE = 1_792_805_227_259
_STEP = 4_000


def test_cache_holds_the_newest_groups_when_ids_skip():
    t = relay._RelayedTrack(((b"relay", b"sparse"), b"meta"))
    groups = [_BASE + k * _STEP for k in range(10)]
    for g in groups:
        for oid in (0, 1):
            t._remember(g, 0, oid, b"x", None, 128, None)
    kept = groups[-relay.CACHE_GROUPS:]
    assert sorted({c[0] for c in t._cache}) == kept
    assert sorted(t._groups) == kept
    assert t.cache_covers((kept[0], 0))
    assert not t.cache_covers((groups[-relay.CACHE_GROUPS - 1], 1))


class _Catalog(PublishedTrack):
    """Like g5-broadcast's catalog: timestamp group IDs, a new group per
    SUBSCRIBE, and FETCH refused outside the retained group, which is
    the current one once a FETCH has been answered."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.current = _BASE
        self.retained = {_BASE}
        self.fetched = []
        self._remember(_BASE, 0, self._payload())

    def _payload(self):
        return b"c" * _SIZES[self.trackname]

    async def _start_generating(self, session, trigger):
        sub = self._sub_for(session)
        self.current += _STEP
        self.retained.add(self.current)
        self._remember(self.current, 0, self._payload())
        sid = await session.open_uni_stream()
        hdr = SubgroupHeader(track_alias=sub.track_alias,
                             group_id=self.current, subgroup_id=0,
                             publisher_priority=128, extensions_present=True,
                             prof=session._profile)
        session.stream_write(sid, hdr.serialize().data)
        await session.stream_write_drain(sid, hdr.next_object(
            payload=self._payload(), extensions=None, object_id=0).data)

    async def _on_fetch(self, session, msg):
        group = msg.location_filter.start_group
        self.fetched.append(group)
        if group not in self.retained:
            session.fetch_error(
                request_id=msg.request_id,
                error_code=RequestErrorCode.INVALID_RANGE,
                reason="only the current catalog group is retained")
            return
        self.retained = {self.current}
        await super()._on_fetch(session, msg)


@fill._TRANSPORTS
async def test_fill_and_fetch_of_a_previous_sparse_group_come_from_cache(
        use_quic):
    async with fill._relay(_PORT + (not use_quic), use_quic) as connect:
        pub = await connect()
        tracks = {n: _Catalog(pub, namespace=_NS, trackname=n)
                  for n in _SIZES}
        for t in tracks.values():
            t.attach()
        await pub.publish_namespace(namespace=_NS, wait_response=True)
        rx = fill._Rx(await connect())
        current = {ParamType.LOCATION_FILTER: LocationFilter.relative(1)}
        oks = {n: await rx.session.subscribe(
            _NS, n, location_filter=LocationFilter.next_object(),
            parameters={D20ParamType.FILL_PARAMETERS: current},
            wait_response=True) for n in _SIZES}
        for name, ok in oks.items():
            assert (ok.largest_group_id, ok.largest_object_id) == (_BASE, 0)
            assert await rx.done(ok.request_id) == (True, [(_BASE, 0)])
            assert rx.sizes(ok.request_id) == {_SIZES[name]}
            relayed = relay._tracks[((b"relay", b"sparse"), name.encode())]
            assert relayed._fetch_bytes["cache"] == _SIZES[name]
            assert tracks[name].fetched.count(_BASE) <= 1
        # The previous group, _STEP IDs below the live one.
        for name in _SIZES:
            ok = await rx.session.fetch(
                _NS, name, location_filter=LocationFilter.absolute(
                    _BASE, 0, _BASE, 0), wait_response=True)
            assert await rx.done(ok.request_id) == (True, [(_BASE, 0)])
            assert rx.sizes(ok.request_id) == {_SIZES[name]}
            assert tracks[name].fetched.count(_BASE) <= 1
        assert rx.session._close_err is None
