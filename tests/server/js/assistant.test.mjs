import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { aliasChoices, assistBody, needsTeam, resultSummary, runChoiceLabel, testRunBody } = await import(new URL('assistant.js', STATIC));

const teamRecord = { id: 'w1', name: 'review', team_id: 't1' };
const instanceRecord = { id: 'w2', name: 'review', team_id: null };

test('aliases come from every provider with lead first', () => {
  assert.deepEqual(aliasChoices([{ aliases: { review: 'a', lead: 'b' } }, { aliases: { fast: 'c', review: 'd' } }]), ['lead', 'fast', 'review']);
  assert.deepEqual(aliasChoices([]), []);
  assert.deepEqual(aliasChoices(null), []);
});

test('an instance object needs a picked team', () => {
  assert.equal(needsTeam(teamRecord), false);
  assert.equal(needsTeam(instanceRecord), true);
  assert.match(assistBody({ kind: 'workflow', record: instanceRecord, body: 'x', instruction: 'go' }).error, /team/);
  assert.deepEqual(assistBody({ kind: 'workflow', record: instanceRecord, body: 'x', instruction: ' go ', teamId: 't9' }).body,
    { kind: 'workflow', id: 'w2', body: 'x', instruction: 'go', alias: 'lead', team_id: 't9' });
});

test('an assist request needs an instruction of a sane length', () => {
  assert.match(assistBody({ kind: 'workflow', record: teamRecord, body: 'x', instruction: '   ' }).error, /Say what/);
  assert.match(assistBody({ kind: 'workflow', record: teamRecord, body: 'x', instruction: 'a'.repeat(4001) }).error, /4000/);
  const { body } = assistBody({ kind: 'document', record: teamRecord, body: 'x', instruction: 'go', alias: 'fast', teamId: 'ignored' });
  assert.deepEqual(body, { kind: 'document', id: 'w1', body: 'x', instruction: 'go', alias: 'fast' });
});

test('a result summary says whether the proposal validates and what it cost', () => {
  const ok = resultSummary({ validation: { ok: true, errors: [] }, cost_usd: 0.0123, attempts: 1 });
  assert.equal(ok.valid, true);
  assert.equal(ok.status, 'The proposal validates.');
  assert.match(ok.cost, /0\.0123/);
  const bad = resultSummary({ validation: { ok: false, errors: [{}, {}] }, cost_usd: 0.02, attempts: 2 });
  assert.equal(bad.valid, false);
  assert.match(bad.status, /2 errors/);
  assert.match(bad.cost, /after 2 tries/);
});

test('a test run body parses inputs and names a team only when needed', () => {
  assert.deepEqual(testRunBody({ record: teamRecord, fromRun: '', inputsText: '', missingEmpty: false }).body,
    { mode: 'stub', from_run: null, inputs: null, missing: 'fail' });
  assert.deepEqual(testRunBody({ record: teamRecord, fromRun: 'r1', inputsText: '{"number": 7}', missingEmpty: true }).body,
    { mode: 'stub', from_run: 'r1', inputs: { number: 7 }, missing: 'empty' });
  assert.match(testRunBody({ record: teamRecord, inputsText: '{nope' }).error, /JSON/);
  assert.match(testRunBody({ record: teamRecord, inputsText: '[1]' }).error, /object/);
  assert.match(testRunBody({ record: instanceRecord, inputsText: '' }).error, /team/);
  assert.equal(testRunBody({ record: instanceRecord, inputsText: '', teamId: 't3' }).body.team_id, 't3');
});

test('a past run reads as when, status, and a short id', () => {
  const label = runChoiceLabel({ id: 'abcdef123456', status: 'succeeded', started_at: '2026-10-23T12:00:00Z' });
  assert.match(label, /succeeded · abcdef12$/);
});
