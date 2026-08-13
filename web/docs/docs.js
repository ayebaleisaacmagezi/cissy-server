/* ─────────────────────────────────────────────────────────────────────────
   The documentation site's navigation.

   One list, rendered into every page, so adding a page means adding a file
   and a line here rather than editing the sidebar in thirteen places. The
   same list produces the previous/next links at the foot of each page, which
   is the only way they stay in the right order as pages get added.

   No build step and no framework, the same as the rest of this product.
   ───────────────────────────────────────────────────────────────────────── */

const PAGES = [
  ['Start here', [
    ['', 'Overview', 'What Web2App does, and what it does not.'],
    ['getting-started', 'Getting started', 'From a URL to an installable app.'],
  ]],
  ['Your app', [
    ['webview', 'The website', 'Allowed domains, external links, HTTPS.'],
    ['studio', 'Studio and navigation', 'Theme, modules, the bottom bar.'],
    ['website-navigation', "Hiding your site's navigation", 'So the app does not show two.'],
    ['offline', 'The offline screen', 'What people see with no connection.'],
  ]],
  ['Notifications', [
    ['notifications', 'How push works', 'Firebase, ownership, and the shape of it.'],
    ['notifications-setup', 'Setting it up', 'Project, identifiers, configuration files.'],
    ['notifications-sending', 'Sending', 'From the Console, or from your own backend.'],
    ['notifications-ios', 'iPhone and APNs', "Apple's half of the delivery path."],
  ]],
  ['Shipping', [
    ['signing', 'Signing keys', 'The key that proves a release is yours.'],
    ['building', 'Building', 'Artifacts, version codes, and drift.'],
    ['publishing', 'Publishing', 'Google Play, and the iOS project on a Mac.'],
    ['billing', 'Plans and billing', 'What a plan includes, and how to pay.'],
  ]],
];

/* The flat order, for previous/next. */
const FLAT = PAGES.flatMap(([, pages]) => pages);

function here() {
  // "/docs/studio", "/docs/studio.html" and "/docs/" all mean the same page.
  const path = location.pathname.replace(/\/+$/, '');
  const last = path.split('/').pop() || '';
  return last === 'docs' ? '' : last.replace(/\.html$/, '');
}

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children || []) {
    node.append(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

function href(slug) {
  return slug ? `/docs/${slug}` : '/docs';
}

function render() {
  const current = here();

  const nav = el('nav', { class: 'docnav' }, [
    el('a', { class: 'brand', href: '/' }, [
      el('img', { src: '/logo.webp', alt: 'Web2App' }),
    ]),
  ]);

  for (const [group, pages] of PAGES) {
    nav.append(el('div', { class: 'grp' }, [group]));
    for (const [slug, title] of pages) {
      nav.append(el('a', {
        href: href(slug),
        class: slug === current ? 'on' : null,
      }, [title]));
    }
  }
  nav.append(el('div', { class: 'back' }, [
    el('a', { href: '/app', style: 'padding:0;border:0' }, ['Back to your apps']),
  ]));

  const position = FLAT.findIndex(([slug]) => slug === current);
  const entry = FLAT[position];

  const bar = el('div', { class: 'docbar' }, [
    el('a', { href: '/docs', style: 'color:inherit;text-decoration:none' }, ['Documentation']),
    document.createTextNode(entry ? '/' : ''),
    entry ? el('b', {}, [entry[1]]) : '',
    el('div', { class: 'spacer' }),
    el('a', { href: '/app', class: 'btn sm' }, ['Open the app']),
  ]);

  const article = document.querySelector('article');
  if (entry && position >= 0) {
    const foot = el('div', { class: 'docfoot' });
    const previous = FLAT[position - 1];
    const next = FLAT[position + 1];
    if (previous) {
      foot.append(el('a', { href: href(previous[0]) }, [
        el('span', {}, ['Previous']), el('b', {}, [previous[1]]),
      ]));
    }
    if (next) {
      foot.append(el('a', { href: href(next[0]), class: 'next' }, [
        el('span', {}, ['Next']), el('b', {}, [next[1]]),
      ]));
    }
    if (foot.childElementCount) article.append(foot);
  }

  const main = el('div', { class: 'docmain' }, [bar]);
  main.append(article);

  const shell = el('div', { class: 'docshell' }, [nav, main]);
  document.body.append(shell);
}

/* The contents grid on the overview page, built from the same list. */
function renderContents(into) {
  for (const [group, pages] of PAGES) {
    into.append(el('h2', {}, [group]));
    const cards = el('div', { class: 'cards' });
    for (const [slug, title, blurb] of pages) {
      if (!slug) continue;
      cards.append(el('a', { href: href(slug) }, [
        el('b', {}, [title]), el('span', {}, [blurb]),
      ]));
    }
    into.append(cards);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const slot = document.getElementById('contents');
  if (slot) renderContents(slot);
  render();
});
