import { api, listOf } from '../api.js';
import { attempt, card, clear, confirmDialog, element, empty, errorBox, loading, statusChip, table, when } from '../dom.js';

export const KINDS = {
  github_app: {
    label: 'GitHub',
    hint: 'For a GitHub App, its client id and a private key from the app’s settings page. Verify finds the app’s installations and checks each can write contents, pull requests, issues, and checks. For the gh CLI, run gh auth login as the server’s user and leave the app fields blank. Verify checks the login has the repo scope. The gh CLI can’t make check runs.',
    config: [['auth', 'Sign in as', '', [['', 'A GitHub App'], ['gh_cli', 'The gh CLI login of the server’s user']]],
      ['host', 'gh host (gh CLI only, optional)', 'github.com'], ['client_id', 'Client id (App only)', 'Iv23li…'], ['app_id', 'App id (optional)', '']],
    secrets: [['private_key_pem', 'Private key (PEM, App only)', 'textarea'], ['webhook_secret', 'Webhook secret (optional)', 'password']],
  },
  slack: {
    label: 'Slack',
    hint: 'The bot token (xoxb-) from OAuth & Permissions and the signing secret from Basic Information. Verify checks the token. The signing secret is checked on the first delivery. A webhook using this connection refuses deliveries until Verify has found the workspace.',
    config: [['team_id', 'Workspace id (optional)', 'T0…'], ['app_id', 'App id (optional)', 'A0…']],
    secrets: [['bot_token', 'Bot token', 'password'], ['signing_secret', 'Signing secret', 'password']],
  },
  jira: {
    label: 'Jira',
    hint: 'Your Jira Cloud site, and the email and API token of the account that comments on and moves issues.',
    config: [['site_url', 'Site', 'your-site.atlassian.net'], ['email', 'Account email', '']],
    secrets: [['api_token', 'API token', 'password'], ['webhook_secret', 'Webhook secret (optional)', 'password']],
  },
  linear: {
    label: 'Linear',
    hint: 'A personal API key from Linear’s settings, under Security & access.',
    config: [],
    secrets: [['api_key', 'API key', 'password'], ['webhook_secret', 'Webhook secret (optional)', 'password']],
  },
};

export const teamConnectionsPath = (teamId) => `/api/teams/${encodeURIComponent(teamId)}/connections`;
export const projectConnectionsPath = (projectId) => `/api/projects/${encodeURIComponent(projectId)}/connections`;
export const connectionPath = (connectionId) => `/api/connections/${encodeURIComponent(connectionId)}`;

// Blank credentials stay out of the body so the server keeps what it has. Blank config goes as null to clear it.
export function connectionBody(kind, values, { creating = true } = {}) {
  const spec = KINDS[kind];
  const body = { name: String(values.name ?? '').trim() };
  if (creating) body.kind = kind;
  for (const [name] of spec.config) body[name] = String(values[name] ?? '').trim() || null;
  for (const [name] of spec.secrets) {
    const value = String(values[name] ?? '').trim();
    if (value) body[name] = value;
  }
  return body;
}

export function credentialText(connection) {
  const hints = connection?.credentials || {};
  if (hints.unreadable) return 'Can’t be read. Enter them again.';
  const labels = Object.fromEntries((KINDS[connection?.kind]?.secrets || []).map(([name, label]) => [name, label.replace(/ \(.*\)$/, '')]));
  const set = Object.entries(hints).filter(([, hint]) => hint?.set)
    .map(([name, hint]) => (hint.last4 ? `${labels[name] || name} ending ${hint.last4}` : labels[name] || name));
  return set.length ? set.join(', ') : 'Not set';
}

export function identityText(connection) {
  const found = connection?.identity || {};
  if (connection?.kind === 'github_app') {
    const owners = (connection.installations || []).map((installation) => installation.owner);
    return [found.bot_login || (found.login && `${found.login} (gh CLI)`), owners.length ? `installed on ${owners.join(', ')}` : null].filter(Boolean).join(', ');
  }
  if (connection?.kind === 'slack') return [found.bot_user && `@${found.bot_user}`, found.team].filter(Boolean).join(' in ');
  if (connection?.kind === 'jira') return found.display || '';
  if (connection?.kind === 'linear') return [found.user, found.org].filter(Boolean).join(' in ');
  return '';
}

function field(id, label, input) {
  input.id = id;
  return element('label', { class: 'stacked', for: id }, label, input);
}

