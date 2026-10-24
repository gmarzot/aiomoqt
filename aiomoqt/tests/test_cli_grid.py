"""The CLI grid is a contract, not a convention.

Two kinds of test here:

  grid semantics  — what each shared group adds, its defaults, and the
                    guards (datagram fit, URL-vs-listener).
  cross-tool      — every shipped tool is introspected and asserted to
                    use the grid's letters for the grid's meanings.
                    This is the test that keeps the flags from drifting
                    apart again: adding `-n` for "number of subscribers"
                    to some future tool fails here.
"""
import argparse
import importlib
import sys

import pytest

from aiomoqt.utils import cli


TOOLS = [
    "pub_bench", "sub_bench", "loopback_bench", "adaptive_bench",
    "pub_server", "load_sim", "relay_probe",
    "moq_interop_client", "moq_interop_relay", "moqtest_origin",
    "pub_media", "sub_media",
]

EXAMPLES = ["pub_example", "sub_example", "join_example", "server_example"]

# One letter, one meaning, everywhere it appears.
SHORT_FLAG_MEANING = {
    "-N": "namespace",
    "-T": "trackname",
    "-D": "datagram",
    "-s": "object_size",
    "-g": "group_size",
    "-P": "streams",
    "-r": "rate",
    "-t": "duration",
    "-i": "interval",
    "-k": "insecure",
    "-d": "debug",
    "-p": "port",
    "-Q": "quic",
    "-W": "wt",
}

# Flags another program invokes a tool with. moq_interop_client follows
# moq-interop-runner docs/TEST-CLIENT-INTERFACE.md ("Options"):
#   -r, --relay <URL>   -t, --test <NAME>   -l, --list   -v, --verbose
EXTERNAL_CONTRACT = {
    "moq_interop_client": {"-r": "--relay", "-t": "--test",
                           "-l": "--list", "-v": "--verbose"},
}


def _parser_actions(mod_name, package="aiomoqt.tools"):
    """Build a tool's parser without running it, and return its actions.

    Tools construct their parser inside parse_args(); we intercept the
    ArgumentParser rather than calling parse_args (which would exit).
    """
    mod = importlib.import_module(f"{package}.{mod_name}")
    captured = []
    real_init = argparse.ArgumentParser.__init__

    def spy(self, *a, **kw):
        real_init(self, *a, **kw)
        captured.append(self)

    argparse.ArgumentParser.__init__ = spy
    try:
        try:
            mod.parse_args([]) if _takes_argv(mod) else mod.parse_args()
        except (SystemExit, TypeError, AttributeError):
            pass          # missing required args — the parser still built
    finally:
        argparse.ArgumentParser.__init__ = real_init
    if not captured:
        pytest.skip(f"{mod_name}: no parser captured")
    return [a for p in captured for a in p._actions]


def _takes_argv(mod):
    import inspect
    try:
        return len(inspect.signature(mod.parse_args).parameters) > 0
    except (ValueError, AttributeError):
        return False


# -- grid semantics --------------------------------------------------

def test_defaults_are_single_sourced():
    p = cli.make_parser("t")
    cli.add_publisher_media(p)
    a = p.parse_args([])
    assert a.object_size == cli.DEFAULT_OBJECT_SIZE == 4096
    assert a.group_size == cli.DEFAULT_GROUP_SIZE == 4096
    assert a.streams == 1 and a.rate == 0 and a.datagram is False


def test_endpoint_is_positional_and_transport_free():
    p = cli.make_parser("t")
    cli.add_endpoint(p)
    a = p.parse_args(["moqt://h:4433/x"])
    assert a.url == "moqt://h:4433/x"
    # the scheme carries the transport — no -q anywhere on the grid
    assert not any(o == "-q" for act in p._actions for o in act.option_strings)


@pytest.mark.parametrize("argv,expect", [
    ([], (False, True)),                 # neither = WT
    (["-W"], (False, True)),
    (["-Q"], (True, False)),
    (["-Q", "-W"], (True, True)),        # both = dual serve, one port
])
def test_listener_resolution(argv, expect):
    p = cli.make_parser("t")
    cli.add_listener(p)
    assert cli.resolve_listener(p.parse_args(argv)) == expect


def test_trackname_prefix_alias_resolves():
    p = cli.make_parser("t")
    cli.add_identity(p)
    assert p.parse_args(["--track", "abc"]).trackname == "abc"


