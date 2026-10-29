import assert from 'node:assert/strict';
import { test } from 'node:test';

// Just enough DOM for a chip to build.
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

  append(...nodes) {
    this.children.push(...nodes);
  }
}
globalThis.Node = FakeNode;
globalThis.document = { createElement: (tag) => new FakeElement(tag), createTextNode: (text) => new FakeText(text) };

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { locationChip, locationText, runHeaders } = await import(new URL('views/runs.js', STATIC));

test('locationText shows the server label and Unknown without one', () => {
  assert.equal(locationText({ kind: 'aws-linux', label: 'AWS Linux' }), 'AWS Linux');
  assert.equal(locationText({ kind: 'local', label: 'M1 (local)' }), 'M1 (local)');
  for (const missing of [null, undefined, {}, { label: '' }, { label: 7 }]) assert.equal(locationText(missing), 'Unknown');
});

test('locationChip names where the run executed and puts the instance in its title', () => {
  const chip = locationChip({ kind: 'aws-mac', label: 'AWS macOS', environment_type: 'ios', instance: 'i-0mac', instance_type: 'mac2.metal' });
  assert.equal(chip.className, 'chip');
  assert.deepEqual(chip.children.map((child) => child.text), ['AWS macOS']);
  assert.equal(chip.attributes.title, 'ios · i-0mac · mac2.metal');
  const unknown = locationChip(null);
  assert.deepEqual(unknown.children.map((child) => child.text), ['Unknown']);
  assert.equal(unknown.attributes.title, undefined);
});

test('runHeaders has a Where column after Outcome', () => {
  const names = runHeaders({}).map((header) => (Array.isArray(header) ? header[0] : header));
  assert.equal(names[names.indexOf('Outcome') + 1], 'Where');
});
