// Fills [data-versions] with the relay's software and start time, [data-pages-build] with the G5 pages' build.
'use strict';

(() => {
  const when = (ms) => new Date(ms).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });

  async function get(url) {
    try {
      const res = await fetch(url, { cache: 'no-store' });
      return res.ok ? await res.text() : null;
    } catch {
      return null;
    }
  }

  // "[aiomoqt … · aiopquic … · picoquic … · deployed: …]" from the relay's platform.json.
  function relayLine(body) {
    try {
      const { generated, relay } = JSON.parse(body);
      const v = relay.versions;
      // "v2026.10.08.c908872-0-gc9088724" names the picoquic snapshot by date.
      const pico = /^v?(\d{4}\.\d\d\.\d\d)/.exec(v.picoquic || '');
      const parts = [`aiomoqt ${v.aiomoqt}`, `aiopquic ${v.aiopquic}`];
      if (pico) parts.push(`picoquic ${pico[1]}`);
      if (relay.uptime_s != null) parts.push(`deployed: ${when((generated - relay.uptime_s) * 1000)}`);
      return `[${parts.join(' · ')}]`;
    } catch {
      return '';
    }
  }

  // /VERSION: "moq-playa <build> <built-at ISO 8601>".
  function pagesLine(body) {
    const m = /^moq-playa (\S+)(?:\s+(\S+))?/.exec((body || '').trim());
    if (!m) return '';
    const at = Date.parse(m[2]);
    return `G5 pages moq-playa ${m[1]}` + (Number.isNaN(at) ? '' : ` · built ${when(at)}`);
  }

  async function fill(selector, url, line) {
    const nodes = document.querySelectorAll(selector);
    if (!nodes.length) return;
    const text = line(await get(url));
    for (const n of nodes) n.textContent = text;
  }

  function render() {
    fill('[data-versions]', '/status/platform.json', relayLine);
    fill('[data-pages-build]', '/VERSION', pagesLine);
  }

  render();
})();
