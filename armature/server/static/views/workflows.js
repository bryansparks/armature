import { api, listOf } from '../api.js';
import { card, element, empty, errorBox, link, pageHeader, table, when } from '../dom.js';
import { hashFor } from '../router.js';
import { mayCreate } from '../scopes.js';
import { currentSession } from '../session.js';
import { detailPage, effectiveText, lintLabel, newPage, scopeText, teamNames, WORKFLOW } from './definition.js';

export function workflowTable(workflows, names) {
  return table(['Workflow', 'Kind', 'Scope', ['Version', 'num'], 'Lint', 'Used by', 'Updated'], workflows.map((workflow) => [
    link(hashFor('workflow', [workflow.id]), workflow.name), WORKFLOW.kindLabel(workflow) || '—', scopeText(workflow, names), String(workflow.current_version),
    lintLabel(workflow.lint_ok), effectiveText(workflow.effective_for), when(workflow.updated_at),
  ]));
}

async function list(root, query) {
  root.append(pageHeader('Workflows', mayCreate(currentSession()) ? link('#/new-workflow', 'New workflow', { class: 'btn' }) : null));
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
  if (name === 'new-workflow') return newPage(root, WORKFLOW, null, query.get('scope'));
  if (name === 'workflow' && args[0]) return detailPage(root, WORKFLOW, args[0], args[1]);
  return list(root, query);
}
