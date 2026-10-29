import { api, listOf } from '../api.js';
import { attempt, card, clear, confirmDialog, element, empty, errorBox, loading, pageHeader, statusChip, table, when } from '../dom.js';
import { parseJson } from '../schema.js';

export const AWS_PATH = '/api/aws';
const LIVE = new Set(['requested', 'booting', 'ready', 'running', 'terminating']);
const AUTH_KINDS = [['access_key', 'Access key'], ['assume_role', 'Assume a role'], ['access_key_assume_role', 'Access key, then assume a role']];
const NUMBERS = [['max_concurrent', 'Most environments at once'], ['max_daily_launches', 'Most launches a day'], ['daily_budget_usd', 'Daily budget (USD, optional)'],
  ['boot_timeout_s', 'Boot timeout (seconds)'], ['max_lifetime_s', 'Longest an environment lives (seconds)']];
const TEXTS = [['region', 'Region', 'us-east-1'], ['vpc_id', 'VPC', 'vpc-…'], ['security_group_id', 'Security group', 'sg-…'],
  ['instance_profile', 'Instance profile', ''], ['mac_instance_profile', 'Mac instance profile', ''], ['image_repository_uri', 'Mac image repository', ''],
  ['session_role_arn', 'Session role ARN', ''], ['artifact_bucket', 'Artifact bucket', ''], ['tag_project', 'Project tag', 'armature-platform'],
  ['install_id', 'Install id', ''], ['stack_name', 'Host role stack (optional, for Verify)', '']];
const TEMPLATES = [['linux', 'Launch template: Linux'], ['mac', 'Launch template: Mac']];
export const TYPE_TEMPLATE = {
  name: 'android', kind: 'ec2-linux', image: 'ami-…', instance_types: ['m6i.large'], market: 'spot-then-on-demand', disk_gb: 100,
  capabilities: ['git', 'platform:android'], job_timeout_s: 3600, hourly_cost_usd: 0.2, installs: [], enabled: true,
};

// "us-east-1a subnet-0abc" per line, so the form needs no table editor.
export function subnetsFromText(text) {
  return String(text || '').split('\n').map((line) => line.trim()).filter(Boolean).map((line) => {
    const [zone, subnet_id] = line.split(/\s+/);
    return { zone, subnet_id };
  });
}

export const subnetsText = (subnets) => (subnets || []).map((subnet) => `${subnet.zone} ${subnet.subnet_id}`).join('\n');

export function tagsFromText(text) {
  return Object.fromEntries(String(text || '').split('\n').map((line) => line.trim()).filter(Boolean).map((line) => {
    const at = line.indexOf('=');
    return at < 0 ? [line, ''] : [line.slice(0, at).trim(), line.slice(at + 1).trim()];
  }));
}

export const tagsText = (tags) => Object.entries(tags || {}).map(([key, value]) => `${key}=${value}`).join('\n');

export const typesFromText = (text) => String(text || '').split(/[\s,]+/).filter(Boolean);

export function credentialText(credentials) {
  if (!credentials?.set) return 'Not set';
  const parts = [];
  if (credentials.access_key_id_last4) parts.push(`access key ending ${credentials.access_key_id_last4}`);
  if (credentials.role_arn) parts.push(`role ${credentials.role_arn}${credentials.external_id_set ? ' with an external id' : ''}`);
  return parts.length ? `Set: ${parts.join(', ')}` : 'Set';
}

// Blank numbers go to the server as null so it keeps its defaults.
export function connectionBody(values) {
  const body = { auth_kind: values.auth_kind, subnets: subnetsFromText(values.subnets), extra_tags: tagsFromText(values.extra_tags),
    allow_host_allocation: Boolean(values.allow_host_allocation), allowed_instance_types: typesFromText(values.allowed_instance_types),
    launch_template_ids: Object.fromEntries(TEMPLATES.map(([kind]) => [kind, values[`template_${kind}`]?.trim()]).filter(([, id]) => id)) };
  for (const [name] of TEXTS) body[name] = values[name]?.trim() || null;
  for (const [name] of NUMBERS) {
    const text = String(values[name] ?? '').trim();
    body[name] = text === '' ? null : Number(text);
  }
  for (const name of ['access_key_id', 'secret_access_key', 'role_arn', 'external_id']) {
    if (values[name]?.trim()) body[name] = values[name].trim();
  }
  return body;
}

export function releaseNote(host, now = Date.now()) {
  const earliest = Date.parse(host.min_release_at);
  return Number.isFinite(earliest) && earliest > now ? `Can be released after ${new Date(earliest).toLocaleString()}` : null;
}

