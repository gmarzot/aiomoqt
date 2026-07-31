"""Shared CLI grid for aiomoqt tools — and for yours.

Every aiomoqt tool builds its parser from these groups, so one flag
means one thing everywhere. Use them when writing your own tool and you
inherit the same contract for free.

The groups take no feature toggles. That is deliberate: a helper with
`add_media(streams=False, datagram=True, ...)` centralizes only the help
TEXT while each call site still picks its own subset and defaults, so
the flags drift apart again one level up. Each group here adds a fixed
set with fixed defaults; a tool needing a different subset asks for a
different group.

THE GRID
--------
addressing
  <URL>              positional, on tools that DIAL somewhere. The
                     scheme selects the transport:
                       moqt://host[:port][/path]   raw QUIC
                       https://host[:port][/path]  WebTransport
                       host[:port]                 WebTransport
                     There is no -q: an address you dial already says
                     how to reach it.
  -Q/--quic          on tools that LISTEN (no URL to imply it). Both
  -W/--wt            together = serve both on one port via per-connection
                     ALPN dispatch. Neither = -W.
  --bind, -p/--port, --path      listener address. The path is shared by
                     both transports: WT CONNECT path / raw-QUIC PATH
                     setup parameter.

identity          -N/--namespace  -T/--trackname
publisher media   -s/--object-size -g/--group-size -P/--streams
                  -r/--rate -D/--datagram --video
run               -t/--duration -i/--interval -d/--debug
session           --draft -k/--insecure --cert/--key --keylogfile
                  --cc-algo --keepalive --max-queued-bytes
                  --max-inflight-bytes --quic-debug --compat
help              -?/--help    (-h stays free for hosts/URLs)

split execution (tools that can run one end or both)
  --role both|pub|sub    which end(s) this process runs
  --mp                   spread this role across processes
"""
import argparse

from aiomoqt.types import parse_draft_spec

# Defaults, one place. Powers of two, same on every tool.
DEFAULT_OBJECT_SIZE = 4096
DEFAULT_GROUP_SIZE = 4096
DEFAULT_DURATION = 30
DEFAULT_INTERVAL = 5.0

# Conservative universal datagram payload cap: the 1200 B frame ceiling
# picoquic admits without consulting live path MTU, minus MoQT header
# margin. The track layer re-checks against the negotiated ceiling.
DGRAM_MAX_OBJECT = 1152

VIDEO_PROFILES = ('240p', '270p', '360p', '480p',
                  '720p', '1080p', '1440p', '4k')


def make_parser(description, epilog=None):
    """Parser in the house style: no -h help (hosts/URLs need it)."""
    return argparse.ArgumentParser(
        add_help=False, description=description, epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter)


def add_help(p):
    p.add_argument('-?', '--help', action='help',
                   help='Show this help message and exit')


# -- addressing ------------------------------------------------------

def add_endpoint(p, required=True):
    """Positional URL for tools that dial. Scheme picks the transport."""
    kw = {} if required else {'nargs': '?', 'default': None}
    p.add_argument('url', metavar='URL', **kw,
                   help='Endpoint. moqt://host[:port][/path] = raw QUIC; '
                        'https://host[:port][/path] = WebTransport; '
                        'host[:port] = WebTransport. The scheme selects '
                        'the transport.')


def add_listener(p, port=4433, path=True):
    """Transport + bind address for tools that listen. -Q -W together
    serve both protocols on one port (ALPN dispatch)."""
    p.add_argument('-Q', '--quic', action='store_true',
                   help='Serve raw QUIC')
    p.add_argument('-W', '--wt', action='store_true',
                   help='Serve H3/WebTransport (default if neither -Q '
                        'nor -W given). With -Q, serves both on one '
                        'port via per-connection ALPN dispatch.')
    p.add_argument('--bind', type=str, default='localhost',
                   help='Bind address (default: localhost)')
    p.add_argument('-p', '--port', type=int, default=port,
                   help=f'Listen port (default: {port})')
    if path:
        p.add_argument('--path', type=str, default='/',
                       help='MoQT path — WT CONNECT path and raw-QUIC '
                            'PATH setup parameter (default: /)')


def resolve_listener(args):
    """Normalize -Q/-W into (serve_quic, serve_wt). Neither = WT."""
    if not getattr(args, 'quic', False) and not getattr(args, 'wt', False):
        return False, True
    return bool(args.quic), bool(args.wt)


# -- identity --------------------------------------------------------

def add_identity(p, namespace='aiomoqt', trackname=None):
    p.add_argument('-N', '--namespace', type=str, default=namespace,
                   help=f'MoQT namespace (default: {namespace})')
    p.add_argument('-T', '--trackname', type=str, default=trackname,
                   help='MoQT track name'
                        + (f' (default: {trackname})' if trackname
                           else ' (default: tool-generated)'))


# -- media -----------------------------------------------------------

