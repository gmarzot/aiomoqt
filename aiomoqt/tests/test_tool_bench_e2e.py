"""pub_server and sub_bench as processes: each listener mode, both
transports, d18 and d22."""
import pytest

from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests._procs import (free_udp_port, objects_received, run, spawn,
                                  stop, wait_for)


@pytest.fixture
def pub_server(tmp_path):
    procs = []

    def start(*listen):
        port = free_udp_port()
        log = tmp_path / f"pub_server-{port}.log"
        proc = spawn("pub_server", "-p", port, "-s", 512, "-r", 200,
                     *listen, log=log)
        procs.append(proc)
        wait_for(log, "publisher server ready", proc)
        return port

    yield start
    for p in procs:
        stop(p)


def _url(scheme, port):
    return (f"moqt://localhost:{port}" if scheme == "quic"
            else f"https://localhost:{port}/")


@requires_certs
@pytest.mark.parametrize("draft", [18, 22])
@pytest.mark.parametrize("listen,dial", [
    (("-Q",), "quic"), (("-W",), "wt"),
    (("-Q", "-W"), "quic"), (("-Q", "-W"), "wt"),
], ids=["Q", "W", "QW-quic", "QW-wt"])
def test_sub_bench_receives_from_pub_server(pub_server, listen, dial, draft):
    port = pub_server(*listen)
    rc, out = run("sub_bench", _url(dial, port), "-k", "-t", 2, "-i", 1,
                  "--draft", draft)
    assert objects_received(out) > 0, out
    assert rc == 0, out
