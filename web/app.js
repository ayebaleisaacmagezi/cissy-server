'use strict';

/* Cissyweb2app — browser client.
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
  user: null,          // whoever is signed in, from /api/auth/session
  demoSms: false,      // true when codes are simulated rather than texted
  pending: null,       // a phone part-way through signup
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

/* Material Symbols Outlined names. The font renders the element's text as a
 * ligature, so the name below IS the icon on screen. */
const ICONS = {
  apps: 'grid_view',
  plus: 'add',
  billing: 'credit_card',
  admin: 'admin_panel_settings',
  overview: 'home',
  identity: 'person',
  webview: 'language',
  branding: 'image',
  features: 'tune',
  offline: 'cloud_off',
  signing: 'key',
  build: 'play_arrow',
  chevron: 'expand_more',
};

function icon(name, cls = 'gl') {
  return el('span', {
    class: cls + ' material-symbols-outlined',
    'aria-hidden': 'true',
    text: ICONS[name] || name,
  });
}

/* The brand-blue square that stands in for an app icon, carrying the app's
 * initial so a list of them is tellable apart. */
function appIcon(name) {
  const letter = (name || '').trim().charAt(0).toUpperCase();
  return el('span', { class: 'ico', text: letter });
}

/* ── api ─────────────────────────────────────────────────────────────── */

/* Nothing to attach any more. The session is an HttpOnly cookie the browser
 * sends on its own, which is also what makes artifact downloads work: a plain
 * <a href> carries the cookie where it could never carry a custom header. */
function authHeaders(extra = {}) {
  return { ...extra };
}

async function api(method, path, body) {
  const options = { method, headers: authHeaders() };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }

  const response = await fetch(path, options);
  if (response.status === 401) {
    // The session went away, so stop pretending otherwise and send them to the
    // sign-in screen rather than retrying into the same wall.
    state.user = null;
    if (!location.hash.startsWith('#/login')) go('#/login');
    throw new Error('Sign in to continue.');
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

/* ── who is signed in ─────────────────────────────────────────────────── */

/* Public on purpose: it answers null rather than 401 when nobody is signed in,
 * so the first thing the page does cannot put an error in front of a visitor
 * who has simply not signed in yet. */
async function loadSession() {
  try {
    const data = await api('GET', '/api/auth/session');
    state.user = data.user;
    state.demoSms = Boolean(data.demo_sms);
  } catch {
    state.user = null;
  }
  return state.user;
}

async function logout() {
  try { await api('POST', '/api/auth/logout'); } catch { /* leaving anyway */ }
  state.user = null;
  state.app = null;
  location.href = '/';
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


/* ── signing up and signing in ─────────────────────────────────────────────
 *
 * These are the only screens that render without the app shell around them,
 * because somebody who is not signed in has no sidebar to put anything in.
 * `authScreen` hides the chrome; every other screen puts it back.
 */

function authScreen(title, subtitle, rows, footer) {
  document.body.classList.add('signed-out');
  document.getElementById('crumb').textContent = '';
  clear(document.getElementById('side-nav'));
  clear(document.getElementById('topbar-actions'));

  const content = clear(document.getElementById('content'));
  content.append(
    el('div', { class: 'authwrap' }, [
      el('div', { class: 'authcard' }, [
        el('div', { class: 'authbrand' }, [
          'Cissy', el('span', { text: 'Web2App' }),
        ]),
        el('h2', { class: 'authtitle', text: title }),
        subtitle ? el('p', { class: 'authsub', text: subtitle }) : null,
        ...rows,
        footer ? el('p', { class: 'authfoot' }, footer) : null,
      ]),
    ]),
  );
  const first = content.querySelector('input');
  if (first) first.focus();
  return content;
}

function authLink(label, hash) {
  return el('a', {
    class: 'authlink', text: label, href: hash,
    onclick: (e) => { e.preventDefault(); go(hash); },
  });
}

/* Submitting has to work from the keyboard, and a form that reloads the page
 * would lose everything, so Enter is wired explicitly. */
function onEnter(input, run) {
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); });
  return input;
}

function authError(box, message) {
  clear(box).append(el('div', { class: 'banner err' }, [message]));
}