export function environmentCells(env) {
  return [env.type || '—', env.purpose === 'warm' ? 'Warm runner' : env.job_id ? `Job ${env.job_id.slice(0, 8)}` : '—', statusChip(env.state),
    env.provider_id || '—', [env.market, env.instance_type].filter(Boolean).join(' ') || '—', when(env.requested_at), env.error || ''];
}

function field(id, label, input) {
  input.id = id;
  return element('label', { class: 'stacked', for: id }, label, input);
}

export function connectionCard(connection, reload) {
  const inputs = {};
  const auth = element('select', {}, AUTH_KINDS.map(([value, label]) => element('option', { value }, label)));
  auth.value = connection?.auth_kind || 'access_key';
  inputs.auth_kind = auth;
  for (const [name, , placeholder] of TEXTS) {
    inputs[name] = element('input', { type: 'text', spellcheck: 'false', autocapitalize: 'none', placeholder });
    inputs[name].value = connection?.[name] ?? '';
  }
  for (const [name] of NUMBERS) {
    inputs[name] = element('input', { type: 'number', min: 0, step: name === 'daily_budget_usd' ? '0.01' : '1' });
    inputs[name].value = connection?.[name] ?? '';
  }
  inputs.subnets = element('textarea', { class: 'mono', rows: 3, spellcheck: 'false', placeholder: 'us-east-1a subnet-0abc…' });
  inputs.subnets.value = subnetsText(connection?.subnets);
  inputs.extra_tags = element('textarea', { class: 'mono', rows: 3, spellcheck: 'false', placeholder: 'owner=platform' });
  inputs.extra_tags.value = tagsText(connection?.extra_tags);
  for (const [kind] of TEMPLATES) {
    inputs[`template_${kind}`] = element('input', { type: 'text', spellcheck: 'false', autocapitalize: 'none', placeholder: 'lt-…' });
    inputs[`template_${kind}`].value = connection?.launch_template_ids?.[kind] ?? '';
  }
  inputs.allowed_instance_types = element('input', { type: 'text', class: 'mono', spellcheck: 'false', autocapitalize: 'none', placeholder: 'm7i.large, mac2.metal' });
  inputs.allowed_instance_types.value = (connection?.allowed_instance_types || []).join(', ');
  inputs.allow_host_allocation = element('input', { type: 'checkbox', id: 'aws-allow-hosts' });
  inputs.allow_host_allocation.checked = Boolean(connection?.allow_host_allocation);
  // Write-only: the page never receives these back, so they always start blank.
  const secrets = { access_key_id: ['Access key id', 'text'], secret_access_key: ['Secret access key', 'password'], role_arn: ['Role ARN', 'text'], external_id: ['External id', 'password'] };
  for (const [name, [, type]] of Object.entries(secrets)) inputs[name] = element('input', { type, autocomplete: 'off', spellcheck: 'false', autocapitalize: 'none' });

  const status = element('div', { role: 'status' });
  const saveButton = element('button', { type: 'submit', class: 'primary' }, 'Save connection');
  const verifyButton = element('button', { type: 'button' }, 'Verify');
  verifyButton.addEventListener('click', async () => {
    if (await attempt(status, verifyButton, () => api(`${AWS_PATH}/connection/verify`, { method: 'POST', body: {} }))) reload();
  });
  const form = element('form', { class: 'stacked-form' },
    element('h2', { class: 'inline' }, 'Connection'),
    element('p', { class: 'note' }, connection
      ? `State: ${connection.state}. Credentials: ${credentialText(connection.credentials)}.${connection.verified_at ? ` Verified ${when(connection.verified_at)}.` : ''}`
      : 'No connection yet. Launches stay off until a connection verifies.'),
    connection?.last_error ? element('p', { class: 'note bad', role: 'alert' }, connection.last_error) : null,
    field('aws-auth-kind', 'Credentials', auth),
    ...Object.entries(secrets).map(([name, [label]]) => field(`aws-${name}`, `${label}${connection?.credentials?.set ? ' (leave blank to keep)' : ''}`, inputs[name])),
    ...TEXTS.map(([name, label]) => field(`aws-${name}`, label, inputs[name])),
    ...TEMPLATES.map(([kind, label]) => field(`aws-template-${kind}`, label, inputs[`template_${kind}`])),
    field('aws-allowed-types', 'Allowed instance types, from the host role stack’s AllowedInstanceTypes', inputs.allowed_instance_types),
    field('aws-subnets', 'Subnets, one "zone subnet-id" per line', inputs.subnets),
    field('aws-extra-tags', 'Extra tags, one NAME=VALUE per line', inputs.extra_tags),
    element('label', { for: inputs.allow_host_allocation.id }, inputs.allow_host_allocation, ' Admins may allocate Mac hosts, which bill for at least 24 hours'),
    ...NUMBERS.map(([name, label]) => field(`aws-${name}`, label, inputs[name])),
    status, element('div', { class: 'wf-actions' }, saveButton, connection ? verifyButton : null));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const values = Object.fromEntries(Object.entries(inputs).map(([name, input]) => [name, input.type === 'checkbox' ? input.checked : input.value]));
    const saved = await attempt(status, saveButton, () => api(`${AWS_PATH}/connection`, { method: 'POST', body: connectionBody(values) }));
    if (saved) {
      for (const name of Object.keys(secrets)) inputs[name].value = '';
      reload();
    }
  });
  return card(form);
}

