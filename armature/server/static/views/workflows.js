import { api, listOf } from '../api.js';
import { card, element, empty, errorBox, link, pageHeader, table, when } from '../dom.js';
import { hashFor } from '../router.js';
import { mayCreate } from '../scopes.js';
import { currentSession } from '../session.js';
import { detailPage, effectiveText, lintLabel, newPage, scopeText, teamNames, WORKFLOW } from './definition.js';

export function groupByName(workflows, names = new Map()) {
  const groups = new Map();
  for (const workflow of workflows) {
    if (!groups.has(workflow.name)) groups.set(workflow.name, []);
    groups.get(workflow.name).push(workflow);
  }
  const rank = (workflow) => (workflow.scope_kind === 'instance' ? 0 : 1);
  return [...groups].map(([name, copies]) => ({
    name, copies: copies.sort((first, second) => rank(first) - rank(second) || scopeText(first, names).localeCompare(scopeText(second, names))),
  }));
}

function copyItem(workflow, name, names) {
  const usedBy = effectiveText(workflow.effective_for);
  return element('li', {},
    element('a', { href: hashFor('workflow', [workflow.id]) }, element('span', { class: 'visually-hidden' }, `${name} `), `${scopeText(workflow, names)} · v${workflow.current_version}`),
    usedBy !== '—' ? element('span', { class: 'muted' }, ` · used by ${usedBy}`) : null,
    workflow.lint_ok !== true ? [' ', lintLabel(workflow.lint_ok)] : null);
}

export const latest = (copies) => copies.reduce((newest, workflow) => (new Date(workflow.updated_at || 0) > new Date(newest || 0) ? workflow.updated_at : newest), null);

export function workflowTable(workflows, names) {
  return table(['Workflow', 'Kind', 'Copies', 'Updated'], groupByName(workflows, names).map(({ name, copies }) => [
    copies.length === 1 ? link(hashFor('workflow', [copies[0].id]), name) : name,
    [...new Set(copies.map((workflow) => WORKFLOW.kindLabel(workflow)).filter(Boolean))].join(', ') || '—',
    element('ul', { class: 'copy-list' }, copies.map((workflow) => copyItem(workflow, name, names))),
    when(latest(copies)),
  ]));
}

async function list(root, query) {
  root.append(pageHeader('Workflows', mayCreate(currentSession()) ? link('#/new-workflow', 'New workflow', { class: 'btn' }) : null, link('#/workflow-guide', 'How to create a workflow')));
  const names = await teamNames();
  const team = query.get('team') || '';
  const teamSelect = element('select', { id: 'workflow-team' }, element('option', { value: '' }, 'All teams'),
    [...names].map(([id, name]) => element('option', { value: id }, name)));
  teamSelect.value = team;
  teamSelect.addEventListener('change', () => {
    location.hash = hashFor('workflows', [], { team: teamSelect.value, project: query.get('project') });
  });
  root.append(element('div', { class: 'filters' }, element('label', { for: teamSelect.id }, 'Team', teamSelect)));
  let workflows;
  try {
    const params = new URLSearchParams();
    if (team) params.set('team', team);
    if (query.get('project')) params.set('project', query.get('project'));
    workflows = listOf(await api(`/api/workflows?${params}`), 'workflows');
  } catch (err) {
    root.append(errorBox(err));
    return;
  }
  root.append(card(workflows.length ? workflowTable(workflows, names) : empty('No workflows yet.')));
}

export async function render(root, { name, args, query }) {
  if (name === 'new-workflow') return newPage(root, WORKFLOW, null, query.get('scope'), query.get('describe'));
  if (name === 'workflow' && args[0]) return detailPage(root, WORKFLOW, args[0], args[1]);
  return list(root, query);
}
