import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { activityRuns, dispositionLabel, outcomeText, statusCapsuleTone, statusLabel, subjectPageHref, subjectPath, totals, workText } =
  await import(new URL('views/subject.js', STATIC));
const { parseHash } = await import(new URL('router.js', STATIC));

test('subjectPageHref keeps a subject with # and / in one route segment', () => {
  const href = subjectPageHref('group/sub/repo#5', 'findings');
  assert.equal(href, '#/subject/group%2Fsub%2Frepo%235/findings');
  assert.deepEqual(parseHash(href).args, ['group/sub/repo#5', 'findings']);
  assert.deepEqual(parseHash(subjectPageHref('PROJ-7')).args, ['PROJ-7']);
});

test('subjectPath keeps slashes literal and encodes the rest of each segment', () => {
  assert.equal(subjectPath('fs-eng/android-tree#1870'), '/api/subjects/fs-eng/android-tree%231870');
  assert.equal(subjectPath('PROJ-7'), '/api/subjects/PROJ-7');
});

test('status capsules read merged, verdicts, and run statuses', () => {
  assert.deepEqual([statusLabel('merged'), statusCapsuleTone('merged')], ['Merged', 'ok']);
  assert.deepEqual([statusLabel('Changes Recommended'), statusCapsuleTone('Changes Recommended')], ['Changes Recommended', 'bad']);
  assert.deepEqual([statusLabel('Approval Recommended'), statusCapsuleTone('Approval Recommended')], ['Approval Recommended', 'ok']);
  assert.deepEqual([statusLabel('running'), statusCapsuleTone('running')], ['Running', 'running']);
  assert.deepEqual([statusLabel('comment'), statusCapsuleTone('comment')], ['comment', 'unknown']);
  assert.equal(statusLabel(null), 'Unknown');
});

test('dispositionLabel names each disposition and a bare finding as raised', () => {
  assert.equal(dispositionLabel('drop_rebutted'), 'Rebutted');
  assert.equal(dispositionLabel('resolved'), 'Resolved');
  assert.equal(dispositionLabel(null), 'Raised');
  assert.equal(dispositionLabel('new_kind'), 'new kind');
});

test('totals add the page and keep cost unknown when every row hides it', () => {
  const rows = [{ runs: 4, reviews: 2, fixes: 1, cost_usd: 1.5 }, { runs: 1, reviews: 0, fixes: 0, cost_usd: null }];
  assert.deepEqual(totals(rows), { subjects: 2, runs: 5, reviews: 2, fixes: 1, cost: 1.5 });
  assert.equal(totals([{ runs: 1, cost_usd: null }]).cost, null);
  assert.equal(workText({ runs: 4, reviews: 2, fixes: 1 }), '2 reviews, 1 fix, 1 other run');
  assert.equal(workText({ runs: 1, reviews: 1, fixes: 0 }), '1 review, 0 fixes');
});

test('activityRuns keeps fixes, merges, ports, and work in order', () => {
  const runs = [{ id: 1, kind: 'review' }, { id: 2, kind: 'fix' }, { id: 3, kind: 'merge' }, { id: 4, kind: 'other' }, { id: 5, kind: 'port' }];
  assert.deepEqual(activityRuns(runs).map((run) => run.id), [2, 3, 5]);
});

test('outcomeText summarizes what a fix or merge reported', () => {
  assert.equal(outcomeText({ kind: 'fix', fix: { outcome: 'build-failed', applied: 1, skipped: 0, requester: 'dev' } }), 'build failed · 1 applied · asked by dev');
  assert.equal(outcomeText({ kind: 'fix', status: 'lost', fix: {} }), 'Lost');
  assert.equal(outcomeText({ kind: 'merge', merge: { merged_by: 'dev', issue: 'P-1', transitioned: true, moved_to: 'Done' } }), 'Merged by dev · P-1 moved to Done');
  assert.equal(outcomeText({ kind: 'merge', merge: null }), 'Merged');
  assert.equal(outcomeText({ kind: 'work', verdict: 'opened', fields: { platform: 'ios' } }), 'opened · platform ios');
});
