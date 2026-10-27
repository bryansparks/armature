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
  assert.equal(stillRunning(model.beat), 'Still running build · 4m 0s');
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
  assert.deepEqual(tailLine({ kind: 'log', payload: { line: '  npm ERR!', stream: 'stderr' } }), { tone: 'muted', label: 'stderr', text: '  npm ERR!', mono: true });
  assert.deepEqual(tailLine({ kind: 'log', payload: { line: 'ok' } }), { tone: 'muted', label: 'log', text: 'ok', mono: true });
  assert.equal(tailLine({ kind: 'log', payload: { message: 'from the engine' } }).mono, true);
  assert.equal(tailLine({ kind: 'log', payload: { line: 'x'.repeat(5000) } }).text.length, 2000);
  assert.deepEqual(tailLine({ kind: 'tool_call', payload: { tool: 'Bash', input: '{"command":"ls -la"}' } }), { tone: 'muted', label: 'Bash', text: 'ls -la', mono: false });
  assert.equal(tailLine({ kind: 'text', payload: { text: 'hello\nthere' } }).text, 'hello there');
  assert.deepEqual(tailLine({ kind: 'step_failed', payload: { error: 'boom' } }), { tone: 'bad', label: 'Step', text: 'Failed: boom', mono: false });
});

test('a script log line from the engine renders as monospace text', () => {
  const line = tailLine({ kind: 'log', step_id: 'build', payload: { stream: 'stderr', message: 'Compiling 12 files' } });
  assert.equal(line.text, 'Compiling 12 files');
  assert.equal(line.mono, true);
  assert.equal(line.tone, 'muted');
});
