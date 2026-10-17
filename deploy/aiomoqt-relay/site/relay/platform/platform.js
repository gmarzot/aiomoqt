// MoQ Ersatz Relay platform page: host and relay-process detail from
// /status/platform.json. Uses Dash from common.js.
'use strict';

const {SERIES, $, el, none, fmt} = Dash;
let latest = null;

const num = (v) => el('td', {class: 'n'}, v);
const pctAxis = (v) => none(v) ? '—' : `${v >= 10 || v === 0 ? Math.round(v) : v.toFixed(1)}%`;
const perSecond = (v) => none(v) ? '—' : `${fmt.rate(v)}/s`;

function fill(table, rows, cols) {
  table.tBodies[0].replaceChildren(...(rows.length ? rows
    : [el('tr', {}, el('td', {colspan: cols, class: 'empty'}, 'None.'))]));
}

// Series in fixed slot order; names stay with their slot on every render.
function series(H, specs) {
  return specs.map(([key, name], i) => ({name, values: H[key] || [], color: SERIES[i]}));
}

function plot(id, H, specs, opts) {
  const s = series(H, specs);
  Dash.legend($(`legend-${id}`), s, opts.stacked ? 'area' : 'line');
  Dash.chart($(`chart-${id}`), H.t || [], s, {labels: !opts.stacked, ...opts});
}

const level = (pct) => pct >= 90 ? 'high' : pct >= 70 ? 'warn-level' : 'ok';

function renderCores(cores) {
  $('cores').replaceChildren(...cores.map((p, i) => el('div', {class: 'core', title: `cpu${i} ${fmt.pct(p)}`},
    el('span', {class: 'core-name'}, `cpu${i}`),
    el('span', {class: 'core-bar'}, el('i', {class: level(p), style: `width:${Math.min(100, p || 0)}%`})),
    el('span', {class: 'core-value'}, fmt.pct(p, 0)))));
}

