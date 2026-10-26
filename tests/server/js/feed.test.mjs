import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { applyStepEvent, branchText, eventStepKey, stepKey, descendants, describe, earlierPage, elapsedSeconds, FeedModel, groupItems, newestBefore, NODE_CAP, oneLine, stepTree, summarizeInput } = await import(new URL('feed.js', STATIC));
const { duration, statusText, statusTone } = await import(new URL('dom.js', STATIC));

let nextSeq = 0;
const event = (kind, payload = {}, stepId = 'build') => ({ seq: ++nextSeq, ts: '2026-10-23T12:00:00Z', kind, step_id: stepId, payload });

// What a renderer holding one node per item would hold after applying each change set.
function rendered(model, batches) {
  const nodes = new Map();
  let most = 0;
  for (const batch of batches) {
    const { added, updated, removed } = model.append(batch);
    for (const id of removed) assert.ok(nodes.delete(id), `removed ${id} was never rendered`);
    for (const item of updated) assert.ok(nodes.has(item.id), `updated ${item.id} is not rendered`);
    for (const item of added) {
      assert.ok(!nodes.has(item.id));
      nodes.set(item.id, item);
    }
    most = Math.max(most, nodes.size);
  }
  return { nodes, most };
}

test('5,000 synthetic events never render more than the node cap', () => {
  nextSeq = 0;
  const kinds = ['text', 'tool_call', 'tool_result', 'log', 'progress', 'cost', 'finding', 'step_start'];
  const events = [];
  for (let index = 0; index < 5000; index += 1) {
    const kind = kinds[index % kinds.length];
    const toolUseId = `t${Math.floor(index / kinds.length)}`;
    events.push(event(kind, { text: `line ${index}`, tool: 'Bash', tool_use_id: toolUseId, input: '{"command":"ls"}', content: 'ok', message: 'm' }, `step-${Math.floor(index / 700)}`));
  }
  const model = new FeedModel();
  const batches = [];
  for (let index = 0; index < events.length; index += 4) batches.push(events.slice(index, index + 4));
  const started = performance.now();
  const { nodes, most } = rendered(model, batches);
  assert.ok(performance.now() - started < 1000);
  assert.ok(most <= NODE_CAP, `rendered ${most}`);
  assert.equal(nodes.size, model.items.length);
  assert.equal(model.items.length, NODE_CAP);
  assert.equal(model.lastSeq, 5000);
  assert.ok(model.hasEarlier);
  assert.ok(model.calls.size <= NODE_CAP);
  for (const call of model.calls.values()) assert.ok(model.items.includes(call));
});

test('one batch larger than the cap renders only its tail', () => {
  nextSeq = 0;
  const model = new FeedModel({ cap: 10 });
  const { added, removed } = model.append(Array.from({ length: 25 }, () => event('log', { message: 'x' })));
  assert.equal(added.length, 10);
  assert.equal(removed.length, 0);
  assert.equal(model.items[0].seq, 16);
});

test('a tool result pairs with its call by tool_use_id', () => {
  nextSeq = 0;
  const model = new FeedModel();
  const first = model.append([event('tool_call', { tool: 'Read', tool_use_id: 'a', input: '{"file_path":"x.py"}' }), event('tool_call', { tool: 'Bash', tool_use_id: 'b', input: '{}' })]);
  assert.equal(first.added.length, 2);
  const second = model.append([event('tool_result', { tool: 'Bash', tool_use_id: 'b', is_error: true, content: 'boom' })]);
  assert.deepEqual(second.added, []);
  assert.equal(second.updated.length, 1);
  assert.equal(second.updated[0].payload.tool_use_id, 'b');
  assert.equal(second.updated[0].result.content, 'boom');
  assert.equal(model.items[0].result, null);
});

test('a result whose call is gone renders on its own', () => {
  nextSeq = 0;
  const model = new FeedModel();
  const { added } = model.append([event('tool_result', { tool: 'Bash', tool_use_id: 'missing', content: 'out' })]);
  assert.equal(added.length, 1);
  assert.equal(describe(added[0]).label, 'Bash');
});

test('a result in the same batch as its call is drawn with the call', () => {
  nextSeq = 0;
  const model = new FeedModel();
  const { added, updated } = model.append([event('tool_call', { tool_use_id: 'c' }), event('tool_result', { tool_use_id: 'c', content: 'done' })]);
  assert.equal(added.length, 1);
  assert.equal(updated.length, 0);
  assert.equal(added[0].result.content, 'done');
});

