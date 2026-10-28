"""aiomoqt-relay stops by draining: GOAWAY to every session, with
--goaway-uri as the New Session URI, a wait of up to --drain-timeout for
sessions to leave, GOAWAY_TIMEOUT for the rest, then exit. pub_media
--reconnect follows a GOAWAY at once, to the URI when given."""
import asyncio
import contextlib
import logging
import signal
import types

import pytest

from aiopquic.asyncio.webtransport import WebTransportError

from aiomoqt.client import MOQTClient
from aiomoqt.protocol import _MOQTSessionMixin
from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._procs import free_udp_port, spawn, stop, wait_for
from aiomoqt.tests.test_pub_media_frindell import _argv
from aiomoqt.tests.test_pub_media_mtt import (  # noqa: F401  (fixtures)
    arrivals, h264_aac,
)
from aiomoqt.tests.test_pub_media_service import (_groups, _pub_events,
                                                  _serve, _stop, _until, _url)
from aiomoqt.tools import moq_interop_relay as relay
from aiomoqt.tools import pub_media, sub_media
from aiomoqt.track import PublishedTrack, SubscribedTrack
from aiomoqt.types import MOQTException, SessionCloseCode

pytestmark = requires_certs

_BASE_PORT = 27100
_NS = ("drain", "live")
_DRAIN_S = 1.0


def _clear_relay():
    for table in (relay._announced, relay._announce_requests, relay._tracks,
                  relay._sessions, relay._catalogs):
        table.clear()
    relay._ns_subs.clear()
    relay._track_subs.clear()


@pytest.fixture
def relay_tables():
    _clear_relay()
    yield
    _clear_relay()


async def _connect(stack, port, use_quic, draft):
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    s = await stack.enter_async_context(client.connect())
    await s.client_session_init()
    return s


async def _publish_and_subscribe(stack, port, use_quic, draft):
    """A publisher and a subscriber through the relay, objects flowing."""
    pub = await _connect(stack, port, use_quic, draft)
    PublishedTrack(pub, namespace=_NS, trackname="video", object_size=256,
                   group_size=10, rate=50).attach()
    await pub.publish_namespace(namespace=_NS, wait_response=True)
    sub = await _connect(stack, port, use_quic, draft)
    got = []
    track = SubscribedTrack(sub, _NS, "video",
                            on_object=lambda msg, *a: got.append(msg))
    await track.subscribe(timeout=5)
    await _until(lambda: got, 5)
    return pub, sub, got


@pytest.mark.parametrize("draft", [16, 18, 22])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_drain_sends_goaway_and_times_out(use_quic, draft,
                                                relay_tables, caplog):
    caplog.set_level(logging.INFO, logger="aiomoqt.relay")
    port = _BASE_PORT + 2 * [16, 18, 22].index(draft) + (not use_quic)
    uri = f"moqt://relay-next.example:{port}/"
    srv, handle = await _serve(port, use_quic, draft)
    loop = asyncio.get_running_loop()
    try:
        async with contextlib.AsyncExitStack() as stack:
            pub, sub, got = await _publish_and_subscribe(
                stack, port, use_quic, draft)
            seen = {"pub": [], "sub": []}

            def leave(msg, rid):
                seen["pub"].append((msg, rid))
                loop.call_soon(pub.close)
            pub.on_goaway = leave
            sub.on_goaway = lambda msg, rid: seen["sub"].append((msg, rid))
            t0 = loop.time()
            drain = asyncio.ensure_future(
                relay._drain([srv], uri, _DRAIN_S, "SIGTERM"))
            await asyncio.sleep(0.3)
            # A session arriving while the relay drains is refused.
            late = None
            # Refused mid-setup, a WT client raises CancelledError.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                late = await _connect(stack, port, use_quic, draft)
            if late is not None:
                await asyncio.wait_for(late.async_closed(), 1)
            await asyncio.wait_for(drain, _DRAIN_S + 2)
            elapsed = loop.time() - t0
            await asyncio.wait_for(sub.async_closed(), 2)
    finally:
        await _stop(srv, handle)
    assert got, "no objects before the drain"
    for who in ("pub", "sub"):
        (msg, rid), = seen[who]
        assert rid is None
        assert msg.new_session_uri == uri
        if draft >= 18:
            assert msg.timeout == int(_DRAIN_S * 1000)
    assert _DRAIN_S <= elapsed < _DRAIN_S + 1.5, elapsed
    assert sub._close_err[0] == SessionCloseCode.GOAWAY_TIMEOUT, sub._close_err
    text = caplog.text
    assert "relay: SIGTERM: draining 2 sessions, GOAWAY to " + uri in text
    assert "relay: drain timed out, 1 sessions left" in text


