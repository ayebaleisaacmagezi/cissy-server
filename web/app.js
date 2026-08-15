'use strict';

/* Cissyweb2app - browser client.
 *
 * No framework and no build step: the whole UI is this file, so editing it on
 * the server means a refresh rather than a toolchain.
 *
 * DOM is built with el() rather than innerHTML throughout. App names, URLs and
 * build logs all come from outside and end up in the page, so string-
 * interpolated HTML would be an injection waiting to happen.
 */

// Mirrors NAV_ICONS in cissy/config.py - the set the generated app can render.
const NAV_ICONS = ['home', 'storefront', 'menu_book', 'article', 'shopping_bag',
  'event', 'call', 'person', 'bookmark', 'download', 'settings', 'info'];

// target value → [label, which feature it needs]

const state = {
  apps: [],
  app: null,
  builds: [],
  draft: null,         // unsaved edits, kept while moving between an app's pages
  section: null,       // which of the app's pages is open
  dirty: false,
  health: null,
  user: null,          // whoever is signed in, from /api/auth/session
  demoSms: false,      // true when codes are simulated rather than texted
  pending: null,       // a phone part-way through signup
  reset: null,         // a phone part-way through a forgotten password
  streaming: null,
  railPinned: null,   // null until the handle is touched, then the user's
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
  studio: 'dashboard_customize',
  offline: 'cloud_off',
  signing: 'key',
  build: 'play_arrow',
  chevron: 'expand_more',
  docs: 'menu_book',
  notifications: 'notifications',
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

/* XMLHttpRequest rather than fetch, which is the one thing fetch cannot do:
 * report how far an upload has got. `onProgress` receives a fraction from 0
 * to 1, or null when the browser cannot measure the total. */
function upload(appId, slot, file, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open('PUT', `/api/apps/${appId}/files/${slot}`);
    for (const [key, value] of Object.entries(authHeaders({ 'X-Filename': file.name }))) {
      request.setRequestHeader(key, value);
    }

    const read = () => {
      try { return JSON.parse(request.responseText || '{}'); } catch { return {}; }
    };

    request.upload.addEventListener('progress', (event) => {
      if (!onProgress) return;
      onProgress(event.lengthComputable ? event.loaded / event.total : null);
    });
    // The bytes are gone but the server has not answered yet: hold the bar
    // full rather than letting it sit at 99% through the slow part.
    request.upload.addEventListener('load', () => onProgress && onProgress(1));
    request.addEventListener('load', () => {
      const payload = read();
      if (request.status >= 200 && request.status < 300) resolve(payload.app);
      else reject(new Error(payload.error || `Upload failed (${request.status})`));
    });
    request.addEventListener('error', () =>
      reject(new Error('The upload did not reach the server. Check your connection.')));
    request.addEventListener('abort', () => reject(new Error('Upload cancelled.')));

    request.send(file);
  });
}

