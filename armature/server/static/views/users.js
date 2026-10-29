import { api, listOf } from '../api.js';
import { attempt, card, clear, confirmDialog, element, empty, errorBox, loading, pageHeader, table } from '../dom.js';
import { canManageTeam, isAdmin, requiresTotp, user as me } from '../session.js';

const INVITE_ROLES_FOR_LEADS = ['project_lead', 'contributor', 'viewer'];

const ROLE_LABELS = { team_lead: 'Team lead', project_lead: 'Project lead', contributor: 'Contributor', viewer: 'Viewer' };
const roleLabel = (role) => ROLE_LABELS[role] || role;

// Every team and project this viewer may grant access to, with the team each project belongs to.
async function grantableScopes() {
  const teams = listOf(await api('/api/teams'), 'teams').filter((team) => canManageTeam(team.id));
  const projectLists = await Promise.all(teams.map((team) => api(`/api/teams/${encodeURIComponent(team.id)}/projects`)
    .then((payload) => listOf(payload, 'projects')).catch(() => [])));
  const projects = projectLists.flatMap((teamProjects, index) => teamProjects.map((project) => ({ ...project, teamName: teams[index].name })));
  return { teams, projects };
}

function canRemove(membership, scopes) {
  if (isAdmin()) return true;
  if (membership.scope_kind === 'team') return canManageTeam(membership.scope_id);
  return scopes.projects.some((project) => String(project.id) === String(membership.scope_id));
}

function membershipChips(account, scopes, status, reload) {
  const held = account.memberships || [];
  if (!held.length) return account.is_admin ? 'Admin, sees everything' : 'None';
  return element('div', { class: 'chips' }, held.map((membership) => {
    const text = `${membership.scope_name || membership.scope_id}: ${roleLabel(membership.role)}`;
    if (!canRemove(membership, scopes)) return element('span', { class: 'chip' }, text);
    const removeButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `Remove ${account.username} from ${membership.scope_name || membership.scope_id}` }, 'Remove');
    removeButton.addEventListener('click', async () => {
      if (!(await confirmDialog(`Remove ${account.username}?`, `They lose their ${roleLabel(membership.role).toLowerCase()} access to ${membership.scope_name || membership.scope_id}.`, 'Remove'))) return;
      if (await attempt(status, removeButton, () => api(`/api/memberships/${encodeURIComponent(membership.id)}/delete`, { method: 'POST', body: {} }))) reload();
    });
    return element('span', { class: 'chip' }, text, ' ', removeButton);
  }));
}

function accountStatus(account) {
  if (account.disabled) return element('span', { class: 'chip warn' }, 'Disabled');
  if (account.is_admin) return element('span', { class: 'chip ok' }, 'Admin');
  return element('span', { class: 'chip ok' }, 'Active');
}

function disableButton(account, status, reload) {
  if (!isAdmin() || account.id === me()?.id) return null;
  const nextDisabled = !account.disabled;
  const button = element('button', { type: 'button', class: 'link-button', 'aria-label': `${nextDisabled ? 'Disable' : 'Enable'} ${account.username}` }, nextDisabled ? 'Disable' : 'Enable');
  button.addEventListener('click', async () => {
    if (nextDisabled && !(await confirmDialog(`Disable ${account.username}?`, 'They are signed out everywhere and cannot sign in until an admin enables them again.', 'Disable'))) return;
    if (await attempt(status, button, () => api(`/api/users/${encodeURIComponent(account.id)}`, { method: 'POST', body: { disabled: nextDisabled } }))) reload();
  });
  return button;
}

function showInvite(shownOnce, username, inviteUrl) {
  clear(shownOnce).append(element('div', { class: 'banner', role: 'status' },
    element('p', {}, `Shown once. Send this invite link to ${username} directly, not in a shared channel. It works once, for 72 hours.`),
    element('p', { class: 'mono wrap-anywhere' }, inviteUrl)));
}

function inviteButton(account, status, shownOnce, reload) {
  if (account.sso || account.id === me()?.id) return null;
  const isReset = account.totp_confirmed || (!requiresTotp() && account.has_password);
  if (isReset && !isAdmin()) return null;
  const label = isReset ? 'Reset sign-in' : 'New invite';
  const button = element('button', { type: 'button', class: 'link-button', 'aria-label': `${label} for ${account.username}` }, label);
  button.addEventListener('click', async () => {
    if (isReset && !(await confirmDialog(`Reset sign-in for ${account.username}?`, `Their ${requiresTotp() ? 'password and authenticator are' : 'password is'} cleared and they are signed out everywhere. They sign in again only through the new invite link.`, 'Reset'))) return;
    const issued = await attempt(status, button, () => api(`/api/users/${encodeURIComponent(account.id)}/invite`, { method: 'POST', body: {} }));
    if (issued?.invite_url) {
      await reload();
      showInvite(shownOnce, account.username, issued.invite_url);
    }
  });
  return button;
}

