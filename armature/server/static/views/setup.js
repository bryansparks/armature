import { api, basePath, listOf } from '../api.js';
import { connectionValues, parseOutputs } from '../awsoutputs.js';
import { attempt, card, clear, element, empty, errorBox, link, loading, pageHeader } from '../dom.js';
import { hashFor } from '../router.js';
import { AWS_PATH, connectionBody, connectionCard } from './aws.js';
import { connectionsCard, teamConnectionsPath } from './connections.js';
import { githubConnections, repoForm } from './projects.js';
import { kindLabel } from './providers.js';
import { keysPath } from './runnerkeys.js';
import { RUNNERS_PATH } from './runners.js';
import { ENVIRONMENT_TYPES_PATH } from './teams.js';
import { SOURCES, webhookPath, webhooksPath } from './webhooks.js';

export const STEPS = [
  { id: 'database', label: 'Database' },
  { id: 'admin', label: 'First admin' },
  { id: 'models', label: 'Models' },
  { id: 'aws', label: 'AWS' },
  { id: 'runners', label: 'Runners' },
  { id: 'connections', label: 'Connections' },
  { id: 'repos', label: 'Repos' },
  { id: 'webhooks', label: 'Webhooks' },
];
export const SKIPPED_KEY = 'setup.skipped';
export const DISMISSED_KEY = 'setup.dismissed';
const INSTANCE_SCOPE = '*';

export const MODEL_CHOICES = {
  anthropic: { label: 'Anthropic API key', kind: 'anthropic', secret: 'ANTHROPIC_API_KEY', keyLabel: 'API key', model: 'claude-sonnet-4-6', help: 'From the Anthropic Console, under API keys.' },
  'claude-subscription': { label: 'Claude subscription', kind: 'claude-subscription', secret: 'CLAUDE_CODE_OAUTH_TOKEN', keyLabel: 'Token', model: 'claude-sonnet-4-6', help: 'Run `claude setup-token` and paste the token it prints.' },
  bedrock: { label: 'AWS Bedrock', kind: 'bedrock', model: 'us.anthropic.claude-sonnet-4-6', region: true, help: 'Runs use the AWS credentials of the machine they run on.' },
  other: { label: 'Other (OpenAI-compatible or Ollama)', kind: 'openai-compatible', secret: 'MODEL_API_KEY', keyLabel: 'API key (if the server needs one)', model: '', apiBase: true, help: 'Your own model server, such as Ollama or vLLM.' },
};

export const TYPE_PRESETS = [
  { name: 'ios', label: 'iOS', kind: 'ec2-mac', capabilities: ['git', 'platform:ios'], imageHelp: 'A Tart image name with Xcode, such as one from your Mac image repository.' },
  { name: 'android', label: 'Android', kind: 'ec2-linux', capabilities: ['git', 'platform:android'], instance_types: ['m6i.large'], imageHelp: 'An AMI id. Any Amazon Linux AMI works to start.' },
  { name: 'dotnet', label: '.NET', kind: 'ec2-linux', capabilities: ['git', 'platform:dotnet'], instance_types: ['m6i.large'], imageHelp: 'An AMI id. Any Amazon Linux AMI works to start.' },
  { name: 'node', label: 'Node', kind: 'ec2-linux', capabilities: ['git', 'platform:node'], instance_types: ['m6i.large'], imageHelp: 'An AMI id. Any Amazon Linux AMI works to start.' },
];

const WEBHOOK_SOURCE = { github_app: 'github', slack: 'slack', jira: 'jira', linear: 'linear' };

// Progress comes from what exists, so a half-finished setup picks up where it left off.
export function setupProgress(snapshot) {
  const done = new Set(['database', 'admin']);
  if (snapshot.providers?.length) done.add('models');
  if (snapshot.aws?.connection?.state === 'ok') done.add('aws');
  if (snapshot.runners?.length) done.add('runners');
  if (snapshot.connections?.length) done.add('connections');
  if (snapshot.projects?.some((project) => project.repo)) done.add('repos');
  if (snapshot.webhooks?.length) done.add('webhooks');
  const skipped = new Set(snapshot.skipped || []);
  const open = STEPS.filter((step) => !done.has(step.id) && !skipped.has(step.id));
  return { done, count: done.size, total: STEPS.length, next: open[0]?.id || null, complete: !open.length };
}

