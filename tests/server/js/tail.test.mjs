import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { isHeartbeat, stillRunning, TAIL_CAP, TailModel, tailLine } = await import(new URL('tail.js', STATIC));

let nextSeq = 0;
const event = (kind, payload = {}, stepId = 'build') => ({ seq: ++nextSeq, ts: '2026-10-23T12:00:00Z', kind, step_id: stepId, payload });

test('isHeartbeat is a progress event with only elapsed time', () => {
  assert.equal(isHeartbeat(event('progress', { stage: 'build', elapsed_s: 12.5 })), true);
  assert.equal(isHeartbeat(event('progress', { elapsed_s: 3 })), true);
  assert.equal(isHeartbeat(event('progress', { elapsed_s: 3, message: 'half way' })), false);
  assert.equal(isHeartbeat(event('progress', {})), false);
  assert.equal(isHeartbeat(event('log', { elapsed_s: 3 })), false);
});

test('heartbeats update one line instead of adding rows', () => {
  const model = new TailModel();
  let change = model.append([event('step_start'), event('progress', { stage: 'build', elapsed_s: 60 }), event('progress', { stage: 'build', elapsed_s: 240 })]);
  assert.equal(change.added.length, 1);
  assert.equal(model.rows.length, 1);
  assert.deepEqual(model.beat, { elapsed_s: 240, step: 'build' });
  assert.equal(stillRunning(model.beat), 'build · still running 4m 0s');
  assert.equal(stillRunning({ elapsed_s: 5, step: '' }), 'Still running · 5s');
  change = model.append([event('progress', { elapsed_s: 5, message: 'real progress' })]);
  assert.equal(change.added.length, 1);
  assert.equal(model.beat, null);
});

test('the tail keeps the newest rows and reports the dropped ones', () => {
  const model = new TailModel(3);
  const first = model.append([event('log', { line: 'a' }), event('log', { line: 'b' })]);
  const second = model.append([event('log', { line: 'c' }), event('log', { line: 'd' }), event('log', { line: 'e' })]);
  assert.deepEqual(model.rows.map((row) => row.payload.line), ['c', 'd', 'e']);
  assert.deepEqual(second.removed, first.added.map((row) => row.id));
  assert.deepEqual(second.added.map((row) => row.payload.line), ['c', 'd', 'e']);
  const flood = model.append(Array.from({ length: 5 }, (_, index) => event('log', { line: `f${index}` })));
  assert.equal(flood.added.length, 3);
  assert.equal(flood.removed.length, 3);
  assert.equal(new TailModel().cap, TAIL_CAP);
});

test('the tail skips events it already has', () => {
  const model = new TailModel();
  const events = [event('log', { line: 'a' }), event('log', { line: 'b' })];
  model.append(events);
  assert.equal(model.append(events).added.length, 0);
  assert.equal(model.rows.length, 2);
});

test('tailLine draws log lines as monospace and the rest through describe', () => {
  assert.deepEqual(tailLine({ kind: 'log', payload: { line: '  npm ERR!', stream: 'stderr' } }), { tone: 'muted', label: 'stderr', text: '  npm ERR!', mono: true, who: null });
  assert.deepEqual(tailLine({ kind: 'log', payload: { line: 'ok' } }), { tone: 'muted', label: 'log', text: 'ok', mono: true, who: null });
  assert.equal(tailLine({ kind: 'log', payload: { message: 'from the engine' } }).mono, true);
  assert.equal(tailLine({ kind: 'log', payload: { line: 'x'.repeat(5000) } }).text.length, 2000);
  assert.deepEqual(tailLine({ kind: 'tool_call', payload: { tool: 'Bash', input: '{"command":"ls -la"}' } }), { tone: 'muted', label: 'Bash', text: 'ls -la', mono: false, who: null });
  assert.equal(tailLine({ kind: 'text', payload: { text: 'hello\nthere' } }).text, 'hello there');
  assert.deepEqual(tailLine({ kind: 'step_failed', payload: { error: 'boom' } }), { tone: 'bad', label: 'Step', text: 'Failed: boom', mono: false, who: null });
});

test('a script log line from the engine renders as monospace text', () => {
  const line = tailLine({ kind: 'log', step_id: 'build', payload: { stream: 'stderr', message: 'Compiling 12 files' } });
  assert.equal(line.text, 'Compiling 12 files');
  assert.equal(line.mono, true);
  assert.equal(line.tone, 'muted');
});

const roled = (kind, role, payload = {}, stepId = 'reviews') => event(kind, { role, ...payload }, stepId);

