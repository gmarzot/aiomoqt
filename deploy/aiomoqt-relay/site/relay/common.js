// Shared by the relay dashboard pages: DOM and format helpers, line and
// stacked-area charts with a crosshair tooltip, and the status poller.
// Names from status files come from publishers: text only, never HTML.
'use strict';

const Dash = (() => {
  const SVG = 'http://www.w3.org/2000/svg';
  // Categorical slots in fixed order, validated against the panel surface.
  const SERIES = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300',
                  '#9085e9', '#e66767'];
  const OTHER = '#6c6c78';
  const WINDOW_S = 600;
  // No new snapshot for this long marks the page stale.
  const STALE_S = 30;

  const $ = (id) => document.getElementById(id);

  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? '' : v);
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
    return `${i === 0 || Math.abs(v) >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
  }

  const fmt = {
    bps: (v) => none(v) ? '—' : scaled(v, 1000, ['bps', 'kbps', 'Mbps', 'Gbps']),
    Bps: (v) => none(v) ? '—' : `${scaled(v, 1000, ['B', 'kB', 'MB', 'GB'])}/s`,
    bpsShort: (v) => none(v) ? '—' : scaled(v, 1000, ['', 'k', 'M', 'G']).replace(' ', ''),
    bytes: (v) => none(v) ? '—' : scaled(v, 1000, ['B', 'kB', 'MB', 'GB', 'TB']),
    num: (v) => none(v) ? '—' : Number(v).toLocaleString('en-US'),
    pct: (v, digits = 1) => none(v) ? '—' : `${Number(v).toFixed(digits)}%`,
    ms: (v) => none(v) ? '—' : `${v >= 100 ? Math.round(v) : Number(v).toFixed(1)} ms`,
    rate: (v) => none(v) ? '—'
      : (v >= 100 ? Number(Math.round(v)).toLocaleString('en-US') : Number(v).toFixed(1)),
    plural: (n, one, many = `${one}s`) => `${none(n) ? '—' : Number(n).toLocaleString('en-US')} ${n === 1 ? one : many}`,
    duration(s) {
      if (none(s)) return '—';
      s = Math.max(0, Math.round(s));
      const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600);
      const m = Math.floor(s % 3600 / 60);
      if (d) return `${d}d ${h}h`;
      if (h) return `${h}h ${m}m`;
      if (m) return `${m}m ${s % 60}s`;
      return `${s}s`;
    },
    uptime(s) {
      if (none(s)) return '—';
      s = Math.max(0, Math.round(s));
      return `${Math.floor(s / 86400)}d ${Math.floor(s % 86400 / 3600)}h ${Math.floor(s % 3600 / 60)}m`;
    },
    date(epoch) {
      const d = new Date(epoch * 1000);
      const p = (n) => String(Math.trunc(Math.abs(n))).padStart(2, '0');
      const off = -d.getTimezoneOffset();
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
        + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())} `
        + `${off < 0 ? '-' : '+'}${p(off / 60)}:${p(off % 60)}`;
    },
  };

  // -- charts ---------------------------------------------------------------

  function niceMax(v) {
    if (!(v > 0)) return 1;
    const p = 10 ** Math.floor(Math.log10(v));
    return [1, 2, 5, 10].map((m) => m * p).find((m) => v <= m);
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

  function spark(container, values, color) {
    let svg = container.querySelector('svg.spark');
    if (!svg) {
      svg = svgEl('svg', {class: 'spark', viewBox: '0 0 100 24', preserveAspectRatio: 'none',
                          'aria-hidden': 'true'});
      container.append(svg);
    }
    const known = values.filter((v) => !none(v));
    const max = Math.max(...known, 0) || 1;
    const n = Math.max(values.length - 1, 1);
    const path = svgEl('path', {d: pathFor(values, (i) => 100 * i / n, (v) => 22 - 20 * v / max)});
    if (color) path.setAttribute('style', `stroke:${color}`);
    svg.replaceChildren(path);
  }

  // Legend keys mirror the mark: a line for lines, a block for areas.
  function legend(container, series, kind = 'line') {
    container.replaceChildren(...series.map((s) => el('span', {class: 'key'},
      el('i', {class: `key-${kind}`, style: `background:${s.color}`}), s.name)));
  }

  /**
   * Draw a time chart into `svg`: `series` is [{name, values, color}] aligned
   * with `t` (epoch seconds), plus an optional short `label` for the line end.
   * opts: fmt (axis and tooltip values), max (fixed
   * top), stacked (areas, bottom first), labels (end-of-line names).
   */
  function chart(svg, t, series, opts = {}) {
    const format = opts.fmt || fmt.num;
    const w = Math.max(240, svg.clientWidth || 600);
    const h = Math.max(120, svg.clientHeight || 180);
    const labels = opts.labels && !opts.stacked && series.length <= 4;
    const pad = {l: 72, r: labels ? 96 : 12, t: 10, b: 22};
    const iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    svg.replaceChildren();
    const n = t.length;
    const now = n ? t[n - 1] : 0;
    const x = (i) => pad.l + iw * (1 - (now - t[i]) / WINDOW_S);
    let tops = series.map((s) => s.values);
    if (opts.stacked) {
      let base = new Array(n).fill(0);
      tops = series.map((s) => {
        const top = s.values.map((v, i) => (none(v) ? null : base[i] + v));
        base = top.map((v, i) => (none(v) ? base[i] : v));
        return top;
      });
    }
    const known = tops.flat().filter((v) => !none(v));
    const max = opts.max || niceMax(Math.max(...known, 0));
    const y = (v) => pad.t + ih * (1 - Math.min(Math.max(v, 0), max) / max);
    for (const f of [0, 0.5, 1]) {
      const gy = y(max * f);
      svg.append(svgEl('line', {class: f ? 'grid' : 'baseline', x1: pad.l, x2: w - pad.r, y1: gy, y2: gy}));
      const label = svgEl('text', {class: 'axis', x: pad.l - 8, y: gy + 4, 'text-anchor': 'end'});
      label.textContent = format(max * f);
      svg.append(label);
    }
    [['−10m', pad.l, 'start'], ['−5m', pad.l + iw / 2, 'middle'], ['now', w - pad.r, 'end']]
      .forEach(([text, lx, anchor]) => {
        const label = svgEl('text', {class: 'axis', x: lx, y: h - 6, 'text-anchor': anchor});
        label.textContent = text;
        svg.append(label);
      });
    if (opts.stacked) {
      let below = new Array(n).fill(0);
      series.forEach((s, k) => {
        const top = tops[k];
        let d = '';
        let run = [];
        const flush = () => {
          if (run.length) {
            d += `M${run.map((i) => `${x(i).toFixed(1)},${y(top[i]).toFixed(1)}`).join('L')}`
              + `L${run.slice().reverse().map((i) => `${x(i).toFixed(1)},${y(below[i]).toFixed(1)}`).join('L')}Z`;
          }
          run = [];
        };
        top.forEach((v, i) => { if (none(v)) flush(); else run.push(i); });
        flush();
        svg.append(svgEl('path', {class: 'area', d, style: `fill:${s.color}`}));
        below = top.map((v, i) => (none(v) ? below[i] : v));
      });
    } else {
      series.forEach((s) => {
        svg.append(svgEl('path', {class: 'line', d: pathFor(s.values, x, y), style: `stroke:${s.color}`}));
      });
    }
    if (labels) {
      const ends = series.map((s) => {
        let i = s.values.length - 1;
        while (i >= 0 && none(s.values[i])) i--;
        return i < 0 ? null : {name: s.label || s.name, y: y(s.values[i])};
      }).filter(Boolean).sort((a, b) => a.y - b.y);
      let last = -Infinity;
      for (const e of ends) {
        e.y = Math.max(e.y, last + 13);
        last = e.y;
        const label = svgEl('text', {class: 'end-label', x: w - pad.r + 6, y: e.y + 4});
        label.textContent = e.name.length > 14 ? `${e.name.slice(0, 13)}…` : e.name;
        svg.append(label);
      }
    }
    const cross = svgEl('line', {class: 'crosshair', x1: 0, x2: 0, y1: pad.t, y2: pad.t + ih,
                                 visibility: 'hidden'});
    svg.append(cross);
    svg._chart = {t, series, x, n, w, cross, format, stacked: opts.stacked};
    hover(svg);
  }

  // One tooltip per chart: the nearest sample's time and every series' value.
  function hover(svg) {
    if (svg._hover) return;
    svg._hover = true;
    svg.setAttribute('tabindex', '0');
    const host = svg.parentElement;
    const tip = el('div', {class: 'chart-tip', role: 'status', hidden: true});
    host.append(tip);
    let index = null;
    const show = (i) => {
      const c = svg._chart;
      if (!c || !c.n) return;
      index = Math.max(0, Math.min(c.n - 1, i));
      const cx = c.x(index);
      c.cross.setAttribute('x1', cx);
      c.cross.setAttribute('x2', cx);
      c.cross.setAttribute('visibility', 'visible');
      const ago = c.t[c.n - 1] - c.t[index];
      const rows = (c.stacked ? c.series.slice().reverse() : c.series).map((s) => el('div', {class: 'tip-row'},
        el('b', {}, c.format(s.values[index])),
        el('i', {class: 'key-line', style: `background:${s.color}`}), s.name));
      tip.replaceChildren(el('div', {class: 'tip-time'}, ago ? `−${fmt.duration(ago)}` : 'now'), ...rows);
      tip.hidden = false;
      const r = svg.getBoundingClientRect();
      const px = r.width * cx / c.w;
      tip.style.left = `${Math.round(px + (px > r.width * 0.6 ? -tip.offsetWidth - 12 : 12))}px`;
      tip.style.top = `${Math.round(svg.offsetTop + 8)}px`;
    };
    const hide = () => {
      tip.hidden = true;
      index = null;
      if (svg._chart) svg._chart.cross.setAttribute('visibility', 'hidden');
    };
    svg.addEventListener('pointermove', (ev) => {
      const c = svg._chart;
      if (!c || !c.n) return;
      const r = svg.getBoundingClientRect();
      const px = (ev.clientX - r.left) * c.w / r.width;
      let best = 0;
      for (let i = 1; i < c.n; i++) if (Math.abs(c.x(i) - px) < Math.abs(c.x(best) - px)) best = i;
      show(best);
    });
    svg.addEventListener('pointerleave', hide);
    svg.addEventListener('blur', hide);
    svg.addEventListener('focus', () => svg._chart && show(svg._chart.n - 1));
    svg.addEventListener('keydown', (ev) => {
      if (index === null) return;
      if (ev.key === 'ArrowLeft') show(index - 1);
      else if (ev.key === 'ArrowRight') show(index + 1);
      else return;
      ev.preventDefault();
    });
  }

  // -- polling --------------------------------------------------------------

  /** Fetch `url` every 2 s and render it; `freshness(stale, age)` after each. */
  function poll(url, render, freshness) {
    let generated = null, changedAt = 0, latest = null;
    const tick = async () => {
      try {
        const r = await fetch(url, {cache: 'no-store'});
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
        const age = (performance.now() - changedAt) / 1000;
        freshness(!latest || age > STALE_S, latest ? age : null);
        setTimeout(tick, 2000);
      }
    };
    tick();
    return () => latest;
  }

  /** The stale banner shared by both pages. */
  function showFreshness(stale, age) {
    $('unavailable').hidden = !stale;
    document.body.classList.toggle('stale', stale && age !== null);
  }

  return {SERIES, OTHER, $, el, svgEl, none, fmt, niceMax, spark, legend, chart, poll,
          showFreshness};
})();
