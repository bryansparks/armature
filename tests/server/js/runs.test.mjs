import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { capsules, chosenWorkflows, groupText, hashParams, isHidden, isNewer, listParams, liveElapsed, matches, prTitle, runHeaders, runStatus, shortWhen,
  rethreaded, runRowHref, subjectHref, threadOf, triggerOf, verdictText, verdictTone, workflowOptions } =
  await import(new URL('views/runs.js', STATIC));
const { hashFor } = await import(new URL('router.js', STATIC));

const display = { workflow_labels: { 'code-review': { label: 'Code review', color: 's1' }, digest: { label: '', hidden: true } } };

test('prTitle prefers the pull request title, then the run title, then the workflow label', () => {
  assert.equal(prTitle({ workflow: 'pr-fix' }, { title: 'Fix the crash' }, display), 'Fix the crash');
  assert.equal(prTitle({ title: 'Own title', workflow: 'pr-fix' }, { title: null }, display), 'Own title');
  assert.equal(prTitle({ title: 'Own title', workflow: 'code-review' }, null, display), 'Own title');
  assert.equal(prTitle({ workflow: 'code-review' }, null, display), 'Code review');
  assert.equal(prTitle({}, null, {}), 'Unnamed workflow');
});

test('verdictText maps review verdicts and tidies the rest', () => {
  for (const verdict of ['approve', 'Approved', 'Approval Recommended']) assert.equal(verdictText(verdict), 'Approved');
  for (const verdict of ['comment', 'changes', 'REQUEST_CHANGES', 'changes_requested', 'Changes Recommended']) {
    assert.equal(verdictText(verdict), 'Changes Recommended');
  }
  assert.equal(verdictText('No changes'), 'No changes');
  assert.equal(verdictText('ok'), 'Ok');
  assert.equal(verdictText('push_failed'), 'Push failed');
  assert.equal(verdictText('opened'), 'PR Created');
  assert.equal(verdictText('review-blocked'), 'Review Blocked');
  assert.equal(verdictText('pushed'), 'Changes Pushed');
  assert.equal(verdictText('blocked'), 'Blocked');
  assert.equal(verdictText(null), '');
});

test('runStatus puts activity and failures before the pull request state', () => {
  const approved = { verdict: 'approve', merged: false };
  const cases = [
    [{ status: 'running' }, { verdict: 'approve', merged: true }, 'In Progress', 'running'],
    [{ status: 'queued' }, null, 'In Progress', 'running'],
    [{ status: 'failed' }, approved, 'Failed', 'bad'],
    [{ status: 'canceled' }, approved, 'Canceled', 'unknown'],
    [{ status: 'lost' }, approved, 'Lost', 'warn'],
    [{ status: 'succeeded', verdict: 'No changes' }, approved, 'Approved', 'ok'],
    [{ status: 'succeeded' }, { verdict: 'Approval Recommended', merged: true }, 'Approved - Merged', 'ok'],
    [{ status: 'succeeded' }, { verdict: 'comment', merged: true }, 'Changes Recommended - Merged', 'warn'],
    [{ status: 'succeeded' }, { verdict: 'comment', merged: false }, 'Changes Recommended', 'warn'],
    [{ status: 'succeeded' }, { verdict: null, merged: true }, 'Merged', 'ok'],
    [{ status: 'succeeded', verdict: 'No changes' }, { verdict: null, merged: false }, 'No changes', 'ok'],
    [{ status: 'succeeded', verdict: 'No changes' }, null, 'No changes', 'ok'],
    [{ status: 'succeeded', fix_outcome: 'declined' }, null, 'Declined', 'ok'],
    [{ status: 'succeeded', fix_outcome: 'push_failed' }, null, 'Push failed', 'bad'],
    [{ status: 'succeeded', verdict: 'No changes', fix_outcome: 'pushed' }, null, 'No changes', 'ok'],
    [{ status: 'succeeded' }, null, 'Done', 'ok'],
    [{ status: 'succeeded', verdict: 'comment', outcome: 'pushed' }, null, 'Changes Pushed', 'ok'],
    [{ status: 'succeeded', outcome: 'recommended' }, null, 'Changes Recommended', 'warn'],
    [{ status: 'succeeded', outcome: 'blocked' }, null, 'Blocked', 'bad'],
    [{ status: 'succeeded', outcome: 'noop' }, null, 'Nothing to Do', 'ok'],
    [{ status: 'succeeded', outcome: 'pr_opened' }, null, 'PR Created', 'ok'],
  ];
  for (const [run, pr, text, tone] of cases) assert.deepEqual(runStatus(run, pr), { text, tone }, JSON.stringify([run, pr]));
});

test('subjectHref links the subject page', () => {
  assert.equal(subjectHref('fs-eng/android-tree#12'), '#/subject/fs-eng%2Fandroid-tree%2312');
});