function render(p) {
  latest = p;
  const h = p.host || {}, H = p.history || {}, proc = h.proc || {}, mem = h.mem || {};
  const v = (p.relay || {}).versions || {};
  $('hw').textContent = [h.arch, h.cores && `${h.cores} vCPU`,
                         h.mem_total && `${(h.mem_total / 2 ** 30).toFixed(0)} GiB RAM`]
    .filter(Boolean).join(' · ') || '—';
  $('cpu-model').textContent = h.cpu_model || '—';
  $('os').textContent = h.os || '—';
  $('kernel').textContent = h.kernel ? `Linux ${h.kernel}` : '—';
  $('host-uptime').textContent = fmt.uptime(h.uptime_s);
  $('load').textContent = (h.load || []).map((x) => x.toFixed(2)).join(' / ') || '—';
  $('procs').textContent = `${fmt.num(h.procs_running)} running · ${fmt.num(h.procs_blocked)} blocked`;
  $('relay').textContent = [v.aiomoqt && `aiomoqt ${v.aiomoqt}`, v.aiopquic && `aiopquic ${v.aiopquic}`,
                            `up ${fmt.uptime((p.relay || {}).uptime_s)}`].filter(Boolean).join(' · ');

  plot('cpu', H, [['cpu_user', 'user'], ['cpu_system', 'system'], ['cpu_iowait', 'iowait'],
                  ['cpu_irq', 'irq + softirq'], ['cpu_steal', 'steal'], ['cpu_nice', 'nice']],
       {stacked: true, max: 100, fmt: pctAxis});
  renderCores(h.cpu_cores || []);
  $('cpu-stats').textContent = `${fmt.pct(h.cpu)} busy · ${fmt.rate(h.ctxt_per_s)} context switches/s · `
    + `${fmt.rate(h.intr_per_s)} interrupts/s · ${fmt.rate(h.forks_per_s)} forks/s`;

  plot('proc', H, [['proc_user', 'user'], ['proc_system', 'system']],
       {stacked: true, fmt: pctAxis});
  const lag = p.loop_lag_ms || {};
  $('proc-stats').textContent = `${fmt.pct(h.proc_cpu)} CPU · ${fmt.bytes(proc.rss)} RSS · `
    + `${fmt.plural(proc.threads, 'thread')} · ${fmt.plural(proc.fds, 'open file')} · `
    + `${fmt.rate(proc.ctx_vol_per_s)} voluntary / ${fmt.rate(proc.ctx_invol_per_s)} involuntary `
    + `switches/s · loop lag p99 ${fmt.ms(lag.p99)}`;

  plot('psi', H, [['psi_cpu', 'cpu'], ['psi_io', 'io'], ['psi_memory', 'memory']],
       {fmt: pctAxis});
  const psi = h.psi || {};
  $('psi-stats').textContent = ['cpu', 'io', 'memory'].map((r) => {
    const x = psi[r] || {};
    return `${r} ${fmt.pct(x.some, 2)} some${none(x.full) ? '' : ` / ${fmt.pct(x.full, 2)} full`}`;
  }).join(' · ') + ' · share of the last 10 s that tasks waited';

  plot('mem', H, [['mem_used', 'used'], ['mem_buffers', 'buffers'], ['mem_cached', 'cache']],
       {stacked: true, max: mem.total || undefined, fmt: fmt.bytes});
  $('mem-stats').textContent = `${fmt.bytes(mem.used)} used · ${fmt.bytes(mem.available)} available `
    + `of ${fmt.bytes(mem.total)} · swap ${fmt.bytes(mem.swap_used)} of ${fmt.bytes(mem.swap_total)}`;

  plot('bw', H, [['ingress_bps', 'ingress'], ['egress_bps', 'egress']], {fmt: fmt.bps});
  const bw = p.bandwidth || {};
  $('bw-stats').textContent = `${fmt.bps(bw.ingress_bps)} in · ${fmt.bps(bw.egress_bps)} out · `
    + `delivery loss ${fmt.pct(bw.loss_pct, 2)}`;

  plot('net', H, [['rx_bps', 'receive'], ['tx_bps', 'transmit']], {fmt: fmt.bps});
  fill($('t-ifaces'), (h.ifaces || []).map((i) => el('tr', {},
    el('td', {}, el('code', {}, i.name)), num(fmt.bps(i.rx_bps)), num(fmt.bps(i.tx_bps)),
    num(fmt.rate(i.rx_pps)), num(fmt.rate(i.tx_pps)), num(fmt.rate(i.errs_per_s)),
    num(fmt.rate(i.drops_per_s)))), 7);

  plot('udp', H, [['udp_in', 'in'], ['udp_out', 'out']], {fmt: perSecond});
  const udp = h.udp || {};
  fill($('t-udp'), [['InDatagrams', 'Datagrams in'], ['OutDatagrams', 'Datagrams out'],
                    ['InErrors', 'Receive errors'], ['RcvbufErrors', 'Receive buffer full'],
                    ['SndbufErrors', 'Send buffer full'], ['NoPorts', 'No listener']]
    .filter(([k]) => k in udp)
    .map(([k, label]) => el('tr', {}, el('td', {}, label), num(fmt.rate(udp[k])))), 2);

  plot('udp-err', H, [['udp_errors', 'host UDP errors'], ['sock_drops', 'relay socket drops']],
       {fmt: perSecond});
  const sock = h.udp_socket;
  $('sock-stats').textContent = sock
    ? `relay socket :${sock.port} · receive queue ${fmt.bytes(sock.rx_queue)} · send queue `
      + `${fmt.bytes(sock.tx_queue)} · ${fmt.num(sock.drops)} drops since start`
    : 'relay socket not found';

  plot('disk', H, [['disk_read_Bps', 'read'], ['disk_write_Bps', 'write']], {fmt: fmt.Bps});
  fill($('t-disks'), (h.disks || []).map((d) => el('tr', {},
    el('td', {}, el('code', {}, d.name)), num(fmt.Bps(d.read_Bps)), num(fmt.Bps(d.write_Bps)),
    num(fmt.rate(d.read_iops)), num(fmt.rate(d.write_iops)), num(fmt.pct(d.busy_pct)))), 6);
  $('fs-stats').textContent = none(h.disk_total) ? ''
    : `root filesystem ${fmt.bytes(h.disk_used)} of ${fmt.bytes(h.disk_total)} used`;
}

let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => latest && render(latest), 150);
});

Dash.poll('/status/platform.json', render, Dash.showFreshness);
