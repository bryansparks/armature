// Insights: one sidebar entry, its own sub-tabs, and a screen per manager question. Every filter is in the URL.
import { api, listOf } from '../../api.js';
import { clear, element } from '../../dom.js';
import { hashFor } from '../../router.js';
import { canManageTeam, isAdmin } from '../../session.js';
import { repoShort } from './format.js';
import { closeDrawer, emptyState, errorState, filters, header, installTooltips, noPermission, select, skeletonRows } from './ui.js';

const SCREENS = {
  '': () => import('./overview.js'),
  repo: () => import('./repo.js'),
  repos: () => import('./repo.js'),
  people: () => import('./people.js'),
  person: () => import('./person.js'),
  findings: () => import('./findings.js'),
  models: () => import('./models.js'),
  fixes: () => import('./fixes.js'),
  pr: () => import('./pr.js'),
  prs: () => import('./prs.js'),
  report: () => import('./report.js'),
};

const TAB_OF = { '': 'overview', repo: 'repos', repos: 'repos', people: 'people', person: 'people', findings: 'findings', models: 'models', fixes: 'fixes', report: 'report' };
const TEAM_KEY = 'armature-insights-team';
const REPO_KEY = 'armature-insights-repo';
export const DAYS = [7, 30, 90];

const remember = (key, value) => {
  try {
    localStorage.setItem(key, value);
  } catch {}
};
const recall = (key) => {
  try {
    return localStorage.getItem(key) || '';
  } catch {
    return '';
  }
};

const canTrack = (teamId) => isAdmin() || canManageTeam(teamId);

export function daysFrom(query, fallback = 30) {
  const days = Number(query.get('days'));
  return DAYS.includes(days) ? days : fallback;
}

// The URL names a repo by its short name when that is unique in the team, else by its full name.
export function repoToken(repos, repo) {
  if (!repo) return '';
  const clash = repos.filter((other) => other.name === repo.name).length > 1;
  return clash ? repo.full_name : repo.name;
}

export function findRepo(repos, token) {
  if (!token) return null;
  return repos.find((repo) => repo.id === token) || repos.find((repo) => repo.full_name === token) || repos.find((repo) => repo.name === token) || null;
}

// Series colors by when each repo was tracked, so a repo keeps its color however the list is sorted or filtered.
export function repoSlots(repos) {
  const at = (repo) => (repo.tracked_at ? Date.parse(repo.tracked_at) : Infinity);
  const ordered = [...repos].sort((a, b) => (at(a) - at(b)) || a.full_name.localeCompare(b.full_name));
  return new Map(ordered.map((repo, index) => [repo.id, Math.min(8, index + 1)]));
}

export const personToken = (who) => (who && typeof who === 'object' ? who.login || who.id : who);

// Builds the hrefs every screen links with, so a drill-down always lands on a shareable URL.
export function linker({ team, teams, repos }) {
  const teamParam = teams.length > 1 ? { team: team.id } : {};
  const make = (sub, args, params = {}) => hashFor('insights', [...(sub ? [sub] : []), ...args], { ...params, ...teamParam });
  const repoById = (id) => repos.find((repo) => repo.id === id);
  const repoParam = (params) => {
    const out = { ...params };
    if (out.repo) out.repo = repoToken(repos, repoById(out.repo)) || out.repo;
    return out;
  };
  return {
    make,
    overview: (params = {}) => make('', [], repoParam(params)),
    repo: (repoId, params = {}) => make('repo', [repoToken(repos, repoById(repoId)) || repoId], params),
    people: (params = {}) => make('people', [], repoParam(params)),
    prs: (params = {}) => make('prs', [], repoParam(params)),
    // Takes a person ({ id, login }) or an id. A login makes the readable link; without one the id still works.
    person: (who, params = {}) => make('person', [personToken(who)], params),
    findings: (params = {}) => make('findings', [], repoParam(params)),
    models: (params = {}) => make('models', [], params),
    fixes: (params = {}) => make('fixes', [], params),
    report: (params = {}) => make('report', [], params),
    // By repo and number, so a pull request's address reads like GitHub's. A repo outside the list falls back to the id.
    pr: (pr) => {
      const repo = repoById(pr?.repo_id);
      if (repo && pr.number !== null && pr.number !== undefined) return make('pr', [repoToken(repos, repo), pr.number]);
      return pr?.id ? make('pr', [pr.id]) : null;
    },
  };
}