test('verdictTone reads approvals as ok and blocks as bad', () => {
  assert.equal(verdictTone('approve'), 'ok');
  assert.equal(verdictTone('Approval Recommended'), 'ok');
  assert.equal(verdictTone('request_changes'), 'bad');
  assert.equal(verdictTone('block'), 'bad');
  assert.equal(verdictTone('Not approved'), 'bad');
  assert.equal(verdictTone('Changes Recommended'), 'bad');
  assert.equal(verdictTone('push-failed'), 'bad');
  assert.equal(verdictTone('pushed'), 'ok');
  assert.equal(verdictTone('comment'), '');
  assert.equal(verdictTone(null), '');
});

test('workflow choices round-trip through the hash', () => {
  const filter = { team: 't', project: '', status: '', subject: '', workflows: ['a', 'b'], hidden: true };
  const hash = hashFor('runs', [], hashParams(filter));
  assert.equal(hash, '#/runs?team=t&workflows=a%2Cb&hidden=1');
  const query = new URLSearchParams(hash.split('?')[1]);
  assert.deepEqual(chosenWorkflows(query), ['a', 'b']);
  assert.deepEqual(chosenWorkflows(new URLSearchParams('workflows=a,,a, b')), ['a', 'b']);
  assert.deepEqual(chosenWorkflows(new URLSearchParams('')), []);
});

test('listParams asks the API for the chosen workflows and hidden ones', () => {
  const params = listParams({ team: 't', subject: 'MEM-1', workflows: ['a', 'b'], hidden: true }, 'cursor');
  assert.equal(String(params), 'limit=50&team=t&subject=MEM-1&workflows=a%2Cb&include_hidden=1&group=1&before=cursor');
  assert.equal(String(listParams({ workflows: [] })), 'limit=50&group=1');
  assert.equal(String(listParams({ workflows: [], every: true })), 'limit=50');
});

test('a thread filter reaches the API and the hash, and groups a pull request with its issue', () => {
  assert.equal(String(listParams({ thread: 'MEM-1', workflows: [], every: true })), 'limit=50&thread=MEM-1');
  assert.equal(hashFor('runs', [], hashParams({ thread: 'MEM-1', workflows: [], every: true })), '#/runs?thread=MEM-1&every=1');
  assert.equal(threadOf({ subject: 'acme/app#7', thread: 'MEM-1' }), 'MEM-1');
  assert.equal(threadOf({ subject: 'acme/app#7', thread: null }), 'acme/app#7');
  assert.equal(threadOf({}), '');
  assert.equal(rethreaded('acme/app#7', { subject: 'acme/app#7', thread: 'MEM-1' }), true);
  assert.equal(rethreaded('MEM-1', { subject: 'acme/app#7', thread: 'MEM-1' }), false);
  assert.equal(rethreaded(undefined, {}), false);
  const base = { workflows: [], hidden: true, thread: 'MEM-1' };
  assert.equal(matches({ workflow: 'x', subject: 'acme/app#7', thread: 'MEM-1' }, base, {}), true);
  assert.equal(matches({ workflow: 'x', subject: 'MEM-1', thread: 'MEM-1' }, base, {}), true);
  assert.equal(matches({ workflow: 'x', subject: 'acme/app#7', thread: null }, base, {}), false);
});

test('every run round-trips through the hash and only groups of several get a marker', () => {
  assert.equal(hashFor('runs', [], hashParams({ workflows: [], every: true })), '#/runs?every=1');
  assert.equal(hashFor('runs', [], hashParams({ workflows: [], every: false })), '#/runs');
  assert.equal(groupText(3), '3 runs');
  assert.equal(groupText(1), '');
  assert.equal(groupText(undefined), '');
});

test('isNewer only lets a later run replace the shown one', () => {
  const shown = { id: 'b', started_at: '2026-10-27T12:00:01Z' };
  assert.equal(isNewer({ id: 'a', started_at: '2026-10-27T12:00:02Z' }, shown), true);
  assert.equal(isNewer({ id: 'z', started_at: '2026-10-27T12:00:00Z' }, shown), false);
  assert.equal(isNewer({ id: 'c', started_at: shown.started_at }, shown), true);
  assert.equal(isNewer({ id: 'a', started_at: shown.started_at }, shown), false);
  assert.equal(isNewer({ id: 'b', started_at: shown.started_at }, shown), false);
  assert.equal(isNewer({ id: 'a', started_at: null }, shown), false);
  assert.equal(isNewer({ id: 'a', started_at: shown.started_at }, { id: 'b' }), true);
});

test('matches hides hidden workflows unless shown or chosen', () => {
  assert.equal(isHidden(display, 'digest'), true);
  assert.equal(isHidden({}, 'digest'), false);
  const base = { workflows: [], hidden: false };
  assert.equal(matches({ workflow: 'digest' }, base, display), false);
  assert.equal(matches({ workflow: 'digest' }, { ...base, hidden: true }, display), true);
  assert.equal(matches({ workflow: 'digest' }, { ...base, workflows: ['digest'] }, display), true);
  assert.equal(matches({ workflow: 'code-review' }, { ...base, workflows: ['digest'] }, display), false);
  assert.equal(matches({ workflow: 'x', subject: 'a' }, { ...base, subject: 'b' }, display), false);
  assert.equal(matches({ workflow: 'x', status: 'failed' }, { ...base, status: 'running' }, display), false);
});

