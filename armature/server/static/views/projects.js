import { api, listOf } from '../api.js';
import { attempt, card, clear, element, empty, errorBox, link, loading, meta, pageHeader, table } from '../dom.js';
import { hashFor } from '../router.js';
import { canManageTeam } from '../session.js';
import { connectionsCard, projectConnectionsPath, teamConnectionsPath } from './connections.js';
import { teamNames } from './definition.js';
import { providersSection } from './providers.js';
import { ENVIRONMENT_TYPES_PATH, environmentTypeOptions, environmentTypePath } from './teams.js';
import { SOURCES, webhookHref, webhooksPath } from './webhooks.js';
import { workflowTable } from './workflows.js';

const PROJECT_EDITORS = new Set(['admin', 'team_lead', 'project_lead']);
export const PROJECTS_PATH = '/api/projects';
export const projectPath = (projectId) => `/api/projects/${encodeURIComponent(projectId)}`;

const REPO = /^(?:(?:https|ssh|git):\/\/(?:git@)?([^/\s@:]+)(?::\d+)?\/|git@([^:/\s]+):|([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\/)?([A-Za-z0-9-]{1,39})\/([A-Za-z0-9._-]{1,100}?)(?:\.git)?\/?$/;

// Mirrors repo_slug in routes_api.py: owner/name from a slug or a clone URL, and the name a new project gets.
export function parseRepo(text) {
  const match = String(text || '').trim().match(REPO);
  if (!match || /^\.+$/.test(match[5])) return null;
  const host = (match[1] || match[2] || match[3] || 'github.com').toLowerCase();
  return host === 'github.com' ? { slug: `${match[4]}/${match[5]}`, name: match[5] } : null;
}

export function projectBody(values) {
  const repo = parseRepo(values.repo);
  if (!repo) return { error: 'Paste the repository’s github.com address, or owner/name.' };
  return { body: { name: String(values.name || '').trim() || repo.name, repo: repo.slug, environment_type_id: values.environment_type_id || null,
    connection_id: values.connection_id || null, default_branch: String(values.default_branch || '').trim() || null } };
}

export const githubConnections = (connections) => connections.filter((connection) => connection.kind === 'github_app');

function select(id, options, value) {
  const node = element('select', { id }, options.map(([optionValue, label]) => element('option', { value: optionValue }, label)));
  node.value = value || '';
  return node;
}

function field(id, label, input, help) {
  input.id = id;
  return element('label', { class: 'stacked', for: id }, label, input, help ? element('span', { class: 'note' }, help) : null);
}

const connectionChoices = (connections) => [['', 'None'], ...githubConnections(connections).map((connection) => [connection.id, connection.name])];

// One repo becomes one project. The wizard and the Projects page both use this row.
export function repoForm(team, { types, connections, prefix, onSaved }) {
  const repoInput = element('input', { type: 'text', required: true, spellcheck: 'false', autocapitalize: 'none', placeholder: 'github.com/owner/name' });
  const typeSelect = select(`${prefix}-type`, environmentTypeOptions(types, null), types.find((type) => type.enabled)?.id);
  const connectionSelect = select(`${prefix}-connection`, connectionChoices(connections), githubConnections(connections)[0]?.id);
  const branchInput = element('input', { type: 'text', spellcheck: 'false', autocapitalize: 'none', placeholder: 'main' });
  const addButton = element('button', { type: 'submit', class: 'primary' }, 'Add repo');
  const status = element('div', { role: 'status' });
  const form = element('form', { class: 'stacked-form' },
    field(`${prefix}-repo`, 'Repository', repoInput, 'The project takes the repository’s name.'),
    types.length ? field(typeSelect.id, 'What does this build with?', typeSelect, 'The environment its jobs run in.') : null,
    githubConnections(connections).length ? field(connectionSelect.id, 'GitHub connection', connectionSelect, 'How Armature reads the code and comments on pull requests.')
      : element('p', { class: 'note' }, 'Connect GitHub first to let Armature read this repository.'),
    field(`${prefix}-branch`, 'Default branch', branchInput, 'Leave blank for main.'),
    status, element('div', { class: 'wf-actions' }, addButton));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const { body, error } = projectBody({ repo: repoInput.value, environment_type_id: typeSelect.value, connection_id: connectionSelect.value, default_branch: branchInput.value });
    if (error) {
      clear(status).append(errorBox(new Error(error)));
      return;
    }
    const saved = await attempt(status, addButton, () => api(`/api/teams/${encodeURIComponent(team.id)}/projects`, { method: 'POST', body }));
    if (saved) onSaved(saved.project);
  });
  return form;
}

