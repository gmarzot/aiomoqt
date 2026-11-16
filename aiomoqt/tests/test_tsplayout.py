"""PCR-locked playout and TS analysis (aiomoqt.media.tsplayout) against
synthetic PCR schedules: release times, late and lost objects and the
breaks they make, the buffers recomputed in one run, PCR discontinuities
and wrap, CC errors, and the JSON lines' schema."""
import io
import json
import os
import random

import jsonschema
import pytest

from aiomoqt.media.tsplayout import BUFFERS_MS, TsPlayout
from aiomoqt.tests.test_msfts import null, ts

MS = 1_000_000
BASE = 10 ** 12
PID = 0x100
SCHEMA = os.path.join(os.path.dirname(__file__), 'vectors',
                      'ts_analysis_schema.json')


def cbr(n, *, every=10, start=0, jump=None):
    """n packets on PID, one a millisecond on the PCR clock, a PCR on
    every `every`-th. `jump` = (index, ticks, discontinuity flag) moves
    the PCR from that packet on."""
    out = []
    for j in range(n):
        pcr = None
        disc = False
        if j % every == 0:
            pcr = start + j * 27_000
            if jump is not None and j >= jump[0]:
                pcr += jump[1]
                disc = jump[2] and j == jump[0]
            pcr %= (1 << 33) * 300
        out.append(ts(PID, b'\x11' * (176 if pcr is not None else 184),
                      cc=j & 15, pcr=pcr, disc=disc))
    return out


def objects(pkts, size=7):
    return [b''.join(pkts[i:i + size]) for i in range(0, len(pkts), size)]


def feed(p, objs, avail_ms, skip=()):
    """Object k available at BASE + avail_ms(k) ms."""
    for k, o in enumerate(objs):
        if k in skip:
            continue
        p.push(o, BASE + int(avail_ms(k) * MS), 0, k)


def _on_time(k):
    return 7 * k + 6                 # its last packet's send time


def _stalled(k):
    # Object 300 is 400 ms late; the ones behind it wait for it.
    return max(_on_time(k), _on_time(300) + 400) if k >= 300 else _on_time(k)


LOST = range(100, 143)               # 301 ms of packets


# -- release ----------------------------------------------------------------

def test_packets_are_released_at_their_pcr_time():
    p = TsPlayout(100)
    pkts = cbr(70)
    feed(p, objects(pkts), _on_time)
    anchor = BASE + 6 * MS           # the first PCR's arrival
    assert p.due() == anchor + 100 * MS
    assert p.pop(anchor + 100 * MS - 1) == b''
    assert p.pop(anchor + 100 * MS) == pkts[0]
    assert p.pop(anchor + 109 * MS) == b''.join(pkts[1:10])
    assert p.pop(anchor + 10 ** 6 * MS) == b''.join(pkts[10:])
    assert p.due() is None


@pytest.mark.parametrize('late', ['drop', 'emit'])
def test_late_and_lost_objects_make_exactly_the_expected_breaks(late):
    p = TsPlayout(100, late=late)
    feed(p, objects(cbr(3000)), _stalled, skip=LOST)
    s = p.summary()
    anchor = BASE + 6 * MS
    # Late: packets 2100.. due before the stall ends, at 2500 ms.
    assert {b: c['late'] for b, c in s['buffers'].items()} == {
        '100': 300, '200': 200, '300': 100, '500': 0, '1000': 0}
    # The loss: packet 699 (due 799 ms) to 1001 (due 1101 ms). The stall:
    # 2099 (due 2199 ms) to what follows at 2500 ms; at 200 ms 2299 to
    # 2500; at 300 ms or more under 200 ms.
    assert {b: (c['breaks'], c['break_ms'])
            for b, c in s['buffers'].items()} == {
        '100': (2, 603.0), '200': (2, 503.0), '300': (1, 302.0),
        '500': (1, 302.0), '1000': (1, 302.0)}
    play = s['playout']
    assert (play['late'], play['breaks'], play['break_ms']) == (300, 2, 603.0)
    assert play['break_list'] == [
        {'start_ns': anchor + 799 * MS, 'duration_ms': 302.0},
        {'start_ns': anchor + 2199 * MS, 'duration_ms': 301.0}]
    assert p.dropped == (300 if late == 'drop' else 0)
    assert s['objects'] == {'received': 386, 'lost': 43, 'dups': 0,
                            'groups': 1, 'groups_missing': 0}
    assert s['cc_errors'] == {str(PID): 1}
    released = []
    while p.due() is not None:
        released.append(p.pop(p.due()))
    assert len(b''.join(released)) // 188 == 3000 - 301 - p.dropped