export const showBanner = (snapshot) => !snapshot.dismissed && !setupProgress(snapshot).complete;

export function modelRequests(teamId, choiceId, values) {
  const choice = MODEL_CHOICES[choiceId];
  if (!choice) return { error: 'Pick where your models come from.' };
  const model = String(values.model || '').trim();
  if (!model) return { error: 'Name the model to use.' };
  const key = choice.secret ? String(values.key || '').trim() : '';
  if (!key && !choice.apiBase && choice.secret) return { error: `Paste the ${choice.keyLabel.toLowerCase()}.` };
  const config = {};
  if (choice.region) config.region = String(values.region || '').trim() || 'us-east-1';
  if (choice.apiBase) {
    config.api_base = String(values.api_base || '').trim();
    if (!config.api_base) return { error: 'Enter the server’s address.' };
  }
  const kind = choice.apiBase && values.flavor === 'ollama' ? 'ollama' : choice.kind;
  const calls = key ? [[`/api/teams/${encodeURIComponent(teamId)}/secrets`, { name: choice.secret, value: key }]] : [];
  calls.push([`/api/teams/${encodeURIComponent(teamId)}/providers`, { kind, config, aliases: { default: model }, credential_secret: key ? choice.secret : null }]);
  return { calls };
}

export function awsValues(pasted, typed) {
  const values = { ...connectionValues(parseOutputs(pasted)) };
  for (const [name, value] of Object.entries(typed)) if (String(value ?? '').trim()) values[name] = value;
  const hasKey = Boolean(String(typed.access_key_id || '').trim());
  values.auth_kind = hasKey && values.role_arn ? 'access_key_assume_role' : hasKey ? 'access_key' : 'assume_role';
  return values;
}

export function presetTypeBody(preset, image) {
  return { name: preset.name, kind: preset.kind, image: String(image || '').trim(), capabilities: preset.capabilities,
    instance_types: preset.instance_types || [], market: 'spot-then-on-demand', enabled: true };
}

export function webhookBody(connection) {
  const source = WEBHOOK_SOURCE[connection.kind];
  return { name: `${SOURCES[source]} ${connection.name}`.slice(0, 100), source, connection_id: connection.id };
}

// GitHub and the rest can't reach a loopback address.
export const reachable = (url) => !/^http:\/\/(localhost|127\.0\.0\.1|\[::1\])([:/]|$)/.test(String(url || ''));

// The page's own origin is public_origin, because the server refuses changes from any other.
export const serverAddress = (doc = globalThis.document, here = globalThis.location) => `${here?.origin || ''}${basePath(doc)}`;

export function runnerCommand(address, key, capabilities = ['llm', 'git']) {
  return [`export ARMATURE_RUNNER_KEY=${key}`,
    `armature runner --server ${address} ${capabilities.map((capability) => `--capability ${capability}`).join(' ')} --slots 1`].join('\n');
}

async function settingsValues() {
  const rows = listOf(await api('/api/settings').catch(() => ({ settings: [] })), 'settings').filter((row) => row.scope === INSTANCE_SCOPE);
  const value = (key) => rows.find((row) => row.key === key)?.value;
  return { skipped: Array.isArray(value(SKIPPED_KEY)) ? value(SKIPPED_KEY) : [], dismissed: value(DISMISSED_KEY) === true };
}

export async function loadSnapshot(teamId) {
  const [teams, settings, aws, runners] = await Promise.all([api('/api/teams').then((payload) => listOf(payload, 'teams')), settingsValues(), api(AWS_PATH).catch(() => null),
    api(RUNNERS_PATH).then((payload) => listOf(payload, 'runners')).catch(() => [])]);
  const team = teams.find((candidate) => candidate.id === teamId) || teams[0] || null;
  if (!team) return { teams, team, aws, runners, ...settings, providers: [], connections: [], projects: [], webhooks: [] };
  const [providers, connections, projects, webhooks] = await Promise.all([
    api(`/api/teams/${encodeURIComponent(team.id)}/providers`).then((payload) => listOf(payload, 'providers')).catch(() => []),
    api(teamConnectionsPath(team.id)).then((payload) => listOf(payload, 'connections')).catch(() => []),
    api(`/api/teams/${encodeURIComponent(team.id)}/projects`).then((payload) => listOf(payload, 'projects')).catch(() => []),
    api(webhooksPath(team.id)).then((payload) => listOf(payload, 'webhooks')).catch(() => []),
  ]);
  return { teams, team, aws, runners, ...settings, providers, connections, projects, webhooks };
}