function warmCard(page, reload) {
  const status = element('div', { role: 'status' });
  if (page.warm) {
    const stopButton = element('button', { type: 'button' }, 'Stop warm runner');
    stopButton.addEventListener('click', async () => {
      if (!(await confirmDialog('Stop the warm runner?', 'Jobs that need only llm wait until a runner is back.', 'Stop'))) return;
      if (await attempt(status, stopButton, () => api(`${AWS_PATH}/warm/stop`, { method: 'POST', body: {} }))) reload();
    });
    return card(element('h2', { class: 'inline' }, 'Warm runner'),
      element('p', {}, 'One on-demand instance takes jobs that need only llm. ', statusChip(page.warm.state), ` since ${when(page.warm.requested_at)}`),
      element('div', { class: 'wf-actions' }, stopButton), status);
  }
  const linux = page.types.filter((type) => type.kind === 'ec2-linux' && type.enabled);
  if (!linux.length) return card(element('h2', { class: 'inline' }, 'Warm runner'), empty('Add an ec2-linux environment type to start a warm runner.'));
  const typeSelect = element('select', { id: 'aws-warm-type' }, linux.map((type) => element('option', { value: type.id }, type.name)));
  const startButton = element('button', { type: 'button', class: 'primary' }, 'Start warm runner');
  startButton.addEventListener('click', async () => {
    if (await attempt(status, startButton, () => api(`${AWS_PATH}/warm/start`, { method: 'POST', body: { type_id: typeSelect.value } }))) reload();
  });
  return card(element('h2', { class: 'inline' }, 'Warm runner'),
    element('p', { class: 'note' }, 'Jobs that need only llm never get their own environment. A warm runner takes them. It runs until you stop it.'),
    element('div', { class: 'wf-actions' }, element('label', { for: typeSelect.id }, 'Image from'), typeSelect, startButton), status);
}

function typesCard(types, reload) {
  const editor = element('textarea', { id: 'aws-type-json', class: 'mono', rows: 14, spellcheck: 'false' });
  editor.value = JSON.stringify(TYPE_TEMPLATE, null, 2);
  let editing = null;
  const heading = element('h2', { class: 'inline' }, 'Add an environment type');
  const status = element('div', { role: 'status' });
  const saveButton = element('button', { type: 'submit', class: 'primary' }, 'Save type');
  const edit = (type) => {
    editing = type.id;
    heading.textContent = `Edit ${type.name}`;
    const { id, updated_at, ...rest } = type;
    editor.value = JSON.stringify(rest, null, 2);
    editor.focus();
  };
  const form = element('form', { class: 'stacked-form' }, heading,
    element('label', { class: 'stacked', for: editor.id }, 'The type, as JSON', editor), status, element('div', { class: 'wf-actions' }, saveButton));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const parsed = parseJson(editor.value);
    if (parsed.error) {
      clear(status).append(errorBox(new Error(`Not valid JSON: ${parsed.error}`)));
      return;
    }
    const path = editing ? `${AWS_PATH}/types/${encodeURIComponent(editing)}` : `${AWS_PATH}/types`;
    if (await attempt(status, saveButton, () => api(path, { method: 'POST', body: parsed.value }))) reload();
  });
  const rows = types.map((type) => {
    const editButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `Edit ${type.name}` }, 'Edit');
    editButton.addEventListener('click', () => edit(type));
    return [type.name, type.kind, element('span', { class: 'mono' }, type.image), (type.capabilities || []).join(', ') || '—', type.enabled ? 'Yes' : 'No', editButton];
  });
  return card(element('h2', { class: 'inline' }, 'Environment types'),
    types.length ? table(['Name', 'Kind', 'Image', 'Provides', 'Enabled', ''], rows) : empty('No environment types yet.'), form);
}

