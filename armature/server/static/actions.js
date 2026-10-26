import { api, listOf } from './api.js';
import { append, clear, confirmDialog, element, errorBox } from './dom.js';

const OUTCOME_TONE = { ok: 'ok', refused: 'warn', failed: 'bad' };
const OUTCOME_WORD = { ok: 'Done', refused: 'Refused', failed: 'Failed' };

const actionsPath = (runId) => `/api/runs/${encodeURIComponent(runId)}/actions`;
const actionPath = (runId, actionId) => `/api/runs/${encodeURIComponent(runId)}/actions/${encodeURIComponent(actionId)}`;

// The server is the real gate, so a disabled action is simply not offered.
export function enabledActions(payload) {
  return listOf(payload, 'actions').filter((action) => action && action.enabled === true && typeof action.id === 'string');
}

export const outcomeTone = (outcome) => OUTCOME_TONE[outcome] || 'warn';

export function outcomeText(operation) {
  const word = OUTCOME_WORD[operation?.outcome] || 'Unknown';
  const message = typeof operation?.message === 'string' ? operation.message.trim() : '';
  return message ? `${word}: ${message}` : word;
}

export function confirmText(action) {
  return typeof action?.confirm === 'string' && action.confirm.trim() ? action.confirm.trim() : `Run "${action?.label || action?.id}" on this run?`;
}

export function outcomeChip(outcome) {
  return element('span', { class: `chip ${outcomeTone(outcome)}` }, OUTCOME_WORD[outcome] || 'Running');
}

// Buttons for the run's enabled actions, each confirmed, then the operation's outcome beside them.
export function actionsBar(runId) {
  const buttons = element('div', { class: 'run-actions' });
  const result = element('div', { role: 'status' });
  const node = element('div', {}, buttons, result);
  let busy = false;

  const press = async (action, button) => {
    if (busy) return;
    if (!(await confirmDialog(action.label, confirmText(action), action.label))) return;
    busy = true;
    for (const other of buttons.querySelectorAll('button')) other.disabled = true;
    clear(result).append(element('p', { class: 'muted' }, `${action.label}…`));
    try {
      const { operation } = await api(actionPath(runId, action.id), { method: 'POST', body: {} });
      clear(result).append(element('div', { class: `banner ${outcomeTone(operation?.outcome)}` }, outcomeText(operation)));
    } catch (err) {
      clear(result).append(errorBox(err));
    } finally {
      busy = false;
      button.disabled = false;
      await refresh();
    }
  };

  const refresh = async () => {
    let offered = [];
    try {
      offered = enabledActions(await api(actionsPath(runId)));
    } catch {
      offered = [];
    }
    append(clear(buttons), offered.map((action) => {
      const button = element('button', { type: 'button' }, action.label);
      button.disabled = busy;
      button.addEventListener('click', () => press(action, button));
      return button;
    }));
  };

  return { node, refresh };
}
