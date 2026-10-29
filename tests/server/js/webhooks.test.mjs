import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { conditionsSummary, heldActions, mappingText, prettyMapping, ruleBody, senderLabel, splitList, webhookHref } = await import(new URL('views/webhooks.js', STATIC));
const { parseHash } = await import(new URL('router.js', STATIC));

test('ruleBody builds the config the server checks and passes the text through', () => {
  const body = ruleBody({ workflow_id: 'w1', order: '2', events: 'push, pull_request,', secrets: '', project_from: ' repository.full_name ', event_from: '', enabled: true, stop: false, shadow: true, dry: true, mapping: 'inputs: {pr: $.number}' });
  assert.deepEqual(body, {
    workflow_id: 'w1', enabled: true, mapping: 'inputs: {pr: $.number}',
    config: { order: 2, stop: false, shadow: true, dry: true, events: ['push', 'pull_request'], secrets: [], project_from: 'repository.full_name' },
  });
  assert.equal(ruleBody({ order: 'x' }).config.order, 0);
  assert.equal(ruleBody({ import_hash: 'a'.repeat(64) }).config.import_hash, 'a'.repeat(64));
  assert.equal('import_hash' in ruleBody({}).config, false);
  assert.equal(ruleBody({ key: 'pull_request.number', cancel_only: true }).config.cancel_only, true);
  assert.equal('cancel_only' in ruleBody({}).config, false);
});

test('prettyMapping formats JSON and leaves YAML as typed', () => {
  assert.equal(prettyMapping('{"inputs":{"a":"$"}}'), '{\n  "inputs": {\n    "a": "$"\n  }\n}');
  assert.equal(prettyMapping('inputs:\n  a: $'), 'inputs:\n  a: $');
});

test('mappingText round-trips through JSON', () => {
  const config = { conditions: [{ path: 'action', op: 'eq', value: 'opened' }], inputs: { pr: '$.number' } };
  assert.deepEqual(JSON.parse(mappingText(config)), config);
  assert.deepEqual(JSON.parse(mappingText({})), { conditions: [], inputs: {} });
});

test('conditionsSummary reads as one line', () => {
  assert.equal(conditionsSummary([]), 'Always');
  assert.equal(conditionsSummary([{ path: 'a', op: 'in', value: ['x'] }, { path: 'b', op: 'exists' }]), 'a in ["x"]; b exists');
  assert.equal(conditionsSummary([{ path: 'c', op: 'starts_with', value: 'fix', ignore_case: true }]), 'c starts_with "fix" (ignoring case)');
});

test('splitList and webhookHref', () => {
  assert.deepEqual(splitList(' a, ,b '), ['a', 'b']);
  assert.deepEqual(parseHash(webhookHref('a/b')).args, ['a/b']);
  assert.equal(parseHash(webhookHref('x')).name, 'webhook');
});

test('heldActions adds unpinned senders and offers no approve without a sender', () => {
  const approved = { source: 'github', approval: 'approved', pinned_senders: ['github:repo:1'] };
  assert.deepEqual(heldActions(approved, { status: 'accepted', sender: 'github:repo:1' }), []);
  assert.deepEqual(heldActions(approved, { status: 'held', sender: 'github:repo:1' }), ['replay', 'dismiss']);
  assert.deepEqual(heldActions(approved, { status: 'held', sender: 'github:repo:2' }), ['approve', 'dismiss']);
  assert.deepEqual(heldActions(approved, { status: 'held', sender: null }), ['dismiss']);
  assert.deepEqual(heldActions({ source: 'github', approval: 'pending', pinned_senders: null }, { status: 'held', sender: null }), ['reject']);
  assert.deepEqual(heldActions({ source: 'github', approval: 'pending', pinned_senders: null }, { status: 'held', sender: 'github:repo:1' }), ['approve', 'reject']);
  assert.deepEqual(heldActions({ source: 'github', approval: 'approved', pinned_senders: null }, { status: 'held', sender: 'github:repo:3' }), ['replay', 'dismiss']);
  assert.deepEqual(heldActions({ source: 'generic', approval: 'pending', pinned_senders: null }, { status: 'held', sender: null }), ['approve', 'reject']);
});

test('senderLabel says what the id is', () => {
  assert.equal(senderLabel('github:repo:123'), 'repository 123');
  assert.equal(senderLabel('github:installation:456'), 'installation 456');
  assert.equal(senderLabel('jira:https://acme.atlassian.net'), 'Jira site https://acme.atlassian.net');
  assert.equal(senderLabel('jira:cloud:c-1'), 'Jira site c-1');
  assert.equal(senderLabel('linear:org:o-1'), 'Linear workspace o-1');
  assert.equal(senderLabel('slack:team:T1'), 'Slack workspace T1');
  assert.equal(senderLabel(null), 'an unknown sender');
});
