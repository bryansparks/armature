import { api, configure, setCsrf, url } from './api.js';
import { clear, confirmDialog, element, errorBox, loading, pageHeader, stepUpDialog } from './dom.js';
import { hasUnsaved, mayLeave, release } from './guard.js';
import { parseHash } from './router.js';
import { isSso, loginPath, navFor, setSession, ssoStepUpPath, user } from './session.js';

const NAV_GROUPS = [
  ['Work', [['overview', 'Overview', 'home'], ['gates', 'Waiting for you', 'inbox'], ['runs', 'Runs', 'list'], ['subjects', 'Pull requests', 'list'], ['trends', 'Trends', 'chart'], ['teams', 'Teams', 'team'], ['projects', 'Projects', 'folder'], ['status', 'Status', 'pulse']]],
  ['Definitions', [['workflows', 'Workflows', 'flow'], ['roles', 'Roles and prompts', 'doc'], ['packs', 'Packs', 'box']]],
  ['Manage', [['setup', 'Setup', 'check'], ['users', 'Users', 'people'], ['secrets', 'Secrets', 'key'], ['runner-keys', 'Runner keys', 'key'], ['runners', 'Runners', 'server'], ['aws', 'AWS', 'cloud'], ['webhooks', 'Webhooks', 'hook'], ['triggers', 'Triggers', 'clock'], ['settings', 'Settings', 'gear']]],
  ['You', [['account', 'Account', 'person']]],
];

// Simple 16px outline icons, drawn as SVG so the menu needs no icon font.
const ICON_PATHS = {
  home: 'M2 7.5L8 2.5l6 5M3.5 6.5v7h9v-7M6.5 13.5v-4h3v4',
  list: 'M5 4h9M5 8h9M5 12h9M2 4h.01M2 8h.01M2 12h.01',
  chart: 'M2 2v12h12M5 11V8M8 11V5M11 11V7',
  team: 'M2 3h12v10H2zM2 6h12M6 6v7',
  people: 'M6 7a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zM1.5 14c0-2.5 2-4.5 4.5-4.5s4.5 2 4.5 4.5M11 2.5a2.5 2.5 0 0 1 0 4.5M12.5 9.8c1.2.6 2 2 2 4.2',
  flow: 'M3 2.5h4v3H3zM9 10.5h4v3H9zM5 5.5v3.5h6v1.5',
  doc: 'M4 1.5h5.5L12 4v10.5H4zM9.5 1.5V4H12M6 7.5h4M6 10h4',
  key: 'M10.5 1.5a4 4 0 1 1-3.2 6.4L2 13.2V15h2.5v-1.5H6V12h1.5l1-1A4 4 0 0 1 10.5 1.5zM11.5 4.5h.01',
  person: 'M8 7.5a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM2.5 15c0-3 2.5-5.5 5.5-5.5s5.5 2.5 5.5 5.5',
  box: 'M2 4.5L8 1.5l6 3v7l-6 3-6-3zM2 4.5l6 3 6-3M8 7.5v7',
  gear: 'M8 10a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4',
  pulse: 'M1.5 8.5h3l2-5 3 9 2-4h3',
  hook: 'M8 1.5v7a3 3 0 1 1-3-3M8 1.5l2 2M8 1.5l-2 2',
  clock: 'M8 14.5a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13zM8 4.5V8l2.5 1.5',
  server: 'M2 2.5h12v4H2zM2 9.5h12v4H2zM4.5 4.5h.01M4.5 11.5h.01',
  cloud: 'M4.5 12.5h7.5a2.5 2.5 0 0 0 .3-5 4 4 0 0 0-7.6-1A3 3 0 0 0 4.5 12.5z',
  folder: 'M1.5 3.5h4.5l1.5 1.5h7v8h-13z',
  check: 'M2.5 8.5l3.5 3.5 7.5-8',
  inbox: 'M1.5 9.5l2-6.5h9l2 6.5v4h-13zM1.5 9.5h4l1 2h3l1-2h4',
  menu: 'M2 4h12M2 8h12M2 12h12',
  collapse: 'M10 3L5 8l5 5',
};

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', 'icon');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', ICON_PATHS[name] || '');
  svg.append(path);
  return svg;
}

