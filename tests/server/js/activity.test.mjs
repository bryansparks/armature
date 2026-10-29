import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const {
  backfill, compactWhere, currentStage, feedSlot, findingBadge, findingWhere, isBlocking, nowText,
  roleState, rosterLine, RunActivity, stageLabel, stageTrack, verdictTone,
} = await import(new URL('activity.js', STATIC));
const { describe } = await import(new URL('feed.js', STATIC));

let nextSeq = 0;
const at = (minute) => `2026-10-27T12:${String(minute).padStart(2, '0')}:00Z`;
const event = (kind, payload = {}, stepId = 'reviews', minute = 0) => ({ seq: ++nextSeq, ts: at(minute), kind, step_id: stepId, payload });
const NOW = new Date(at(10)).getTime();

function reviewRun() {
  const activity = new RunActivity();
  activity.apply([
    event('log', { role: 'security', state: 'started', message: 'started reviewing fs/app#7 (sonnet 5.5)' }, 'reviews', 1),
    event('log', { role: 'beauty', state: 'started', message: 'started reviewing fs/app#7 (sonnet 5.5)' }, 'reviews', 1),
    event('tool_call', { role: 'security', tool: 'Read', input: '{"file_path":"/w/App/FamilyTreeTabs.swift"}' }, 'reviews', 2),
    event('tool_call', { role: 'beauty', tool: 'Bash', input: '{"command":"git diff main..HEAD"}' }, 'reviews', 3),
    event('log', { role: 'security', state: 'finished', message: 'finished in 4m 0s: 2 findings', findings: 2, review: '## Finding 1' }, 'reviews', 5),
  ]);
  return activity;
}

const STEPS = [
  { step_id: 'resolve', status: 'succeeded', started_at: at(0), finished_at: at(1) },
  { step_id: 'reviews', status: 'running', started_at: at(1), finished_at: null, branch_total: 9 },
  { step_id: 'verify', status: 'running', started_at: at(1), finished_at: null },
  { step_id: 'team_lead', status: 'queued', started_at: null, finished_at: null },
];

test('roleState reads the state field, then the level, then the first word', () => {
  assert.equal(roleState({ state: 'finished', message: 'whatever' }), 'finished');
  assert.equal(roleState({ level: 'error', message: 'AWS authentication failed' }), 'failed');
  assert.equal(roleState({ message: 'Finished in 3m' }), 'finished');
  assert.equal(roleState({ message: 'started reviewing' }), 'started');
  assert.equal(roleState({ message: 'weighing 9 reviews' }), null);
});

test('nowText says what a role is doing in a few words', () => {
  assert.equal(nowText('tool_call', { tool: 'Read', input: '{"file_path":"/w/App/FamilyTreeTabs.swift"}' }), 'Read FamilyTreeTabs.swift');
  assert.equal(nowText('tool_call', { tool: 'Bash', input: '{"command":"git diff main"}' }), 'Bash: git diff main');
  assert.equal(nowText('tool_call', { tool: 'Glob' }), 'Glob');
  assert.equal(nowText('text', { text: 'Looking at\nthe view' }), 'thinking: Looking at the view');
  assert.ok(nowText('text', { text: 'x'.repeat(500) }).length <= 120);
  assert.equal(nowText('tool_result', { tool: 'Read' }), null);
  assert.equal(nowText('cost', {}), null);
});

test('RunActivity follows each role from start to its finish and keeps its review', () => {
  const activity = reviewRun();
  const [security, beauty] = activity.rolesOf('reviews');
  assert.deepEqual([security.role, security.state, security.findings, security.review, security.now], ['security', 'finished', 2, '## Finding 1', null]);
  assert.deepEqual([beauty.state, beauty.now], ['working', 'Bash: git diff main..HEAD']);
  assert.ok(activity.roleSteps.has('reviews'));
  assert.equal(activity.reportedFindings(), 2);
  assert.equal(activity.apply([event('tool_call', { role: 'security', tool: 'Read', input: '{}' })]), false, 'a finished role ignores late activity');
});

test('RunActivity skips events it already has and records a failure with its message', () => {
  const activity = new RunActivity();
  const failed = event('log', { role: 'correctness', state: 'failed', level: 'error', message: 'API Error: AWS authentication failed.' });
  assert.equal(activity.apply([failed]), true);
  assert.equal(activity.apply([failed]), false);
  const [entry] = activity.rolesOf('reviews');
  assert.deepEqual([entry.state, entry.error], ['failed', 'API Error: AWS authentication failed.']);
  assert.equal(rosterLine(entry, NOW).label, 'failed');
  assert.equal(rosterLine(entry, NOW).detail, 'API Error: AWS authentication failed.');
});

