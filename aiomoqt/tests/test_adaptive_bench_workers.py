"""CLI outcomes and transport settings survive the worker boundary."""
import asyncio
import queue
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aiomoqt.tools import adaptive_bench as ab
from aiomoqt.utils import workers


class _Queue(queue.Queue):
    def cancel_join_thread(self):
        pass

    def close(self):
        pass


def _processes(monkeypatch, *, receive=True, fixed=False, published=True):
    configs = []

    class Process:
        def __init__(self, target, args, **kwargs):
            self.target = target
            self.args = args
            self.alive = False
            self.exitcode = None
            configs.append((target.__name__, args[0]))

        def start(self):
            self.alive = not fixed
            self.exitcode = 0 if fixed else None
            events = self.args[-1]
            cfg = self.args[0]
            if self.target.__name__ in ('pub_worker_entry', 'loopback_server_entry'):
                if fixed and not published:
                    events.put({'kind': 'pub_stats', 'cumulative_objs': 1})
                events.put({'kind': 'pub_health',
                            'state': 'published' if published else 'failed'})
            elif receive:
                batch = self.target.__name__ == 'sub_batch_worker_entry'
                events.put({'kind': 'batch_stats' if batch else 'stats',
                            'worker_id': cfg.get('worker_id', 0),
                            'active': 1, 't': time.monotonic(),
                            'rx_bytes': 1_000_000, 'rx_objs': 1,
                            'total_objs': 1})

        def is_alive(self):
            return self.alive

        def join(self, **kwargs):
            self.alive = False
            self.exitcode = 0

        def terminate(self):
            self.alive = False
            self.exitcode = -15

    monkeypatch.setattr(workers, 'MP_CTX', SimpleNamespace(
        Queue=_Queue, Event=threading.Event, Process=Process))
    return configs


def _argv(monkeypatch, *extra):
    monkeypatch.setattr(sys, 'argv', [
        'moq-adaptive-bench', 'moqt://127.0.0.1:4433',
        '--interval', '0.001', '--start-mbps', '1', '--max-mbps', '1',
        '--max-subs', '1', *extra,
    ])


@pytest.mark.parametrize('receive, expected', [(False, 1), (True, 0)])
async def test_main_exit_status_requires_received_data(monkeypatch, capsys, receive, expected):
    _processes(monkeypatch, receive=receive)
    _argv(monkeypatch)
    assert await ab.main() == expected
    output = capsys.readouterr().out
    if not receive:
        assert 'ABORT: rx=0' in output
    else:
        assert 'reached max' in output


async def test_duration_cap_without_measurements_fails(monkeypatch):
    _processes(monkeypatch, receive=False)
    _argv(monkeypatch, '--duration', '0.000001')
    assert await ab.main() == 1


async def test_no_data_abort_stays_failed_after_earlier_delivery():
    state = ab.BenchState()
    signals = [ab.Signal(t=time.monotonic(), level=1, latency_mean_ms=0,
                         latency_p90_ms=0, loss_pct=0, shortfall=False,
                         target_mbps=1, tx_mbps=1, rx_mbps=rx)
               for rx in (1, 0, 0, 0)]
    actuator = SimpleNamespace(initial_level=1, unit='Mbps',
                               apply=AsyncMock(), observe=AsyncMock(side_effect=signals))
    controller = ab.AIMDController(actuator, state, interval_s=0.001)
    await controller.run()
    assert state.received_data and state.failed
    assert 'no data received' in state.ceiling_reason


@pytest.mark.parametrize('published, expected', [(False, 1), (True, 0)])
async def test_fixed_publisher_requires_confirmation(monkeypatch, published, expected):
    _processes(monkeypatch, fixed=True, published=published)
    _argv(monkeypatch, '--mode', 'subs', '--role', 'pub')
    assert await ab.main() == expected


@pytest.mark.parametrize('mode, role', [('bw', 'both'), ('subs', 'both'),
                                       ('subs', 'sub'), ('subs', 'pub'),
                                       ('mp', 'both')])
@pytest.mark.parametrize('budget', [0, 123456])
async def test_cli_passes_transport_settings_to_every_worker(monkeypatch, mode, role, budget):
    configs = _processes(monkeypatch, fixed=role == 'pub')
    _argv(monkeypatch, '--mode', 'bw' if mode == 'mp' else mode,
          '--role', role, '--cc-algo', 'cubic',
          '--max-queued-bytes', str(budget), '--max-inflight-bytes', str(budget),
          '--keepalive', '2.5')
    if mode == 'mp':
        sys.argv.remove('moqt://127.0.0.1:4433')
        sys.argv.extend(['--mp', '--cert', 'certs/cert.pem', '--key', 'certs/key.pem'])
    assert await ab.main() == 0
    assert configs
    for _, cfg in configs:
        assert cfg['congestion_control_algorithm'] == 'cubic'
        assert cfg['tx_max_queued_bytes'] == budget
        assert cfg['tx_max_inflight_bytes'] == (budget or None)
        assert cfg['keep_alive_interval'] == 2.5


class _Constructed(BaseException):
    """Stop at the transport constructor, before opening any socket."""


@pytest.mark.parametrize('kind', ['publisher', 'subscriber', 'batch', 'loopback'])
@pytest.mark.parametrize('budget', [None, 123456])
async def test_worker_applies_transport_settings(monkeypatch, kind, budget):
    captured = {}

    def capture(*args, **kwargs):
        captured.update(kwargs)
        raise _Constructed

    monkeypatch.setattr(workers, 'MOQTClient', capture)
    from aiomoqt import server
    monkeypatch.setattr(server, 'MOQTServer', capture)
    monkeypatch.setattr(workers, '_bridge_stop_event', lambda _: asyncio.Event())
    cfg = dict(relay_url='moqt://127.0.0.1:4433', sub_id=0,
               namespace='ns', trackname='track', port=4433,
               cert='test-cert', key='test-key',
               congestion_control_algorithm='cubic', tx_max_queued_bytes=0,
               tx_max_inflight_bytes=budget, keep_alive_interval=2.5)
    with pytest.raises(_Constructed):
        if kind == 'publisher':
            await workers._publisher_task(cfg, None, None, None)
        elif kind == 'subscriber':
            await workers._subscriber_task(cfg, None, None)
        elif kind == 'batch':
            relay = SimpleNamespace(host='127.0.0.1', port=4433, path='', use_quic=True)
            await workers._slot_supervisor(cfg, relay, asyncio.Event(), None, {}, 0)
        else:
            await workers._loopback_server_task(cfg, None, None, None)
    assert captured['congestion_control_algorithm'] == 'cubic'
    assert captured['tx_max_queued_bytes'] == 0
    assert captured['tx_max_inflight_bytes'] is budget
    assert captured['keep_alive_interval'] == 2.5
