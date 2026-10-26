import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { labelEvery, lineSegments, niceMax, scale, shortDay, stackTotals, ticks } = await import(new URL('charts.js', STATIC));
const { workflowColor, workflowLabel } = await import(new URL('display.js', STATIC));
const { largest } = await import(new URL('views/trends.js', STATIC));

test('niceMax rounds up to 1, 2, 2.5, or 5 times a power of ten', () => {
  assert.equal(niceMax(0), 1);
  assert.equal(niceMax(-3), 1);
  assert.equal(niceMax(0.07), 0.1);
  assert.equal(niceMax(1), 1);
  assert.equal(niceMax(1.2), 2);
  assert.equal(niceMax(23), 25);
  assert.equal(niceMax(41), 50);
  assert.equal(niceMax(501), 1000);
});

test('ticks split the axis evenly from zero', () => {
  assert.deepEqual(ticks(25), [0, 6.25, 12.5, 18.75, 25]);
  assert.deepEqual(ticks(1, 2), [0, 0.5, 1]);
});

test('scale maps zero to the bottom and the maximum to the top', () => {
  const y = scale(50, 200);
  assert.equal(y(0), 200);
  assert.equal(y(50), 0);
  assert.equal(y(25), 100);
  assert.equal(y(-5), 200);
  assert.equal(y(null), 200);
});

test('shortDay drops the year and leading zeros', () => {
  assert.equal(shortDay('2026-10-03'), '10/3');
});

test('labelEvery keeps day labels at least a label apart', () => {
  assert.equal(labelEvery(7, 600), 1);
  assert.equal(labelEvery(30, 300), 5);
  assert.equal(labelEvery(90, 300), 15);
  assert.equal(labelEvery(0, 300), 1);
});

test('stackTotals adds every series per day', () => {
  assert.deepEqual(stackTotals(3, [{ values: [1, 2, null] }, { values: [0.5, 0, 4] }]), [1.5, 2, 4]);
});

test('lineSegments break at missing days', () => {
  const segments = lineSegments([1, null, 2, 3, undefined], (index) => index, (value) => value * 10);
  assert.deepEqual(segments, [[[0, 10]], [[2, 20], [3, 30]]]);
});

test('workflow labels and colors follow the settings, then the order', () => {
  const display = { workflow_labels: { 'code-review': { label: 'Review', color: 's5' } } };
  assert.equal(workflowLabel(display, 'code-review'), 'Review');
  assert.equal(workflowLabel(display, 'jira-work'), 'jira-work');
  assert.equal(workflowLabel(display, 'other'), 'Other');
  assert.equal(workflowColor(display, 'code-review', ['jira-work', 'code-review']), 's5');
  assert.equal(workflowColor(display, 'jira-work', ['jira-work', 'code-review']), 's1');
  assert.equal(workflowColor(display, 'missing', ['jira-work']), 'other');
  assert.equal(workflowColor({ workflow_labels: { constructor: 'x' } }, 'toString', []), 'other');
});

test('largest picks the series with the biggest total', () => {
  assert.deepEqual(largest({ a: [1, 2], b: [5, 0], c: [0, 0] }), { key: 'b', total: 5 });
  assert.equal(largest({ a: [0] }), null);
});