function tabs(sub, links, { lead, admin, waiting, query }) {
  const current = sub === 'prs' ? (query.get('category') ? 'findings' : 'repos') : TAB_OF[sub];
  const items = [
    ['overview', 'Overview', links.overview()],
    ['repos', 'Repos', links.make('repos', [])],
    lead ? ['people', 'People', links.people()] : null,
    ['findings', 'Findings', links.findings()],
    ['models', 'Models', links.models()],
    lead ? ['fixes', 'Fixes', links.fixes()] : null,
    admin ? ['report', 'Daily report', links.report()] : null,
  ].filter(Boolean);
  return element('nav', { class: 'in-tabs', 'aria-label': 'Insights pages' }, items.map(([id, label, href]) => element('a', {
    class: 'in-tab', href, 'aria-current': id === current ? 'page' : null,
  }, label, id === 'fixes' && waiting ? element('span', { class: 'in-count', 'aria-label': `${waiting} waiting` }, String(waiting)) : null)));
}

function teamSwitcher(teams, team, onPick) {
  if (teams.length < 2) return null;
  return select({ label: 'Team', value: team.id, options: teams.map((option) => [option.id, option.name]), onChange: onPick, visibleLabel: true });
}

// The query after an in-page filter change: an empty value removes its key.
export function mergeQuery(current, changes) {
  const next = new URLSearchParams(current);
  for (const [key, value] of Object.entries(changes)) {
    if (value === null || value === undefined || value === '') next.delete(key);
    else next.set(key, String(value));
  }
  return next;
}

// Re-renders a screen into a fresh body and swaps it in, keeping the old content dimmed meanwhile. A reload that
// finishes after a newer one started is dropped, so a slow response never replaces a newer one.
export function reloader(body, renderInto, signal) {
  let latest = 0;
  return async () => {
    const mine = ++latest;
    const fresh = element('div', { class: 'in-body' });
    body.classList.add('in-stale');
    await renderInto(fresh);
    if (mine !== latest || signal.aborted || !body.isConnected) return false;
    const focused = document.activeElement?.id;
    body.replaceChildren(...fresh.childNodes);
    body.classList.remove('in-stale');
    if (focused) document.getElementById(focused)?.focus();
    return true;
  };
}

// Renders a screen's header at once, its skeleton while the data loads, then the evidence.
export async function page(body, ctx, { question, filterBar = null, who = null, skeleton, load, draw, what }) {
  const head = header({ question, filterBar, who });
  const placeholder = element('div', { 'aria-busy': 'true' }, skeleton ? skeleton() : skeletonRows());
  clear(body).append(head.node, head.scopeNode, placeholder);
  let data;
  try {
    data = await load();
  } catch (err) {
    if (ctx.signal.aborted) return;
    clear(head.answerNode);
    placeholder.replaceWith(errorState(what, err, () => page(body, ctx, { question, filterBar, who, skeleton, load, draw, what })));
    return;
  }
  if (ctx.signal.aborted) return;
  const nodes = draw(data, head);
  placeholder.replaceWith(...[nodes].flat().filter(Boolean));
}

