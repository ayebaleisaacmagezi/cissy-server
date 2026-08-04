'use strict';

/* Cissy Build — browser client.
 *
 * No framework and no build step: the whole UI is this file, so editing it on
 * the server means a refresh rather than a toolchain.
 *
 * DOM is built with el() rather than innerHTML throughout. App names, URLs and
 * build logs all come from outside and end up in the page, so string-
 * interpolated HTML would be an injection waiting to happen.
 */

const FEATURES = [
  ['File upload', 'Let the site open the file picker'],
  ['Downloads', 'Save files to the device and open them'],
  ['Native sharing', "Use the phone's share sheet"],
  ['Pull to refresh', 'Swipe down to reload'],
  ['Camera', 'Adds a permission prompt'],
  ['Location', 'Adds a permission prompt'],
  ['Deep links', 'Open the app from links to your domain'],
];

const state = {
  apps: [],
  app: null,
  builds: [],
  dirty: false,
  health: null,
  password: localStorage.getItem('cissy-password') || '',
  streaming: null,
};

/* ── tiny DOM helper ─────────────────────────────────────────────────── */

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key in node && key !== 'list') node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/* ── api ─────────────────────────────────────────────────────────────── */

function authHeaders(extra = {}) {
  const headers = { ...extra };
  if (state.password) headers['X-Cissy-Password'] = state.password;
  return headers;
}

async function api(method, path, body) {
  const options = { method, headers: authHeaders() };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }

  const response = await fetch(path, options);
  if (response.status === 401) {
    await askForPassword(Boolean(state.password));
    return api(method, path, body);
  }

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || `Request failed (${response.status})`);
    error.detail = payload.detail;
    throw error;
  }
  return payload;
}

async function upload(appId, slot, file) {
  const response = await fetch(`/api/apps/${appId}/files/${slot}`, {
    method: 'PUT',
    headers: authHeaders({ 'X-Filename': file.name }),
    body: file,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || 'Upload failed');
  return payload.app;
}

/* Reads a server-sent event stream over fetch rather than EventSource.
 * EventSource cannot send headers, which would mean putting the password in a
 * query string — where it lands in proxy logs and browser history. */
async function readEvents(path, { onLine, onDone }) {
  const response = await fetch(path, { headers: authHeaders() });
  if (!response.ok || !response.body) return;

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let split;
    while ((split = buffer.indexOf('\n\n')) !== -1) {
      const chunk = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      if (chunk.startsWith(':')) continue;

      let name = 'message';
      const data = [];
      for (const line of chunk.split('\n')) {
        if (line.startsWith('event: ')) name = line.slice(7);
        else if (line.startsWith('data: ')) data.push(line.slice(6));
      }
      const payload = data.join('\n');
      if (name === 'line') onLine(payload);
      else if (name === 'done') onDone(JSON.parse(payload));
    }
  }
}

function toast(message, bad = false) {
  const node = document.getElementById('toast');
  node.textContent = message;
  node.className = bad ? 'bad' : '';
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, bad ? 7000 : 2800);
}

/* ── modals ──────────────────────────────────────────────────────────── */

function openModal(title, subtitle, bodyNodes, actions) {
  // One modal at a time. Two stacked dialogs share a container, so closing
  // either would tear down both and strand whatever the other was awaiting.
  const root = clear(document.getElementById('modal-root'));
  const modal = el('div', { class: 'modal' }, [
    el('h3', { text: title }),
    subtitle ? el('p', { class: 'sub', text: subtitle }) : null,
    ...bodyNodes,
    el('div', { class: 'modal-actions' }, actions),
  ]);
  const veil = el('div', { class: 'veil' }, [modal]);
  veil.addEventListener('mousedown', (event) => {
    if (event.target === veil) close();
  });
  function close() { clear(root); }
  root.append(veil);
  const first = modal.querySelector('input');
  if (first) first.focus();
  return close;
}

/* The page fires several requests at once, so a wrong or missing password
 * produces several 401s at the same moment. They must all wait on one prompt:
 * a dialog per request strands every request but the one that gets answered. */
let passwordPrompt = null;