function lookupControl(userSelect) {
  const queryInput = element('input', { type: 'text', id: 'grant-lookup', maxlength: 191, autocapitalize: 'none', spellcheck: 'false', autocomplete: 'off' });
  const findButton = element('button', { type: 'button' }, 'Find');
  const status = element('div', { role: 'status', class: 'note' });
  findButton.addEventListener('click', async () => {
    const found = await attempt(status, findButton, () => api(`/api/users/lookup?q=${encodeURIComponent(queryInput.value.trim())}`));
    const matches = listOf(found, 'users');
    if (found && !matches.length) status.textContent = 'No user has exactly that username or email.';
    for (const match of matches) {
      if (![...userSelect.options].some((option) => option.value === match.id)) userSelect.append(element('option', { value: match.id }, match.username));
      userSelect.value = match.id;
    }
  });
  return element('div', {},
    element('label', { class: 'stacked', for: queryInput.id }, 'Find someone not listed, by exact username or email', element('div', { class: 'wf-actions' }, queryInput, findButton)),
    status);
}

function grantForm(accounts, scopes, reload) {
  const userSelect = element('select', { id: 'grant-user', required: true }, accounts.map((account) => element('option', { value: account.id }, account.username)));
  const scopeSelect = element('select', { id: 'grant-scope', required: true },
    scopes.teams.length ? element('optgroup', { label: 'Teams' }, scopes.teams.map((team) => element('option', { value: `team:${team.id}` }, team.name))) : null,
    scopes.projects.length ? element('optgroup', { label: 'Projects' }, scopes.projects.map((project) => element('option', { value: `project:${project.id}` }, `${project.name} (${project.teamName})`))) : null);
  const roleSelect = element('select', { id: 'grant-role' }, Object.entries(ROLE_LABELS).map(([role, label]) => element('option', { value: role }, label)));
  roleSelect.value = 'contributor';
  const grantButton = element('button', { type: 'submit', class: 'primary' }, 'Grant access');
  const status = element('div', { role: 'status' });
  const form = element('form', { class: 'stacked-form' },
    element('h2', { class: 'inline' }, 'Grant access'),
    element('label', { class: 'stacked', for: userSelect.id }, 'User', userSelect),
    lookupControl(userSelect),
    element('label', { class: 'stacked', for: scopeSelect.id }, 'Team or project', scopeSelect),
    element('label', { class: 'stacked', for: roleSelect.id }, 'Role', roleSelect),
    element('p', { class: 'note' }, 'A team lead manages a whole team. The other roles usually apply to one project.'),
    element('div', { class: 'wf-actions' }, grantButton), status);
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const separator = scopeSelect.value.indexOf(':');
    const granted = await attempt(status, grantButton, () => api('/api/memberships', {
      method: 'POST',
      body: { user_id: userSelect.value, scope_kind: scopeSelect.value.slice(0, separator), scope_id: scopeSelect.value.slice(separator + 1), role: roleSelect.value },
    }));
    if (granted) reload();
  });
  return card(form);
}

