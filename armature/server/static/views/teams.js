import { api, listOf } from '../api.js';
import { attempt, card, clear, element, empty, errorBox, link, loading, pageHeader, table, tabs } from '../dom.js';
import { hashFor } from '../router.js';
import { scopeKey } from '../scopes.js';
import { canManageTeam, isAdmin } from '../session.js';
import { connectionsCard, teamConnectionsPath } from './connections.js';
import { teamNames } from './definition.js';
import { providersSection } from './providers.js';
import { runnerKeysSection } from './runnerkeys.js';
import { workflowTable } from './workflows.js';

const PLATFORMS = ['ios', 'android', 'backend', 'dotnet'];
export const ENVIRONMENT_TYPES_PATH = '/api/environment-types';

export const environmentTypePath = (projectId) => `/api/projects/${encodeURIComponent(projectId)}/environment-type`;

export function environmentTypeOptions(types, currentId) {
  return [['', 'None: jobs wait for a runner'], ...types.filter((type) => type.enabled || type.id === currentId)
    .map((type) => [type.id, `${type.name} (${type.kind})${type.enabled ? '' : ', disabled'}`])];
}

function environmentTypeSelect(id, types, currentId) {
  const select = element('select', { id }, environmentTypeOptions(types, currentId).map(([value, label]) => element('option', { value }, label)));
  select.value = currentId || '';
  return select;
}

export const teamPageHref = (teamId) => hashFor('team', [teamId]);

export const newWorkflowHref = (teamId) => hashFor('new-workflow', [], { scope: scopeKey({ scope_kind: 'team', scope_id: teamId }) });

function addProjectForm(team, types, reload) {
  const nameInput = element('input', { type: 'text', id: `project-name-${team.id}`, required: true, maxlength: 100, autocapitalize: 'none', spellcheck: 'false', placeholder: 'mobile-ios' });
  const platformInput = element('input', { type: 'text', id: `project-platform-${team.id}`, required: true, maxlength: 32, list: 'platforms', autocapitalize: 'none', spellcheck: 'false' });
  const typeSelect = environmentTypeSelect(`project-environment-${team.id}`, types, null);
  const addButton = element('button', { type: 'submit' }, 'Add project');
  const status = element('div', { role: 'status' });
  const form = element('form', { class: 'wf-actions' },
    element('label', { for: nameInput.id }, 'Project'), nameInput, element('label', { for: platformInput.id }, 'Platform'), platformInput,
    types.length ? [element('label', { for: typeSelect.id }, 'Environment'), typeSelect] : null, addButton);
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const added = await attempt(status, addButton, () => api(`/api/teams/${encodeURIComponent(team.id)}/projects`, {
      method: 'POST', body: { name: nameInput.value.trim(), platform: platformInput.value.trim(), environment_type_id: typeSelect.value || null },
    }));
    if (added) reload();
  });
  return element('div', {}, element('datalist', { id: 'platforms' }, PLATFORMS.map((platform) => element('option', { value: platform }))), form, status);
}

function addTeamForm(reload) {
  const nameInput = element('input', { type: 'text', id: 'new-team-name', required: true, maxlength: 60 });
  const addButton = element('button', { type: 'submit', class: 'primary' }, 'Add team');
  const status = element('div', { role: 'status' });
  const form = element('form', { class: 'wf-actions' }, element('label', { for: nameInput.id }, 'Name'), nameInput, addButton);
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    if (await attempt(status, addButton, () => api('/api/teams', { method: 'POST', body: { name: nameInput.value.trim() } }))) reload();
  });
  return card(element('h2', { class: 'inline' }, 'Add a team'), form, status);
}

async function list(root) {
  root.append(pageHeader('Teams'));
  const body = element('div', {}, loading());
  root.append(body);

  async function reload() {
    let teams;
    try {
      teams = listOf(await api('/api/teams'), 'teams');
    } catch (err) {
      clear(body).append(errorBox(err));
      return;
    }
    clear(body).append(card(teams.length
      ? table(['Team'], teams.map((team) => [link(teamPageHref(team.id), team.name)]))
      : empty(isAdmin() ? 'No teams yet. Add the first one below.' : 'You are not on any team yet.')));
    if (isAdmin()) body.append(addTeamForm(reload));
  }

  await reload();
}

