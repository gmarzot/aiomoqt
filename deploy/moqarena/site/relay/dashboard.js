// moqarena relay dashboard: polls the relay's status.json and renders it.
// Namespace and track names come from publishers: text only, never HTML.
'use strict';

const STATUS_URL = '/status/status.json';
const POLL_MS = 2000;
// No new snapshot for this long marks the relay stale.
const STALE_S = 30;
const WINDOW_S = 600;
const PLAYER = '/g5-player/';
const PLAYER_DRAFT = '18';
const SVG = 'http://www.w3.org/2000/svg';
const STATES = new Set(['live', 'idle', 'held', 'parked', 'ended']);

const $ = (id) => document.getElementById(id);

function el(tag, attrs, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v !== undefined && v !== null) e.setAttribute(k, v);
  }
  for (const c of children) e.append(c instanceof Node ? c : String(c ?? ''));
  return e;
}

function svgEl(tag, attrs) {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  return e;
}

// -- formatting -----------------------------------------------------------

const none = (v) => v === null || v === undefined || Number.isNaN(v);

function scaled(v, step, units) {
  let i = 0;
  while (Math.abs(v) >= step && i < units.length - 1) { v /= step; i++; }
  return `${i === 0 || v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

const fmtBps = (v) => none(v) ? '—' : scaled(v, 1000, ['bps', 'kbps', 'Mbps', 'Gbps']);
const fmtBytes = (v) => none(v) ? '—' : scaled(v, 1000, ['B', 'kB', 'MB', 'GB', 'TB']);
const fmtNum = (v) => none(v) ? '—' : Number(v).toLocaleString('en-US');
const fmtPct = (v, digits = 1) => none(v) ? '—' : `${Number(v).toFixed(digits)}%`;
const fmtMs = (v) => none(v) ? '—' : `${v >= 100 ? Math.round(v) : Number(v).toFixed(1)} ms`;
const fmtRate = (v) => none(v) ? '—' : (v >= 100 ? fmtNum(Math.round(v)) : Number(v).toFixed(1));
const plural = (n, one, many = `${one}s`) => `${fmtNum(n)} ${n === 1 ? one : many}`;

function fmtDuration(s) {
  if (none(s)) return '—';
  s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600);
  const m = Math.floor(s % 3600 / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

// -- tiles and sparklines ---------------------------------------------------

function setTile(key, value, sub, title) {
  const t = document.querySelector(`.tile[data-key="${key}"]`);
  t.querySelector('.value').textContent = value;
  t.querySelector('.sub').textContent = sub || '';
  if (title) t.title = title;
}

function pathFor(values, x, y) {
  // One run per stretch of known values: a gap breaks the line.
  let d = '', pen = false;
  values.forEach((v, i) => {
    if (none(v)) { pen = false; return; }
    d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
    pen = true;
  });
  return d;
}

function drawSpark(tile, values) {
  let svg = tile.querySelector('svg.spark');
  if (!svg) {
    svg = svgEl('svg', {class: 'spark', viewBox: '0 0 100 24', preserveAspectRatio: 'none',
                        'aria-hidden': 'true'});
    tile.append(svg);
  }
  const known = values.filter((v) => !none(v));
  const max = Math.max(...known, 0) || 1;
  const n = Math.max(values.length - 1, 1);
  svg.replaceChildren(svgEl('path', {
    d: pathFor(values, (i) => 100 * i / n, (v) => 22 - 20 * v / max)}));
}

function renderTiles(s) {
  const T = s.totals;
  const transports = Object.entries(T.by_transport || {})
    .map(([k, v]) => `${v} ${k}`).join(' · ');
  const drafts = Object.entries(T.by_draft || {})
    .map(([k, v]) => `${k}: ${v}`).join(', ');
  const playable = s.namespaces.filter((r) => r.format).length;
  setTile('sessions', fmtNum(T.sessions), transports, drafts ? `By draft: ${drafts}` : '');
  setTile('namespaces', fmtNum(T.namespaces), playable ? `${playable} playable` : '');
  setTile('tracks', fmtNum(T.tracks), plural(T.fetches, 'fetch', 'fetches'));
  setTile('subscribers', fmtNum(T.subscribers), plural(T.publishers, 'publisher'));
  setTile('ingress', fmtBps(T.ingress_bps), `media ${fmtBps(T.media_in_bps)}`);
  setTile('egress', fmtBps(T.egress_bps), `media ${fmtBps(T.media_out_bps)}`);
  const groups = s.tracks.reduce((n, r) => n + (r.groups_per_s || 0), 0);
  setTile('objects', fmtRate(T.objects_in_per_s), `${fmtRate(groups)} groups/s in`);
  setTile('delivered', fmtBytes(T.delivered_bytes), 'media since start');
  setTile('drops', fmtNum(T.drops), 'subscribers dropped');
  setTile('loss', fmtPct(T.loss_pct, 2), 'delivery to subscribers');
  setTile('rtt', fmtMs(T.rtt_p50_ms), `p99 ${fmtMs(T.rtt_p99_ms)}`);
  const lag = T.loop_lag_ms || {};
  setTile('lag', fmtMs(lag.p99), `p50 ${fmtMs(lag.p50)}`);
  for (const tile of document.querySelectorAll('.tile[data-spark]')) {
    drawSpark(tile, s.history[tile.dataset.spark] || []);
  }
}

// -- charts -------------------------------------------------------------------

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  return [1, 2, 5, 10].map((m) => m * p).find((m) => v <= m);
}

function drawChart(svg, t, series, fmt, fixedMax) {
  const w = Math.max(240, svg.clientWidth || 600);
  const h = Math.max(120, svg.clientHeight || 180);
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.replaceChildren();
  const pad = {l: 72, r: 10, t: 10, b: 22};
  const iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;
  const known = series.flatMap((s) => s.values.filter((v) => !none(v)));
  const max = fixedMax || niceMax(Math.max(...known, 0));
  const now = t.length ? t[t.length - 1] : 0;
  const x = (i) => pad.l + iw * (1 - (now - t[i]) / WINDOW_S);
  const y = (v) => pad.t + ih * (1 - Math.min(v, max) / max);
  for (const f of [0, 0.5, 1]) {
    const gy = y(max * f);
    svg.append(svgEl('line', {class: 'grid', x1: pad.l, x2: w - pad.r, y1: gy, y2: gy}));
    const label = svgEl('text', {class: 'axis', x: pad.l - 8, y: gy + 4, 'text-anchor': 'end'});
    label.textContent = fmt(max * f);
    svg.append(label);
  }
  [['−10m', pad.l, 'start'], ['−5m', pad.l + iw / 2, 'middle'], ['now', w - pad.r, 'end']]
    .forEach(([text, lx, anchor]) => {
      const label = svgEl('text', {class: 'axis', x: lx, y: h - 6, 'text-anchor': anchor});
      label.textContent = text;
      svg.append(label);
    });
  for (const s of series) {
    svg.append(svgEl('path', {class: `line ${s.cls}`, d: pathFor(s.values, x, y)}));
  }
}

function renderCharts(s) {
  const H = s.history;
  drawChart($('chart-bw'), H.t, [
    {values: H.ingress_bps, cls: 'k-in'}, {values: H.egress_bps, cls: 'k-out'}], fmtBps);
  drawChart($('chart-cpu'), H.t, [
    {values: H.cpu, cls: 'k-host'}, {values: H.proc_cpu, cls: 'k-proc'}],
  (v) => `${Math.round(v)}%`, 100);
}

// -- tables -------------------------------------------------------------------

function fill(table, rows, cols, empty) {
  const body = table.tBodies[0];
  if (!rows.length) {
    body.replaceChildren(el('tr', {}, el('td', {colspan: cols, class: 'empty'}, empty)));
    return;
  }
  body.replaceChildren(...rows);
}

const num = (v) => el('td', {class: 'n'}, v);

function playLink(ns, url) {
  const q = new URLSearchParams({url, ns, v: PLAYER_DRAFT});
  return el('a', {class: 'play', href: `${PLAYER}?${q}`, target: '_blank', rel: 'noopener'},
            '▶ Play');
}

function renderNamespaces(rows, wtUrl) {
  fill($('t-namespaces'), rows.map((r) => el('tr', {},
    el('td', {}, el('code', {}, r.namespace)),
    el('td', {}, r.format ? el('span', {class: 'badge'}, r.format) : '—'),
    el('td', {}, r.origin || '—'),
    num(fmtNum(r.tracks)), num(fmtNum(r.subscribers)),
    num(fmtBps(r.in_bps)), num(fmtBps(r.out_bps)), num(fmtBytes(r.bytes_out)),
    el('td', {class: 'n'}, r.format ? playLink(r.namespace, wtUrl) : ''))),
  9, 'No namespaces.');
}

function renderTracks(rows) {
  fill($('t-tracks'), rows.map((r) => {
    const state = STATES.has(r.state) ? r.state : 'idle';
    return el('tr', {},
      el('td', {}, el('code', {}, r.namespace)),
      el('td', {title: r.largest ? `Largest ${r.largest[0]} · ${r.largest[1]}` : null},
         el('code', {}, r.name)),
      el('td', {}, el('span', {class: `state state-${state}`}, state)),
      num(fmtNum(r.subscribers)), num(fmtNum(r.peak)),
      num(fmtBps(r.in_bps)), num(fmtBps(r.out_bps)), num(fmtBytes(r.bytes_out)),
      num(fmtRate(r.groups_per_s)), num(fmtRate(r.objects_per_s)),
      el('td', {class: 'n', title: `RTT up ${fmtMs(r.rtt_up_ms)} · down p50 ${fmtMs(r.rtt_down_ms)}`},
         fmtMs(r.latency_ms)),
      num(fmtNum(r.fetches)), num(fmtNum(r.drops)),
      num(`${fmtNum(r.cache_groups)} g · ${fmtBytes(r.cache_bytes)}`));
  }), 14, 'No tracks.');
}

// -- relay and host -------------------------------------------------------------

function relayUrls(relay) {
  const host = location.hostname;
  return {
    wt: `https://${host}:${relay.port}/`,
    quic: `moqt://${host}:${relay.quic_port || relay.port}`,
  };
}