/* Reads a server-sent event stream over fetch rather than EventSource.
 * EventSource cannot send headers, which would mean putting the password in a
 * query string - where it lands in proxy logs and browser history. */
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
      el('span', { text: health.ok ? 'Toolchain ready' : 'Toolchain not ready' }),
    ]),
  );
  for (const tool of health.tools) {
    foot.append(el('div', { class: 'ln' }, [el('span', { text: tool.ok
      ? `${tool.name} ${tool.version}`
      : `${tool.name} - ${tool.detail}` })]));
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
          el('img', { src: '/logo.webp', alt: 'Web2App' }),
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

/* ── password fields ───────────────────────────────────────────────────────
 *
 * These are the rules, not a suggestion. The same five run in
 * accounts.validate_password, so a full meter means the server will take it -
 * keep the two lists in step and in the same order.
 *
 * The button is never disabled, though. A dead button says nothing about why
 * it is dead, so clicking it is allowed and answers the question instead.
 */
const PASSWORD_RULES = [
  ['8 characters', (value) => value.length >= 8],
  ['Lowercase letter', (value) => /[a-z]/.test(value)],
  ['Capital letter', (value) => /[A-Z]/.test(value)],
  ['Number', (value) => /[0-9]/.test(value)],
  ['Symbol', (value) => /[^A-Za-z0-9]/.test(value)],
];

const PASSWORD_INCOMPLETE = 'Your password is missing something below. Fill in every tick.';

const STRENGTH = ['weak', 'medium', 'strong'];

/* A password input with an eye, and optionally a strength meter under it.
 *
 * Returned as a bundle rather than appended anywhere, because the caller
 * decides which `field()` it sits in and what the label says. */
function passwordField({ placeholder = 'Your password', meter = false, autocomplete } = {}) {
  const input = el('input', {
    class: 'input', type: 'password', placeholder,
    autocomplete: autocomplete || (meter ? 'new-password' : 'current-password'),
  });

  const eye = el('button', {
    class: 'pw-eye', type: 'button', 'aria-label': 'Show password',
    onclick: () => {
      const hidden = input.type === 'password';
      input.type = hidden ? 'text' : 'password';
      eye.setAttribute('aria-label', hidden ? 'Hide password' : 'Show password');
      clear(eye).append(icon(hidden ? 'visibility_off' : 'visibility', 'gl'));
      input.focus();
    },
  }, [icon('visibility', 'gl')]);

  const wrap = el('div', { class: 'pw-wrap' }, [input, eye]);
  // A field with no meter is a field for a password that already exists, so
  // there is nothing to hold it to. It always passes.
  if (!meter) {
    return { input, node: wrap, value: () => input.value, complete: () => true };
  }

  const fill = el('div', { class: 'pw-fill' });
  const label = el('strong', { class: 'pw-label', text: 'Start typing' });
  const rules = PASSWORD_RULES.map(([text]) => el('span', { class: 'pw-rule', text }));

  const redraw = () => {
    const value = input.value;
    let met = 0;
    PASSWORD_RULES.forEach(([, passes], index) => {
      const ok = passes(value);
      if (ok) met += 1;
      rules[index].classList.toggle('met', ok);
    });
    // Nothing typed is not weak, it is nothing. Saying "Weak" at an empty box
    // reads as a verdict on a password that does not exist yet.
    const level = !value ? '' : STRENGTH[met <= 2 ? 0 : met < 5 ? 1 : 2];
    fill.className = `pw-fill ${level}`;
    label.className = `pw-label ${level}`;
    label.textContent = value
      ? level.charAt(0).toUpperCase() + level.slice(1)
      : 'Start typing';
  };
  input.addEventListener('input', redraw);

  return {
    input,
    value: () => input.value,
    complete: () => PASSWORD_RULES.every(([, passes]) => passes(input.value)),
    node: el('div', {}, [
      wrap,
      // Announced, but politely: a reader hears the strength settle after a
      // pause rather than being interrupted on every keystroke.
      el('div', { class: 'pw-strength', 'aria-live': 'polite' }, [
        el('div', { class: 'pw-head' }, [
          el('span', { text: 'Password strength' }),
          label,
        ]),
        el('div', { class: 'pw-track' }, [fill]),
        el('div', { class: 'pw-rules' }, rules),
      ]),
    ]),
  };
}

async function showSignup() {
  const name = el('input', { class: 'input', placeholder: 'Your name', autocomplete: 'name' });
  const phone = phoneField();
  const pass = passwordField({ placeholder: 'At least 8 characters', meter: true });
  const problem = el('div', {});
  const button = el('button', { class: 'btn primary full', text: 'Create account' });

  const submit = async () => {
    if (!pass.complete()) {
      authError(problem, PASSWORD_INCOMPLETE);
      pass.input.focus();
      return;
    }
    button.disabled = true;
    clear(problem);
    try {
      const data = await api('POST', '/api/auth/signup', {
        name: name.value, phone: phone.value(), password: pass.value(),
      });
      state.pending = { phone: data.phone, code: data.code || '', demo: data.demo };
      go('#/verify');
    } catch (error) {
      authError(problem, error.message);
      button.disabled = false;
    }
  };
  button.addEventListener('click', submit);
  for (const input of [name, phone.input, pass.input]) onEnter(input, submit);

  authScreen(
    'Create your account',
    'Free. Three builds to try it with, and no card.',
    [
      problem,
      field('Your name', name),
      field('Phone number', phone.node,
        'We send a code to confirm it. This is also the number you will pay from.'),
      field('Password', pass.node),
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
  const phone = phoneField();
  // No meter here. The password already exists, so rating it is a judgement
  // nobody asked for at the moment they are trying to get in.
  const pass = passwordField({ placeholder: 'Your password' });
  const problem = el('div', {});
  const button = el('button', { class: 'btn primary full', text: 'Log in' });

  const submit = async () => {
    button.disabled = true;
    clear(problem);
    try {
      const data = await api('POST', '/api/auth/login', {
        phone: phone.value(), password: pass.value(),
      });
      state.user = data.user;
      go('#/');
    } catch (error) {
      authError(problem, error.message);
      button.disabled = false;
    }
  };
  button.addEventListener('click', submit);
  for (const input of [phone.input, pass.input]) onEnter(input, submit);

  authScreen(
    'Welcome back',
    'Log in to your apps and builds.',
    [
      problem,
      field('Phone number', phone.node),
      field('Password', pass.node),
      button,
      el('p', { class: 'authfoot' }, [authLink('Forgot your password?', '#/forgot')]),
    ],
    ['New here? ', authLink('Create an account', '#/signup')],
  );
}

/* A forgotten password, in two screens: prove you hold the number, then choose
 * a new one. The code is the same six digits signup uses, so the second screen
 * below is deliberately close to the verify screen above. */

async function showForgot() {
  const phone = phoneField(state.reset ? state.reset.phone : '');
  const problem = el('div', {});
  const button = el('button', { class: 'btn primary full', text: 'Send me a code' });

  const submit = async () => {
    button.disabled = true;
    clear(problem);
    try {
      const data = await api('POST', '/api/auth/forgot', { phone: phone.value() });
      // The server answers the same way for a number it has never seen, so
      // there is nothing here to tell them apart on - and nothing should be.
      state.reset = { phone: data.phone, code: data.code || '', demo: data.demo };
      go('#/reset');
    } catch (error) {
      authError(problem, error.message);
      button.disabled = false;
    }
  };
  button.addEventListener('click', submit);
  onEnter(phone.input, submit);

  authScreen(
    'Forgot your password?',
    'We will text a code to the number on the account.',
    [problem, field('Phone number', phone.node), button],
    ['Remembered it? ', authLink('Log in', '#/login')],
  );
}

async function showReset() {
  if (!state.reset) { go('#/forgot'); return; }
  const { phone } = state.reset;

  const code = el('input', {
    class: 'input mono code', placeholder: '000000', inputmode: 'numeric', maxlength: 6,
  });
  const pass = passwordField({ placeholder: 'At least 8 characters', meter: true });
  // The one screen where a typo locks somebody out: there is no old password
  // left to fall back on, and the code they used to get here is spent.
  const repeat = passwordField({ placeholder: 'Repeat your password' });
  const problem = el('div', {});
  const button = el('button', { class: 'btn primary full', text: 'Set new password' });

  const submit = async () => {
    if (!pass.complete()) {
      authError(problem, PASSWORD_INCOMPLETE);
      pass.input.focus();
      return;
    }
    if (pass.value() !== repeat.value()) {
      authError(problem, 'The two passwords do not match.');
      repeat.input.select();
      return;
    }
    button.disabled = true;
    clear(problem);
    try {
      const data = await api('POST', '/api/auth/reset', {
        phone, code: code.value, password: pass.value(),
      });
      state.user = data.user;
      state.reset = null;
      toast('Password changed. Other devices have been signed out.');
      go('#/');
    } catch (error) {
      authError(problem, error.message);
      button.disabled = false;
      code.select();
    }
  };
  button.addEventListener('click', submit);
  for (const input of [code, pass.input, repeat.input]) onEnter(input, submit);

  const resend = el('button', {
    class: 'btn full', text: 'Send another code',
    onclick: async () => {
      resend.disabled = true;
      try {
        const data = await api('POST', '/api/auth/forgot', { phone });
        state.reset = { ...state.reset, code: data.code || '' };
        toast(data.note || 'A new code is on its way');
        go('#/reset');
      } catch (error) {
        authError(problem, error.message);
      }
      resend.disabled = false;
    },
  });

  authScreen(
    'Choose a new password',
    `If that number has an account, a 6-digit code is on its way to ${phone}.`,
    [
      problem,
      // Demo mode only, exactly as on the verify screen. A live server never
      // sends the code back down the channel it is checking.
      state.reset.demo && state.reset.code
        ? el('div', { class: 'banner info demo-code' }, [
            el('b', { text: 'Demo mode, nothing was texted' }),
            'Your code is ',
            el('code', { text: state.reset.code }),
          ])
        : null,
      field('Code', code),
      field('New password', pass.node),
      field('Confirm password', repeat.node,
        'Setting it signs you in here and signs out every other device.'),
      button,
      resend,
    ],
    ['Wrong number? ', authLink('Start again', '#/forgot')],
  );
}

/* ── sidebar ─────────────────────────────────────────────────────────── */

/* Each entry is a full page of its own: [id, label, icon, subtitle]. The order
 * here is the order the Next buttons at the foot of each page walk through. */
const APP_PAGES = [
  ['overview', 'Overview', 'overview', 'Builds, artifacts and the state of this app.'],
  ['identity', 'Identity', 'identity', 'What the app is called, and the IDs the stores know it by.'],
  ['webview', 'WebView', 'webview', 'The website the app wraps, and how it behaves.'],
  ['branding', 'Branding', 'branding', 'The icon and the splash screen.'],
  ['studio', 'Studio', 'studio', 'Modules, theme and navigation - with a live preview.'],
  ['notifications', 'Notifications', 'notifications',
    'Push notifications, through your own Firebase project.'],
  ['signing', 'Signing', 'signing', 'The key that proves every release comes from you.'],
  ['build', 'Build', 'build', 'Turn the configuration into an installable app.'],
];

function planCard() {
  const user = state.user;
  if (!user) return null;
  const left = user.builds_left ?? 0;
  const total = user.builds_limit ?? 0;
  const share = total ? Math.max(0, Math.min(100, (left / total) * 100)) : 0;
  const tone = left === 0 ? 'out' : left <= 1 ? 'low' : '';

  return el('div', { class: 'plancard ' + tone }, [
    el('b', { text: user.plan_name }),
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
  const initials = (state.user.name || '?').trim().split(/\s+/).slice(0, 2)
    .map((part) => part[0]).join('').toUpperCase();

  box.append(el('button', {
    class: 'avatar', text: initials, title: state.user.name,
    onclick: () => openModal(state.user.name, state.user.phone, [
      kv('Plan', state.user.plan_name),
      kv('Builds left', `${state.user.builds_left ?? 0} of ${state.user.builds_limit ?? 0}`),
      state.user.plan_until ? kv('Renews', shortDate(state.user.plan_until)) : null,
    ].filter(Boolean), [
      el('button', { class: 'btn', text: 'Log out', onclick: logout }),
      el('button', { class: 'btn primary', text: 'Billing', onclick: () => go('#/billing') }),
    ]),
  }));
}

/* A nav label, as an element rather than a text node, so the folded rail has
 * something to hide. */
function navText(label) {
  return el('span', { class: 'navtext', text: label });
}

function renderSidebar() {
  renderAccount();
  renderRail();
  const nav = clear(document.getElementById('side-nav'));

  const onBilling = location.hash.startsWith('#/billing');

  if (!state.app) {
    nav.append(el('div', { class: 'nav-group' }, [
      el('button', { class: 'nav-item' + (onBilling ? '' : ' active'), onclick: () => go('#/') }, [
        icon('apps'), navText('All apps'),
        el('span', { class: 'badge', text: String(state.apps.length) }),
      ]),
      el('button', { class: 'nav-item', onclick: newAppDialog }, [
        icon('plus'), navText('New app'),
      ]),
      el('button', { class: 'nav-item' + (onBilling ? ' active' : ''), onclick: () => go('#/billing') }, [
        icon('billing'), navText('Billing'),
      ]),
      el('a', { class: 'nav-item', href: '/docs' }, [
        icon('docs'), navText('Documentation'),
      ]),
      state.user && state.user.is_admin
        ? el('button', { class: 'nav-item', onclick: () => go('#/admin') }, [
            icon('admin'), navText('Admin'),
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
      ...APP_PAGES.map(([id, label, glyph]) =>
        el('button', {
          class: 'nav-item' + (state.section === id ? ' active' : ''),
          onclick: () => go(`#/app/${state.app.id}/${id}`),
        }, [icon(glyph), navText(label)]),
      ),
      el('a', { class: 'nav-item', href: '/docs' }, [
        icon('docs'), navText('Documentation'),
      ]),
    ]),
  );
}

/* The sidebar folded to a rail of icons.
 *
 * The Studio asks for it on arrival - it is the one page that wants the width,
 * and 194px is most of a panel. Touching the handle is a decision, so from then
 * on the choice is the user's for the rest of the session and no page overrides
 * it again. */
function renderRail() {
  const shell = document.querySelector('.shell');
  if (!shell) return;
  const wanted = state.railPinned === null
    ? state.section === 'studio'
    : state.railPinned;
  shell.classList.toggle('railed', Boolean(wanted));

  const toggle = document.getElementById('rail-toggle');
  if (toggle) {
    toggle.title = wanted ? 'Widen the sidebar' : 'Fold the sidebar';
  }
}

function toggleRail() {
  const shell = document.querySelector('.shell');
  state.railPinned = !shell.classList.contains('railed');
  renderRail();
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
            el('div', { text: `v${app.version_name || '1.0.0'} (${app.version_code ?? 1})` }),
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

// The client's own reversed domain (portal.example.com → com.example.portal),
// so apps are published under the client's identity, not ours. Falls back to
// the app name when no usable URL has been typed yet.
function suggestPackageId(nameValue, urlValue) {
  const raw = urlValue.trim();
  let host = '';
  try {
    host = new URL(raw.includes('://') ? raw : 'https://' + raw).hostname;
  } catch { /* not a URL yet */ }
  const parts = host.toLowerCase().split('.')
    .filter((p) => p && p !== 'www')
    .map((p) => p.replace(/[^a-z0-9_]/g, '').replace(/^[0-9_]+/, ''))
    .filter(Boolean);
  if (parts.length >= 2) return parts.reverse().join('.');
  const slug = nameValue.trim().toLowerCase().replace(/[^a-z0-9]+/g, '');
  return slug ? `com.${slug}.app` : '';
}

function newAppDialog() {
  const name = el('input', { class: 'input', placeholder: 'My Business' });
  const url = el('input', { class: 'input mono', placeholder: 'https://www.example.com' });
  const pkg = el('input', { class: 'input mono', placeholder: 'com.example.app' });

  // Suggested rather than generated silently: the package id is permanent once
  // the app is on Play.
  const suggest = () => {
    if (pkg.dataset.touched) return;
    pkg.value = suggestPackageId(name.value, url.value);
  };
  name.addEventListener('input', suggest);
  url.addEventListener('input', suggest);
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
    'All of this can change later - except the package ID, once it is on Play.',
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

/* ── the phone field ─────────────────────────────────────────────────────
 *
 * One control for the three places a number is asked for - signup, login and
 * paying - because the number somebody verifies on is the number they pay
 * from, and typing it two different ways is how those quietly drift apart.
 *
 * The country is shown, never chosen. Collecto charges Ugandan mobile money
 * in shillings and has no currency field, so a picker would offer two hundred
 * countries of which one works, and the refusal would arrive after the number
 * and after the button. It also means `value()` is the single place the 256 is
 * put on, so it cannot be left off.
 */
function phoneField(msisdn) {
  const input = el('input', {
    class: 'input tel', inputmode: 'tel', placeholder: '772 000 000',
    autocomplete: 'tel-national', value: localDigits(msisdn),
  });
  const net = el('span', { class: 'net' });

  const redraw = () => {
    // Grouped in threes as they type. People proof-read a number they just
    // typed by scanning it in groups, and 772145903 is the shape that hides a
    // transposed digit. The leading 0 goes silently - typing it is habit, not
    // a mistake worth an error message.
    const atEnd = input.selectionStart === input.value.length;
    const digits = input.value.replace(/\D/g, '').replace(/^0+/, '').slice(0, 9);
    input.value = digits.replace(/(\d{3})(?=\d)/g, '$1 ').trim();
    if (atEnd) input.selectionStart = input.selectionEnd = input.value.length;

    const carrier = networkOf(digits);
    net.style.display = carrier ? '' : 'none';
    clear(net).append(el('span', { class: 'dot' }), carrier);
  };
  input.addEventListener('input', redraw);
  redraw();

  return {
    input,
    node: el('div', {}, [
      el('div', { class: 'phone-wrap' }, [
        el('span', { class: 'prefix', text: '🇺🇬 +256' }),
        input,
      ]),
      net,
    ]),
    value: () => '256' + input.value.replace(/\D/g, ''),
  };
}

/* Naming the network back to them is the cheapest confidence in the whole
 * flow: no lookup, no request, just a prefix table - and it catches the
 * transposed digit before a stranger's handset rings. */
function networkOf(digits) {
  const two = digits.slice(0, 2);
  if (digits.length < 3) return '';
  if (['77', '78', '76', '39'].includes(two)) return 'MTN Uganda';
  if (['70', '74', '75', '20'].includes(two)) return 'Airtel Uganda';
  return '';
}

/* A stored MSISDN back to the nine digits the field shows. */
function localDigits(msisdn) {
  const digits = String(msisdn || '')
    .replace(/\D/g, '').replace(/^256/, '').replace(/^0+/, '').slice(0, 9);
  return digits.replace(/(\d{3})(?=\d)/g, '$1 ').trim();
}

/* ── app page ────────────────────────────────────────────────────────── */

async function showAppPage(appId, section) {
  const [{ app }, { builds }] = await Promise.all([
    api('GET', '/api/apps/' + appId),
    api('GET', `/api/apps/${appId}/builds`),
  ]);
  state.app = app;
  state.builds = builds;
  state.section = APP_PAGES.some(([id]) => id === section) ? section : 'overview';

  // The draft outlives any single page, so edits survive moving between them.
  // It belongs to the app: opening a different app starts a fresh one.
  if (!state.draft || state.draft.id !== app.id) {
    state.draft = { ...app };
    state.dirty = false;
  }
  const draft = state.draft;

  const [, label, , subtitle] = APP_PAGES.find(([id]) => id === state.section);
  document.getElementById('crumb').textContent = `${app.name} · ${label}`;
  renderSidebar();

  const markDirty = () => { state.dirty = true; renderTopbar(); };
  const bind = (key, input, transform = (v) => v) => {
    input.addEventListener('input', () => { draft[key] = transform(input.value); markDirty(); });
    return input;
  };

  async function save() {
    try {
      const { app: saved } = await api('PUT', '/api/apps/' + app.id, draft);
      state.app = saved;
      state.draft = { ...saved };
      state.dirty = false;
      document.getElementById('crumb').textContent = `${saved.name} · ${label}`;
      renderSidebar();
      renderTopbar();
      toast('Saved');
      return true;
    } catch (error) {
      toast(error.message, true);
      return false;
    }
  }

  function renderTopbar() {
    clear(document.getElementById('topbar-actions')).append(
      el('span', { class: 'pill' + (state.dirty ? ' warn' : ''),
        text: state.dirty ? 'Unsaved changes' : 'Saved' }),
      el('button', { class: 'btn', text: 'Save', disabled: !state.dirty, onclick: save }),
      el('button', { class: 'btn primary', text: 'Build', onclick: () => buildDialog(app) }),
    );
  }

  const pages = {
    overview: () => overviewPage(app, builds),
    identity: () => identitySection(draft, bind),
    webview: () => webviewSection(draft, bind, markDirty),
    branding: () => brandingSection(app, draft, markDirty),
    studio: () => studioPage(app, draft, markDirty),
    notifications: () => notificationsSection(app, draft, markDirty),
    signing: () => signingSection(app),
    build: () => buildSection(app),
  };

  // The Studio is a canvas rather than a page of fields, and a canvas wants the
  // height. Its heading said "Studio" directly under a topbar already reading
  // "<app> · Studio", and that repetition cost about 115px - which is the
  // difference between the phone sitting in the middle of the screen and
  // sitting below it until you scroll.
  const canvas = state.section === 'studio';

  clear(document.getElementById('content')).append(
    el('div', { class: 'page' + (canvas ? ' page-canvas' : '') }, [
      canvas ? null : el('div', { class: 'page-head' }, [
        el('h2', { class: 'sec', text: label }),
        el('p', { class: 'sub', text: subtitle }),
      ]),
      pages[state.section](),
      pager(app, state.section, save),
    ]),
  );
  renderTopbar();
}

/* Back and Next at the foot of every page, so finishing one step leads to the
 * next without hunting the sidebar. Next saves first - moving on must never
 * shed the edits, and a failed save keeps you here with the reason on screen. */
function pager(app, section, save) {
  const index = APP_PAGES.findIndex(([id]) => id === section);
  const prev = APP_PAGES[index - 1];
  const next = APP_PAGES[index + 1];
  const goTo = async (id) => {
    if (state.dirty && !(await save())) return;
    go(`#/app/${app.id}/${id}`);
  };
  return el('div', { class: 'pager' }, [
    prev
      ? el('button', { class: 'btn', onclick: () => goTo(prev[0]) }, ['← ', prev[1]])
      : el('span'),
    next
      ? el('button', { class: 'btn primary', onclick: () => goTo(next[0]) },
          ['Next: ' + next[1] + ' →'])
      : null,
  ]);
}

/* One card per page. The page heading already names the section, so the card
 * itself carries no legend - except when a page stacks more than one card and
 * each needs its own title. */
function fieldset(id, legend, children) {
  return el('section', { class: 'group', id: 'sec-' + id },
    [legend ? el('h3', { class: 'group-title', text: legend }) : null, ...children]);
}

function checkbox(label, checked, onchange, hint) {
  return el('label', { class: 'check', style: 'margin-bottom:14px' }, [
    el('input', { type: 'checkbox', checked, onchange: (e) => onchange(e.target.checked) }),
    el('span', {}, [label, hint ? el('div', { class: 'hint', text: hint }) : null]),
  ]);
}

function kv(key, value) {
  return el('div', { class: 'kv' }, [
    el('span', { text: key }), el('span', { text: value || '-' }),
  ]);
}

/* ── sections ────────────────────────────────────────────────────────── */

function overviewPage(app, builds) {
  const btn = 'width:100%;justify-content:center;margin-top:8px';
  const aside = el('aside', { class: 'summary' }, [
    el('h4', { text: 'Summary' }),
    kv('Website', hostOf(app.website_url)),
    kv('Package', app.android_package_id),
    kv('Features', `${(app.features || []).length} enabled`),
    kv('Next build', `${app.version_name || '1.0.0'} (${app.next_version_code ?? app.version_code ?? 1})`),
    kv('Signing', app.keystore_file && app.key_alias ? 'Upload key' : 'Debug key'),
    el('button', { class: 'btn', style: btn + ';margin-top:14px',
      text: 'Duplicate as new app', onclick: () => duplicateDialog(app) }),
    el('button', { class: 'btn danger sm', style: btn,
      text: 'Delete app', onclick: () => deleteDialog(app) }),
  ]);
  return el('div', { class: 'cols' }, [overviewSection(app, builds), aside]);
}

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

/* Opaque /d/<token> links wherever the build minted a token - hovering one
 * reveals nothing about apps, builds or the API. The readable path form only
 * remains for builds recorded before tokens existed. */
function artifactHref(app, number, artifact) {
  return artifact.token
    ? `/d/${encodeURIComponent(artifact.token)}`
    : `/api/apps/${app.id}/builds/${number}/artifacts/${encodeURIComponent(artifact.name)}`;
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
      el('div', { class: 'app-url', text: `v${build.version_name || '1.0.0'} (${build.version_code ?? 1})` }),
    ]),
    el('td', {}, [
      el('div', { text: build.started_at
        ? shortDate(new Date(build.started_at * 1000).toISOString()) : '-' }),
      el('div', { class: 'app-url', text: seconds(build.duration) }),
    ]),
    el('td', {}, [
      el('span', { class: 'pill ' + status[0] }, [el('i', { class: 'dot' }), status[1]]),
      build.hint ? el('div', { class: 'hint', text: build.hint }) : null,
    ]),
    el('td', {}, (build.artifacts || []).map((artifact) =>
      el('a', {
        class: 'btn sm', style: 'margin:2px 4px 2px 0',
        href: artifactHref(app, build.number, artifact),
        text: `${artifact.kind.toUpperCase()} · ${megabytes(artifact.size)}`,
        download: artifact.name,
      }))),
  ]);
}

function identitySection(draft, bind) {
  return fieldset('identity', '', [
    field('Project name', bind('name', el('input', { class: 'input', value: draft.name || '' }))),
    field('App name', bind('app_name',
      el('input', { class: 'input', value: draft.app_name || draft.name || '' })),
      'Shown under the icon on the phone.'),
    el('div', { class: 'row2' }, [
      field('Android package ID',
        el('input', { class: 'input mono', value: draft.android_package_id || '', disabled: true }),
        'Permanent - duplicate the app to change it.'),
      field('iOS bundle ID',
        bind('ios_bundle_id', el('input', { class: 'input mono', value: draft.ios_bundle_id || '' }))),
    ]),
  ]);
}

function webviewSection(draft, bind, markDirty) {
  const external = el('select', { class: 'input' },
    [['browser', "Open in the phone's browser"],
     ['webview', 'Stay inside the app'],
     ['block', 'Block them']].map(([value, label]) =>
      el('option', { value, text: label, selected: draft.external_link_behavior === value })));
  external.addEventListener('change', () => {
    draft.external_link_behavior = external.value;
    markDirty();
  });

  return fieldset('webview', '', [
    field('Website URL',
      bind('website_url', el('input', { class: 'input mono', value: draft.website_url || '' })),
      'The page the app opens on launch.'),
    field('Allowed domains',
      bind('allowed_domains',
        el('input', { class: 'input mono', value: (draft.allowed_domains || []).join(', ') }),
        (v) => v.split(',').map((s) => s.trim()).filter(Boolean)),
      'Comma separated. Subdomains are included.'),
    field('Links outside those domains', external),
    field('Custom user agent',
      bind('custom_user_agent',
        el('input', { class: 'input mono', value: draft.custom_user_agent || '',
          placeholder: 'Leave blank for the default' }),
        (v) => v.trim() || null)),
    checkbox('Require HTTPS', draft.require_https,
      (on) => { draft.require_https = on; markDirty(); },
      'Blocks insecure page loads inside the app.'),
    checkbox('Enable JavaScript', draft.javascript_enabled,
      (on) => { draft.javascript_enabled = on; markDirty(); }),
    checkbox('Enable local storage', draft.dom_storage_enabled,
      (on) => { draft.dom_storage_enabled = on; markDirty(); },
      'Needed by most sites that keep you signed in.'),
  ]);
}

const SPLASH_LIGHT = '#ffffff';
const SPLASH_DARK = '#101014';

function brandingSection(app, draft, markDirty) {
  return el('div', {}, [
    fieldset('branding', '', [
      el('div', { class: 'row2' }, [
        uploadSlot(app, 'icon', 'App icon', 'PNG, ideally 1024×1024', '.png'),
        uploadSlot(app, 'splash', 'Splash image',
          'Only used when the splash below is set to an image', '.png,.jpg,.jpeg'),
      ]),
    ]),
    splashSection(app, draft, markDirty),
  ]);
}

/* What covers the app while the first page loads.
 *
 * The default is the icon on a background rather than an uploaded image: the
 * icon is already there, it cannot be cropped by a phone whose aspect nobody
 * predicted, and the background can follow the system into dark mode, which one
 * flat image cannot. So the two phones are the whole point of this card - two
 * colours cannot be shown on one. */
function splashSection(app, draft, markDirty) {
  const card = el('section', { class: 'group', id: 'sec-splash' });

  const style = el('select', { class: 'input' }, [
    el('option', { value: 'icon', text: 'Your app icon on a background',
      selected: draft.splash_style !== 'image' }),
    el('option', { value: 'image', text: 'The image above, edge to edge',
      selected: draft.splash_style === 'image' }),
  ]);
  style.addEventListener('change', () => {
    draft.splash_style = style.value; markDirty(); render();
  });

  const light = colourWell('splash_bg_light', SPLASH_LIGHT);
  const dark = colourWell('splash_bg_dark', SPLASH_DARK);

  function colourWell(key, fallback) {
    const value = () => draft[key] || fallback;
    const well = el('input', { class: 'colorwell', type: 'color', value: value() });
    const hex = el('input', { class: 'input mono', value: value() });
    const set = (next) => {
      draft[key] = next;
      well.value = next; hex.value = next;
      markDirty(); paintPhones();
    };
    well.addEventListener('input', () => set(well.value));
    hex.addEventListener('input', () => {
      // Only once it is a colour. Repainting on "#1" would flash black between
      // every keystroke of a hex somebody is typing out by hand.
      if (/^#[0-9a-fA-F]{6}$/.test(hex.value)) set(hex.value.toLowerCase());
    });
    return { node: el('div', { class: 'colorrow' }, [well, hex]), value };
  }

  const phones = ['light', 'dark'].map((mode) => {
    const shot = el('div', { class: 'spscreen' });
    return {
      mode,
      shot,
      node: el('figure', { class: 'spfig' }, [
        el('div', { class: 'sp' }, [shot]),
        el('figcaption', { text: mode === 'light' ? 'Light' : 'Dark' }),
      ]),
    };
  });

  function paintPhones() {
    const image = draft.splash_style === 'image';
    const stamp = Date.parse(app.updated_at) || 0;
    for (const phone of phones) {
      const file = image ? app.splash_file : app.icon_file;
      const slot = image ? 'splash' : 'icon';
      phone.shot.style.background = image
        ? '#1c1b1b'
        : (phone.mode === 'dark' ? dark.value() : light.value());
      phone.shot.classList.toggle('cover', image);
      clear(phone.shot);
      if (file) {
        phone.shot.append(el('img', {
          alt: '', src: `/api/apps/${app.id}/files/${slot}?t=${stamp}`,
        }));
      }
    }
  }

  function render() {
    const image = draft.splash_style === 'image';
    const needsIcon = !image && !app.icon_file;

    clear(card).append(
      el('h3', { class: 'group-title', text: 'Splash screen' }),
      el('div', { class: 'splashrow' }, [
        el('div', {}, [
          field('Screen', style),
          needsIcon
            ? el('div', { class: 'needicon' }, [
                icon('image'),
                el('div', {}, [
                  el('b', { text: 'Upload an app icon first' }),
                  el('div', { class: 'hint',
                    text: 'This splash shows your icon, and there is not one yet.' }),
                ]),
              ])
            : null,
          image ? null : el('div', { class: 'wells' }, [
            el('div', { class: 'well' }, [
              el('b', { text: 'Light mode' }), light.node,
            ]),
            el('div', { class: 'well' }, [
              el('b', { text: 'Dark mode' }), dark.node,
            ]),
          ]),
          el('p', { class: 'hint', text: image
            ? 'Filled edge to edge, so anything close to the border may be cropped.'
            : 'The phone picks which of the two by its own dark-mode setting.' }),
        ]),
        el('div', { class: 'splashpreview' }, phones.map((phone) => phone.node)),
      ]),
    );
    paintPhones();
  }

  render();
  return card;
}

function uploadSlot(app, slot, label, hint, accept) {
  const current = app[slot + '_file'];
  const input = el('input', { type: 'file', accept, style: 'display:none' });
  const box = el('div', { class: 'drop' + (current ? ' filled' : '') });

  // A keystore is not a picture; the image slots show what was uploaded.
  const thumb = current && (slot === 'icon' || slot === 'splash')
    ? el('img', {
        class: 'drop-thumb', alt: '',
        src: `/api/apps/${app.id}/files/${slot}?t=`
          + (Date.parse(app.updated_at) || 0),
      })
    : null;

  const render = () => {
    // Native append(), so a bare `null` in the list would come out as the
    // literal text "null" - el()'s null-skipping does not apply here.
    clear(box).append(...[
      thumb,
      el('b', { text: current ? current : label }),
      el('div', { class: 'hint', text: current ? 'Uploaded' : hint }),
      el('button', { class: 'btn sm', style: 'margin-top:10px',
        text: current ? 'Replace' : 'Choose file', onclick: () => input.click() }),
      current ? el('button', { class: 'btn sm ghost', style: 'margin-top:10px',
        text: 'Remove', onclick: () => removeFile(app.id, slot) }) : null,
    ].filter(Boolean));
  };

  input.addEventListener('change', async () => {
    const file = input.files[0];
    if (!file) return;
    // Cleared so that picking the same file again after a failure still
    // counts as a change - otherwise a retry does nothing at all.
    input.value = '';
    // The box becomes the progress bar for the duration; route() repaints the
    // whole page on success, render() restores this box on failure.
    const fill = el('i');
    const bar = el('div', { class: 'upbar' }, [fill]);
    const amount = el('div', { class: 'hint', text: 'Starting…' });
    clear(box).append(
      el('b', { text: file.name }),
      bar,
      amount,
    );
    const show = (fraction) => {
      if (fraction === null) {
        // Some browsers cannot measure the total for a raw body: show motion
        // rather than a number that would be a lie.
        bar.classList.add('unknown');
        amount.textContent = 'Uploading…';
        return;
      }
      const percent = Math.round(fraction * 100);
      fill.style.width = percent + '%';
      amount.textContent = percent >= 100
        ? 'Finishing up…'
        : `${percent}% of ${megabytes(file.size)}`;
    };
    show(0);

    try {
      const updated = await upload(app.id, slot, file, show);
      // The draft outlives this page, and a stale file field in it would
      // ride along on the next save. The server ignores these fields on
      // save too - this keeps the copy the UI shows truthful.
      if (updated && state.draft && state.draft.id === app.id) {
        state.draft[slot + '_file'] = updated[slot + '_file'];
      }
      toast(`${label} uploaded`);
      route();
    } catch (error) {
      toast(error.message, true);
      render();
    }
  });

  render();
  return el('div', {}, [box, input]);
}

async function removeFile(appId, slot) {
  try {
    await api('DELETE', `/api/apps/${appId}/files/${slot}`);
    if (state.draft && state.draft.id === appId) {
      state.draft[slot + '_file'] = null;
      if (slot === 'keystore') state.draft.key_alias = null;
    }
    toast('Removed');
    route();
  } catch (error) { toast(error.message, true); }
}

/* ── the studio: modules, theme and navigation, with a live preview ──────
 *
 * Laid out like a studio rather than a form: the module library on the left,
 * the phone in the middle, the knobs for what is selected on the right. The
 * library writes the same draft.features the Features page reads, so the two
 * can never disagree.
 */

// Tiles that toggle a feature by name. Everything else on the library is
// either always in the app or explicitly not built yet.
const STUDIO_MODULES = [
  ['share', 'Native sharing', "The phone's share sheet on every page"],
  ['refresh', 'Pull to refresh', 'Swipe down to reload'],
  ['upload_file', 'File upload', 'Lets the site open the file picker'],
  ['photo_camera', 'Camera', 'Adds a permission prompt'],
  ['location_on', 'Location', 'Adds a permission prompt'],
  ['link', 'Deep links', 'Links to your domain open the app'],
];

const STUDIO_SOON = [
  ['qr_code_scanner', 'QR scanner'],
  ['fingerprint', 'Biometric lock'],
  ['contact_page', 'Contact sheet'],
];

function studioPage(app, draft, markDirty) {
  // The tabs get edited in place, so they must not share objects with the
  // saved app - a discarded draft would otherwise still have changed it.
  // The match list is an array inside the tab, so a shallow copy would leave
  // the draft and the saved app sharing it - and editing a discarded draft
  // would still have changed the app.
  draft.nav_tabs = (draft.nav_tabs || []).map((tab) => ({
    ...tab, match: [...(tab.match || [])],
  }));

  /* The site's own address, with a slash on the end.
   *
   * A tab's link starts here, so the box opens ready to have "shop" typed onto
   * it. A placeholder would have said the same thing and then vanished at the
   * first keystroke, leaving the question of whether a path or a full address
   * was wanted unanswered at exactly the moment it was being asked. */
  function homeLink() {
    return `${(draft.website_url || '').replace(/\/+$/, '')}/`;
  }
  draft.nav_style = draft.nav_style || 'none';
  draft.features = [...(draft.features || [])];
  draft.hide_selectors = [...(draft.hide_selectors || [])];

  const library = el('div', { class: 'modlib' });

  /* The phone is a persistent shell: top chrome, a body slot, bottom nav.
   * Persistent because the body can hold the live website in an iframe, and
   * an iframe that gets detached and re-appended reloads - so re-renders
   * must repaint around it, never through it. */
  const pmTop = el('div');
  const pmSlot = el('div', { class: 'pm-slot' });
  const pmBottom = el('div');
  const preview = el('div', { class: 'phone-mock lg' }, [pmTop, pmSlot, pmBottom]);
  const tabsBox = el('div');
  const tabsField = el('div', { class: 'field' });

  const accent = () => (
    /^#[0-9a-f]{6}$/i.test(draft.theme_color || '') ? draft.theme_color : '#607d8b');
  const tint = () => accent() + '26';
  const has = (name) => draft.features.includes(name);

  const setFeature = (name, on) => {
    const current = new Set(draft.features);
    if (on) current.add(name); else current.delete(name);
    draft.features = [...current];
    markDirty(); renderLibrary(); renderPreview();
  };

  /* the library */

  function modTile(glyph, name, blurb, on, toggle, extra = '') {
    return el('button', {
      class: 'modtile' + (on ? ' on' : '') + extra,
      onclick: toggle,
      type: 'button',
    }, [
      el('span', { class: 'material-symbols-outlined', text: glyph }),
      el('span', { class: 'modtxt' }, [
        el('b', { text: name }),
        blurb ? el('span', { class: 'hint', text: blurb }) : null,
      ]),
    ]);
  }

  function renderLibrary() {
    clear(library).append(
      el('div', { class: 'modgroup', text: 'In every app' }),
      modTile('language', 'Website', 'Your site, full screen', true, null, ' locked'),
      modTile('wallpaper', 'Splash screen', 'Set it under Branding', true, null, ' locked'),
      modTile('palette', 'Theme colour', 'Set on the right', true, null, ' locked'),

      el('div', { class: 'modgroup', text: 'Modules' }),
      modTile('menu', 'Bottom navigation', 'Native tabs along the bottom',
        draft.nav_style === 'bottom', () => setNav(draft.nav_style !== 'bottom')),
      modTile('cloud_off', 'Offline screens', 'Branded screens when the connection fails',
        Boolean(draft.offline_fallback_enabled), () => {
          draft.offline_fallback_enabled = !draft.offline_fallback_enabled;
          if (!draft.offline_fallback_enabled) stageMode = 'app';
          markDirty(); renderLibrary(); renderOfflineCard(); renderPreview();
        }),
      ...STUDIO_MODULES.map(([glyph, name, blurb]) =>
        modTile(glyph, name, blurb, has(name), () => setFeature(name, !has(name)))),

      // Shown only once it applies, which is the one thing worth keeping from
      // the Features page this replaced - there it sat on screen permanently,
      // including for the apps it had nothing to do with.
      ...(has('Camera') || has('Location') ? [el('p', {
        class: 'hint', style: 'padding: 2px 6px 10px',
        text: 'Camera and Location need a reason shown to the user. iOS '
          + 'rejects builds without one, so a sensible default is written if '
          + 'you leave it blank.',
      })] : []),

      el('div', { class: 'modgroup', text: 'Coming soon' }),
      ...STUDIO_SOON.map(([glyph, name]) =>
        modTile(glyph, name, null, false, null, ' soon')),
    );
  }

  /* theme colour */
  const color = el('input', { type: 'color', class: 'colorwell', value: accent() });
  const hex = el('input', {
    class: 'input mono', style: 'max-width:130px',
    value: draft.theme_color || '', placeholder: '#01a6ff',
  });
  color.addEventListener('input', () => {
    hex.value = color.value;
    draft.theme_color = color.value;
    markDirty(); renderPreview();
  });
  hex.addEventListener('input', () => {
    draft.theme_color = hex.value.trim().toLowerCase();
    if (/^#[0-9a-f]{6}$/.test(draft.theme_color)) color.value = draft.theme_color;
    markDirty(); renderPreview();
  });

  /* navigation style - settable from the select and from the library tile */
  const styleSel = el('select', { class: 'input' }, [
    el('option', { value: 'none', text: 'No navigation - just the website',
      selected: draft.nav_style !== 'bottom' }),
    el('option', { value: 'bottom', text: 'Bottom navigation bar',
      selected: draft.nav_style === 'bottom' }),
  ]);

  function setNav(on) {
    draft.nav_style = on ? 'bottom' : 'none';
    styleSel.value = draft.nav_style;
    if (on && !draft.nav_tabs.length) {
      draft.nav_tabs = [
        { label: 'Home', icon: 'home', target: homeLink() },
        { label: 'Shop', icon: 'storefront', target: `${homeLink()}shop` },
      ];
    }
    markDirty(); renderLibrary(); renderTabs(); renderSiteNavCard(); renderPreview();
  }

  styleSel.addEventListener('change', () => setNav(styleSel.value === 'bottom'));

  function tabRow(tab, index) {
    const label = el('input', { class: 'input', value: tab.label || '', placeholder: 'Home' });
    label.addEventListener('input', () => {
      tab.label = label.value; markDirty(); renderPreview();
    });

    /* The icon, as a button showing the icon, and a searchable grid behind it.
     *
     * This was a <select> of names: you chose "menu book" and found out what it
     * looked like by watching the phone redraw, on the one screen whose whole
     * job is showing you what things look like. Drawing all of them instead was
     * worse - a hundred and twenty-six buttons per tab, five tabs, in a 300px
     * column. Closed by default is the only version of this that fits.
     */
    const grid = el('div', { class: 'iconpick' });
    const search = el('input', {
      class: 'input sm', type: 'search', placeholder: 'Search icons',
    });
    const drawer = el('div', { class: 'icondrawer', hidden: true }, [search, grid]);

    const chosen = () => tab.icon || 'home';
    const trigger = el('button', {
      class: 'icontrigger', type: 'button', title: 'Change icon',
      'aria-label': `Icon: ${chosen().replace(/_/g, ' ')}`,
      'aria-expanded': 'false',
      onclick: () => {
        drawer.hidden = !drawer.hidden;
        trigger.setAttribute('aria-expanded', String(!drawer.hidden));
        if (!drawer.hidden) { paintGrid(); search.focus(); }
      },
    }, [icon(chosen()), icon('chevron', 'gl caret')]);

    function paintGrid() {
      const term = search.value.trim().toLowerCase();
      const hit = NAV_ICONS.filter((name) => name.includes(term));
      clear(grid).append(...hit.map((name) => el('button', {
        class: 'iconopt' + (chosen() === name ? ' on' : ''),
        type: 'button',
        // The name still travels, as a tooltip and for a screen reader. It is
        // what the server stores and what a build error would quote back.
        title: name.replace(/_/g, ' '),
        'aria-label': name.replace(/_/g, ' '),
        'aria-pressed': chosen() === name ? 'true' : 'false',
        onclick: () => {
          tab.icon = name;
          markDirty();
          // Picking one is the end of the job, so the drawer closes behind it.
          drawer.hidden = true;
          trigger.setAttribute('aria-expanded', 'false');
          clear(trigger).append(icon(name), icon('chevron', 'gl caret'));
          trigger.setAttribute('aria-label', `Icon: ${name.replace(/_/g, ' ')}`);
          renderPreview();
        },
      }, [icon(name)])));
      if (!hit.length) {
        grid.append(el('p', { class: 'hint iconnone', text: `Nothing matches "${search.value.trim()}".` }));
      }
    }
    search.addEventListener('input', paintGrid);

    /* The link, in a box, always there. It was a dropdown you had to set to
     * "Website page…" before any box appeared, so the field for the link was
     * hidden behind a choice you had to make before knowing it existed. */
    const link = el('input', {
      class: 'input mono', value: tab.target || homeLink(),
      placeholder: homeLink(),
    });
    link.addEventListener('input', () => { tab.target = link.value; markDirty(); });

    /* Three dots in the corner. Remove used to be a word on the same line as
     * the label and the link, and three words on one row in a 300px column is
     * why both fields ended up about 28px wide. */
    const menu = el('div', { class: 'tabmenu', hidden: true }, [
      el('button', {
        class: 'tabmenuitem', type: 'button', text: 'Remove tab',
        onclick: () => {
          draft.nav_tabs.splice(index, 1);
          markDirty(); renderTabs(); renderPreview();
        },
      }),
    ]);
    const more = el('button', {
      class: 'tabmore', type: 'button', title: 'More', 'aria-label': 'More',
      onclick: (event) => {
        event.stopPropagation();
        for (const other of document.querySelectorAll('.tabmenu')) {
          if (other !== menu) other.hidden = true;
        }
        menu.hidden = !menu.hidden;
      },
    }, [icon('more_vert')]);

    // Icon and name on one line, the link under it, the dots in the corner.
    const top = el('div', { class: 'tabtop' }, [
      trigger, label, el('div', { class: 'tabcorner' }, [more, menu]),
    ]);
    return el('div', { class: 'tabcard' }, [top, drawer, link]);
  }

  function renderTabs() {
    clear(tabsField);
    if (draft.nav_style !== 'bottom') return;
    clear(tabsBox).append(...draft.nav_tabs.map(tabRow));
    // Native append() again - a bare null here would render as "null".
    tabsField.append(...[
      el('label', { text: 'Tabs (2-5)' }),
      tabsBox,
      draft.nav_tabs.length < 5 ? el('button', {
        class: 'btn sm', text: '+ Add tab',
        onclick: () => {
          draft.nav_tabs.push({ label: '', icon: 'article', target: homeLink() });
          markDirty(); renderTabs(); renderPreview();
        },
      }) : null,
      el('p', { class: 'hint',
        text: 'Each tab opens a page of your website - a path like /shop, or a full address.' }),
    ].filter(Boolean));
  }

  /* the website's own navigation - hidden, so the native bar is not a second one */

  const siteNavCard = el('section', { class: 'group' });

  function selectorRows() {
    const list = draft.hide_selectors;
    const rows = list.map((value, index) => {
      const input = el('input', {
        class: 'input mono', value, placeholder: '.mobile-bottom-nav',
      });
      input.addEventListener('input', () => {
        list[index] = input.value; markDirty();
      });
      return el('div', { class: 'selrow' }, [
        input,
        el('button', {
          class: 'btn sm ghost', text: 'Remove', title: 'Stop hiding this',
          onclick: () => {
            list.splice(index, 1); markDirty(); renderSiteNavCard();
          },
        }),
      ]);
    });
    // Typing in the blank row grows another one, the way the tab editor works.
    // No separate Add button for something most apps use once or twice.
    const blank = el('input', {
      class: 'input mono', placeholder: 'Add a selector, e.g. .mobile-bottom-nav',
    });
    blank.addEventListener('change', () => {
      const value = blank.value.trim();
      if (!value) return;
      list.push(value); markDirty(); renderSiteNavCard();
    });
    rows.push(el('div', { class: 'selrow' }, [blank]));
    return rows;
  }

  function renderSiteNavCard() {
    // Only fires in the state that is actually wrong: a native bar with
    // nothing hiding the website's. An app in Mode A has one bar and is fine.
    const hiding = draft.hide_selectors.length || draft.body_class || draft.url_flag;
    const warn = draft.nav_style === 'bottom' && !hiding
      ? el('div', { class: 'banner warn' }, [
          el('b', { text: 'Two navigation bars' }),
          'This app draws a native bottom bar, and the website draws its own '
            + 'above it. On a phone that is a lot of the screen spent twice. '
            + "Name the website's bar below and the app will hide it.",
        ])
      : null;

    const bodyClass = el('input', {
      class: 'input mono', value: draft.body_class || '',
      placeholder: 'web2app-native',
    });
    bodyClass.addEventListener('input', () => {
      draft.body_class = bodyClass.value; markDirty();
    });

    const flag = el('input', {
      class: 'input mono', value: draft.url_flag || '',
      placeholder: 'source=web2app',
    });
    flag.addEventListener('input', () => {
      draft.url_flag = flag.value; markDirty();
    });

    clear(siteNavCard).append(...[
      el('h3', { class: 'group-title', text: 'Website navigation' }),
      warn,
      el('div', { class: 'field' }, [
        el('label', { text: 'Hide these elements' }),
        ...selectorRows(),
        el('p', { class: 'hint',
          text: 'CSS selectors, written the way you would in a stylesheet. '
            + 'Hidden inside the app only - your website is untouched in a browser.' }),
      ]),
      field('Body class', bodyClass,
        'Added to <body> inside the app, so your own stylesheet can hide things. '
          + 'Survives a redesign in a way selectors do not, but you need to own the CSS.'),
      field('Entry URL flag', flag,
        'Appended to the home URL and to navigation tabs. It does not survive the '
          + 'first link the visitor clicks, so treat it as a hint about how the '
          + 'session started, not a reliable signal.'),
    ].filter(Boolean));
  }

  /* the offline screen - the built-in one, or the developer's own HTML */

  const offlineCard = el('section', { class: 'group' });
  // What the phone in the middle is showing: the app, or the offline screen.
  // Set by the offline editor on the right - previewing a screen you are
  // editing is what that button is for - rather than by a pair of tabs sitting
  // permanently above the phone for a mode most visits never look at.
  let stageMode = 'app';
  let previewTimer = null;

  // Starter templates served alongside the client. The canonical copies live
  // in examples/ at the repo root; these are the ones the browser can reach.
  const OFFLINE_TEMPLATES = [
    ['Illustrated', '/offline-templates/illustrated.html'],
    ['Simple', '/offline-templates/simple.html'],
  ];

  function renderOfflineCard() {
    clear(offlineCard).append(
      el('h3', { class: 'group-title', text: 'Offline' }),
      // Both switches used to live on a page of their own in the sidebar,
      // whose main content was a button sending you here. The preview cannot
      // move, so the page was the half that could.
      checkbox('Cache pages for faster loading', draft.cache_enabled,
        (on) => { draft.cache_enabled = on; markDirty(); }),
      checkbox('Show a branded screen when a page fails to load',
        draft.offline_fallback_enabled,
        (on) => {
          draft.offline_fallback_enabled = on;
          markDirty(); renderLibrary(); renderOfflineCard(); renderPreview();
        },
        'Replaces the browser error page with one that offers Try again and Go home.'),
    );

    if (!draft.offline_fallback_enabled) return;

    const custom = Boolean((draft.offline_custom_html || '').trim());
    const mode = el('select', { class: 'input' }, [
      el('option', { value: 'builtin', text: 'Built-in - matches the theme colour',
        selected: !custom }),
      el('option', { value: 'custom', text: 'My own HTML', selected: custom }),
    ]);

    const editorBox = el('div', { style: custom ? '' : 'display:none' });
    const editor = el('textarea', {
      class: 'input mono htmlbox', rows: 12, spellcheck: false,
      placeholder: '<!doctype html>\n<html>…',
    });
    editor.value = draft.offline_custom_html || '';
    editor.addEventListener('input', () => {
      draft.offline_custom_html = editor.value;
      markDirty();
      // Redrawing the iframe on every keystroke makes typing stutter.
      clearTimeout(previewTimer);
      previewTimer = setTimeout(renderPreview, 400);
    });
    editor.addEventListener('focus', () => {
      if (stageMode !== 'offline') { stageMode = 'offline'; renderPreview(); }
    });

    const useTemplate = (name, url) => async () => {
      if (editor.value.trim() &&
          !confirm(`Replace what is in the editor with the ${name} template?`)) return;
      try {
        const response = await fetch(url);
        if (!response.ok) throw new Error();
        editor.value = await response.text();
        draft.offline_custom_html = editor.value;
        stageMode = 'offline';
        markDirty(); renderPreview();
      } catch {
        toast('Could not load the template.', true);
      }
    };

    editorBox.append(
      el('div', { class: 'tplrow' }, [
        el('span', { class: 'hint', text: 'Start from:' }),
        ...OFFLINE_TEMPLATES.map(([name, url]) =>
          el('button', { class: 'btn sm', text: name, onclick: useTemplate(name, url) })),
      ]),
      editor,
      el('p', { class: 'hint',
        text: 'Must be a single self-contained file - it shows precisely when '
          + 'there is no internet, so nothing can load from the web. A link to '
          + 'app://retry becomes the Try-again button, app://home goes to the '
          + 'start page, and your theme colour arrives as the --accent CSS '
          + 'variable.' }),
    );

    mode.addEventListener('change', () => {
      const wantsCustom = mode.value === 'custom';
      if (!wantsCustom && editor.value.trim()) {
        // The pasted HTML is the only copy; losing it to a mis-click would
        // be brutal.
        if (!confirm('Go back to the built-in screen and discard your HTML?')) {
          mode.value = 'custom';
          return;
        }
        draft.offline_custom_html = '';
        editor.value = '';
        markDirty();
      }
      editorBox.style.display = wantsCustom ? '' : 'none';
      stageMode = 'offline';
      renderPreview();
    });

    offlineCard.append(field('Design', mode), editorBox);
  }

  /* what the phone shows when it is showing the offline screen */

  function offlineMock() {
    const html = (draft.offline_custom_html || '').trim();
    if (!html) {
      // The built-in Material screen, as _ErrorView renders it in the app.
      return el('div', { class: 'pm-offline' }, [
        el('span', { class: 'material-symbols-outlined pm-off-icon', text: 'wifi_off' }),
        el('b', { text: 'You appear to be offline' }),
        el('span', { class: 'hint', text: 'Check your internet connection and try again.' }),
        el('span', { class: 'pm-off-btn', style: `background:${accent()}` }, [
          el('span', { class: 'material-symbols-outlined', text: 'replay' }),
          'Try again',
        ]),
        el('span', { class: 'pm-off-link', style: `color:${accent()}`, text: 'Go to home page' }),
      ]);
    }
    // Sandboxed: scripts may run so the page previews honestly, but app://
    // links and anything else that navigates goes nowhere. The injected
    // script mirrors what the built app does with the theme colour.
    const inject = [
      '<script>',
      /^#[0-9a-f]{6}$/i.test(draft.theme_color || '')
        ? `document.documentElement.style.setProperty('--accent', '${draft.theme_color}');`
        : '',
      'document.addEventListener("click", (e) => {',
      '  const a = e.target.closest && e.target.closest("a[href]");',
      '  if (a) e.preventDefault();',
      '}, true);',
      '<\/script>',
    ].join('\n');
    const frame = el('iframe', { class: 'pm-frame', sandbox: 'allow-scripts' });
    frame.srcdoc = html + inject;
    return frame;
  }

  function renderPreview() {
    const tabs = draft.nav_style === 'bottom' ? draft.nav_tabs : [];
    const hasNav = tabs.length > 0;
    const actions = [];
    if (hasNav && has('Native sharing')) actions.push('share');

    if (!draft.offline_fallback_enabled) stageMode = 'app';

    const statusIcons = stageMode === 'offline'
      ? ['signal_cellular_alt', 'battery_full']
      : ['signal_cellular_alt', 'wifi', 'battery_full'];
    clear(pmTop).append(
      el('div', { class: 'pm-status' }, [
        el('span', { text: '9:41' }),
        el('span', { class: 'pm-sicons' }, statusIcons
          .map((glyph) => el('span', { class: 'material-symbols-outlined', text: glyph }))),
      ]),
    );

    if (stageMode === 'offline') {
      clear(pmBottom);
      clear(pmSlot).append(offlineMock());
      return;
    }

    // Mirrors the built app: the top bar only exists when a module puts a
    // button in it. Otherwise the website runs edge to edge.
    if (actions.length) {
      pmTop.append(
        el('div', { class: 'pm-appbar' }, [
          el('span', { class: 'pm-title', text: draft.app_name || draft.name || 'App' }),
          el('span', { class: 'pm-abx' }, actions.map((glyph) =>
            el('span', { class: 'material-symbols-outlined', text: glyph }))),
        ]),
      );
    }

    // The real website inside the phone. Left alone when the URL has not
    // changed - touching the iframe means reloading the site.
    const site = (draft.website_url || '').trim();
    if (/^https?:\/\//i.test(site)) {
      const current = pmSlot.querySelector('.pm-site');
      if (!(current && current.getAttribute('data-site') === site)) {
        // Rendered at a real phone's viewport width and scaled down to the
        // mock - laid out at the mock's actual width, sites look zoomed in.
        clear(pmSlot).append(el('div', { class: 'pm-scale' }, [
          el('iframe', {
            class: 'pm-site', src: site, 'data-site': site,
            sandbox: 'allow-scripts allow-same-origin allow-forms',
          }),
        ]));
      }
    } else {
      clear(pmSlot).append(el('div', { class: 'pm-body' }, [
        el('div', { class: 'pm-hero', style: `background:${tint()}` }, [
          el('span', { class: 'pm-hero-dot', style: `background:${accent()}` }),
        ]),
        el('div', { class: 'pm-line' }),
        el('div', { class: 'pm-line short' }),
        el('div', { class: 'pm-cards' }, [
          el('div', { class: 'pm-card' }, [
            el('div', { class: 'pm-line', style: 'width:70%' }),
            el('div', { class: 'pm-line short' }),
          ]),
          el('div', { class: 'pm-card' }, [
            el('div', { class: 'pm-line', style: 'width:60%' }),
            el('div', { class: 'pm-line short' }),
          ]),
        ]),
        el('div', { class: 'pm-chip', style: `background:${accent()}` }),
      ]));
    }

    clear(pmBottom);
    if (hasNav) {
      pmBottom.append(el('div', { class: 'pm-nav' }, tabs.map((tab, i) =>
        el('span', { class: 'pm-item' + (i === 0 ? ' on' : '') }, [
          el('span', {
            class: 'material-symbols-outlined', text: tab.icon || 'public',
            style: i === 0 ? `color:${accent()};background:${tint()}` : '',
          }),
          el('span', { text: tab.label || '·' }),
        ]))));
    }
  }

  renderLibrary();
  renderTabs();
  renderSiteNavCard();
  renderOfflineCard();
  renderPreview();

  return el('div', { class: 'studio-full' }, [
    el('section', { class: 'group modlib-card' }, [
      el('h3', { class: 'group-title', text: 'Modules' }),
      library,
    ]),
    el('div', { class: 'studio-stage' }, [
      preview,
    ]),
    el('div', { class: 'studio-side' }, [
      el('section', { class: 'group' }, [
        el('h3', { class: 'group-title', text: 'Appearance' }),
        field('Theme colour',
          el('div', { class: 'colorrow' }, [color, hex]),
          "The app's accent - buttons, highlights, the active tab. Use the website's brand colour."),
        field('Navigation', styleSel,
          'A bottom bar with a native top app bar.'),
        tabsField,
      ]),
      siteNavCard,
      offlineCard,
    ]),
  ]);
}

/* ── notifications ───────────────────────────────────────────────────────
 *
 * Numbered cards on one page rather than a wizard. A wizard would be a second
 * navigation model inside a product that already has one, and it hides the
 * shape of the job from somebody deciding whether to start. Here the whole
 * thing is one scroll, the steps can be done in any order, and nothing traps
 * anyone on step three of six.
 *
 * The Firebase setup spans three parties - us, Google and Apple - so each card
 * carries its own state rather than the page carrying one.
 */

const PUSH_FOREGROUND = [
  ['notification', 'Show a notification',
    'The same banner as when the app is closed. Predictable.'],
  ['banner', 'Show a bar inside the app',
    'Quieter. Good for anything that arrives often.'],
  ['silent', 'Nothing visible',
    'Recorded but not shown. For silent updates.'],
];

function stepHead(number, title, done, aside) {
  return el('div', { class: 'grouphead' }, [
    el('span', { class: 'stepnum' + (done ? ' done' : '') },
      [done ? '✓' : String(number)]),
    el('h3', { class: 'group-title', text: title }),
    aside ? el('span', { class: 'steppill', text: aside }) : null,
  ]);
}

function copyRow(value) {
  const input = el('input', { class: 'input mono', value, disabled: 'disabled' });
  return el('div', { class: 'copyrow' }, [
    input,
    el('button', {
      class: 'btn sm', text: 'Copy',
      onclick: async () => {
        try {
          await navigator.clipboard.writeText(value);
          toast('Copied');
        } catch (error) {
          // Clipboard access is refused outside a secure context, which is
          // exactly where this product runs during development.
          input.removeAttribute('disabled');
          input.select();
          toast('Press Ctrl+C to copy');
        }
      },
    }),
  ]);
}

function notificationsSection(app, draft, markDirty) {
  draft.push_topics = (draft.push_topics || []).map((t) => ({ ...t }));

  const wrap = el('div');
  const status = app.firebase || {};

  const render = () => {
    const on = !!draft.push_enabled;
    const android = status.android;
    const ios = status.ios;
    const androidOk = !!(android && android.ok);

    clear(wrap).append(...[

      /* ── on or off ── */
      el('section', { class: 'group' }, [
        el('h3', { class: 'group-title', text: 'Push notifications' }),
        toggleRow(
          'Push notifications',
          'Adds Firebase to the app, and a permission prompt your users see.',
          on,
          (want) => { draft.push_enabled = want; markDirty(); render(); },
        ),
        on ? null : el('p', { class: 'hint', style: 'margin-top:10px' }, [
          'Your users get told when something happens on your site, even with '
          + 'the app closed. You will need a free Firebase project on your own '
          + 'Google account - ',
          el('a', { href: '/docs/notifications', text: 'how push works' }),
          '.',
        ]),
      ]),

      /* everything below only matters once it is on */
      ...(!on ? [] : [

        /* ── 1 identifiers ── */
        el('section', { class: 'group' }, [
          stepHead(1, '1 Your app’s identifiers', true),
          el('div', { class: 'banner warn' }, [
            el('b', { text: 'Do not change these after you publish' }),
            'Firebase, Google Play and the App Store all key a listing to its '
            + 'identifier. Changing one later means a new listing, not an update.',
          ]),
          field('Android package name', copyRow(app.android_package_id)),
          field('iOS bundle ID', copyRow(app.ios_bundle_id)),
        ]),

        /* ── 2 the project ── */
        el('section', { class: 'group' }, [
          stepHead(2, '2 Create your Firebase project', androidOk),
          el('p', { class: 'sub', style: 'margin-bottom:14px',
            text: 'The project is yours, on your own Google account. We never ask '
              + 'for your password and cannot see inside it. If you leave Web2App, '
              + 'it stays with you.' }),
          androidOk
            ? el('div', { class: 'banner ok' }, [
                el('b', { text: `Connected · ${android.project_id}` }),
                'Read from the configuration file you uploaded below.',
              ])
            : null,
          el('a', { class: 'btn', href: 'https://console.firebase.google.com',
            target: '_blank', rel: 'noopener', text: 'Open Firebase Console' }),
        ]),

        /* ── 3 android ── */
        el('section', { class: 'group' }, [
          stepHead(3, '3 Android configuration', androidOk),
          firebaseState(android, app.android_package_id, 'package name'),
          uploadSlot(app, 'firebase_android', 'Upload google-services.json',
            'Add an Android app in Firebase using the package name above, then '
            + 'drop the file here.', '.json'),
        ]),

        /* ── 4 ios ── */
        el('section', { class: 'group' }, [
          stepHead(4, '4 iOS configuration', !!(ios && ios.ok),
            'optional for Android'),
          firebaseState(ios, app.ios_bundle_id, 'bundle ID'),
          uploadSlot(app, 'firebase_ios', 'Upload GoogleService-Info.plist',
            'Add an iOS app in Firebase using the bundle ID above, then drop '
            + 'the file here.', '.plist'),
        ]),

        /* ── 5 apple ── */
        el('section', { class: 'group' }, [
          stepHead(5, '5 Apple push key', false, 'iPhone only'),
          el('p', { class: 'sub', style: 'margin-bottom:12px' }, [
            'Five steps in your Apple and Firebase accounts. The key never comes '
            + 'to us - you upload it straight to Firebase, which is both safer '
            + 'and one fewer thing to trust us with. ',
            el('a', { href: '/docs/notifications-ios', text: 'Walk me through it' }),
            '.',
          ]),
        ]),

        topicsCard(draft, markDirty, render),
        behaviourCard(draft, markDirty, render),
        promptCard(draft, markDirty),
        sendingCard(app, draft, markDirty),
        testCard(app, status),
      ]),
    ].filter(Boolean));
  };

  render();
  return wrap;
}

function toggleRow(title, hint, on, change) {
  const knob = el('div', { class: 'sw' + (on ? ' on' : '') });
  return el('button', {
    class: 'toprow', style: 'width:100%;background:none;border:0;font:inherit;'
      + 'text-align:left;cursor:pointer;padding:10px 0',
    onclick: () => change(!on),
  }, [
    el('div', {}, [
      el('div', { class: 'tl', text: title }),
      el('div', { class: 'th', text: hint }),
    ]),
    knob,
  ]);
}

/* What an uploaded Firebase file says, and whether it still fits. Recomputed
 * by the server on every read, so a package name changed after the upload
 * shows up here rather than three minutes into a build. */
function firebaseState(entry, expected, what) {
  if (!entry) return null;
  if (entry.ok) {
    return el('div', { class: 'banner ok' }, [
      el('b', { text: 'Valid, and it matches this app' }),
      `Project ${entry.project_id} · ${what} ${expected}`,
    ]);
  }
  return el('div', { class: 'banner err' }, [
    el('b', { text: 'This file does not match this app' }),
    entry.problem || 'Upload the file for this app.',
  ]);
}

function topicsCard(draft, markDirty, rerender) {
  const rows = draft.push_topics.map((topic, index) => {
    const label = el('input', { class: 'input', value: topic.label || '',
      placeholder: 'Order updates' });
    label.addEventListener('input', () => {
      topic.label = label.value; markDirty();
    });
    const id = el('input', { class: 'input mono', value: topic.id || '',
      placeholder: 'orders' });
    id.addEventListener('input', () => { topic.id = id.value; markDirty(); });

    const on = el('div', { class: 'sw' + (topic.default ? ' on' : '') });
    const toggle = el('button', {
      class: 'btn sm ghost', title: 'On by default for a new install',
      onclick: () => { topic.default = !topic.default; markDirty(); rerender(); },
    }, [on]);

    return el('div', { class: 'tabedit' }, [
      label, id, toggle,
      el('button', { class: 'btn sm ghost', text: 'Remove',
        onclick: () => {
          draft.push_topics.splice(index, 1); markDirty(); rerender();
        } }),
    ]);
  });

  return el('section', { class: 'group' }, [
    stepHead(6, '6 Categories', draft.push_topics.length > 0),
    el('p', { class: 'sub', style: 'margin-bottom:12px',
      text: 'Your users switch these on and off inside the app. Send to a '
        + 'category and Firebase delivers it to everyone subscribed - you never '
        + 'handle a list of devices.' }),
    ...rows,
    draft.push_topics.length < 12 ? el('button', {
      class: 'btn sm', text: '+ Add a category',
      onclick: () => {
        draft.push_topics.push({ id: '', label: '', default: true });
        markDirty(); rerender();
      },
    }) : null,
    el('p', { class: 'hint' }, [
      'Name, then the topic your backend sends to, then whether it is on for a '
      + 'new install. ',
      el('b', { text: 'The topic cannot be changed once your app is published' }),
      ' - installs already subscribed would silently stop receiving.',
    ]),
  ].filter(Boolean));
}

function behaviourCard(draft, markDirty, rerender) {
  const current = draft.push_foreground || 'notification';
  return el('section', { class: 'group' }, [
    stepHead(7, '7 While the app is open', true),
    el('p', { class: 'sub', style: 'margin-bottom:12px',
      text: 'Android and iPhone both hand a message to the app instead of '
        + 'showing it when the app is in front, so this is your choice.' }),
    el('div', { class: 'radios' }, PUSH_FOREGROUND.map(([value, title, blurb]) =>
      el('button', {
        class: 'radiocard' + (current === value ? ' on' : ''),
        onclick: () => {
          draft.push_foreground = value; markDirty(); rerender();
        },
      }, [
        el('span', { class: 'rd' }),
        el('span', {}, [
          el('b', { text: title }),
          el('span', { text: blurb }),
        ]),
      ]))),
  ]);
}

function promptCard(draft, markDirty) {
  const title = el('input', { class: 'input',
    value: draft.push_prompt_title || '', placeholder: 'Stay updated' });
  title.addEventListener('input', () => {
    draft.push_prompt_title = title.value; markDirty();
  });
  const body = el('textarea', { class: 'input', rows: '3',
    placeholder: 'Get told when your order is ready.' });
  body.value = draft.push_prompt_body || '';
  body.addEventListener('input', () => {
    draft.push_prompt_body = body.value; markDirty();
  });

  return el('section', { class: 'group' }, [
    stepHead(8, '8 How the app asks', true),
    field('Heading', title),
    field('Explanation', body,
      'Shown before the phone’s own prompt. Say what people get, not that '
      + 'you would like permission.'),
    el('div', { class: 'banner info' }, [
      el('b', { text: 'Asked on the second visit, not on first launch' }),
      'Somebody who has not seen your app yet has no reason to say yes, and on '
      + 'both platforms a refusal can only be undone by the user in system '
      + 'settings.',
    ]),
  ]);
}

/* The generated integration guide. Values come from the server rather than
 * being assembled here, because the project id is read out of the uploaded
 * configuration file - so the guide cannot describe a project the app is not
 * actually built against. */
function sendingCard(app, draft, markDirty) {
  const modes = [
    ['console', 'From the Firebase Console',
      'Type a message, press send. Nothing to build. Good for announcements.'],
    ['backend', 'From my own website or backend',
      'Your server sends when something happens - an order is ready, a result '
      + 'is published. Web2App is not involved and does not need to be running.'],
  ];
  let mode = draft.push_token_endpoint ? 'backend' : 'backend';
  let stack = 'node';

  const picker = el('div', { class: 'stackpick' });
  const codeBox = el('div');
  const endpointField = el('div');
  const body = el('div');

  async function loadGuide() {
    clear(codeBox).append(el('p', { class: 'hint', text: 'Loading…' }));
    try {
      const data = await api(
        'GET', `/api/apps/${app.id}/push-docs?stack=${encodeURIComponent(stack)}`);
      clear(picker).append(...data.stacks.map((s) =>
        el('button', {
          class: 'btn sm' + (s.id === stack ? ' on' : ''),
          text: s.label,
          onclick: () => { stack = s.id; loadGuide(); },
        })));
      const guide = data.guide;
      clear(codeBox).append(...[
        data.configured ? null : el('div', { class: 'banner warn' }, [
          el('b', { text: 'Upload your Firebase file first' }),
          'Until then this guide shows a placeholder project id.',
        ]),
        guide.install ? el('div', { class: 'field' }, [
          el('label', { text: 'Install' }),
          el('pre', { class: 'console', style: 'max-height:none',
            text: guide.install }),
        ]) : null,
        el('div', { class: 'banner info' }, [
          el('b', { text: 'Your values are already in it' }),
          'Nothing below is a placeholder. Copy it into your project as it is.',
        ]),
        el('pre', { class: 'console', style: 'max-height:none', text: guide.code }),
        el('button', {
          class: 'btn sm', style: 'margin-top:10px', text: 'Copy',
          onclick: async () => {
            try {
              await navigator.clipboard.writeText(guide.code);
              toast('Copied');
            } catch (error) {
              toast('Select the code and press Ctrl+C', true);
            }
          },
        }),
        ...guide.notes.map((note) =>
          el('p', { class: 'hint', style: 'margin-top:10px', text: note })),
      ].filter(Boolean));
    } catch (error) {
      clear(codeBox).append(
        el('div', { class: 'banner err' }, [
          el('b', { text: 'Could not build the guide' }), error.message,
        ]));
    }
  }

  function renderBody() {
    if (mode === 'console') {
      clear(body).append(
        el('p', { class: 'sub' }, [
          'In your Firebase project, open Messaging and compose a notification. '
          + 'Add ', el('code', { text: 'action=open_url' }), ' and ',
          el('code', { text: 'url=/offers' }),
          ' under custom data to make it open a page. ',
          el('a', { href: '/docs/notifications-sending', text: 'More' }), '.',
        ]),
        el('a', { class: 'btn', href: 'https://console.firebase.google.com',
          target: '_blank', rel: 'noopener', text: 'Open Firebase Console' }),
      );
      return;
    }
    clear(body).append(
      el('div', { class: 'field' }, [
        el('label', { text: 'What does your backend use?' }), picker,
      ]),
      codeBox,
      endpointField,
    );
    loadGuide();
  }

  const endpoint = el('input', { class: 'input mono',
    value: draft.push_token_endpoint || '',
    placeholder: 'https://yoursite.example/api/push-token' });
  endpoint.addEventListener('input', () => {
    draft.push_token_endpoint = endpoint.value; markDirty();
  });
  clear(endpointField).append(
    el('h3', { class: 'group-title', style: 'margin-top:22px',
      text: 'Messages for one person' }),
    el('p', { class: 'sub' }, [
      'Categories are broadcasts. To reach one person your backend needs to '
      + 'know which device belongs to which user. The simplest way is to ask '
      + 'the app from a page where you already know who is signed in - ',
      el('a', { href: '/docs/notifications-sending', text: 'the two-line version' }),
      '. If your site cannot run that, the app can post the token here instead:',
    ]),
    field('Push token endpoint', endpoint,
      'Optional. Leave it empty and the app never phones anywhere.'),
  );

  const card = el('section', { class: 'group' }, [
    stepHead(10, '10 How will you send notifications?', true),
    el('div', { class: 'radios' }, modes.map(([value, title, blurb]) =>
      el('button', {
        class: 'radiocard' + (mode === value ? ' on' : ''),
        onclick: (event) => {
          mode = value;
          for (const node of card.querySelectorAll('.radiocard')) {
            node.classList.remove('on');
          }
          event.currentTarget.classList.add('on');
          renderBody();
        },
      }, [
        el('span', { class: 'rd' }),
        el('span', {}, [el('b', { text: title }), el('span', { text: blurb })]),
      ]))),
    el('div', { class: 'radiocard off' }, [
      el('span', { class: 'rd' }),
      el('span', {}, [
        el('b', { text: 'Through Web2App' }),
        el('span', { text: 'A managed sending API. Not built, and deliberately '
          + 'optional when it is - you should never need us online for your '
          + 'notifications to arrive.' }),
      ]),
    ]),
    body,
  ]);
  renderBody();
  return card;
}

function testCard(app, status) {
  const android = status.android;
  const ios = status.ios;
  const kv = (name, value, tone) => el('div', { class: 'kv' }, [
    el('span', { text: name }),
    el('span', { style: tone ? `color:var(--${tone})` : '', text: value }),
  ]);
  return el('section', { class: 'group' }, [
    stepHead(9, '9 Check it worked', false),
    kv('Android configuration',
      android ? (android.ok ? 'Valid' : 'Does not match') : 'Not uploaded',
      android && android.ok ? 'ok-ink' : 'ink-3'),
    kv('iOS configuration',
      ios ? (ios.ok ? 'Valid' : 'Does not match') : 'Not uploaded',
      ios && ios.ok ? 'ok-ink' : 'ink-3'),
    el('p', { class: 'hint', style: 'margin-top:12px' }, [
      'Build the app, install it, and open it twice. Then send yourself one '
      + 'from Firebase’s message composer. ',
      el('a', { href: '/docs/notifications-setup#checking',
        text: 'If it does not arrive' }),
      '.',
    ]),
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
    text: 'Passwords are never stored - you enter them each time you build. A leaked upload key cannot be revoked, so keep a backup somewhere safe.' }));

  return fieldset('signing', '', children);
}

function buildSection(app) {
  return fieldset('build', '', [
    el('p', { class: 'sub',
      text: 'Runs on this server. One build at a time, typically 3-5 minutes.' }),
    el('button', { class: 'btn primary', text: 'Build now', onclick: () => buildDialog(app) }),
    el('button', { class: 'btn', style: 'margin-left:8px',
      text: 'Generate project only', onclick: () => generateOnly(app) }),
    el('p', { class: 'hint', style: 'margin-top:12px',
      text: 'Generating writes the Flutter project without building it - the fastest way to get the iOS project onto a Mac.' }),
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

  // APK first: most people want something they can install on a phone right
  // now. The Play bundle is the choice you make when you are publishing.
  const output = el('select', { class: 'input' }, [
    el('option', { value: 'apk', text: 'APK - install on a phone' }),
    el('option', { value: 'aab', text: 'App bundle (.aab) - for Google Play' }),
  ]);
  const storePassword = el('input', { class: 'input', type: 'password' });
  const keyPassword = el('input', { class: 'input', type: 'password' });

  const body = [
    field('Output', output),
    el('div', { class: 'banner info' }, [
      el('b', { text: `Will build version ${app.version_name || '1.0.0'} (${app.next_version_code ?? app.version_code ?? 1})` }),
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
      el('b', { text: 'No keystore - this will use a debug key' }),
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
 * running - and without a URL of its own there was no way back to it. */
async function showBuild(appId, number) {
  const [{ app }, { build }] = await Promise.all([
    api('GET', '/api/apps/' + appId),
    api('GET', `/api/apps/${appId}/builds/${number}`),
  ]);

  state.app = app;
  state.section = null;
  state.dirty = false;
  renderSidebar();
  document.getElementById('crumb').textContent = `${app.name} - build #${build.number}`;
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
        clear(status).append(el('i', { class: 'dot' }), 'Lost connection - the build carries on');
      }
    });

    // Streaming has more places to go wrong than the rest of this put
    // together - a proxy that buffers, a browser that waits for a full buffer
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
      ? `Built in ${seconds(finished.duration)}`
      : `Failed after ${seconds(finished.duration)}`);

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
          href: artifactHref(app, finished.number, artifact),
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
    if (name.includes('arm64-v8a')) return 'most phones - start here';
    if (name.includes('armeabi-v7a')) return 'older 32-bit phones';
    if (name.includes('x86_64')) return 'emulators';
    return 'sideload & testing';
  }
  return { aab: 'for Google Play', zip: 'full Flutter project' }[kind] || kind || 'file';
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
      state.draft = null;
      state.dirty = false;
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

  // The simulator is an admin's testing tool. A customer just sees plans;
  // what happens when they try to pay is handled in payDialog.
  if (data.mode === 'demo' && data.user && data.user.is_admin) {
    content.append(el('div', { class: 'banner warn' }, [
      el('b', { text: 'Demo mode - no money moves (only admins see this)' }),
      'There is no Collecto account configured, so payments run against a ' +
      'simulator that answers like the real one: it can approve, decline, stall, ' +
      'drop a connection or return rubbish. Set CISSY_COLLECTO_USERNAME and ' +
      'CISSY_COLLECTO_KEY to go live. Customers can browse the plans, but ' +
      'trying to pay tells them payments are not switched on yet.',
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

  // Only what was actually paid for, and only for people who have paid. An
  // attempt nobody approved is not a thing a customer needs listed back at
  // them, and a reference is our key, not something they can use.
  const paid = data.payments.filter((payment) => payment.status === 'successful');
  if (paid.length) {
    content.append(
      el('h2', { class: 'sec', text: 'Receipts' }),
      el('table', { class: 'apps' }, [
        el('thead', {}, [el('tr', {},
          ['Plan', 'Amount', 'Paid'].map((h) => el('th', { text: h })))]),
        el('tbody', {}, paid.map((payment) =>
          el('tr', { class: 'row', onclick: () => go('#/billing/pay/' + payment.reference) }, [
            el('td', { text: payment.plan_name }),
            el('td', { text: money(payment.amount) }),
            el('td', { class: 'app-url', text: shortDate(payment.created_at) }),
          ]))),
      ]),
    );
  }
}


function subscriptionCard(user) {
  if (!user) return el('div', {});
  const left = `${user.builds_left ?? 0} of ${user.builds_limit ?? 0} builds left`;
  if (user.plan === 'trial') {
    return el('div', { class: 'banner info' }, [
      el('b', { text: 'Free trial' }),
      `${left}. Pick a plan below when you need more - you pay from your phone ` +
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
    el('b', { text: `${user.plan_name} - active` }),
    `${left}` + (user.plan_until ? `, renews by ${shortDate(user.plan_until)}.` : '.'),
  ]);
}

function payDialog(plan, mode) {
  // No gateway and not an admin: say so kindly, before asking for a number.
  // The server enforces the same rule, so this is honesty, not security.
  if (mode === 'demo' && !(state.user && state.user.is_admin)) {
    const close = openModal(
      'Payments are not open yet',
      '',
      [el('p', { class: 'sub', text:
        'Mobile money payments are not switched on for this server yet, so '
        + `the ${plan.name} plan cannot be bought right now. Nothing was `
        + 'charged. Contact support and we will activate a plan for you.' })],
      [el('button', { class: 'btn primary', text: 'OK', onclick: () => close() })],
    );
    return;
  }

  // Prefilled with the number the account was verified on, which is the one we
  // already know reaches this person. Most people will not have to type at all.
  const phone = phoneField(state.user && state.user.phone);
  const scenario = el('select', { class: 'input' }, [
    el('option', { value: 'approve', text: 'They approve it' }),
    el('option', { value: 'decline', text: 'They decline it' }),
    el('option', { value: 'silent', text: 'They never touch the prompt' }),
    el('option', { value: 'flaky', text: 'The connection drops once' }),
    el('option', { value: 'garbage', text: 'Collecto returns something that is not JSON' }),
  ]);

  const send = el('button', { class: 'btn primary', text: 'Send prompt' });

  const submit = async () => {
    if (send.disabled) return;
    // Dots only for as long as the app is the one working. The handset takes a
    // few seconds to buzz, and telling somebody to check a phone that has not
    // rung yet is how a working flow gets read as broken - so the modal holds
    // here until the send comes back, then hands over to the payment page.
    send.disabled = true;
    clear(send).append(el('span', { class: 'dots' },
      [0, 1, 2, 3, 4].map(() => el('i', {}))));
    try {
      const body = { plan: plan.id, phone: phone.value() };
      if (mode === 'demo') body.scenario = scenario.value;
      const { payment } = await api('POST', '/api/billing/pay', body);
      close();
      go('#/billing/pay/' + payment.reference);
    } catch (error) {
      send.disabled = false;
      clear(send).append('Send prompt');
      toast(error.message, true);
    }
  };
  send.addEventListener('click', submit);

  const close = openModal(
    `${plan.name} - ${money(plan.amount)}`,
    'You will get a prompt on your phone. Your PIN is entered there, never here.',
    [
      field('Mobile money number', phone.node, 'The number that will be charged.'),
      mode === 'demo'
        ? field('Demo: what the handset does', scenario,
            'Only in demo mode. The unhappy paths are the ones worth watching.')
        : null,
    ].filter(Boolean),
    [
      el('button', { class: 'btn ghost', text: 'Cancel', onclick: () => close() }),
      send,
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

    // A prompt nobody answered leaves nothing to look at and nothing to do.
    // Standing on a dead page reads as being stuck, so they go back to the one
    // screen where starting again is a single tap.
    // Nothing came back inside the two minutes. There is nothing on this page
    // to read and nothing to do, so they go where starting again is one tap.
    // Admins stay, because an unanswered payment is exactly the kind they get
    // asked about later.
    if (payment.status === 'abandoned' && !payment.trail) {
      stopBillingPoll();
      toast('That did not go through. Nothing was charged.');
      go('#/billing');
      return;
    }

    clear(head).append(
      el('h2', { class: 'sec', text: money(payment.amount) + ' · ' + payment.plan_name }),
      // Our lookup key, not theirs. Kept for admins, who quote it in support.
      payment.trail ? el('p', { class: 'sub mono', text: payment.reference }) : null,
      statusBanner(payment),
    );

    clear(body).append(
      el('div', { class: 'cols' }, [
        el('div', {}, [
          // The trail only arrives for admins. For everybody else it is the
          // gateway's own words about keys and IP addresses, which is not a
          // customer's business, so they get the same journey in plain steps.
          payment.trail
            ? el('div', { class: 'paycard' }, [
                el('h3', { class: 'paycard-h', text: 'What the server has done' }),
                el('div', { class: 'console' }, payment.trail.map((line) =>
                  el('div', { class: 'l', text: line }))),
              ])
            : el('div', { class: 'paycard' }, [
                el('h3', { class: 'paycard-h', text: 'Where this is' }),
                stepList(payment),
              ]),
        ]),
        el('div', {}, [
          data.prompt ? handsetPanel(data.prompt, payment) : null,
          payment.trail ? el('div', { class: 'paycard' }, [
            el('h3', { class: 'paycard-h', text: 'Details' }),
            kv('Status', payment.status),
            kv('Checks made', String(payment.checks ?? 0)),
            kv('Gateway id', payment.transaction_id || '-'),
            kv('Mode', payment.mode),
            payment.status === 'pending' && Number.isFinite(payment.expires_in)
              ? kv('Gives up in', Math.max(0, payment.expires_in) + 's')
              : null,
          ].filter(Boolean)) : null,
          // Only while there is something to check. On a settled payment it is
          // a button that asks a question already answered.
          payment.status === 'pending' ? el('button', {
            class: 'btn full',
            text: 'Check now',
            onclick: async () => {
              try {
                draw(await api('POST', `/api/billing/payments/${reference}/check`));
              } catch (error) { toast(error.message, true); }
            },
          }) : null,
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
      el('b', { text: 'No answer in time' }),
      'Nothing came back from the handset inside the two minutes, so the ' +
      'server stopped asking. Nothing was charged.',
    ]);
  }
  // Pending is the state worth designing. Nothing here is working on their
  // behalf - the payment moves when they enter a PIN, and a spinner would say
  // the opposite. So: what to do, where, and how long they have.
  const left = Number.isFinite(payment.expires_in) ? payment.expires_in : 0;
  return el('div', { class: 'banner info' }, [
    el('b', { text: 'Check your phone' }),
    `Enter your mobile money PIN on ${prettyPhone(payment.phone)} to pay ` +
    `${money(payment.amount)}.`,
    left > 0 ? el('div', { class: 'countdown' }, [
      el('b', { text: clock(left) }),
      ' left to approve. Safe to close this page - the server keeps checking.',
    ]) : null,
  ]);
}

/* The same journey the console shows an admin, in the three steps a customer
 * cares about. Which step is current is the whole point: the middle one is
 * where nothing moves until they act. */
function stepList(payment) {
  const paid = payment.status === 'successful';
  const over = payment.status === 'failed' || payment.status === 'abandoned';
  return el('div', { class: 'steps' }, [
    ['Prompt sent to your phone', 'done'],
    ['You enter your PIN', paid ? 'done' : over ? 'stopped' : 'now'],
    ['Plan switched on', paid ? 'done' : 'todo'],
  ].map(([label, state]) =>
    el('div', { class: 'step ' + state }, [el('i', { class: 'dot' }), label])));
}

function clock(seconds) {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

/* 256772145903 back into the form somebody recognises as their own number. */
function prettyPhone(msisdn) {
  const digits = String(msisdn || '').replace(/\D/g, '').replace(/^256/, '');
  if (digits.length !== 9) return msisdn || 'your phone';
  return '0' + digits.slice(0, 2) + ' ' + digits.slice(2, 5) + ' ' + digits.slice(5);
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
      ? el('p', { class: 'hint', text: 'Answered: ' + (prompt.status || 'done') })
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
        el('div', { class: 'app-url mono', text: (user.phone || '-')
          + (user.apps && user.apps.length ? ' · ' + user.apps.join(', ') : ' · no apps') }),
      ]),
      el('td', {}, [user.is_admin
        ? el('span', { class: 'pill ok' }, [el('i', { class: 'dot' }), 'Admin'])
        : el('span', { class: 'pill ' + (user.plan === 'trial' ? 'warn' : 'ok') },
            [el('i', { class: 'dot' }), user.plan_name])]),
      el('td', { text: `${user.builds_used ?? 0} / ${user.builds_limit ?? 0}` }),
      el('td', { text: megabytes(user.disk || 0) }),
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

const AUTH_ROUTES = ['#/login', '#/signup', '#/verify', '#/forgot', '#/reset'];

async function route() {
  const hash = location.hash || '#/';
  stopBillingPoll();
  try {
    // Signed out: only the auth screens exist, and login is the fallback for
    // anything else. Signed in: those screens are not ones you should be
    // looking at, so they bounce home.
    if (!state.user) {
      if (hash === '#/signup') { await showSignup(); return; }
      if (hash === '#/verify') { await showVerify(); return; }
      if (hash === '#/forgot') { await showForgot(); return; }
      if (hash === '#/reset') { await showReset(); return; }
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
    const app = hash.match(/^#\/app\/([^/]+)(?:\/([a-z]+))?\/?$/);
    if (app) await showAppPage(decodeURIComponent(app[1]), app[2] || 'overview');
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
  return Number.isNaN(date.getTime()) ? '-' : date.toLocaleString();
}

function seconds(value) {
  return Number.isFinite(value) ? `${Math.round(value)}s` : '-';
}

function megabytes(bytes) {
  return bytes > 1048576
    ? `${(bytes / 1048576).toFixed(1)} MB`
    : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

/* Any open dots menu closes on a click elsewhere. One listener on the document
 * rather than one per menu, because the menus are rebuilt on every render. */
document.addEventListener('click', () => {
  for (const menu of document.querySelectorAll('.tabmenu:not([hidden])')) {
    menu.hidden = true;
  }
});

window.addEventListener('hashchange', route);
window.addEventListener('beforeunload', (event) => {
  if (state.dirty) event.preventDefault();
});

/* Session first, then draw. Routing before we know who this is would flash the
 * app shell at somebody who is about to be shown a login screen. */
document.getElementById('rail-toggle').addEventListener('click', toggleRail);

(async () => {
  await loadSession();
  route();
  if (state.user) {
    refreshHealth();
    setInterval(refreshHealth, 60000);
  }
})();
