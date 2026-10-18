// MoQ Ersatz Relay dashboard: relay status, namespaces and tracks, scoped to a
// namespace, from /status/status.json. Uses Dash from common.js.
'use strict';

const {OTHER, $, el, svgEl, none, fmt} = Dash;
const PLAYER = '/g5-player/';
const PLAYER_DRAFT = '18';
const STATES = new Set(['live', 'idle', 'held', 'parked', 'ended']);
const TRACK_COLS = 8;
// Media track colors by name; other tracks take the remaining palette hues in order.
const NAMED = [[/^catalog$/i, '#d9532b'], [/audio/i, '#6fbff5'], [/video/i, '#a57be6']];
const REST = ['#199e70', '#c98500', '#d55181', '#008300', '#e66767'];
const ORDER = [...NAMED.map(([, c]) => c), ...REST];

// The selected namespace: from the URL hash, else the first one listed.
let scope = new URLSearchParams(location.hash.slice(1)).get('ns');
// Track id -> color; fixed once seen, so a track keeps it in every view.
const colors = new Map();
let latest = null;

function colorOf(id) {
  if (!colors.has(id)) {
    const name = id.split('\x1f').pop();
    const named = NAMED.find(([re]) => re.test(name));
    const used = [...colors.values()].filter((c) => REST.includes(c)).length;
    colors.set(id, named ? named[1] : (used < REST.length ? REST[used] : null));
  }
  return colors.get(id);
}

function setScope(ns) {
  scope = ns;
  history.replaceState(null, '', `#${new URLSearchParams({ns})}`);
  if (latest) render(latest);
}

// -- header -------------------------------------------------------------------

function relayUrls(relay) {
  const host = location.hostname;
  return {wt: `https://${host}:${relay.port}/`,
          quic: `moqt://${host}:${relay.quic_port || relay.port}`};
}

function renderRelay(s, urls) {
  const r = s.relay, v = r.versions || {}, h = s.host || {};
  $('hostname').textContent = location.hostname;
  $('url-wt').textContent = urls.wt;
  $('url-quic').textContent = urls.quic;
  $('drafts').textContent = (r.drafts || []).join(', ');
  const badge = $('badge-version');
  badge.textContent = v.aiomoqt ? `aiomoqt ${v.aiomoqt}` : 'aiomoqt ersatz relay';
  badge.title = [['aiopquic', v.aiopquic], ['picoquic', v.picoquic], ['Python', v.python]]
    .filter(([, x]) => x).map(([k, x]) => `${k} ${x}`).join(' · ');
  $('uptime').textContent = fmt.uptime(r.uptime_s)
    + (r.watchdog_s ? ` · watchdog ${r.watchdog_s}s` : '');
  $('date').textContent = fmt.date(s.generated);
  $('hw').textContent = [h.arch, h.cores && `${h.cores} vCPU`,
                         h.mem_total && `${(h.mem_total / 2 ** 30).toFixed(0)} GiB RAM`]
    .filter(Boolean).join(' · ') || '—';
  $('cpu-model').textContent = h.cpu_model || '—';
  $('os').textContent = h.os || '—';
}

// 270° sweep from lower left (225°) clockwise to lower right (-45°).
const G = {cx: 60, cy: 58, r: 42, tick: 52, from: 225, sweep: 270};

function polar(r, deg) {
  const a = deg * Math.PI / 180;
  return `${(G.cx + r * Math.cos(a)).toFixed(2)},${(G.cy - r * Math.sin(a)).toFixed(2)}`;
}

function arc(r, f0, f1) {
  const a0 = G.from - G.sweep * f0, a1 = G.from - G.sweep * f1;
  const large = G.sweep * (f1 - f0) > 180 ? 1 : 0;
  return `M${polar(r, a0)}A${r},${r} 0 ${large} 1 ${polar(r, a1)}`;
}

const level = (pct) => pct >= 90 ? 'high' : pct >= 70 ? 'warn-level' : 'ok';