const VIEW_LOADERS = {
  overview: () => import('./views/overview.js'),
  trends: () => import('./views/trends.js'),
  gates: () => import('./views/gates.js'),
  runs: () => import('./views/runs.js'),
  run: () => import('./views/run.js'),
  subjects: () => import('./views/subject.js'),
  subject: () => import('./views/subject.js'),
  teams: () => import('./views/teams.js'),
  team: () => import('./views/teams.js'),
  projects: () => import('./views/projects.js'),
  project: () => import('./views/projects.js'),
  setup: () => import('./views/setup.js'),
  users: () => import('./views/users.js'),
  secrets: () => import('./views/secrets.js'),
  'runner-keys': () => import('./views/runnerkeys.js'),
  runners: () => import('./views/runners.js'),
  aws: () => import('./views/aws.js'),
  webhooks: () => import('./views/webhooks.js'),
  webhook: () => import('./views/webhooks.js'),
  triggers: () => import('./views/triggers.js'),
  trigger: () => import('./views/triggers.js'),
  workflows: () => import('./views/workflows.js'),
  workflow: () => import('./views/workflows.js'),
  'new-workflow': () => import('./views/workflows.js'),
  roles: () => import('./views/roles.js'),
  document: () => import('./views/roles.js'),
  'new-document': () => import('./views/roles.js'),
  packs: () => import('./views/packs.js'),
  settings: () => import('./views/settings.js'),
  status: () => import('./views/status.js'),
  account: () => import('./views/account.js'),
};

const NAV_PARENT = { team: 'teams', project: 'projects', run: 'runs', subject: 'subjects', workflow: 'workflows', 'new-workflow': 'workflows', document: 'roles', 'new-document': 'roles', webhook: 'webhooks', trigger: 'triggers' };

let allowedViews = [];
let renderSequence = 0;
let viewController = null;
let shownHash = location.hash;

async function route() {
  release();
  shownHash = location.hash;
  const parsed = parseHash(location.hash);
  const { name, args, query } = parsed;
  closeDrawer();
  const main = document.getElementById('content');
  for (const navLink of document.querySelectorAll('nav.side a[data-view]')) {
    if (navLink.dataset.view === (NAV_PARENT[name] || name)) navLink.setAttribute('aria-current', 'page');
    else navLink.removeAttribute('aria-current');
  }
  const sequence = ++renderSequence;
  // Ends the last view's streams and timers.
  viewController?.abort();
  const controller = new AbortController();
  viewController = controller;
  const isAllowed = allowedViews.includes(NAV_PARENT[name] || name);
  const loadView = VIEW_LOADERS[name];
  if (!loadView || !isAllowed) {
    clear(main).append(element('div', { class: 'wrap' }, pageHeader('Not found'), element('p', {}, 'No such page. Pick one from the menu.')));
    document.title = 'Not found | Armature';
    return;
  }
  main.classList.add('stale');
  if (!main.firstChild) main.append(element('div', { class: 'wrap' }, loading()));
  try {
    const viewModule = await loadView();
    if (sequence !== renderSequence) return;
    const viewRoot = element('div', { class: 'wrap' });
    await viewModule.render(viewRoot, { name, args, query, signal: controller.signal });
    if (sequence !== renderSequence) return;
    clear(main).append(viewRoot);
    document.title = `${viewRoot.querySelector('h1')?.textContent || 'Armature'} | Armature`;
  } catch (err) {
    if (sequence !== renderSequence) return;
    clear(main).append(element('div', { class: 'wrap' }, errorBox(err)));
  } finally {
    if (sequence === renderSequence) main.classList.remove('stale');
  }
}

// Phone: the menu is a drawer. Desktop: it collapses to icons.
const COLLAPSE_KEY = 'armature-menu-collapsed';
let drawerReturnFocus = null;

function openDrawer() {
  drawerReturnFocus = document.activeElement;
  document.getElementById('layout').classList.add('drawer-open');
  document.getElementById('scrim').hidden = false;
  document.getElementById('menu-button').setAttribute('aria-expanded', 'true');
  document.querySelector('nav.side a')?.focus();
}

function closeDrawer() {
  const layout = document.getElementById('layout');
  if (!layout.classList.contains('drawer-open')) return;
  layout.classList.remove('drawer-open');
  document.getElementById('scrim').hidden = true;
  document.getElementById('menu-button').setAttribute('aria-expanded', 'false');
  drawerReturnFocus?.focus?.();
}

function setCollapsed(isCollapsed) {
  document.getElementById('layout').classList.toggle('collapsed', isCollapsed);
  const toggle = document.getElementById('collapse-button');
  toggle.setAttribute('aria-expanded', String(!isCollapsed));
  toggle.setAttribute('aria-label', isCollapsed ? 'Expand menu' : 'Collapse menu');
  try {
    localStorage.setItem(COLLAPSE_KEY, isCollapsed ? '1' : '0');
  } catch {}
}