async function showSignup() {
  const name = el('input', { class: 'input', placeholder: 'Your name', autocomplete: 'name' });
  const phone = el('input', { class: 'input mono', placeholder: '07XX 000 000', inputmode: 'tel' });
  const pass = el('input', { class: 'input', type: 'password', placeholder: 'At least 8 characters', autocomplete: 'new-password' });
  const problem = el('div', {});
  const button = el('button', { class: 'btn primary full', text: 'Create account' });

  const submit = async () => {
    button.disabled = true;
    clear(problem);
    try {
      const data = await api('POST', '/api/auth/signup', {
        name: name.value, phone: phone.value, password: pass.value,
      });
      state.pending = { phone: data.phone, code: data.code || '', demo: data.demo };
      go('#/verify');
    } catch (error) {
      authError(problem, error.message);
      button.disabled = false;
    }
  };
  button.addEventListener('click', submit);
  for (const input of [name, phone, pass]) onEnter(input, submit);

  authScreen(
    'Create your account',
    'Free. Three builds to try it with, and no card.',
    [
      problem,
      field('Your name', name),
      field('Phone number', phone,
        'We send a code to confirm it. This is also the number you will pay from.'),
      field('Password', pass),
      button,
    ],
    ['Already have one? ', authLink('Log in', '#/login')],
  );
}

async function showVerify() {
  if (!state.pending) { go('#/signup'); return; }
  const { phone } = state.pending;

  const code = el('input', {
    class: 'input mono code', placeholder: '000000', inputmode: 'numeric', maxlength: 6,
  });
  const problem = el('div', {});
  const button = el('button', { class: 'btn primary full', text: 'Confirm' });

  const submit = async () => {
    button.disabled = true;
    clear(problem);
    try {
      const data = await api('POST', '/api/auth/verify', { phone, code: code.value });
      state.user = data.user;
      state.pending = null;
      toast('Welcome to CissyWeb2App');
      go('#/');
    } catch (error) {
      authError(problem, error.message);
      button.disabled = false;
      code.select();
    }
  };
  button.addEventListener('click', submit);
  onEnter(code, submit);

  const resend = el('button', {
    class: 'btn full', text: 'Send another code',
    onclick: async () => {
      resend.disabled = true;
      try {
        const data = await api('POST', '/api/auth/resend', { phone });
        state.pending = { ...state.pending, code: data.code || '' };
        toast('A new code is on its way');
        go('#/verify');
      } catch (error) {
        authError(problem, error.message);
      }
      resend.disabled = false;
    },
  });

  authScreen(
    'Check your phone',
    `We sent a 6-digit code to ${phone}.`,
    [
      problem,
      // Demo mode only. The server decides this, and a live one never sends
      // the code back down the same channel it is verifying.
      state.pending.demo && state.pending.code
        ? el('div', { class: 'banner info demo-code' }, [
            el('b', { text: 'Demo mode, nothing was texted' }),
            'Your code is ',
            el('code', { text: state.pending.code }),
          ])
        : null,
      field('Code', code),
      button,
      resend,
    ],
    ['Wrong number? ', authLink('Start again', '#/signup')],
  );
}

async function showLogin() {
  const phone = el('input', { class: 'input mono', placeholder: '07XX 000 000', inputmode: 'tel' });
  const pass = el('input', { class: 'input', type: 'password', placeholder: 'Your password', autocomplete: 'current-password' });
  const problem = el('div', {});
  const button = el('button', { class: 'btn primary full', text: 'Log in' });

  const submit = async () => {
    button.disabled = true;
    clear(problem);
    try {
      const data = await api('POST', '/api/auth/login', {
        phone: phone.value, password: pass.value,
      });
      state.user = data.user;
      go('#/');
    } catch (error) {
      authError(problem, error.message);
      button.disabled = false;
    }
  };
  button.addEventListener('click', submit);
  for (const input of [phone, pass]) onEnter(input, submit);

  authScreen(
    'Welcome back',
    'Log in to your apps and builds.',
    [problem, field('Phone number', phone), field('Password', pass), button],
    ['New here? ', authLink('Create an account', '#/signup')],
  );
}

