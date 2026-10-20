// MoQ Ersatz Relay log viewer: tails the relay's --log-dir files under files/,
// with level and text filters. Log text is inserted as text only.
'use strict';

(() => {
  const {$, el, fmt} = Dash;
  const BASE = 'files/';
  const NAMES = /^(relay\.log(\.[1-9])?|crash\.log)$/;
  const CHUNK = 256 * 1024;
  const POLL_MS = 2000;
  const LIST_MS = 30000;
  const MAX_RECORDS = 20000;
  // Shift- or Ctrl-click on "Load earlier" loads the whole file under this cap.
  const MAX_RECORDS_ALL = 200000;
  const LEVELS = {DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50};
  // aiomoqt loggers: "… 20:14:09.826 INFO : msg"; others: "… 20:14:09,826 INFO name: msg".
  const HEAD = /^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[.,]\d{3}) (DEBUG|INFO|WARNING|ERROR|CRITICAL)(?: +(\S+))? *: /;
  const START = /^relay: start /;
  // faulthandler: a fatal signal, or the watchdog's stall dump.
  const CRASH = /^(Fatal Python error|Timeout \(\d+:\d\d:\d\d(\.\d+)?\)!)/;
  const decoder = new TextDecoder('utf-8');

  const state = {
    file: 'relay.log', level: 0, q: '', follow: true,
    records: [], start: 0, end: 0, size: null, gen: 0, cap: MAX_RECORDS,
    polled: null, error: null, timer: null, cursor: {},
  };

  // -- parsing ----------------------------------------------------------------

  /** Complete lines of `bytes` (at file offset `base`); a trailing partial line is left. */
  function splitLines(bytes, base) {
    const lines = [];
    let from = 0;
    for (let i = 0; i < bytes.length; i++) {
      if (bytes[i] !== 10) continue;
      let to = i;
      if (to > from && bytes[to - 1] === 13) to--;
      lines.push({off: base + from, end: base + i + 1, text: decoder.decode(bytes.subarray(from, to))});
      from = i + 1;
    }
    return {lines, used: from};
  }

  function newRecord(line) {
    const r = {off: line.off, end: line.end, head: null, level: 0, kind: null, lines: [line.text]};
    const m = HEAD.exec(line.text);
    if (m) {
      r.head = {ts: m[1], level: m[2], logger: m[3]};
      r.level = LEVELS[m[2]];
      r.lines[0] = line.text.slice(m[0].length);
      if (START.test(r.lines[0])) r.kind = 'start';
      else if (r.level >= 40) r.kind = 'error';
      return r;
    }
    if (START.test(line.text)) return Object.assign(r, {level: 20, kind: 'start'});
    if (CRASH.test(line.text)) return Object.assign(r, {level: 50, kind: 'crash'});
    return null;
  }

  /** Group lines into records; lines with no header continue the one before. */
  function group(lines, recs) {
    const touched = new Set();
    for (const line of lines) {
      const r = newRecord(line);
      const last = recs[recs.length - 1];
      if (r) recs.push(r);
      else if (last) { last.lines.push(line.text); last.end = line.end; touched.add(last); }
      else recs.push({off: line.off, end: line.end, head: null, level: 0, kind: null,
                      lines: [line.text]});
    }
    return touched;
  }

  // -- fetching ---------------------------------------------------------------

  async function getRange(name, range) {
    const r = await fetch(BASE + encodeURIComponent(name),
                          {headers: {Range: range}, cache: 'no-store'});
    const cr = r.headers.get('Content-Range') || '';
    const total = Number((/\/(\d+)$/.exec(cr) || [])[1] ?? NaN);
    if (r.status === 416) return {status: 416, total, start: total, bytes: new Uint8Array()};
    if (r.status !== 200 && r.status !== 206) {
      const e = new Error(`HTTP ${r.status}`);
      e.status = r.status;
      throw e;
    }
    const bytes = new Uint8Array(await r.arrayBuffer());
    if (r.status === 200) return {status: 200, total: bytes.length, start: 0, bytes};
    return {status: 206, total, start: Number((/bytes (\d+)-/.exec(cr) || [])[1] ?? 0), bytes};
  }

  /** Load the last CHUNK bytes of the current file; `note` marks why the view restarted. */
  async function loadTail(note) {
    const gen = ++state.gen;
    state.cap = MAX_RECORDS;
    let res;
    try {
      res = await getRange(state.file, `bytes=-${CHUNK}`);
    } catch (e) {
      if (gen === state.gen) fail(e);
      return;
    }
    if (gen !== state.gen) return;
    let {bytes, start} = res;
    if (start > 0) {
      const nl = bytes.indexOf(10);
      bytes = nl < 0 ? new Uint8Array() : bytes.subarray(nl + 1);
      start += nl < 0 ? res.bytes.length : nl + 1;
    }
    const {lines, used} = splitLines(bytes, start);
    state.records = [];
    if (note) state.records.push({off: start, end: start, head: null, level: 20, kind: 'note',
                                  lines: [note]});
    group(lines, state.records);
    Object.assign(state, {start, end: start + used, size: res.total, error: null,
                          polled: Date.now(), cursor: {}});
    renderAll(true);
  }

  async function poll() {
    const gen = state.gen;
    let res;
    try {
      res = await getRange(state.file, `bytes=${state.end}-`);
    } catch (e) {
      if (gen === state.gen) fail(e);
      return;
    }
    if (gen !== state.gen) return;
    state.polled = Date.now();
    state.error = null;
    // A file shorter than what was read has been rotated or replaced.
    if (res.total < state.end) {
      await loadTail(`${state.file} rotated; showing the new file`);
      return;
    }
    let bytes = res.bytes;
    if (res.status === 200) bytes = bytes.subarray(state.end);
    state.size = res.total;
    if (!bytes.length) { renderStatus(); return; }
    const {lines, used} = splitLines(bytes, state.end);
    state.end += used;
    if (!lines.length) { renderStatus(); return; }
    const before = state.records.length;
    const touched = group(lines, state.records);
    appendRecords(before, touched);
  }

  async function loadEarlier(e) {
    const all = !!(e && (e.shiftKey || e.ctrlKey || e.metaKey));
    if (all) state.cap = MAX_RECORDS_ALL;
    if (state.start <= 0 || state.records.length >= state.cap) return;
    const gen = state.gen;
    const from = all ? 0 : Math.max(0, state.start - CHUNK);
    let res;
    try {
      res = await getRange(state.file, `bytes=${from}-${state.start - 1}`);
    } catch (e) {
      if (gen === state.gen) fail(e);
      return;
    }
    if (gen !== state.gen || res.status !== 206) return;
    let {bytes, start} = res;
    if (start > 0) {
      const nl = bytes.indexOf(10);
      if (nl < 0) return;
      bytes = bytes.subarray(nl + 1);
      start += nl + 1;
    }
    const recs = [];
    group(splitLines(bytes, start).lines, recs);
    // A first record without a header continues the last one loaded here.
    const first = state.records[0];
    const last = recs[recs.length - 1];
    if (first && last && !first.head && !first.kind) {
      last.lines.push(...first.lines);
      last.end = first.end;
      state.records.shift();
    }
    const pane = $('pane');
    const fromBottom = pane.scrollHeight - pane.scrollTop;
    state.records = recs.concat(state.records);
    state.start = start;
    renderAll(false);
    pane.scrollTop = pane.scrollHeight - fromBottom;
  }

  function fail(e) {
    state.error = e.status === 401 ? 'Not signed in: reload the page.'
      : e.status === 404 ? `${state.file} not found.`
      : e.status ? `${state.file}: ${e.message}.` : 'Relay logs unreachable; retrying.';
    renderStatus();
  }

  async function loadList() {
    let list;
    try {
      const r = await fetch(BASE, {cache: 'no-store'});
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      list = (await r.json()).filter((f) => f.type === 'file' && NAMES.test(f.name));
    } catch (e) {
      list = null;
    }
    const sel = $('file');
    const order = (n) => n === 'relay.log' ? 0 : n === 'crash.log' ? 99 : Number(n.split('.').pop());
    const files = (list || []).sort((a, b) => order(a.name) - order(b.name));
    if (!files.some((f) => f.name === state.file)) files.unshift({name: state.file});
    sel.replaceChildren(...files.map((f) => {
      const age = f.mtime ? (Date.now() - Date.parse(f.mtime)) / 1000 : null;
      const meta = f.size === undefined ? '' : ` · ${fmt.bytes(f.size)} · ${fmt.duration(age)} ago`;
      return el('option', {value: f.name, selected: f.name === state.file}, f.name + meta);
    }));
    if (list && !list.length) {
      state.error = 'No relay logs: the relay is running without --log-dir.';
      renderStatus();
    }
  }

  // -- rendering --------------------------------------------------------------

  const text = (r) => r.lines.join('\n');
  const lower = (r) => ((r.head ? `${r.head.ts} ${r.head.level} ${r.head.logger || ''}: ` : '')
    + text(r)).toLowerCase();

  function visible(r) {
    if (r.kind === 'start' || r.kind === 'note') return true;
    if (r.level < state.level) return false;
    return !state.q || lower(r).includes(state.q);
  }

  /** `s` with each case-insensitive match of the filter wrapped in <mark>. */
  function marked(s) {
    const q = state.q;
    if (!q) return [s];
    const out = [];
    const ls = s.toLowerCase();
    let i = 0;
    for (let j = ls.indexOf(q); j >= 0; j = ls.indexOf(q, i)) {
      if (j > i) out.push(s.slice(i, j));
      out.push(el('mark', {}, s.slice(j, j + q.length)));
      i = j + q.length;
    }
    if (i < s.length) out.push(s.slice(i));
    return out;
  }

  function node(r) {
    const cls = ['rec'];
    if (r.head) cls.push(`lvl-${r.head.level.toLowerCase()}`);
    if (r.kind) cls.push(`kind-${r.kind}`);
    const n = el('div', {class: cls.join(' ')});
    if (r.head) {
      n.append(el('span', {class: 'ts'}, r.head.ts), ' ', el('span', {class: 'lv'}, r.head.level), ' ');
      if (r.head.logger) n.append(el('span', {class: 'lg'}, `${r.head.logger}:`), ' ');
    }
    n.append(...marked(text(r)));
    r.node = n;
    return n;
  }

  const atBottom = (pane) => pane.scrollHeight - pane.scrollTop - pane.clientHeight < 8;

  function trim() {
    const over = state.records.length - state.cap;
    if (over <= 0) return;
    for (const r of state.records.splice(0, over)) r.node?.remove();
    state.start = state.records[0].off;
  }

  function renderAll(toBottom) {
    trim();
    const pane = $('pane');
    pane.replaceChildren(...state.records.filter(visible).map(node));
    if (toBottom) pane.scrollTop = pane.scrollHeight;
    renderStatus();
  }

  function appendRecords(before, touched) {
    const pane = $('pane');
    const stick = atBottom(pane);
    for (const r of touched) {
      if (r.node?.isConnected) r.node.replaceWith(node(r));
    }
    const add = state.records.slice(before).filter(visible).map(node);
    pane.append(...add);
    trim();
    if (stick) pane.scrollTop = pane.scrollHeight;
    renderStatus();
  }

  function renderStatus() {
    const shown = state.records.filter((r) => r.node?.isConnected);
    const s = state.error
      || `${state.file} · ${fmt.bytes(state.size)} · showing ${fmt.bytes(state.end - state.start)}`
        + (state.start === 0 ? ' (whole file)' : '')
        + ` · ${state.follow ? 'following' : 'paused'}`
        + (state.polled ? `, read ${new Date(state.polled).toISOString().slice(11, 19)} UTC` : '');
    $('status').textContent = s;
    $('status').classList.toggle('bad', !!state.error);
    for (const kind of ['start', 'error', 'crash']) {
      const n = shown.filter((r) => r.kind === kind).length;
      const b = $(`jump-${kind === 'start' ? 'restart' : kind}`);
      b.querySelector('b').textContent = fmt.num(n);
      b.disabled = n === 0;
    }
    const earlier = $('earlier');
    earlier.disabled = state.start <= 0 || state.records.length >= state.cap;
    earlier.title = state.records.length >= state.cap
      ? `At ${fmt.num(state.cap)} records: download the raw file for more.`
      : 'Shift- or Ctrl-click: load the whole file';
  }

  /** Scroll to the latest shown record of `kind`, then to earlier ones on each click. */
  function jump(kind) {
    const shown = state.records.filter((r) => r.kind === kind && r.node?.isConnected);
    if (!shown.length) return;
    const prev = state.cursor[kind];
    let i = prev ? shown.indexOf(prev) - 1 : shown.length - 1;
    if (i < 0) i = shown.length - 1;
    const r = shown[i];
    state.cursor[kind] = r;
    r.node.scrollIntoView({block: 'center'});
    r.node.classList.remove('hit');
    void r.node.offsetWidth;
    r.node.classList.add('hit');
  }

  // -- controls ---------------------------------------------------------------

  function saveHash() {
    const p = new URLSearchParams();
    if (state.file !== 'relay.log') p.set('file', state.file);
    if (state.level) p.set('level', String(state.level));
    if (state.q) p.set('q', state.q);
    const h = p.toString();
    history.replaceState(null, '', h ? `#${h}` : location.pathname + location.search);
  }

  function readHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    const f = p.get('file');
    if (f && NAMES.test(f)) state.file = f;
    const lv = Number(p.get('level'));
    if ([0, 20, 30, 40].includes(lv)) state.level = lv;
    state.q = (p.get('q') || '').toLowerCase();
  }

  function schedule() {
    clearTimeout(state.timer);
    if (!state.follow) return;
    state.timer = setTimeout(async () => {
      if (!document.hidden) await poll();
      schedule();
    }, POLL_MS);
  }

  function setFile(name) {
    state.file = name;
    $('download').href = BASE + encodeURIComponent(name);
    $('download').setAttribute('download', name);
    saveHash();
    loadTail().then(schedule);
  }

  function init() {
    readHash();
    $('level').value = String(state.level);
    $('filter').value = state.q;
    $('wrap').addEventListener('change', (e) => $('pane').classList.toggle('wrap', e.target.checked));
    $('follow').addEventListener('change', (e) => {
      state.follow = e.target.checked;
      renderStatus();
      if (state.follow) poll().then(schedule); else clearTimeout(state.timer);
    });
    $('file').addEventListener('change', (e) => setFile(e.target.value));
    $('level').addEventListener('change', (e) => {
      state.level = Number(e.target.value);
      saveHash();
      renderAll(true);
    });
    let typing;
    $('filter').addEventListener('input', (e) => {
      clearTimeout(typing);
      typing = setTimeout(() => {
        state.q = e.target.value.trim().toLowerCase();
        saveHash();
        renderAll(true);
      }, 200);
    });
    $('earlier').addEventListener('click', loadEarlier);
    $('jump-restart').addEventListener('click', () => jump('start'));
    $('jump-error').addEventListener('click', () => jump('error'));
    $('jump-crash').addEventListener('click', () => jump('crash'));
    loadList();
    setInterval(loadList, LIST_MS);
    setFile(state.file);
  }

  init();
})();
