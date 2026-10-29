import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { canCancel } = await import(new URL('runpage.js', STATIC));

const run = { team_id: 't1', project_id: 'p1' };
const member = (scope_kind, scope_id, role) => ({ user: { is_admin: false }, memberships: [{ scope_kind, scope_id, role }] });

test('canCancel offers Cancel to contributors and up on the run\'s team or project', () => {
  const job = { status: 'running', cancel_requested_at: null };
  assert.equal(canCancel(job, run, member('team', 't1', 'contributor')), true);
  assert.equal(canCancel(job, run, member('project', 'p1', 'project_lead')), true);
  assert.equal(canCancel(job, run, member('team', 't1', 'viewer')), false);
  assert.equal(canCancel(job, run, member('team', 't2', 'team_lead')), false);
  assert.equal(canCancel(job, run, { user: { is_admin: true }, memberships: [] }), true);
  assert.equal(canCancel(job, run, null), false);
});

test('canCancel needs a job that is still waiting or running and not already asked to stop', () => {
  const admin = { user: { is_admin: true } };
  assert.equal(canCancel(null, run, admin), false);
  assert.equal(canCancel({ status: 'queued' }, run, admin), true);
  assert.equal(canCancel({ status: 'succeeded' }, run, admin), false);
  assert.equal(canCancel({ status: 'running', cancel_requested_at: '2026-10-28T00:00:00Z' }, run, admin), false);
});
