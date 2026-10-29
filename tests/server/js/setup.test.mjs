import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { connectionValues, parseOutputs } = await import(new URL('awsoutputs.js', STATIC));
const { awsValues, modelRequests, presetTypeBody, reachable, runnerCommand, serverAddress, setupProgress, showBanner, STEPS, TYPE_PRESETS, webhookBody } = await import(new URL('views/setup.js', STATIC));
const { githubConnections, parseRepo, projectBody } = await import(new URL('views/projects.js', STATIC));
const { connectionBody } = await import(new URL('views/aws.js', STATIC));

const DESCRIBE = JSON.stringify({ Stacks: [{ Outputs: [
  { OutputKey: 'SecurityGroupId', OutputValue: 'sg-0abc', Description: 'x' },
  { Description: 'Linux', OutputValue: 'lt-0linux', OutputKey: 'LaunchTemplateIds' },
  { OutputKey: 'BucketName', OutputValue: 'armature-results' },
  { OutputKey: 'WorkerRoleArn', OutputValue: 'arn:aws:iam::1:role/worker' },
] }] }, null, 2);
const HOST = JSON.stringify([{ OutputKey: 'HostManagerRoleArn', OutputValue: 'arn:aws:iam::1:role/host' },
  { OutputKey: 'SessionRoleArn', OutputValue: 'arn:aws:iam::1:role/session' }, { OutputKey: 'AllowHostAllocation', OutputValue: 'false' }]);

test('parseOutputs reads describe-stacks JSON, several stacks pasted together', () => {
  const found = parseOutputs(`${DESCRIBE}\n${HOST}`);
  assert.equal(found.SecurityGroupId, 'sg-0abc');
  assert.equal(found.LaunchTemplateIds, 'lt-0linux');
  assert.equal(found.HostManagerRoleArn, 'arn:aws:iam::1:role/host');
  assert.equal(found.AllowHostAllocation, 'false');
});

test('parseOutputs reads an object and Name=Value, Name: Value, and tab lines', () => {
  assert.deepEqual(parseOutputs('{"BucketName": "b", "SecurityGroupId": "sg-1"}'), { BucketName: 'b', SecurityGroupId: 'sg-1' });
  assert.deepEqual(parseOutputs('BucketName=b\nSecurityGroupId: sg-1\nInstanceProfileName\tprofile\n\nnot a pair at all'),
    { BucketName: 'b', SecurityGroupId: 'sg-1', InstanceProfileName: 'profile' });
  assert.deepEqual(parseOutputs(''), {});
});

test('connectionValues maps outputs to the connection form and drops the rest', () => {
  const values = connectionValues(parseOutputs(`${DESCRIBE}\n${HOST}`));
  assert.deepEqual(values, { role_arn: 'arn:aws:iam::1:role/host', session_role_arn: 'arn:aws:iam::1:role/session', allow_host_allocation: false,
    security_group_id: 'sg-0abc', template_linux: 'lt-0linux', artifact_bucket: 'armature-results' });
  assert.equal(connectionValues({ AllowHostAllocation: 'True' }).allow_host_allocation, true);
});

test('awsValues fills the connection body from pasted outputs plus typed fields', () => {
  const values = awsValues(`${DESCRIBE}\n${HOST}`, { region: 'us-west-2', vpc_id: 'vpc-1', subnets: 'us-west-2a subnet-1', external_id: 'ext', access_key_id: '', secret_access_key: '' });
  assert.equal(values.auth_kind, 'assume_role');
  const body = connectionBody(values);
  assert.equal(body.region, 'us-west-2');
  assert.equal(body.role_arn, 'arn:aws:iam::1:role/host');
  assert.equal(body.external_id, 'ext');
  assert.deepEqual(body.launch_template_ids, { linux: 'lt-0linux' });
  assert.deepEqual(body.subnets, [{ zone: 'us-west-2a', subnet_id: 'subnet-1' }]);
  assert.equal(body.security_group_id, 'sg-0abc');
  assert.equal(awsValues(HOST, { access_key_id: 'AKIA1', secret_access_key: 's' }).auth_kind, 'access_key_assume_role');
  assert.equal(awsValues('', { access_key_id: 'AKIA1', secret_access_key: 's' }).auth_kind, 'access_key');
});

