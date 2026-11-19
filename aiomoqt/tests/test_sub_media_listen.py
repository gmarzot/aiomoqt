"""sub_media -Q/-W: pub_media --pub-ns dials a listening sub_media, which
subscribes the catalog, audio and video over that session and can FETCH
back over it, over raw QUIC and WebTransport."""
import asyncio
import contextlib
import csv
import sys

import pytest

from aiomoqt.media import MediaSubscriber
from aiomoqt.server import MOQTServer
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests.test_media_locmaf import clip  # noqa: F401  (fixture)
from aiomoqt.tools import pub_media, sub_media

_BASE_PORT = 17440
_DRAFT = 18


def _argv(monkeypatch, tool, *argv):
    monkeypatch.setattr(sys, 'argv', [tool, *argv])
    return (pub_media if tool == 'pub_media' else sub_media).parse_args()


def _rows(report):
    with open(report) as fh:
        return {r['track']: r for r in csv.DictReader(fh)
                if r['kind'] == 'summary'}


async def _publish(args):
    # The listener's close ends the publisher's only session: exit 69.
    with contextlib.suppress(SystemExit):
        await pub_media.run(args)


async def _p2p(monkeypatch, clip, tmp_path, port, use_quic,  # noqa: F811
               pub_extra=(), on_catalog=None):
    """A listening sub_media --analyze with pub_media dialing it; its exit
    code and per-track summary rows."""
    listening = asyncio.Event()

    class _Server(MOQTServer):
        async def _up(self, serving):
            handle = await serving
            listening.set()
            return handle

        def serve(self):
            return self._up(super().serve())

        def serve_dual(self):
            return self._up(super().serve_dual())
    monkeypatch.setattr(sub_media, 'MOQTServer', _Server)
    if on_catalog is not None:
        start = MediaSubscriber.start

        async def started(self, *a, **kw):
            catalog = await start(self, *a, **kw)
            on_catalog(self)
            return catalog
        monkeypatch.setattr(MediaSubscriber, 'start', started)
    ns = f'p2p/{port}'
    report = tmp_path / f'analyze-{port}.csv'
    sub_args = _argv(monkeypatch, 'sub_media', '-Q' if use_quic else '-W',
                     '-p', str(port), '--bind', 'localhost', '--cert', CERT,
                     '--key', KEY, '--draft', str(_DRAFT), '-N', ns,
                     '--analyze', '-t', '3', '-i', '1', '--report',
                     str(report))
    url = f"{'moqt' if use_quic else 'https'}://localhost:{port}/"
    pub_args = _argv(monkeypatch, 'pub_media', url, '-k', '--pub-ns',
                     '--draft', str(_DRAFT), '-N', ns, '--mp4', clip,
                     '--loop', '--stats', '0', '--catalog-interval', '0.5',
                     *pub_extra)
    sub = asyncio.create_task(sub_media.run(sub_args))
    pub = None
    try:
        await asyncio.wait({sub, asyncio.ensure_future(listening.wait())},
                           timeout=10, return_when=asyncio.FIRST_COMPLETED)
        assert listening.is_set(), "sub_media never listened"
        pub = asyncio.create_task(_publish(pub_args))
        rc = await asyncio.wait_for(sub, 30)
    finally:
        for task in (sub, pub):
            if task is not None and not task.done():
                task.cancel()
        if pub is not None:
            await asyncio.gather(pub, return_exceptions=True)
    return rc, _rows(report)


@requires_certs
@pytest.mark.parametrize('use_quic', [True, False], ids=['quic', 'wt'])
async def test_publisher_dials_sub_media(clip, tmp_path,  # noqa: F811
                                         monkeypatch, use_quic):
    pytest.importorskip('av')
    rc, rows = await _p2p(monkeypatch, clip, tmp_path,
                          _BASE_PORT + (not use_quic), use_quic)
    assert rc == 0
    assert set(rows) == {'video', 'audio'}, rows
    for track, row in rows.items():
        assert int(row['objects']) > 0, (track, row)


@requires_certs
async def test_fetch_back_over_the_accepted_session(clip, tmp_path,  # noqa: F811
                                                    monkeypatch):
    pytest.importorskip('av')
    fetched = []
    tasks = []

    async def fetch(sub):
        await asyncio.sleep(1.5)
        ok = await sub.session.fetch(
            sub.namespace, 'video', start_group=0, end_group=0,
            wait_response=True,
            on_object=lambda obj, *_: fetched.append(obj))
        assert ok is not None

    def on_catalog(sub):
        tasks.append(asyncio.ensure_future(fetch(sub)))
    rc, rows = await _p2p(monkeypatch, clip, tmp_path, _BASE_PORT + 2, True,
                          pub_extra=['--fetch-history', '1048576'],
                          on_catalog=on_catalog)
    assert rc == 0 and tasks
    await asyncio.gather(*tasks)
    assert fetched, "FETCH delivered no objects"
    assert int(rows['video']['objects']) > 0


def test_url_and_listener_flags_are_rejected(monkeypatch):
    with pytest.raises(SystemExit):
        _argv(monkeypatch, 'sub_media', 'moqt://localhost:4433/', '-Q')


def test_neither_url_nor_listener_is_rejected(monkeypatch):
    with pytest.raises(SystemExit):
        _argv(monkeypatch, 'sub_media')


def test_listener_flags(monkeypatch):
    with pytest.raises(SystemExit):
        _argv(monkeypatch, 'sub_media', 'moqt://localhost:4433/',
              '--cert', CERT)
    args = _argv(monkeypatch, 'sub_media', '-Q', '-p', '4433', '-k')
    assert (args.url, args.listen, args.port) == (None, True, 4433)


class _Session:
    def __init__(self):
        self._moqt_session_setup = asyncio.get_running_loop().create_future()


@pytest.mark.parametrize('outcome,called', [
    (True, True), (False, False), (RuntimeError('setup failed'), False),
    (None, False)], ids=['ok', 'refused', 'raised', 'cancelled'])
async def test_session_handler_sees_only_completed_setups(outcome, called):
    loop = asyncio.get_running_loop()
    errors = []
    loop.set_exception_handler(lambda _loop, ctx: errors.append(ctx))
    try:
        server = MOQTServer(host='localhost', port=0, certificate=CERT,
                            private_key=KEY)
        seen = []
        server.register_session_handler(seen.append)
        session = server._adopt(_Session())
        fut = session._moqt_session_setup
        if outcome is None:
            fut.cancel()
        elif isinstance(outcome, Exception):
            fut.set_exception(outcome)
        else:
            fut.set_result(outcome)
        for _ in range(3):
            await asyncio.sleep(0)
        assert seen == ([session] if called else [])
        assert errors == []
    finally:
        loop.set_exception_handler(None)