const putSetting = (key, value) => api('/api/settings', { method: 'POST', body: { key, value } });

function field(id, label, input, help) {
  input.id = id;
  return element('label', { class: 'stacked', for: id }, label, input, help ? element('span', { class: 'note' }, help) : null);
}

function copyLine(text, announce) {
  const button = element('button', { type: 'button', class: 'link-button' }, 'Copy');
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(text);
      announce.textContent = 'Copied.';
    } catch {
      announce.textContent = 'Could not copy. Select the text and copy it.';
    }
  });
  return element('div', { class: 'copy-line' }, element('code', {}, text), button);
}

function teamForm(redraw) {
  const nameInput = element('input', { type: 'text', required: true, maxlength: 60, value: 'My team' });
  const button = element('button', { type: 'submit', class: 'primary' }, 'Create team');
  const status = element('div', { role: 'status' });
  const form = element('form', { class: 'stacked-form' }, field('setup-team-name', 'Team name', nameInput, 'People, workflows, and models belong to a team. You can rename it later.'),
    status, element('div', { class: 'wf-actions' }, button));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const created = await attempt(status, button, () => api('/api/teams', { method: 'POST', body: { name: nameInput.value.trim() } }));
    if (created) redraw(created.team.id);
  });
  return form;
}

function modelsStep(snapshot, redraw) {
  if (!snapshot.team) return [element('p', {}, 'First, a team to hold your models and repos.'), teamForm(redraw)];
  if (snapshot.providers.length) {
    return [element('p', {}, `${snapshot.team.name} has models set up.`),
      element('ul', {}, snapshot.providers.map((provider) => element('li', {}, `${kindLabel(provider.kind)}: ${Object.entries(provider.aliases || {}).map(([alias, model]) => `${alias} is ${model}`).join(', ') || 'no aliases'}`))),
      element('p', { class: 'note' }, link(hashFor('team', [snapshot.team.id, 'providers']), 'Change models on the team page'))];
  }
  const inputs = { key: element('input', { type: 'password', autocomplete: 'off', spellcheck: 'false' }), model: element('input', { type: 'text', spellcheck: 'false', autocapitalize: 'none' }),
    region: element('input', { type: 'text', spellcheck: 'false', autocapitalize: 'none', placeholder: 'us-east-1' }),
    flavor: element('select', {}, element('option', { value: 'openai-compatible' }, 'OpenAI-compatible'), element('option', { value: 'ollama' }, 'Ollama')), api_base: element('input', { type: 'text', spellcheck: 'false', autocapitalize: 'none', placeholder: 'Such as the address of your Ollama server' }) };
  const fields = { key: field('setup-model-key', 'Key', inputs.key), model: field('setup-model', 'Model', inputs.model, 'Used by every stage that doesn’t name a model.'),
    region: field('setup-model-region', 'Region', inputs.region), flavor: field('setup-model-flavor', 'Server kind', inputs.flavor), api_base: field('setup-model-base', 'Server address', inputs.api_base) };
  const help = element('p', { class: 'note' });
  let choice = 'anthropic';
  const sync = () => {
    const picked = MODEL_CHOICES[choice];
    fields.key.hidden = !picked.secret;
    fields.key.firstChild.textContent = picked.keyLabel || 'Key';
    fields.region.hidden = !picked.region;
    fields.api_base.hidden = !picked.apiBase;
    fields.flavor.hidden = !picked.apiBase;
    inputs.model.value = picked.model;
    help.textContent = picked.help;
  };
  const choices = element('div', { class: 'choices', role: 'radiogroup', 'aria-label': 'Where your models come from' },
    Object.entries(MODEL_CHOICES).map(([id, entry]) => {
      const radio = element('input', { type: 'radio', name: 'setup-model-choice', value: id, checked: id === choice });
      radio.addEventListener('change', () => { choice = id; sync(); });
      return element('label', {}, radio, entry.label);
    }));
  const button = element('button', { type: 'submit', class: 'primary' }, 'Save models');
  const status = element('div', { role: 'status' });
  const form = element('form', { class: 'stacked-form' }, choices, help, fields.flavor, fields.api_base, fields.key, fields.region, fields.model,
    element('p', { class: 'note' }, 'Armature checks the key on the first run, not now.'), status, element('div', { class: 'wf-actions' }, button));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const { calls, error } = modelRequests(snapshot.team.id, choice, Object.fromEntries(Object.entries(inputs).map(([name, input]) => [name, input.value])));
    if (error) {
      clear(status).append(errorBox(new Error(error)));
      return;
    }
    const saved = await attempt(status, button, async () => {
      for (const [path, body] of calls) await api(path, { method: 'POST', body });
    });
    inputs.key.value = '';
    if (saved) redraw();
  });
  sync();
  return [element('p', {}, `Where should ${snapshot.team.name} get its models?`), form];
}