// `bands`: amber and red past 70% and 90%; without, high is good and stays green.
function drawGauge(svg, pct, bands = true) {
  const tone = (v) => bands ? level(v) : 'ok';
  svg.replaceChildren(svgEl('path', {class: 'g-track', d: arc(G.r, 0, 1)}));
  if (bands) {
    svg.append(svgEl('path', {class: 'g-tick warn-level', d: arc(G.tick, 0.7, 0.9)}),
               svgEl('path', {class: 'g-tick high', d: arc(G.tick, 0.9, 1)}));
  }
  const text = svgEl('text', {class: `g-value ${none(pct) ? '' : tone(pct)}`, x: G.cx,
                              y: G.cy + 8, 'text-anchor': 'middle', stroke: 'none'});
  text.textContent = none(pct) ? '—' : `${Math.round(pct)}%`;
  if (!none(pct)) {
    const f = Math.min(Math.max(pct, 0), 100) / 100;
    svg.append(svgEl('path', {class: `g-arc ${tone(pct)}`, d: arc(G.r, 0, Math.max(f, 0.004))}));
  }
  svg.append(text);
}

function renderGauges(h) {
  const pct = (used, total) => (none(used) || !total) ? null : 100 * used / total;
  drawGauge($('gauge-cpu'), h.cpu);
  drawGauge($('gauge-ram'), pct(h.mem_used, h.mem_total));
  drawGauge($('gauge-disk'), pct(h.disk_used, h.disk_total));
}

// -- tiles ----------------------------------------------------------------------

function setTile(key, value, sub, title) {
  const t = document.querySelector(`.tile[data-key="${key}"]`);
  t.querySelector('.value').textContent = value;
  t.querySelector('.sub').textContent = sub || '';
  if (title) t.title = title;
}

// Transport and draft counts of `list`: "2 wt · 1 quic", "d18: 2, d22: 1".
function sessionMix(list) {
  const count = (key) => {
    const m = new Map();
    for (const x of list) m.set(key(x), (m.get(key(x)) || 0) + 1);
    return [...m];
  };
  return {
    transports: count((x) => x.transport).map(([k, v]) => `${v} ${k}`).join(' · '),
    drafts: count((x) => `d${x.draft}`).map(([k, v]) => `${k}: ${v}`).join(', '),
  };
}

function renderTiles(s) {
  const T = s.totals;
  const sessions = s.sessions || [];
  const pubList = sessions.filter((x) => (x.role || '').includes('pub'));
  const subList = sessions.filter((x) => (x.role || '').includes('sub'));
  const pubs = sessionMix(pubList), subs = sessionMix(subList);
  const playable = s.namespaces.filter((r) => r.format).length;
  const live = s.tracks.filter((r) => r.state === 'live').length;
  const groups = s.tracks.reduce((n, r) => n + (r.groups_per_s || 0), 0);
  setTile('publishers', fmt.num(T.publishers), pubs.transports || 'publishing sessions',
          pubList.length ? `By draft: ${pubs.drafts}` : 'No publishing sessions');
  // A viewer counts once; without totals.subscriptions, count sub-role sessions.
  const subscriptions = T.subscriptions ?? T.subscribers;
  const viewers = none(T.subscriptions) ? subList.length : T.subscribers;
  setTile('subscribers', fmt.num(viewers),
          `${fmt.plural(subscriptions, 'subscription')} · peak ${fmt.num(T.subscribers_peak)}`,
          subList.length ? `${subs.transports}; by draft: ${subs.drafts}` : 'No subscribing sessions');
  setTile('namespaces', fmt.num(T.namespaces), playable ? `${playable} playable` : '');
  setTile('tracks', fmt.num(T.tracks), `${fmt.num(live)} live`);
  setTile('rtt', fmt.ms(T.rtt_p50_ms), `p99 ${fmt.ms(T.rtt_p99_ms)}`);
  const lag = T.loop_lag_ms || {};
  setTile('lag', fmt.ms(lag.p99), `p50 ${fmt.ms(lag.p50)}`);
  setTile('loss', fmt.pct(T.loss_pct, 2), 'delivery sessions');
  setTile('drops', fmt.num(T.drops), 'subscribers dropped');
  setTile('groups', fmt.rate(none(T.groups_per_s) ? groups : T.groups_per_s), 'in, all tracks');
  setTile('objects', fmt.rate(T.objects_in_per_s), 'in, all tracks');
  setTile('ingress', fmt.bps(T.ingress_bps), `media ${fmt.bps(T.media_in_bps)}`);
  setTile('egress', fmt.bps(T.egress_bps), `media ${fmt.bps(T.media_out_bps)}`);
  for (const tile of document.querySelectorAll('.tile[data-spark]')) {
    Dash.spark(tile, s.history[tile.dataset.spark] || []);
  }
}

