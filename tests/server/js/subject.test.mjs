import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { currentRun, dispositionLabel, joinsPage, nextRedraw, outcomeText, statusCapsuleTone, statusLabel, subjectPageHref, subjectPath, totals, workText } =
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

test('currentRun leads with a run in progress, else the newest', () => {
  assert.equal(currentRun([{ id: 1, status: 'succeeded' }, { id: 2, status: 'running' }, { id: 3, status: 'failed' }]).id, 2);
  assert.equal(currentRun([{ id: 1, status: 'succeeded' }, { id: 2, status: 'failed' }]).id, 2);
  assert.equal(currentRun([{ id: 1, status: 'queued' }, { id: 2, status: 'running' }]).id, 2);
  assert.equal(currentRun([]), null);
});

test('outcomeText summarizes what a fix or merge reported', () => {
  assert.equal(outcomeText({ kind: 'fix', fix: { outcome: 'build-failed', applied: 1, skipped: 0, requester: 'dev' } }), 'build failed · 1 applied · asked by dev');
  assert.equal(outcomeText({ kind: 'fix', status: 'lost', fix: {} }), 'Lost');
  assert.equal(outcomeText({ kind: 'merge', merge: { merged_by: 'dev', issue: 'P-1', transitioned: true, moved_to: 'Done' } }), 'Merged by dev · P-1 moved to Done');
  assert.equal(outcomeText({ kind: 'merge', merge: null }), 'Merged');
  assert.equal(outcomeText({ kind: 'work', verdict: 'opened', fields: { platform: 'ios' } }), 'opened · platform ios');
});

test('nextRedraw redraws at once, then backs off, then stops until things go quiet', () => {
  let state = { count: 0, last: 0 };
  const delays = [];
  for (let now = 1000; ; now += 10) {
    const next = nextRedraw(state, now);
    if (!next) break;
    delays.push(next.delay);
    state = next;
  }
  assert.deepEqual(delays, [0, 2000, 4000, 8000, 16000]);
  assert.equal(nextRedraw(state, state.last + 61000).delay, 0, 'a quiet minute starts over');
});

test('joinsPage takes a run whose subject or thread is the page', () => {
  assert.equal(joinsPage({ subject: 'acme/app#2', thread: 'FS-1' }, 'FS-1'), true);
  assert.equal(joinsPage({ subject: 'acme/app#2', thread: 'FS-1' }, 'acme/app#2'), true);
  assert.equal(joinsPage({ subject: 'acme/app#3', thread: null }, 'acme/app#2'), false);
  assert.equal(joinsPage({ subject: 'acme/app#3', thread: 'FS-2' }, 'FS-1'), false);
});

