"""--video on the bench publishers: the grid's -r/-g meanings, the flags a
profile refuses, and the shape on the wire (pub_server and the bench
workers) over raw QUIC and WebTransport."""
import asyncio
import sys
from functools import partial

import pytest

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.tools import adaptive_bench as ab
from aiomoqt.tools import pub_server
from aiomoqt.track import SubscribedTrack, VideoTrack
from aiomoqt.types import MOQTMessageType
from aiomoqt.utils import cli, workers

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._procs import free_udp_port
from aiomoqt.tests.test_adaptive_bench_workers import _argv, _processes


def _media(argv):
    p = cli.make_parser("t")
    cli.add_publisher_media(p)
    args = p.parse_args(argv)
    cli.check_video(p, args)
    return args


def test_video_defaults_to_30_fps_and_one_second_gops():
    a = _media(["--video", "1080p"])
    assert (a.rate, a.group_size, a.streams) == (30, 30, 1)
    assert a.object_size == VideoTrack.mean_object_size("1080p", 30)


def test_rate_and_group_size_keep_their_meaning_with_video():
    a = _media(["--video", "720p", "-r", "60", "-g", "120"])
    assert (a.rate, a.group_size) == (60, 120)
    assert a.object_size == VideoTrack.mean_object_size("720p", 120)


def test_without_video_nothing_changes():
    a = _media([])
    assert (a.object_size, a.group_size, a.streams, a.rate) == (
        cli.DEFAULT_OBJECT_SIZE, cli.DEFAULT_GROUP_SIZE, 1, 0)


@pytest.mark.parametrize("flag", [["-s", "4096"], ["-P", "1"], ["-D"]])
def test_video_refuses_the_flags_it_replaces(flag, capsys):
    with pytest.raises(SystemExit):
        _media(["--video", "1080p", *flag])
    assert f"cannot be combined with {flag[0]}" in capsys.readouterr().err


def test_video_needs_a_frame_rate(capsys):
    with pytest.raises(SystemExit):
        _media(["--video", "1080p", "-r", "0"])
    assert "frame rate" in capsys.readouterr().err


def test_pub_server_takes_the_grid_flags(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pub_server", "--video", "480p", "-s", "1"])
    with pytest.raises(SystemExit):
        pub_server.parse_args()
    assert "cannot be combined with -s" in capsys.readouterr().err


def _pub_server_handler(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["pub_server", *argv])
    return partial(pub_server._on_subscribe, args=pub_server.parse_args())


def _worker_handler(cfg):
    async def on_subscribe(session, msg):
        track = workers.bench_track(session, cfg, rate=0, num_subgroups=1)
        ok = session.subscribe_ok(request_msg=msg)
        track.track_alias = ok.track_alias
        track._generating = True
        track._quiet = True
        await track.generate(session, ok.track_alias)
    return on_subscribe


async def _receive(handler, use_quic, names, groups):
    """Subscribe `names` on one session; (group, object, payload) per name
    until each has `groups` whole groups."""
    port = free_udp_port()
    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic)
    server.register_handler(MOQTMessageType.SUBSCRIBE, handler)
    handle = await server.serve()
    got = {n: [] for n in names}
    client = MOQTClient("localhost", port, path="/", use_quic=use_quic,
                        verify_tls=False)
    try:
        async with client.connect() as session:
            await session.client_session_init()
            for n in names:
                sub = SubscribedTrack(session, "aiomoqt", n)

                def on_object(msg, size, recv, gid, sg, _n=n):
                    if not msg.status:
                        got[_n].append((gid, msg.object_id, bytes(msg.payload)))
                sub.on_object = on_object
                await sub.subscribe()
            for _ in range(500):
                if all(len({g for g, _, _ in v}) > groups + 1
                       for v in got.values()):
                    break
                await asyncio.sleep(0.01)
    finally:
        handle.close()
    return got


def _assert_profile_shape(objs, res, gop):
    prof = VideoTrack.PROFILES[res]
    size = {"I": prof["i_frame"], "P": prof["p_frame"], "B": prof["b_frame"]}
    pattern = VideoTrack._build_gop("ibp", gop)
    by_group = {}
    for gid, oid, payload in objs:
        by_group.setdefault(gid, []).append((oid, payload))
    whole = sorted(by_group)[1:-1]
    assert len(whole) >= 2, sorted(by_group)
    for gid in whole:
        frames = sorted(by_group[gid])
        assert [oid for oid, _ in frames] == list(range(gop)), gid
        for oid, payload in frames:
            ft = pattern[oid]
            assert payload.startswith(f"{gid}.{oid}.{ft}|".encode())
            assert len(payload) == size[ft], (gid, oid, ft)


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_pub_server_serves_the_profile_on_each_track(monkeypatch, use_quic):
    handler = _pub_server_handler(
        monkeypatch, ["--video", "240p", "-r", "100", "-g", "10"])
    got = await _receive(handler, use_quic, ["one", "two"], groups=2)
    for objs in got.values():
        _assert_profile_shape(objs, "240p", 10)


@requires_certs
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_bench_worker_publishes_the_profile(use_quic):
    cfg = dict(namespace="aiomoqt", trackname="t", object_size=0,
               group_size=10, video="240p", video_fps=100)
    got = await _receive(_worker_handler(cfg), use_quic, ["one", "two"],
                         groups=2)
    for objs in got.values():
        _assert_profile_shape(objs, "240p", 10)


async def test_adaptive_subs_mode_publishes_the_profile(monkeypatch):
    configs = _processes(monkeypatch)
    _argv(monkeypatch, "--mode", "subs", "--video", "720p")
    assert await ab.main() == 0
    mean = VideoTrack.mean_object_size("720p", 30)
    pubs = [c for name, c in configs if name == "pub_worker_entry"]
    subs = [c for name, c in configs if name == "sub_batch_worker_entry"]
    assert pubs and subs
    for c in pubs:
        assert (c["video"], c["video_fps"], c["group_size"]) == ("720p", 30, 30)
        assert c["object_size"] == mean
        assert c["initial_rate_ops"] == pytest.approx(30)
    for c in subs:
        assert c["per_sub_mbps"] == pytest.approx(mean * 8 * 30 / 1e6)


@pytest.mark.parametrize("extra, why", [
    (["--mode", "bw"], "subs-mode only"),
    (["--mode", "subs", "--sub-mbps", "5"], "drop --sub-mbps"),
    (["--mode", "subs", "-s", "100"], "cannot be combined with -s"),
    (["--mode", "subs", "-P", "2"], "cannot be combined with -P"),
])
def test_adaptive_refuses_what_a_profile_fixes(monkeypatch, capsys, extra, why):
    _argv(monkeypatch, "--video", "720p", *extra)
    with pytest.raises(SystemExit):
        ab.parse_args()
    assert why in capsys.readouterr().err