// -- namespaces and scope -------------------------------------------------------

function fill(table, rows, cols, empty) {
  table.tBodies[0].replaceChildren(...(rows.length ? rows
    : [el('tr', {}, el('td', {colspan: cols, class: 'empty'}, empty))]));
}

const num = (v) => el('td', {class: 'n'}, v);

function playLink(ns, url) {
  const q = new URLSearchParams({url, ns, v: PLAYER_DRAFT});
  return el('a', {class: 'play', href: `${PLAYER}?${q}`, target: '_blank', rel: 'noopener',
                  title: 'Play in G5 Player'}, '▶');
}

// "[h3/wt draft-18]" from a transport and draft.
function via(transport, draft) {
  if (!transport) return '';
  return `[${transport === 'wt' ? 'h3/wt' : transport}${none(draft) ? '' : ` draft-${draft}`}]`;
}

// An origin such as "wt d18" as its transport and draft.
function viaOrigin(origin) {
  const m = /^(\S+) d(\d+)$/.exec(origin || '');
  return m ? via(m[1], m[2]) : (origin ? `[${origin}]` : '');
}

// "addr [h3/wt draft-18]"; the address only when the relay reports it.
const peer = (addr, label) => [addr, label].filter(Boolean).join(' ') || '—';

function originCell(r) {
  const label = viaOrigin(r.origin);
  const td = el('td', {}, r.origin_addr || '');
  if (label) td.append(r.origin_addr ? ' ' : '', el('span', {class: 'via'}, label));
  if (!r.origin_addr && !label) td.append('—');
  return td;
}

function renderNamespaces(rows, wtUrl) {
  fill($('t-namespaces'), rows.map((r) => {
    const tr = el('tr', {class: `pick${r.namespace === scope ? ' selected' : ''}`, tabindex: '0',
                         title: `${fmt.bytes(r.bytes_out)} delivered`},
      el('td', {}, el('code', {}, r.namespace)),
      originCell(r),
      num(fmt.num(r.tracks)), num(fmt.num(r.subscribers)), num(fmt.num(r.peak)),
      inOut(r.in_bps, r.out_bps),
      el('td', {class: 'n'}, r.format ? playLink(r.namespace, wtUrl) : ''));
    tr.dataset.ns = r.namespace;
    return tr;
  }), 7, 'No namespaces.');
}

// -- tracks: charts and table ---------------------------------------------------

function scoped(tracks) {
  return tracks.filter((r) => r.namespace === scope);
}

function seriesFor(s, tracks, field) {
  const series = [];
  let other = null;
  for (const r of tracks) {
    const hist = (s.track_history || {})[r.id];
    if (!hist) continue;
    const color = colorOf(r.id);
    if (color) {
      series.push({name: r.name, values: hist[field], color});
    } else {
      other = other ? other.map((v, i) => (none(v) && none(hist[field][i]) ? null
        : (v || 0) + (hist[field][i] || 0))) : hist[field].slice();
    }
  }
  series.sort((a, b) => ORDER.indexOf(a.color) - ORDER.indexOf(b.color));
  if (other) series.push({name: 'Other', values: other, color: OTHER});
  return series;
}

function renderTrackCharts(s, tracks) {
  const t = s.history.t || [];
  const subs = seriesFor(s, tracks, 'subscribers');
  const egress = seriesFor(s, tracks, 'out_bps');
  Dash.legend($('legend-subs'), subs);
  Dash.legend($('legend-egress'), egress);
  Dash.chart($('chart-subs'), t, subs,
             {labels: true, fmt: (v) => none(v) ? '—' : fmt.num(Math.round(v))});
  Dash.chart($('chart-egress'), t, egress, {labels: true, fmt: fmt.bps});
}

const inOut = (i, o) => el('td', {class: 'n', title: `in ${fmt.bps(i)} · out ${fmt.bps(o)}`},
  `${fmt.bpsShort(i)} / ${fmt.bpsShort(o)}`);