function awsStep(snapshot, redraw, announce) {
  const connection = snapshot.aws?.connection || null;
  const parts = [];
  if (!snapshot.aws) return [element('p', {}, 'AWS settings need an admin.')];
  if (connection) {
    parts.push(element('p', {}, connection.state === 'ok' ? 'The connection is verified.'
      : connection.state === 'error' ? 'The connection did not verify.' : 'The connection is saved but not verified yet.'),
      connection.last_error ? element('p', { class: 'note bad', role: 'alert' }, connection.last_error) : null);
  }
  parts.push(element('p', {}, 'Deploy the stacks in deploy/aws/README.md, then paste each stack’s outputs here.'),
    copyLine('aws cloudformation describe-stacks --stack-name <stack> --query "Stacks[0].Outputs"', announce));
  const pasted = element('textarea', { class: 'mono', rows: 6, spellcheck: 'false' });
  const found = element('p', { class: 'note', 'aria-live': 'polite' });
  pasted.addEventListener('input', () => {
    const count = Object.keys(connectionValues(parseOutputs(pasted.value))).length;
    found.textContent = count ? `Found ${count} of the fields.` : '';
  });
  const typed = {
    region: element('input', { type: 'text', spellcheck: 'false', autocapitalize: 'none', value: connection?.region || 'us-east-1' }),
    vpc_id: element('input', { type: 'text', spellcheck: 'false', autocapitalize: 'none', placeholder: 'vpc-…', value: connection?.vpc_id || '' }),
    subnets: element('textarea', { class: 'mono', rows: 2, spellcheck: 'false', placeholder: 'us-east-1a subnet-0abc…' }),
    external_id: element('input', { type: 'password', autocomplete: 'off', spellcheck: 'false' }),
    access_key_id: element('input', { type: 'text', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'none' }),
    secret_access_key: element('input', { type: 'password', autocomplete: 'off', spellcheck: 'false' }),
  };
  typed.subnets.value = (connection?.subnets || []).map((subnet) => `${subnet.zone} ${subnet.subnet_id}`).join('\n');
  const saveButton = element('button', { type: 'submit', class: 'primary' }, 'Save and verify');
  const status = element('div', { role: 'status' });
  const form = element('form', { class: 'stacked-form' },
    field('setup-aws-outputs', 'Stack outputs', pasted, 'JSON or one Name=Value per line.'), found,
    field('setup-aws-region', 'Region', typed.region),
    field('setup-aws-vpc', 'VPC', typed.vpc_id, 'The VpcId you deployed the stacks with.'),
    field('setup-aws-subnets', 'Subnets', typed.subnets, 'One "zone subnet-id" per line.'),
    field('setup-aws-external-id', 'External id', typed.external_id, 'The ExternalId you deployed the host stack with.'),
    element('details', {}, element('summary', {}, 'I use an access key'),
      field('setup-aws-key-id', 'Access key id', typed.access_key_id), field('setup-aws-secret', 'Secret access key', typed.secret_access_key)),
    status, element('div', { class: 'wf-actions' }, saveButton));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const values = awsValues(pasted.value, Object.fromEntries(Object.entries(typed).map(([name, input]) => [name, input.value])));
    const saved = await attempt(status, saveButton, async () => {
      await api(`${AWS_PATH}/connection`, { method: 'POST', body: connectionBody(values) });
      await api(`${AWS_PATH}/connection/verify`, { method: 'POST', body: {} });
    });
    typed.external_id.value = '';
    typed.secret_access_key.value = '';
    if (saved) redraw();
  });
  parts.push(form, element('details', {}, element('summary', {}, 'Advanced: every connection field'), connectionCard(connection, redraw)));
  if (connection) parts.push(typesForm(snapshot, redraw, announce));
  parts.push(element('p', { class: 'note' }, 'No AWS? Skip this step and run jobs on your own computer in the next step.'));
  return parts;
}

