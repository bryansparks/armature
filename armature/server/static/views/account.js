import { api } from '../api.js';
import { attempt, card, element, empty, meta, pageHeader, table } from '../dom.js';
import { isAdmin, isSso, memberships, user } from '../session.js';
import { signOut } from '../app.js';

const ROLE_LABELS = { team_lead: 'Team lead', project_lead: 'Project lead', contributor: 'Contributor', viewer: 'Viewer' };

function passwordForm() {
  const field = (id, label, autocomplete) => {
    const input = element('input', { type: 'password', id, name: id, autocomplete, required: true });
    return { input, node: element('label', { class: 'stacked', for: id }, label, input) };
  };
  const current = field('current-password', 'Current password', 'current-password');
  const next = field('new-password', 'New password', 'new-password');
  const again = field('new-password-again', 'New password again', 'new-password');
  const status = element('div', { role: 'status', class: 'note' });
  const saveButton = element('button', { type: 'submit', class: 'primary' }, 'Save password');
  const form = element('form', { class: 'stacked-form' },
    element('h2', { class: 'inline' }, 'Change password'), current.node, next.node, again.node, element('div', { class: 'wf-actions' }, saveButton), status);
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    if (next.input.value !== again.input.value) {
      status.textContent = 'The two new passwords differ.';
      again.input.focus();
      return;
    }
    const saved = await attempt(status, saveButton, () => api('/api/account/password', { method: 'POST', body: { current: current.input.value, new: next.input.value } }));
    for (const { input } of [current, next, again]) input.value = '';
    if (saved) status.textContent = 'Saved.';
  });
  return card(form);
}

export const linkedText = (account) => (account.slack_user_id
  ? `Linked to Slack member ${account.slack_user_id} in workspace ${account.slack_workspace_id}. Your Slack approvals count as yours.`
  : 'Not linked. Link your Slack account before you approve gates in Slack.');

// The bot sends a code to the Slack account in a direct message, so only its holder can link it.
function slackForm(account) {
  const field = (id, label, attributes = {}) => {
    const input = element('input', { type: 'text', id, name: id, autocomplete: 'off', ...attributes });
    return { input, node: element('label', { class: 'stacked', for: id }, label, input) };
  };
  const linked = element('p', { class: 'note' }, linkedText(account));
  const workspace = field('slack-workspace', 'Slack workspace id', { placeholder: 'T0123ABCD' });
  const member = field('slack-member', 'Your Slack member id', { placeholder: 'U0123ABCD' });
  const code = field('slack-code', 'Code from the Slack message', { inputmode: 'numeric', maxlength: 6 });
  const status = element('div', { role: 'status', class: 'note' });
  const sendButton = element('button', { type: 'button' }, 'Send me a code');
  const confirmButton = element('button', { type: 'button', class: 'primary' }, 'Link');
  const confirmRow = element('div', { class: 'stacked-form', hidden: true }, code.node, element('div', { class: 'wf-actions' }, confirmButton));
  sendButton.addEventListener('click', async () => {
    const sent = await attempt(status, sendButton, () => api('/api/account/slack/start', { method: 'POST', body: { workspace_id: workspace.input.value, user_id: member.input.value } }));
    if (sent) {
      status.textContent = 'Sent. Check your direct messages from the Armature bot.';
      confirmRow.hidden = false;
      code.input.focus();
    }
  });
  confirmButton.addEventListener('click', async () => {
    const done = await attempt(status, confirmButton, () => api('/api/account/slack/confirm', { method: 'POST', body: { code: code.input.value } }));
    if (done) {
      linked.textContent = linkedText(done);
      confirmRow.hidden = true;
      code.input.value = '';
      status.textContent = 'Linked.';
    }
  });
  return card(element('h2', { class: 'inline' }, 'Slack'), linked,
    element('div', { class: 'stacked-form' }, workspace.node, member.node, element('div', { class: 'wf-actions' }, sendButton)),
    confirmRow, status);
}

export async function render(root) {
  const account = user();
  root.append(pageHeader('Account'));
  root.append(meta(element('span', { class: 'mono' }, account.username || '—'), account.display_name, isAdmin() ? 'Admin' : null));
  const held = memberships();
  root.append(card(element('h2', { class: 'inline' }, 'Your access'), held.length
    ? table(['Team or project', 'Kind', 'Role'], held.map((membership) => [membership.scope_name || membership.scope_id, membership.scope_kind, ROLE_LABELS[membership.role] || membership.role]))
    : empty(isAdmin() ? 'Admins can see and change everything.' : 'You are not on any team or project yet.')));
  root.append(slackForm(account));
  if (isSso()) root.append(card(element('p', { class: 'note' }, 'You sign in with single sign-on, so your password is managed there.')));
  else root.append(passwordForm());
  root.append(card(element('button', { type: 'button', onclick: signOut }, 'Sign out')));
}