function createForm(scopes, reload, shownOnce) {
  const usernameInput = element('input', { type: 'text', id: 'new-username', required: true, maxlength: 40, autocapitalize: 'none', spellcheck: 'false', autocomplete: 'off' });
  const nameInput = element('input', { type: 'text', id: 'new-display-name', maxlength: 120, autocomplete: 'off' });
  const emailInput = element('input', { type: 'email', id: 'new-email', maxlength: 191, autocomplete: 'off' });
  const teamSelect = element('select', { id: 'new-team', required: !isAdmin() },
    isAdmin() ? element('option', { value: '' }, 'No team') : null,
    scopes.teams.map((team) => element('option', { value: team.id }, team.name)));
  const roles = isAdmin() ? Object.keys(ROLE_LABELS) : INVITE_ROLES_FOR_LEADS;
  const roleSelect = element('select', { id: 'new-role' }, roles.map((role) => element('option', { value: role }, roleLabel(role))));
  roleSelect.value = 'contributor';
  const createButton = element('button', { type: 'submit', class: 'primary' }, 'Invite user');
  const status = element('div', { role: 'status' });
  const form = element('form', { class: 'stacked-form' },
    element('h2', { class: 'inline' }, 'Invite a user'),
    element('p', { class: 'note' }, `You get a link to send them. It works once, for 72 hours, and they set their own ${requiresTotp() ? 'password and authenticator' : 'password'} with it.`),
    element('label', { class: 'stacked', for: usernameInput.id }, 'Username', usernameInput),
    element('label', { class: 'stacked', for: nameInput.id }, 'Display name', nameInput),
    element('label', { class: 'stacked', for: emailInput.id }, 'Email', emailInput),
    element('label', { class: 'stacked', for: teamSelect.id }, 'Team', teamSelect),
    element('label', { class: 'stacked', for: roleSelect.id }, 'Role on the team', roleSelect),
    element('div', { class: 'wf-actions' }, createButton), status);
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const body = { username: usernameInput.value.trim(), display_name: nameInput.value.trim(), email: emailInput.value.trim() };
    if (teamSelect.value) Object.assign(body, { team_id: teamSelect.value, role: roleSelect.value });
    const created = await attempt(status, createButton, () => api('/api/users', { method: 'POST', body }));
    if (!created) return;
    await reload();
    if (created.invite_url) showInvite(shownOnce, body.username, created.invite_url);
  });
  return card(form);
}

// Admins link a user's Slack account, so a Slack approval counts only from someone an admin vouched for.
function slackForm(accounts, reload) {
  const field = (id, label, placeholder) => {
    const input = element('input', { type: 'text', id, name: id, placeholder, autocomplete: 'off', spellcheck: 'false', class: 'mono' });
    return { input, node: element('label', { class: 'stacked', for: id }, label, input) };
  };
  const picker = element('select', { id: 'slack-user', name: 'slack-user' }, accounts.map((account) => element('option', { value: account.id }, account.username)));
  const workspace = field('slack-workspace', 'Slack workspace id', 'T0123ABCD');
  const member = field('slack-member', 'Slack member id', 'U0123ABCD');
  const fill = () => {
    const account = accounts.find((candidate) => candidate.id === picker.value);
    workspace.input.value = account?.slack_workspace_id || '';
    member.input.value = account?.slack_user_id || '';
  };
  picker.addEventListener('change', fill);
  fill();
  const status = element('div', { role: 'status', class: 'note' });
  const saveButton = element('button', { type: 'submit', class: 'primary' }, 'Save Slack account');
  const form = element('form', { class: 'stacked-form' }, element('h2', { class: 'inline' }, 'Slack account'),
    element('p', { class: 'note' }, 'Slack approvals count only from a linked account. Leave both ids empty to unlink.'),
    element('label', { class: 'stacked', for: picker.id }, 'User', picker), workspace.node, member.node,
    element('div', { class: 'wf-actions' }, saveButton), status);
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const saved = await attempt(status, saveButton, () => api(`/api/users/${encodeURIComponent(picker.value)}/slack`, { method: 'POST', body: { workspace_id: workspace.input.value, user_id: member.input.value } }));
    if (saved?.user) reload();
  });
  return card(form);
}

export async function render(root) {
  root.append(pageHeader('Users'));
  const shownOnce = element('div');
  const listSlot = element('div', {}, loading());
  const formsSlot = element('div');
  const status = element('div', { role: 'status' });
  root.append(shownOnce, status, listSlot, formsSlot);

  async function reload() {
    let accounts;
    let scopes;
    try {
      [accounts, scopes] = await Promise.all([api('/api/users').then((payload) => listOf(payload, 'users')), grantableScopes()]);
    } catch (err) {
      clear(listSlot).append(errorBox(err));
      return;
    }
    clear(shownOnce);
    clear(listSlot).append(card(accounts.length ? table(['Username', 'Name', 'Email', 'Access', 'Status', ''], accounts.map((account) => [
      element('span', { class: 'mono' }, account.username, account.id === me()?.id ? ' (you)' : ''),
      account.display_name || '—', account.email || '—', membershipChips(account, scopes, status, reload), accountStatus(account),
      element('span', { class: 'wf-actions' }, disableButton(account, status, reload), inviteButton(account, status, shownOnce, reload)),
    ])) : empty('No users yet.')));
    clear(formsSlot);
    if (scopes.teams.length || scopes.projects.length) formsSlot.append(grantForm(accounts, scopes, reload));
    if (isAdmin() || scopes.teams.length) formsSlot.append(createForm(scopes, reload, shownOnce));
    if (isAdmin() && accounts.length) formsSlot.append(slackForm(accounts, reload));
  }

  await reload();
}