// Everything the row leaves out, for its hover text.
function trackDetail(r) {
  const now = Date.now() / 1000;
  const lines = [
    `${r.namespace} / ${r.name}`,
    `peak ${fmt.num(r.peak)} subscribers · ${fmt.bytes(r.bytes_out)} delivered · `
      + `${fmt.plural(r.fetches, 'fetch', 'fetches')} · ${fmt.plural(r.drops, 'drop')}`,
    `cache ${fmt.num(r.cache_groups)} groups, ${fmt.bytes(r.cache_bytes)}`
      + (r.largest ? ` · largest ${r.largest[0]} · ${r.largest[1]}` : ''),
    `from ${peer(r.origin_addr, viaOrigin(r.origin))} · RTT up ${fmt.ms(r.rtt_up_ms)}, down p50 ${fmt.ms(r.rtt_down_ms)}`,
  ];
  const subs = r.subscriber_list || [];
  if (subs.length) {
    lines.push('subscribers:');
    for (const x of subs.slice(0, 12)) {
      lines.push(`  ${peer(x.addr, via(x.transport, x.draft))} · ${fmt.ms(x.rtt_ms)} · ${x.forward ? 'forwarding' : 'paused'}`
        + (none(x.since) ? '' : ` · ${fmt.duration(now - x.since)}`));
    }
    if (r.subscribers > 12) lines.push(`  … ${fmt.num(r.subscribers - 12)} more`);
  }
  return lines.join('\n');
}

// `format`: the namespace's MSF format, shown on its catalog track.
function trackRow(r, format) {
  const state = STATES.has(r.state) ? r.state : 'idle';
  const color = colorOf(r.id) || OTHER;
  const name = [el('code', {}, r.name)];
  if (format && r.name === 'catalog') name.push(el('span', {class: 'format'}, ` (${format})`));
  return el('tr', {title: trackDetail(r)},
    el('td', {}, el('i', {class: 'key-line', style: `background:${color}`}), ...name),
    num(fmt.num(r.subscribers)), num(fmt.num(r.peak)),
    num(fmt.rate(r.groups_per_s)), num(fmt.rate(r.objects_per_s)),
    inOut(r.in_bps, r.out_bps),
    num(fmt.ms(r.latency_ms)),
    el('td', {}, el('span', {class: `state state-${state}`}, state)));
}

function renderTracks(tracks, format) {
  $('tracks-scope').textContent = scope || '';
  fill($('t-tracks'), tracks.map((r) => trackRow(r, format)), TRACK_COLS,
       scope === null ? 'No namespaces.' : 'No tracks in this namespace.');
}

// -- page -----------------------------------------------------------------------

function render(s) {
  latest = s;
  const urls = relayUrls(s.relay);
  renderRelay(s, urls);
  renderGauges(s.host || {});
  renderTiles(s);
  if (!s.namespaces.some((r) => r.namespace === scope)) {
    scope = s.namespaces.length ? s.namespaces[0].namespace : null;
  }
  renderNamespaces(s.namespaces, urls.wt);
  for (const r of s.tracks) colorOf(r.id);
  const tracks = scoped(s.tracks);
  renderTrackCharts(s, tracks);
  renderTracks(tracks, (s.namespaces.find((r) => r.namespace === scope) || {}).format);
}

function toast(target, text) {
  const t = $('toast');
  const box = target.getBoundingClientRect();
  t.textContent = text;
  t.style.left = `${Math.round(box.left)}px`;
  t.style.top = `${Math.round(box.bottom + 6)}px`;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 1200);
}

function copyText(target) {
  const select = () => {
    const range = document.createRange();
    range.selectNodeContents(target);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    toast(target, 'Press ⌘C / Ctrl+C');
  };
  if (navigator.clipboard) {
    navigator.clipboard.writeText(target.textContent).then(() => toast(target, 'Copied'), select);
  } else {
    select();
  }
}

function activate(target) {
  const copy = target.closest('.copyable');
  if (copy) return copyText(copy);
  const pick = target.closest('tr.pick');
  if (pick && !target.closest('a')) setScope(pick.dataset.ns);
}

document.addEventListener('click', (ev) => activate(ev.target));
document.addEventListener('keydown', (ev) => {
  if ((ev.key === 'Enter' || ev.key === ' ') && ev.target.closest
      && ev.target.closest('.copyable, tr.pick')) {
    ev.preventDefault();
    activate(ev.target);
  }
});

let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => latest && render(latest), 150);
});

Dash.poll('/status/status.json', render, Dash.showFreshness);