function buildNav() {
  const nav = document.getElementById('nav');
  const collapseButton = element('button', { type: 'button', id: 'collapse-button', class: 'icon-button collapse', 'aria-expanded': 'true', 'aria-label': 'Collapse menu' }, icon('collapse'));
  collapseButton.addEventListener('click', () => setCollapsed(!document.getElementById('layout').classList.contains('collapsed')));
  nav.append(element('div', { class: 'brand' }, element('span', { class: 'brand-name' }, 'Armature'), collapseButton));
  for (const [groupName, items] of NAV_GROUPS) {
    const shown = items.filter(([view]) => allowedViews.includes(view));
    if (!shown.length) continue;
    const list = element('ul', { role: 'list' }, shown.map(([view, label, iconName]) => element('li', {},
      element('a', { href: `#/${view}`, 'data-view': view, title: label }, icon(iconName), element('span', { class: 'nav-label' }, label)))));
    nav.append(element('div', { class: 'group' }, element('div', { class: 'group-name' }, groupName), list));
  }
  nav.append(element('div', { 'data-theme-toggle': '', class: 'nav-theme' }));
  let isCollapsed = false;
  try {
    isCollapsed = localStorage.getItem(COLLAPSE_KEY) === '1';
  } catch {}
  setCollapsed(isCollapsed);
}

export async function signOut() {
  try {
    await api('/api/logout', { method: 'POST' });
  } finally {
    location.replace(url('/login'));
  }
}

function accountMenu() {
  const button = element('button', { type: 'button', class: 'account-button', 'aria-haspopup': 'menu', 'aria-expanded': 'false', 'aria-controls': 'account-menu' },
    user()?.display_name || user()?.username || 'Account');
  const items = [
    element('button', { type: 'button', role: 'menuitem', tabindex: -1, onclick: () => { location.hash = '#/account'; } }, 'Account'),
    element('button', { type: 'button', role: 'menuitem', tabindex: -1, onclick: signOut }, 'Sign out'),
  ].filter(Boolean);
  const menu = element('div', { class: 'menu', id: 'account-menu', role: 'menu', 'aria-label': 'Account', hidden: true }, items);
  const close = (shouldRefocus) => {
    menu.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    if (shouldRefocus) button.focus();
  };
  const open = () => {
    menu.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    items[0].focus();
  };
  button.addEventListener('click', () => (menu.hidden ? open() : close(false)));
  button.addEventListener('keydown', (keyEvent) => {
    if (keyEvent.key === 'ArrowDown') {
      keyEvent.preventDefault();
      open();
    }
  });
  menu.addEventListener('keydown', (keyEvent) => {
    const focusedIndex = items.indexOf(document.activeElement);
    if (keyEvent.key === 'Escape') close(true);
    else if (keyEvent.key === 'ArrowDown' || keyEvent.key === 'ArrowUp') {
      keyEvent.preventDefault();
      items[(focusedIndex + (keyEvent.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus();
    } else if (keyEvent.key === 'Tab') close(false);
  });
  menu.addEventListener('click', () => close(false));
  document.addEventListener('click', (clickEvent) => {
    if (!menu.hidden && !clickEvent.target.closest('.account')) close(false);
  });
  return element('div', { class: 'account' }, button, menu);
}

function buildTopbar() {
  const menuButton = element('button', { type: 'button', id: 'menu-button', class: 'icon-button menu-button', 'aria-label': 'Open menu', 'aria-expanded': 'false', 'aria-controls': 'nav' }, icon('menu'));
  menuButton.addEventListener('click', openDrawer);
  document.getElementById('topbar').append(menuButton, element('div', { class: 'topbar-spacer' }),
    element('div', { 'data-theme-toggle': '', class: 'topbar-theme' }), accountMenu());
  document.getElementById('scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (keyEvent) => {
    if (keyEvent.key === 'Escape') closeDrawer();
  });
}

// The address goes back while the dialog is open, so a cancel leaves the page and its URL as they were.
async function leaveThenRoute() {
  const target = location.hash;
  if (hasUnsaved()) {
    if (target !== shownHash) history.replaceState(null, '', shownHash || location.pathname + location.search);
    const leave = await mayLeave(() => confirmDialog('Leave without saving?', 'Your edits on this page are not saved. Leaving discards them.', 'Leave'));
    if (!leave) return;
    if (target !== shownHash) history.replaceState(null, '', target);
  }
  route();
}

function stepUp() {
  if (!isSso()) return stepUpDialog();
  location.assign(url(ssoStepUpPath(location.hash)));
  return Promise.resolve(null);
}

configure({ signedOut: () => location.replace(url(loginPath(location.hash))), stepUp });

async function start() {
  let sessionPayload;
  try {
    sessionPayload = await api('/api/session');
  } catch (err) {
    clear(document.getElementById('content')).append(element('div', { class: 'wrap' }, errorBox(err)));
    return;
  }
  if (!sessionPayload?.user) {
    location.replace(url(loginPath(location.hash)));
    return;
  }
  setSession(sessionPayload);
  setCsrf(sessionPayload.csrf);
  allowedViews = navFor(sessionPayload);
  buildNav();
  buildTopbar();
  window.armatureThemeToggles?.();
  document.getElementById('skip').addEventListener('click', () => document.getElementById('content').focus());
  window.addEventListener('hashchange', leaveThenRoute);
  window.addEventListener('beforeunload', (unloadEvent) => {
    if (hasUnsaved()) unloadEvent.preventDefault();
  });
  route();
}

start();
