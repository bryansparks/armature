import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { SYNC_LABELS, repoBody, repoDeletePath, repoPath, reposPath, syncText } = await import(new URL('views/repos.js', STATIC));

test('repo paths encode the team and repo ids', () => {
  assert.equal(reposPath('team 1'), '/api/teams/team%201/repos');
  assert.equal(repoPath('t', 'r/1'), '/api/teams/t/repos/r%2F1');
  assert.equal(repoDeletePath('t', 'r'), '/api/teams/t/repos/r/delete');
});

test('syncText shows the next run, or the last error', () => {
  assert.deepEqual(Object.keys(SYNC_LABELS), ['backfill', 'poll', 'conflicts', 'gap_fill']);
  assert.equal(syncText({ kind: 'poll', next_run_at: null }), 'Poll: next run —');
  assert.equal(syncText({ kind: 'gap_fill', last_error: 'GitHub answered HTTP 502.' }), 'Gap fill: failed: GitHub answered HTTP 502.');
});

test('repoBody leaves blank optional fields out', () => {
  assert.deepEqual(repoBody({ full_name: ' Acme/App ', connection_id: '', backfill_since: '' }), { full_name: 'Acme/App' });
  assert.deepEqual(repoBody({ full_name: 'a/b', connection_id: 'c', backfill_since: '2025-05-01' }), { full_name: 'a/b', connection_id: 'c', backfill_since: '2025-05-01' });
});
