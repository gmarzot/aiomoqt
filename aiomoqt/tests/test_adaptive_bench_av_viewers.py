"""adaptive_bench --audio/--catalog: flags shared with pub_bench, each viewer
taking every track, and per-track stats merged for the slot checks; through
the relay over raw QUIC and WebTransport."""
import asyncio
import signal
import subprocess
from functools import partial
from types import SimpleNamespace

import pytest

from aiomoqt.tools import adaptive_bench as ab
from aiomoqt.track import VideoTrack
from aiomoqt.utils import cli, workers
from aiomoqt.utils.stats import TrackStats, ViewerStats

from aiomoqt.tests._certs import CERT, KEY, requires_certs
from aiomoqt.tests._procs import free_udp_port, spawn, stop, wait_for
from aiomoqt.tests.test_adaptive_bench_workers import _argv, _processes


def _interrupt(proc):
    """SIGINT: the bench stops its publisher worker before exiting."""
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    stop(proc)


def _video_mbps(profile="720p"):
    return VideoTrack.mean_object_size(profile, 30) * 8 * 30 / 1e6


def test_side_tracks_add_their_bitrate_and_default_the_video_name(monkeypatch):
    _argv(monkeypatch, "--mode", "subs", "--video", "720p",
          "--audio", "--catalog")
    args = ab.parse_args()
    assert args.trackname == "video"
    assert [s[0] for s in args.side_tracks] == ["catalog", "audio"]
    assert args.sub_mbps == pytest.approx(
        _video_mbps() + cli.side_tracks_mbps(args.side_tracks))


def test_datagram_applies_to_the_audio_track(monkeypatch):
    _argv(monkeypatch, "--mode", "subs", "--video", "720p", "--audio", "-D")
    args = ab.parse_args()
    assert args.side_tracks == [
        ("audio", cli.AUDIO_OBJECT_SIZE, cli.AUDIO_RATE, True)]
    assert not args.datagram


@pytest.mark.parametrize("extra, why", [
    (["--mode", "subs", "--audio"], "--audio needs --video"),
    (["--mode", "subs", "--catalog"], "--catalog needs --video"),
    (["--mode", "subs", "-D"], "-D applies only"),
    (["--mode", "subs", "--video", "720p", "--audio", "-T", "audio"],
     "clashes with --audio"),
])
def test_refuses(monkeypatch, capsys, extra, why):
    _argv(monkeypatch, *extra)
    with pytest.raises(SystemExit):
        ab.parse_args()
    assert why in capsys.readouterr().err


async def test_workers_get_the_side_tracks(monkeypatch):
    configs = _processes(monkeypatch)
    _argv(monkeypatch, "--mode", "subs", "--video", "720p", "--audio", "-D")
    assert await ab.main() == 0
    shape = ("audio", cli.AUDIO_OBJECT_SIZE, cli.AUDIO_RATE, True)
    pubs = [c for name, c in configs if name == "pub_worker_entry"]
    subs = [c for name, c in configs if name == "sub_batch_worker_entry"]
    assert pubs and subs
    assert all(c["side_tracks"] == [shape] for c in pubs + subs)
    assert all(c["per_sub_mbps"] == pytest.approx(
        _video_mbps() + cli.side_tracks_mbps([shape])) for c in subs)


def _snap(**kw):
    base = dict(t=1.0, dt=1.0, rx_objs=0, rx_bytes=0, iv_lost=0, lat_n=0,
                lat_mean_ms=0.0, lat_p90_ms=0.0, lat_p99_ms=0.0, jitter_ms=0.0,
                loss=0, groups=0, total_objs=0, total_bytes=0, ts_source=None)
    return {**base, **kw}


