import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { groupByName } = await import(new URL('views/workflows.js', STATIC));

const names = new Map([['t1', 'Zeta'], ['t2', 'Alpha']]);

test('copies of one name share a group, in first-seen name order', () => {
  const groups = groupByName([
    { id: 1, name: 'review', scope_kind: 'team', team_id: 't1' },
    { id: 2, name: 'fix', scope_kind: 'team', team_id: 't1' },
    { id: 3, name: 'review', scope_kind: 'team', team_id: 't2' },
  ], names);
  assert.deepEqual(groups.map((group) => group.name), ['review', 'fix']);
  assert.deepEqual(groups.map((group) => group.copies.map((copy) => copy.id)), [[3, 1], [2]]);
});

test('the instance copy comes first, then copies by scope name', () => {
  const groups = groupByName([
    { id: 1, name: 'review', scope_kind: 'project', scope_name: 'Zeta / web' },
    { id: 2, name: 'review', scope_kind: 'project', scope_name: 'Alpha / api' },
    { id: 3, name: 'review', scope_kind: 'instance', team_id: null },
  ], names);
  assert.deepEqual(groups[0].copies.map((copy) => copy.id), [3, 2, 1]);
});

test('no workflows means no groups', () => {
  assert.deepEqual(groupByName([]), []);
});

test('the newest updated time wins', async () => {
  const { latest } = await import(new URL('views/workflows.js', STATIC));
  assert.equal(latest([{ updated_at: '2026-01-01T00:00:00Z' }, { updated_at: '2026-03-01T00:00:00Z' }]), '2026-03-01T00:00:00Z');
  assert.equal(latest([{ updated_at: 'not a date' }, {}, { updated_at: '2026-02-01T00:00:00Z' }, { updated_at: null }]), '2026-02-01T00:00:00Z');
  assert.equal(latest([{}, { updated_at: 'nope' }]), null);
  assert.equal(latest([]), null);
});
