import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { capsules, chosenWorkflows, hashParams, isHidden, isLive, listParams, liveElapsed, liveRuns, matches, prTitle, repoOf, runHeaders, runStatus,
  subjectHref, triggerOf, verdictText, verdictTone, workflowOptions } =
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

test('repoOf splits a pull request subject and rejects anything else', () => {
  assert.deepEqual(repoOf('fs-eng/android-tree#1818'), { repo: 'fs-eng/android-tree', number: '1818' });
  assert.equal(repoOf('daily-review'), null);
  assert.equal(repoOf('PROJ-7'), null);
  assert.equal(repoOf('acme/app#0'), null);
  assert.equal(repoOf('acme app/x#1'), null);
  assert.equal(repoOf(null), null);
});

test('verdictText maps review verdicts and tidies the rest', () => {
  for (const verdict of ['approve', 'Approved', 'Approval Recommended']) assert.equal(verdictText(verdict), 'Approved');
  for (const verdict of ['comment', 'changes', 'REQUEST_CHANGES', 'changes_requested', 'Changes Recommended']) {
    assert.equal(verdictText(verdict), 'Changes Recommended');
  }
  assert.equal(verdictText('No changes'), 'No changes');
  assert.equal(verdictText('ok'), 'Ok');
  assert.equal(verdictText('push_failed'), 'Push failed');
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
    [{ status: 'succeeded', outcome: 'declined' }, null, 'Declined', 'ok'],
    [{ status: 'succeeded', outcome: 'push_failed' }, null, 'Push failed', 'bad'],
    [{ status: 'succeeded' }, null, 'Done', 'ok'],
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
  assert.equal(String(params), 'limit=50&team=t&subject=MEM-1&workflows=a%2Cb&include_hidden=1&before=cursor');
  assert.equal(String(listParams({ workflows: [] })), 'limit=50');
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
  assert.deepEqual(runHeaders({ show_cost: true }).at(-1), ['Cost', 'num']);
  assert.deepEqual(runHeaders({ show_cost: false }).at(-1), ['Time', 'num']);
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

test('live runs are running or queued, pass the filter, and ignore the status filter', () => {
  const base = { team: '', project: '', status: 'failed', subject: '', workflows: [], hidden: false };
  assert.equal(isLive({ status: 'running', workflow: 'code-review' }, base, display), true);
  assert.equal(isLive({ status: 'queued', workflow: 'code-review' }, base, display), true);
  assert.equal(isLive({ status: 'succeeded', workflow: 'code-review' }, base, display), false);
  assert.equal(isLive({ status: 'running', workflow: 'digest' }, base, display), false);
  assert.equal(isLive({ status: 'running', workflow: 'jira-work' }, { ...base, workflows: ['code-review'] }, display), false);
  assert.equal(isLive({ status: 'running', workflow: 'code-review', triggered_by: 'bot' }, base, { ...display, hidden_actors: ['bot'] }), false);
  const runs = [
    { id: 1, status: 'running', workflow: 'code-review', started_at: '2026-10-01T00:00:00Z' },
    { id: 2, status: 'queued', workflow: 'code-review', started_at: '2026-10-02T00:00:00Z' },
    { id: 3, status: 'failed', workflow: 'code-review', started_at: '2026-10-03T00:00:00Z' },
    { id: 1, status: 'running', workflow: 'code-review', started_at: '2026-10-01T00:00:00Z' },
  ];
  assert.deepEqual(liveRuns(runs, base, display).map((run) => run.id), [2, 1]);
});

test('liveElapsed ticks from the start and waits without one', () => {
  assert.equal(liveElapsed({ started_at: '2026-10-01T00:00:00Z' }, Date.parse('2026-10-01T00:04:05Z')), '4m 5s');
  assert.equal(liveElapsed({ started_at: null }), 'Waiting');
});