/* ── sidebar ─────────────────────────────────────────────────────────── */

const SECTIONS = [
  ['overview', 'Overview', 'overview'],
  ['identity', 'Identity', 'identity'],
  ['webview', 'WebView', 'webview'],
  ['branding', 'Branding', 'branding'],
  ['features', 'Features', 'features'],
  ['offline', 'Offline', 'offline'],
  ['signing', 'Signing', 'signing'],
  ['build', 'Build', 'build'],
];

function planCard() {
  const user = state.user;
  if (!user) return null;
  const left = user.builds_left;
  const total = user.builds_limit;
  const share = total ? Math.max(0, Math.min(100, (left / total) * 100)) : 0;
  const tone = left === 0 ? 'out' : left <= 1 ? 'low' : '';

  return el('div', { class: 'plancard ' + tone }, [
    el('b', { text: user.plan === 'trial' ? 'Free trial' : user.plan_name || 'Your plan' }),
    el('div', { class: 'mut', text: `${left} build${left === 1 ? '' : 's'} left of ${total}` }),
    el('div', { class: 'meter' }, [el('i', { style: `width:${share}%` })]),
    el('button', {
      class: 'btn sm full',
      text: left === 0 ? 'Choose a plan' : 'See plans',
      onclick: () => go('#/billing'),
    }),
  ]);
}

/* The avatar in the corner. Rendered from renderSidebar so every screen that
 * redraws the chrome gets it, rather than each one remembering to. */
function renderAccount() {
  const box = clear(document.getElementById('account'));
  if (!state.user) return;
  const initials = state.user.name.trim().split(/\s+/).slice(0, 2)
    .map((part) => part[0]).join('').toUpperCase();

  box.append(el('button', {
    class: 'avatar', text: initials, title: state.user.name,
    onclick: () => openModal(state.user.name, state.user.phone, [
      kv('Plan', state.user.plan === 'trial' ? 'Free trial' : state.user.plan),
      kv('Builds left', `${state.user.builds_left} of ${state.user.builds_limit}`),
      state.user.plan_until ? kv('Renews', shortDate(state.user.plan_until)) : null,
    ].filter(Boolean), [
      el('button', { class: 'btn', text: 'Log out', onclick: logout }),
      el('button', { class: 'btn primary', text: 'Billing', onclick: () => go('#/billing') }),
    ]),
  }));
}