test('progress and dropped coalesce within a step, text does not', () => {
  nextSeq = 0;
  const model = new FeedModel();
  model.append([event('progress', { n: 1 }), event('progress', { n: 2 }), event('dropped', { count: 3 }), event('dropped', { count: 4 })]);
  model.append([event('text', { text: 'a' }), event('text', { text: 'b' }), event('progress', { n: 3 }, 'other')]);
  assert.deepEqual(model.items.map((item) => item.kind), ['progress', 'dropped', 'text', 'text', 'progress']);
  assert.equal(model.items[0].payload.n, 2);
  assert.equal(model.items[0].count, 2);
  assert.equal(model.items[1].payload.count, 7);
  assert.equal(describe(model.items[1]).text, '7 updates skipped');
});

test('a coalesced update to an item added in the same batch is not reported twice', () => {
  nextSeq = 0;
  const model = new FeedModel();
  const { added, updated } = model.append([event('progress'), event('progress')]);
  assert.equal(added.length, 1);
  assert.equal(updated.length, 0);
});

test('replayed and out-of-date events are ignored', () => {
  nextSeq = 0;
  const model = new FeedModel();
  const events = [event('log'), event('log'), event('log')];
  model.append(events);
  assert.deepEqual(model.append(events).added, []);
  assert.deepEqual(model.append([{ kind: 'log' }]).added, []);
  assert.equal(model.items.length, 3);
});

test('loading earlier keeps the cap and leaves the feed behind live', () => {
  nextSeq = 0;
  const all = Array.from({ length: 30 }, () => event('log', { message: 'x' }));
  const model = new FeedModel({ cap: 10 });
  model.append(all);
  assert.equal(model.firstSeq, 21);
  const page = earlierPage(model.firstSeq, 5);
  assert.deepEqual(page, { after: 15, limit: 5 });
  model.prepend(all.slice(page.after, page.after + page.limit));
  assert.equal(model.items.length, 10);
  assert.equal(model.firstSeq, 16);
  assert.ok(model.detached);
  assert.equal(model.lastSeq, 25);
  const { added } = model.append([event('log')]);
  assert.deepEqual(added, []);
  assert.equal(model.missed, 1);
  model.reset();
  assert.equal(model.detached, false);
});

test('earlierPage stops at the first event', () => {
  assert.deepEqual(earlierPage(3, 500), { after: 0, limit: 2 });
  assert.deepEqual(earlierPage(1, 500), { after: 0, limit: 0 });
});

test('groupItems groups consecutive items of one step', () => {
  const groups = groupItems([{ step_key: 'a' }, { step_key: 'a' }, { step_key: null }, { step_key: 'a' }]);
  assert.deepEqual(groups.map((group) => [group.step_key, group.items.length]), [['a', 2], [null, 1], ['a', 1]]);
  assert.deepEqual(groupItems([]), []);
});

test('summarizeInput picks the telling field, even from cut-off JSON', () => {
  assert.equal(summarizeInput('{"command":"git status\\n  --short","description":"x"}'), 'git status --short');
  assert.equal(summarizeInput('{"file_path":"/a/b.py","limit":5}'), '/a/b.py');
  assert.equal(summarizeInput('{"pattern":"TODO","path":"src", "glob": "*.py'), 'TODO');
  assert.equal(summarizeInput('{"other":1}'), '{"other":1}');
  assert.equal(summarizeInput('plain text'), 'plain text');
  assert.equal(summarizeInput(undefined), '');
  assert.equal(summarizeInput('x'.repeat(500)).length, 140);
});

test('oneLine flattens whitespace and truncates', () => {
  assert.equal(oneLine('a\n\n b\tc'), 'a b c');
  assert.equal(oneLine('abcdef', 4), 'abc…');
  assert.equal(oneLine(null), '');
});

test('applyStepEvent tracks a step through its events', () => {
  const steps = [];
  assert.equal(applyStepEvent(steps, { kind: 'log', step_id: 'a' }), null);
  applyStepEvent(steps, { kind: 'step_start', step_id: 'a', ts: '2026-10-23T12:00:00Z' });
  assert.equal(steps[0].status, 'running');
  applyStepEvent(steps, { kind: 'step_failed', step_id: 'a', ts: '2026-10-23T12:00:05Z', payload: { error: 'boom' } });
  assert.deepEqual([steps[0].status, steps[0].error, steps[0].finished_at], ['failed', 'boom', '2026-10-23T12:00:05Z']);
  applyStepEvent(steps, { kind: 'retry', step_id: 'a', ts: '2026-10-23T12:00:06Z' });
  assert.equal(steps[0].status, 'running');
  applyStepEvent(steps, { kind: 'step_end', step_id: 'a', ts: '2026-10-23T12:00:09Z' });
  assert.equal(steps[0].status, 'succeeded');
  assert.equal(steps[0].started_at, '2026-10-23T12:00:00Z');
  applyStepEvent(steps, { kind: 'step_skipped', step_id: 'b', ts: '2026-10-23T12:00:09Z' });
  assert.equal(steps.length, 2);
});

