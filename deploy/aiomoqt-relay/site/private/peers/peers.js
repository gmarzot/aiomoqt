// aiomoqt-relay peers: IPs, what they publish and subscribe, from /stats/peers.json.
// Names come from peers: text only, never HTML.
'use strict';

const {$, el, none, fmt} = Dash;
let report = null;

const when = (t) => none(t) ? '—' : new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ');

const SHOWN = 3;

// The first SHOWN namespaces, the rest of the sample behind a toggle.
function nsList(side) {
  if (!side || !side.n) return '—';
  const line = (s) => el('div', {title: `${s.tracks} track(s) · last ${when(s.last)}`},
    el('code', {}, s.namespace));
  const out = side.sample.slice(0, SHOWN).map(line);
  const rest = side.sample.slice(SHOWN).map(line);
  const unlisted = side.n - side.sample.length;
  if (unlisted > 0) rest.push(el('div', {}, `${unlisted} more not listed`));
  if (rest.length) out.push(el('details', {}, el('summary', {}, `+${side.n - SHOWN} more`), ...rest));
  return out;
}

function fill(id, rows, cols, emptyText) {
  const body = $(id).tBodies[0];
  body.replaceChildren(...(rows.length ? rows
    : [el('tr', {}, el('td', {colspan: cols, class: 'empty'}, emptyText))]));
}

function cell(content, attrs) {
  const td = el('td', attrs || {});
  td.append(...(Array.isArray(content) ? content : [content]));
  return td;
}

function render() {
  if (!report) return;
  const q = $('q').value.trim().toLowerCase();
  const hit = (...xs) => !q || xs.some((x) => String(x).toLowerCase().includes(q));
  const today = report.days[0] || {};
  for (const [key, v] of [['ips', today.ips], ['pub_ips', today.pub_ips], ['sub_ips', today.sub_ips],
                          ['sessions', today.sessions], ['namespaces', today.namespaces],
                          ['all', report.ip_count]]) {
    document.querySelector(`.tile[data-key="${key}"] .value`).textContent = fmt.num(v);
  }
  document.querySelector('.tile[data-key="all"] .sub').textContent = `${report.retain_days} days of detail`;
  $('generated').textContent = `report ${when(report.generated)} UTC · sampled every ${report.interval_s} s`;

  const nsOf = (side) => side.sample.map((s) => s.namespace);
  fill('t-ips', report.ips.filter((p) => hit(p.ip, ...nsOf(p.pub), ...nsOf(p.sub))).map((p) => el('tr', {},
    cell(el('code', {}, p.ip)), cell(p.roles.join(' + ') || '—'), cell(fmt.num(p.sessions), {class: 'n'}),
    cell(when(p.first)), cell(when(p.last)),
    cell([p.transports.join(', '), p.drafts.length ? ` · d${p.drafts.join('/')}` : ''].join('') || '—'),
    cell(nsList(p.pub)), cell(nsList(p.sub)))), 8, q ? 'No match.' : 'No peers.');

  fill('t-namespaces', report.namespaces.filter((n) => hit(n.namespace, ...n.publishers, ...n.sub_sample))
    .map((n) => el('tr', {},
      cell(el('code', {}, n.namespace)),
      cell(n.publishers.length ? n.publishers.map((ip) => el('div', {}, el('code', {}, ip))) : '—'),
      cell(fmt.num(n.tracks), {class: 'n'}),
      cell(fmt.num(n.subscribers), {class: 'n', title: n.sub_sample.join('\n')}),
      cell(when(n.first)), cell(when(n.last)))), 6, q ? 'No match.' : 'No namespaces.');

  fill('t-days', report.days.slice(0, 90).map((d) => el('tr', {},
    cell(d.day), cell(fmt.num(d.ips), {class: 'n'}), cell(fmt.num(d.pub_ips), {class: 'n'}),
    cell(fmt.num(d.sub_ips), {class: 'n'}), cell(fmt.num(d.sessions), {class: 'n'}),
    cell(fmt.num(d.namespaces), {class: 'n'}))), 6, 'No days.');
}

async function load() {
  try {
    const r = await fetch('/stats/peers.json', {cache: 'no-store'});
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    report = await r.json();
    $('unavailable').hidden = true;
    render();
  } catch (e) {
    console.debug('peers report failed', e);
    $('unavailable').hidden = report !== null;
  } finally {
    setTimeout(load, 60000);
  }
}

$('q').addEventListener('input', render);
load();
