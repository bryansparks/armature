import assert from 'node:assert/strict';
import { test } from 'node:test';

// Just enough DOM for the thread page's sections to build.
class FakeNode {}
class FakeText extends FakeNode {
  constructor(text) {
    super();
    this.text = text;
  }
}
class FakeElement extends FakeNode {
  constructor(tag) {
    super();
    this.tag = tag;
    this.attributes = {};
    this.children = [];
    this.className = '';
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  addEventListener() {}

  contains() {
    return false;
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  replaceChildren(...nodes) {
    this.children = nodes;
  }
}
globalThis.Node = FakeNode;
globalThis.document = { createElement: (tag) => new FakeElement(tag), createTextNode: (text) => new FakeText(text), activeElement: null };

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { latestReview, threadParts } = await import(new URL('views/subject.js', STATIC));
const { append } = await import(new URL('dom.js', STATIC));

const texts = (node) => (node instanceof FakeText ? [node.text] : (node.children || []).flatMap(texts));
const run = (id, kind, status = 'succeeded', verdict = null) => ({ id, kind, status, verdict, workflow: kind, started_at: `2026-10-27T1${id}:00:00Z`, artifacts: [] });
const detail = (runs, findings = []) => ({ subject: 'acme/app#2', title: 'Fix it', status: 'merged', latest_verdict: null, links: [], runs, findings });
const signal = new AbortController().signal;

test('a thread page with no earlier runs, findings, or links draws no "null"', () => {
  const page = append(new FakeElement('div'), threadParts(detail([run(1, 'review', 'succeeded', 'Approved')]), {}, signal, () => {}));
  assert.ok(!texts(page).includes('null'), texts(page).join('|'));
  assert.ok(!texts(page).includes('Latest review'));
});

test('latestReview finds the newest review when a later run is something else', () => {
  const runs = [run(1, 'review', 'succeeded', 'Changes Recommended'), run(2, 'fix'), run(3, 'review', 'succeeded', 'Approved'), run(4, 'merge')];
  assert.equal(latestReview(runs, runs[3]).id, 3);
  assert.equal(latestReview(runs.slice(0, 3), runs[2]), null, 'a review that leads the page needs no second section');
  assert.equal(latestReview([run(1, 'merge')], run(1, 'merge')), null);
  assert.equal(latestReview([], null), null);
});

test('a merged pull request keeps its latest review open under the result', () => {
  const runs = [run(1, 'review', 'succeeded', 'Changes Recommended'), run(3, 'review', 'succeeded', 'Approved'), run(4, 'merge')];
  const parts = threadParts(detail(runs), {}, signal, () => {});
  const titles = parts.map((part) => texts(part).join(' '));
  const now = titles.findIndex((text) => text.startsWith('Latest result'));
  const review = titles.findIndex((text) => text.startsWith('Latest review'));
  assert.ok(now >= 0 && review === now + 1, titles.join('\n'));
  assert.equal(parts[review].tag, 'section');
});

test('a finding card shows one badge per specialist who raised it', async () => {
  const { findingCard } = await import(new URL('result.js', STATIC));
  const chips = (node) => [...(node.className === 'chip' && node.attributes?.title?.startsWith('Raised by') ? [texts(node).join('')] : []),
    ...(node.children || []).flatMap(chips)];
  const card = findingCard({ claim: 'Leak', severity: 'high', raised_by: ['security', ' correctness ', 'security', 7, ''] });
  assert.deepEqual(chips(card), ['Security', 'Correctness']);
  assert.deepEqual(chips(findingCard({ claim: 'Plain' })), []);
});
