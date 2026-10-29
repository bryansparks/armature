import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { decisionGroups, decisionHeadline, decisionSummary, dropReason, externalLinks, isDropped, outcomeSentence, raisedBy, riskBadge, specialists, threadRiskBadge } =
  await import(new URL('outcome.js', STATIC));
const { RunActivity } = await import(new URL('activity.js', STATIC));

let nextSeq = 0;
const event = (kind, payload = {}, stepId = 'team_lead') => ({ seq: ++nextSeq, ts: '2026-10-27T12:00:00Z', kind, step_id: stepId, payload });

test('externalLinks puts the pull request, issue, and Slack thread first with labels that say which', () => {
  const links = externalLinks({ links: [
    { label: 'Review', url: 'https://github.com/acme/app/pull/12#pullrequestreview-1' },
    { label: 'Thread', url: 'https://acme.slack.com/archives/C1/p1700000000000100' },
    { label: 'Issue', url: 'https://acme.atlassian.net/browse/FS-42' },
    { label: 'Pull request', url: 'https://github.com/acme/app/pull/12' },
    { label: 'Ticket', url: 'https://linear.app/acme/issue/ENG-7/fix-it' },
  ] });
  assert.deepEqual(links.map((item) => [item.kind, item.label]), [
    ['pull', 'Pull request #12'], ['issue', 'Jira FS-42'], ['issue', 'Linear ENG-7'], ['slack', 'Slack thread'], ['other', 'Review']]);
  assert.deepEqual(links.map((item) => item.short), ['#12', 'FS-42', 'ENG-7', 'Slack', 'Review']);
});

test('externalLinks drops unsafe and repeated links and keeps same-origin paths', () => {
  const links = externalLinks({ links: [
    { label: 'Pull request', url: 'https://github.com/acme/app/pull/3' },
    { label: 'Again', url: 'https://github.com/acme/app/pull/3' },
    { label: 'Bad', url: 'javascript:alert(1)' },
    { label: 'Plain', url: 'http://example.com' },
    { label: 'Report', url: '/api/runs/1/artifacts/review.md' },
  ] });
  assert.deepEqual(links.map((item) => item.label), ['Pull request #3', 'Report']);
  assert.deepEqual(externalLinks({}), []);
  assert.equal(externalLinks({ links: [{ label: 'Follow-up pull request', url: 'https://github.com/acme/app/pull/4' }] })[0].label,
    'Follow-up pull request #4');
});

test('decisionGroups sorts the lead findings into blocking, should fix, and dropped', () => {
  const groups = decisionGroups([
    { claim: 'Naming', severity: 'low', blocking: false, seq: 1 },
    { claim: 'Crash', severity: 'medium', blocking: true, seq: 2 },
    { claim: 'Rebutted', severity: 'high', blocking: true, disposition: 'drop_rebutted', seq: 3 },
    { claim: 'Main actor', severity: 'high', blocking: false, seq: 4 },
    { claim: 'Fixed already', disposition: 'resolved', seq: 5 },
  ]);
  assert.deepEqual(groups.blocking.map((finding) => finding.claim), ['Crash']);
  assert.deepEqual(groups.shouldFix.map((finding) => finding.claim), ['Main actor', 'Naming']);
  assert.deepEqual(groups.dropped.map((finding) => finding.claim), ['Rebutted', 'Fixed already']);
  assert.equal(isDropped({ decision: 'drop' }), true);
  assert.equal(dropReason({ disposition: 'drop_rebutted' }), 'Rebutted in the discussion.');
  assert.equal(dropReason({ disposition: 'drop', reason: 'Not in this diff.' }), 'Not in this diff.');
});

test('decisionGroups puts fixed findings in their own group, even ones the lead marked blocking', () => {
  const groups = decisionGroups([
    { claim: 'Crash', blocking: true, decision: 'fixed', seq: 1 },
    { claim: 'Naming', decision: 'suggest', seq: 2 },
    { claim: 'Noise', decision: 'drop', reason: 'Outside the diff.', seq: 3 },
  ]);
  assert.deepEqual(groups.fixed.map((finding) => finding.claim), ['Crash']);
  assert.deepEqual(groups.blocking, []);
  assert.deepEqual(groups.shouldFix.map((finding) => finding.claim), ['Naming']);
  assert.deepEqual(groups.dropped.map((finding) => dropReason(finding)), ['Outside the diff.']);
});

test('decisionGroups follows the lead decision over the finding blocking flag', () => {
  const groups = decisionGroups([
    { claim: 'Lead blocks it', blocking: false, decision: 'block', seq: 1 },
    { claim: 'Lead suggests it', blocking: true, decision: 'suggest', seq: 2 },
    { claim: 'No decision', blocking: true, seq: 3 },
  ]);
  assert.deepEqual(groups.blocking.map((finding) => finding.claim).sort(), ['Lead blocks it', 'No decision']);
  assert.deepEqual(groups.shouldFix.map((finding) => finding.claim), ['Lead suggests it']);
});