def add_publisher_media(p):
    """Fixed publisher-side media set. Subscriber tools don't call this
    — a subscriber chooses neither object shape nor rate."""
    p.add_argument('-s', '--object-size', type=int,
                   default=DEFAULT_OBJECT_SIZE,
                   help=f'Object payload bytes '
                        f'(default: {DEFAULT_OBJECT_SIZE})')
    p.add_argument('-g', '--group-size', type=int,
                   default=DEFAULT_GROUP_SIZE,
                   help=f'Objects per group (default: '
                        f'{DEFAULT_GROUP_SIZE}). Stream turnover rate = '
                        f'rate / group-size; lower it to exercise churn '
                        f'(e.g. -g 240 for 2 s GOPs at 120 obj/s).')
    p.add_argument('-P', '--streams', type=int, default=1,
                   help='Parallel subgroup streams (default: 1)')
    p.add_argument('-r', '--rate', type=float, default=0,
                   help='Aggregate objects/sec across all streams '
                        '(0 = max, default: max). Per-stream emit rate '
                        'is rate/streams.')
    p.add_argument('-D', '--datagram', action='store_true',
                   help=f'ObjectDatagrams instead of subgroup streams '
                        f'(raw QUIC only; object must fit one packet, '
                        f'<= {DGRAM_MAX_OBJECT} B)')
    p.add_argument('--video', type=str, default=None, metavar='RES',
                   choices=VIDEO_PROFILES,
                   help='Video profile — sets object size, rate and GOP '
                        'from the resolution. Overrides -s/-r/-g.')


# -- run / session ---------------------------------------------------

def add_run(p, duration=DEFAULT_DURATION, interval=True):
    p.add_argument('-t', '--duration', type=int, default=duration,
                   help=f'Duration seconds (default: {duration})')
    if interval:
        p.add_argument('-i', '--interval', type=float,
                       default=DEFAULT_INTERVAL,
                       help=f'Report interval seconds '
                            f'(default: {DEFAULT_INTERVAL:g})')
    p.add_argument('-d', '--debug', action='store_true',
                   help='Verbose logging')


def add_session(p, insecure=True, certs=False, keepalive=False,
                compat=False):
    p.add_argument('--draft', type=parse_draft_spec, default=None,
                   help='MoQT draft version: 14, 16, or 18')
    if insecure:
        p.add_argument('-k', '--insecure', action='store_true',
                       help='Skip TLS certificate verification')
    if certs:
        p.add_argument('--cert', type=str, default=None,
                       help='TLS certificate file')
        p.add_argument('--key', type=str, default=None,
                       help='TLS private key file')
    p.add_argument('--keylogfile', type=str, default=None,
                   help='TLS secrets log (NSS Key Log Format) for '
                        'Wireshark decryption')
    p.add_argument('--cc-algo', type=str, default=None,
                   help='Congestion control (bbr | bbr1 | newreno | '
                        'cubic | dcubic | prague | fast). Default: '
                        'aiopquic default (bbr1)')
    if keepalive:
        p.add_argument('--keepalive', type=float, default=None,
                       metavar='SEC',
                       help='QUIC keep-alive interval seconds (PING), so '
                            'a flow-controlled quiet connection is not '
                            'dropped on idle timeout. Default: off')
    p.add_argument('--max-queued-bytes', type=int, default=None,
                   help='Aggregate publisher TX budget across ALL '
                        'streams. Default: aiopquic default (4 MiB). '
                        '0 disables.')
    p.add_argument('--max-inflight-bytes', type=int, default=None,
                   help='Per-stream TX budget. Default: aiomoqt default '
                        '(1 MiB). 0 disables.')
    p.add_argument('--quic-debug', action='store_true',
                   help='Verbose QUIC-layer logging')
    if compat:
        p.add_argument('--compat', type=str, default='',
                       help='Comma-separated relay compat tolerances '
                            '(e.g. lenient-extensions for relays that '
                            'emit a truncated trailing-extensions block '
                            'on control messages)')


def add_split_role(p, mp=True):
    """For tools that can run one end or both. --role says which end(s);
    --mp says whether to spread that role across processes; URL presence
    says whether the peer is a remote relay."""
    p.add_argument('--role', choices=('both', 'pub', 'sub'),
                   default='both',
                   help='Which end(s) this process runs (default: both)')
    if mp:
        p.add_argument('--mp', action='store_true',
                       help='Run this role across separate processes '
                            'instead of one')


# -- validation ------------------------------------------------------

def check_datagram(parser, args, serve_quic=None):
    """-D validity: fits one frame, and raw QUIC is available."""
    if not getattr(args, 'datagram', False):
        return
    size = getattr(args, 'object_size', 0)
    if size > DGRAM_MAX_OBJECT:
        parser.error(
            f'-D/--datagram: object-size {size} can never fit a DATAGRAM '
            f'frame (max ~{DGRAM_MAX_OBJECT} B; frames cannot fragment)')
    if serve_quic is False:
        parser.error('-D/--datagram requires raw QUIC (-Q); '
                     'WebTransport datagram TX is not wired yet')


def check_url_vs_listener(parser, args):
    """A URL means you dial; listener flags mean you bind. Never both."""
    if not getattr(args, 'url', None):
        return
    for flag, attr in (('-Q', 'quic'), ('-W', 'wt'),
                       ('--loopback', 'loopback')):
        if getattr(args, attr, None):
            parser.error(
                f'{flag} is a listener option and cannot be combined '
                f'with a URL — a URL says where to dial, listener '
                f'options say what to bind')