def test_datagram_guard_rejects_unfittable_object():
    p = cli.make_parser("t")
    cli.add_publisher_media(p)
    args = p.parse_args(["-D", "-s", str(cli.DGRAM_MAX_OBJECT + 1)])
    with pytest.raises(SystemExit):
        cli.check_datagram(p, args)


def test_datagram_guard_allows_fitting_object():
    p = cli.make_parser("t")
    cli.add_publisher_media(p)
    args = p.parse_args(["-D", "-s", str(cli.DGRAM_MAX_OBJECT)])
    cli.check_datagram(p, args, serve_quic=True)   # no raise


def test_datagram_guard_rejects_wt_transport():
    p = cli.make_parser("t")
    cli.add_publisher_media(p)
    args = p.parse_args(["-D", "-s", "512"])
    with pytest.raises(SystemExit):
        cli.check_datagram(p, args, serve_quic=False)


def test_url_and_listener_options_are_mutually_exclusive():
    p = cli.make_parser("t")
    p.add_argument("url", nargs="?")
    cli.add_listener(p)
    with pytest.raises(SystemExit):
        cli.check_url_vs_listener(p, p.parse_args(["moqt://h", "-Q"]))
    cli.check_url_vs_listener(p, p.parse_args(["moqt://h"]))       # ok
    cli.check_url_vs_listener(p, p.parse_args(["-Q"]))             # ok


def _groups():
    return [getattr(cli, n) for n in sorted(dir(cli))
            if n.startswith("add_") and callable(getattr(cli, n))]


def _group_flags(fn, **kw):
    p = cli.make_parser("t")
    fn(p, **kw)
    return sorted(tuple(a.option_strings) or (a.dest,) for a in p._actions)


def _other_value(default):
    if isinstance(default, bool):
        return not default
    if default is None:
        return "other"
    if isinstance(default, (int, float)):
        return default + 1
    return "other"


def test_groups_take_no_feature_toggles():
    """The anti-drift property: a caller cannot ask for a subset, so
    every tool that adds a group gets the identical flag set. Arguments
    may set defaults only."""
    import inspect
    groups = _groups()
    assert len(groups) >= 12
    for fn in groups:
        params = list(inspect.signature(fn).parameters.values())[1:]
        assert not [prm.name for prm in params
                    if isinstance(prm.default, bool)], fn.__name__
        base = _group_flags(fn)
        for prm in params:
            try:
                flags = _group_flags(fn, **{prm.name: _other_value(prm.default)})
            except (TypeError, ValueError):
                # An unannotated None default may be numeric.
                flags = _group_flags(fn, **{prm.name: 1})
            assert flags == base, (
                f"{fn.__name__}({prm.name}=...) changes the flag set")


# -- cross-tool consistency -----------------------------------------

@pytest.mark.parametrize("tool", TOOLS + EXAMPLES)
def test_tool_short_flags_match_grid(tool):
    pkg = "aiomoqt.tools" if tool in TOOLS else "aiomoqt.examples"
    contract = EXTERNAL_CONTRACT.get(tool, {})
    for act in _parser_actions(tool, pkg):
        for opt in act.option_strings:
            if opt in SHORT_FLAG_MEANING and opt not in contract:
                want = SHORT_FLAG_MEANING[opt]
                assert act.dest == want, (
                    f"{tool}: {opt} means {act.dest!r} but the grid "
                    f"reserves it for {want!r}")


def test_short_flag_has_one_meaning_across_tools():
    meanings = {}
    for tool in TOOLS + EXAMPLES:
        pkg = "aiomoqt.tools" if tool in TOOLS else "aiomoqt.examples"
        contract = EXTERNAL_CONTRACT.get(tool, {})
        for act in _parser_actions(tool, pkg):
            for opt in act.option_strings:
                if opt.startswith("--") or opt in contract:
                    continue
                meanings.setdefault(opt, {}).setdefault(act.dest, []).append(
                    tool)
    clashes = {o: m for o, m in meanings.items() if len(m) > 1}
    assert not clashes