test('raisedBy keeps each named specialist once', () => {
  assert.deepEqual(raisedBy({ raised_by: ['security', 'security', ' tests ', 3, ''] }), ['security', 'tests']);
  assert.deepEqual(raisedBy({ raised_by: 'security' }), []);
  assert.deepEqual(raisedBy(null), []);
});

test('threadRiskBadge says risk unknown, never low, when the newest run has no risk', () => {
  assert.deepEqual(threadRiskBadge(null), { text: 'Risk unknown', tone: '', title: null });
  assert.deepEqual(threadRiskBadge('low'), { text: 'Low risk', tone: 'ok', title: null });
});

test('outcomeSentence says the outcome in one sentence, with counts for recommended changes', () => {
  assert.equal(outcomeSentence('pr_opened'), 'Pull request opened.');
  assert.equal(outcomeSentence('recommended', { blocking: 2, shouldFix: 3 }), 'Changes recommended: 2 blocking, 3 should fix.');
  assert.equal(outcomeSentence('recommended'), 'Changes recommended.');
  assert.equal(outcomeSentence('noop'), 'Nothing to do.');
  assert.equal(outcomeSentence(null), '');
  assert.equal(outcomeSentence('toString'), '');
});

test('riskBadge names the level, tones it, and keeps the first reason as its tooltip', () => {
  assert.deepEqual(riskBadge('high', 'migrations and schema: db/migrations/1.sql'), { text: 'High risk', tone: 'bad', title: 'migrations and schema: db/migrations/1.sql' });
  assert.deepEqual(riskBadge('low'), { text: 'Low risk', tone: 'ok', title: null });
  assert.equal(riskBadge(null), null);
  assert.equal(riskBadge('severe'), null);
});

test('decisionHeadline says the verdict and what is left to fix', () => {
  assert.equal(decisionHeadline('Changes Recommended', { blocking: 2, shouldFix: 3 }), 'Changes Recommended: 2 blocking, 3 should fix');
  assert.equal(decisionHeadline('Approved', { blocking: 0, shouldFix: 1 }), 'Approved: 1 should fix');
  assert.equal(decisionHeadline('Approved', { blocking: 0, shouldFix: 0 }), 'Approved');
});

test('decisionSummary counts the lead findings, and its own counts when it sent none', () => {
  const activity = new RunActivity();
  assert.equal(decisionSummary(activity), null);
  activity.apply([
    event('log', { role: 'security', state: 'started', message: 'started' }, 'reviews'),
    event('finding', { role: 'security', severity: 'high', claim: 'Token in log', path: 'a.py', line: 3 }, 'reviews'),
    event('log', { role: 'security', state: 'finished', message: 'finished', findings: 1, review: '1. Token in log' }, 'reviews'),
    event('finding', { role: 'team lead', severity: 'medium', blocking: true, claim: 'Token in log', path: 'a.py', line: 3 }),
    event('finding', { role: 'team lead', severity: 'low', blocking: false, claim: 'Naming' }),
    event('log', { role: 'team lead', state: 'finished', verdict: 'Changes recommended', findings: 2, blocking: 1, message: 'Changes recommended: 2 findings' }),
  ]);
  const summary = decisionSummary(activity);
  assert.deepEqual(summary.counts, { blocking: 1, shouldFix: 1 });
  assert.deepEqual(summary.groups.blocking.map((finding) => finding.claim), ['Token in log']);
  const [security] = specialists(activity);
  assert.deepEqual([security.role, security.count, security.review, security.findings.length], ['security', 1, '1. Token in log', 1]);
  assert.equal(specialists(activity).length, 1, 'the lead is not a specialist');

  const quiet = new RunActivity();
  quiet.apply([event('log', { role: 'team lead', state: 'finished', verdict: 'Changes recommended', findings: 4, blocking: 1, message: 'x' })]);
  assert.deepEqual(decisionSummary(quiet).counts, { blocking: 1, shouldFix: 3 });
});

test('specialists with findings start open only when there are three findings or fewer in all', async () => {
  const { startsOpen } = await import(new URL('result.js', STATIC));
  const few = [{ count: 2, findings: [] }, { count: 0, findings: [] }, { count: null, findings: [{}] }];
  assert.deepEqual(few.map((entry) => startsOpen(entry, few)), [true, false, true]);
  const many = [{ count: 2, findings: [] }, { count: 2, findings: [] }];
  assert.deepEqual(many.map((entry) => startsOpen(entry, many)), [false, false]);
});
