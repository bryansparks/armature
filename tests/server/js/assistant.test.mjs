import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const {
  aliasChoices, applyNote, assistBody, elapsedText, HISTORY_MAX, inputsText, MARKERS, markerText, needsTeam, NOTE_MAX, outputText,
  proposeBody, pushHistory, resultSummary, runChoiceLabel, startedByText, stepKind, stepNotes, stepRows, testRunBody,
} = await import(new URL('assistant.js', STATIC));

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

const step = (id, extra = {}) => ({ id, label: id, kind: 'llm', what: `${id} does it`, after: [], fan_out: null, output: 'text', optional: false, retry: null, ...extra });

test('steps after a change carry added, changed, and renamed markers, and removed steps are listed', () => {
  const described = { steps: [step('plan'), step('search'), step('sign_off'), step('notify')], post_run: [step('director')] };
  const changes = { added: ['notify'], removed: ['approve_old'], changed: [{ id: 'search', fields: ['fan_out', 'after'] }], renamed: [{ from: 'approve', to: 'sign_off' }] };
  const rows = stepRows(described, changes);
  assert.deepEqual(rows.steps.map((row) => row.marker), [null, 'changed', 'renamed', 'added']);
  assert.deepEqual(rows.postRun.map((row) => row.marker), [null]);
  assert.deepEqual(rows.removed, ['approve_old']);
  assert.equal(markerText(rows.steps[0]), null);
  assert.equal(markerText(rows.steps[1]), 'Changed: fan-out, order');
  assert.equal(markerText(rows.steps[2]), 'Renamed from approve');
  assert.equal(markerText(rows.steps[3]), 'Added');
  assert.equal(markerText({ marker: 'removed' }), 'Removed');
  for (const marker of Object.values(MARKERS)) assert.ok(marker.icon && marker.text);
  assert.deepEqual(stepRows(described, null).steps.map((row) => row.marker), [null, null, null, null]);
  assert.deepEqual(stepRows(null, null), { steps: [], postRun: [], removed: [] });
});

test('a step reads as its order, fan-out, output, and failure notes', () => {
  assert.deepEqual(stepNotes(step('a')), ['Returns text']);
  assert.deepEqual(stepNotes(step('b', { after: ['a', 'c'], fan_out: 'for each item in ⟨a.items⟩, up to 3 at a time', output: 'structured: verdict, notes', optional: true, retry: 'Tries again up to 2 times if it fails' })),
    ['After a, c', 'Runs for each item in ⟨a.items⟩, up to 3 at a time', 'Returns verdict, notes', 'Optional: the run goes on if it fails', 'Tries again up to 2 times if it fails']);
  assert.equal(outputText('structured'), 'Returns structured data');
  assert.equal(stepKind('gate').label, 'Approval');
  assert.equal(stepKind('mystery').label, 'Step');
  assert.equal(inputsText([{ name: 'topic', required: true }, { name: 'focus' }]), 'topic (required), focus');
  assert.equal(inputsText([]), 'none');
});

test('a webhook rule reads as its name, events, and state', () => {
  assert.equal(startedByText({ webhook: 'gh', events: ['pull_request'], shadow: false, enabled: true }), 'gh on pull_request');
  assert.equal(startedByText({ webhook: 'gh', events: [], shadow: true, enabled: false }), 'gh on any event (shadow: records only, off)');
});

test('history keeps the last five turns and the note joins them within the limit', () => {
  let history = [];
  for (let turn = 0; turn < HISTORY_MAX + 2; turn += 1) history = pushHistory(history, `ask ${turn}`, `did ${turn}`);
  assert.equal(history.length, HISTORY_MAX);
  assert.equal(history[0].instruction, 'ask 2');
  assert.equal(applyNote([{ instruction: 'Add a step.' }, { instruction: '  Make it\noptional. ' }]), 'Add a step. Then: Make it optional.');
  const long = applyNote([{ instruction: 'x'.repeat(NOTE_MAX + 50) }]);
  assert.equal(long.length, NOTE_MAX);
  assert.ok(long.endsWith('…'));
});

test('a describe request names the workflow and version, or the new name and scope', () => {
  assert.match(proposeBody({ record: teamRecord, instruction: '  ' }).error, /Describe/);
  assert.deepEqual(proposeBody({ record: teamRecord, baseVersion: 3, instruction: ' add a gate ' }).body,
    { kind: 'workflow', instruction: 'add a gate', alias: 'lead', workflow_id: 'w1', base_version: 3 });
  const refined = proposeBody({ record: instanceRecord, baseVersion: 1, instruction: 'more', history: [{ instruction: 'a', explanation: 'b' }], draft: 'name: x\n' }).body;
  assert.equal(refined.draft, 'name: x\n');
  assert.equal(refined.history.length, 1);
  assert.equal(refined.team_id, undefined);
  assert.match(proposeBody({ instruction: 'go', name: ' ', scope: { scope_kind: 'team', scope_id: 't1' } }).error, /name/);
  assert.deepEqual(proposeBody({ instruction: 'go', name: 'triage', scope: { scope_kind: 'project', scope_id: 'p1' } }).body,
    { kind: 'workflow', instruction: 'go', alias: 'lead', name: 'triage', scope_kind: 'project', scope_id: 'p1' });
  assert.deepEqual(proposeBody({ instruction: 'go', name: 'triage', scope: { scope_kind: 'instance', scope_id: null } }).body,
    { kind: 'workflow', instruction: 'go', alias: 'lead', name: 'triage', scope_kind: 'instance' });
  assert.equal(elapsedText(12.7), '12s');
});

test('a past run reads as when, status, and a short id', () => {
  const label = runChoiceLabel({ id: 'abcdef123456', status: 'succeeded', started_at: '2026-10-23T12:00:00Z' });
  assert.match(label, /succeeded · abcdef12$/);
});