async function projectsTab(panel, team) {
  const [projects, types] = await Promise.all([
    api(`/api/teams/${encodeURIComponent(team.id)}/projects`).then((payload) => listOf(payload, 'projects')),
    api(ENVIRONMENT_TYPES_PATH).then((payload) => listOf(payload, 'types')).catch(() => []),
  ]);
  const typeNames = new Map(types.map((type) => [type.id, type.name]));
  const canManage = canManageTeam(team.id);
  clear(panel).append(card(
    projects.length ? table(['Project', 'Platform', 'Environment'], projects.map((project) => [
      link(hashFor('project', [project.id]), project.name), project.platform || '—', typeNames.get(project.environment_type_id) || '—',
    ])) : empty('No projects yet.'),
    canManage ? addProjectForm(team, types, () => showTab(panel, team, 'projects')) : null,
  ));
}

async function workflowsTab(panel, team) {
  const [workflows, names] = await Promise.all([
    api(`/api/workflows?${new URLSearchParams({ team: team.id })}`).then((payload) => listOf(payload, 'workflows')), teamNames(),
  ]);
  clear(panel).append(card(
    canManageTeam(team.id) ? element('div', { class: 'wf-actions' }, link(newWorkflowHref(team.id), 'Add workflow', { class: 'btn' })) : null,
    workflows.length ? workflowTable(workflows, names) : empty('No workflows for this team yet.'),
  ));
}

async function providersTab(panel, team) {
  const canManage = canManageTeam(team.id);
  const [providers, teamSecrets, instanceSecrets] = await Promise.all([
    // Project-only members cannot read a team's providers, so a refusal just shows none.
    api(`/api/teams/${encodeURIComponent(team.id)}/providers`).then((payload) => listOf(payload, 'providers')).catch(() => []),
    canManage ? api(`/api/teams/${encodeURIComponent(team.id)}/secrets`).then((payload) => listOf(payload, 'secrets')).catch(() => []) : [],
    isAdmin() ? api('/api/secrets').then((payload) => listOf(payload, 'secrets')).catch(() => []) : [],
  ]);
  const secretNames = [...new Set([...teamSecrets, ...instanceSecrets].map((secret) => secret.name))].sort();
  const section = providersSection(team, providers, { canManage, secretNames, heading: null, reload: () => showTab(panel, team, 'providers') });
  clear(panel).append(card(section || empty('No model providers.')));
}

async function showTab(panel, team, active) {
  try {
    if (active === 'workflows') await workflowsTab(panel, team);
    else if (active === 'providers') await providersTab(panel, team);
    else if (active === 'runners') clear(panel).append(card(runnerKeysSection(team.id, { heading: null })));
    else if (active === 'connections') clear(panel).append(connectionsCard({ listPath: teamConnectionsPath(team.id), canManage: canManageTeam(team.id), prefix: `connections-${team.id}` }));
    else await projectsTab(panel, team);
  } catch (err) {
    clear(panel).append(errorBox(err));
  }
}

async function detail(root, teamId, requestedTab) {
  const crumb = element('p', { class: 'crumb' }, link(hashFor('teams'), 'Teams'));
  let team;
  try {
    team = listOf(await api('/api/teams'), 'teams').find((candidate) => String(candidate.id) === teamId);
  } catch (err) {
    root.append(crumb, pageHeader('Team'), errorBox(err));
    return;
  }
  if (!team) {
    root.append(crumb, pageHeader('Team'), card(empty('Team not found.')));
    return;
  }
  const sections = [{ id: 'projects', label: 'Projects' }, { id: 'workflows', label: 'Workflows' }, { id: 'providers', label: 'Model providers' },
    { id: 'connections', label: 'Connections' }, ...(canManageTeam(team.id) ? [{ id: 'runners', label: 'Runner keys' }] : [])];
  const active = sections.some((tab) => tab.id === requestedTab) ? requestedTab : 'projects';
  const { tablist, panel } = tabs(teamPageHref(team.id), sections, active, { label: 'Team sections' });
  panel.append(loading());
  root.append(crumb, pageHeader(team.name), tablist, panel);
  await showTab(panel, team, active);
}

export async function render(root, { name, args }) {
  if (name === 'team' && args[0]) return detail(root, args[0], args[1]);
  return list(root);
}
