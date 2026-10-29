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

const { canRerun, rerunNote, rerunPending } = await import(new URL('runpage.js', STATIC));

test('canRerun follows the server\'s can_rerun on any finished run, a refused-looking success included', () => {
  for (const status of ['succeeded', 'failed', 'canceled', 'lost']) assert.equal(canRerun({ status, can_rerun: true }), true);
  assert.equal(canRerun({ status: 'running', can_rerun: true }), false);
  assert.equal(canRerun({ status: 'failed', can_rerun: false }), false);
  assert.equal(canRerun({ status: 'failed' }), false);
});

test('rerunNote links the rerun once it has a run and says where its job stands before', () => {
  assert.equal(rerunNote(null), null);
  assert.deepEqual(rerunNote({ id: 'j1', status: 'running', run_id: 'r2' }), { text: 'Open the rerun', runId: 'r2' });
  assert.match(rerunNote({ id: 'j1', status: 'queued', run_id: null }).text, /queued/);
  assert.match(rerunNote({ id: 'j1', status: 'failed', run_id: null }).text, /ended failed/);
  assert.equal(rerunPending({ status: 'queued', run_id: null }), true);
  assert.equal(rerunPending({ status: 'running', run_id: 'r2' }), false);
  assert.equal(rerunPending({ status: 'failed', run_id: null }), false);
});

const { settledJob } = await import(new URL('runpage.js', STATIC));

test('settledJob waits for a held job to let go before the rerun answer is final', () => {
  assert.equal(settledJob(null), true);
  for (const status of ['queued', 'provisioning', 'claimed', 'running']) assert.equal(settledJob({ status }), false);
  for (const status of ['succeeded', 'failed', 'canceled', 'suspended']) assert.equal(settledJob({ status }), true);
});
