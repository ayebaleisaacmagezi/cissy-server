'use strict';

/* Cissy Build — browser client.
 *
 * No framework and no build step: the whole UI is this file, and editing it on
 * the server means a refresh rather than a toolchain.
 *
 * DOM is built with el() rather than innerHTML throughout. App names and URLs
 * come from user input and end up in the page, so string-interpolated HTML
 * would be an injection waiting to happen.
 */

const FEATURES = [
  ['File upload', 'Let the site open the file picker'],
  ['Downloads', 'Save files to the device and open them'],
  ['Native sharing', "Use the phone's share sheet"],
  ['Pull to refresh', 'Swipe down to reload'],
  ['Camera', 'Needs a permission prompt'],
  ['Location', 'Needs a permission prompt'],
  ['Deep links', 'Open the app from links to your domain'],
];

const state = {
  apps: [],
  app: null,
  dirty: false,
  health: null,
  password: localStorage.getItem('cissy-password') || '',
};

/* ── tiny DOM helper ─────────────────────────────────────────────────── */

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
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

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (state.password) options.headers['X-Cissy-Password'] = state.password;
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }

  const response = await fetch(path, options);
  if (response.status === 401) {
    // A password we already held and that was rejected is worth saying so about,
    // rather than silently reopening an identical-looking prompt.
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

function toast(message, bad = false) {
  const node = document.getElementById('toast');
  node.textContent = message;
  node.className = bad ? 'bad' : '';
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, bad ? 6000 : 2800);
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

/* The page fires several requests at once on load, so a wrong or missing
 * password produces several 401s at the same moment. They must all wait on one
 * prompt: opening a dialog per request strands every request but the one the
 * user happens to answer. */
let passwordPrompt = null;

function askForPassword(wasWrong = false) {
  if (passwordPrompt) return passwordPrompt;

  passwordPrompt = new Promise((resolve) => {
    const input = el('input', {
      class: 'input',
      type: 'password',
      placeholder: 'Server password',
      value: '',
    });
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
  } catch (error) {
    clear(foot).append(el('div', { class: 'ln bad', text: 'Server unreachable' }));
    return;
  }

  const health = state.health;
  clear(foot);
  foot.append(
    el('div', { class: 'ln ' + (health.ok ? 'good' : 'bad') }, [
      el('i', { class: 'dot' }),
      health.ok ? 'Toolchain ready' : 'Toolchain not ready',
    ]),
  );
  for (const tool of health.tools) {
    foot.append(
      el('div', { class: 'ln' }, [
        tool.ok ? `${tool.name} ${tool.version}` : `${tool.name} — ${tool.detail}`,
      ]),
    );
  }
}

/* ── sidebar ─────────────────────────────────────────────────────────── */

const SECTIONS = [
  ['identity', 'Identity', '▣'],
  ['webview', 'WebView', '◎'],
  ['features', 'Features', '⊞'],
  ['offline', 'Offline', '☁'],
  ['signing', 'Signing', '⚿'],
  ['build', 'Build', '▶'],
];

function renderSidebar() {
  const nav = clear(document.getElementById('side-nav'));

  if (!state.app) {
    nav.append(
      el('div', { class: 'nav-group' }, [
        el('button', {
          class: 'nav-item active',
          onclick: () => go('#/'),
        }, [el('span', { class: 'gl', text: '▦' }), 'All apps',
            el('span', { class: 'badge', text: String(state.apps.length) })]),
        el('button', {
          class: 'nav-item',
          onclick: newAppDialog,
        }, [el('span', { class: 'gl', text: '＋' }), 'New app']),
      ]),
    );
    return;
  }

  nav.append(
    el('button', { class: 'appswitch', onclick: () => go('#/') }, [
      el('span', { class: 'ico' }),
      el('span', { class: 'nm', text: state.app.name }),
      el('span', { class: 'car', text: '⌄' }),
    ]),
    el('div', { class: 'nav-group' }, [
      el('div', { class: 'nav-label', text: 'Configure' }),
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
  const actions = clear(document.getElementById('topbar-actions'));
  actions.append(el('button', { class: 'btn primary', text: '+ New app', onclick: newAppDialog }));

  renderSidebar();
  const content = clear(document.getElementById('content'));

  if (!apps.length) {
    content.append(
      el('div', { class: 'empty' }, [
        el('h3', { text: 'No apps yet' }),
        el('p', { text: 'Point Cissy at a website and it will build an Android app from it.' }),
        el('button', { class: 'btn primary', text: '+ New app', onclick: newAppDialog }),
      ]),
    );
    return;
  }

  const rows = apps.map((app) =>
    el('tr', { class: 'row', onclick: () => go('#/app/' + app.id) }, [
      el('td', {}, [el('span', { class: 'ico' }), el('span', { class: 'app-name', text: app.name })]),
      el('td', { class: 'app-url mono', text: hostOf(app.website_url) }),
      el('td', {}, [
        el('div', { text: `v${app.version_name} (${app.version_code})` }),
        el('div', { class: 'app-url', text: 'Edited ' + shortDate(app.updated_at) }),
      ]),
      el('td', {}, [signingPill(app)]),
    ]),
  );

  content.append(
    el('h2', { class: 'sec', text: 'Your apps' }),
    el('p', { class: 'sub', text: `${apps.length} on this server` }),
    el('table', { class: 'apps' }, [
      el('thead', {}, [
        el('tr', {}, ['App', 'Website', 'Version', 'Signing'].map((h) => el('th', { text: h }))),
      ]),
      el('tbody', {}, rows),
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

  // The package id is permanent once the app is on Play, so suggest one rather
  // than generating it silently.
  name.addEventListener('input', () => {
    if (pkg.dataset.touched) return;
    const slug = name.value.trim().toLowerCase().replace(/[^a-z0-9]+/g, '');
    pkg.value = slug ? `com.cissytech.${slug}` : '';
  });
  pkg.addEventListener('input', () => { pkg.dataset.touched = '1'; });

  const create = async () => {
    try {
      const { app } = await api('POST', '/api/apps', {
        name: name.value,
        website_url: url.value,
        android_package_id: pkg.value,
      });
      close();
      go('#/app/' + app.id);
      toast(`Created ${app.name}`);
    } catch (error) {
      toast(error.message, true);
    }
  };

  const close = openModal(
    'New app',
    'You can change all of this later — except the package ID, once it is on Play.',
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
    el('label', { text: label }),
    input,
    hint ? el('p', { class: 'hint', text: hint }) : null,
  ]);
}

/* ── app editor ──────────────────────────────────────────────────────── */

async function showApp(appId) {
  const { app } = await api('GET', '/api/apps/' + appId);
  state.app = app;
  state.dirty = false;

  document.getElementById('crumb').textContent = app.name;
  renderSidebar();
  renderTopbarActions();

  const content = clear(document.getElementById('content'));
  const draft = { ...app };

  const markDirty = () => {
    state.dirty = true;
    renderTopbarActions();
    renderSummary();
  };

  const bind = (key, input, transform = (v) => v) => {
    input.addEventListener('input', () => {
      draft[key] = transform(input.value);
      markDirty();
    });
    return input;
  };

  /* identity */
  const nameInput = bind('name', el('input', { class: 'input', value: app.name }));
  const appNameInput = bind('app_name', el('input', { class: 'input', value: app.app_name || app.name }));
  const pkgInput = el('input', { class: 'input mono', value: app.android_package_id, disabled: true });
  const bundleInput = bind('ios_bundle_id', el('input', { class: 'input mono', value: app.ios_bundle_id }));

  /* webview */
  const urlInput = bind('website_url', el('input', { class: 'input mono', value: app.website_url }));
  const domainsInput = bind(
    'allowed_domains',
    el('input', { class: 'input mono', value: (app.allowed_domains || []).join(', ') }),
    (v) => v.split(',').map((s) => s.trim()).filter(Boolean),
  );
  const uaInput = bind(
    'custom_user_agent',
    el('input', { class: 'input mono', value: app.custom_user_agent || '', placeholder: 'Leave blank for the default' }),
    (v) => v.trim() || null,
  );
  const httpsInput = checkbox('Require HTTPS', app.require_https, (on) => {
    draft.require_https = on;
    markDirty();
  });
  const externalSelect = el('select', { class: 'input' },
    [['browser', "Open in the phone's browser"], ['webview', 'Stay inside the app'], ['block', 'Block them']]
      .map(([value, label]) => el('option', { value, text: label, selected: app.external_link_behavior === value })),
  );
  externalSelect.addEventListener('change', () => {
    draft.external_link_behavior = externalSelect.value;
    markDirty();
  });

  /* features */
  const featureSet = new Set(app.features || []);
  const featureNodes = FEATURES.map(([name, hint]) =>
    el('label', { class: 'check' }, [
      el('input', {
        type: 'checkbox',
        checked: featureSet.has(name),
        onchange: (event) => {
          if (event.target.checked) featureSet.add(name); else featureSet.delete(name);
          draft.features = [...featureSet];
          markDirty();
        },
      }),
      el('span', {}, [name, el('div', { class: 'hint', text: hint })]),
    ]),
  );

  /* offline */
  const cacheInput = checkbox('Cache pages for faster loading', app.cache_enabled, (on) => {
    draft.cache_enabled = on;
    markDirty();
  });
  const offlineInput = checkbox('Show a branded screen when a page fails to load', app.offline_fallback_enabled, (on) => {
    draft.offline_fallback_enabled = on;
    markDirty();
  });

  /* version */
  const versionNameInput = bind('version_name', el('input', { class: 'input mono', value: app.version_name }));

  content.append(
    fieldset('sec-identity', 'Identity', [
      field('Project name', nameInput, 'How it appears in this list.'),
      field('App name', appNameInput, "Shown under the icon on the phone."),
      el('div', { class: 'row2' }, [
        field('Android package ID', pkgInput, 'Permanent once published — create a new app to change it.'),
        field('iOS bundle ID', bundleInput),
      ]),
    ]),
    fieldset('sec-webview', 'WebView', [
      field('Website URL', urlInput, 'The page the app opens on launch.'),
      field('Allowed domains', domainsInput, 'Comma separated. Links elsewhere follow the rule below.'),
      field('Links outside those domains', externalSelect),
      field('Custom user agent', uaInput),
      httpsInput,
    ]),
    fieldset('sec-features', 'Features', [
      el('div', { class: 'checks' }, featureNodes),
    ]),
    fieldset('sec-offline', 'Offline', [cacheInput, offlineInput]),
    fieldset('sec-signing', 'Signing', [signingSection(app)]),
    fieldset('sec-build', 'Build', [buildSection(app)]),
    field('Version name', versionNameInput, `Next build will be version code ${app.version_code + 1}.`),
  );

  /* summary rail */
  const summary = el('aside', { class: 'summary' });
  const layout = el('div', { class: 'cols' }, [
    el('div', {}, [...content.childNodes]),
    summary,
  ]);
  clear(content).append(layout);

  function renderSummary() {
    clear(summary).append(
      el('h4', { text: 'Summary' }),
      kv('Website', hostOf(draft.website_url)),
      kv('Package', draft.android_package_id),
      kv('Features', String((draft.features || []).length) + ' enabled'),
      kv('Next version', `${draft.version_name} (${app.version_code + 1})`),
      kv('Signing', app.keystore_file ? 'Upload key' : 'Debug key'),
      el('button', {
        class: 'btn primary',
        style: 'width:100%;justify-content:center;margin-top:14px',
        text: state.dirty ? 'Save changes' : 'Saved',
        disabled: !state.dirty,
        onclick: save,
      }),
      el('button', {
        class: 'btn',
        style: 'width:100%;justify-content:center;margin-top:8px',
        text: 'Duplicate as new app',
        onclick: () => duplicateDialog(app),
      }),
      el('button', {
        class: 'btn danger sm',
        style: 'width:100%;justify-content:center;margin-top:8px',
        text: 'Delete app',
        onclick: () => deleteDialog(app),
      }),
    );
  }

  async function save() {
    try {
      const { app: saved } = await api('PUT', '/api/apps/' + app.id, draft);
      state.app = saved;
      state.dirty = false;
      document.getElementById('crumb').textContent = saved.name;
      renderSidebar();
      renderTopbarActions();
      renderSummary();
      toast('Saved');
    } catch (error) {
      toast(error.message, true);
    }
  }

  function renderTopbarActions() {
    const actions = clear(document.getElementById('topbar-actions'));
    actions.append(
      el('span', { class: 'pill' + (state.dirty ? ' warn' : ''), text: state.dirty ? 'Unsaved changes' : 'Saved' }),
      el('button', { class: 'btn primary', text: 'Save', disabled: !state.dirty, onclick: save }),
    );
  }

  renderSummary();
}

function fieldset(id, legend, children) {
  return el('fieldset', { class: 'group', id }, [el('legend', { text: legend }), ...children]);
}

function checkbox(label, checked, onchange) {
  return el('label', { class: 'check', style: 'margin-bottom:14px' }, [
    el('input', { type: 'checkbox', checked, onchange: (e) => onchange(e.target.checked) }),
    label,
  ]);
}

function kv(key, value) {
  return el('div', { class: 'kv' }, [el('span', { text: key }), el('span', { text: value || '—' })]);
}

function signingSection(app) {
  if (app.keystore_file && app.key_alias) {
    return el('div', { class: 'banner ok' }, [
      el('b', { text: `Signing with ${app.keystore_file}` }),
      `Key alias "${app.key_alias}". Passwords are entered per build and never stored.`,
    ]);
  }
  return el('div', { class: 'banner warn' }, [
    el('b', { text: 'No upload keystore yet' }),
    'Builds will be signed with a debug key, which Google Play rejects. ' +
    'Keystore upload arrives with the build step.',
  ]);
}

function buildSection(app) {
  // Honest placeholder rather than a button that does nothing: generating and
  // building are phases 3 and 4, and the API for them does not exist yet.
  return el('div', { class: 'banner info' }, [
    el('b', { text: 'Building is not wired up yet' }),
    'The server can store and validate this configuration. Generating the ' +
    'Flutter project and running the build come next.',
  ]);
}

function duplicateDialog(app) {
  const name = el('input', { class: 'input', value: app.name + ' copy' });
  const run = async () => {
    try {
      const { app: copy } = await api('POST', `/api/apps/${app.id}/duplicate`, { name: name.value });
      close();
      go('#/app/' + copy.id);
      toast(`Created ${copy.name}`);
    } catch (error) {
      toast(error.message, true);
    }
  };
  const close = openModal(
    'Duplicate app',
    'Copies the settings and icon. The signing key and build history stay with the original.',
    [field('New name', name)],
    [
      el('button', { class: 'btn ghost', text: 'Cancel', onclick: () => close() }),
      el('button', { class: 'btn primary', text: 'Duplicate', onclick: run }),
    ],
  );
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
    } catch (error) {
      toast(error.message, true);
    }
  };
  const close = openModal(
    'Delete this app?',
    'Removes the configuration, the generated project and every build. This cannot be undone.',
    [field(`Type "${app.name}" to confirm`, confirmInput)],
    [
      el('button', { class: 'btn ghost', text: 'Cancel', onclick: () => close() }),
      el('button', { class: 'btn danger', text: 'Delete', onclick: run }),
    ],
  );
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
        el('b', { text: error.message }),
        error.detail || '',
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

window.addEventListener('hashchange', route);
window.addEventListener('beforeunload', (event) => {
  if (state.dirty) event.preventDefault();
});

route();
refreshHealth();
setInterval(refreshHealth, 60000);