export async function render(root, { args, query, signal }) {
  root.classList.add('insights');
  installTooltips();
  closeDrawer({ restore: false });
  const sub = args[0] || '';
  const loadScreen = SCREENS[sub];
  let teams;
  try {
    teams = listOf(await api('/api/teams'), 'teams');
  } catch (err) {
    root.append(element('div', { class: 'in-head' }, element('h1', { class: 'in-question' }, 'Insights')), errorState('your teams', err, () => location.reload()));
    return;
  }
  // The daily report covers the whole server, so an admin on no team still reads it.
  if (!teams.length && sub !== 'report') {
    root.append(header({ question: 'How is the team doing?', answer: 'You aren’t on a team yet.' }).node,
      emptyState('Insights shows a team’s pull requests, reviews, and findings. Ask an admin to add you to a team.'));
    return;
  }
  const wanted = query.get('team') || recall(TEAM_KEY);
  const team = teams.find((option) => option.id === wanted) || teams[0] || null;
  if (team) remember(TEAM_KEY, team.id);
  const lead = Boolean(team) && canManageTeam(team.id);
  const [repos, fixes] = team ? await Promise.all([
    api(`/api/teams/${encodeURIComponent(team.id)}/insights/repos`).then((payload) => listOf(payload, 'repos')).catch(() => []),
    lead ? api(`/api/teams/${encodeURIComponent(team.id)}/insights/fixes?status=waiting`).catch(() => null) : null,
  ]) : [[], null];
  const links = linker({ team, teams, repos });
  const slots = repoSlots(repos);
  const waiting = (fixes?.waiting || []).length;

  const body = element('div', { class: 'in-body' });
  const switchTeam = (picked) => {
    remember(TEAM_KEY, picked);
    location.hash = hashFor('insights', sub === 'person' || sub === 'pr' ? [] : args, { team: picked });
  };
  root.append(tabs(sub, links, { lead, admin: isAdmin(), waiting, query }), body);

  if (!loadScreen) {
    body.append(header({ question: 'Page not found', answer: 'No such Insights page.' }).node, element('p', { class: 'in-empty' }, element('a', { class: 'in-link', href: links.overview() }, 'Open the overview')));
    return;
  }
  if (sub === 'fixes' && !lead) {
    body.append(header({ question: 'What fixes are waiting on me?', answer: 'Fixes are approved by team leads.' }).node,
      noPermission('Ask an admin for the team lead role to approve fixes.'));
    return;
  }
  if (sub === 'report' && !isAdmin()) {
    body.append(header({ question: 'What did Armature learn yesterday?', answer: 'The daily report covers every team.' }).node,
      noPermission('Only admins read the daily report.'));
    return;
  }
  const screen = await loadScreen();
  let currentQuery = new URLSearchParams(query);
  let reload = null;
  const ctx = {
    team, teams, repos, lead, links, slots, waiting, signal, args, sub,
    teamControl: () => teamSwitcher(teams, team, switchTeam),
    get query() { return currentQuery; },
    repoName: (id) => repoShort(repos.find((repo) => repo.id === id)?.full_name),
    findRepo: (token) => findRepo(repos, token),
    rememberRepo: (id) => remember(REPO_KEY, id),
    lastRepo: () => recall(REPO_KEY),
    api: (path) => api(path),
    post: (path, body = {}) => api(path, { method: 'POST', body }),
    // An in-page filter: the URL changes in place, so Back leaves the screen instead of undoing each filter.
    setQuery: async (changes, { reload: again = true } = {}) => {
      currentQuery = mergeQuery(currentQuery, changes);
      const hash = hashFor('insights', ctx.pathArgs || args, Object.fromEntries(currentQuery));
      history.replaceState(null, '', `${location.pathname}${location.search}${hash}`);
      if (again) await reload();
    },
  };
  reload = reloader(body, (fresh) => screen.render(fresh, ctx), signal);
  if (!repos.length && screen.QUESTION && sub !== 'fixes' && sub !== 'models' && sub !== 'report') {
    const track = canTrack(team.id) ? element('a', { class: 'in-button', href: '#/repos' }, 'Track a repo') : null;
    const switcher = ctx.teamControl();
    body.append(header({ question: screen.QUESTION, answer: 'No repos are tracked yet.', filterBar: switcher ? filters(switcher) : null }).node,
      emptyState(track ? 'Track a repo to start collecting pull requests, reviews, and flags.' : 'Ask a team lead to track a repo. Insights starts collecting pull requests, reviews, and flags from then on.', track));
    return;
  }
  screen.render(body, ctx);
}
