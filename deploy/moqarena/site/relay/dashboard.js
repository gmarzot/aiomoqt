// moqarena relay dashboard: relay status, namespaces and tracks, scoped to a
// namespace, from /status/status.json. Uses Dash from common.js.
'use strict';

const {SERIES, OTHER, $, el, svgEl, none, fmt} = Dash;
const PLAYER = '/g5-player/';
const PLAYER_DRAFT = '18';
const STATES = new Set(['live', 'idle', 'held', 'parked', 'ended']);
const TRACK_COLS = 13;

// Namespace scope (null: all), from and to the URL hash.
let scope = new URLSearchParams(location.hash.slice(1)).get('ns');
// Track ids whose subscriber list is open.
const expanded = new Set();
// Track id -> series color, in first-seen order; it never changes with scope.
const colors = new Map();
let latest = null;

function colorOf(id) {
  if (!colors.has(id)) colors.set(id, colors.size < SERIES.length ? SERIES[colors.size] : null);
  return colors.get(id);
}

function setScope(ns) {
  scope = ns;
  history.replaceState(null, '', ns === null ? location.pathname
    : `#${new URLSearchParams({ns})}`);
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

function renderGauges(h, T) {
  const pct = (used, total) => (none(used) || !total) ? null : 100 * used / total;
  drawGauge($('gauge-cpu'), h.cpu);
  drawGauge($('gauge-ram'), pct(h.mem_used, h.mem_total));
  drawGauge($('gauge-disk'), pct(h.disk_used, h.disk_total));
  drawGauge($('gauge-cdn'), none(T.efficiency) ? null : 100 * T.efficiency, false);
}

// -- tiles ----------------------------------------------------------------------

function setTile(key, value, sub, title) {
  const t = document.querySelector(`.tile[data-key="${key}"]`);
  t.querySelector('.value').textContent = value;
  t.querySelector('.sub').textContent = sub || '';
  if (title) t.title = title;
}

function renderTiles(s) {
  const T = s.totals;
  const transports = Object.entries(T.by_transport || {}).map(([k, v]) => `${v} ${k}`).join(' · ');
  const drafts = Object.entries(T.by_draft || {}).map(([k, v]) => `${k}: ${v}`).join(', ');
  const playable = s.namespaces.filter((r) => r.format).length;
  const groups = s.tracks.reduce((n, r) => n + (r.groups_per_s || 0), 0);
  setTile('sessions', fmt.num(T.sessions), transports, drafts ? `By draft: ${drafts}` : '');
  setTile('namespaces', fmt.num(T.namespaces), playable ? `${playable} playable` : '');
  setTile('tracks', fmt.num(T.tracks), fmt.plural(T.fetches, 'fetch', 'fetches'));
  setTile('subscribers', fmt.num(T.subscribers), fmt.plural(T.publishers, 'publisher'));
  setTile('ingress', fmt.bps(T.ingress_bps), `media ${fmt.bps(T.media_in_bps)}`);
  setTile('egress', fmt.bps(T.egress_bps), `media ${fmt.bps(T.media_out_bps)}`);
  setTile('objects', fmt.rate(T.objects_in_per_s), `${fmt.rate(groups)} groups/s in`);
  setTile('delivered', fmt.bytes(T.delivered_bytes), 'media since start');
  setTile('drops', fmt.num(T.drops), 'subscribers dropped');
  setTile('loss', fmt.pct(T.loss_pct, 2), 'delivery to subscribers');
  setTile('rtt', fmt.ms(T.rtt_p50_ms), `p99 ${fmt.ms(T.rtt_p99_ms)}`);
  const lag = T.loop_lag_ms || {};
  setTile('lag', fmt.ms(lag.p99), `p50 ${fmt.ms(lag.p50)}`);
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
  return el('a', {class: 'play', href: `${PLAYER}?${q}`, target: '_blank', rel: 'noopener'},
            '▶ Play');
}

function renderNamespaces(rows, wtUrl) {
  fill($('t-namespaces'), rows.map((r) => {
    const tr = el('tr', {class: `pick${r.namespace === scope ? ' selected' : ''}`,
                         tabindex: '0', title: 'Show this namespace\'s tracks'},
      el('td', {}, el('code', {}, r.namespace)),
      el('td', {}, r.format ? el('span', {class: 'badge'}, r.format) : '—'),
      el('td', {}, r.origin || '—'),
      num(fmt.num(r.tracks)), num(fmt.num(r.subscribers)),
      num(fmt.bps(r.in_bps)), num(fmt.bps(r.out_bps)), num(fmt.bytes(r.bytes_out)),
      el('td', {class: 'n'}, r.format ? playLink(r.namespace, wtUrl) : ''));
    tr.dataset.ns = r.namespace;
    return tr;
  }), 9, 'No namespaces.');
}

function renderScope(namespaces) {
  const chip = (label, ns) => {
    const b = el('button', {type: 'button', class: `chip${scope === ns ? ' on' : ''}`,
                            'aria-pressed': String(scope === ns)}, label);
    b.dataset.scope = ns === null ? '' : ns;
    b.dataset.all = ns === null ? '1' : '';
    return b;
  };
  $('scope').replaceChildren(el('span', {class: 'scope-label'}, 'Scope'),
    chip('All namespaces', null), ...namespaces.map((r) => chip(r.namespace, r.namespace)));
}

// -- tracks: charts and table ---------------------------------------------------

function scoped(tracks) {
  return scope === null ? tracks : tracks.filter((r) => r.namespace === scope);
}

function seriesFor(s, tracks, field) {
  const series = [];
  let other = null;
  for (const r of tracks) {
    const hist = (s.track_history || {})[r.id];
    if (!hist) continue;
    const color = colorOf(r.id);
    const name = scope === null ? `${r.name} · ${r.namespace}` : r.name;
    if (color) {
      series.push({name, label: r.name, values: hist[field], color});
    } else {
      other = other ? other.map((v, i) => (none(v) && none(hist[field][i]) ? null
        : (v || 0) + (hist[field][i] || 0))) : hist[field].slice();
    }
  }
  series.sort((a, b) => SERIES.indexOf(a.color) - SERIES.indexOf(b.color));
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

function subscriberRow(r) {
  const list = r.subscriber_list || [];
  const now = Date.now() / 1000;
  const body = list.length ? list.map((x) => el('tr', {},
    el('td', {}, x.transport), el('td', {}, `d${x.draft}`), num(fmt.ms(x.rtt_ms)),
    el('td', {}, x.forward ? 'forwarding' : 'paused'),
    num(none(x.since) ? '—' : fmt.duration(now - x.since))))
    : [el('tr', {}, el('td', {colspan: 5, class: 'empty'}, 'No subscribers.'))];
  const more = r.subscribers > list.length ? el('p', {class: 'note'},
    `First ${list.length} of ${fmt.num(r.subscribers)}.`) : '';
  return el('tr', {class: 'subs-row'}, el('td', {colspan: TRACK_COLS},
    el('table', {class: 'subs'},
      el('thead', {}, el('tr', {}, el('th', {}, 'Transport'), el('th', {}, 'Draft'),
        el('th', {class: 'n'}, 'RTT'), el('th', {}, 'Forward'),
        el('th', {class: 'n'}, 'Connected'))),
      el('tbody', {}, ...body)), more));
}

function trackRow(r) {
  const state = STATES.has(r.state) ? r.state : 'idle';
  const open = expanded.has(r.id);
  const color = colorOf(r.id) || OTHER;
  const toggle = el('button', {type: 'button', class: 'expand', 'aria-expanded': String(open),
                               title: open ? 'Hide subscribers' : 'Show subscribers'},
                    open ? '▾' : '▸');
  toggle.dataset.track = r.id;
  const tr = el('tr', {class: open ? 'open' : null},
    el('td', {title: r.largest ? `Largest ${r.largest[0]} · ${r.largest[1]}` : null},
       toggle, el('i', {class: 'key-line', style: `background:${color}`}),
       el('code', {}, r.name)),
    el('td', {}, el('span', {class: `state state-${state}`}, state)),
    num(fmt.num(r.subscribers)), num(fmt.num(r.peak)),
    num(fmt.bps(r.in_bps)), num(fmt.bps(r.out_bps)), num(fmt.bytes(r.bytes_out)),
    num(fmt.rate(r.groups_per_s)), num(fmt.rate(r.objects_per_s)),
    el('td', {class: 'n', title: `RTT up ${fmt.ms(r.rtt_up_ms)} · down p50 ${fmt.ms(r.rtt_down_ms)}`},
       fmt.ms(r.latency_ms)),
    num(fmt.num(r.fetches)), num(fmt.num(r.drops)),
    num(`${fmt.num(r.cache_groups)} g · ${fmt.bytes(r.cache_bytes)}`));
  return open ? [tr, subscriberRow(r)] : [tr];
}

function renderTracks(tracks) {
  $('tracks-scope').textContent = scope === null ? 'all namespaces' : scope;
  const rows = [];
  let ns = null;
  for (const r of tracks) {
    if (scope === null && r.namespace !== ns) {
      ns = r.namespace;
      const group = el('tr', {class: 'group pick', tabindex: '0', title: 'Scope to this namespace'},
        el('td', {colspan: TRACK_COLS}, el('code', {}, ns)));
      group.dataset.ns = ns;
      rows.push(group);
    }
    rows.push(...trackRow(r));
  }
  fill($('t-tracks'), rows, TRACK_COLS, scope === null ? 'No tracks.' : 'No tracks in this namespace.');
}

// -- page -----------------------------------------------------------------------

function render(s) {
  latest = s;
  const urls = relayUrls(s.relay);
  renderRelay(s, urls);
  renderGauges(s.host || {}, s.totals || {});
  renderTiles(s);
  renderNamespaces(s.namespaces, urls.wt);
  renderScope(s.namespaces);
  for (const r of s.tracks) colorOf(r.id);
  const tracks = scoped(s.tracks);
  renderTrackCharts(s, tracks);
  renderTracks(tracks);
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
  const chip = target.closest('.chip');
  if (chip) return setScope(chip.dataset.all ? null : chip.dataset.scope);
  const toggle = target.closest('.expand');
  if (toggle) {
    const id = toggle.dataset.track;
    if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
    return latest && render(latest);
  }
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
