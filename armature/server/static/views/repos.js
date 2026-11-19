import { api, listOf } from '../api.js';
import { attempt, card, clear, confirmDialog, element, empty, errorBox, loading, pageHeader, table, tabs, when } from '../dom.js';
import { canManageTeam } from '../session.js';

export const SYNC_LABELS = { backfill: 'Backfill', poll: 'Poll', conflicts: 'Conflicts', gap_fill: 'Gap fill' };

export const reposPath = (teamId) => `/api/teams/${encodeURIComponent(teamId)}/repos`;
export const repoPath = (teamId, repoId) => `${reposPath(teamId)}/${encodeURIComponent(repoId)}`;
export const repoDeletePath = (teamId, repoId) => `${repoPath(teamId, repoId)}/delete`;

export function syncText(state) {
  const label = SYNC_LABELS[state.kind] || state.kind;
  return state.last_error ? `${label}: failed: ${state.last_error}` : `${label}: next run ${when(state.next_run_at)}`;
}

// Blank optional fields are left out, so the server picks the connection and keeps no backfill date.
export function repoBody(values) {
  const body = { full_name: (values.full_name || '').trim() };
  if (values.connection_id) body.connection_id = values.connection_id;
  if (values.backfill_since) body.backfill_since = values.backfill_since;
  return body;
}

function addForm(team, connections, onDone) {
  const prefix = `repo-${team.id}`;
  const nameInput = element('input', { type: 'text', id: `${prefix}-name`, required: true, placeholder: 'owner/name', spellcheck: 'false', autocapitalize: 'none', autocomplete: 'off', maxlength: 201 });
  const connectionSelect = element('select', { id: `${prefix}-connection` }, element('option', { value: '' }, 'Automatic'),
    connections.map((connection) => element('option', { value: connection.id }, `${connection.name}${connection.scope_kind === 'project' ? ' (project)' : ''}`)));
  const sinceInput = element('input', { type: 'date', id: `${prefix}-since` });
  const status = element('div', { role: 'status' });
  const addButton = element('button', { type: 'submit', class: 'primary' }, 'Track repository');
  const form = element('form', { class: 'stacked-form' }, element('h2', { class: 'inline' }, 'Track a repository'),
    element('label', { class: 'stacked', for: nameInput.id }, 'Repository', nameInput),
    element('label', { class: 'stacked', for: connectionSelect.id }, 'GitHub connection', connectionSelect,
      element('span', { class: 'note' }, 'Automatic uses the team connection installed on the owner.')),
    element('label', { class: 'stacked', for: sinceInput.id }, 'Backfill since (optional)', sinceInput), status,
    element('div', { class: 'wf-actions' }, addButton));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const body = repoBody({ full_name: nameInput.value, connection_id: connectionSelect.value, backfill_since: sinceInput.value });
    if (await attempt(status, addButton, () => api(reposPath(team.id), { method: 'POST', body }))) onDone();
  });
  return card(form);
}

function actions(team, repo, status, reload) {
  const toggleButton = element('button', { type: 'button' }, repo.enabled ? 'Disable' : 'Enable');
  toggleButton.addEventListener('click', async () => {
    if (await attempt(status, toggleButton, () => api(repoPath(team.id, repo.id), { method: 'POST', body: { enabled: !repo.enabled } }))) reload();
  });
  const removeButton = element('button', { type: 'button' }, 'Remove');
  removeButton.addEventListener('click', async () => {
    if (!(await confirmDialog(`Stop tracking ${repo.full_name}?`, 'Its collected pull requests, pushes, reviews, and checks are deleted.', 'Remove'))) return;
    if (await attempt(status, removeButton, () => api(repoDeletePath(team.id, repo.id), { method: 'POST', body: {} }))) reload();
  });
  return element('div', { class: 'wf-actions' }, toggleButton, removeButton);
}

function teamPanel(team, panel) {
  const status = element('div', { role: 'status' });
  const listSlot = element('div', {}, loading());
  const formSlot = element('div');
  async function reload() {
    let page;
    try {
      page = await api(reposPath(team.id));
    } catch (err) {
      clear(listSlot).append(errorBox(err));
      return;
    }
    const repos = listOf(page, 'repos');
    clear(listSlot).append(card(repos.length ? table(['Repository', 'Project', 'Enabled', 'Backfill since', 'Sync', ''], repos.map((repo) => [
      element('span', { class: 'mono' }, repo.full_name), repo.project_name || '—', repo.enabled ? 'Yes' : 'No', repo.backfill_since || '—',
      element('div', {}, (repo.sync || []).map((state) => element('div', {}, syncText(state)))), actions(team, repo, status, reload),
    ])) : empty('This team tracks no repositories yet.')));
    clear(formSlot).append(addForm(team, page.connections || [], reload));
  }
  panel.append(status, listSlot, formSlot);
  reload();
}

export async function render(root, { args }) {
  root.append(pageHeader('Tracked repositories'));
  root.append(element('p', { class: 'note' }, 'The server keeps the pull request history of each tracked repository: pull requests, pushes, reviews, and checks.'));
  let teams;
  try {
    teams = listOf(await api('/api/teams'), 'teams').filter((team) => canManageTeam(team.id));
  } catch (err) {
    root.append(errorBox(err));
    return;
  }
  if (!teams.length) {
    root.append(card(empty('You do not lead any team.')));
    return;
  }
  const scopes = teams.map((team) => ({ id: String(team.id), label: team.name }));
  const teamId = scopes.some((scope) => scope.id === args[0]) ? args[0] : scopes[0].id;
  const { tablist, panel } = tabs('#/repos', scopes, teamId, { label: 'Team' });
  root.append(tablist, panel);
  teamPanel(teams.find((team) => String(team.id) === teamId), panel);
}
