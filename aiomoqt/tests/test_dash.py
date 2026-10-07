"""DASH manifest handling: MPD parsing, segment addressing, selection,
and the segment feed's live timing, against in-test MPDs with a fake
fetch and clock (no network, no PyAV)."""
import threading
from fractions import Fraction

import pytest

from aiomoqt.media.dash import (
    DashError, Presentation, SegmentFeed, expand, parse_datetime,
    parse_duration, parse_mpd,
)

_NS = 'xmlns="urn:mpeg:dash:schema:mpd:2011"'


def test_parse_duration_and_datetime():
    assert parse_duration('PT2S') == 2
    assert parse_duration('PT1H2M3.5S') == Fraction(7447, 2)
    assert parse_duration('P1DT1S') == 86401
    assert parse_duration(None) is None
    with pytest.raises(DashError):
        parse_duration('2 seconds')
    assert parse_datetime('1970-01-01T00:16:40Z') == 1000.0
    assert parse_datetime('1970-01-01T00:00:01.5') == 1.5


def test_expand():
    assert expand('$RepresentationID$/$Number%05d$.m4s', 'v1', number=42) \
        == 'v1/00042.m4s'
    assert expand('s-$Time$-$Bandwidth$$$.m4s', 'a', time_=900, bandwidth=8) \
        == 's-900-8$.m4s'
    with pytest.raises(DashError):
        expand('$Number$.m4s', 'v')


_STATIC = f"""<MPD {_NS} type="static" mediaPresentationDuration="PT5S">
  <BaseURL>https://cdn.example/root/</BaseURL>
  <Period>
    <BaseURL>p0/</BaseURL>
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <SegmentTemplate timescale="1000" initialization="$RepresentationID$/init.mp4"
                       media="$RepresentationID$/$Time$.m4s"/>
      <Representation id="hi" codecs="avc1.64001f" bandwidth="3000000"
                      width="1280" height="720">
        <SegmentTemplate startNumber="10">
          <SegmentTimeline><S t="0" d="2000" r="1"/><S d="1000"/></SegmentTimeline>
        </SegmentTemplate>
      </Representation>
      <Representation id="lo" codecs="avc1.64001e" bandwidth="800000"
                      width="640" height="360">
        <BaseURL>../alt/</BaseURL>
        <SegmentTemplate>
          <SegmentTimeline><S t="0" d="2000" r="-1"/></SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4" lang="fr">
      <SegmentTemplate timescale="48000" duration="96000" startNumber="1"
                       initialization="a/init.mp4" media="a/$Number$.m4s"/>
      <Representation id="a1" codecs="mp4a.40.2" bandwidth="64000"/>
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4" lang="en">
      <Role schemeIdUri="urn:mpeg:dash:role:2011" value="main"/>
      <SegmentList timescale="1000" duration="2500">
        <Initialization sourceURL="b.mp4" range="0-99"/>
        <SegmentURL media="b.mp4" mediaRange="100-199"/>
        <SegmentURL media="b.mp4" mediaRange="200-299"/>
      </SegmentList>
      <Representation id="b1" codecs="mp4a.40.2" bandwidth="96000"/>
      <Representation id="b2" codecs="mp4a.40.2" bandwidth="128000"/>
    </AdaptationSet>
    <AdaptationSet contentType="text" mimeType="application/mp4">
      <Representation id="t" codecs="stpp" bandwidth="1"/>
    </AdaptationSet>
  </Period>
</MPD>""".encode()


def _locate(rep, live_edge=None):
    return [(s.number, s.start, s.duration, rep.locate(s).url, s.byte_range)
            for s in rep.segments(live_edge)]


def test_static_mpd_addressing():
    mpd = parse_mpd(_STATIC, 'https://cdn.example/manifest.mpd')
    reps = {r.id: r for r in mpd.representations}
    hi, lo, a1, b1 = reps['hi'], reps['lo'], reps['a1'], reps['b1']
    assert (hi.kind, hi.width, hi.timescale, hi.start_number) == ('video', 1280, 1000, 10)
    assert hi.init_url == 'https://cdn.example/root/p0/hi/init.mp4'
    assert _locate(hi) == [
        (10, 0, 2000, 'https://cdn.example/root/p0/hi/0.m4s', None),
        (11, 2000, 2000, 'https://cdn.example/root/p0/hi/2000.m4s', None),
        (12, 4000, 1000, 'https://cdn.example/root/p0/hi/4000.m4s', None)]
    # r=-1 repeats to the period end; BaseURL resolves per level
    assert [u for *_, u, _ in _locate(lo)] == [
        f'https://cdn.example/root/alt/lo/{t}.m4s' for t in (0, 2000, 4000)]
    # $Number$ with @duration: ceil(5 s / 2 s) segments
    assert [(n, u) for n, _, _, u, _ in _locate(a1)] == [
        (1, 'https://cdn.example/root/p0/a/1.m4s'),
        (2, 'https://cdn.example/root/p0/a/2.m4s'),
        (3, 'https://cdn.example/root/p0/a/3.m4s')]
    assert (b1.init_url, b1.init_range) == ('https://cdn.example/root/p0/b.mp4', '0-99')
    assert [(n, s, r) for n, s, _, _, r in _locate(b1)] == [
        (1, 0, '100-199'), (2, 2500, '200-299')]
    assert not reps['t'].supported


def test_selection():
    pres = Presentation('https://cdn.example/manifest.mpd',
                        lambda url, rng=None: _STATIC)
    videos, audio = pres.select('best', True)
    assert [v.id for v in videos] == ['hi']
    assert audio.id == 'b2'  # main-role set first, then its best bandwidth
    videos, audio = pres.select('all', False)
    assert [v.id for v in videos] == ['hi', 'lo'] and audio is None