@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_drain_ends_when_sessions_leave(use_quic, relay_tables,
                                              caplog):
    caplog.set_level(logging.INFO, logger="aiomoqt.relay")
    port = _BASE_PORT + 10 + (not use_quic)
    srv, handle = await _serve(port, use_quic, 18)
    loop = asyncio.get_running_loop()
    try:
        async with contextlib.AsyncExitStack() as stack:
            pub, sub, got = await _publish_and_subscribe(
                stack, port, use_quic, 18)
            uris = []
            for s in (pub, sub):
                s.on_goaway = (lambda s: lambda msg, rid: (
                    uris.append(msg.new_session_uri),
                    loop.call_soon(s.close)))(s)
            t0 = loop.time()
            await relay._drain([srv], "", 5.0, "SIGINT")
            elapsed = loop.time() - t0
    finally:
        await _stop(srv, handle)
    assert got, "no objects before the drain"
    assert uris == ["", ""]
    assert elapsed < 1.0, elapsed
    assert ("relay: SIGINT: draining 2 sessions, GOAWAY to this address"
            in caplog.text)
    assert "relay: drained, all sessions gone" in caplog.text


# -- the relay process ----------------------------------------------------------

@pytest.mark.parametrize("transport", ["quic", "wt"])
async def test_relay_process_drains_on_sigterm(tmp_path, transport,
                                               relay_tables):
    port = free_udp_port()
    use_quic = transport == "quic"
    log = tmp_path / "relay.log"
    uri = "https://relay-next.example/moq"
    proc = spawn("moq_interop_relay", "--bind", "127.0.0.1", "--port", port,
                 "--dual", "--cert", CERT, "--key", KEY, "--draft", 18,
                 "--drain-timeout", 2, "--goaway-uri", uri, log=log)
    loop = asyncio.get_running_loop()
    try:
        wait_for(log, "Listening on", proc)
        async with contextlib.AsyncExitStack() as stack:
            pub, sub, got = await _publish_and_subscribe(
                stack, port, use_quic, 18)
            seen = []
            for s in (pub, sub):
                s.on_goaway = lambda msg, rid: seen.append(msg)
            t0 = loop.time()
            proc.send_signal(signal.SIGTERM)
            rc = await asyncio.to_thread(proc.wait, 10)
            elapsed = loop.time() - t0
    finally:
        stop(proc)
    out = log.read_text(errors="replace")
    assert got, "no objects before the drain"
    assert [m.new_session_uri for m in seen] == [uri, uri], out
    assert rc == 0, out
    assert 2.0 <= elapsed < 4.0, elapsed
    assert f"relay: SIGTERM: draining 2 sessions, GOAWAY to {uri}" in out
    assert "relay: drain timed out, 2 sessions left" in out
    assert "relay: exit" in out
    assert "Traceback" not in out, out


async def test_relay_process_second_signal_exits_at_once(tmp_path,
                                                         relay_tables):
    port = free_udp_port()
    log = tmp_path / "relay.log"
    proc = spawn("moq_interop_relay", "--bind", "127.0.0.1", "--port", port,
                 "--quic", "--cert", CERT, "--key", KEY, "--draft", 18,
                 "--drain-timeout", 30, log=log)
    loop = asyncio.get_running_loop()
    try:
        wait_for(log, "Listening on", proc)
        async with contextlib.AsyncExitStack() as stack:
            sub = await _connect(stack, port, True, 18)
            seen = []
            sub.on_goaway = lambda msg, rid: seen.append(msg)
            t0 = loop.time()
            proc.send_signal(signal.SIGTERM)
            await _until(lambda: seen, 5)
            proc.send_signal(signal.SIGINT)
            rc = await asyncio.to_thread(proc.wait, 10)
            elapsed = loop.time() - t0
    finally:
        stop(proc)
    out = log.read_text(errors="replace")
    assert rc == 0, out
    assert elapsed < 3.0, elapsed
    assert "relay: SIGINT again, exiting now" in out
    assert "relay: exit" in out
    assert "Traceback" not in out, out