@pytest.mark.parametrize("tool", sorted(EXTERNAL_CONTRACT))
def test_external_contract_matches_its_interface(tool):
    contract = EXTERNAL_CONTRACT[tool]
    acts = _parser_actions(tool)
    for short, long_ in contract.items():
        owners = [a for a in acts if short in a.option_strings]
        assert len(owners) == 1, short
        assert owners[0].option_strings == [short, long_]
        assert owners[0].dest == long_[2:]
    shorts = {o for a in acts for o in a.option_strings
              if not o.startswith("--")}
    assert shorts == set(contract) | {"-h", "-?"}


@pytest.mark.parametrize("tool", TOOLS)
def test_no_tool_reintroduces_dash_q_transport(tool):
    """-q used to mean 'force raw QUIC'. The URL scheme owns that now."""
    for act in _parser_actions(tool):
        assert "-q" not in act.option_strings, (
            f"{tool}: -q is retired — the URL scheme selects transport")


@pytest.mark.parametrize("tool", TOOLS)
def test_draft_is_long_only(tool):
    for act in _parser_actions(tool):
        if act.dest == "draft":
            assert all(o.startswith("--") for o in act.option_strings), (
                f"{tool}: --draft must stay long-only ({act.option_strings})")


@pytest.mark.parametrize("tool", TOOLS + EXAMPLES)
def test_uvloop_is_gone(tool):
    pkg = "aiomoqt.tools" if tool in TOOLS else "aiomoqt.examples"
    for act in _parser_actions(tool, pkg):
        assert "--uvloop" not in act.option_strings, (
            f"{tool}: --uvloop was removed — it never helped")


HELP_FLAGS = ("-h", "-?", "--help")


@pytest.mark.parametrize("tool", TOOLS + EXAMPLES)
def test_help_action_is_the_shared_one(tool):
    pkg = "aiomoqt.tools" if tool in TOOLS else "aiomoqt.examples"
    helps = [a for a in _parser_actions(tool, pkg)
             if isinstance(a, argparse._HelpAction)]
    assert [tuple(a.option_strings) for a in helps] == [HELP_FLAGS]


@pytest.mark.parametrize("flag", HELP_FLAGS)
@pytest.mark.parametrize("tool", TOOLS + EXAMPLES)
def test_help_flags_print_help(tool, flag, monkeypatch, capsys):
    pkg = "aiomoqt.tools" if tool in TOOLS else "aiomoqt.examples"
    mod = importlib.import_module(f"{pkg}.{tool}")
    monkeypatch.setattr(sys, "argv", [tool, flag])
    with pytest.raises(SystemExit) as e:
        mod.parse_args([flag]) if _takes_argv(mod) else mod.parse_args()
    assert e.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_packaging_flag_agrees_across_media_tools():
    """--packaging is long-only and offers the same choices on both media
    tools: no new letter, no per-tool subset."""
    from aiomoqt.media import MEDIA_PACKAGINGS
    for tool in ("pub_media", "sub_media"):
        acts = [a for a in _parser_actions(tool) if a.dest == "packaging"]
        assert len(acts) == 1, tool
        assert acts[0].option_strings == ["--packaging"], tool
        assert tuple(acts[0].choices) == MEDIA_PACKAGINGS, tool


def test_loc_bare_flag_agrees_across_media_tools():
    """--loc-bare is long-only and the same switch on both media tools."""
    for tool in ("pub_media", "sub_media"):
        acts = [a for a in _parser_actions(tool) if a.dest == "loc_bare"]
        assert len(acts) == 1, tool
        assert acts[0].option_strings == ["--loc-bare"], tool
        assert isinstance(acts[0], argparse._StoreTrueAction), tool


def test_load_sim_draft_takes_a_draft_list():
    acts = [a for a in _parser_actions("load_sim") if a.dest == "draft"]
    assert acts[0].type("22,20") == [22, 20]
    assert "22" in acts[0].help


@pytest.mark.parametrize("argv,insecure", [
    ([], True), (["-k"], True), (["--insecure"], True),
    (["--cert", "c.pem", "--key", "k.pem"], False),
    (["--cert", "c.pem", "--key", "k.pem", "-k"], True),
])
def test_loopback_bench_insecure(monkeypatch, argv, insecure):
    from aiomoqt.tools import loopback_bench
    monkeypatch.setattr(sys, "argv", ["loopback_bench", *argv])
    args = loopback_bench.parse_args()
    assert args.insecure is insecure
    assert args.cert
