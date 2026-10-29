import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { connectionBody, credentialText, releaseNote, subnetsFromText, subnetsText, tagsFromText, tagsText } = await import(new URL('views/aws.js', STATIC));
const { environmentTypeOptions, environmentTypePath } = await import(new URL('views/teams.js', STATIC));

test('subnets and tags round-trip through their one-per-line text', () => {
  const subnets = [{ zone: 'us-east-1a', subnet_id: 'subnet-0abc' }, { zone: 'us-east-1b', subnet_id: 'subnet-0def' }];
  assert.deepEqual(subnetsFromText(subnetsText(subnets)), subnets);
  assert.deepEqual(subnetsFromText('  us-east-1a   subnet-0abc \n\n'), [subnets[0]]);
  assert.deepEqual(tagsFromText(tagsText({ owner: 'platform', 'bpmeta:finops:team': 'a=b' })), { owner: 'platform', 'bpmeta:finops:team': 'a=b' });
});

test('connectionBody leaves blank credentials out so the server keeps them', () => {
  const body = connectionBody({ auth_kind: 'access_key', region: ' us-east-1 ', subnets: '', extra_tags: '', max_concurrent: '3', daily_budget_usd: '',
    access_key_id: '', secret_access_key: '  ', allow_host_allocation: false });
  assert.equal(body.region, 'us-east-1');
  assert.equal(body.max_concurrent, 3);
  assert.equal(body.daily_budget_usd, null);
  assert.equal(body.vpc_id, null);
  assert.equal('access_key_id' in body, false);
  assert.equal('secret_access_key' in body, false);
  assert.equal(connectionBody({ auth_kind: 'assume_role', role_arn: 'arn:aws:iam::1:role/x' }).role_arn, 'arn:aws:iam::1:role/x');
  const full = connectionBody({ auth_kind: 'access_key', template_linux: ' lt-0abc ', template_mac: '', allowed_instance_types: 'm7i.large, mac2.metal\n t3.large' });
  assert.deepEqual(full.launch_template_ids, { linux: 'lt-0abc' });
  assert.deepEqual(full.allowed_instance_types, ['m7i.large', 'mac2.metal', 't3.large']);
});

test('credentialText shows only whether credentials are set and a hint', () => {
  assert.equal(credentialText(null), 'Not set');
  assert.equal(credentialText({ set: true, access_key_id_last4: 'WXYZ' }), 'Set: access key ending WXYZ');
  assert.equal(credentialText({ set: true, role_arn: 'arn:r', external_id_set: true }), 'Set: role arn:r with an external id');
});

test('releaseNote holds a Mac host until its 24 hours are up', () => {
  const now = Date.parse('2026-10-29T12:00:00Z');
  assert.match(releaseNote({ min_release_at: '2026-10-30T12:00:00Z' }, now), /^Can be released after /);
  assert.equal(releaseNote({ min_release_at: '2026-10-29T11:00:00Z' }, now), null);
});

test('the project picker offers enabled types and keeps a disabled current one', () => {
  const types = [{ id: 'a', name: 'android', kind: 'ec2-linux', enabled: true }, { id: 'i', name: 'ios', kind: 'ec2-mac', enabled: false }];
  assert.deepEqual(environmentTypeOptions(types, null).map(([value]) => value), ['', 'a']);
  assert.deepEqual(environmentTypeOptions(types, 'i').map(([value]) => value), ['', 'a', 'i']);
  assert.equal(environmentTypePath('p/1'), '/api/projects/p%2F1/environment-type');
});
