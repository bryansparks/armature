import { outcomeChip } from '../actions.js';
import { api, listOf } from '../api.js';
import { ago, append, card, clear, element, empty, errorBox, link, pageHeader, table, when } from '../dom.js';
import { every } from '../live.js';
import { renderMarkdown } from '../markdown.js';
import { hashFor } from '../router.js';

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

export async function render(root, { signal }) {
  const updated = element('span', { class: 'muted', role: 'status' });
  const errorSlot = element('div', {});
  const body = element('div', {});
  const operationsSlot = element('div', {});
  root.append(pageHeader('Status', updated), errorSlot, card(body));
  root.append(element('h2', {}, 'Operations'), card(operationsSlot));

  const draw = async () => {
    let status;
    try {
      status = await api('/api/status');
    } catch (err) {
      clear(errorSlot).append(errorBox(err));
      return;
    }
    updated.textContent = updatedText(status);
    append(clear(errorSlot), [status.error ? element('div', { class: 'banner warn', role: 'alert' }, status.error) : null]);
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