function typesForm(snapshot, redraw, announce) {
  const existing = new Set(listOf(snapshot.aws, 'types').map((type) => type.name));
  const rows = TYPE_PRESETS.map((preset) => {
    const box = element('input', { type: 'checkbox', id: `setup-type-${preset.name}`, disabled: existing.has(preset.name), checked: existing.has(preset.name) });
    const image = element('input', { type: 'text', spellcheck: 'false', autocapitalize: 'none', 'aria-label': `${preset.label} image` });
    const imageField = field(`setup-type-image-${preset.name}`, 'Image', image, preset.imageHelp);
    imageField.hidden = true;
    box.addEventListener('change', () => { imageField.hidden = !box.checked; });
    return { preset, box, image, node: element('div', {}, element('label', { for: box.id }, box, ` ${preset.label}${existing.has(preset.name) ? ' (added)' : ''}`), imageField) };
  });
  const button = element('button', { type: 'submit' }, 'Add types');
  const status = element('div', { role: 'status' });
  const form = element('form', { class: 'stacked-form' }, element('h3', {}, 'What do your projects build with?'),
    ...rows.map((row) => row.node), element('p', { class: 'note' }, 'Bake a ready image later with:'), copyLine('armature env build <type>', announce),
    status, element('div', { class: 'wf-actions' }, button));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const picked = rows.filter((row) => row.box.checked && !existing.has(row.preset.name));
    const saved = await attempt(status, button, async () => {
      for (const row of picked) await api(`${AWS_PATH}/types`, { method: 'POST', body: presetTypeBody(row.preset, row.image.value) });
    });
    if (saved) redraw();
  });
  return form;
}

function addressNote() {
  const address = serverAddress();
  return element('p', { class: 'note' }, `Runners and webhooks reach this server at ${address}. To change it, set public_origin in server.yaml and restart the server.`,
    reachable(address) ? null : ' This address works only on this computer.');
}

function runnerWatch(keyName, signal) {
  const box = element('div', { 'aria-live': 'polite' }, element('p', { class: 'note' }, 'Waiting for the runner to say hello…'));
  const poll = async () => {
    if (signal.aborted) return;
    const runners = listOf(await api(RUNNERS_PATH).catch(() => ({ runners: [] })), 'runners');
    const hello = runners.find((runner) => runner.key === keyName && runner.online);
    if (hello) {
      clear(box).append(element('p', {}, `${hello.name} said hello. It can do ${(hello.capabilities || []).join(', ') || 'jobs that need nothing'}.`));
      return;
    }
    setTimeout(poll, 3000);
  };
  poll();
  return box;
}

function runnersStep(snapshot, announce, signal) {
  if (!snapshot.team) return [empty('Create a team in the Models step first.')];
  const parts = [element('p', {}, 'A runner is a computer that runs jobs. Armature never starts one itself. Run this command where jobs should run: on this computer, another one, or an AWS instance.'),
    addressNote()];
  if (snapshot.runners.length) {
    parts.push(element('ul', {}, snapshot.runners.map((runner) => element('li', {}, `${runner.name}: ${runner.online ? 'online' : 'offline'}`))));
  }
  const slot = element('div');
  const status = element('div', { role: 'status' });
  const button = element('button', { type: 'button', class: 'primary' }, 'Make a key and show the command');
  button.addEventListener('click', async () => {
    const keyName = `runner ${new Date().toISOString().slice(0, 16).replace('T', ' ')}`;
    const created = await attempt(status, button, () => api(keysPath(snapshot.team.id), { method: 'POST', body: { name: keyName } }));
    if (!created?.key) return;
    button.hidden = true;
    clear(slot).append(element('p', { class: 'note', role: 'alert' }, 'Copy it now. The key is not shown again.'),
      copyLine(runnerCommand(serverAddress(), created.key), announce),
      element('p', { class: 'note' }, 'Add --capability claude-code, or platform:ios, for each thing the computer can do.'),
      runnerWatch(keyName, signal));
  });
  parts.push(button, status, slot);
  if (snapshot.aws?.connection?.state === 'ok') {
    parts.push(element('p', { class: 'note' }, 'On AWS, start the warm runner from ', link(hashFor('aws'), 'the AWS page'), '. It enrolls with its own one-time token.'));
  }
  return parts;
}