async def _dial_refused(port, use_quic, draft):
    """Dial and wait for the close: (seconds to it, setup completed)."""
    loop = asyncio.get_running_loop()
    client = MOQTClient("127.0.0.1", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    setup = False
    t0 = loop.time()
    # Only whether setup completed matters; a refused setup raises.
    with contextlib.suppress(Exception, asyncio.CancelledError):
        async with client.connect() as s:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await s.client_session_init(timeout=3)
                setup = True
            await asyncio.wait_for(s.async_closed(), 3)
    return loop.time() - t0, setup


@pytest.mark.parametrize("draft", [16, 22])
@pytest.mark.parametrize("transport", ["quic", "wt"])
async def test_relay_process_refuses_newcomers_until_exit(
        tmp_path, transport, draft, relay_tables):
    port = free_udp_port()
    log = tmp_path / "relay.log"
    proc = spawn("moq_interop_relay", "--bind", "127.0.0.1", "--port", port,
                 "--dual", "--cert", CERT, "--key", KEY,
                 "--drain-timeout", 0.5, log=log)
    try:
        wait_for(log, "Listening on", proc)
        async with contextlib.AsyncExitStack() as stack:
            # A WT session that ignores GOAWAY keeps the close-out open:
            # its connection outlives the relay's GOAWAY_TIMEOUT close.
            holder = await _connect(stack, port, False, draft)
            holder.on_goaway = lambda msg, rid: None
            proc.send_signal(signal.SIGTERM)
            await asyncio.to_thread(wait_for, log, "relay: drain timed out",
                                    proc, 5)
            elapsed, setup = await _dial_refused(port, transport == "quic",
                                                 draft)
            alive = proc.poll() is None
            rc = await asyncio.to_thread(proc.wait, 10)
    finally:
        stop(proc)
    out = log.read_text(errors="replace")
    assert alive, out
    assert not setup, out
    assert elapsed < 0.5, (elapsed, out)
    assert rc == 0, out
    after = out[out.index("relay: SIGTERM: draining"):]
    assert "relay: refusing new connections" in out, out
    assert "setup complete" not in after, out
    assert "stream data after close" not in out, out
    assert " ERROR " not in out, out
    assert "Traceback" not in out, out


async def _dial_transport(port, use_quic, draft):
    """Dial: (seconds to the failure, exception), the exception None if the
    connection came up."""
    loop = asyncio.get_running_loop()
    client = MOQTClient("127.0.0.1", port, path="/", use_quic=use_quic,
                        verify_tls=False, supported_drafts=draft)
    t0 = loop.time()
    try:
        async with client.connect():
            return loop.time() - t0, None
    except BaseException as e:
        return loop.time() - t0, e


@pytest.mark.parametrize("draft", [16, 22])
@pytest.mark.parametrize("transport", ["quic", "wt"])
async def test_relay_process_refuses_connections_while_draining(
        tmp_path, transport, draft, relay_tables):
    port = free_udp_port()
    use_quic = transport == "quic"
    log = tmp_path / "relay.log"
    proc = spawn("moq_interop_relay", "--bind", "127.0.0.1", "--port", port,
                 "--dual", "--cert", CERT, "--key", KEY,
                 "--drain-timeout", 2, log=log)
    loop = asyncio.get_running_loop()
    try:
        wait_for(log, "Listening on", proc)
        async with contextlib.AsyncExitStack() as stack:
            # Ignoring GOAWAY keeps the drain window open.
            holder = await _connect(stack, port, use_quic, draft)
            seen = []
            holder.on_goaway = lambda msg, rid: seen.append(msg)
            t0 = loop.time()
            proc.send_signal(signal.SIGTERM)
            await _until(lambda: seen, 5)
            elapsed, exc = await _dial_transport(port, use_quic, draft)
            draining = loop.time() - t0 < 2.0 and proc.poll() is None
            rc = await asyncio.to_thread(proc.wait, 10)
            await asyncio.wait_for(holder.async_closed(), 2)
    finally:
        stop(proc)
    out = log.read_text(errors="replace")
    assert draining, out
    if use_quic:
        assert isinstance(exc, ConnectionError), repr(exc)
        assert "during handshake" in str(exc), repr(exc)
    else:
        assert isinstance(exc, WebTransportError), repr(exc)
        assert "before READY" in str(exc), repr(exc)
    assert elapsed < 0.5, (elapsed, out)
    assert len(seen) == 1, out
    assert holder._close_err[0] == SessionCloseCode.GOAWAY_TIMEOUT, \
        holder._close_err
    assert rc == 0, out
    assert out.index("relay: refusing new connections") < out.index(
        "relay: SIGTERM: draining 1 sessions"), out
    assert "relay: drain timed out, 1 sessions left" in out
    assert "Traceback" not in out, out


@pytest.mark.parametrize("transport", ["quic", "wt"])
async def test_close_sessions_refuses_arrivals_while_it_waits(
        transport, relay_tables):
    port = free_udp_port()
    srv = relay._build_server("127.0.0.1", port, CERT, KEY, use_quic=False,
                              draft=None)
    handle = await srv.serve_dual()
    loop = asyncio.get_running_loop()
    try:
        async with contextlib.AsyncExitStack() as stack:
            # A WT peer that keeps its connection holds close_sessions
            # waiting out its timeout.
            holder = await _connect(stack, port, False, None)
            t0 = loop.time()
            closing = asyncio.ensure_future(srv.close_sessions(timeout=1.5))
            await asyncio.wait_for(holder.async_closed(), 1)
            elapsed, setup = await _dial_refused(port, transport == "quic",
                                                 None)
            waiting = not closing.done()
            await asyncio.wait_for(closing, 3)
            closed_in = loop.time() - t0
    finally:
        handle.close()
    assert waiting
    assert not setup
    assert elapsed < 0.5, elapsed
    assert closed_in < 2.0, closed_in


@pytest.mark.parametrize("draft", [16, 22])
async def test_wt_client_refused_mid_setup_raises_the_close(draft,
                                                           relay_tables):
    port = free_udp_port()
    srv = relay._build_server("127.0.0.1", port, CERT, KEY, use_quic=False,
                              draft=None)
    handle = await srv.serve()
    srv.refuse_sessions("relay shutting down")
    raised = []
    try:
        # The close lands at different points of setup from dial to dial.
        for _ in range(10):
            client = MOQTClient("127.0.0.1", port, path="/", use_quic=False,
                                verify_tls=False, supported_drafts=draft)
            try:
                async with client.connect() as s:
                    await s.client_session_init(timeout=3)
            except BaseException as e:
                raised.append(e)
    finally:
        handle.close()
    assert len(raised) == 10
    for e in raised:
        assert isinstance(e, MOQTException), repr(e)
        assert e.error_code == SessionCloseCode.NO_ERROR, repr(e)


async def test_client_setup_cancellation_still_propagates():
    """A close mid-setup raises as MOQTException; cancelling the caller
    still cancels it."""
    session = types.SimpleNamespace(_close_err=None)

    async def closed_mid_setup(timeout, parameters):
        session._close_err = (SessionCloseCode.NO_ERROR, "refused")
        raise asyncio.CancelledError()
    session._client_session_init = closed_mid_setup
    with pytest.raises(MOQTException):
        await _MOQTSessionMixin.client_session_init(session)

    async def waits(timeout, parameters):
        session._close_err = (SessionCloseCode.NO_ERROR, "refused")
        await asyncio.sleep(10)
    session._client_session_init = waits
    task = asyncio.ensure_future(_MOQTSessionMixin.client_session_init(session))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("exc, closed, error", [
    (WebTransportError("WT session closed"), True, False),
    (ConnectionError("WT session closed"), False, True),
    (ValueError("boom"), True, True),
])
async def test_control_task_ended_by_close_logs_no_error(exc, closed, error,
                                                         caplog):
    caplog.set_level(logging.DEBUG)
    loop = asyncio.get_running_loop()
    session = types.SimpleNamespace(_tasks=set(),
                                    _moqt_session_closed=loop.create_future())
    if closed:
        session._moqt_session_closed.set_result((0, "refused"))

    async def fail():
        raise exc
    task = loop.create_task(fail())
    with contextlib.suppress(Exception):
        await task
    _MOQTSessionMixin._control_task_done(session, task)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert bool(errors) == error, caplog.text


def _announced_on(server):
    return any(s in server._live for held in relay._announced.values()
               for s in held)


# -- pub_media --reconnect --------------------------------------------------------

@pytest.mark.parametrize("migrate", [False, True], ids=["same", "uri"])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_pub_media_reconnects_on_goaway(
        monkeypatch, capsys, h264_aac, arrivals, use_quic,  # noqa: F811
        migrate):
    port = _BASE_PORT + 20 + 4 * migrate + 2 * (not use_quic)
    # The relay's close-out runs to its bound on WT; a redial landing in the
    # restart gap gives up in time for the next one.
    monkeypatch.setattr(pub_media, "_DIAL_TIMEOUT_S", 2.0)
    url, next_url = _url(port, use_quic), _url(port + 1, use_quic)
    ns = f"drain/media{int(migrate)}"
    pub_args = _argv(monkeypatch, "pub_media", url, "-k", "--draft", "18",
                     "-t", "12", "--stats", "0", "--pub-ns", "-N", ns,
                     "--mp4", h264_aac, "--loop", "--reconnect",
                     "--stats-json")

    def sub_args(at):
        return _argv(monkeypatch, "sub_media", at, "-k", "--draft", "18",
                     "-N", ns, "--analyze", "-t", "2", "-i", "1")
    srv, handle = await _serve(port, use_quic, 18)
    servers = [(srv, handle)]
    pub = None
    loop = asyncio.get_running_loop()
    try:
        pub = asyncio.ensure_future(pub_media.run(pub_args))
        await _until(lambda: relay._announced or pub.done(), 10)
        assert await sub_media.run(sub_args(url)) == 0
        first = list(arrivals)
        if migrate:
            servers.append(await _serve(port + 1, use_quic, 18))
        t0 = loop.time()
        await relay._drain([srv], next_url if migrate else "", 2.0,
                           "SIGTERM")
        drained = loop.time() - t0
        await srv.close_sessions()
        handle.close()
        servers.remove((srv, handle))
        if not migrate:
            await asyncio.sleep(0.3)
            servers.append(await _serve(port, use_quic, 18))
        await _until(lambda: _announced_on(servers[0][0]) or pub.done(), 10)
        assert not pub.done()
        assert await sub_media.run(
            sub_args(next_url if migrate else url)) == 0
        second = arrivals[len(first):]
        await asyncio.wait_for(pub, 20)
    finally:
        if pub is not None and not pub.done():
            pub.cancel()
        for s, h in servers:
            await _stop(s, h)
    # pub_media left at the GOAWAY instead of waiting out the drain.
    assert drained < 1.0, drained
    for track in ("video", "audio"):
        assert _groups(first, track) and _groups(second, track), track
    events = [e for e in _pub_events(capsys.readouterr().out)
              if e["event"] != "stats"]
    lost = [e for e in events if e["event"] == "endpoint_lost"]
    assert lost[0]["reason"] == ("GOAWAY " + next_url if migrate
                                 else "GOAWAY"), lost
    up = [e for e in events if e["event"] == "endpoint_up"]
    assert up and up[-1]["url"] == (next_url if migrate else url), events
    if migrate:
        assert [e["event"] for e in events] == [
            "ready", "endpoint_lost", "endpoint_up", "done"], events
    assert events[-1]["reason"] == "duration"