def test_one_run_answers_every_buffer():
    rng = random.Random(7)
    jitter = [rng.expovariate(1 / 40) for _ in range(500)]
    objs = objects(cbr(3500))
    one = TsPlayout(None)
    feed(one, objs, lambda k: _on_time(k) + jitter[k], skip=range(200, 230))
    got = one.summary()['buffers']
    for ms in BUFFERS_MS:
        alone = TsPlayout(ms)
        feed(alone, objs, lambda k: _on_time(k) + jitter[k],
             skip=range(200, 230))
        play = alone.summary()['playout']
        assert got[str(ms)] == {k: play[k] for k in ('late', 'breaks',
                                                    'break_ms')}, ms
    assert got['100']['late'] > got['1000']['late']


def test_a_missing_object_is_placed_by_the_next_pcr():
    p = TsPlayout(100)
    pkts = cbr(80)
    objs = objects(pkts)
    feed(p, objs, _on_time, skip={3})   # packets 21-27 lost
    anchor = BASE + 6 * MS
    out = []
    while p.due() is not None:
        out.append((p.due() - anchor - 100 * MS) // MS)
        p.pop(p.due())
    assert out == [j for j in range(80) if not 21 <= j <= 27]


# -- PCR ------------------------------------------------------------------

@pytest.mark.parametrize('jump', [(500, 270_000_000, True),
                                  (500, -27_000_000 * 300, False)],
                         ids=['forward-flagged', 'backward'])
def test_pcr_discontinuity_is_rebased(jump):
    p = TsPlayout(100)
    feed(p, objects(cbr(1400, jump=jump)), _on_time)
    s = p.summary()
    assert s['pcr']['discontinuities'] == 1
    assert s['playout']['late'] == 0 and s['playout']['breaks'] == 0
    assert s['buffers']['100']['breaks'] == 0


def test_pcr_wrap_is_not_a_discontinuity():
    start = (1 << 33) * 300 - 27_000 * 500
    p = TsPlayout(100)
    feed(p, objects(cbr(1400, start=start)), _on_time)
    s = p.summary()
    assert s['pcr']['discontinuities'] == 0
    assert s['pcr']['interval_ms'] == {'mean': 10.0, 'max': 10.0}
    assert s['playout']['breaks'] == 0 and s['playout']['late'] == 0


def test_delay_and_cushion():
    p = TsPlayout(None)
    # Every object on time but one, 50 ms late.
    feed(p, objects(cbr(700)), lambda k: 7 * k + (50 if k == 40 else 0))
    s = p.summary()
    assert s['delay_ms']['p0'] == 0.0 and s['delay_ms']['p50'] == 0.1
    assert s['delay_ms']['p100'] == pytest.approx(50.1)
    assert s['cushion_ms'] == {'p999': pytest.approx(50.1),
                               'p100': pytest.approx(50.1)}
    assert s['pcr']['jitter_ms'] > 0


# -- CC -----------------------------------------------------------------

def test_cc_errors_per_pid():
    a, b = 0x101, 0x102
    pkts = []
    for j in range(40):
        if j != 17:                                    # one lost on a
            pkts.append(ts(a, b'\x01' * 184, cc=j & 15))
        pkts.append(ts(b, b'\x02' * 184, cc=j & 15))
        if j == 5:
            pkts.append(ts(b, b'\x02' * 184, cc=j & 15))  # a duplicate
        pkts.append(ts(b, b'', cc=j & 15, pcr=j * 27_000))  # no payload
        pkts.append(null())
    pkts.append(ts(a, b'\x01' * 182, cc=3, disc=True))  # flagged: no error
    pkts.append(ts(a, b'\x01' * 184, cc=9))              # an error
    p = TsPlayout(None, pcr_pid=b)
    for k, o in enumerate(objects(pkts)):
        p.push(o, BASE + k * MS, 0, k)
    s = p.summary()
    assert s['cc_errors'] == {str(a): 2}
    assert s['cc_errors_total'] == 2 and s['cc_dups'] == 1
    assert s['ts_packets'] == len(pkts)
    assert s['pcr']['pid'] == b and s['pcr']['count'] == 40


# -- objects, wall clocks, logs, schema ---------------------------------

def test_object_loss_groups_and_end_to_end_delay():
    p = TsPlayout(None, wall_offset_ns=5 * MS)
    pkts = objects(cbr(140))
    p.push(pkts[0], BASE, 0, 0, wall_us=(BASE - 20 * MS) // 1000)
    p.push(pkts[1], BASE + MS, 0, 1)
    p.push(pkts[2], BASE + 2 * MS, 0, 3)                 # one lost
    p.push(pkts[3], BASE + 3 * MS, 2, 1)                 # group 1, 2.0
    s = p.summary()
    assert s['objects'] == {'received': 4, 'lost': 2, 'dups': 0,
                            'groups': 2, 'groups_missing': 1}
    assert s['e2e_ms']['source'] == 'loc-timestamp'
    assert s['e2e_ms']['samples'] == 1
    assert s['e2e_ms']['p100'] == pytest.approx(25.1)

    q = TsPlayout(None, wall_offset_ns=0)
    q.push(pkts[0], BASE, 7, 0)
    q.timeline([[0, [7, 0], (BASE - 30 * MS) // MS], [40, [8, 0], 0]])
    q.timeline([[0, [7, 0], (BASE - 30 * MS) // MS]])    # repeated: once
    q.timeline([[40, [8, 0], (BASE - 10 * MS) // MS]])
    q.push(pkts[1], BASE + 5 * MS, 8, 0)
    e2e = q.summary()['e2e_ms']
    assert e2e['source'] == 'media-timeline' and e2e['samples'] == 2
    assert e2e['p100'] == pytest.approx(30.1)


def test_arrival_log_has_a_line_per_pcr():
    log = io.StringIO()
    p = TsPlayout(200, arrival_log=log, wall_offset_ns=7)
    feed(p, objects(cbr(100)), _on_time)
    lines = [json.loads(ln) for ln in log.getvalue().splitlines()]
    assert len(lines) == 10
    anchor = BASE + 6 * MS + 7
    for k, rec in enumerate(lines):
        assert set(rec) == {'pcr', 'arrival_ns', 'release_ns'}
        assert rec['pcr'] == k * 10 * 27_000
        assert rec['release_ns'] == anchor + 200 * MS + k * 10 * MS
        assert rec['arrival_ns'] >= anchor


def test_report_lines_match_the_schema():
    with open(SCHEMA) as fh:
        schema = json.load(fh)
    for buffer_ms in (None, 300):
        p = TsPlayout(buffer_ms, wall_offset_ns=1)
        objs = objects(cbr(3000))
        lines = []
        for k, o in enumerate(objs):
            if k in LOST:
                continue
            p.push(o, BASE + int(_stalled(k) * MS), 0, k,
                   wall_us=BASE // 1000)
            if k % 100 == 99:
                lines.append({'type': 'interval', 'track': 'program-1',
                              't0': 0, 't1': 1, **p.interval()})
        lines.append({'type': 'interval', 'track': 'program-1', 't0': 0,
                      't1': 1, **p.interval()})
        lines.append({'type': 'summary', 'track': 'program-1', 't0': 0,
                      't1': 1, **p.summary()})
        for line in lines:
            jsonschema.validate(json.loads(json.dumps(line)), schema)
        total = lines[-1]
        assert total['ts_packets'] == sum(x['ts_packets']
                                          for x in lines[:-1])
        assert total['buffers']['100']['late'] == sum(
            x['buffers']['100']['late'] for x in lines[:-1])
    empty = {'type': 'summary', 'track': 't', 't0': 0, 't1': 0,
             **TsPlayout(None).summary()}
    jsonschema.validate(empty, schema)


def test_wall_clock_is_read_per_object():
    """A wall clock that steps against the monotonic one moves only the
    objects after the step."""
    log = io.StringIO()
    p = TsPlayout(100, arrival_log=log)
    objs = objects(cbr(70))
    wall = BASE * 3
    for k, o in enumerate(objs):
        step = 20 * MS if k >= 5 else 0
        at = BASE + _on_time(k) * MS
        p.push(o, at, 0, k, wall_us=(wall + at - BASE - MS) // 1000,
               wall_ns=wall + at - BASE + step)
    e2e = p.summary()['e2e_ms']
    assert e2e['samples'] == 10
    assert (e2e['p50'], e2e['p100']) == (pytest.approx(1.1),
                                         pytest.approx(21.1))
    recs = [json.loads(ln) for ln in log.getvalue().splitlines()]
    assert recs[0]['arrival_ns'] == wall + 6 * MS
    # The last PCR, packet 60, is in object 8.
    assert recs[-1]['arrival_ns'] == wall + _on_time(8) * MS + 20 * MS
    assert recs[-1]['release_ns'] - recs[-1]['arrival_ns'] == (
        6 * MS + 100 * MS + 60 * MS - _on_time(8) * MS)



def test_the_pmt_names_the_pcr_pid():
    from aiomoqt.tests.test_msfts import pat, pmt
    other = 0x200
    head = pat() + pmt(((0x02, other),), pcr_pid=other)
    pkts = []
    for j in range(200):
        pcr = j * 27_000 if j % 10 == 0 else None
        pkts.append(ts(other, b'\x11' * (176 if pcr is not None else 184),
                       cc=j & 15, pcr=pcr))
        pkts.append(ts(PID, b'', cc=0, pcr=10 ** 12))   # a decoy PCR
    p = TsPlayout(100, pcr_pid=PID)          # the catalog said PID
    p.push(head, BASE, 0, 0)
    for k, o in enumerate(objects(pkts, 14), 1):
        p.push(o, BASE + int(_on_time(k) * MS), 0, k)
    s = p.summary()
    assert s['pcr']['pid'] == other and s['pcr']['count'] == 20
    assert s['pcr']['interval_ms'] == {'mean': 10.0, 'max': 10.0}


def test_a_switch_starts_the_next_track_clean():
    p = TsPlayout(300)
    a = objects(cbr(700))
    for k, o in enumerate(a):
        p.push(o, BASE + _on_time(k) * MS, 3, k)
    # The next track: groups from 0, its counters restarted mid-span.
    b = objects(cbr(700, start=700 * 27_000))
    p.switch()
    for k, o in enumerate(b[20:]):
        p.push(o, BASE + (_on_time(len(a)) + 140 + 7 * k) * MS, 0, k)
    s = p.summary()
    assert s['switches'] == 1
    assert s['cc_errors_total'] == 0
    assert s['objects']['lost'] == 0 and s['objects']['groups_missing'] == 0
    assert s['objects']['groups'] == 2
    assert s['playout']['late'] == 0
    assert s['pcr']['discontinuities'] == 0
