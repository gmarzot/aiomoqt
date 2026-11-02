"""Every failed join releases its parent's pending slot and records its cause."""
import asyncio
import queue
import time
from types import SimpleNamespace

import pytest

from aiomoqt.tools import load_sim as ls
from aiomoqt.utils.stats import TrackStats


@pytest.mark.parametrize('stage', ['connect', 'setup', 'subscribe'])
async def test_failed_join_is_reported_once_and_releases_pending(monkeypatch, stage):
    detail = f'{stage} refused'

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            return self

        async def __aenter__(self):
            if stage == 'connect':
                raise ConnectionError(detail)
            return self

        async def __aexit__(self, *args):
            pass

        async def client_session_init(self):
            if stage == 'setup':
                raise ConnectionError(detail)

        def close(self):
            pass

    class Track:
        timing = None

        def __init__(self, *args, **kwargs):
            pass

        async def subscribe(self, **kwargs):
            raise ConnectionError(detail)

    monkeypatch.setattr(ls, 'MOQTClient', Client)
    monkeypatch.setattr(ls, 'SubscribedTrack', Track)
    events = queue.Queue()
    slot = dict(stop=asyncio.Event(), stats=TrackStats(), live=False, done=False)
    relay = SimpleNamespace(host='127.0.0.1', port=4433, path='', use_quic=True)
    await ls._slot_task({'namespace': 'ns', 'trackname': 'track'}, relay,
                        slot, events, 0, 0, 0)
    assert slot['done'] and not slot['live']
    assert events.qsize() == 1
    event = events.get_nowait()
    assert event['ev'] == 'join_failed' and detail in event['why']

    pub = ls.PubSpec({'namespace': 'ns', 'track': 'track', 'bitrate_kbps': 100}, 1)
    group = ls.GroupRT(ls.GroupSpec(0, {'subs': 1}, pub))
    shard = ls.ShardRT(0, 1, None, None, None)
    shard.pending.append(time.monotonic())
    group.shards.append(shard)
    events.put(event)
    ls._drain_sub_events(events, {0: group})
    assert group.fails == 1 and group.iv['fail'] == 1
    assert not shard.pending and shard.occupancy() == 0


async def test_timeout_reports_its_type(monkeypatch):
    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            return self

        async def __aenter__(self):
            raise TimeoutError()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(ls, 'MOQTClient', Client)
    events = queue.Queue()
    slot = dict(stop=asyncio.Event(), stats=TrackStats(), live=False, done=False)
    relay = SimpleNamespace(host='127.0.0.1', port=4433, path='', use_quic=True)
    await ls._slot_task({}, relay, slot, events, 0, 0, 0)
    assert events.get_nowait()['why'] == 'TimeoutError: '


@pytest.mark.parametrize('delay', [0, 5])
async def test_stop_mid_join_is_not_a_failure(monkeypatch, delay):
    stop = asyncio.Event()

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            return self

        async def __aenter__(self):
            stop.set()
            raise ConnectionError('closed by stop')

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(ls, 'MOQTClient', Client)
    events = queue.Queue()
    slot = dict(stop=stop, stats=TrackStats(), live=False, done=False)
    if delay:
        stop.set()
    relay = SimpleNamespace(host='127.0.0.1', port=4433, path='', use_quic=True)
    await ls._slot_task({}, relay, slot, events, 0, 0, delay)
    assert slot['done']
    event = events.get_nowait()
    assert event['ev'] == 'join_cancelled' and events.empty()

    pub = ls.PubSpec({'namespace': 'ns', 'track': 'track', 'bitrate_kbps': 100}, 1)
    group = ls.GroupRT(ls.GroupSpec(0, {'subs': 1}, pub))
    shard = ls.ShardRT(0, 1, None, None, None)
    shard.pending.append(time.monotonic())
    group.shards.append(shard)
    events.put(event)
    ls._drain_sub_events(events, {0: group})
    assert group.fails == 0 and not shard.pending