test('every status maps to a distinct tone and keeps its word', () => {
  const statuses = ['queued', 'running', 'succeeded', 'failed', 'canceled', 'lost'];
  const tones = statuses.map(statusTone);
  assert.deepEqual(tones, ['queued', 'running', 'ok', 'bad', 'unknown', 'warn']);
  assert.equal(new Set(tones).size, statuses.length);
  assert.equal(statusTone('something-new'), 'unknown');
  assert.deepEqual(statuses.map(statusText), ['Queued', 'Running', 'Succeeded', 'Failed', 'Canceled', 'Lost']);
});

test('elapsedSeconds ticks while running and stops at the end', () => {
  const start = '2026-10-23T12:00:00Z';
  assert.equal(elapsedSeconds(start, '2026-10-23T12:01:30Z'), 90);
  assert.equal(elapsedSeconds(start, null, Date.parse('2026-10-23T12:00:07Z')), 7);
  assert.equal(elapsedSeconds(null, null), null);
  assert.equal(elapsedSeconds('nonsense', null), null);
  assert.equal(elapsedSeconds(start, '2026-10-23T11:00:00Z'), 0);
});

test('duration formats seconds, minutes, and hours', () => {
  assert.equal(duration(null), '—');
  assert.equal(duration(0), '—');
  assert.equal(duration(42.4), '42s');
  assert.equal(duration(90), '1m 30s');
  assert.equal(duration(3725), '1h 2m');
});

test('describe covers each kind and marks errors and truncation', () => {
  const line = (kind, payload) => describe({ kind, payload });
  assert.equal(line('step_failed', { error: 'bad\nthing' }).tone, 'bad');
  assert.equal(line('step_failed', { error: 'bad\nthing' }).text, 'Failed: bad thing');
  assert.equal(line('error', { message: 'x' }).tone, 'bad');
  assert.equal(line('run_end', { status: 'failed', cost_usd: 1.5 }).text, 'Ended: Failed · $1.50');
  assert.equal(line('run_end', { status: 'succeeded' }).tone, 'ok');
  assert.equal(line('cost', { model: 'opus', input_tokens: 1200, output_tokens: 30, cost_usd: 0.0123 }).text, 'opus · 1,200 in, 30 out · $0.0123');
  assert.equal(line('cost', { model: 'opus', usage: { input_tokens: 5, output_tokens: 6 }, total_cost_usd: 0.5 }).text, 'opus · 5 in, 6 out · $0.5000');
  assert.equal(line('finding', { severity: 'high', title: 'Leak', path: 'a.py', line: 3 }).text, 'Leak at a.py:3');
  assert.equal(line('retry', { attempt: 2 }).text, 'Attempt 2');
  assert.equal(line('log', { message: 'hi', truncated: true }).text, 'hi (truncated)');
  const hidden = (kind, payload) => describe({ kind, payload }, { showCost: false });
  assert.equal(hidden('run_end', { status: 'failed', cost_usd: 1.5 }).text, 'Ended: Failed');
  assert.deepEqual(hidden('cost', { model: 'opus', input_tokens: 1200, output_tokens: 30, cost_usd: 0.0123 }), { tone: 'muted', label: 'Usage', text: 'opus · 1,200 in, 30 out' });
  const unknown = line('brand_new', { a: '<b>' });
  assert.deepEqual([unknown.label, unknown.text], ['brand_new', '{"a":"<b>"}']);
});

test('stepTree nests children under their parent and survives bad parents', () => {
  const steps = [
    { step_id: 'child-b', parent_step: 'review' }, { step_id: 'review' }, { step_id: 'grandchild', parent_step: 'child-b' },
    { step_id: 'orphan', parent_step: 'gone' }, { step_id: 'self', parent_step: 'self' }, { step_id: 'x', parent_step: 'y' }, { step_id: 'y', parent_step: 'x' },
  ];
  const tree = stepTree(steps);
  assert.deepEqual(tree.map(({ key, depth, parent }) => [key, depth, parent]), [
    ['review', 0, null], ['review/child-b', 1, 'review'], ['child-b/grandchild', 2, 'review/child-b'], ['gone/orphan', 0, null], ['self/self', 0, null], ['y/x', 0, null], ['x/y', 0, null],
  ]);
  assert.deepEqual([...descendants(steps, 'review')].sort(), ['child-b/grandchild', 'review', 'review/child-b']);
  assert.deepEqual([...descendants(steps, 'gone/orphan')], ['gone/orphan']);
});

