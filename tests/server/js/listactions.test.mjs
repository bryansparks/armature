import assert from 'node:assert/strict';
import { test } from 'node:test';

// Just enough DOM for list cells and a button to build.
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
    this.listeners = {};
    this.className = '';
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  addEventListener(name, handler) {
    this.listeners[name] = handler;
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  replaceChildren(...nodes) {
    this.children = nodes;
  }
}
globalThis.Node = FakeNode;
globalThis.document = { createElement: (tag) => new FakeElement(tag), createTextNode: (text) => new FakeText(text) };
globalThis.location = { hash: '' };

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { cancelControl } = await import(new URL('views/runs.js', STATIC));
const { subjectCells, subjectHeaders } = await import(new URL('views/subject.js', STATIC));

const texts = (node) => (node instanceof FakeText ? [node.text] : node instanceof FakeElement ? node.children.flatMap(texts) : node == null ? [] : [String(node)]);
const lead = { user: { is_admin: false }, memberships: [{ role: 'team_lead', scope_kind: 'team', scope_id: 't1' }] };
const viewer = { user: { is_admin: false }, memberships: [{ role: 'viewer', scope_kind: 'team', scope_id: 't1' }] };
const open = { id: 'r1', status: 'running', team_id: 't1', job: { id: 'j1', status: 'running', cancel_requested_at: null } };

test('an open run in a list offers Cancel to those who may cancel it', () => {
  const control = cancelControl(open, lead);
  const [button] = control.children;
  assert.equal(button.tag, 'button');
  assert.deepEqual(texts(button), ['Cancel']);
  assert.equal(typeof button.listeners.click, 'function');
  assert.equal(cancelControl(open, viewer), null);
  assert.equal(cancelControl({ ...open, status: 'succeeded' }, lead), null);
  assert.equal(cancelControl({ ...open, job: null }, lead), null);
});

test('a run whose cancel was asked for says so instead of offering it again', () => {
  const asked = { ...open, job: { ...open.job, cancel_requested_at: '2026-10-30T20:00:00Z' } };
  assert.deepEqual(texts(cancelControl(asked, lead)), ['Cancel requested']);
  assert.equal(cancelControl({ ...asked, status: 'canceled' }, lead), null);
});

test('the pull request list shows the author and where the latest run executed', () => {
  assert.deepEqual(subjectHeaders(false), ['Title', 'Author', 'Status', 'Work', 'Environment', ['Last activity', 'nowrap']]);
  const row = { subject: 'acme/app#12', title: 'Fix it', author: 'octocat', status: 'running', runs: 1, reviews: 1, fixes: 0,
    location: { kind: 'aws-linux', label: 'AWS Linux' }, last_activity: null };
  const cells = subjectCells(row, false);
  assert.equal(cells[1], 'octocat');
  assert.deepEqual(texts(cells[4]), ['AWS Linux']);
  assert.equal(subjectCells({ ...row, author: null, location: null }, false)[1], '—');
  assert.deepEqual(texts(subjectCells({ ...row, location: null }, false)[4]), ['Unknown']);
});
