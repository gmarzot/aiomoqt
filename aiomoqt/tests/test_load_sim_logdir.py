"""load_sim --logdir DIR -d: each process file holds aiomoqt DEBUG logging."""
from aiomoqt.tests._certs import requires_certs
from aiomoqt.tests.test_load_sim_verdict import run_loopback_load_sim


@requires_certs
def test_logdir_debug_records_aiomoqt_logging(tmp_path):
    logdir = tmp_path / "logs"
    rc, out = run_loopback_load_sim("--logdir", str(logdir), "-d")
    assert rc == 0, out
    pub = (logdir / "pub.log").read_text()
    sub = (logdir / "grp-0-0.log").read_text()
    assert "PublishNamespace(" in pub, pub
    assert "Subscribe(" in sub, sub
    assert "SubscribeOk(" in sub, sub
    assert any(" DEBUG " in line and "MOQT" in line
               for line in sub.splitlines()), sub