async function list(root) {
  root.append(pageHeader('Projects'));
  const body = element('div', {}, loading());
  root.append(body);
  let projects;
  let teams;
  try {
    [projects, teams] = await Promise.all([api(PROJECTS_PATH).then((payload) => listOf(payload, 'projects')), api('/api/teams').then((payload) => listOf(payload, 'teams'))]);
  } catch (err) {
    clear(body).append(errorBox(err));
    return;
  }
  const managed = teams.filter((team) => canManageTeam(team.id));
  clear(body).append(card(projects.length
    ? table(['Project', 'Team', 'Repository', 'Default branch'], projects.map((project) => [
      link(hashFor('project', [project.id]), project.name), project.team_name, project.repo || '—', project.default_branch || 'main']))
    : empty(managed.length ? 'No projects yet. Add a repo below.' : 'You are not on any project yet.')));
  if (!managed.length) return;
  const teamSelect = select('projects-team', managed.map((team) => [team.id, team.name]), managed[0].id);
  const slot = element('div');
  const draw = async () => {
    const team = managed.find((candidate) => candidate.id === teamSelect.value);
    const [types, connections] = await Promise.all([
      api(ENVIRONMENT_TYPES_PATH).then((payload) => listOf(payload, 'types')).catch(() => []),
      api(teamConnectionsPath(team.id)).then((payload) => listOf(payload, 'connections')).catch(() => []),
    ]);
    clear(slot).append(repoForm(team, { types, connections, prefix: 'projects-add', onSaved: (project) => { location.hash = hashFor('project', [project.id]); } }));
  };
  teamSelect.addEventListener('change', draw);
  body.append(card(element('h2', { class: 'inline' }, 'Add a repo'), managed.length > 1 ? field('projects-team', 'Team', teamSelect) : null, slot));
  await draw();
}

function repoCard(projectRecord, connections, canManage) {
  if (!canManage) {
    return card(element('h2', { class: 'inline' }, 'Repository'), element('p', {}, projectRecord.repo || 'No repository yet.'),
      meta(`Default branch ${projectRecord.default_branch || 'main'}`,
        `GitHub connection ${connections.find((connection) => connection.id === projectRecord.connection_id)?.name || 'none'}`));
  }
  const repoInput = element('input', { type: 'text', spellcheck: 'false', autocapitalize: 'none', placeholder: 'github.com/owner/name' });
  repoInput.value = projectRecord.repo || '';
  const branchInput = element('input', { type: 'text', spellcheck: 'false', autocapitalize: 'none', placeholder: 'main' });
  branchInput.value = projectRecord.default_branch || '';
  const connectionSelect = select('project-connection', connectionChoices(connections), projectRecord.connection_id);
  const saveButton = element('button', { type: 'submit' }, 'Save');
  const status = element('div', { role: 'status' });
  const form = element('form', { class: 'stacked-form' },
    field('project-repo', 'Repository', repoInput, 'owner/name, or the address from the browser.'),
    field('project-branch', 'Default branch', branchInput, 'Leave blank for main.'),
    field(connectionSelect.id, 'GitHub connection', connectionSelect, 'Its webhook deliveries for this repository start this project’s workflows.'),
    status, element('div', { class: 'wf-actions' }, saveButton));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const body = { repo: repoInput.value.trim() || null, default_branch: branchInput.value.trim() || null, connection_id: connectionSelect.value || null };
    const saved = await attempt(status, saveButton, () => api(projectPath(projectRecord.id), { method: 'POST', body }));
    if (saved) status.append(element('span', { class: 'muted' }, 'Saved.'));
  });
  return card(element('h2', { class: 'inline' }, 'Repository'), form);
}

async function environmentCard(projectRecord) {
  const types = listOf(await api(ENVIRONMENT_TYPES_PATH).catch(() => ({ types: [] })), 'types');
  const typeSelect = select('project-environment-type', environmentTypeOptions(types, projectRecord.environment_type_id), projectRecord.environment_type_id);
  const saveButton = element('button', { type: 'submit' }, 'Save');
  const status = element('div', { role: 'status' });
  const form = element('form', { class: 'wf-actions' }, element('label', { for: typeSelect.id }, 'Environment type'), typeSelect, saveButton);
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const saved = await attempt(status, saveButton, () => api(environmentTypePath(projectRecord.id), { method: 'POST', body: { environment_type_id: typeSelect.value || null } }));
    if (saved) status.append(element('span', { class: 'muted' }, 'Saved.'));
  });
  return card(element('h2', { class: 'inline' }, 'Environment'),
    element('p', { class: 'note' }, 'Jobs for this project that need the source, and that no runner can take, start an environment of this type.'), form, status);
}