test('stageTrack lists the top-level steps with their roles and how many are still waiting', () => {
  const track = stageTrack([...STEPS, { step_id: 'child', parent_step: 'reviews', status: 'running' }], reviewRun(), NOW);
  assert.deepEqual(track.map((stage) => [stage.label, stage.state, stage.icon]), [
    ['resolve', 'done', '✓'], ['reviews', 'running', '⟳'], ['verify', 'running', '⟳'], ['team lead', 'waiting', '…']]);
  const reviews = track[1];
  assert.deepEqual([reviews.done, reviews.total, reviews.waiting, reviews.findings, reviews.current], [1, 9, 7, 2, true]);
  assert.equal(reviews.seconds, 540);
  assert.equal(currentStage(track).key, 'reviews');
  assert.equal(compactWhere(track, NOW), 'reviews ⟳ · 1/9 done: security ✓ 2, beauty ⟳, 7 more waiting');
  assert.equal(rosterLine(reviews.roles[1], NOW).detail, '9m 0s · Bash: git diff main..HEAD');
  assert.equal(rosterLine(reviews.roles[0], NOW).label, '2 findings');
  assert.equal(stageLabel('team_lead'), 'team lead');
});

test('a lone working role gets a headline with the findings reported so far', () => {
  const activity = reviewRun();
  activity.apply([event('log', { role: 'team lead', state: 'started', message: 'weighing 9 reviews', model: 'opus 5.5' }, 'team_lead', 6)]);
  const steps = STEPS.map((step) => (step.step_id === 'team_lead' ? { ...step, status: 'running', started_at: at(6) }
    : { ...step, status: 'succeeded', finished_at: at(6) }));
  const track = stageTrack(steps, activity, NOW);
  const lead = track.at(-1);
  assert.equal(lead.headline, 'Team lead is weighing 9 reviews (2 findings so far)');
  assert.equal(compactWhere(track, NOW), 'team lead ⟳ · Team lead is weighing 9 reviews (2 findings so far)');
});

test('a finding reads as where it is and its badge in words', () => {
  assert.equal(findingWhere({ path: 'a.swift', line: 2 }), 'a.swift:2');
  assert.equal(findingWhere({ path: 'c.swift' }), 'c.swift');
  assert.equal(findingBadge({ severity: 'medium', blocking: true }), 'Blocking · Medium');
  assert.equal(findingBadge({ severity: 'high', blocking: false }), 'High');
  assert.equal(findingBadge({}), 'Finding');
});

test('verdictTone reads the verdict text', () => {
  assert.equal(verdictTone('Approved'), 'ok');
  assert.equal(verdictTone('Changes recommended'), 'warn');
  assert.equal(verdictTone('Not approved'), 'bad');
  assert.equal(verdictTone('Approved with suggestions'), 'warn');
  assert.equal(isBlocking({ severity: 'critical' }), true);
  assert.equal(isBlocking({ severity: 'high' }), false);
});

test('feedSlot collapses role activity, sets boundaries apart, and keeps the rest', () => {
  assert.deepEqual(feedSlot({ kind: 'tool_call', payload: { role: 'security' } }), { slot: 'activity', role: 'security' });
  assert.deepEqual(feedSlot({ kind: 'tool_call', payload: {} }), { slot: 'line', role: null });
  assert.deepEqual(feedSlot({ kind: 'log', payload: { role: 'security', message: 'started' } }), { slot: 'line', role: 'security' });
  assert.deepEqual(feedSlot({ kind: 'step_end', payload: {} }), { slot: 'boundary', role: null });
  assert.deepEqual(feedSlot({ kind: 'step_failed', payload: {} }), { slot: 'line', role: null });
});

test('describe labels a role log with its role and a blocking finding as blocking', () => {
  assert.deepEqual(describe({ kind: 'log', payload: { role: 'security', message: 'started reviewing' } }), { tone: 'muted', label: 'security', text: 'started reviewing' });
  assert.equal(describe({ kind: 'log', payload: { role: 'security', level: 'error', message: 'x' } }).tone, 'bad');
  assert.deepEqual(describe({ kind: 'finding', payload: { blocking: true, severity: 'low', title: 'T' } }).label, 'Blocking');
  assert.equal(describe({ kind: 'summary', payload: { title: 'Fix it', verdict: 'pushed', links: [{}, {}] } }).text, 'outcome pushed, title "Fix it", 2 links');
});

test('backfill pages forward until the server says there is no more', async () => {
  const activity = new RunActivity();
  const first = event('log', { role: 'security', state: 'started', message: 'started' });
  const pages = [
    { events: [first], next_after: first.seq },
    { events: [event('log', { role: 'beauty', state: 'started', message: 'started' })], next_after: null },
  ];
  const asked = [];
  await backfill(activity, async (after) => {
    asked.push(after);
    return pages.shift();
  });
  assert.deepEqual(asked, [0, first.seq]);
  assert.deepEqual(activity.rolesOf('reviews').map((entry) => entry.role), ['security', 'beauty']);
});