function fmtUptime(s) {
  if (none(s)) return '—';
  s = Math.max(0, Math.round(s));
  return `${Math.floor(s / 86400)}d ${Math.floor(s % 86400 / 3600)}h ${Math.floor(s % 3600 / 60)}m`;
}

function fmtDate(epoch) {
  const d = new Date(epoch * 1000);
  const p = (n) => String(Math.trunc(Math.abs(n))).padStart(2, '0');
  const off = -d.getTimezoneOffset();
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())} `
    + `${off < 0 ? '-' : '+'}${p(off / 60)}:${p(off % 60)}`;
}

function renderRelay(s, urls) {
  const r = s.relay, v = r.versions || {};
  $('hostname').textContent = location.hostname;
  $('url-wt').textContent = urls.wt;
  $('url-quic').textContent = urls.quic;
  $('drafts').textContent = (r.drafts || []).join(', ');
  const badge = $('badge-version');
  badge.textContent = v.aiomoqt ? `aiomoqt ${v.aiomoqt}` : 'aiomoqt ersatz relay';
  badge.title = [['aiopquic', v.aiopquic], ['picoquic', v.picoquic], ['Python', v.python]]
    .filter(([, x]) => x).map(([k, x]) => `${k} ${x}`).join(' · ');
  $('uptime').textContent = fmtUptime(r.uptime_s)
    + (r.watchdog_s ? ` · watchdog ${r.watchdog_s}s` : '');
  $('date').textContent = fmtDate(s.generated);
  const h = s.host || {};
  $('hw').textContent = [h.arch, h.cores && `${h.cores} vCPU`,
                         h.mem_total && `${(h.mem_total / 2 ** 30).toFixed(0)} GiB RAM`]
    .filter(Boolean).join(' · ') || '—';
  $('cpu-model').textContent = h.cpu_model || '—';
  $('os').textContent = h.os || '—';
}

// -- dials ----------------------------------------------------------------------

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

function setHost(key, text) {
  document.querySelector(`[data-host="${key}"]`).textContent = text;
}

function renderHost(h) {
  setHost('cpu', `${h.cpu_model || '—'} · ${plural(h.cores, 'core')} · ${fmtPct(h.cpu)} busy`);
  setHost('system', [h.os, h.kernel && `Linux ${h.kernel}`, h.arch,
                     !none(h.uptime_s) && `up ${fmtDuration(h.uptime_s)}`]
    .filter(Boolean).join(' · '));
  setHost('load', (h.load || []).map((x) => x.toFixed(2)).join(' / ') || '—');
  setHost('memory', `${fmtBytes(h.mem_used)} of ${fmtBytes(h.mem_total)}`);
  setHost('disk', none(h.disk_total) ? '—'
    : `${fmtBytes(h.disk_used)} of ${fmtBytes(h.disk_total)}`);
  setHost('network', `rx ${fmtBps(h.rx_bps)} · tx ${fmtBps(h.tx_bps)}`);
  const rate = (v) => none(v) ? '—' : v.toFixed(1);
  setHost('udp', `${rate(h.udp_in_errors)} in · ${rate(h.udp_rcvbuf_errors)} rcvbuf per s`);
  setHost('process', `${fmtPct(h.proc_cpu)} CPU · ${fmtBytes(h.proc_rss)} RSS · `
    + plural(h.proc_threads, 'thread'));
}

// -- polling ------------------------------------------------------------------

let latest = null;
let generated = null;
let changedAt = 0;

function render(s) {
  const urls = relayUrls(s.relay);
  renderRelay(s, urls);
  renderTiles(s);
  renderCharts(s);
  renderNamespaces(s.namespaces, urls.wt);
  renderTracks(s.tracks);
  renderHost(s.host);
  renderGauges(s.host || {}, s.totals || {});
}

function showFreshness() {
  const age = (performance.now() - changedAt) / 1000;
  const stale = !latest || age > STALE_S;
  $('live').classList.toggle('ok', !stale);
  $('live').classList.toggle('stale', stale);
  $('unavailable').hidden = !stale;
  document.body.classList.toggle('stale', stale && !!latest);
  $('updated').textContent = !latest ? 'no status'
    : stale ? `stale · last update ${fmtDuration(age)} ago` : 'live';
}

async function poll() {
  try {
    const r = await fetch(STATUS_URL, {cache: 'no-store'});
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const s = await r.json();
    if (s.generated !== generated) {
      generated = s.generated;
      changedAt = performance.now();
    }
    latest = s;
    render(s);
  } catch (e) {
    console.debug('status poll failed', e);
  } finally {
    showFreshness();
    setTimeout(poll, POLL_MS);
  }
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

document.addEventListener('click', (ev) => {
  const target = ev.target.closest('.copyable');
  if (target) copyText(target);
});
document.addEventListener('keydown', (ev) => {
  const target = ev.target.closest && ev.target.closest('.copyable');
  if (target && (ev.key === 'Enter' || ev.key === ' ')) {
    ev.preventDefault();
    copyText(target);
  }
});

let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => latest && renderCharts(latest), 150);
});

poll();
