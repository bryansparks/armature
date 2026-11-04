import { outcomeChip } from '../actions.js';
import { api, listOf } from '../api.js';
import { ago, append, card, clear, element, empty, errorBox, link, loading, meta, pageHeader, table, when } from '../dom.js';
import { every } from '../live.js';
import { renderMarkdown } from '../markdown.js';
import { hashFor } from '../router.js';
import { residentsPath } from './residents.js';

const REFRESH_MS = 30000;
const OPERATIONS_LIMIT = 50;

export function updatedText(status) {
  if (!status?.configured) return 'No status command is configured on this server.';
  if (!status.updated_at) return 'Waiting for the first report from the host.';
  return `Updated ${ago(status.updated_at)}`;
}

function operationsTable(operations) {
  return operations.length ? table(['Started', 'Action', 'Run', 'Outcome', 'Message'], operations.map((operation) => [
    when(operation.started_at), operation.action_id, link(hashFor('run', [operation.run_id]), 'Open run'),
    outcomeChip(operation.outcome), operation.message || '',
  ])) : empty('No operations yet.');
}

// The status command knows nothing about residents, and no endpoint aggregates them, so the
// counts come from each workflow's instance list — fetched once per page view, never on the
// 30s poll, to stay inside the residents read throttle.
async function drawResidents(slot) {
  try {
    const workflows = listOf(await api('/api/workflows'), 'workflows');
    const lists = await Promise.all(workflows.map((workflow) =>
      api(residentsPath(workflow.id))
        .then((payload) => ({ workflow, residents: listOf(payload, 'residents') }))
        .catch(() => null)));
    const rows = lists.filter((entry) => entry && entry.residents.length);
    const open = rows.reduce((sum, { residents }) => sum + residents.filter((resident) => resident.state !== 'closed').length, 0);
    clear(slot).append(rows.length
      ? [meta(`Open instances: ${open}`),
        table(['Workflow', 'Instances', 'Open', 'Waiting'], rows.map(({ workflow, residents }) => [
          link(hashFor('workflow', [workflow.id, 'residents']), workflow.name),
          String(residents.length),
          String(residents.filter((resident) => resident.state !== 'closed').length),
          String(residents.reduce((sum, resident) => sum + Number(resident.inbox || 0), 0)),
        ]))]
      : empty('No resident instances yet.'));
  } catch (err) {
    clear(slot).append(errorBox(err));
  }
}

export async function render(root, { signal }) {
  const updated = element('span', { class: 'muted', role: 'status' });
  const errorSlot = element('div', {});
  const body = element('div', {});
  const residentsSlot = element('div', {}, loading());
  const operationsSlot = element('div', {});
  root.append(pageHeader('Status', updated), errorSlot, card(body));
  root.append(element('h2', {}, 'Residents'), card(residentsSlot));
  root.append(element('h2', {}, 'Operations'), card(operationsSlot));
  await drawResidents(residentsSlot);

  const draw = async () => {
    let status;
    try {
      status = await api('/api/status');
    } catch (err) {
      clear(errorSlot).append(errorBox(err));
      return;
    }
    updated.textContent = updatedText(status);
    const environments = status.environments?.message || (status.environments?.reasons || []).join(' ');
    append(clear(errorSlot), [status.error ? element('div', { class: 'banner warn', role: 'alert' }, status.error) : null,
      environments ? element('div', { class: 'banner warn', role: 'status' }, `Environments: ${environments}`) : null]);
    clear(body).append(status.markdown ? renderMarkdown(status.markdown) : empty('Nothing to show yet.'));
    try {
      clear(operationsSlot).append(operationsTable(listOf(await api(`/api/operations?limit=${OPERATIONS_LIMIT}`), 'operations')));
    } catch (err) {
      clear(operationsSlot).append(errorBox(err));
    }
  };
  await draw();
  every(REFRESH_MS, draw, signal);
}
