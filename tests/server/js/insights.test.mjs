import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

// Just enough DOM for the Insights builders to run under node.
class FakeNode {
  get isConnected() { return false; }
}
class FakeText extends FakeNode {
  constructor(text) {
    super();
    this.text = text;
  }
}
class FakeElement extends FakeNode {
  constructor(tag) {
    super();
    this.tag = tag;
    this.attributes = {};
    this.children = [];
    this.listeners = {};
    this.className = '';
    this.style = {};
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  addEventListener(name, handler) {
    this.listeners[name] = handler;
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  get firstChild() { return this.children[0] || null; }

  querySelector() { return null; }
}
globalThis.Node = FakeNode;
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createElementNS: (_, tag) => new FakeElement(tag),
  createTextNode: (text) => new FakeText(text),
};
globalThis.location = { hash: '' };
globalThis.requestAnimationFrame = () => 0;

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const format = await import(new URL('views/insights/format.js', STATIC));
const words = await import(new URL('views/insights/words.js', STATIC));
const { daysFrom, findRepo, linker, mergeQuery, repoSlots, repoToken } = await import(new URL('views/insights/index.js', STATIC));
const { countAxis, yAxis } = await import(new URL('views/insights/chart.js', STATIC));
const { breakablePath, rankFiles } = await import(new URL('views/insights/repo.js', STATIC));
const { stageBlocks } = words;
const { COLUMNS, VIEWS, mixBar, standing } = await import(new URL('views/insights/people.js', STATIC));
const { trendWords, AREAS } = await import(new URL('views/insights/person.js', STATIC));
const { timeline } = await import(new URL('views/insights/pr.js', STATIC));
const { parseHash } = await import(new URL('router.js', STATIC));

const texts = (node) => (node instanceof FakeText ? [node.text] : node instanceof FakeElement ? node.children.flatMap(texts) : node == null ? [] : [String(node)]);
const scope = { days: 30, start: '2026-10-14T12:00:00+00:00', end: '2026-11-13T12:00:00+00:00', previous_start: '2026-09-14T12:00:00+00:00' };
const repos = [
  { id: 'r1', full_name: 'acme/android-tree', name: 'android-tree' },
  { id: 'r2', full_name: 'acme/mobile-ios', name: 'mobile-ios' },
  { id: 'r3', full_name: 'other/mobile-ios', name: 'mobile-ios' },
];
const hrefs = linker({ team: { id: 't1' }, teams: [{ id: 't1' }], repos });

test('figures carry their unit separately, and say a dash for no value', () => {
  assert.deepEqual(format.figure(1.62, 'days', 1), { value: '1.6', unit: 'days' });
  assert.deepEqual(format.figure(1, 'hours', 0), { value: '1', unit: 'hour' });
  assert.deepEqual(format.figure(0.042, 'share', 0), { value: '4%', unit: '' });
  assert.deepEqual(format.figure(350.466, 'usd', 2), { value: '$350.47', unit: '' });
  assert.deepEqual(format.figure(null, 'days', 1), { value: '—', unit: '' });
  assert.equal(format.figureText(1234, 'count', 0), '1,234');
  assert.equal(format.short(4, 'hours', 1), '4.0 h');
});

test('a delta says the direction of the number and whether that is good', () => {
  const merged = format.delta(148, 136, format.MEASURES.merged, 30);
  assert.equal(merged.dir, 'up');
  assert.equal(merged.good, true);
  assert.equal(merged.text, '9%');
  assert.equal(merged.sr, 'Up 9 percent compared with the previous 30 days, which is better.');
  const rounds = format.delta(2.3, 1.9, format.MEASURES.review_rounds, 30);
  assert.deepEqual([rounds.dir, rounds.good, rounds.text], ['up', false, '0.4']);
  const ttm = format.delta(1.6, 2.0, format.MEASURES.time_to_merge, 7);
  assert.deepEqual([ttm.dir, ttm.good, ttm.text], ['down', true, '0.4 days']);
  const ci = format.delta(0.05, 0.03, format.MEASURES.ci_failure_rate, 30);
  assert.equal(ci.text, '2 points');
  assert.equal(format.delta(10, 10, format.MEASURES.merged, 30).dir, 'flat');
  assert.equal(format.delta(4, 0, format.MEASURES.merged, 30).text, '4');
  const small = format.delta(14, 7, format.MEASURES.merged, 30);
  assert.deepEqual([small.dir, small.good, small.text, small.sr], ['up', true, '7', 'Up 7 compared with the previous 30 days, which is better.']);
  assert.equal(format.delta(12, 10, format.MEASURES.merged, 30).text, '20%');
  assert.equal(format.delta(9, 9.5, format.MEASURES.conflict_hours, 30).text, '1 hour');
  assert.equal(format.delta(12, 0, format.MEASURES.conflict_hours, 30).text, '12 hours');
  assert.equal(format.delta(3.2, 2, format.MEASURES.spend, 30).text, '$1.20');
  for (const previous of [0, 1, 5, 9.9]) assert.ok(!format.delta(20, previous, format.MEASURES.merged, 30).text.includes('%'), String(previous));
  assert.equal(format.delta(4, null, format.MEASURES.merged, 30), null);
});

test('dates read as a manager would say them', () => {
  assert.equal(format.dateRange(scope.start, scope.end), 'October 14 to November 13');
  assert.equal(format.dateRange('2025-12-20T12:00:00+00:00', '2026-01-19T12:00:00+00:00'), 'December 20, 2025 to January 19, 2026');
  assert.equal(format.monthDay('2026-11-03T08:00:00Z', false), 'Nov 3');
  assert.equal(format.daysAgo('2026-11-10T12:00:00Z', Date.parse('2026-11-13T13:00:00Z')), '3 days');
});

test('internal names become plain words', () => {
  assert.equal(format.categoryName('security-privacy'), 'Security and privacy');
  assert.equal(format.categoryName('brand-new-thing'), 'Brand new thing');
  assert.equal(format.categoryName(null), 'Not yet categorized');
  assert.equal(format.pushKindName('merge_from_base'), 'Merge from main');
  assert.equal(format.pushKindName(null), 'Not yet classified');
  assert.equal(format.changeTypeName('code-maintenance'), 'Maintenance');
  assert.equal(format.specialistName('ui'), 'UI');
  assert.equal(format.specialistName('concurrency'), 'Concurrency');
  assert.equal(format.initials('Tomás Herrera'), 'TH');
  assert.equal(format.initials('Prince'), 'P');
});

test('the people helpers find concentration and percentiles', () => {
  assert.equal(format.halfBy([10, 8, 1, 1]), 1);
  assert.equal(format.halfBy([3, 3, 3, 3]), 2);
  assert.equal(format.halfBy([]), 0);
  assert.equal(format.median([3, 1, 2, null]), 2);
  assert.equal(format.quantile([1, 2, 3, 4, 5], 0.8), 4.2);
  assert.deepEqual(format.tickIndexes(30, 5), [0, 7, 15, 22, 29]);
  assert.deepEqual(format.tickIndexes(1, 5), [0]);
});

test('sorting keeps missing values last in either direction', () => {
  const rows = [{ v: 2 }, { v: null }, { v: 5 }];
  assert.deepEqual(format.sortRows(rows, 'v', 'descending').map((row) => row.v), [5, 2, null]);
  assert.deepEqual(format.sortRows(rows, 'v', 'ascending').map((row) => row.v), [2, 5, null]);
});

test('metric names in the URL are short and round-trip', () => {
  assert.equal(format.metricFromKey('rounds', ['merged', 'review_rounds']), 'review_rounds');
  assert.equal(format.metricFromKey('review_rounds', ['review_rounds']), 'review_rounds');
  assert.equal(format.metricFromKey('nope', ['review_rounds']), null);
});

test('the scope line says who, where, when, and the comparison', () => {
  assert.equal(words.scopeLine({ scope, people: 12, repos: 3 }), '12 people across 3 repos, October 14 to November 13. Changes compare with the 30 days before.');
  assert.equal(words.scopeLine({ scope, repoName: 'acme/android-tree' }), 'acme/android-tree, October 14 to November 13. Changes compare with the 30 days before.');
  assert.equal(words.scopeLine({ scope }), 'October 14 to November 13. Changes compare with the 30 days before.');
  assert.equal(words.scopeLine({ scope, repos: 3, compare: false }), '3 repos, October 14 to November 13.');
});

test('the overview answer leads with shipping, then what is getting worse', () => {
  const data = {
    scope,
    metrics: [{ id: 'merged', value: 148, previous: 146 }, { id: 'time_to_merge', value: 1.6, previous: 2.0 }, { id: 'review_rounds', value: 2.3, previous: 1.9 }],
    attention: [{ kind: 'rounds_rising', repo: 'acme/android-tree', top_category: 'concurrency' }],
  };
  assert.equal(words.overviewAnswer(data), 'Shipping is steady and pull requests merge faster than the previous period. android-tree pull requests need more review rounds, mostly for concurrency.');
  assert.equal(words.overviewAnswer({ scope, metrics: [{ id: 'merged', value: 0, previous: 0 }], attention: [] }, 'tree-service'), 'No pull requests merged in tree-service in the last 30 days.');
  const slower = { scope, metrics: [{ id: 'merged', value: 100, previous: 130 }, { id: 'time_to_merge', value: 2.4, previous: 1.6 }], attention: [] };
  assert.equal(words.overviewAnswer(slower), 'Shipping slowed and pull requests take longer to merge, 2.4 days at the median.');
});

test('the chart takeaway names the repo that moved', () => {
  const dates = ['2026-10-15T12:00:00Z', '2026-10-16T12:00:00Z', '2026-10-17T12:00:00Z'];
  const series = [{ name: 'android-tree', values: [null, 2.2, 3.1] }, { name: 'mobile-ios', values: [1.9, 1.95, 2.0] }];
  assert.equal(words.takeaway('review_rounds', series, { dates }), 'android-tree’s 7-day average is 3.1 review rounds per pull request, up from 2.2 on October 16. The other repos held steady.');
  assert.equal(words.takeaway('review_rounds', series, { dates, flat: true }), 'Steady over the period for the team as a whole.');
  assert.equal(words.takeaway('time_to_first_review', [{ name: 'lib', values: [2.6, 3.1] }], { dates, flat: true }), 'lib held steady over the period.');
  assert.equal(words.takeaway('time_to_first_review', [{ name: 'lib', values: [2.6, 3.1] }], { dates }), 'lib’s 7-day median wait for a first review is 3.1 hours, up from 2.6 on October 15.');
  assert.equal(words.takeaway('time_to_merge', [{ name: 'lib', values: [2.4, 1.8] }], { dates }), 'lib’s 7-day median time to merge is 1.8 days, down from 2.4 on October 15.');
  assert.equal(words.takeaway('conflict_hours', [{ name: 'lib', values: [2, 10] }], { dates }), 'lib spent 10 hours blocked by conflicts in the last 7 days, up from 2 in the 7 days to October 15.');
  assert.equal(words.takeaway('ci_failure_rate', [{ name: 'lib', values: [0.02, 0.08] }], { dates }), '8% of checks in lib failed in the last 7 days, up from 2% in the 7 days to October 15.');
  assert.equal(words.takeaway('review_rounds', [{ name: 'a', values: [2, 2.05] }, { name: 'b', values: [1, 1] }]), 'Every repo held steady over the period.');
  assert.equal(words.takeaway('merged', [{ name: 'a', values: [3, 5] }, { name: 'b', values: [9, 12] }]), 'b merged the most, 12 pull requests in the last 7 days.');
  assert.equal(words.takeaway('merged', [{ name: 'a', values: [null, null] }]), 'Not enough data in this period to draw a trend.');
});

const brief = (number, title, extra = {}) => ({ id: `pr${number}`, repo: 'acme/tree-service', repo_id: 'r1', number, title, author: 'Omar Haddad', ...extra });

test('attention items are worded from the data, in plain terms', () => {
  const bug = { kind: 'dropped_flag_bug', link: 'revert', confidence: 1, pr: brief(4127, 'Faster person search'), bug_pr: brief(4130, 'Revert search'), flag: { category: 'correctness', claim: 'Join can repeat rows', specialist: 'correctness' } };
  const worded = words.attentionWords(bug, { hrefs });
  assert.equal(worded.title, 'A merged pull request was reverted');
  assert.equal(worded.why, 'tree-service #4127 was reverted by #4130. The review flagged correctness, and the flag was dropped.');
  assert.equal(worded.icon, 'bad');
  assert.deepEqual(worded.link, ['Open #4127', '#/insights/pr/android-tree/4127']);
  assert.ok(!JSON.stringify(worded).includes('candidate'));

  const rising = words.attentionWords({ kind: 'rounds_rising', repo_id: 'r1', repo: 'acme/android-tree', value: 3.1, previous: 2.2, top_category: 'concurrency', categories: [{ category: 'concurrency', count: 23 }] }, { hrefs });
  assert.equal(rising.title, 'android-tree pull requests need more review rounds');
  assert.equal(rising.why, '3.1 rounds per pull request, up from 2.2. Most extra rounds were flagged for concurrency.');
  assert.deepEqual(rising.ranked, [['Concurrency', 23]]);
  assert.equal(rising.link[1], '#/insights/findings?repo=android-tree');

  const conflict = words.attentionWords({ kind: 'conflict_file', repo_id: 'r1', repo: 'acme/android-tree', path: 'app/build.gradle.kts', hours: 14, prs: 9, next_hours: 4.5, files: [] }, { hrefs });
  assert.equal(conflict.why, '14 hours blocked across 9 pull requests, three times more than any other file.');
  const only = words.attentionWords({ kind: 'conflict_file', repo_id: 'r1', repo: 'acme/android-tree', path: 'a.kt', hours: 1, prs: 1, next_hours: 0, files: [] }, { hrefs });
  assert.equal(only.why, '1 hour blocked across 1 pull request, the only file with conflicts in this period.');

  const waiting = { kind: 'waiting_on_review', person_id: 'p1', person: 'Hannah Kowalski', prs: [brief(4171, 'Cache ancestor lookups', { waiting_days: 4.2 }), brief(4176, 'Retry uploads', { waiting_days: 3 })] };
  assert.equal(words.attentionWords(waiting, { hrefs }).why, '2 pull requests have gone more than 2 days without a review.');
  assert.deepEqual(words.attentionWords(waiting, { hrefs, canSeePeople: true }).link, ['Open Hannah’s page', '#/insights/person/p1']);
  assert.deepEqual(words.attentionWords({ ...waiting, login: 'hkowalski' }, { hrefs, canSeePeople: true }).link, ['Open Hannah’s page', '#/insights/person/hkowalski']);
  assert.deepEqual(words.attentionWords(waiting, { hrefs }).link, ['Open #4171', '#/insights/pr/android-tree/4171']);
  const anonymous = words.attentionWords({ kind: 'waiting_on_review', count: 3, prs: [brief(1, 'a'), brief(2, 'b'), brief(3, 'c')] }, { hrefs, canSeePeople: true });
  assert.equal(anonymous.title, '3 pull requests are waiting on a first review');
  assert.equal(anonymous.why, 'Each has gone more than 2 days without a review.');
  assert.deepEqual(anonymous.link, ['Open #1', '#/insights/pr/android-tree/1']);

  const specialist = words.attentionWords({ kind: 'specialist_dropped_bugs', specialist: 'correctness', count: 2 }, { hrefs, all: [bug] });
  assert.equal(specialist.title, 'Dropped correctness flags led to bugs');
  assert.equal(specialist.events.length, 1);
  assert.equal(words.attentionWords({ kind: 'something_new' }, { hrefs }), null);
});

test('screen answers stay neutral about people', () => {
  const shipping = { scope, view: 'shipping', rows: [10, 9, 1, 1, 1, 1, 1, 1, 1, 1].map((merged, i) => ({ id: `p${i}`, measures: { merged } })) };
  assert.equal(words.peopleAnswer(shipping), '10 people merged 27 pull requests. Half of them came from 2 people.');
  const review = { scope, view: 'review', rows: [{ measures: { reviews_given: 5 } }, { measures: { reviews_given: 5 } }] };
  assert.equal(words.peopleAnswer(review), '2 people gave 10 reviews.');
  assert.equal(words.peopleAnswer({ scope, view: 'quality', rows: [] }), 'Nobody opened, merged, or reviewed a pull request in the last 30 days.');
  for (const text of [words.peopleAnswer(shipping), words.peopleAnswer(review)]) assert.ok(!/slow|poor|bad/i.test(text));

  const person = {
    scope, person: { name: 'Mei Chen', repo: 'acme/android-tree' }, shipping: { merged: 13, by_type: { enhancement: 8, 'bug-fix': 5 } },
    quality: { rounds_per_pr: 3.2 }, review: { reviews_given: 0 }, team_median: { quality: { rounds_per_pr: 2.3 } }, flagged_categories: [{ category: 'concurrency', count: 9 }],
  };
  assert.equal(words.personAnswer(person), 'Mei shipped 13 pull requests, mostly enhancements in android-tree. Mei’s pull requests needed 3.2 review rounds, above the team median of 2.3, most often flagged for concurrency.');
});

test('repo, findings, models, fixes, and pull request answers', () => {
  const repo = {
    scope, repo: { name: 'android-tree' }, stages: { pr: { id: 'x' }, waiting_first_review: 19, in_review: 22, blocked_by_conflicts: 40, waiting_on_ci: 1, approved_to_merged: 2 },
    tiles: [{ id: 'time_to_first_review', value: 19 }, { id: 'review_rounds', value: 3.1 }],
  };
  assert.equal(words.repoAnswer(repo), 'Most of the median pull request’s time goes to review rounds. Across the period, pull requests wait 19.0 hours for a first review at the median and average 3.1 review rounds.');
  const winner = (key) => words.repoAnswer({ ...repo, tiles: [], stages: { pr: { id: 'x' }, waiting_first_review: 1, in_review: 1, waiting_on_ci: 1, approved_to_merged: 1, [key]: 50 } });
  assert.equal(winner('waiting_first_review'), 'Most of the median pull request’s time goes to waiting for a first review.');
  assert.equal(winner('in_review'), 'Most of the median pull request’s time goes to review rounds.');
  assert.equal(words.repoAnswer({ ...repo, tiles: [], stages: { pr: { id: 'x' }, waiting_first_review: 1, in_review: 1, waiting_on_ci: 40, approved_to_merged: 50 } }), 'Most of the median pull request’s time goes to waiting on CI.');
  assert.equal(winner('approved_to_merged'), 'Most of the median pull request’s time goes to the wait between approval and merge.');
  assert.equal(words.repoAnswer({ scope, repo: { name: 'lib' }, stages: { pr: null } }), 'No pull requests merged in lib in the last 30 days.');

  const findings = { scope, category: null, categories: [{ category: 'concurrency', count: 23, previous: 9 }, { category: 'testing', count: 5, previous: 5 }] };
  assert.equal(words.findingsAnswer(findings), 'Concurrency is flagged most: 23 times in the last 30 days, up from 9. Tests comes next with 5.');
  assert.equal(words.findingsAnswer({ ...findings, category: 'testing' }), 'Tests was flagged 5 times in the last 30 days.');
  assert.equal(words.findingsAnswer({ scope, categories: [] }), 'Nothing was flagged in the last 30 days.');
  const tops = [{ repo_id: 'a', category: 'concurrency', count: 9 }, { repo_id: 'b', category: 'concurrency', count: 4 }, { repo_id: 'c', category: 'testing', count: 3 }];
  assert.equal(words.findingsAnswer({ ...findings, top_by_repo: tops }), 'Concurrency is flagged most: 23 times in the last 30 days, up from 9. It is the top category in two of three repos.');
  assert.equal(words.findingsAnswer({ ...findings, top_by_repo: tops.slice(0, 2) }), 'Concurrency is flagged most: 23 times in the last 30 days, up from 9. It is the top category in every repo.');
  assert.equal(words.findingsAnswer({ ...findings, top_by_repo: tops }, 'lib'), 'Concurrency is flagged most: 23 times in lib in the last 30 days, up from 9. Tests comes next with 5.');

  assert.equal(words.modelsAnswer({ scope, spend_usd: { value: 100, previous: 80 }, cost_per_reviewed_pr_usd: { value: 0.5 }, comparisons: [] }),
    'Reviews cost $100.00, up 25% on the 30 days before. That is $0.50 per reviewed pull request. No comparison runs have finished yet, so there is no recommendation.');
  assert.equal(words.fixesAnswer({ waiting: [] }), 'Nothing is waiting for you.');
  assert.equal(words.fixesAnswer({ waiting: [{}, {}] }), '2 fixes are waiting for a team lead.');
  assert.equal(words.fixesAnswer({ waiting: [{ may_decide: true }, {}] }), '2 fixes are waiting for your approval.');
  const android = (label) => ({ may_decide: true, pattern: { label, repo: 'acme/android-tree' } });
  assert.equal(words.fixesAnswer({ waiting: [android('concurrency'), android('concurrency')] }),
    '2 fixes are waiting for your approval. Both target concurrency in android-tree.');
  assert.equal(words.fixesAnswer({ waiting: [android('performance')] }), 'One fix is waiting for your approval. It targets performance in android-tree.');
  assert.equal(words.fixesAnswer({ waiting: [android('concurrency'), android('performance')] }), '2 fixes are waiting for your approval.');

  const pr = { pr: { title: 'Faster person search', author: 'Omar Haddad', merged_at: '2026-10-30T12:00:00Z', rounds: 1 }, rounds: [{ flags: [{ status: 'dropped' }, { status: 'kept' }] }], links: [{ direction: 'fixed_by', kind: 'revert', pr: { number: 4130 } }] };
  assert.equal(words.prAnswer(pr), 'Faster person search, by Omar Haddad. Merged October 30 after 1 review round, with 2 flags (1 kept, 1 dropped). It was later reverted by #4130.');
});

test('links name repos by short name unless two repos share it', () => {
  assert.equal(repoToken(repos, repos[0]), 'android-tree');
  assert.equal(repoToken(repos, repos[1]), 'acme/mobile-ios');
  assert.equal(findRepo(repos, 'android-tree').id, 'r1');
  assert.equal(findRepo(repos, 'other/mobile-ios').id, 'r3');
  assert.equal(findRepo(repos, 'r2').id, 'r2');
  assert.equal(findRepo(repos, 'missing'), null);
  assert.equal(hrefs.repo('r1', { days: 7 }), '#/insights/repo/android-tree?days=7');
  assert.equal(hrefs.findings({ repo: 'r2', category: 'concurrency' }), '#/insights/findings?repo=acme%2Fmobile-ios&category=concurrency');
  const parsed = parseHash(hrefs.person('p 1', { tab: 'quality' }));
  assert.deepEqual([parsed.name, parsed.args, parsed.query.get('tab')], ['insights', ['person', 'p 1'], 'quality']);
  const multi = linker({ team: { id: 't2' }, teams: [{ id: 't1' }, { id: 't2' }], repos });
  assert.equal(multi.overview(), '#/insights?team=t2');
  assert.equal(hrefs.pr(null), null);
  assert.equal(hrefs.person({ id: 'p1', login: 'mei-chen' }, { days: 7 }), '#/insights/person/mei-chen?days=7');
  assert.equal(hrefs.person({ id: 'p1', login: null }), '#/insights/person/p1');
  assert.equal(hrefs.person('p1'), '#/insights/person/p1');
  assert.equal(hrefs.pr({ id: 'p9', repo_id: 'r2', number: 12 }), '#/insights/pr/acme%2Fmobile-ios/12');
  assert.deepEqual(parseHash(hrefs.pr({ id: 'p9', repo_id: 'r2', number: 12 })).args, ['pr', 'acme/mobile-ios', '12']);
  assert.equal(hrefs.pr({ id: 'p9', repo_id: 'elsewhere', number: 12 }), '#/insights/pr/p9');
});

test('repo colors follow tracking order, not names', () => {
  const slots = repoSlots([
    { id: 'a', full_name: 'acme/a', tracked_at: '2026-10-03T00:00:00Z' },
    { id: 'b', full_name: 'acme/b', tracked_at: '2026-09-01T00:00:00Z' },
    { id: 'c', full_name: 'acme/c', tracked_at: '2026-09-01T00:00:00Z' },
    { id: 'd', full_name: 'acme/d' },
  ]);
  assert.deepEqual(['a', 'b', 'c', 'd'].map((id) => slots.get(id)), [3, 1, 2, 4]);
});

test('the period comes from the URL and falls back to a valid one', () => {
  assert.equal(daysFrom(new URLSearchParams('days=7')), 7);
  assert.equal(daysFrom(new URLSearchParams('days=8')), 30);
  assert.equal(daysFrom(new URLSearchParams(''), 90), 90);
});

test('time to merge splits into stages that add up, with CI inside the wait after approval', () => {
  const blocks = stageBlocks({ pr: { id: 'x' }, waiting_first_review: 2, in_review: 5, blocked_by_conflicts: 3, waiting_on_ci: 1, approved_to_merged: 4 });
  assert.deepEqual(blocks.map((block) => [block.key, block.hours]), [['waiting_first_review', 2], ['in_review', 5], ['waiting_on_ci', 1], ['approved_to_merged', 3]]);
  assert.equal(blocks.reduce((sum, block) => sum + block.hours, 0), 11);
  assert.equal(stageBlocks({ pr: { id: 'x' }, waiting_first_review: 1, in_review: 0, waiting_on_ci: 9, approved_to_merged: 2 })[2].hours, 2);
  assert.deepEqual(stageBlocks({ pr: null }), []);
});

test('file bars follow the same score that orders the list', () => {
  const files = [
    { path: 'a', findings: 40, conflict_hours: 104, churn: 11 },
    { path: 'b', findings: 27, conflict_hours: 28, churn: 12 },
    { path: 'c', findings: 29, conflict_hours: 45, churn: 10 },
    { path: 'd', findings: 38, conflict_hours: 0, churn: 11 },
  ];
  for (const rankBy of ['combined', 'findings', 'conflict_hours', 'churn']) {
    const scores = rankFiles(files, rankBy).map((item) => item.score);
    assert.deepEqual(scores, [...scores].sort((x, y) => y - x), rankBy);
  }
  assert.deepEqual(rankFiles(files, 'findings').map((item) => [item.row.path, item.score]), [['a', 40], ['d', 38], ['c', 29], ['b', 27]]);
  assert.deepEqual(rankFiles(files, 'conflict_hours')[0].score, 104);
});

test('people columns cover the four views and never a combined score', () => {
  assert.deepEqual(VIEWS.map(([id]) => id), ['shipping', 'quality', 'review', 'unblocking']);
  for (const [id] of VIEWS) for (const column of COLUMNS[id]) assert.ok(column.def, `${id}.${column.key} has a definition`);
  assert.ok(!Object.values(COLUMNS).flat().some((column) => /score/i.test(column.label)));
  const values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
  assert.equal(standing(10, values), 'high');
  assert.equal(standing(1, values), 'low');
  assert.equal(standing(5, values), null);
  assert.equal(standing(5, [5, 5]), null);
  const bar = mixBar({ 'bug-fix': 2, enhancement: 1 });
  assert.equal(bar.attributes['aria-label'], 'Bug fix 2, Enhancement 1');
  assert.equal(bar.children.length, 2);
});

test('the person trend says how recent weeks compare with the team', () => {
  assert.equal(trendWords(AREAS.shipping, [1, 2, 3, 3, 3, 3], [2, 2, 2, 2, 2, 2], 'Mei Chen'), 'Over the last 4 weeks, Mei averaged 3.0 merged a week, above the team median of 2.0.');
  assert.equal(trendWords(AREAS.quality, [2, 2], [2.1, 2.1], 'Mei Chen'), 'Over the last 4 weeks, Mei averaged 2.0 rounds per pull request, close to the team median.');
  assert.equal(trendWords(AREAS.review, [null], [1], 'Mei Chen'), null);
});

test('the pull request timeline is in time order with later links at the end', () => {
  const rows = timeline({
    pr: { created_at: '2026-10-28T10:00:00Z', author: 'Omar Haddad' },
    pushes: [{ at: '2026-10-29T09:00:00Z', kind: 'fix_after_review', url: 'u1' }],
    rounds: [{ at: '2026-10-28T15:00:00Z', reviews: [{ reviewer: 'Aisha Bello', state: 'changes_requested' }] }],
    conflicts: [{ start: '2026-10-29T12:00:00Z', hours: 2 }],
    checks: [{ name: 'ci', conclusion: 'failure', completed_at: '2026-10-29T10:00:00Z' }, { name: 'lint', conclusion: 'success', completed_at: '2026-10-29T10:00:00Z' }],
    merge: { at: '2026-10-30T10:00:00Z' },
    links: [{ direction: 'fixed_by', kind: 'revert', pr: { id: 'b', number: 4130, title: 'Revert' } }],
  });
  assert.deepEqual(rows.map((row) => row[1]), [
    'Opened by Omar Haddad.', 'Review round 1: Aisha Bello asked for changes.', 'Pushed: fix after review.', 'Check ci failed.',
    'Blocked by a merge conflict for 2.0 hours.', 'Merged.', 'Reverted by #4130 Revert.',
  ]);
  assert.equal(rows[2][2], 'u1');
  assert.deepEqual(rows[6][2], { pr: { id: 'b', number: 4130, title: 'Revert' } });
});

test('a tile shows the value, the delta, and its definition for screen readers', async () => {
  const { tile } = await import(new URL('views/insights/ui.js', STATIC));
  const node = tile({ measure: format.MEASURES.review_rounds, value: 2.3, previous: 1.9, days: 30, selected: true });
  const all = texts(node).join('|');
  assert.ok(all.includes('Review rounds per pull request'));
  assert.ok(all.includes('2.3'));
  assert.ok(all.includes('0.4'));
  assert.ok(all.includes('vs previous 30 days'));
  assert.ok(all.includes('Up 0.4 compared with the previous 30 days, which is worse.'));
  assert.equal(node.attributes.role, 'tab');
  assert.equal(node.attributes['aria-selected'], 'true');
  const fresh = tile({ measure: format.MEASURES.merged, value: 4, previous: null, days: 30 });
  assert.ok(texts(fresh).includes('Not enough history to compare yet.'));
});

test('format handles missing values, rounding to nothing, and invalid dates', () => {
  assert.equal(format.delta(undefined, 3, format.MEASURES.merged, 30), null);
  assert.equal(format.delta(3, undefined, format.MEASURES.merged, 30), null);
  assert.equal(format.delta(null, null, format.MEASURES.merged, 30), null);
  assert.equal(format.delta(3, 3, null, 30), null);
  const tiny = format.delta(100.2, 100, format.MEASURES.merged, 30);
  assert.deepEqual([tiny.dir, tiny.text, tiny.sr], ['flat', 'No change', 'No change compared with the previous 30 days.']);
  assert.equal(format.delta(0.05, 0.04, format.MEASURES.ci_failure_rate, 30).text, '1 point');
  assert.equal(format.delta(1.62, 1.6, format.MEASURES.time_to_merge, 30).dir, 'flat');
  const now = Date.parse('2026-11-13T12:00:00Z');
  assert.equal(format.daysAgo('2026-11-13T08:00:00Z', now), 'Today');
  assert.equal(format.daysAgo('2026-11-12T08:00:00Z', now), '1 day');
  assert.equal(format.daysAgo('not a date', now), '');
  assert.equal(format.monthDay('not a date'), '');
  assert.equal(format.dateRange('nope', scope.end), '');
  assert.equal(format.dateRange(scope.start, scope.end), 'October 14 to November 13');
  assert.deepEqual(format.sortRows([{ n: 'b' }, { n: 'a' }, { n: null }], 'n', 'ascending').map((row) => row.n), ['a', 'b', null]);
  assert.deepEqual(format.sortRows([{ n: 'b' }, { n: 'a' }], 'n', 'descending').map((row) => row.n), ['b', 'a']);
  assert.equal(format.figureText(1, 'days', 1), '1.0 days');
  assert.equal(format.figureText(1, 'days', 0), '1 day');
  assert.equal(format.figureText(2, 'hours', 0), '2 hours');
  assert.equal(format.figureText(null, 'hours', 0), '—');
});

test('model ids read as product names, the same cases the server tests', () => {
  // tests/server/model_names.json is shared with the server's model_name test, so both sides write one name.
  const cases = JSON.parse(readFileSync(new URL('../model_names.json', import.meta.url), 'utf8'));
  assert.ok(cases.length >= 6);
  for (const [id, name] of cases) assert.equal(format.modelName(id), name, id);
});

test('the y axis is round and its labels read cleanly', () => {
  assert.deepEqual(yAxis(9).labels, ['0', '2', '4', '6', '8', '10']);
  assert.deepEqual(yAxis(0.9).labels, ['0', '0.2', '0.4', '0.6', '0.8', '1.0']);
  assert.deepEqual(yAxis(4.3).labels, ['0', '1', '2', '3', '4', '5']);
  assert.deepEqual(yAxis(0).labels, ['0', '0.5', '1.0', '1.5', '2.0']);
  assert.equal(yAxis(9).max, 10);
});

test('an in-page filter change merges into the query and drops empty values', () => {
  const next = mergeQuery(new URLSearchParams('days=30&repo=lib&metric=rounds'), { repo: '', days: 7, view: 'quality', metric: null });
  assert.equal(next.toString(), 'days=7&view=quality');
  assert.equal(mergeQuery('', { a: 0 }).toString(), 'a=0');
});

test('a path breaks after each folder', () => {
  const parts = breakablePath('app/src/Main.kt');
  assert.deepEqual(parts.map((part) => (typeof part === 'string' ? part : part.tag)), ['app/', 'wbr', 'src/', 'wbr', 'Main.kt']);
});

test('conflict wording uses a multiplier only from twice as many', () => {
  const words6 = words.attentionWords({ kind: 'conflict_file', repo_id: 'r1', repo: 'acme/android-tree', path: 'a.kt', hours: 6, prs: 2, next_hours: 5, files: [] }, { hrefs });
  assert.equal(words6.why, '6 hours blocked across 2 pull requests, more than any other file.');
  const words8 = words.attentionWords({ kind: 'conflict_file', repo_id: 'r1', repo: 'acme/android-tree', path: 'a.kt', hours: 8, prs: 2, next_hours: 5, files: [] }, { hrefs });
  assert.equal(words8.why, '8 hours blocked across 2 pull requests, more than any other file.');
  const words35 = words.attentionWords({ kind: 'conflict_file', repo_id: 'r1', repo: 'acme/android-tree', path: 'a.kt', hours: 35, prs: 6, next_hours: 5, files: [] }, { hrefs });
  assert.equal(words35.why, '35 hours blocked across 6 pull requests, 7 times more than any other file.');
  const words10 = words.attentionWords({ kind: 'conflict_file', repo_id: 'r1', repo: 'acme/android-tree', path: 'a.kt', hours: 10, prs: 3, next_hours: 5, files: [] }, { hrefs });
  assert.equal(words10.why, '10 hours blocked across 3 pull requests, twice as many as any other file.');
});

test('every answer-line branch reads as written', () => {
  const rows = (key, values) => values.map((value, i) => ({ id: `p${i}`, measures: { [key]: value } }));
  // People, quality
  assert.equal(words.peopleAnswer({ scope, view: 'quality', rows: rows('rounds_per_pr', [2]), team_median: {} }), 'No merged pull requests to measure in this period.');
  assert.equal(words.peopleAnswer({ scope, view: 'quality', rows: [...rows('rounds_per_pr', [2, 2, 4]), { measures: { bugs_traced: 2 } }], team_median: { rounds_per_pr: 2 } }),
    'Pull requests needed 2.0 review rounds at the team median. 1 person’s pull requests needed half again as many. 2 bugs traced back to merged work.');
  assert.equal(words.peopleAnswer({ scope, view: 'quality', rows: rows('rounds_per_pr', [2, 2.2]), team_median: { rounds_per_pr: 2.1 } }),
    'Pull requests needed 2.1 review rounds at the team median. Nobody is far from it.');
  // People, unblocking
  assert.equal(words.peopleAnswer({ scope, view: 'unblocking', rows: [{ measures: { hotspot_prs: 0, platform_prs: 0 } }] }),
    'No pull requests touched the files that keep causing problems, and none were platform maintenance.');
  assert.equal(words.peopleAnswer({ scope, view: 'unblocking', rows: [{ measures: { hotspot_prs: 3, platform_prs: 1 } }, { measures: { hotspot_prs: 2, platform_prs: 0 } }] }),
    '2 people worked on files that keep causing problems, in 5 pull requests. 1 pull request went to platform maintenance.');
  // People, shipping and review
  assert.equal(words.peopleAnswer({ scope, view: 'shipping', rows: rows('merged', [3, 3]) }, { teamMerged: 8 }),
    '2 people merged 6 pull requests. The work is spread across the team. Another 2 came from people outside the team.');
  assert.equal(words.peopleAnswer({ scope, view: 'shipping', rows: rows('merged', [3, 3]) }, { teamMerged: 6 }), '2 people merged 6 pull requests. The work is spread across the team.');
  assert.equal(words.peopleAnswer({ scope, view: 'review', rows: rows('reviews_given', [10, 1, 1, 1, 1]) }), '5 people gave 14 reviews. Review work is uneven: 1 person gave half of all reviews.');
  assert.equal(words.outsideCount(123, 117), 6);
  assert.equal(words.outsideCount(100, 120), 0);
  assert.equal(words.outsideCount(null, 3), 0);

  // Overview
  const metrics = (merged, ttm, rounds = null) => [{ id: 'merged', ...merged }, { id: 'time_to_merge', ...ttm }, ...(rounds ? [{ id: 'review_rounds', ...rounds }] : [])];
  assert.equal(words.overviewAnswer({ scope, metrics: metrics({ value: 12, previous: 0 }, { value: 1, previous: null }), attention: [] }), '12 pull requests merged.');
  assert.equal(words.overviewAnswer({ scope, metrics: metrics({ value: 12, previous: 0 }, { value: 1, previous: null }), attention: [] }, 'lib'), '12 pull requests merged in lib.');
  assert.equal(words.overviewAnswer({ scope, metrics: metrics({ value: 130, previous: 100 }, { value: 1, previous: 1 }), attention: [] }), 'Shipping is up.');
  assert.equal(words.overviewAnswer({ scope, metrics: metrics({ value: 100, previous: 100 }, { value: 1, previous: 1 }, { value: 1.8, previous: 2.4 }), attention: [] }),
    'Shipping is steady. Review rounds fell to 1.8 per pull request.');
  assert.equal(words.overviewAnswer({ scope, metrics: metrics({ value: 100, previous: 100 }, { value: 1, previous: 1 }), attention: [{ kind: 'rounds_rising', repo: 'acme/lib', top_category: null }] }),
    'Shipping is steady. lib pull requests need more review rounds.');

  // Person
  const person = (extra) => ({ scope, person: { name: 'Mei Chen' }, shipping: { merged: 0 }, quality: {}, review: {}, team_median: { quality: { rounds_per_pr: 2 } }, ...extra });
  assert.equal(words.personAnswer(person({})), 'Mei merged no pull requests in the last 30 days.');
  assert.equal(words.personAnswer(person({ shipping: { merged: 4, by_type: { 'bug-fix': 2, enhancement: 2 } }, quality: { rounds_per_pr: 1.5 }, review: { reviews_given: 1 } })),
    'Mei shipped 4 pull requests. Mei’s pull requests needed 1.5 review rounds, below the team median of 2.0. Mei gave 1 review.');
  assert.equal(words.personAnswer(person({ shipping: { merged: 3, by_type: { 'bug-fix': 3 } }, quality: { rounds_per_pr: 2.1 } })),
    'Mei shipped 3 pull requests, mostly bug fixes. Mei’s pull requests needed 2.1 review rounds, close to the team median.');

  // Findings
  assert.equal(words.findingsAnswer({ scope, category: 'testing', categories: [] }, 'lib'), 'Tests wasn’t flagged in lib in the last 30 days.');
  assert.equal(words.findingsAnswer({ scope, category: null, categories: [{ category: 'testing', count: 4, previous: 6 }] }, 'lib'), 'Tests is flagged most: 4 times in lib in the last 30 days, down from 6.');

  // Models
  assert.equal(words.modelsAnswer({ scope, spend_usd: { value: 0, previous: 0 } }), 'No review spend in the last 30 days.');
  assert.equal(words.modelsAnswer({ scope, spend_usd: { value: 50, previous: 50 }, comparisons: [{}] }), 'Reviews cost $50.00.');
  assert.equal(words.modelsAnswer({ scope, spend_usd: { value: 5, previous: 2 }, comparisons: [{}] }), 'Reviews cost $5.00, up $3.00 on the 30 days before.');

  // Pull request
  assert.equal(words.prAnswer({ pr: { title: 'Try it', state: 'closed', rounds: 2 }, rounds: [], links: [] }), 'Try it. Closed without merging after 2 review rounds.');
  assert.equal(words.prAnswer({ pr: { title: 'Try it', author: 'Ann', state: 'open', created_at: '2026-11-10T12:00:00Z', rounds: 1 }, rounds: [{ flags: [{ status: null }] }], links: [] }),
    'Try it, by Ann. Open since November 10, 1 review round so far, with 1 flag.');

  // Attention, the other variants
  const fix = words.attentionWords({ kind: 'dropped_flag_bug', link: 'fix', pr: brief(7, 'Speed up'), bug_pr: { id: 'b', repo: 'acme/tree-service', number: 9, title: 'Fix it' }, flag: { category: null, claim: '', specialist: 'performance' } }, { hrefs });
  assert.equal(fix.title, 'A merged pull request needed a bug fix');
  assert.equal(fix.why, 'tree-service #7 was fixed by #9. The review flagged a problem, and the flag was dropped.');
  assert.deepEqual(fix.events[3], ['Fixed', '#9 Fix it']);
  const quiet = words.attentionWords({ kind: 'rounds_rising', repo_id: 'r1', repo: 'acme/lib', value: 2.6, previous: 2 }, { hrefs });
  assert.equal(quiet.why, '2.6 rounds per pull request, up from 2.0.');
  assert.equal(quiet.evidenceTitle, '');
  const one = words.attentionWords({ kind: 'specialist_dropped_bugs', specialist: 'ui', count: 1 }, { hrefs });
  assert.equal(one.why, '1 flag from the UI review was dropped and later traced to a bug.');
});

test('the pull request list names its filter in the answer line', async () => {
  const { listFilters, stageLabel } = await import(new URL('views/insights/prs.js', STATIC));
  const base = { scope, total: 49, prs: [] };
  assert.equal(words.prsAnswer({ ...base, stage: 'in_review' }, { repoName: 'android-tree' }), '49 pull requests merged in android-tree in the last 30 days, longest in review rounds first.');
  assert.equal(words.prsAnswer({ ...base, total: 31, push_class: 'fix_after_review' }, { repoName: 'android-tree' }), '31 pull requests with fixes pushed after review in android-tree in the last 30 days, newest first.');
  assert.equal(words.prsAnswer({ ...base, total: 1, push_class: 'rebase' }), 'One pull request rebased in the last 30 days, newest first.');
  assert.equal(words.prsAnswer({ ...base, total: 40, category: 'concurrency' }, { personName: 'Mei Chen' }), '40 pull requests opened or merged by Mei Chen with a concurrency flag in the last 30 days, newest first.');
  assert.equal(words.prsAnswer({ ...base, total: 0, push_class: 'unclassified' }, { repoName: 'lib' }), 'No pull requests with a push not yet classified in lib in the last 30 days.');
  assert.equal(words.prsAnswer({ scope, prs: [{}, {}] }), '2 pull requests opened or merged in the last 30 days, newest first.');
  assert.deepEqual(listFilters(new URLSearchParams('stage=in_review&push_class=rebase&category=testing')), { stage: 'in_review', push_class: '', category: 'testing', person: '' });
  assert.deepEqual(listFilters(new URLSearchParams('stage=nope&push_class=rebase')), { stage: '', push_class: 'rebase', category: '', person: '' });
  assert.equal(stageLabel('blocked'), 'Blocked by conflicts');
  assert.equal(stageLabel('waiting_on_ci'), 'Waiting on CI');
  assert.equal(hrefs.prs({ repo: 'r1', stage: 'in_review', days: 30 }), '#/insights/prs?repo=android-tree&stage=in_review&days=30');
});

test('comparisons and recommendations read as settings, not ids', async () => {
  const effort = { specialist: 'concurrency', variable: 'effort', baseline: { effort: 'xhigh' }, variant: { effort: 'high' } };
  assert.equal(words.comparisonQuestion(effort), 'Concurrency at high effort instead of xhigh effort');
  assert.equal(words.comparisonQuestion({ ...effort, specialist: 'test_adequacy', baseline: { effort: null } }), 'Test adequacy at high effort instead of the default effort');
  const model = { specialist: 'ui', variable: 'model', baseline: { model: 'claude-opus-4-6' }, variant: { model: 'claude-sonnet-4-6' } };
  assert.equal(words.comparisonQuestion(model), 'UI on Claude Sonnet 4.6 instead of Claude Opus 4.6');
  assert.equal(words.changeWords({ specialist: 'concurrency', effort: 'high' }, { effort: 'xhigh' }), 'Concurrency: effort from xhigh to high');
  assert.equal(words.changeWords({ specialist: 'concurrency', effort: 'high' }), 'Concurrency: effort from the default to high');
  assert.equal(words.changeWords({ specialist: 'simplicity', model: 'claude-sonnet-4-6' }, { model: 'claude-opus-4-6' }), 'Simplicity: model from Claude Opus 4.6 to Claude Sonnet 4.6');
  const mirrored = { specialist: 'ui', variable: 'model', variant: { model: 'claude-opus-4-6', alias: 'mirror' }, baseline: { model: 'claude-opus-4-6', alias: 'review' } };
  assert.equal(words.comparisonQuestion(mirrored), 'UI on Claude Opus 4.6 (mirror) instead of Claude Opus 4.6 (review)');
  assert.equal(words.changeWords({ specialist: 'ui', model: 'claude-opus-4-6', alias: 'mirror' }, mirrored.baseline), 'UI: model from Claude Opus 4.6 (review) to Claude Opus 4.6 (mirror)');
  assert.equal(words.changeWords({ specialist: 'ui', model: 'claude-sonnet-4-6', alias: 'fast' }, mirrored.baseline), 'UI: model from Claude Opus 4.6 to Claude Sonnet 4.6');
  const sentence = 'Over 40 pull requests, concurrency at high effort found everything xhigh found at 60% of the cost.';
  const base = { scope, spend_usd: { value: 50, previous: 50 }, comparisons: [{}] };
  assert.equal(words.modelsAnswer({ ...base, recommendations: [{ sentence }] }), `Reviews cost $50.00. ${sentence.replace(/^o/, 'O')}`);
  assert.equal(words.modelsAnswer({ ...base, recommendations: [{ sentence }, { sentence }] }), `Reviews cost $50.00. ${sentence} 1 more recommendation waits for approval.`);
  assert.equal(words.modelsAnswer({ ...base, recommendations: [{ sentence }, { sentence }, { sentence }] }), `Reviews cost $50.00. ${sentence} 2 more recommendations wait for approval.`);

  const { comparisonDetail } = await import(new URL('views/insights/models.js', STATIC));
  const row = { ...effort, min_prs: 30 };
  const detail = {
    prs: 12, result: { kind: 'early', words: 'Too early to tell' }, found: { both: 9, only_current: 2, only_variant: 1 },
    valid: { both: 8, only_current: 1, only_variant: 0 },
    sides: { current: { valid: 10, cost_usd: 1.2, latency_s: 300 }, variant: { valid: 9, cost_usd: 0.7, latency_s: 240 } },
    environment_shift: { recall: { mean: -0.05 }, cost_usd: { mean: 0.1 } }, arms: { skipped: 2 }, points: [],
  };
  const filled = comparisonDetail(row, detail);
  assert.equal(filled.why, 'Too early to tell. 12 pull requests compared of the 30 a result needs. 2 comparison runs skipped, for a budget, a cap, or a missing runner.');
  const shown = filled.evidence.flatMap(texts).join('|');
  assert.ok(shown.includes('Now: xhigh effort|Tried: high effort'));
  assert.ok(shown.includes('Raised only by this side|2|1') && shown.includes('Cost per review|$1.20|$0.70'));
  assert.ok(shown.includes('Real issues found only by this side|1|0'));
  assert.ok(shown.includes('more than twice its standard error'));
  assert.ok(shown.includes('changed results by 5 points less in real issues found and $0.10 more in cost per review on its own'));
  const { shiftWords } = await import(new URL('views/insights/models.js', STATIC));
  assert.equal(shiftWords({ recall: { mean: 0 }, cost_usd: { mean: 0 } }), 'Running later as a comparison didn’t change the results.');
  assert.equal(shiftWords({ recall: { mean: 0.0004 }, cost_usd: { mean: 0.001 } }), 'Running later as a comparison didn’t change the results.');
  const partial = shiftWords({ recall: { mean: -0.13 }, cost_usd: { mean: 0.001 } });
  assert.ok(partial.includes('13 points less in real issues found') && !partial.includes('$0.00') && !partial.includes(' and '));
});

test('a count axis keeps whole-number ticks for small counts', () => {
  assert.deepEqual(countAxis(2).labels, ['0', '1', '2']);
  assert.deepEqual(countAxis(0).labels, ['0', '1']);
  assert.deepEqual(countAxis(40).labels, yAxis(40).labels);
});