test('matches drops hidden actors unless hidden runs are shown', () => {
  const hiding = { ...display, hidden_actors: ['smoke'] };
  const base = { workflows: [], hidden: false };
  assert.equal(matches({ workflow: 'code-review', triggered_by: 'smoke' }, base, hiding), false);
  assert.equal(matches({ workflow: 'code-review', triggered_by: 'smoke' }, { ...base, workflows: ['code-review'] }, hiding), false);
  assert.equal(matches({ workflow: 'code-review', triggered_by: 'smoke' }, { ...base, hidden: true }, hiding), true);
  assert.equal(matches({ workflow: 'code-review', triggered_by: 'dev' }, base, hiding), true);
  assert.equal(matches({ workflow: 'code-review', triggered_by: null }, base, hiding), true);
  assert.equal(matches({ workflow: 'code-review', triggered_by: 'smoke' }, base, display), true);
  assert.equal(matches({ workflow: 'digest' }, { ...base, hidden: true, workflows: ['code-review'] }, display), false);
});

test('workflowOptions merges configured and seen workflows by label', () => {
  assert.deepEqual(workflowOptions(display, ['jira-work', 'code-review', null]), [
    { id: 'code-review', name: 'Code review', hidden: false },
    { id: 'digest', name: 'digest', hidden: true },
    { id: 'jira-work', name: 'jira-work', hidden: false },
  ]);
});

test('runHeaders drops the Cost column when cost is hidden', () => {
  assert.deepEqual(runHeaders({ show_cost: true }).at(-1), ['Cost', 'num col-tight']);
  assert.deepEqual(runHeaders({ show_cost: false }).at(-1), ['Time', 'num col-tight']);
});

test('runHeaders names the Trigger column, not Triggered by', () => {
  assert.ok(runHeaders({}).includes('Trigger'));
  assert.ok(!runHeaders({}).includes('Triggered by'));
});

test('triggerOf reads the server trigger and falls back to the actor', () => {
  assert.deepEqual(triggerOf({ trigger: { label: 'repo hooks', detail: 'pull_request' }, triggered_by: 'runner' }), { label: 'repo hooks', detail: 'pull_request' });
  assert.deepEqual(triggerOf({ trigger: { label: 'Schedule', detail: null } }), { label: 'Schedule', detail: '' });
  assert.deepEqual(triggerOf({ trigger: null, triggered_by: 'ada' }), { label: 'ada', detail: '' });
  assert.deepEqual(triggerOf({}), { label: '—', detail: '' });
});

test('capsules list All and each visible workflow, pressing one only for a single choice', () => {
  const items = capsules(display, ['jira-work', 'code-review'], []);
  assert.deepEqual(items.map((item) => [item.id, item.name, item.pressed]), [['', 'All', true], ['code-review', 'Code review', false], ['jira-work', 'jira-work', false]]);
  assert.equal(items[1].color, 's1');
  assert.equal(items[2].color, 's2');
  assert.deepEqual(capsules(display, ['jira-work', 'code-review'], ['jira-work']).filter((item) => item.pressed).map((item) => item.id), ['jira-work']);
  assert.deepEqual(capsules(display, ['jira-work', 'code-review'], ['jira-work', 'code-review']).filter((item) => item.pressed), []);
  assert.ok(!capsules(display, ['digest'], []).some((item) => item.id === 'digest'));
});

test('liveElapsed ticks from the start and waits without one', () => {
  assert.equal(liveElapsed({ started_at: '2026-10-01T00:00:00Z' }, Date.parse('2026-10-01T00:04:05Z')), '4m 5s');
  assert.equal(liveElapsed({ started_at: null }), 'Waiting');
});

test('shortWhen keeps the start time to month/day and time', () => {
  const now = new Date(2026, 9, 27, 12, 0);
  assert.equal(shortWhen(new Date(2026, 9, 27, 9, 5).toISOString(), now), '10/27 9:05');
  assert.equal(shortWhen(new Date(2025, 11, 31, 23, 59).toISOString(), now), '12/31/25 23:59');
  assert.equal(shortWhen(null, now), '—');
});

test('runRowHref opens the thread when the run has one, else the run', () => {
  assert.equal(runRowHref({ id: 'r1', thread: 'FS-1', subject: 'acme/app#2' }), hashFor('subject', ['FS-1']));
  assert.equal(runRowHref({ id: 'r1', subject: 'acme/app#2' }), hashFor('subject', ['acme/app#2']));
  assert.equal(runRowHref({ id: 'r1' }), hashFor('run', ['r1']));
});