@pytest.mark.parametrize('extra, reason', [
    ('<ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011"/>',
     'encrypted'),
    ('<SegmentBase indexRange="0-99"/>', 'SegmentBase'),
])
def test_unsupported_representations_explain_themselves(extra, reason):
    xml = f"""<MPD {_NS} type="static" mediaPresentationDuration="PT2S">
      <Period><AdaptationSet mimeType="video/mp4">{extra}
        <Representation id="v" codecs="avc1.64001f" bandwidth="1"/>
      </AdaptationSet></Period></MPD>""".encode()
    pres = Presentation('http://x/m.mpd', lambda url, rng=None: xml)
    with pytest.raises(DashError, match=reason):
        pres.select('best', True)


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


def _feed(pres, rep_id, stop, start=None):
    rep = pres.representation(rep_id)
    return SegmentFeed(pres, rep, pres.start_time([rep]) if start is None
                       else start, stop)


_LIVE = f"""<MPD {_NS} type="dynamic" availabilityStartTime="1970-01-01T00:00:00Z"
     minimumUpdatePeriod="PT10S" suggestedPresentationDelay="PT4S">
  <UTCTiming schemeIdUri="urn:mpeg:dash:utc:direct:2014" value="1970-01-01T00:16:40Z"/>
  <Period start="PT0S">
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <SegmentTemplate timescale="90000" duration="180000" startNumber="0"
                       initialization="$RepresentationID$/init.mp4"
                       media="$RepresentationID$/$Number$.m4s"/>
      <Representation id="v" codecs="avc1.64001f" bandwidth="1"/>
    </AdaptationSet>
  </Period>
</MPD>""".encode()


def test_live_number_feed_waits_for_availability():
    clock = _Clock(990.0)  # UTCTiming says it is really 1000.0
    fetched = []

    def fetch(url, rng=None):
        if url.endswith('.mpd'):
            return _LIVE
        fetched.append((round(clock.t + 10, 1), url.rsplit('/', 2)[-2:]))
        return url.encode()

    stop = threading.Event()
    pres = Presentation('http://live/m.mpd', fetch, clock=clock, sleep=clock.sleep)
    assert pres.now() == 1000.0
    feed = _feed(pres, 'v', stop)
    assert feed.read(1 << 20) == b'http://live/v/init.mp4'
    # 4 s behind a 1000 s edge, 2 s segments: start in segment 498
    for _ in range(4):
        feed.read(1 << 20)
    assert fetched[1:] == [
        (1000.0, ['v', '498.m4s']), (1000.0, ['v', '499.m4s']),
        (1002.0, ['v', '500.m4s']), (1004.0, ['v', '501.m4s'])]
    assert feed.segment_of(Fraction(998)) == 499
    assert feed.segment_of(Fraction(9978, 10)) == 498
    assert feed.segment_of(Fraction(9995, 10)) == 499  # just before 500
    assert feed.segment_of(Fraction(99995, 100)) == 500  # within the slack
    stop.set()
    assert feed.read(1) == b''


def _timeline_mpd(start_number, entries):
    s = ''.join(f'<S t="{t}" d="2000"/>' for t in entries)
    return f"""<MPD {_NS} type="dynamic" availabilityStartTime="1970-01-01T00:00:00Z"
         minimumUpdatePeriod="PT2S">
      <Period start="PT0S"><AdaptationSet mimeType="video/mp4">
        <SegmentTemplate timescale="1000" startNumber="{start_number}"
                         initialization="init.mp4" media="$Time$.m4s">
          <SegmentTimeline>{s}</SegmentTimeline></SegmentTemplate>
        <Representation id="v" codecs="avc1.64001f" bandwidth="1"/>
      </AdaptationSet></Period></MPD>""".encode()


def test_live_timeline_refresh_keeps_numbers_monotonic():
    # The window slides between reloads; startNumber is never updated.
    clock = _Clock(106.0)
    versions = [_timeline_mpd(1, [100000, 102000, 104000]),
                _timeline_mpd(1, [102000, 104000, 106000])]
    fetched = []

    def fetch(url, rng=None):
        if url.endswith('.mpd'):
            return versions[0] if clock.t < 108.0 else versions[1]
        fetched.append(url.rsplit('/', 1)[-1])
        return b'x'

    stop = threading.Event()
    pres = Presentation('http://live/m.mpd', fetch, clock=clock, sleep=clock.sleep)
    feed = _feed(pres, 'v', stop, start=Fraction(102))
    for _ in range(4):
        feed.read(1)
    assert fetched == ['init.mp4', '102000.m4s', '104000.m4s', '106000.m4s']
    assert [n for *_, n in feed._history] == [2, 3, 4]


def test_static_feed_takes_each_segment_once():
    # A short last segment once made the next-segment pick repeat it.
    xml = f"""<MPD {_NS} type="static" mediaPresentationDuration="PT4.1S">
      <Period><AdaptationSet mimeType="audio/mp4">
        <SegmentTemplate timescale="1000" initialization="i.mp4" media="$Time$.m4s">
          <SegmentTimeline><S t="0" d="2000" r="1"/><S d="100"/></SegmentTimeline>
        </SegmentTemplate>
        <Representation id="a" codecs="mp4a.40.2" bandwidth="1"/>
      </AdaptationSet></Period></MPD>""".encode()
    fetched = []

    def fetch(url, rng=None):
        fetched.append(url.rsplit('/', 1)[-1])
        return xml if url.endswith('.mpd') else b'x'

    pres = Presentation('http://vod/m.mpd', fetch)
    feed = _feed(pres, 'a', threading.Event())
    while feed.read(1):
        pass
    assert fetched == ['m.mpd', 'i.mp4', '0.m4s', '2000.m4s', '4000.m4s']