test('a role keeps one now line that its next activity replaces and its finish removes', () => {
  const model = new TailModel();
  let change = model.append([roled('log', 'security', { state: 'started', message: 'started reviewing fs/app#7 (sonnet 5.5)' }),
    roled('tool_call', 'security', { tool: 'Read', input: '{"file_path":"/w/App/FamilyTreeTabs.swift"}' }),
    roled('tool_call', 'beauty', { tool: 'Bash', input: '{"command":"git diff main"}' })]);
  assert.equal(change.added.length, 1);
  assert.deepEqual(model.nowLines().map((line) => [line.role, line.text]), [['security', 'Read FamilyTreeTabs.swift'], ['beauty', 'Bash: git diff main']]);
  model.append([roled('text', 'security', { text: 'The selection resets when the view rebuilds' }), roled('tool_result', 'security', { tool: 'Read', content: 'x' })]);
  assert.equal(model.nowLines()[0].text, 'thinking: The selection resets when the view rebuilds');
  change = model.append([roled('log', 'security', { state: 'finished', message: 'finished in 4m 2s: 2 findings', findings: 2 })]);
  assert.equal(change.added.length, 1);
  assert.deepEqual(model.nowLines().map((line) => line.role), ['beauty']);
  assert.equal(model.rows.length, 2);
});

test('a step with roles drops its start and finish rows but keeps failures', () => {
  const model = new TailModel();
  const first = model.append([event('step_start', {}, 'reviews'), event('step_start', {}, 'verify')]);
  const second = model.append([roled('log', 'security', { state: 'started', message: 'started' }), event('step_end', {}, 'reviews'),
    event('step_failed', { error: 'boom' }, 'reviews')]);
  assert.deepEqual(second.removed, [first.added[0].id]);
  assert.deepEqual(model.rows.map((row) => [row.kind, row.step_key]), [['step_start', 'verify'], ['log', 'reviews'], ['step_failed', 'reviews']]);
});

test('tailLine labels a role line with the role and colors a verdict', () => {
  assert.deepEqual(tailLine({ kind: 'log', payload: { role: 'security', message: 'started reviewing fs/app (sonnet 5.5)' } }),
    { tone: 'muted', label: null, text: 'started reviewing fs/app (sonnet 5.5)', mono: false, who: 'security' });
  assert.equal(tailLine({ kind: 'log', payload: { role: 'security', level: 'error', message: 'AWS authentication failed' } }).tone, 'bad');
  assert.equal(tailLine({ kind: 'log', payload: { role: 'team lead', verdict: 'Changes recommended', message: 'Changes recommended: 3 findings (1 blocking)' } }).tone, 'warn');
  assert.equal(tailLine({ kind: 'log', payload: { role: 'team lead', verdict: 'Approved', message: 'Approved: no findings' } }).tone, 'ok');
  const finding = tailLine({ kind: 'finding', payload: { role: 'team lead', blocking: true, severity: 'medium', title: 'Selection is lost', path: 'a.swift', line: 3 } });
  assert.deepEqual([finding.tone, finding.label, finding.who], ['bad', 'Blocking', 'team lead']);
  assert.equal(finding.text, 'Selection is lost at a.swift:3');
});

test('endNotice tells of an end only when the run was seen running, then ending', async () => {
  const { endNotice } = await import(new URL('tail.js', STATIC));
  const told = [];
  const watched = endNotice((run) => told.push(run.id));
  watched.opened({ id: 1, status: 'running' });
  watched.loaded();
  watched.ending();
  assert.equal(watched.ended({ id: 1 }), true);
  assert.equal(watched.ended({ id: 1 }), false, 'once only');

  const over = endNotice((run) => told.push(run.id));
  over.opened({ id: 2, status: 'succeeded' });
  over.loaded();
  over.ending();
  assert.equal(over.ended({ id: 2 }), false, 'already over when the tail opened');

  const replayed = endNotice((run) => told.push(run.id));
  replayed.opened({ id: 3, status: 'running' });
  replayed.ending();
  replayed.loaded();
  assert.equal(replayed.ended({ id: 3 }), false, 'its end was among the events loaded on opening');
  assert.deepEqual(told, [1]);
});

test('endNotice tells at once when the page drew a run as running that is already over', async () => {
  const { endNotice } = await import(new URL('tail.js', STATIC));
  const told = [];
  const notice = endNotice((run) => told.push(run.id), { drawnLive: true });
  assert.equal(notice.opened({ id: 4, status: 'succeeded' }), true);
  notice.loaded();
  notice.ending();
  assert.equal(notice.ended({ id: 4 }), false, 'once only');
  assert.equal(endNotice(() => told.push('x'), { drawnLive: true }).opened({ id: 5, status: 'running' }), false);
  assert.deepEqual(told, [4]);
});