test('setupProgress counts what exists, and skipped steps settle without counting as done', () => {
  const empty = { providers: [], runners: [], connections: [], projects: [], webhooks: [], skipped: [] };
  const start = setupProgress(empty);
  assert.equal(start.count, 2);
  assert.equal(start.total, STEPS.length);
  assert.equal(start.next, 'models');
  assert.equal(start.complete, false);
  const partway = setupProgress({ ...empty, providers: [{}], aws: { connection: { state: 'error' } }, projects: [{ repo: null }], skipped: ['aws'] });
  assert.deepEqual([...partway.done], ['database', 'admin', 'models']);
  assert.equal(partway.next, 'runners');
  const finished = setupProgress({ providers: [{}], aws: { connection: { state: 'ok' } }, runners: [{}], connections: [{}], projects: [{ repo: 'a/b' }], webhooks: [{}], skipped: [] });
  assert.equal(finished.count, 8);
  assert.equal(finished.complete, true);
  assert.equal(setupProgress({ ...empty, skipped: ['models', 'aws', 'runners', 'connections', 'repos', 'webhooks'] }).complete, true);
  assert.equal(showBanner(empty), true);
  assert.equal(showBanner({ ...empty, dismissed: true }), false);
});

test('modelRequests saves the key as a team secret, then a provider whose default alias is the model', () => {
  const { calls } = modelRequests('t/1', 'anthropic', { key: ' sk-ant ', model: 'claude-sonnet-4-6' });
  assert.deepEqual(calls, [
    ['/api/teams/t%2F1/secrets', { name: 'ANTHROPIC_API_KEY', value: 'sk-ant' }],
    ['/api/teams/t%2F1/providers', { kind: 'anthropic', config: {}, aliases: { default: 'claude-sonnet-4-6' }, credential_secret: 'ANTHROPIC_API_KEY' }],
  ]);
  assert.match(modelRequests('t', 'anthropic', { key: '', model: 'm' }).error, /api key/);
  assert.match(modelRequests('t', 'claude-subscription', { key: '', model: 'm' }).error, /token/);
  assert.match(modelRequests('t', 'anthropic', { key: 'k', model: ' ' }).error, /model/);
  assert.match(modelRequests('t', 'nope', {}).error, /Pick/);
});

test('modelRequests for Bedrock and other servers needs no key', () => {
  assert.deepEqual(modelRequests('t', 'bedrock', { model: 'm', region: '' }).calls,
    [['/api/teams/t/providers', { kind: 'bedrock', config: { region: 'us-east-1' }, aliases: { default: 'm' }, credential_secret: null }]]);
  const ollama = modelRequests('t', 'other', { model: 'llama3', api_base: 'http://box:11434', flavor: 'ollama' }).calls;
  assert.equal(ollama.length, 1);
  assert.equal(ollama[0][1].kind, 'ollama');
  const compatible = modelRequests('t', 'other', { model: 'm', api_base: 'https://llm.internal', flavor: 'openai-compatible', key: 'k' }).calls;
  assert.equal(compatible[0][1].name, 'MODEL_API_KEY');
  assert.equal(compatible[1][1].kind, 'openai-compatible');
  assert.match(modelRequests('t', 'other', { model: 'm', api_base: '' }).error, /address/);
});

test('presetTypeBody and webhookBody build what the server takes', () => {
  const android = TYPE_PRESETS.find((preset) => preset.name === 'android');
  assert.deepEqual(presetTypeBody(android, ' ami-0123456789 '), { name: 'android', kind: 'ec2-linux', image: 'ami-0123456789', capabilities: ['git', 'platform:android'],
    instance_types: ['m6i.large'], market: 'spot-then-on-demand', enabled: true });
  assert.equal(presetTypeBody(TYPE_PRESETS[0], 'img').kind, 'ec2-mac');
  assert.deepEqual(webhookBody({ id: 'c1', kind: 'github_app', name: 'main' }), { name: 'GitHub main', source: 'github', connection_id: 'c1' });
  assert.equal(webhookBody({ id: 'c2', kind: 'slack', name: 'chat' }).source, 'slack');
});