const projectProvidersPath = (projectId) => `/api/projects/${encodeURIComponent(projectId)}/providers`;

// A project's aliases win over its team's for the project's runs. The credential is still one of the team's secrets.
async function projectProvidersCard(slot, projectRecord) {
  const canManage = PROJECT_EDITORS.has(projectRecord.role);
  const [providers, teamSecrets] = await Promise.all([
    api(projectProvidersPath(projectRecord.id)).then((payload) => listOf(payload, 'providers')).catch(() => []),
    canManage ? api(`/api/teams/${encodeURIComponent(projectRecord.team_id)}/secrets`).then((payload) => listOf(payload, 'secrets')).catch(() => []) : [],
  ]);
  const section = providersSection({ id: projectRecord.id }, providers, {
    canManage, secretNames: teamSecrets.map((secret) => secret.name).sort(), heading: null,
    reload: () => projectProvidersCard(slot, projectRecord), createPath: projectProvidersPath(projectRecord.id),
  });
  clear(slot).append(card(element('h2', { class: 'inline' }, 'Model providers'),
    element('p', { class: 'note' }, 'Aliases set here win over the team’s for this project’s runs.'), section || empty('No model providers for this project.')));
}

async function workflowsCard(projectRecord) {
  const params = new URLSearchParams({ team: projectRecord.team_id, project: projectRecord.id });
  const [workflows, names] = await Promise.all([api(`/api/workflows?${params}`).then((payload) => listOf(payload, 'workflows')).catch(() => []), teamNames()]);
  return card(element('h2', { class: 'inline' }, 'Workflows'), workflows.length ? workflowTable(workflows, names) : empty('No workflows for this project yet.'));
}

// Webhooks belong to the team. A delivery reaches this project when its repository matches the project's.
async function webhooksCard(projectRecord) {
  const hooks = listOf(await api(webhooksPath(projectRecord.team_id)).catch(() => ({ webhooks: [] })), 'webhooks');
  return card(element('h2', { class: 'inline' }, 'Webhooks'),
    element('p', { class: 'note' }, 'The team’s webhooks. A delivery for this project’s repository starts this project’s workflows.'),
    hooks.length ? table(['Webhook', 'Source', 'Last delivery'], hooks.map((hook) => [link(webhookHref(hook.id), hook.name), SOURCES[hook.source] || hook.source,
      hook.last_delivery?.status || '—'])) : empty('No webhooks yet.'));
}

async function detail(root, projectId) {
  const crumb = element('p', { class: 'crumb' }, link(hashFor('projects'), 'Projects'));
  let projectRecord;
  try {
    projectRecord = (await api(projectPath(projectId))).project;
  } catch (err) {
    root.append(crumb, pageHeader('Project'), errorBox(err));
    return;
  }
  const canManage = PROJECT_EDITORS.has(projectRecord.role);
  const connections = listOf(await api(teamConnectionsPath(projectRecord.team_id)).catch(() => ({ connections: [] })), 'connections');
  root.append(crumb, pageHeader(projectRecord.name || 'Project'), meta(projectRecord.platform ? `Platform ${projectRecord.platform}` : null),
    card(element('p', {}, link(hashFor('runs', [], { team: projectRecord.team_id, project: projectRecord.id }), 'See this project’s runs'))),
    repoCard(projectRecord, connections, canManage));
  if (canManage) root.append(await environmentCard(projectRecord));
  const providersSlot = element('div');
  root.append(providersSlot);
  await projectProvidersCard(providersSlot, projectRecord);
  root.append(await workflowsCard(projectRecord));
  if (canManageTeam(projectRecord.team_id)) root.append(await webhooksCard(projectRecord));
  root.append(connectionsCard({ listPath: projectConnectionsPath(projectRecord.id), canManage, prefix: `connections-${projectRecord.id}`,
    kinds: ['slack', 'jira', 'linear'], note: 'A project’s own connection wins over its team’s of the same kind. A GitHub App connection belongs to the team.' }));
}

export async function render(root, { name, args }) {
  if (name === 'project' && args[0]) return detail(root, args[0]);
  return list(root);
}