test('branch events count toward a running step without ending it', () => {
  const steps = [];
  applyStepEvent(steps, { kind: 'step_start', step_id: 'fan', ts: 't0', payload: { branches: ['a', 'b', 'c'] } });
  assert.equal(branchText(steps[0]), '0/3 branches done');
  applyStepEvent(steps, { kind: 'step_start', step_id: 'fan', ts: 't1', payload: { branch: 'a' } });
  applyStepEvent(steps, { kind: 'step_end', step_id: 'fan', ts: 't2', payload: { branch: 'a' } });
  applyStepEvent(steps, { kind: 'step_end', step_id: 'fan', ts: 't2', payload: { branch: 'a' } });
  applyStepEvent(steps, { kind: 'step_failed', step_id: 'fan', ts: 't3', payload: { branch: 'b', error: 'x' } });
  assert.equal(steps[0].status, 'running');
  assert.equal(branchText(steps[0]), '2/3 branches done');
  applyStepEvent(steps, { kind: 'step_end', step_id: 'fan', ts: 't4' });
  assert.equal(steps[0].status, 'succeeded');
  assert.equal(branchText(steps[0]), '');
  applyStepEvent(steps, { kind: 'step_start', step_id: 'n', ts: 't5', payload: { branches: 9, parent_step: 'fan' } });
  applyStepEvent(steps, { kind: 'step_end', step_id: 'n', ts: 't6', payload: { branch: 0, parent_step: 'fan' } });
  assert.equal(branchText(steps[1]), '1/9 branches done');
  assert.equal(steps[1].parent_step, 'fan');
  assert.equal(branchText({ status: 'running', branches_done: ['a'] }), '1 branches done');
});

test('the newest page and prev_before decide whether earlier events exist', () => {
  nextSeq = 100;
  assert.equal(newestBefore(250), 251);
  assert.equal(newestBefore(0), 1);
  const model = new FeedModel({ cap: 10 });
  const page = [event('log'), event('log'), event('log')];
  model.append(page);
  model.noteStart({ prev_before: 101 }, page);
  assert.ok(model.hasEarlier);
  assert.equal(model.firstSeq, 101);
  model.noteStart({ prev_before: null }, page);
  assert.equal(model.startSeq, 101);
  assert.equal(model.hasEarlier, false);
  model.append(Array.from({ length: 10 }, () => event('log')));
  assert.ok(model.hasEarlier, 'trimming the front brings earlier events back');
  model.noteStart({}, []);
  assert.equal(model.startSeq, 101);
  model.reset();
  assert.equal(model.startSeq, null);
  const empty = new FeedModel();
  empty.noteStart({ prev_before: null }, []);
  assert.equal(empty.hasEarlier, false);
});

test('step keys tell apart children with the same id under different parents', () => {
  assert.equal(stepKey({ step_id: 'a' }), 'a');
  assert.equal(stepKey({ step_id: 'a', parent_step: 'p' }), 'p/a');
  assert.equal(stepKey({ step_id: 'a', parent_step: 'p', step_key: 'server-key' }), 'server-key');
  assert.equal(eventStepKey({ step_id: 'lint', payload: { parent_step: 'ios' } }), 'ios/lint');
  assert.equal(eventStepKey({ step_id: 'lint', parent_step: 'android' }), 'android/lint');
  assert.equal(eventStepKey({ step_id: 'lint' }), 'lint');
  assert.equal(eventStepKey({ step_id: null }), null);
  const steps = [{ step_id: 'ios', step_key: 'ios' }, { step_id: 'android', step_key: 'android' },
    { step_id: 'lint', parent_step: 'ios', step_key: 'ios/lint' }, { step_id: 'lint', parent_step: 'android', step_key: 'android/lint' }];
  applyStepEvent(steps, { kind: 'step_failed', step_id: 'lint', ts: 't', payload: { parent_step: 'android', error: 'x' } });
  assert.deepEqual(steps.map((step) => step.status ?? null), [null, null, null, 'failed']);
  applyStepEvent(steps, { kind: 'step_start', step_id: 'lint', ts: 't', payload: { parent_step: 'web' } });
  assert.equal(steps.at(-1).step_key, 'web/lint');
  assert.deepEqual(stepTree(steps).map(({ key, parent }) => [key, parent]), [['ios', null], ['ios/lint', 'ios'], ['android', null], ['android/lint', 'android'], ['web/lint', null]]);
  assert.deepEqual([...descendants(steps, 'ios')], ['ios', 'ios/lint']);
  const model = new FeedModel();
  nextSeq = 0;
  model.append([event('progress', {}, 'lint'), { ...event('progress', {}, 'lint'), payload: { parent_step: 'ios' } }]);
  assert.deepEqual(groupItems(model.items).map((group) => group.step_key), ['lint', 'ios/lint']);
});
