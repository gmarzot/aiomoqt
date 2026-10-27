"""aiomoqt-relay --config: flags from a TOML file keyed by long flag name;
the command line overrides it."""
import pathlib
import re

import pytest

from aiomoqt.tools import moq_interop_relay as relay

_DEPLOY_TOML = (pathlib.Path(__file__).resolve().parents[2]
                / "deploy" / "aiomoqt-relay" / "relay.toml")


def _config(tmp_path, text):
    path = tmp_path / "relay.toml"
    path.write_text(text)
    return str(path)


def _refused(capsys, *argv):
    with pytest.raises(SystemExit) as e:
        relay.parse_args(list(argv))
    assert e.value.code == 2
    return capsys.readouterr().err


def test_the_file_sets_flags(tmp_path):
    path = _config(tmp_path, """
cache-bytes = 1048576
catalog-probe = true
cc-algo = "bbr"
watchdog = 30
draft = "22,18"
upstream = ["moqt://origin.example:4433/", "https://origin.example/moq"]
log = ["relay=debug"]
stream-scheduling = "round_robin"
""")
    args = relay.parse_args(["--config", path])
    assert args.cache_bytes == 1048576
    assert args.catalog_probe is True
    assert args.cc_algo == "bbr"
    assert args.watchdog == 30.0 and isinstance(args.watchdog, float)
    assert args.draft == [22, 18]
    assert args.upstream == ["moqt://origin.example:4433/",
                             "https://origin.example/moq"]
    assert args.log == ["relay=debug"]
    assert args.stream_scheduling == "round_robin"
    # Untouched flags keep their defaults.
    assert args.port == 4443 and args.quic is False


def test_the_command_line_overrides_the_file(tmp_path):
    path = _config(tmp_path, """
cache-bytes = 1048576
cc-algo = "bbr"
upstream = ["moqt://a.example:4433/"]
""")
    args = relay.parse_args(["--cache-bytes", "2048", "--config", path,
                             "--upstream", "moqt://b.example:4433/"])
    assert args.cache_bytes == 2048
    assert args.upstream == ["moqt://b.example:4433/"]
    assert args.cc_algo == "bbr"


def test_an_unknown_key_is_an_error(tmp_path, capsys):
    path = _config(tmp_path, "cache-byte = 1048576\n")
    assert "unknown key 'cache-byte'" in _refused(capsys, "--config", path)


@pytest.mark.parametrize("text,key", [
    ('cache-bytes = "big"\n', "cache-bytes"),
    ("cache-bytes = 1.5\n", "cache-bytes"),
    ('catalog-probe = "yes"\n', "catalog-probe"),
    ("cc-algo = 3\n", "cc-algo"),
    ('upstream = "moqt://a.example:4433/"\n', "upstream"),
    ('stream-scheduling = "lifo"\n', "stream-scheduling"),
    ("watchdog = true\n", "watchdog"),
])
def test_a_wrong_type_is_an_error(tmp_path, capsys, text, key):
    err = _refused(capsys, "--config", _config(tmp_path, text))
    assert f": {key}: " in err, err


@pytest.mark.parametrize("text", ["config = 'x.toml'\n", "help = true\n"])
def test_config_and_help_are_not_keys(tmp_path, capsys, text):
    assert "unknown key" in _refused(capsys, "--config",
                                     _config(tmp_path, text))


def test_a_missing_or_malformed_file_is_an_error(tmp_path, capsys):
    assert "--config" in _refused(capsys, "--config",
                                  str(tmp_path / "missing.toml"))
    assert "--config" in _refused(capsys, "--config",
                                  _config(tmp_path, "cache-bytes = \n"))


@pytest.mark.skipif(not _DEPLOY_TOML.exists(), reason="no deploy tree")
def test_the_deploy_config_loads():
    args = relay.parse_args(["--config", str(_DEPLOY_TOML)])
    assert args.draft == [22, 20, 18, 16, 14]
    assert args.watchdog == 30.0
    assert args.status_dir == "/status" and args.log_dir == "/logs"
    assert args.control == "/status/control.sock"
    assert args.catalog_probe is True
    assert args.cache_bytes == 268435456
    assert args.cc_algo == "bbr"


@pytest.mark.skipif(not _DEPLOY_TOML.exists(), reason="no deploy tree")
def test_the_deploy_config_holds_no_addresses_or_hosts():
    text = _DEPLOY_TOML.read_text()
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)
    assert not re.search(r"[0-9a-fA-F]{1,4}(:[0-9a-fA-F]{0,4}){2,}", text)
    assert not re.search(r"[A-Za-z0-9-]+\.(net|com|org|io|dev)\b", text)
