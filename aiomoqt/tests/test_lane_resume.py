"""Forward State 0 then 1 on one of two peers of produce() tracks: with
a FETCH history holding the open group's head, that peer continues the
group at its next object on a new stream (FIRST_OBJECT clear); without
one it waits for the next group. The other peer sees no reset and no
extra stream. Two tracks per session, over raw QUIC and WebTransport."""
import asyncio
import contextlib

import pytest

from aiomoqt.track import PublishedTrack

from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_multi_track_publish import (_Receiver, _loopback,
                                                    _until)

pytestmark = requires_certs

_PORT = 27850
_DRAFTS = [16, 18, 22]
_NS = "lane/resume"
# Objects per group, per track.
_GROUP = {"video": 6, "audio": 3}


class _Stepped(PublishedTrack):
    """Writes the Locations the test queues, through produce()."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.steps: asyncio.Queue = asyncio.Queue()

    async def produce(self, out):
        while True:
            group_id, object_id = await self.steps.get()
            await out.write(group_id, object_id,
                            b"%d.%d" % (group_id, object_id),
                            group_start=object_id == 0)
            self.steps.task_done()


async def _feed(tracks, first, last):
    """Objects first..last of a running count on every track."""
    for n in range(first, last + 1):
        for name, track in tracks.items():
            track.steps.put_nowait(divmod(n, _GROUP[name]))
    for track in tracks.values():
        await asyncio.wait_for(track.steps.join(), 5.0)


def _locs(name, first, last):
    return [divmod(n, _GROUP[name]) for n in range(first, last + 1)]


async def _forward(rx, oks, tracks, value):
    """Set peer A's Forward State on every track; wait until applied."""
    for name in tracks:
        rx.session.request_update(oks[name].request_id, forward=value)
    assert await _until(lambda: all(
        t.subscriptions[0].forward == bool(value) for t in tracks.values()))


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", _DRAFTS)
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
@pytest.mark.parametrize("history", [True, False], ids=["history", "none"])
async def test_one_peer_resumes_mid_group(history, use_quic, draft):
    port = _PORT + 2 * (_DRAFTS.index(draft) + 3 * (not use_quic)
                        + 6 * (not history))
    rx_a, rx_b = _Receiver(), _Receiver()
    async with contextlib.AsyncExitStack() as stack:
        a = await stack.enter_async_context(
            _loopback(port, draft, rx_a, use_quic=use_quic))
        b = await stack.enter_async_context(
            _loopback(port + 1, draft, rx_b, use_quic=use_quic))
        tracks = {}
        for name in _GROUP:
            track = _Stepped(a, namespace=_NS, trackname=name,
                             fetch_history_bytes=(1 << 20) if history else 0)
            track.add_session(b)
            track.attach()
            track.attach(b)
            tracks[name] = track
        resets = {a: [], b: []}
        for s in (a, b):
            def spy(sid, code, s=s, reset=s.stream_reset):
                resets[s].append(sid)
                return reset(sid, code)
            s.stream_reset = spy
            await s.publish_namespace(namespace=_NS, wait_response=True)

        got = {rx: {name: [] for name in _GROUP} for rx in (rx_a, rx_b)}
        oks = {rx: {} for rx in (rx_a, rx_b)}
        for rx in (rx_a, rx_b):
            for name in _GROUP:
                ok = await rx.session.subscribe(_NS, name, forward=1,
                                                wait_response=True)
                oks[rx][name] = ok

                def on_object(msg, _n, _ts, g, _sg, rec=got[rx][name]):
                    if msg.payload:
                        rec.append((g, msg.object_id, msg.stream_flags[0]))
                rx.session.register_object_handler(ok.track_alias, on_object)
        assert await _until(lambda: all(
            s.generating for t in tracks.values() for s in t.subscriptions))

        def through(rx, n):
            return all(got[rx][name] and got[rx][name][-1][:2]
                       == divmod(n, _GROUP[name]) for name in _GROUP)

        await _feed(tracks, 0, 7)
        assert await _until(lambda: through(rx_a, 7)), got[rx_a]
        await _forward(rx_a, oks[rx_a], tracks, 0)
        # Video stops at 1.3, audio at 3.0: both mid-group.
        await _feed(tracks, 8, 9)
        assert await _until(lambda: through(rx_b, 9)), got[rx_b]
        await _forward(rx_a, oks[rx_a], tracks, 1)
        await _feed(tracks, 10, 17)
        assert await _until(lambda: through(rx_a, 17) and through(rx_b, 17))
        await asyncio.sleep(0.1)

        flags = draft >= 18   # FIRST_OBJECT is on the wire from d18
        for name in _GROUP:
            b_got = got[rx_b][name]
            assert [loc[:2] for loc in b_got] == _locs(name, 0, 17)
            if flags:
                assert all(first for *_, first in b_got)
            assert tracks[name].subscriptions[1].delivery.stream_count == (
                17 // _GROUP[name] + 1)

            a_got = got[rx_a][name]
            resumed = 10 if history else 12
            assert [loc[:2] for loc in a_got] == (
                _locs(name, 0, 7) + _locs(name, resumed, 17)), name
            g, o, _ = a_got[8]
            if history:
                # The open group again, on a new stream from its next object.
                assert (g, o) == divmod(10, _GROUP[name]) and o > 0
            if flags:
                # stream_flags carries the stream header's FIRST_OBJECT.
                assert all(first == (gid != g or not history)
                           for gid, _, first in a_got[8:])
        assert resets[b] == []
        # One cut per track at Forward State 0, none at the resume.
        assert len(resets[a]) == len(_GROUP)
        assert rx_a.session._close_err is None
        assert rx_b.session._close_err is None
