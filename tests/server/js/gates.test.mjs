import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { decisionText, gatePath, waitingForYou } = await import(new URL('views/gates.js', STATIC));
const { runStatus } = await import(new URL('views/runs.js', STATIC));

test('waitingForYou keeps the open gates this viewer may decide', () => {
  const gates = [{ id: 'a', state: 'open', may_decide: true }, { id: 'b', state: 'open', may_decide: false }, { id: 'c', state: 'approved', may_decide: false }];
  assert.deepEqual(waitingForYou(gates).map((gate) => gate.id), ['a']);
});

test('decisionText says who decided and why, and no note for an expiry', () => {
  assert.equal(decisionText({ state: 'open' }), '');
  assert.match(decisionText({ state: 'rejected', decided_by_name: 'lead', decided_at: '2026-10-23T12:00:00Z', decision_note: 'not yet' }), /^Rejected by lead .*: not yet$/);
  assert.equal(decisionText({ state: 'expired', decision_note: 'expired' }), 'Expired');
});

test('gatePath escapes the id and runStatus names a waiting run', () => {
  assert.equal(gatePath('a/b', 'approve'), '/api/gates/a%2Fb/approve');
  assert.deepEqual(runStatus({ status: 'waiting' }, null), { text: 'Waiting for approval', tone: 'warn' });
});