function hostsCard(hosts, connection, reload) {
  const status = element('div', { role: 'status' });
  const rows = hosts.map((host) => {
    const releaseButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `Release ${host.name}` }, 'Release');
    const note = releaseNote(host);
    if (note) releaseButton.disabled = true;
    releaseButton.addEventListener('click', async () => {
      if (!(await confirmDialog(`Release ${host.name}?`, 'The host and its macOS instance go away. Mac jobs wait for another host.', 'Release'))) return;
      if (await attempt(status, releaseButton, () => api(`${AWS_PATH}/hosts/${encodeURIComponent(host.id)}/release`, { method: 'POST', body: {} }))) reload();
    });
    const instanceInput = element('input', { type: 'text', class: 'mono', placeholder: 'i-…', 'aria-label': `macOS instance on ${host.name}`, spellcheck: 'false' });
    instanceInput.value = host.instance_id || '';
    const setButton = element('button', { type: 'button', class: 'link-button' }, 'Set instance');
    setButton.addEventListener('click', async () => {
      if (await attempt(status, setButton, () => api(`${AWS_PATH}/hosts/${encodeURIComponent(host.id)}/instance`, { method: 'POST', body: { instance_id: instanceInput.value.trim() } }))) reload();
    });
    return [host.name, statusChip(host.state), host.zone || '—', String(host.slots), element('span', {}, instanceInput, setButton), element('span', {}, releaseButton, note ? element('span', { class: 'muted' }, ` ${note}`) : null)];
  });
  const nameInput = element('input', { type: 'text', required: true, maxlength: 63, spellcheck: 'false', autocapitalize: 'none', placeholder: 'mac-1' });
  const zoneInput = element('input', { type: 'text', required: true, spellcheck: 'false', autocapitalize: 'none', placeholder: 'us-east-1a' });
  const typeInput = element('input', { type: 'text', spellcheck: 'false', autocapitalize: 'none', value: 'mac2.metal' });
  const confirmBox = element('input', { type: 'checkbox', id: 'aws-host-confirm', required: true });
  const allocateButton = element('button', { type: 'submit' }, 'Allocate host');
  const form = element('form', { class: 'stacked-form' }, element('h2', { class: 'inline' }, 'Allocate a Mac host'),
    connection?.allow_host_allocation ? null : element('p', { class: 'note' }, 'This connection doesn’t allow host allocation. Ask the account owner to allocate a host.'),
    field('aws-host-name', 'Name', nameInput), field('aws-host-zone', 'Zone', zoneInput), field('aws-host-type', 'Instance type', typeInput),
    element('label', { for: confirmBox.id }, confirmBox, ' I understand AWS bills a Mac host for at least 24 hours from allocation, even if it’s released sooner'),
    element('div', { class: 'wf-actions' }, allocateButton));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const body = { name: nameInput.value.trim(), zone: zoneInput.value.trim(), instance_type: typeInput.value.trim(), confirm_cost: confirmBox.checked };
    if (await attempt(status, allocateButton, () => api(`${AWS_PATH}/hosts`, { method: 'POST', body }))) reload();
  });
  return card(element('h2', { class: 'inline' }, 'Mac hosts'),
    hosts.length ? table(['Name', 'State', 'Zone', 'Slots', 'macOS instance', ''], rows) : empty('No Mac hosts.'), status, form);
}

function environmentsCard(envs, reload) {
  const status = element('div', { role: 'status' });
  const rows = envs.map((env) => {
    if (!LIVE.has(env.state)) return [...environmentCells(env), ''];
    const button = element('button', { type: 'button', class: 'link-button', 'aria-label': `Terminate ${env.id}` }, 'Terminate');
    button.addEventListener('click', async () => {
      if (!(await confirmDialog('Terminate this environment?', 'Its instance stops now, and a job running on it fails.', 'Terminate'))) return;
      if (await attempt(status, button, () => api(`${AWS_PATH}/environments/${encodeURIComponent(env.id)}/terminate`, { method: 'POST', body: {} }))) reload();
    });
    return [...environmentCells(env), button];
  });
  return card(element('h2', { class: 'inline' }, 'Environments'), status,
    envs.length ? table(['Type', 'For', 'State', 'Instance', 'Market', 'Requested', 'Error', ''], rows) : empty('No environments yet.'));
}

export async function render(root) {
  root.append(pageHeader('AWS'));
  root.append(element('p', { class: 'note' }, 'Jobs that need the source, and that no runner can take, each get an environment of their project’s type. It enrolls once, runs that one job, and is terminated when the job ends.'));
  const body = element('div', {}, loading());
  root.append(body);

  async function reload() {
    let page;
    try {
      page = await api(AWS_PATH);
    } catch (err) {
      clear(body).append(errorBox(err));
      return;
    }
    const banner = page.status?.message || (page.status?.reasons || []).join(' ');
    clear(body).append(
      banner ? element('div', { class: 'banner warn', role: 'alert' }, banner) : null,
      connectionCard(page.connection, reload), warmCard({ ...page, types: listOf(page, 'types') }, reload), typesCard(listOf(page, 'types'), reload),
      hostsCard(listOf(page, 'hosts'), page.connection, reload), environmentsCard(listOf(page, 'environments'), reload));
  }

  await reload();
}
