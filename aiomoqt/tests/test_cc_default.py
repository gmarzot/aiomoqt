"""Congestion control defaults to bbr in the library, relay, tools and examples."""
import argparse
import inspect

import pytest

from aiopquic.quic.configuration import QuicConfiguration

from aiomoqt.client import MOQTClient
from aiomoqt.server import MOQTServer
from aiomoqt.utils import cli
from aiomoqt.tests.test_cli_grid import _parser_actions


def test_client_and_server_default_to_bbr():
    assert MOQTClient("localhost", 4433).congestion_control_algorithm == "bbr"
    param = inspect.signature(MOQTServer.__init__).parameters
    assert param["congestion_control_algorithm"].default == "bbr"


def test_client_keeps_a_passed_configurations_algorithm():
    cfg = QuicConfiguration(is_client=True)
    cfg.congestion_control_algorithm = "cubic"
    client = MOQTClient("localhost", 4433, configuration=cfg)
    assert client.congestion_control_algorithm is None
    assert MOQTClient("localhost", 4433, configuration=cfg,
                      congestion_control_algorithm="bbr"
                      ).congestion_control_algorithm == "bbr"


def test_session_group_defaults_to_bbr():
    p = argparse.ArgumentParser()
    cli.add_session(p)
    assert p.parse_args([]).cc_algo == "bbr"


@pytest.mark.parametrize("module", [
    "aiomoqt.tools.moq_interop_relay",
    "aiomoqt.tools.pub_server",
    "aiomoqt.tools.adaptive_bench",
    "aiomoqt.tools.loopback_bench",
    "aiomoqt.examples.pub_example",
    "aiomoqt.examples.sub_example",
    "aiomoqt.examples.join_example",
    "aiomoqt.examples.server_example",
])
def test_cli_defaults_to_bbr(module, monkeypatch):
    package, name = module.rsplit(".", 1)
    monkeypatch.setattr("sys.argv", [name])
    cc = [a for a in _parser_actions(name, package) if a.dest == "cc_algo"]
    assert len(cc) == 1
    assert cc[0].default == "bbr"
    assert "Default: bbr" in cc[0].help