function connectionsStep(snapshot) {
  if (!snapshot.team) return [empty('Create a team in the Models step first.')];
  return [element('p', {}, 'Connect GitHub so Armature can read repos and comment on pull requests. Slack, Jira, and Linear are optional.'),
    connectionsCard({ listPath: teamConnectionsPath(snapshot.team.id), canManage: true, prefix: 'setup-connections' })];
}

async function reposStep(snapshot, redraw) {
  if (!snapshot.team) return [empty('Create a team in the Models step first.')];
  const types = listOf(await api(ENVIRONMENT_TYPES_PATH).catch(() => ({ types: [] })), 'types');
  const withRepo = snapshot.projects.filter((project) => project.repo);
  return [element('p', {}, 'Each repo becomes a project. Add as many as you like, now or later on the Projects page.'),
    withRepo.length ? element('ul', {}, withRepo.map((project) => element('li', {}, link(hashFor('project', [project.id]), project.name), ` ${project.repo}`))) : null,
    githubConnections(snapshot.connections).length ? null : element('p', { class: 'note' }, link(hashFor('setup', ['connections']), 'Connect GitHub first')),
    repoForm(snapshot.team, { types, connections: snapshot.connections, prefix: 'setup-repo', onSaved: () => redraw() })];
}

function deliveriesWatch(hook, signal) {
  const box = element('div', { 'aria-live': 'polite' }, element('p', { class: 'note' }, 'Waiting for the first delivery…'));
  const status = element('div', { role: 'status' });
  const poll = async () => {
    if (signal.aborted) return;
    const deliveries = listOf(await api(`${webhookPath(hook.id)}/deliveries?limit=5`).catch(() => ({ deliveries: [] })), 'deliveries');
    const held = deliveries.find((delivery) => delivery.status === 'held');
    if (held) {
      const approve = element('button', { type: 'button', class: 'primary' }, 'Approve this sender');
      approve.addEventListener('click', async () => {
        if (await attempt(status, approve, () => api(`${webhookPath(hook.id)}/deliveries/${encodeURIComponent(held.id)}/approve`, { method: 'POST', body: {} }))) {
          clear(box).append(element('p', {}, 'Approved. Deliveries from this sender go through now.'));
        }
      });
      clear(box).append(element('p', {}, 'A delivery arrived and is held until you approve its sender.'), approve, status);
      return;
    }
    if (deliveries.length) {
      clear(box).append(element('p', {}, `Last delivery: ${deliveries[0].status}.`));
      return;
    }
    setTimeout(poll, 5000);
  };
  poll();
  return box;
}

function webhooksStep(snapshot, redraw, announce, signal) {
  if (!snapshot.team) return [empty('Create a team in the Models step first.')];
  const linked = snapshot.connections.filter((connection) => WEBHOOK_SOURCE[connection.kind]);
  if (!linked.length) return [element('p', {}, 'Webhooks start workflows when something happens in GitHub, Slack, Jira, or Linear. Add a connection first.'), addressNote()];
  return [addressNote(), ...linked.map((connection) => {
    const hook = snapshot.webhooks.find((candidate) => candidate.connection_id === connection.id);
    const source = SOURCES[WEBHOOK_SOURCE[connection.kind]];
    if (!hook) {
      const button = element('button', { type: 'button', class: 'primary' }, `Create the ${source} webhook`);
      const status = element('div', { role: 'status' });
      button.addEventListener('click', async () => {
        if (await attempt(status, button, () => api(webhooksPath(snapshot.team.id), { method: 'POST', body: webhookBody(connection) }))) redraw();
      });
      return card(element('h3', {}, `${source}: ${connection.name}`), button, status);
    }
    return card(element('h3', {}, `${source}: ${connection.name}`),
      element('p', {}, `Paste this address in ${source}’s webhook settings, with the connection’s webhook secret.`), copyLine(hook.url, announce),
      reachable(hook.url) ? null : element('p', { class: 'note bad' }, `${source} can’t reach this address. Set public_origin in server.yaml to an address ${source} can reach, then restart the server.`),
      element('p', { class: 'note' }, link(hashFor('webhook', [hook.id]), 'Pick which workflows it starts')),
      deliveriesWatch(hook, signal));
  })];
}