function askForPassword(wasWrong = false) {
  if (passwordPrompt) return passwordPrompt;

  passwordPrompt = new Promise((resolve) => {
    const input = el('input', { class: 'input', type: 'password', value: '' });
    const submit = () => {
      if (!input.value) return;
      state.password = input.value;
      localStorage.setItem('cissy-password', input.value);
      passwordPrompt = null;
      close();
      resolve();
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
    const close = openModal(
      wasWrong ? 'That password was not accepted' : 'Password required',
      'Whatever CISSY_PASSWORD was set to when the server was started.',
      [el('div', { class: 'field' }, [input])],
      [el('button', { class: 'btn primary', text: 'Unlock', onclick: submit })],
    );
  });

  return passwordPrompt;
}

/* ── health ──────────────────────────────────────────────────────────── */

async function refreshHealth(force = false) {
  const foot = document.getElementById('side-foot');
  try {
    state.health = await api('GET', '/api/health' + (force ? '?refresh=1' : ''));
  } catch {
    clear(foot).append(el('div', { class: 'ln bad', text: 'Server unreachable' }));
    return;
  }

  const health = state.health;
  clear(foot).append(
    el('div', { class: 'ln ' + (health.ok ? 'good' : 'bad') }, [
      el('i', { class: 'dot' }),
      health.ok ? 'Toolchain ready' : 'Toolchain not ready',
    ]),
  );
  for (const tool of health.tools) {
    foot.append(el('div', { class: 'ln', text: tool.ok
      ? `${tool.name} ${tool.version}`
      : `${tool.name} — ${tool.detail}` }));
  }
}

/* ── sidebar ─────────────────────────────────────────────────────────── */

const SECTIONS = [
  ['overview', 'Overview', '▣'],
  ['identity', 'Identity', '⬚'],
  ['webview', 'WebView', '◎'],
  ['branding', 'Branding', '◈'],
  ['features', 'Features', '⊞'],
  ['offline', 'Offline', '☁'],
  ['signing', 'Signing', '⚿'],
  ['build', 'Build', '▶'],
];

function renderSidebar() {
  const nav = clear(document.getElementById('side-nav'));

  if (!state.app) {
    nav.append(el('div', { class: 'nav-group' }, [
      el('button', { class: 'nav-item active', onclick: () => go('#/') }, [
        el('span', { class: 'gl', text: '▦' }), 'All apps',
        el('span', { class: 'badge', text: String(state.apps.length) }),
      ]),
      el('button', { class: 'nav-item', onclick: newAppDialog }, [
        el('span', { class: 'gl', text: '＋' }), 'New app',
      ]),
    ]));
    return;
  }

  nav.append(
    el('button', { class: 'appswitch', onclick: () => go('#/') }, [
      el('span', { class: 'ico' }),
      el('span', { class: 'nm', text: state.app.name }),
      el('span', { class: 'car', text: '⌄' }),
    ]),
    el('div', { class: 'nav-group' }, [
      el('div', { class: 'nav-label', text: 'App' }),
      ...SECTIONS.map(([id, label, glyph]) =>
        el('button', {
          class: 'nav-item',
          onclick: () => {
            const target = document.getElementById('sec-' + id);
            if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
          },
        }, [el('span', { class: 'gl', text: glyph }), label]),
      ),
    ]),
  );
}

/* ── app list ────────────────────────────────────────────────────────── */

async function showList() {
  state.app = null;
  const { apps } = await api('GET', '/api/apps');
  state.apps = apps;

  document.getElementById('crumb').textContent = 'All apps';
  clear(document.getElementById('topbar-actions')).append(
    el('button', { class: 'btn primary', text: '+ New app', onclick: newAppDialog }),
  );

  renderSidebar();
  const content = clear(document.getElementById('content'));

  if (!apps.length) {
    content.append(el('div', { class: 'empty' }, [
      el('h3', { text: 'No apps yet' }),
      el('p', { text: 'Point Cissy at a website and it will build an Android app from it.' }),
      el('button', { class: 'btn primary', text: '+ New app', onclick: newAppDialog }),
    ]));
    return;
  }

  content.append(
    el('h2', { class: 'sec', text: 'Your apps' }),
    el('p', { class: 'sub', text: `${apps.length} on this server` }),
    el('table', { class: 'apps' }, [
      el('thead', {}, [el('tr', {},
        ['App', 'Website', 'Version', 'Signing'].map((h) => el('th', { text: h })))]),
      el('tbody', {}, apps.map((app) =>
        el('tr', { class: 'row', onclick: () => go('#/app/' + app.id) }, [
          el('td', {}, [el('span', { class: 'ico' }), el('span', { class: 'app-name', text: app.name })]),
          el('td', { class: 'app-url mono', text: hostOf(app.website_url) }),
          el('td', {}, [
            el('div', { text: `v${app.version_name} (${app.version_code})` }),
            el('div', { class: 'app-url', text: 'Edited ' + shortDate(app.updated_at) }),
          ]),
          el('td', {}, [signingPill(app)]),
        ]))),
    ]),
  );
}

function signingPill(app) {
  return app.keystore_file && app.key_alias
    ? el('span', { class: 'pill ok' }, [el('i', { class: 'dot' }), 'Upload key'])
    : el('span', { class: 'pill warn' }, [el('i', { class: 'dot' }), 'Debug key']);
}

function newAppDialog() {
  const name = el('input', { class: 'input', placeholder: 'Cissytech Portal' });
  const url = el('input', { class: 'input mono', placeholder: 'https://portal.cissytech.com' });
  const pkg = el('input', { class: 'input mono', placeholder: 'com.cissytech.portal' });

  // Suggested rather than generated silently: the package id is permanent once
  // the app is on Play.
  name.addEventListener('input', () => {
    if (pkg.dataset.touched) return;
    const slug = name.value.trim().toLowerCase().replace(/[^a-z0-9]+/g, '');
    pkg.value = slug ? `com.cissytech.${slug}` : '';
  });
  pkg.addEventListener('input', () => { pkg.dataset.touched = '1'; });

  const create = async () => {
    try {
      const { app } = await api('POST', '/api/apps', {
        name: name.value, website_url: url.value, android_package_id: pkg.value,
      });
      close();
      go('#/app/' + app.id);
      toast(`Created ${app.name}`);
    } catch (error) { toast(error.message, true); }
  };

  const close = openModal(
    'New app',
    'All of this can change later — except the package ID, once it is on Play.',
    [
      field('App name', name),
      field('Website URL', url, 'The page the app opens on launch.'),
      field('Android package ID', pkg, 'Lower-case, dot-separated, permanent once published.'),
    ],
    [
      el('button', { class: 'btn ghost', text: 'Cancel', onclick: () => close() }),
      el('button', { class: 'btn primary', text: 'Create', onclick: create }),
    ],
  );
}

function field(label, input, hint) {
  return el('div', { class: 'field' }, [
    el('label', { text: label }), input,
    hint ? el('p', { class: 'hint', text: hint }) : null,
  ]);
}

/* ── app page ────────────────────────────────────────────────────────── */

async function showApp(appId) {
  const [{ app }, { builds }] = await Promise.all([
    api('GET', '/api/apps/' + appId),
    api('GET', `/api/apps/${appId}/builds`),
  ]);
  state.app = app;
  state.builds = builds;
  state.dirty = false;

  document.getElementById('crumb').textContent = app.name;
  renderSidebar();

  const draft = { ...app };
  const content = clear(document.getElementById('content'));
  const summary = el('aside', { class: 'summary' });
  const main = el('div');
  content.append(el('div', { class: 'cols' }, [main, summary]));

  const markDirty = () => { state.dirty = true; renderTopbar(); renderSummary(); };
  const bind = (key, input, transform = (v) => v) => {
    input.addEventListener('input', () => { draft[key] = transform(input.value); markDirty(); });
    return input;
  };

  main.append(
    overviewSection(app, builds),
    identitySection(app, bind),
    webviewSection(app, draft, bind, markDirty),
    brandingSection(app),
    featuresSection(app, draft, markDirty),
    offlineSection(app, draft, markDirty),
    signingSection(app),
    buildSection(app),
  );

  function renderSummary() {
    clear(summary).append(
      el('h4', { text: 'Summary' }),
      kv('Website', hostOf(draft.website_url)),
      kv('Package', draft.android_package_id),
      kv('Features', `${(draft.features || []).length} enabled`),
      kv('Next build', `${draft.version_name} (${app.next_version_code})`),
      kv('Signing', app.keystore_file && app.key_alias ? 'Upload key' : 'Debug key'),
      el('button', {
        class: 'btn primary', style: 'width:100%;justify-content:center;margin-top:14px',
        text: state.dirty ? 'Save changes' : 'Saved', disabled: !state.dirty, onclick: save,
      }),
      el('button', {
        class: 'btn', style: 'width:100%;justify-content:center;margin-top:8px',
        text: 'Duplicate as new app', onclick: () => duplicateDialog(app),
      }),
      el('button', {
        class: 'btn danger sm', style: 'width:100%;justify-content:center;margin-top:8px',
        text: 'Delete app', onclick: () => deleteDialog(app),
      }),
    );
  }

  function renderTopbar() {
    clear(document.getElementById('topbar-actions')).append(
      el('span', { class: 'pill' + (state.dirty ? ' warn' : ''),
        text: state.dirty ? 'Unsaved changes' : 'Saved' }),
      el('button', { class: 'btn', text: 'Save', disabled: !state.dirty, onclick: save }),
      el('button', { class: 'btn primary', text: 'Build', onclick: () => buildDialog(app) }),
    );
  }

  async function save() {
    try {
      const { app: saved } = await api('PUT', '/api/apps/' + app.id, draft);
      state.app = saved;
      state.dirty = false;
      document.getElementById('crumb').textContent = saved.name;
      renderSidebar();
      renderTopbar();
      renderSummary();
      toast('Saved');
    } catch (error) { toast(error.message, true); }
  }

  renderTopbar();
  renderSummary();
}

function fieldset(id, legend, children) {
  return el('fieldset', { class: 'group', id: 'sec-' + id },
    [el('legend', { text: legend }), ...children]);
}

function checkbox(label, checked, onchange, hint) {
  return el('label', { class: 'check', style: 'margin-bottom:14px' }, [
    el('input', { type: 'checkbox', checked, onchange: (e) => onchange(e.target.checked) }),
    el('span', {}, [label, hint ? el('div', { class: 'hint', text: hint }) : null]),
  ]);
}

function kv(key, value) {
  return el('div', { class: 'kv' }, [
    el('span', { text: key }), el('span', { text: value || '—' }),
  ]);
}

/* ── sections ────────────────────────────────────────────────────────── */

function overviewSection(app, builds) {
  const latest = builds.find((b) => b.status === 'succeeded');
  const children = [];

  // The drift warning is the point of this section: without it there is no way
  // to tell whether the artifact below matches the settings above.
  if (latest && Date.parse(app.updated_at) / 1000 > latest.started_at) {
    children.push(el('div', { class: 'banner warn' }, [
      el('b', { text: `Edited since build #${latest.number}` }),
      'The artifacts below do not include your latest changes. Build again to pick them up.',
    ]));
  }

  if (!builds.length) {
    children.push(el('p', { class: 'sub', text: 'No builds yet.' }));
  } else {
    children.push(el('table', { class: 'apps' }, [
      el('thead', {}, [el('tr', {},
        ['Build', 'When', 'Result', 'Files'].map((h) => el('th', { text: h })))]),
      el('tbody', {}, builds.map((build) => buildRow(app, build))),
    ]));
  }

  return fieldset('overview', 'Build history', children);
}

function buildRow(app, build) {
  const status = {
    succeeded: ['ok', build.signed ? 'Signed' : 'Debug key'],
    failed: ['err', 'Failed'],
    running: ['', 'Running'],
  }[build.status] || ['', build.status];

  return el('tr', {}, [
    el('td', {}, [
      el('span', { class: 'app-name mono', text: '#' + build.number }),
      el('div', { class: 'app-url', text: `v${build.version_name} (${build.version_code})` }),
    ]),
    el('td', {}, [
      el('div', { text: shortDate(new Date(build.started_at * 1000).toISOString()) }),
      el('div', { class: 'app-url', text: `${Math.round(build.duration)}s` }),
    ]),
    el('td', {}, [
      el('span', { class: 'pill ' + status[0] }, [el('i', { class: 'dot' }), status[1]]),
      build.hint ? el('div', { class: 'hint', text: build.hint }) : null,
    ]),
    el('td', {}, (build.artifacts || []).map((artifact) =>
      el('a', {
        class: 'btn sm', style: 'margin:2px 4px 2px 0',
        href: `/api/apps/${app.id}/builds/${build.number}/artifacts/${encodeURIComponent(artifact.name)}`,
        text: `${artifact.kind.toUpperCase()} · ${megabytes(artifact.size)}`,
        download: artifact.name,
      }))),
  ]);
}

function identitySection(app, bind) {
  return fieldset('identity', 'Identity', [
    field('Project name', bind('name', el('input', { class: 'input', value: app.name }))),
    field('App name', bind('app_name',
      el('input', { class: 'input', value: app.app_name || app.name })),
      'Shown under the icon on the phone.'),
    el('div', { class: 'row2' }, [
      field('Android package ID',
        el('input', { class: 'input mono', value: app.android_package_id, disabled: true }),
        'Permanent — duplicate the app to change it.'),
      field('iOS bundle ID',
        bind('ios_bundle_id', el('input', { class: 'input mono', value: app.ios_bundle_id }))),
    ]),
  ]);
}

function webviewSection(app, draft, bind, markDirty) {
  const external = el('select', { class: 'input' },
    [['browser', "Open in the phone's browser"],
     ['webview', 'Stay inside the app'],
     ['block', 'Block them']].map(([value, label]) =>
      el('option', { value, text: label, selected: app.external_link_behavior === value })));
  external.addEventListener('change', () => {
    draft.external_link_behavior = external.value;
    markDirty();
  });

  return fieldset('webview', 'WebView', [
    field('Website URL',
      bind('website_url', el('input', { class: 'input mono', value: app.website_url })),
      'The page the app opens on launch.'),
    field('Allowed domains',
      bind('allowed_domains',
        el('input', { class: 'input mono', value: (app.allowed_domains || []).join(', ') }),
        (v) => v.split(',').map((s) => s.trim()).filter(Boolean)),
      'Comma separated. Subdomains are included.'),
    field('Links outside those domains', external),
    field('Custom user agent',
      bind('custom_user_agent',
        el('input', { class: 'input mono', value: app.custom_user_agent || '',
          placeholder: 'Leave blank for the default' }),
        (v) => v.trim() || null)),
    checkbox('Require HTTPS', app.require_https,
      (on) => { draft.require_https = on; markDirty(); },
      'Blocks insecure page loads inside the app.'),
    checkbox('Enable JavaScript', app.javascript_enabled,
      (on) => { draft.javascript_enabled = on; markDirty(); }),
    checkbox('Enable local storage', app.dom_storage_enabled,
      (on) => { draft.dom_storage_enabled = on; markDirty(); },
      'Needed by most sites that keep you signed in.'),
  ]);
}

function brandingSection(app) {
  return fieldset('branding', 'Branding', [
    el('div', { class: 'row2' }, [
      uploadSlot(app, 'icon', 'App icon', 'PNG, ideally 1024×1024', '.png'),
      uploadSlot(app, 'splash', 'Splash image', 'Shown while the first page loads', '.png,.jpg,.jpeg'),
    ]),
  ]);
}

function uploadSlot(app, slot, label, hint, accept) {
  const current = app[slot + '_file'];
  const input = el('input', { type: 'file', accept, style: 'display:none' });
  const box = el('div', { class: 'drop' + (current ? ' filled' : '') });

  const render = () => {
    clear(box).append(
      el('b', { text: current ? current : label }),
      el('div', { class: 'hint', text: current ? 'Uploaded' : hint }),
      el('button', { class: 'btn sm', style: 'margin-top:10px',
        text: current ? 'Replace' : 'Choose file', onclick: () => input.click() }),
      current ? el('button', { class: 'btn sm ghost', style: 'margin-top:10px',
        text: 'Remove', onclick: () => removeFile(app.id, slot) }) : null,
    );
  };

  input.addEventListener('change', async () => {
    const file = input.files[0];
    if (!file) return;
    try {
      await upload(app.id, slot, file);
      toast(`${label} uploaded`);
      route();
    } catch (error) { toast(error.message, true); }
  });

  render();
  return el('div', {}, [box, input]);
}

async function removeFile(appId, slot) {
  try {
    await api('DELETE', `/api/apps/${appId}/files/${slot}`);
    toast('Removed');
    route();
  } catch (error) { toast(error.message, true); }
}

function featuresSection(app, draft, markDirty) {
  const enabled = new Set(app.features || []);
  return fieldset('features', 'Features', [
    el('div', { class: 'checks' }, FEATURES.map(([name, hint]) =>
      el('label', { class: 'check' }, [
        el('input', {
          type: 'checkbox', checked: enabled.has(name),
          onchange: (event) => {
            if (event.target.checked) enabled.add(name); else enabled.delete(name);
            draft.features = [...enabled];
            markDirty();
          },
        }),
        el('span', {}, [name, el('div', { class: 'hint', text: hint })]),
      ]))),
    el('p', { class: 'hint', style: 'margin-bottom:14px',
      text: 'Camera and Location need a reason shown to the user. iOS rejects builds without one, so a sensible default is written if you leave it blank.' }),
  ]);
}

function offlineSection(app, draft, markDirty) {
  return fieldset('offline', 'Offline', [
    checkbox('Cache pages for faster loading', app.cache_enabled,
      (on) => { draft.cache_enabled = on; markDirty(); }),
    checkbox('Show a branded screen when a page fails to load',
      app.offline_fallback_enabled,
      (on) => { draft.offline_fallback_enabled = on; markDirty(); },
      'Replaces the browser error page with one that offers Try again and Go home.'),
  ]);
}

function signingSection(app) {
  const signed = app.keystore_file && app.key_alias;
  const children = [];

  children.push(signed
    ? el('div', { class: 'banner ok' }, [
        el('b', { text: `Signing with ${app.keystore_file}` }),
        `Key alias "${app.key_alias}".`,
      ])
    : el('div', { class: 'banner warn' }, [
        el('b', { text: 'No upload keystore' }),
        'Builds will use a debug key, which Google Play rejects. Uploads and sideloading still work.',
      ]));

  children.push(uploadSlot(app, 'keystore', 'Upload keystore',
    '.jks or .keystore', '.jks,.keystore,.p12,.pfx'));

  const alias = el('input', { class: 'input mono', value: app.key_alias || '',
    placeholder: 'upload' });
  const saveAlias = async () => {
    try {
      await api('PUT', '/api/apps/' + app.id, { key_alias: alias.value.trim() || null });
      toast('Key alias saved');
      route();
    } catch (error) { toast(error.message, true); }
  };
  children.push(el('div', { class: 'row2', style: 'margin-top:14px' }, [
    field('Key alias', alias),
    el('div', { class: 'field' }, [
      el('label', { text: ' ' }),
      el('button', { class: 'btn', text: 'Save alias', onclick: saveAlias }),
    ]),
  ]));

  children.push(el('p', { class: 'hint', style: 'margin-bottom:14px',
    text: 'Passwords are never stored — you enter them each time you build. A leaked upload key cannot be revoked, so keep a backup somewhere safe.' }));

  return fieldset('signing', 'Signing', children);
}

function buildSection(app) {
  return fieldset('build', 'Build', [
    el('p', { class: 'sub',
      text: 'Runs on this server. One build at a time, typically 3–5 minutes.' }),
    el('button', { class: 'btn primary', text: 'Build now', onclick: () => buildDialog(app) }),
    el('button', { class: 'btn', style: 'margin-left:8px',
      text: 'Generate project only', onclick: () => generateOnly(app) }),
    el('p', { class: 'hint', style: 'margin-top:12px',
      text: 'Generating writes the Flutter project without building it — the fastest way to get the iOS project onto a Mac.' }),
  ]);
}

async function generateOnly(app) {
  toast('Generating…');
  try {
    await api('POST', `/api/apps/${app.id}/generate`);
    toast('Project generated on the server');
  } catch (error) { toast(error.message, true); }
}

/* ── building ────────────────────────────────────────────────────────── */

function buildDialog(app) {
  const signed = app.keystore_file && app.key_alias;

  const output = el('select', { class: 'input' }, [
    el('option', { value: 'aab', text: 'App bundle (.aab) — for Google Play' }),
    el('option', { value: 'apk', text: 'APK — for sideloading and testing' }),
  ]);
  const storePassword = el('input', { class: 'input', type: 'password' });
  const keyPassword = el('input', { class: 'input', type: 'password' });

  const body = [
    field('Output', output),
    el('div', { class: 'banner info' }, [
      el('b', { text: `Will build version ${app.version_name} (${app.next_version_code})` }),
      app.next_version_code === app.version_code
        ? 'This app has not been built at this version code yet, so it is used as it stands.'
        : 'The version code moves up because this app has already built at the current one, and Play rejects an upload that reuses a code.',
    ]),
  ];

  if (signed) {
    body.push(
      el('div', { class: 'row2' }, [
        field('Keystore password', storePassword),
        field('Key password', keyPassword),
      ]),
      el('p', { class: 'hint', text: `Signing with ${app.keystore_file}, alias "${app.key_alias}".` }),
    );
  } else {
    body.push(el('div', { class: 'banner warn' }, [
      el('b', { text: 'No keystore — this will use a debug key' }),
      'Google Play will reject the result. Fine for testing on a device.',
    ]));
  }

  const start = async () => {
    try {
      const { build } = await api('POST', `/api/apps/${app.id}/build`, {
        output: output.value,
        store_password: storePassword.value,
        key_password: keyPassword.value,
      });
      close();
      showBuild(app, build);
    } catch (error) { toast(error.message, true); }
  };

  const close = openModal('Build ' + app.name, null, body, [
    el('button', { class: 'btn ghost', text: 'Cancel', onclick: () => close() }),
    el('button', { class: 'btn primary', text: 'Start build', onclick: start }),
  ]);
}

function showBuild(app, build) {
  document.getElementById('crumb').textContent = `${app.name} — build #${build.number}`;
  clear(document.getElementById('topbar-actions')).append(
    el('button', { class: 'btn', text: 'Back to app', onclick: () => go('#/app/' + app.id) }),
  );

  const status = el('span', { class: 'pill' }, [el('i', { class: 'dot' }), 'Starting…']);
  const console = el('div', { class: 'console mono' });
  const result = el('div');

  const content = clear(document.getElementById('content'));
  content.append(
    el('div', { style: 'display:flex;align-items:center;gap:12px;margin-bottom:14px' }, [
      el('h2', { class: 'sec', style: 'margin:0', text: 'Build #' + build.number }),
      status,
    ]),
    result,
    console,
  );

  let pinned = true;
  console.addEventListener('scroll', () => {
    // Stop yanking the view back if the user has scrolled up to read something.
    pinned = console.scrollHeight - console.scrollTop - console.clientHeight < 40;
  });

  const append = (line) => {
    console.append(el('div', { class: 'l ' + lineClass(line), text: line }));
    if (pinned) console.scrollTop = console.scrollHeight;
  };

  readEvents(`/api/apps/${app.id}/builds/${build.number}/events`, {
    onLine: append,
    onDone: (finished) => {
      status.className = 'pill ' + (finished.status === 'succeeded' ? 'ok' : 'err');
      clear(status).append(el('i', { class: 'dot' }),
        finished.status === 'succeeded'
          ? `Built in ${Math.round(finished.duration)}s`
          : `Failed after ${Math.round(finished.duration)}s`);

      clear(result);
      if (finished.status === 'succeeded') {
        result.append(
          el('div', { class: 'banner ok' }, [
            el('b', { text: finished.signed
              ? 'Signed with your upload key'
              : 'Signed with a debug key' }),
            finished.signed
              ? 'This artifact can be uploaded to Google Play.'
              : 'Google Play will reject this. Good for testing on a device.',
          ]),
          el('div', { class: 'dl' }, (finished.artifacts || []).map((artifact) =>
            el('a', { class: 'card', download: artifact.name,
              href: `/api/apps/${app.id}/builds/${finished.number}/artifacts/${encodeURIComponent(artifact.name)}`,
            }, [
              el('div', { class: 'ttl', text: artifact.name }),
              el('div', { class: 'meta', text: `${megabytes(artifact.size)} · ${describe(artifact.kind)}` }),
              el('span', { class: 'btn sm primary', text: 'Download' }),
            ]))),
          el('div', { class: 'banner warn', style: 'margin-top:16px' }, [
            el('b', { text: 'For iOS, take the .zip to a Mac' }),
            'It has a complete ios/ folder with your bundle ID, name, icons and ' +
            'permission strings set. There: flutter pub get, then flutter build ipa. ' +
            "Apple's toolchain is macOS-only, so this is the one step the server cannot do.",
          ]),
        );
      } else {
        result.append(el('div', { class: 'banner err' }, [
          el('b', { text: finished.hint || 'The build failed' }),
          finished.hint ? (finished.error || '') : (finished.error || 'The log below has the details.'),
        ]));
      }
    },
  }).catch(() => {
    status.className = 'pill err';
    clear(status).append(el('i', { class: 'dot' }), 'Lost connection to the build');
  });

  status.className = 'pill';
  clear(status).append(el('i', { class: 'dot' }), 'Building…');
}

function lineClass(line) {
  if (/^ERROR|FAILURE|error:/i.test(line)) return 'r';
  if (/^WARNING|^Note:|warning:/i.test(line)) return 'y';
  if (/^Saved |^Done in |^✓/.test(line)) return 'g';
  if (line.startsWith('$ ')) return 'w';
  return '';
}

function describe(kind) {
  return { aab: 'for Google Play', apk: 'sideload & testing', zip: 'full Flutter project' }[kind] || kind;
}

/* ── destructive dialogs ─────────────────────────────────────────────── */

function duplicateDialog(app) {
  const name = el('input', { class: 'input', value: app.name + ' copy' });
  const run = async () => {
    try {
      const { app: copy } = await api('POST', `/api/apps/${app.id}/duplicate`, { name: name.value });
      close();
      go('#/app/' + copy.id);
      toast(`Created ${copy.name}`);
    } catch (error) { toast(error.message, true); }
  };
  const close = openModal('Duplicate app',
    'Copies the settings and icon. The signing key and build history stay with the original.',
    [field('New name', name)],
    [
      el('button', { class: 'btn ghost', text: 'Cancel', onclick: () => close() }),
      el('button', { class: 'btn primary', text: 'Duplicate', onclick: run }),
    ]);
}

function deleteDialog(app) {
  const confirmInput = el('input', { class: 'input', placeholder: app.name });
  const run = async () => {
    if (confirmInput.value.trim() !== app.name) {
      toast('Type the app name exactly to confirm.', true);
      return;
    }
    try {
      await api('DELETE', '/api/apps/' + app.id);
      close();
      go('#/');
      toast(`Deleted ${app.name}`);
    } catch (error) { toast(error.message, true); }
  };
  const close = openModal('Delete this app?',
    'Removes the configuration, the generated project and every build. This cannot be undone.',
    [field(`Type "${app.name}" to confirm`, confirmInput)],
    [
      el('button', { class: 'btn ghost', text: 'Cancel', onclick: () => close() }),
      el('button', { class: 'btn danger', text: 'Delete', onclick: run }),
    ]);
}

/* ── routing ─────────────────────────────────────────────────────────── */

function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

async function route() {
  const hash = location.hash || '#/';
  try {
    const match = hash.match(/^#\/app\/([^/]+)$/);
    if (match) await showApp(decodeURIComponent(match[1]));
    else await showList();
  } catch (error) {
    document.getElementById('crumb').textContent = 'Problem';
    clear(document.getElementById('content')).append(
      el('div', { class: 'banner err' }, [
        el('b', { text: error.message }), error.detail || '',
      ]),
      el('button', { class: 'btn', text: 'Back to all apps', onclick: () => go('#/') }),
    );
  }
}

function hostOf(url) {
  try { return new URL(url).host; } catch { return url || ''; }
}

function shortDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

function megabytes(bytes) {
  return bytes > 1048576
    ? `${(bytes / 1048576).toFixed(1)} MB`
    : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

window.addEventListener('hashchange', route);
window.addEventListener('beforeunload', (event) => {
  if (state.dirty) event.preventDefault();
});

route();
refreshHealth();
setInterval(refreshHealth, 60000);