function connectionForm(prefix, createPath, editing, onDone, kinds) {
  const offered = editing ? [editing.kind] : kinds;
  const kindSelect = element('select', { id: `${prefix}-kind` }, offered.map((value) => element('option', { value }, KINDS[value].label)));
  kindSelect.value = editing?.kind || offered[0];
  kindSelect.disabled = Boolean(editing);
  const nameInput = element('input', { type: 'text', required: true, maxlength: 100, spellcheck: 'false', autocapitalize: 'none', placeholder: 'main' });
  nameInput.value = editing?.name || '';
  const inputs = {};
  const sections = Object.fromEntries(Object.entries(KINDS).map(([kind, spec]) => {
    const hint = element('p', { class: 'note' }, spec.hint);
    const configFields = spec.config.map(([name, label, placeholder, options]) => {
      const input = options
        ? element('select', {}, options.map(([value, text]) => element('option', { value }, text)))
        : element('input', { type: 'text', spellcheck: 'false', autocapitalize: 'none', placeholder });
      input.value = editing?.kind === kind ? editing.config?.[name] ?? '' : '';
      inputs[`${kind}.${name}`] = input;
      return field(`${prefix}-${kind}-${name}`, label, input);
    });
    // Write-only: the page never receives these back, so they always start blank.
    const secretFields = spec.secrets.map(([name, label, type]) => {
      const input = type === 'textarea'
        ? element('textarea', { class: 'mono', rows: 4, spellcheck: 'false', autocomplete: 'off' })
        : element('input', { type, autocomplete: 'off', spellcheck: 'false', autocapitalize: 'none' });
      inputs[`${kind}.${name}`] = input;
      return field(`${prefix}-${kind}-${name}`, `${label}${editing?.credentials?.[name]?.set ? ' (leave blank to keep)' : ''}`, input);
    });
    return [kind, element('div', {}, hint, ...configFields, ...secretFields)];
  }));
  const sync = () => {
    for (const [kind, section] of Object.entries(sections)) section.hidden = kind !== kindSelect.value;
  };
  kindSelect.addEventListener('change', sync);
  sync();
  const status = element('div', { role: 'status' });
  const saveButton = element('button', { type: 'submit', class: 'primary' }, editing ? 'Save connection' : 'Add connection');
  const cancelButton = editing ? element('button', { type: 'button' }, 'Cancel') : null;
  cancelButton?.addEventListener('click', () => onDone(false));
  const form = element('form', { class: 'stacked-form' },
    element('h3', {}, editing ? `Change ${editing.name}` : 'Add a connection'),
    field(`${prefix}-kind`, 'Kind', kindSelect), field(`${prefix}-name`, 'Name', nameInput),
    ...Object.values(sections), status, element('div', { class: 'wf-actions' }, saveButton, cancelButton));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const kind = kindSelect.value;
    const values = { name: nameInput.value };
    for (const [key, input] of Object.entries(inputs)) if (key.startsWith(`${kind}.`)) values[key.slice(kind.length + 1)] = input.value;
    const body = connectionBody(kind, values, { creating: !editing });
    const saved = await attempt(status, saveButton, () => api(editing ? connectionPath(editing.id) : createPath, { method: 'POST', body }));
    if (saved) onDone(true);
  });
  return form;
}

function rowFor(connection, canManage, status, edit, reload) {
  const cells = [KINDS[connection.kind]?.label || connection.kind, connection.name, statusChip(connection.state),
    credentialText(connection), when(connection.verified_at), identityText(connection) || '—'];
  if (!canManage) return cells;
  const verifyButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `Verify ${connection.name}` }, 'Verify');
  verifyButton.addEventListener('click', async () => {
    if (await attempt(status, verifyButton, () => api(`${connectionPath(connection.id)}/verify`, { method: 'POST', body: {} }))) reload();
  });
  const editButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `Change ${connection.name}` }, 'Change');
  editButton.addEventListener('click', () => edit(connection));
  const deleteButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `Delete ${connection.name}` }, 'Delete');
  deleteButton.addEventListener('click', async () => {
    if (!(await confirmDialog(`Delete ${connection.name}?`, 'Its credentials are gone for good. Workflows that use it fail until another connection fits.', 'Delete'))) return;
    if (await attempt(status, deleteButton, () => api(`${connectionPath(connection.id)}/delete`, { method: 'POST', body: {} }))) reload();
  });
  return [...cells, element('span', {}, verifyButton, ' ', editButton, ' ', deleteButton)];
}

// One card for a team's or a project's connections. Everyone who sees the scope sees them, and only leads change them.
export function connectionsCard({ listPath, canManage, prefix, note, kinds = Object.keys(KINDS) }) {
  const body = element('div', {}, loading());
  const slot = element('div');
  async function reload() {
    let connections;
    try {
      connections = listOf(await api(listPath), 'connections');
    } catch (err) {
      clear(body).append(errorBox(err));
      return;
    }
    const status = element('div', { role: 'status' });
    const edit = (connection) => clear(slot).append(connectionForm(`${prefix}-edit`, listPath, connection, () => reload(), kinds));
    const problems = connections.filter((connection) => connection.last_error)
      .map((connection) => element('p', { class: 'note bad', role: 'alert' }, `${connection.name}: ${connection.last_error}`));
    const headers = ['Kind', 'Name', 'State', 'Credentials', 'Verified', 'Who it is', ...(canManage ? [''] : [])];
    clear(slot);
    if (canManage) slot.append(connectionForm(`${prefix}-new`, listPath, null, () => reload(), kinds));
    clear(body).append(
      connections.length ? table(headers, connections.map((connection) => rowFor(connection, canManage, status, edit, reload))) : empty('No connections yet.'),
      ...problems, status, slot);
  }
  reload();
  return card(element('h2', { class: 'inline' }, 'Connections'), note ? element('p', { class: 'note' }, note) : null, body);
}
