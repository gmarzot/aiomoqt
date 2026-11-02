"""pub_media --fetch-history: its video and audio LOC tracks answer a
standalone FETCH through aiomoqt-relay over raw QUIC and WebTransport;
without the flag the FETCH is refused with NOT_SUPPORTED."""
import asyncio
import contextlib

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.context import canonical_request_error
from aiomoqt.messages.params import LocationFilter
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests.test_media_locmaf import clip  # noqa: F401  (fixture)
from aiomoqt.tests.test_pub_media_frindell import _argv
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tools import pub_media
from aiomoqt.types import FilterType, MOQTRequestError, RequestErrorCode

_BASE_PORT = 16680
_TRACKS = ('video', 'audio')


@contextlib.asynccontextmanager
async def _publishing(monkeypatch, clip, port, draft, use_quic,  # noqa: F811
                      history):
    """pub_media through the relay, a subscriber on both tracks. The
    relay caches nothing, so every FETCH reaches pub_media."""
    monkeypatch.setattr(relay, 'CACHE_BYTES', 0)
    relay._announced.clear()
    relay._tracks.clear()
    handle = await relay._build_server('localhost', port, CERT, KEY,
                                       use_quic=use_quic, draft=draft).serve()
    url = f"{'moqt' if use_quic else 'https'}://localhost:{port}/"
    ns = f'fetchhist/{port}'
    args = _argv(monkeypatch, 'pub_media', url, '-k', '--draft', str(draft),
                 '-t', '8', '--stats', '0', '--pub-ns', '-N', ns,
                 '--mp4', clip, '--loop', '--fetch-history', str(history))
    pub = asyncio.create_task(pub_media.run(args))
    try:
        for _ in range(200):
            if relay._announced or pub.done():
                break
            await asyncio.sleep(0.05)
        assert relay._announced, "publisher never announced"
        client = MOQTClient('localhost', port, path='/', use_quic=use_quic,
                            verify_tls=False, supported_drafts=draft)
        async with client.connect() as rx:
            await rx.client_session_init()
            for name in _TRACKS:
                await rx.subscribe(ns, name, forward=1,
                                   filter_type=FilterType.LATEST_OBJECT,
                                   wait_response=True)
            # One key frame a second: groups 0 and 1 are complete.
            await asyncio.sleep(3.5)
            yield rx, ns
            assert rx._close_err is None
    finally:
        pub.cancel()
        with contextlib.suppress(BaseException):
            await pub
        handle.close()
        relay._announced.clear()
        relay._tracks.clear()


def _groups_0_1(draft):
    if draft >= 20:
        return {'location_filter': LocationFilter.absolute(0, 0, 1)}
    return {'start_group': 0, 'start_object': 0, 'end_group': 1,
            'end_object': 0}


@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
@pytest.mark.parametrize('draft', [18, 22])
async def test_media_tracks_answer_fetch_from_history(
        clip, monkeypatch, draft, use_quic):  # noqa: F811
    pytest.importorskip('av')
    port = _BASE_PORT + 2 * [18, 22].index(draft) + (not use_quic)
    got = {}
    async with _publishing(monkeypatch, clip, port, draft, use_quic,
                           1 << 20) as (rx, ns):
        rx.on_fetch_object = lambda obj, _n, _ts, rid: got.setdefault(
            rid, []).append((obj.group_id, obj.object_id))
        for name in _TRACKS:
            ok = await asyncio.wait_for(rx.fetch(
                ns, name, wait_response=True, **_groups_0_1(draft)), 10)
            assert await rx.await_fetch_done(ok.request_id, timeout=8), name
            objs = got.get(ok.request_id, [])
            assert {g for g, _ in objs} == {0, 1}, (name, objs)
            for g in (0, 1):
                ids = [o for gg, o in objs if gg == g]
                assert ids == list(range(len(ids))), (name, g, ids)
            if name == 'video':
                assert len(objs) >= 2 * 25, objs


@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_media_tracks_refuse_fetch_without_history(
        clip, monkeypatch, use_quic):  # noqa: F811
    pytest.importorskip('av')
    port = _BASE_PORT + 4 + (not use_quic)
    async with _publishing(monkeypatch, clip, port, 18, use_quic,
                           0) as (rx, ns):
        for name in _TRACKS:
            with pytest.raises(MOQTRequestError) as err:
                await asyncio.wait_for(rx.fetch(
                    ns, name, wait_response=True, **_groups_0_1(18)), 10)
            assert err.value.response is not None, "timed out, no reply"
            assert canonical_request_error(
                err.value.error_code, rx._profile, fetch=True) == \
                RequestErrorCode.NOT_SUPPORTED, name