async function stepBody(stepId, snapshot, redraw, announce, signal) {
  if (stepId === 'models') return modelsStep(snapshot, redraw);
  if (stepId === 'aws') return awsStep(snapshot, redraw, announce);
  if (stepId === 'runners') return runnersStep(snapshot, announce, signal);
  if (stepId === 'connections') return connectionsStep(snapshot);
  if (stepId === 'repos') return reposStep(snapshot, redraw);
  if (stepId === 'webhooks') return webhooksStep(snapshot, redraw, announce, signal);
  return [element('p', {}, 'Done when you first signed in.')];
}

export async function render(root, { args, query, signal }) {
  root.append(pageHeader('Setup'));
  const body = element('div', {}, loading());
  const announce = element('div', { class: 'visually-hidden', 'aria-live': 'polite' });
  root.append(element('p', { class: 'note' }, 'Every step can be skipped and finished later. Come back here from Setup in the menu.'), body, announce);
  let teamId = query.get('team') || null;
  let focusHeading = false;

  async function draw() {
    let snapshot;
    try {
      snapshot = await loadSnapshot(teamId);
    } catch (err) {
      clear(body).append(errorBox(err));
      return;
    }
    teamId = snapshot.team?.id || null;
    const progress = setupProgress(snapshot);
    const active = STEPS.some((step) => step.id === args[0]) ? args[0] : progress.next || 'models';
    const steps = element('ol', { class: 'wizard-steps', 'aria-label': 'Setup steps' }, STEPS.map((step) => element('li', {},
      element('a', { href: hashFor('setup', [step.id]), class: progress.done.has(step.id) ? 'done' : null, 'aria-current': step.id === active ? 'step' : null }, step.label))));
    const heading = element('h2', { tabindex: -1 }, STEPS.find((step) => step.id === active).label);
    const status = element('div', { role: 'status' });
    const index = STEPS.findIndex((step) => step.id === active);
    const next = STEPS[index + 1];
    const skip = element('button', { type: 'button' }, 'Skip for now');
    skip.addEventListener('click', async () => {
      const skipped = [...new Set([...snapshot.skipped, active])];
      if (await attempt(status, skip, () => putSetting(SKIPPED_KEY, skipped))) location.hash = hashFor('setup', next ? [next.id] : []);
    });
    const hide = element('button', { type: 'button', class: 'link-button' }, 'Hide the setup checklist');
    hide.addEventListener('click', async () => {
      if (await attempt(status, hide, () => putSetting(DISMISSED_KEY, true))) location.hash = '#/overview';
    });
    const teamPicker = snapshot.teams.length > 1 ? element('select', { id: 'setup-team', 'aria-label': 'Team' }, snapshot.teams.map((team) => element('option', { value: team.id }, team.name))) : null;
    if (teamPicker) {
      teamPicker.value = teamId;
      teamPicker.addEventListener('change', () => { teamId = teamPicker.value; draw(); });
    }
    clear(body).append(
      element('p', {}, `${progress.count} of ${progress.total} done.`, teamPicker ? [' Team: ', teamPicker] : null), steps,
      card(heading, ...(await stepBody(active, snapshot, (createdTeam) => { if (typeof createdTeam === 'string') teamId = createdTeam; draw(); }, announce, signal)),
        element('div', { class: 'wf-actions' }, progress.done.has(active) || !next ? null : skip,
          next ? element('a', { class: 'btn', href: hashFor('setup', [next.id]) }, `Next: ${next.label}`) : link('#/overview', 'Go to the overview', { class: 'btn' })),
        status),
      element('p', {}, hide));
    if (focusHeading || args[0]) requestAnimationFrame(() => heading.focus());
    focusHeading = true;
  }

  await draw();
}
