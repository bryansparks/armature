import { api, listOf } from '../api.js';
import { ago, attempt, card, element, empty, errorBox, link, pageHeader, statusChip, when } from '../dom.js';
import { renderMarkdown } from '../markdown.js';
import { hashFor } from '../router.js';

const DECIDED = { approved: 'Approved', rejected: 'Rejected', expired: 'Expired', canceled: 'Canceled' };

export function gatePath(gateId, action = '') {
  const id = encodeURIComponent(gateId);
  if (action === 'approve') return `/api/gates/${id}/approve`;
  if (action === 'reject') return `/api/gates/${id}/reject`;
  return `/api/gates/${id}`;
}

export function decisionText(gate) {
  const verb = DECIDED[gate.state];
  if (!verb) return '';
  const who = gate.decided_by_name ? ` by ${gate.decided_by_name}` : '';
  return `${verb}${who}${gate.decided_at ? ` ${when(gate.decided_at)}` : ''}${gate.decision_note && gate.state !== 'expired' ? `: ${gate.decision_note}` : ''}`;
}

export const waitingForYou = (gates) => gates.filter((gate) => gate.state === 'open' && gate.may_decide);

// The question a gate asks, its decision once made, and Approve and Reject while it is open and the viewer may decide.
export function gateCard(gate, onDecided) {
  const question = element('div', { class: 'gate-question' }, renderMarkdown(gate.present || 'Review required.'));
  const parts = [
    element('div', { class: 'gate-head' }, element('strong', {}, gate.step_id), ' ', statusChip(gate.state === 'open' ? 'waiting' : gate.state)),
    question,
  ];
  if (gate.card_error) parts.push(element('p', { class: 'note bad' }, `The Slack card could not be sent: ${gate.card_error}`));
  if (gate.state !== 'open') {
    parts.push(element('p', { class: 'muted' }, decisionText(gate)));
    return card(...parts);
  }
  parts.push(element('p', { class: 'muted' }, `Waiting since ${when(gate.created_at)}. Expires ${when(gate.expires_at)}, then it counts as rejected.`));
  if (!gate.may_decide) {
    parts.push(element('p', { class: 'note' }, gate.min_role === 'project_lead' ? 'A project lead decides this.' : `A ${gate.min_role.replace('_', ' ')} decides this.`));
    return card(...parts);
  }
  const noteId = `gate-note-${gate.id}`;
  const note = element('textarea', { id: noteId, rows: 2, maxlength: 2000 });
  const status = element('div', { role: 'status' });
  const decide = (action, button) => async () => {
    const answer = await attempt(status, button, () => api(gatePath(gate.id, action), { method: 'POST', body: { nonce: gate.nonce, note: note.value } }));
    if (answer?.gate) onDecided?.(answer.gate);
  };
  const approve = element('button', { type: 'button', class: 'primary' }, 'Approve');
  const reject = element('button', { type: 'button' }, 'Reject');
  approve.addEventListener('click', decide('approve', approve));
  reject.addEventListener('click', decide('reject', reject));
  parts.push(element('label', { class: 'stacked', for: noteId }, 'Note (optional)', note), element('div', { class: 'wf-actions' }, approve, reject), status);
  return card(...parts);
}

export async function render(root, { signal }) {
  root.append(pageHeader('Waiting for you'));
  const body = element('div', {});
  root.append(body);
  const draw = async () => {
    let gates;
    try {
      gates = listOf(await api('/api/gates?state=open'), 'gates');
    } catch (err) {
      body.replaceChildren(errorBox(err));
      return;
    }
    if (signal?.aborted) return;
    const mine = waitingForYou(gates);
    body.replaceChildren(mine.length
      ? element('div', {}, mine.map((gate) => element('section', { class: 'gate' },
        element('p', { class: 'crumb' }, link(hashFor('run', [gate.run_id]), gate.workflow || 'Run'), ` · ${ago(gate.created_at)}`),
        gateCard(gate, draw))))
      : card(empty('Nothing is waiting for you.')));
  };
  await draw();
}
