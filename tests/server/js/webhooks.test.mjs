import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { conditionsSummary, mappingText, prettyMapping, ruleBody, splitList, webhookHref } = await import(new URL('views/webhooks.js', STATIC));
const { parseHash } = await import(new URL('router.js', STATIC));

test('ruleBody builds the config the server checks and passes the text through', () => {
  const body = ruleBody({ workflow_id: 'w1', order: '2', events: 'push, pull_request,', secrets: '', project_from: ' repository.full_name ', event_from: '', enabled: true, stop: false, shadow: true, mapping: 'inputs: {pr: $.number}' });
  assert.deepEqual(body, {
    workflow_id: 'w1', enabled: true, mapping: 'inputs: {pr: $.number}',
    config: { order: 2, stop: false, shadow: true, events: ['push', 'pull_request'], secrets: [], project_from: 'repository.full_name' },
  });
  assert.equal(ruleBody({ order: 'x' }).config.order, 0);
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
