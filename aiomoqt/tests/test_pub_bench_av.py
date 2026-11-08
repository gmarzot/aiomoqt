"""pub_bench --audio and --catalog: tracks beside --video on one session,
their shape on the wire through the relay over raw QUIC and WebTransport,
and the usage errors without --video."""
import asyncio
import sys
import time

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.messages import ObjectDatagram
from aiomoqt.tools import pub_bench
from aiomoqt.utils import cli as _cli
from aiomoqt.track import SubscribedTrack, VideoTrack

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._procs import free_udp_port, spawn, stop, wait_for

WINDOW_S = 4.0


@pytest.mark.parametrize("flag", ["--audio", "--catalog"])
def test_side_tracks_need_video(monkeypatch, capsys, flag):
    monkeypatch.setattr(sys, "argv", ["pub_bench", "moqt://x:1", flag])
    with pytest.raises(SystemExit):
        pub_bench.parse_args()
    assert f"{flag} needs --video" in capsys.readouterr().err


def test_side_tracks_default_the_video_name(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pub_bench", "moqt://x:1", "--video",
                                      "720p", "--audio", "--catalog"])
    args = pub_bench.parse_args()
    assert args.trackname == "video"
    assert (args.rate, args.group_size) == (30, 30)


def test_trackname_may_not_take_a_side_track_name(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pub_bench", "moqt://x:1", "--video",
                                      "720p", "--audio", "-T", "audio"])
    with pytest.raises(SystemExit):
        pub_bench.parse_args()
    assert "clashes with --audio" in capsys.readouterr().err


def test_datagram_with_video_applies_to_audio(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pub_bench", "moqt://x:1", "--video",
                                      "720p", "--audio", "-D"])
    args = pub_bench.parse_args()
    assert args.audio_datagram and not args.datagram


def test_datagram_with_video_needs_audio(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pub_bench", "moqt://x:1", "--video",
                                      "720p", "-D"])
    with pytest.raises(SystemExit):
        pub_bench.parse_args()
    assert "--video cannot be combined with -D" in capsys.readouterr().err


async def _receive(port, use_quic, names):
    """Subscribe `names` on one session; (recv time, group, object, size,
    datagram) per name over WINDOW_S."""
    got = {n: [] for n in names}
    client = MOQTClient("127.0.0.1", port, path="/", use_quic=use_quic,
                        verify_tls=False)
    async with client.connect() as session:
        await session.client_session_init()
        for n in names:
            def on_object(msg, size, recv, gid, sg, *_a, _n=n, **_kw):
                if not msg.status:
                    got[_n].append((time.monotonic(), gid, msg.object_id,
                                    len(msg.payload),
                                    isinstance(msg, ObjectDatagram)))
            await SubscribedTrack(session, "aiomoqt", n,
                                  on_object=on_object).subscribe()
        await asyncio.sleep(WINDOW_S)
    return got


def _rate(objs):
    return (len(objs) - 1) / (objs[-1][0] - objs[0][0])


@requires_certs
@pytest.mark.parametrize("audio_dgram", [False, True], ids=["", "audio-dgram"])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_audio_and_catalog_beside_video(tmp_path, use_quic,
                                              audio_dgram):
    port = free_udp_port()
    rlog = tmp_path / "relay.log"
    relay = spawn("moq_interop_relay", "--bind", "127.0.0.1", "--port", port,
                  "--dual", "--cert", CERT, "--key", KEY, log=rlog)
    pub = None
    try:
        wait_for(rlog, "Listening on", relay)
        url = (f"moqt://127.0.0.1:{port}" if use_quic
               else f"https://127.0.0.1:{port}/")
        plog = tmp_path / "pub.log"
        pub = spawn("pub_bench", url, "-k", "--video", "240p", "-r", 30,
                    "-g", 30, "--audio", "--catalog", "-t", 30,
                    *(["-D"] if audio_dgram else []), log=plog)
        wait_for(plog, "Published 'aiomoqt/audio'", pub)
        got = await _receive(port, use_quic, ["catalog", "video", "audio"])
    finally:
        if pub is not None:
            stop(pub)
        stop(relay)

    video, audio, catalog = got["video"], got["audio"], got["catalog"]
    assert video and audio and catalog, {k: len(v) for k, v in got.items()}
    prof = VideoTrack.PROFILES["240p"]
    assert {size for _, _, _, size, _ in video} <= {
        prof["i_frame"], prof["p_frame"], prof["b_frame"]}
    assert len({gid for _, gid, _, _, _ in video}) < len(video)
    for objs, size in ((audio, _cli.AUDIO_OBJECT_SIZE),
                       (catalog, _cli.CATALOG_OBJECT_SIZE)):
        assert {s for _, _, _, s, _ in objs} == {size}
        assert {oid for _, _, oid, _, _ in objs} == {0}
        gids = [gid for _, gid, _, _, _ in objs]
        assert len(set(gids)) == len(gids)
    assert {d for *_, d in audio} == {audio_dgram}
    assert not any(d for *_, d in video + catalog)
    assert _rate(audio) == pytest.approx(_cli.AUDIO_RATE, rel=0.1)
    assert _rate(video) == pytest.approx(30, rel=0.1)
    assert 2 <= len(catalog) <= WINDOW_S + 2