test('reachable flags a loopback webhook address', () => {
  assert.equal(reachable('http://127.0.0.1:8098/hooks/x'), false);
  assert.equal(reachable('http://localhost:8098/hooks/x'), false);
  assert.equal(reachable(`https://arm.example.com/hooks/x`), true);
});

test('parseRepo and projectBody turn an address into owner/name and a project name', () => {
  assert.deepEqual(parseRepo('https://github.com/acme/app.git'), { slug: 'acme/app', name: 'app' });
  assert.deepEqual(parseRepo('git@github.com:acme/app.git'), { slug: 'acme/app', name: 'app' });
  assert.deepEqual(parseRepo('github.com/acme/app'), { slug: 'acme/app', name: 'app' });
  assert.deepEqual(parseRepo('acme/app'), { slug: 'acme/app', name: 'app' });
  assert.equal(parseRepo('https://github.com/acme/app/tree/main'), null);
  assert.equal(parseRepo('https://gitlab.com/acme/app'), null);
  assert.equal(parseRepo('ac.me/app'), null);
  assert.equal(parseRepo('acme/..'), null);
  assert.deepEqual(parseRepo('https://GitHub.com/acme/app/'), { slug: 'acme/app', name: 'app' });
  assert.deepEqual(projectBody({ repo: 'acme/app', environment_type_id: '', connection_id: 'c1', default_branch: ' ' }).body,
    { name: 'app', repo: 'acme/app', environment_type_id: null, connection_id: 'c1', default_branch: null });
  assert.match(projectBody({ repo: 'nope' }).error, /owner\/name/);
  assert.deepEqual(githubConnections([{ kind: 'slack' }, { kind: 'github_app', id: 'g' }]), [{ kind: 'github_app', id: 'g' }]);
});

test('loadSnapshot reads the first team’s models, connections, projects, and webhooks, and the instance settings', async () => {
  const { loadSnapshot } = await import(new URL('views/setup.js', STATIC));
  const replies = {
    '/api/teams': { teams: [{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }] },
    '/api/settings': { settings: [{ key: 'setup.skipped', value: ['aws'], scope: '*' }, { key: 'setup.dismissed', value: true, scope: 'a' }] },
    '/api/aws': { connection: { state: 'ok' }, types: [] },
    '/api/runners': { runners: [{ name: 'box', online: true }] },
    '/api/teams/b/providers': { providers: [{ kind: 'anthropic' }] },
    '/api/teams/b/connections': { connections: [] },
    '/api/teams/b/projects': { projects: [{ repo: 'o/r' }] },
    '/api/teams/b/webhooks': { webhooks: [] },
  };
  const asked = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (path, options) => {
    asked.push([options.method, path]);
    return { status: 200, json: async () => replies[path] };
  };
  try {
    const snapshot = await loadSnapshot('b');
    assert.equal(snapshot.team.id, 'b');
    assert.deepEqual(snapshot.skipped, ['aws']);
    assert.equal(snapshot.dismissed, false);
    assert.deepEqual([...setupProgress(snapshot).done], ['database', 'admin', 'models', 'aws', 'runners', 'repos']);
    assert.deepEqual(asked.map(([, path]) => path).sort(), Object.keys(replies).sort());
    assert.ok(asked.every(([method]) => method === 'GET'));
  } finally {
    globalThis.fetch = realFetch;
  }
});

test('runnerCommand keeps the key in the environment and names the server address', () => {
  assert.equal(runnerCommand('https://arm.example.com', 'ark_1'),
    'export ARMATURE_RUNNER_KEY=ark_1\narmature runner --server https://arm.example.com --capability llm --capability git --slots 1');
  assert.match(runnerCommand('x', 'k', ['llm']), /--capability llm --slots 1$/);
  const doc = { querySelector: () => ({ getAttribute: () => '/arm/' }) };
  assert.equal(serverAddress(doc, { origin: 'https://arm.example.com' }), 'https://arm.example.com/arm');
  assert.equal(reachable('http://127.0.0.1:8098'), false);
});
