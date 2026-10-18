// Fills [data-versions] with the relay's software and start time and the G5 pages' build.
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

  function relayParts(body) {
    try {
      const { generated, relay } = JSON.parse(body);
      const v = relay.versions;
      // "v2026.10.08.c908872-0-gc9088724" names the picoquic snapshot by date.
      const pico = /^v?(\d{4}\.\d\d\.\d\d)/.exec(v.picoquic || '');
      const parts = [`relay aiomoqt ${v.aiomoqt}`, `aiopquic ${v.aiopquic}`];
      if (pico) parts.push(`picoquic ${pico[1]}`);
      if (relay.uptime_s != null) parts.push(`up since ${when((generated - relay.uptime_s) * 1000)}`);
      return parts;
    } catch {
      return [];
    }
  }

  // /VERSION: "moq-playa <build> <built-at ISO 8601>".
  function pagesParts(body) {
    const m = /^moq-playa (\S+)(?:\s+(\S+))?/.exec((body || '').trim());
    if (!m) return [];
    const at = Date.parse(m[2]);
    return [`G5 pages moq-playa ${m[1]}`].concat(Number.isNaN(at) ? [] : [`built ${when(at)}`]);
  }

  async function render() {
    const nodes = document.querySelectorAll('[data-versions]');
    if (!nodes.length) return;
    const [platform, site] = await Promise.all([get('/status/platform.json'), get('/VERSION')]);
    const line = relayParts(platform).concat(pagesParts(site)).join(' · ');
    for (const n of nodes) n.textContent = line;
  }

  render();
})();
