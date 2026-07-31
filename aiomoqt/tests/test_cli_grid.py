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

import pytest

from aiomoqt.utils import cli


TOOLS = [
    "pub_bench", "sub_bench", "loopback_bench", "adaptive_bench",
    "pub_server", "load_sim", "relay_probe",
    "moq_interop_client", "moq_interop_relay",
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

# No tool is exempt wholesale. moq_interop_client keeps two flags that
# collide with the grid because the external interop-runner invokes it
# with them; they are enumerated so the exemption is auditable and
# disappears the moment the runner PR lands. Everything else about that
# tool — and every other tool — must satisfy the grid.
GRID_EXEMPT: set = set()
KNOWN_GRID_VIOLATIONS = {
    # tool: {short flag: dest it currently means}
    "moq_interop_client": {"-r": "relay", "-t": "test"},
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
    cli.add_endpoint(p, required=False)
    cli.add_listener(p)
    with pytest.raises(SystemExit):
        cli.check_url_vs_listener(p, p.parse_args(["moqt://h", "-Q"]))
    cli.check_url_vs_listener(p, p.parse_args(["moqt://h"]))       # ok
    cli.check_url_vs_listener(p, p.parse_args(["-Q"]))             # ok


def test_groups_take_no_feature_toggles():
    """The anti-drift property: a caller cannot ask for a subset, so
    every tool that adds a group gets the identical flag set."""
    import inspect
    for fn in (cli.add_publisher_media, cli.add_identity, cli.add_endpoint):
        params = inspect.signature(fn).parameters
        bool_toggles = [n for n, prm in params.items()
                        if isinstance(prm.default, bool)
                        and n not in ("required",)]
        assert not bool_toggles, (
            f"{fn.__name__} grew boolean toggles {bool_toggles} — that "
            f"moves the flag choice back to each call site")


# -- cross-tool consistency -----------------------------------------

@pytest.mark.parametrize("tool", TOOLS)
def test_tool_short_flags_match_grid(tool):
    allowed = KNOWN_GRID_VIOLATIONS.get(tool, {})
    for act in _parser_actions(tool):
        for opt in act.option_strings:
            if opt in SHORT_FLAG_MEANING:
                if allowed.get(opt) == act.dest:
                    continue      # enumerated, pending the runner PR
                want = SHORT_FLAG_MEANING[opt]
                ok = act.dest == want or act.dest.endswith("_" + want)
                assert ok, (
                    f"{tool}: {opt} means {act.dest!r} but the grid "
                    f"reserves it for {want!r}")


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


@pytest.mark.parametrize("tool", TOOLS + EXAMPLES)
def test_help_flag_is_question_mark(tool):
    """-h stays free for hosts/URLs, so help is -?/--help."""
    pkg = "aiomoqt.tools" if tool in TOOLS else "aiomoqt.examples"
    acts = _parser_actions(tool, pkg)
    helps = [a for a in acts if isinstance(a, argparse._HelpAction)]
    if not helps:
        pytest.skip(f"{tool}: no help action captured")
    opts = {o for a in helps for o in a.option_strings}
    assert "-?" in opts or "--help" in opts


def test_known_violations_are_still_real():
    """The exemption list must not outlive the violations it covers —
    once the interop-runner PR lands and the flags change, this fails
    and the stale entry gets deleted."""
    for tool, flags in KNOWN_GRID_VIOLATIONS.items():
        actual = {o: a.dest for a in _parser_actions(tool)
                  for o in a.option_strings}
        for opt, dest in flags.items():
            assert actual.get(opt) == dest, (
                f"{tool}: {opt} no longer means {dest!r} — remove it "
                f"from KNOWN_GRID_VIOLATIONS")
