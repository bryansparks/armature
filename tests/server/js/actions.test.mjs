import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { confirmText, enabledActions, outcomeText, outcomeTone } = await import(new URL('actions.js', STATIC));
const { updatedText } = await import(new URL('views/status.js', STATIC));

test('enabledActions keeps only what the server enabled', () => {
  const payload = { actions: [
    { id: 'rerun', label: 'Rerun', enabled: true },
    { id: 'cancel', label: 'Cancel', enabled: false, reason: 'Your role cannot do this.' },
    { id: 'odd', label: 'Odd', enabled: 'yes' },
    null,
  ] };
  assert.deepEqual(enabledActions(payload).map((action) => action.id), ['rerun']);
  assert.deepEqual(enabledActions(null), []);
  assert.deepEqual(enabledActions({ actions: 'nope' }), []);
});

test('outcomeText leads with the outcome and keeps the host message', () => {
  assert.equal(outcomeText({ outcome: 'ok', message: 'Queued fs-eng_app-12.json' }), 'Done: Queued fs-eng_app-12.json');
  assert.equal(outcomeText({ outcome: 'refused', message: ' Already retried. ' }), 'Refused: Already retried.');
  assert.equal(outcomeText({ outcome: 'failed', message: '' }), 'Failed');
  assert.equal(outcomeText({ outcome: 'weird' }), 'Unknown');
  assert.equal(outcomeText(null), 'Unknown');
});

test('outcomeTone reads refused as a warning and failed as bad', () => {
  assert.equal(outcomeTone('ok'), 'ok');
  assert.equal(outcomeTone('refused'), 'warn');
  assert.equal(outcomeTone('failed'), 'bad');
  assert.equal(outcomeTone(undefined), 'warn');
});

test('confirmText uses the configured text, else names the action', () => {
  assert.equal(confirmText({ id: 'rerun', label: 'Rerun', confirm: 'Queue this run again?' }), 'Queue this run again?');
  assert.equal(confirmText({ id: 'rerun', label: 'Rerun', confirm: '  ' }), 'Run "Rerun" on this run?');
  assert.equal(confirmText({ id: 'cancel' }), 'Run "cancel" on this run?');
});

test('updatedText says when the host last reported', () => {
  assert.equal(updatedText({ configured: false }), 'No status command is configured on this server.');
  assert.equal(updatedText({ configured: true, updated_at: null }), 'Waiting for the first report from the host.');
  assert.match(updatedText({ configured: true, updated_at: new Date().toISOString() }), /^Updated \d+s ago$/);
});