function renderSidebar() {
  renderAccount();
  const nav = clear(document.getElementById('side-nav'));

  const onBilling = location.hash.startsWith('#/billing');

  if (!state.app) {
    nav.append(el('div', { class: 'nav-group' }, [
      el('button', { class: 'nav-item' + (onBilling ? '' : ' active'), onclick: () => go('#/') }, [
        icon('apps'), 'All apps',
        el('span', { class: 'badge', text: String(state.apps.length) }),
      ]),
      el('button', { class: 'nav-item', onclick: newAppDialog }, [
        icon('plus'), 'New app',
      ]),
      el('button', { class: 'nav-item' + (onBilling ? ' active' : ''), onclick: () => go('#/billing') }, [
        icon('billing'), 'Billing',
      ]),
      state.user && state.user.is_admin
        ? el('button', { class: 'nav-item', onclick: () => go('#/admin') }, [
            icon('admin'), 'Admin',
          ])
        : null,
    ]));
    nav.append(planCard());
    return;
  }

  nav.append(
    el('button', { class: 'appswitch', onclick: () => go('#/') }, [
      appIcon(state.app.name),
      el('span', { class: 'nm', text: state.app.name }),
      icon('chevron', 'car'),
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
        }, [icon(glyph), label]),
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
          el('td', {}, [appIcon(app.name), el('span', { class: 'app-name', text: app.name })]),
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

  // The build outlives the page, so arriving here mid-build must not look like
  // nothing is happening.
  const running = builds.find((b) => b.status === 'running');
  if (running) {
    children.push(el('div', { class: 'banner info' }, [
      el('b', { text: `Build #${running.number} is running` }),
      'It carries on whether or not this page is open.',
      el('button', {
        class: 'btn sm', style: 'margin-top:10px', text: 'Watch it',
        onclick: () => go(`#/app/${app.id}/build/${running.number}`),
      }),
    ]));
  }

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

  return el('tr', {
    class: 'row',
    onclick: (event) => {
      // Let the download links do their own job.
      if (event.target.closest('a')) return;
      go(`#/app/${app.id}/build/${build.number}`);
    },
  }, [
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
      go(`#/app/${app.id}/build/${build.number}`);
    } catch (error) { toast(error.message, true); }
  };

  const close = openModal('Build ' + app.name, null, body, [
    el('button', { class: 'btn ghost', text: 'Cancel', onclick: () => close() }),
    el('button', { class: 'btn primary', text: 'Start build', onclick: start }),
  ]);
}

/* Reattaching matters because the build outlives the page. It runs in a thread
 * on the server, so a reload, a closed tab or a dropped connection leaves it
 * running — and without a URL of its own there was no way back to it. */
async function showBuild(appId, number) {
  const [{ app }, { build }] = await Promise.all([
    api('GET', '/api/apps/' + appId),
    api('GET', `/api/apps/${appId}/builds/${number}`),
  ]);

  state.app = app;
  state.dirty = false;
  renderSidebar();
  document.getElementById('crumb').textContent = `${app.name} — build #${build.number}`;
  clear(document.getElementById('topbar-actions')).append(
    el('button', { class: 'btn', text: 'Back to app', onclick: () => go('#/app/' + app.id) }),
  );

  const status = el('span', { class: 'pill' }, [el('i', { class: 'dot' }), 'Loading…']);
  const consoleBox = el('div', { class: 'console mono' });
  const result = el('div');

  clear(document.getElementById('content')).append(
    el('div', { style: 'display:flex;align-items:center;gap:12px;margin-bottom:14px' }, [
      el('h2', { class: 'sec', style: 'margin:0', text: 'Build #' + build.number }),
      status,
    ]),
    result,
    consoleBox,
  );

  let pinned = true;
  consoleBox.addEventListener('scroll', () => {
    // Stop yanking the view back if the user has scrolled up to read something.
    pinned = consoleBox.scrollHeight - consoleBox.scrollTop - consoleBox.clientHeight < 40;
  });

  const append = (line) => {
    consoleBox.append(el('div', { class: 'l ' + lineClass(line), text: line }));
    if (pinned) consoleBox.scrollTop = consoleBox.scrollHeight;
  };

  if (build.status === 'running') {
    status.className = 'pill';
    clear(status).append(el('i', { class: 'dot' }), 'Building…');

    let delivered = 0;
    let polling = false;

    // The stream replays everything already logged before going live, so a
    // browser arriving late still sees the whole build.
    readEvents(`/api/apps/${appId}/builds/${number}/events`, {
      onLine: (line) => { if (!polling) { delivered += 1; append(line); } },
      onDone: (finished) => { if (!polling) finishBuild(app, finished, status, result); },
    }).catch(() => {
      if (!polling) {
        status.className = 'pill err';
        clear(status).append(el('i', { class: 'dot' }), 'Lost connection — the build carries on');
      }
    });

    // Streaming has more places to go wrong than the rest of this put
    // together — a proxy that buffers, a browser that waits for a full buffer
    // before releasing the first byte. Rather than depend on all of them
    // behaving, fall back to asking for the log outright. Slower, but it
    // cannot silently show an empty console while a build is running.
    setTimeout(() => {
      if (delivered > 0 || polling) return;
      polling = true;
      pollBuildLog(app, number, { status, result, consoleBox, append });
    }, 5000);
    return;
  }

  // Already finished. The event stream only carries live builds, so the log
  // comes from what was written to disk.
  try {
    const { lines } = await api('GET', `/api/apps/${appId}/builds/${number}/log`);
    lines.forEach(append);
  } catch {
    append('The log for this build was not kept.');
  }
  finishBuild(app, build, status, result);
}

/* Fallback for when the event stream delivers nothing: ask for the log on a
 * timer instead. Only reached if five seconds pass with no line at all. */
async function pollBuildLog(app, number, { status, result, consoleBox, append }) {
  clear(consoleBox);
  let shown = 0;

  while (true) {
    try {
      const { lines } = await api('GET', `/api/apps/${app.id}/builds/${number}/log`);
      for (let i = shown; i < lines.length; i += 1) append(lines[i]);
      shown = lines.length;

      const { build } = await api('GET', `/api/apps/${app.id}/builds/${number}`);
      if (build.status !== 'running') {
        finishBuild(app, build, status, result);
        return;
      }
    } catch (error) {
      status.className = 'pill err';
      clear(status).append(el('i', { class: 'dot' }), 'Lost track of the build');
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
}

function finishBuild(app, finished, status, result) {
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
          el('div', { class: 'meta', text: `${megabytes(artifact.size)} · ${describe(artifact.kind, artifact.name)}` }),
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
}

function lineClass(line) {
  if (/^ERROR|FAILURE|error:/i.test(line)) return 'r';
  if (/^WARNING|^Note:|warning:/i.test(line)) return 'y';
  if (/^Saved |^Done in |^✓/.test(line)) return 'g';
  if (line.startsWith('$ ')) return 'w';
  return '';
}

function describe(kind, name = '') {
  if (kind === 'apk') {
    // Split builds produce one APK per architecture, and picking the wrong one
    // fails to install with a message that explains nothing.
    if (name.includes('arm64-v8a')) return 'most phones — start here';
    if (name.includes('armeabi-v7a')) return 'older 32-bit phones';
    if (name.includes('x86_64')) return 'emulators';
    return 'sideload & testing';
  }
  return { aab: 'for Google Play', zip: 'full Flutter project' }[kind] || kind;
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

/* ── billing ─────────────────────────────────────────────────────────────
 *
 * Collecto has no webhook, so nothing here is push. The customer approves on
 * their handset and the server finds out by asking. This page therefore only
 * ever reads: it polls a record the server owns, and the payment would finish
 * exactly the same way with this tab closed. That is the property worth
 * noticing while watching it.
 */

let billingTimer = null;

function stopBillingPoll() {
  if (billingTimer) clearInterval(billingTimer);
  billingTimer = null;
}

async function showBilling() {
  state.app = null;
  const data = await api('GET', '/api/billing');

  document.getElementById('crumb').textContent = 'Billing';
  clear(document.getElementById('topbar-actions'));
  renderSidebar();
  const content = clear(document.getElementById('content'));

  content.append(el('h2', { class: 'sec', text: 'Plan and payments' }));

  if (data.mode === 'demo') {
    content.append(el('div', { class: 'banner warn' }, [
      el('b', { text: 'Demo mode — no money moves' }),
      'There is no Collecto account configured, so payments run against a ' +
      'simulator that answers like the real one: it can approve, decline, stall, ' +
      'drop a connection or return rubbish. Set CISSY_COLLECTO_USERNAME and ' +
      'CISSY_COLLECTO_KEY to go live.',
    ]));
  }

  content.append(subscriptionCard(data.user));

  content.append(
    el('h2', { class: 'sec', text: 'Plans' }),
    el('div', { class: 'plans' }, data.plans.map((plan) =>
      el('div', { class: 'plan' }, [
        el('div', { class: 'plan-name', text: plan.name }),
        el('div', { class: 'plan-price', text: money(plan.amount) }),
        el('div', { class: 'plan-blurb', text: plan.blurb }),
        el('button', {
          class: 'btn primary full',
          text: 'Pay with mobile money',
          onclick: () => payDialog(plan, data.mode),
        }),
      ]))),
  );

  if (data.payments.length) {
    content.append(
      el('h2', { class: 'sec', text: 'Payments' }),
      el('table', { class: 'apps' }, [
        el('thead', {}, [el('tr', {},
          ['Reference', 'Plan', 'Amount', 'When', 'Status'].map((h) => el('th', { text: h })))]),
        el('tbody', {}, data.payments.map((payment) =>
          el('tr', { class: 'row', onclick: () => go('#/billing/pay/' + payment.reference) }, [
            el('td', { class: 'app-url mono', text: payment.reference }),
            el('td', { text: payment.plan }),
            el('td', { text: money(payment.amount) }),
            el('td', { class: 'app-url', text: shortDate(payment.created_at) }),
            el('td', {}, [paymentPill(payment.status)]),
          ]))),
      ]),
    );
  }
}

function subscriptionCard(user) {
  if (!user) return el('div', {});
  const left = `${user.builds_left} of ${user.builds_limit} builds left`;
  if (user.plan === 'trial') {
    return el('div', { class: 'banner info' }, [
      el('b', { text: 'Free trial' }),
      `${left}. Pick a plan below when you need more — you pay from your phone ` +
      'and the plan comes on as soon as the payment lands.',
    ]);
  }
  if (user.plan_expired) {
    return el('div', { class: 'banner warn' }, [
      el('b', { text: 'Your plan has ended' }),
      'Everything you built stays downloadable. Pay again below to keep building.',
    ]);
  }
  return el('div', { class: 'banner ok' }, [
    el('b', { text: `${user.plan} — active` }),
    `${left}` + (user.plan_until ? `, renews by ${shortDate(user.plan_until)}.` : '.'),
  ]);
}

function paymentPill(status) {
  const look = { successful: 'ok', failed: 'err', abandoned: 'warn' }[status] || 'warn';
  const label = { successful: 'Paid', failed: 'Failed', abandoned: 'Not approved' }[status]
    || 'Waiting';
  return el('span', { class: 'pill ' + look }, [el('i', { class: 'dot' }), label]);
}

function payDialog(plan, mode) {
  const phone = el('input', { class: 'input mono', placeholder: '07XX 000 000' });
  const scenario = el('select', { class: 'input' }, [
    el('option', { value: 'approve', text: 'They approve it' }),
    el('option', { value: 'decline', text: 'They decline it' }),
    el('option', { value: 'silent', text: 'They never touch the prompt' }),
    el('option', { value: 'flaky', text: 'The connection drops once' }),
    el('option', { value: 'garbage', text: 'Collecto returns something that is not JSON' }),
  ]);

  const submit = async () => {
    try {
      const body = { plan: plan.id, phone: phone.value };
      if (mode === 'demo') body.scenario = scenario.value;
      const { payment } = await api('POST', '/api/billing/pay', body);
      close();
      go('#/billing/pay/' + payment.reference);
    } catch (error) {
      toast(error.message, true);
    }
  };

  const close = openModal(
    `${plan.name} — ${money(plan.amount)}`,
    'You will get a prompt on your phone. Your PIN is entered there, never here.',
    [
      field('Mobile money number', phone, 'The number that will be charged.'),
      mode === 'demo'
        ? field('Demo: what the handset does', scenario,
            'Only in demo mode. The unhappy paths are the ones worth watching.')
        : null,
    ].filter(Boolean),
    [
      el('button', { class: 'btn ghost', text: 'Cancel', onclick: () => close() }),
      el('button', { class: 'btn primary', text: 'Send prompt', onclick: submit }),
    ],
  );
}

async function showPayment(reference) {
  state.app = null;
  stopBillingPoll();

  document.getElementById('crumb').textContent = 'Payment';
  clear(document.getElementById('topbar-actions')).append(
    el('button', { class: 'btn', text: 'Back to billing', onclick: () => go('#/billing') }),
  );
  renderSidebar();
  const content = clear(document.getElementById('content'));

  const head = el('div', {});
  const body = el('div', {});
  content.append(head, body);

  const draw = (data) => {
    const payment = data.payment;
    clear(head).append(
      el('h2', { class: 'sec', text: money(payment.amount) + ' · ' + payment.plan }),
      el('p', { class: 'sub mono', text: payment.reference }),
      statusBanner(payment),
    );

    clear(body).append(
      el('div', { class: 'cols' }, [
        el('div', {}, [
          el('div', { class: 'paycard' }, [
            el('h3', { class: 'paycard-h', text: 'What the server has done' }),
            el('div', { class: 'console' }, payment.trail.map((line) =>
              el('div', { class: 'l', text: line }))),
          ]),
        ]),
        el('div', {}, [
          data.prompt ? handsetPanel(data.prompt, payment) : null,
          el('div', { class: 'paycard' }, [
            el('h3', { class: 'paycard-h', text: 'Details' }),
            kv('Status', payment.status),
            kv('Checks made', String(payment.checks)),
            kv('Gateway id', payment.transaction_id || '—'),
            kv('Mode', payment.mode),
            payment.status === 'pending'
              ? kv('Gives up in', payment.expires_in + 's')
              : null,
          ].filter(Boolean)),
          el('button', {
            class: 'btn full',
            text: 'Check now',
            onclick: async () => {
              try {
                draw(await api('POST', `/api/billing/payments/${reference}/check`));
              } catch (error) { toast(error.message, true); }
            },
          }),
        ]),
      ]),
    );

    if (payment.status !== 'pending') stopBillingPoll();
  };

  draw(await api('GET', '/api/billing/payments/' + reference));

  // A plain read on a timer. The server's own sweeper is what moves the payment
  // along; this only watches. Close the tab and it still finishes.
  billingTimer = setInterval(async () => {
    try {
      draw(await api('GET', '/api/billing/payments/' + reference));
    } catch {
      stopBillingPoll();
    }
  }, 2000);
}

function statusBanner(payment) {
  if (payment.status === 'successful') {
    return el('div', { class: 'banner ok' }, [
      el('b', { text: 'Paid' }), payment.message || 'The plan is active.',
    ]);
  }
  if (payment.status === 'failed') {
    return el('div', { class: 'banner err' }, [
      el('b', { text: 'Not paid' }),
      (payment.message || 'The payment did not go through.') +
      ' Nothing was charged. You can start again.',
    ]);
  }
  if (payment.status === 'abandoned') {
    return el('div', { class: 'banner warn' }, [
      el('b', { text: 'The prompt expired' }),
      'It was not approved in time, so the server stopped checking. Starting ' +
      'again sends a fresh prompt.',
    ]);
  }
  return el('div', { class: 'banner info' }, [
    el('b', { text: 'Check your phone' }),
    `A prompt was sent to ${payment.phone}. Enter your PIN there to approve ` +
    `${money(payment.amount)}. Safe to close this page — the server keeps checking.`,
  ]);
}

/* The pretend handset. It exists only in demo mode and the server refuses the
 * endpoint otherwise, so there is no version of this that can approve a real
 * payment. */
function handsetPanel(prompt, payment) {
  const act = async (action) => {
    try {
      await api('POST', `/api/billing/demo/${payment.reference}`, { action });
    } catch (error) { toast(error.message, true); }
  };
  const done = prompt.status !== 'pending';
  return el('div', { class: 'paycard handset' }, [
    el('h3', { class: 'paycard-h', text: 'Demo handset' }),
    el('p', { class: 'hint', text: 'Stands in for the customer tapping their PIN. '
      + 'Scenario: ' + (prompt.scenario || 'approve') }),
    el('div', { class: 'handset-screen' }, [
      el('div', { class: 'handset-from', text: 'Mobile Money' }),
      el('div', { text: `Pay ${money(payment.amount)} to Cissytech?` }),
      el('div', { class: 'hint mono', text: payment.reference }),
    ]),
    done
      ? el('p', { class: 'hint', text: 'Answered: ' + prompt.status })
      : el('div', { class: 'handset-actions' }, [
          el('button', { class: 'btn primary sm', text: 'Enter PIN', onclick: () => act('approve') }),
          el('button', { class: 'btn danger sm', text: 'Decline', onclick: () => act('decline') }),
        ]),
  ]);
}

function money(amount) {
  return 'UGX ' + Number(amount || 0).toLocaleString('en-US');
}


/* ── admin ────────────────────────────────────────────────────────────────
 *
 * The only screen that shows one customer's details to somebody else. It is
 * reached on its own routes and its own endpoints rather than by a flag on a
 * customer screen, so the answer to "could a customer see this?" is "there is
 * no route", not "there is a check".
 */
async function showAdmin() {
  state.app = null;
  const data = await api('GET', '/api/admin/users');

  document.getElementById('crumb').textContent = 'Admin';
  clear(document.getElementById('topbar-actions'));
  renderSidebar();
  const content = clear(document.getElementById('content'));

  content.append(
    el('h2', { class: 'sec', text: 'Everyone on this server' }),
    el('p', { class: 'sub', text:
      `${data.users.length} account${data.users.length === 1 ? '' : 's'} · `
      + `${data.sms_today} code${data.sms_today === 1 ? '' : 's'} sent today` }),
  );

  if (data.building) {
    content.append(el('div', { class: 'banner warn' }, [
      el('b', { text: 'A build is running' }),
      `${data.building.app_id} for ${data.building.owner}`,
    ]));
  }

  content.append(el('table', { class: 'apps' }, [
    el('thead', {}, [el('tr', {},
      ['Person', 'Plan', 'Builds', 'Disk', ''].map((h) => el('th', { text: h })))]),
    el('tbody', {}, data.users.map((user) => el('tr', {}, [
      el('td', {}, [
        el('div', { class: 'app-name', text: user.name }),
        el('div', { class: 'app-url mono', text: user.phone
          + (user.apps.length ? ' · ' + user.apps.join(', ') : ' · no apps') }),
      ]),
      el('td', {}, [user.is_admin
        ? el('span', { class: 'pill ok' }, [el('i', { class: 'dot' }), 'Admin'])
        : el('span', { class: 'pill ' + (user.plan === 'trial' ? 'warn' : 'ok') },
            [el('i', { class: 'dot' }), user.plan === 'trial' ? 'Trial' : user.plan])]),
      el('td', { text: `${user.builds_used} / ${user.builds_limit}` }),
      el('td', { text: megabytes(user.disk) }),
      el('td', { style: 'text-align:right' }, [
        el('button', {
          class: 'btn sm', text: '+5 builds',
          onclick: async (e) => {
            e.target.disabled = true;
            try {
              await api('POST', `/api/admin/users/${user.id}/grant`, { builds: 5 });
              toast(`Gave ${user.name} five more builds`);
              route();
            } catch (error) { toast(error.message, true); }
          },
        }),
      ]),
    ]))),
  ]));

  if (state.demoSms) {
    content.append(el('h2', { class: 'sec', style: 'margin-top:28px', text: 'Codes sent' }));
    try {
      const log = await api('GET', '/api/admin/sms');
      content.append(el('div', { class: 'console' }, log.messages.length
        ? log.messages.map((m) => el('div', { class: 'l', text: `${m.phone}  ${m.message}` }))
        : [el('div', { class: 'l', text: 'Nothing yet.' })]));
    } catch { /* the live channel keeps no log, which is correct */ }
  }
}

/* ── routing ─────────────────────────────────────────────────────────── */

function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

const AUTH_ROUTES = ['#/login', '#/signup', '#/verify'];

async function route() {
  const hash = location.hash || '#/';
  stopBillingPoll();
  try {
    // Signed out: only the three auth screens exist. Signed in: those three
    // are not screens you should be looking at, so they bounce home.
    if (!state.user) {
      if (hash === '#/signup') { await showSignup(); return; }
      if (hash === '#/verify') { await showVerify(); return; }
      await showLogin();
      return;
    }
    document.body.classList.remove('signed-out');
    if (AUTH_ROUTES.includes(hash)) { go('#/'); return; }

    if (hash === '#/admin') { await showAdmin(); return; }

    const pay = hash.match(/^#\/billing\/pay\/([^/]+)$/);
    if (pay) {
      await showPayment(decodeURIComponent(pay[1]));
      return;
    }
    if (hash === '#/billing') {
      await showBilling();
      return;
    }
    const build = hash.match(/^#\/app\/([^/]+)\/build\/(\d+)$/);
    if (build) {
      await showBuild(decodeURIComponent(build[1]), build[2]);
      return;
    }
    const app = hash.match(/^#\/app\/([^/]+)$/);
    if (app) await showApp(decodeURIComponent(app[1]));
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

/* Session first, then draw. Routing before we know who this is would flash the
 * app shell at somebody who is about to be shown a login screen. */
(async () => {
  await loadSession();
  route();
  if (state.user) {
    refreshHealth();
    setInterval(refreshHealth, 60000);
  }
})();
