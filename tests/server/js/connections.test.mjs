import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { KINDS, connectionBody, credentialText, identityText } = await import(new URL('views/connections.js', STATIC));
const { connectionOptions } = await import(new URL('views/webhooks.js', STATIC));

test('connectionBody leaves blank credentials out so the server keeps them', () => {
  const body = connectionBody('slack', { name: ' main ', team_id: ' T0ACME ', app_id: '', bot_token: '  ', signing_secret: 'sig' });
  assert.deepEqual(body, { name: 'main', kind: 'slack', team_id: 'T0ACME', app_id: null, signing_secret: 'sig' });
  assert.equal('kind' in connectionBody('linear', { name: 'l' }, { creating: false }), false);
  assert.deepEqual(Object.keys(connectionBody('jira', { name: 'j' })).sort(), ['email', 'kind', 'name', 'site_url']);
  assert.deepEqual(connectionBody('github_app', { name: 'cli', auth: 'gh_cli', private_key_pem: ' ' }),
    { name: 'cli', kind: 'github_app', auth: 'gh_cli', host: null, client_id: null, app_id: null });
});

test('every kind names its config and secret fields', () => {
  assert.deepEqual(Object.keys(KINDS), ['github_app', 'slack', 'jira', 'linear']);
  for (const spec of Object.values(KINDS)) assert.ok(spec.secrets.length && spec.hint);
});

test('credentialText shows only which credentials are set and a token hint', () => {
  assert.equal(credentialText({ kind: 'slack', credentials: { bot_token: { set: true, last4: 'mnop' }, signing_secret: { set: false } } }), 'Bot token ending mnop');
  assert.equal(credentialText({ kind: 'github_app', credentials: { private_key_pem: { set: true }, webhook_secret: { set: true } } }), 'Private key, Webhook secret');
  assert.equal(credentialText({ kind: 'linear', credentials: { unreadable: true } }), 'Can’t be read. Enter them again.');
  assert.equal(credentialText({ kind: 'jira', credentials: {} }), 'Not set');
});

test('identityText says who each kind of connection is', () => {
  assert.equal(identityText({ kind: 'github_app', identity: { bot_login: 'armature[bot]' }, installations: [{ owner: 'acme' }, { owner: 'fs' }] }), 'armature[bot], installed on acme, fs');
  assert.equal(identityText({ kind: 'github_app', identity: { auth: 'gh_cli', login: 'stephan' }, installations: [] }), 'stephan (gh CLI)');
  assert.equal(identityText({ kind: 'slack', identity: { bot_user: 'bot', team: 'Acme' } }), '@bot in Acme');
  assert.equal(identityText({ kind: 'linear', identity: { user: 'Armature', org: 'Acme' } }), 'Armature in Acme');
  assert.equal(identityText({ kind: 'jira', identity: {} }), '');
});

test('a webhook offers only connections of its source’s kind', () => {
  const connections = [{ id: 'a', kind: 'github_app', name: 'bot' }, { id: 'b', kind: 'slack', name: 'main' }];
  assert.deepEqual(connectionOptions(connections, 'github'), [['', 'None: a team secret'], ['a', 'Connection bot']]);
  assert.deepEqual(connectionOptions(connections, 'generic'), [['', 'None: a team secret']]);
});