def test_viewer_stats_sum_counts_and_keep_the_worst_latency():
    stats = ViewerStats(["video", "audio", "catalog"], group_loss=True)
    snaps = {
        "video": _snap(rx_objs=30, rx_bytes=9000, lat_n=30, lat_mean_ms=20.0,
                       lat_p90_ms=40.0, lat_p99_ms=50.0, jitter_ms=2.0, loss=1,
                       total_objs=30, total_bytes=9000),
        "audio": _snap(rx_objs=10, rx_bytes=500, lat_n=10, lat_mean_ms=60.0,
                       lat_p90_ms=90.0, lat_p99_ms=95.0, jitter_ms=5.0, loss=2,
                       total_objs=10, total_bytes=500),
        "catalog": _snap(),
    }
    for name, t in stats.tracks.items():
        t.snapshot = partial(dict, snaps[name])
    merged = stats.snapshot()
    assert merged["rx_bytes"] == 9500 and merged["loss"] == 3
    assert merged["lat_n"] == 40
    assert merged["lat_p90_ms"] == 90.0 and merged["jitter_ms"] == 5.0
    assert merged["lat_mean_ms"] == pytest.approx(30.0)


def test_a_cycled_viewer_keeps_its_tracks():
    config = dict(trackname="video",
                  side_tracks=[("audio", 341, 46.875, False)])
    first = workers.new_viewer_stats(config)
    state = workers.new_slot_state(workers.new_join_tally())
    state["new_stats"] = partial(workers.new_viewer_stats, config)
    second = workers.renew_stats(state, first)
    assert isinstance(second, ViewerStats) and second is not first
    assert set(second.tracks) == {"video", "audio"}


def test_a_video_only_viewer_keeps_plain_track_stats():
    stats = workers.new_viewer_stats(dict(trackname="video"))
    assert isinstance(stats, TrackStats)


@requires_certs
@pytest.mark.parametrize("dgram", [False, True], ids=["streams", "dgram"])
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_a_viewer_takes_every_track_through_the_relay(tmp_path, use_quic,
                                                            dgram):
    port = free_udp_port()
    rlog, plog = tmp_path / "relay.log", tmp_path / "pub.log"
    relay = spawn("moq_interop_relay", "--bind", "127.0.0.1", "--port", port,
                  "--dual", "--cert", CERT, "--key", KEY, log=rlog)
    pub = None
    url = (f"moqt://127.0.0.1:{port}" if use_quic
           else f"https://127.0.0.1:{port}/")
    config = dict(relay_url=url, namespace="aiomoqt", trackname="video",
                  insecure=True, side_tracks=[
                      ("catalog", cli.CATALOG_OBJECT_SIZE, cli.CATALOG_RATE, False),
                      ("audio", cli.AUDIO_OBJECT_SIZE, cli.AUDIO_RATE, dgram)])
    stats = workers.new_viewer_stats(config)
    state = workers.new_slot_state(workers.new_join_tally())
    stop_ev = asyncio.Event()
    try:
        wait_for(rlog, "Listening on", relay)
        pub = spawn("adaptive_bench", url, "-k", "--role", "pub", "--mode", "subs",
                    "--video", "240p", "-g", 30, "--audio", "--catalog",
                    *(["-D"] if dgram else []), "-N", "aiomoqt", "-t", 30, log=plog)
        wait_for(plog, "role=pub", pub)
        endpoint = SimpleNamespace(host="127.0.0.1", port=port, path="",
                                   use_quic=use_quic)
        slot = asyncio.create_task(workers._slot_supervisor(
            config, endpoint, stop_ev, stats, state, 0))
        await asyncio.sleep(5.0)
        assert state["active"]
        stop_ev.set()
        await asyncio.wait_for(slot, 10)
    finally:
        stop_ev.set()
        if pub is not None:
            _interrupt(pub)
        stop(relay)
    objs = {n: t.total_objects for n, t in stats.tracks.items()}
    assert objs["video"] >= 60 and objs["audio"] >= 100 and objs["catalog"] >= 2, objs
    assert stats.total_lost == 0
