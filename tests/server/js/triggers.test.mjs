import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { KINDS, OPEN_SPRINT_NOTE, connectionChoices, lastResultText, outcomeText, scheduleText, seededText, triggerBody, triggerHref, verdictText } = await import(new URL('views/triggers.js', STATIC));
const { waitReason } = await import(new URL('views/runners.js', STATIC));
const { ruleBody } = await import(new URL('views/webhooks.js', STATIC));
const { parseHash } = await import(new URL('router.js', STATIC));

test('scheduleText reads the interval or the cron and its zone', () => {
  assert.equal(scheduleText({ kind: 'jira_poll', config: {} }), 'Every 5 minutes');
  assert.equal(scheduleText({ kind: 'linear_poll', config: { interval_minutes: 1 } }), 'Every minute');
  assert.equal(scheduleText({ kind: 'schedule', config: { cron: '30 6 * * *', timezone: 'America/Denver' } }), '30 6 * * * (America/Denver)');
  assert.deepEqual(Object.keys(KINDS), ['jira_poll', 'linear_poll', 'schedule']);
});

test('lastResultText and seededText say how the last poll went', () => {
  assert.equal(lastResultText(null), 'Never ran');
  assert.equal(lastResultText({ last_status: 'ok', last_matched: 4, last_new: 1 }), '4 matched, 1 new');
  assert.equal(lastResultText({ last_status: 'error', last_error: 'Jira answered HTTP 401.' }), 'Failed: Jira answered HTTP 401.');
  assert.match(seededText({}), /first poll marks what it finds/);
  assert.match(seededText({ seeded_at: '2026-10-23T12:00:00Z', seeded_count: 12 }), /^Seeded 12 keys on /);
});

test('triggerBody builds each kind’s config and leaves the other kinds’ fields out', () => {
  const jira = triggerBody('jira_poll', { name: ' Sprint ', workflow_id: 'w', connection_id: 'c', jql: ' project = ENG ', interval_minutes: '10', inputs: '{"issue": "$.key"}', secrets: 'A, B', enabled: true, seed_first: true, shadow: false });
  assert.deepEqual(jira, { kind: 'jira_poll', workflow_id: 'w', enabled: true, config: { name: 'Sprint', inputs: { issue: '$.key' }, secrets: ['A', 'B'], connection_id: 'c', interval_minutes: 10, seed_first: true, shadow: false, jql: 'project = ENG' } });
  const linear = triggerBody('linear_poll', { connection_id: 'c', team: 'eng', states: ['started'], assignee_me: true, label: '', interval_minutes: 'x' });
  assert.deepEqual(linear.config, { name: '', secrets: [], connection_id: 'c', interval_minutes: 5, seed_first: false, shadow: false, team: 'ENG', states: ['started'], assignee: 'me', label: null });
  const schedule = triggerBody('schedule', { cron: ' 30 6 * * 1-5 ', timezone: '' });
  assert.deepEqual(schedule.config, { name: '', secrets: [], cron: '30 6 * * 1-5', timezone: 'UTC' });
  assert.throws(() => triggerBody('schedule', { inputs: '{not json' }));
});

test('outcomeText and verdictText explain a seen key', () => {
  assert.equal(outcomeText({ outcome: 'queued', run_id: 'r' }), 'Queued as a run');
  assert.equal(outcomeText({ outcome: 'queued', job_status: 'queued' }), 'Queued, job queued');
  assert.equal(outcomeText({ outcome: 'seeded' }), 'Seeded');
  assert.match(verdictText({ verdict: 'seen', seen: { outcome: 'seeded', first_seen_at: '2026-10-23T12:00:00Z' } }), /^Already seen on .*: seeded\.$/);
  assert.equal(verdictText({ verdict: 'not_returned', message: 'The JQL doesn’t return it.' }), 'The JQL doesn’t return it.');
  assert.match(OPEN_SPRINT_NOTE, /openSprints\(\)/);
});

test('connectionChoices offers only connections of the trigger’s kind', () => {
  const connections = [{ id: 'a', kind: 'jira', name: 'me', scope_kind: 'team' }, { id: 'b', kind: 'linear', name: 'lin', scope_kind: 'project' }];
  assert.deepEqual(connectionChoices(connections, 'jira_poll'), [['a', 'me']]);
  assert.deepEqual(connectionChoices(connections, 'linear_poll'), [['b', 'lin (project)']]);
  assert.deepEqual(connectionChoices(connections, 'schedule'), []);
  assert.equal(parseHash(triggerHref('t/1')).name, 'trigger');
  assert.deepEqual(parseHash(triggerHref('t/1')).args, ['t/1']);
});

test('waitReason says a debounced job waits for quiet and a keyed one waits its turn', () => {
  const now = Date.parse('2026-10-23T12:00:00Z');
  assert.match(waitReason({ status: 'queued', not_before: '2026-10-23T12:10:00Z' }, now), /^Waiting for a quiet period until /);
  assert.equal(waitReason({ status: 'queued', not_before: '2026-10-23T11:00:00Z', concurrency_key: 'a/b#7' }, now), 'Waiting for a runner, one at a time for a/b#7.');
  assert.equal(waitReason({ status: 'queued', warning: 'No runner can.' }, now), 'No runner can.');
  assert.equal(waitReason({ status: 'provisioning' }, now), 'Starting an environment.');
});

test('ruleBody adds the key and concurrency settings only when set', () => {
  const body = ruleBody({ workflow_id: 'w', events: 'pull_request', key: 'repository.full_name, number', concurrency: 'supersede', debounce_seconds: '', cancel_running: false, sticky: 'full' });
  assert.deepEqual(body.config.key, ['repository.full_name', 'number']);
  assert.equal(body.config.concurrency, 'supersede');
  assert.equal('debounce_seconds' in body.config, false);
  assert.equal(body.config.cancel_running, false);
  assert.deepEqual(body.config.sticky, ['full']);
  const plain = ruleBody({ workflow_id: 'w', concurrency: 'none', debounce_seconds: '0', cancel_running: true, sticky: 'full' });
  assert.equal(plain.config.debounce_seconds, 0);
  assert.equal(['key', 'concurrency', 'cancel_running', 'sticky'].some((name) => name in plain.config), false);
});
