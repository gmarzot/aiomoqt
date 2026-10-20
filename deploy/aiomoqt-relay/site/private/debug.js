// The relay's DEBUG logging switch: GET /private/debug reads it, POST toggles it.
'use strict';

(() => {
  const URL = '/private/debug';

  async function call(method) {
    // The custom header keeps cross-site forms from toggling it.
    const headers = method === 'POST' ? {'X-Relay-Control': '1'} : {};
    const res = await fetch(URL, {method, headers, cache: 'no-store'});
    if (!res.ok) throw new Error(String(res.status));
    return (await res.json()).debug === true;
  }

  function mount(root) {
    const label = document.createElement('span');
    const button = document.createElement('button');
    button.type = 'button';
    root.replaceChildren(label, button);
    let on = null;
    const show = (value) => {
      on = value;
      label.textContent = `Debug logging: ${on ? 'on' : 'off'}`;
      button.textContent = on ? 'Turn off' : 'Turn on';
      root.classList.toggle('on', on);
      button.disabled = false;
    };
    const unavailable = () => {
      on = null;
      label.textContent = 'Debug logging: unavailable';
      button.textContent = 'Retry';
      root.classList.remove('on');
      button.disabled = false;
    };
    button.addEventListener('click', () => {
      button.disabled = true;
      call(on === null ? 'GET' : 'POST').then(show, unavailable);
    });
    call('GET').then(show, unavailable);
  }

  for (const root of document.querySelectorAll('[data-debug-toggle]')) mount(root);
})();
