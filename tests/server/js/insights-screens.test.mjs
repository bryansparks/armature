import assert from 'node:assert/strict';
import { test } from 'node:test';

// A small DOM: enough tree operations for the Insights page frame and its states to run under node.
class FakeNode {
  constructor() {
    this.parent = null;
  }

  get isConnected() { return Boolean(this.connected) || Boolean(this.parent?.isConnected); }

  replaceWith(...nodes) {
    const parent = this.parent;
    if (!parent) return;
    const at = parent.children.indexOf(this);
    for (const node of nodes) node.parent = parent;
    parent.children.splice(at, 1, ...nodes);
    this.parent = null;
  }
}
class FakeText extends FakeNode {
  constructor(text) {
    super();
    this.text = String(text);
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
    this.value = '';
    const classes = new Set();
    this.classList = { add: (name) => classes.add(name), remove: (name) => classes.delete(name), contains: (name) => classes.has(name) };
  }

  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  get firstChild() { return this.children[0] || null; }
  get childNodes() { return [...this.children]; }

  append(...nodes) {
    for (const node of nodes) {
      node.parent?.removeChild(node);
      node.parent = this;
      this.children.push(node);
    }
  }

  removeChild(node) {
    this.children = this.children.filter((child) => child !== node);
    node.parent = null;
  }

  replaceChildren(...nodes) {
    for (const child of this.children) child.parent = null;
    this.children = [];
    this.append(...nodes.map((node) => (typeof node === 'string' ? new FakeText(node) : node)));
  }

  set textContent(text) { this.replaceChildren(new FakeText(text)); }
  get textContent() { return texts(this).join(''); }
  querySelector() { return null; }
}
globalThis.Node = FakeNode;
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createElementNS: (_, tag) => new FakeElement(tag),
  createTextNode: (text) => new FakeText(text),
  activeElement: null,
  title: '',
};
globalThis.location = { hash: '', pathname: '/', search: '' };
globalThis.history = { replaceState: () => {} };
globalThis.requestAnimationFrame = () => 0;

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { page, reloader } = await import(new URL('views/insights/index.js', STATIC));
const { errorState } = await import(new URL('views/insights/ui.js', STATIC));
const people = await import(new URL('views/insights/people.js', STATIC));
const person = await import(new URL('views/insights/person.js', STATIC));
const pr = await import(new URL('views/insights/pr.js', STATIC));

function texts(node) {
  if (node instanceof FakeText) return [node.text];
  if (node instanceof FakeElement) return node.children.flatMap(texts);
  return [];
}
const words = (node) => texts(node).join(' ').replace(/\s+/g, ' ');
const find = (node, test) => (test(node) ? node : (node.children || []).map((child) => find(child, test)).find(Boolean) || null);
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

function context({ lead = true, api = async () => ({}), aborted = false } = {}) {
  return {
    lead, signal: { aborted }, query: new URLSearchParams(), args: [], team: { id: 't1' }, repos: [], slots: new Map(),
    teamControl: () => null, setQuery: async () => {}, findRepo: () => null, api,
    links: { people: () => '#/insights/people', person: (id) => `#/insights/person/${id}`, findings: () => '#/insights/findings', overview: () => '#/insights' },
  };
}

test('an error says what failed and what to do, by cause', () => {
  assert.equal(words(errorState('the team overview', { status: 0, message: 'Could not reach the server.' }, null)), 'Couldn’t load the team overview. The server didn’t respond.');
  assert.equal(words(errorState('people', { status: 403, message: 'Forbidden' }, null)), 'Couldn’t load people. You don’t have access to this.');
  assert.equal(words(errorState('findings', { status: 500, message: 'Database is locked.' }, null)), 'Couldn’t load findings. Database is locked.');
  const retry = find(errorState('findings', { status: 500, message: 'x' }, () => {}), (node) => node.tag === 'button');
  assert.equal(words(retry), 'Try again');
});

test('a failed load shows the error, and Try again loads the page again', async () => {
  const body = new FakeElement('div');
  let calls = 0;
  const load = async () => {
    calls += 1;
    if (calls === 1) throw Object.assign(new Error('Database is locked.'), { status: 500 });
    return { n: 3 };
  };
  await page(body, context(), { question: 'How is the team doing?', what: 'the team overview', load, draw: (data) => new FakeText(`Loaded ${data.n}`) });
  assert.ok(words(body).includes('Couldn’t load the team overview. Database is locked. Try again'));
  find(body, (node) => node.tag === 'button').listeners.click();
  await settle();
  assert.equal(calls, 2);
  assert.ok(words(body).includes('Loaded 3'));
  assert.ok(!words(body).includes('Couldn’t load'));
});

test('a page whose route was left draws nothing when its data arrives', async () => {
  const body = new FakeElement('div');
  await page(body, context({ aborted: true }), { question: 'Q?', what: 'x', load: async () => ({}), draw: () => new FakeText('Drawn') });
  assert.ok(!words(body).includes('Drawn'));
  assert.ok(find(body, (node) => node.attributes?.['aria-busy'] === 'true'));
});

test('a reload that finishes after a newer one never replaces it', async () => {
  const body = new FakeElement('div');
  body.connected = true;
  const pending = [];
  const reload = reloader(body, (fresh) => new Promise((resolve) => pending.push(() => { fresh.append(new FakeText(`render ${pending.length}`)); resolve(); })), { aborted: false });
  const first = reload();
  const second = reload();
  assert.ok(body.classList.contains('in-stale'));
  pending[1]();
  assert.equal(await second, true);
  assert.equal(words(body), 'render 2');
  pending[0]();
  assert.equal(await first, false);
  assert.equal(words(body), 'render 2');
  assert.ok(!body.classList.contains('in-stale'));
});

test('People shows the no-permission state below team lead, without asking the server', async () => {
  const body = new FakeElement('div');
  let asked = false;
  await people.render(body, context({ lead: false, api: async () => { asked = true; return {}; } }));
  assert.equal(asked, false);
  assert.ok(words(body).includes('People pages are visible to team leads.'));
  assert.ok(words(body).includes('Ask an admin for the team lead role to see how everyone contributes.'));
});

test('A person page someone may not see, and one that does not exist, each get their own state', async () => {
  const denied = new FakeElement('div');
  await person.render(denied, { ...context({ lead: false, api: async () => { throw Object.assign(new Error('no'), { status: 403 }); } }), args: ['person', 'p9'] });
  assert.ok(words(denied).includes('People pages are visible to team leads and to the person themself.'));
  assert.ok(words(denied).includes('Ask an admin for the team lead role'));

  const missing = new FakeElement('div');
  await person.render(missing, { ...context({ api: async () => { throw Object.assign(new Error('no'), { status: 404 }); } }), args: ['person', 'gone'] });
  assert.ok(words(missing).includes('No such person'));
  assert.ok(words(missing).includes('This person isn’t on the team, or was merged into someone else.'));
  assert.ok(find(missing, (node) => node.tag === 'a' && node.attributes.href === '#/insights/people'));

  const broken = new FakeElement('div');
  await person.render(broken, { ...context({ api: async () => { throw Object.assign(new Error('Database is locked.'), { status: 500 }); } }), args: ['person', 'p1'] });
  assert.ok(words(broken).includes('Couldn’t load this person. Database is locked.'));
});

test('the Person route takes a person id or a GitHub login', async () => {
  const asked = [];
  const api = async (path) => { asked.push(path); throw Object.assign(new Error('no'), { status: 404 }); };
  await person.render(new FakeElement('div'), { ...context({ api }), args: ['person', 'mei-chen'] });
  await person.render(new FakeElement('div'), { ...context({ api }), args: ['person', 'ed91e2c1-f35a-4cce-bb05-ab49df2c7be9'] });
  assert.deepEqual(asked, ['/api/teams/t1/insights/people/by-login/mei-chen?days=30', '/api/teams/t1/insights/people/ed91e2c1-f35a-4cce-bb05-ab49df2c7be9?days=30']);
  assert.equal(person.isPersonId('ED91E2C1-F35A-4CCE-BB05-AB49DF2C7BE9'), true);
  assert.equal(person.isPersonId('octocat'), false);
});

test('the pull request route reads repo and number, and an unknown repo is a missing page without a request', async () => {
  const asked = [];
  const api = async (path) => { asked.push(path); throw Object.assign(new Error('no'), { status: 404 }); };
  const repoCtx = (findRepo) => ({ ...context({ api }), findRepo, links: { ...context().links, repo: () => '#/insights/repo/lib', pr: () => null } });
  await pr.render(new FakeElement('div'), { ...repoCtx((token) => (token === 'lib' ? { id: 'r1', name: 'lib' } : null)), args: ['pr', 'lib', '12'] });
  await pr.render(new FakeElement('div'), { ...repoCtx(() => null), args: ['pr', 'p77'] });
  assert.deepEqual(asked, ['/api/teams/t1/insights/repos/r1/prs/12', '/api/teams/t1/insights/prs/p77']);
  const missing = new FakeElement('div');
  await pr.render(missing, { ...repoCtx(() => null), args: ['pr', 'nope', '12'] });
  assert.equal(asked.length, 2);
  assert.ok(words(missing).includes('No such pull request'));
});

const models = await import(new URL('views/insights/models.js', STATIC));
const tagged = (node, tag) => {
  const found = [];
  const walk = (n) => { if (n.tag === tag) found.push(n); (n.children || []).forEach(walk); };
  walk(node);
  return found;
};
const buttonsOf = (node) => tagged(node, 'button');

test('a recommendation offers approval by who may approve, and says what happened', async () => {
  const effort = { id: 'r1', experiment_id: 'e1', sentence: 'Over 30 pull requests, concurrency at high effort found as much as xhigh found.', change: { specialist: 'concurrency', effort: 'high' }, evidence: { prs: 30 }, can_approve: true };
  const model = { ...effort, id: 'r2', change: { specialist: 'simplicity', model: 'claude-sonnet-4-6' }, can_approve: false };
  const posted = [];
  let fail = null;
  const ctx = { ...context(), post: async (path) => { posted.push(path); if (fail) throw fail; return {}; } };
  const list = models.recommendationList([effort, model], [{ id: 'e1', baseline: { effort: 'xhigh' } }], ctx);
  const [first, second] = list.children;
  assert.ok(words(first).includes('Concurrency: effort from xhigh to high'));
  assert.deepEqual(buttonsOf(first).map(words), ['Approve change', 'Dismiss']);
  assert.ok(words(second).includes('Needs an admin to approve'));
  assert.deepEqual(buttonsOf(second).map(words), ['Dismiss']);

  fail = Object.assign(new Error('Cancelled. Nothing changed.'), { status: 403, payload: { error: 'step_up_required' } });
  await buttonsOf(first)[0].listeners.click();
  assert.ok(words(first).includes('Couldn’t approve it. Cancelled. Nothing changed.'));
  fail = Object.assign(new Error('Forbidden'), { status: 403, payload: { error: 'step_up_required' } });
  await buttonsOf(first)[0].listeners.click();
  assert.ok(words(first).includes('Sign in again to approve.'));
  assert.equal(buttonsOf(first)[0].disabled, false);
  fail = null;
  await buttonsOf(first)[0].listeners.click();
  assert.ok(words(first).includes('Approved. The change applies to new reviews.'));
  assert.deepEqual(buttonsOf(first), []);
  assert.ok(posted.at(-1).endsWith('/recommendations/r1/approve'));

  const viewer = models.recommendationList([effort], [], { ...context({ lead: false }), post: async () => ({}) });
  assert.deepEqual(buttonsOf(viewer), []);
  assert.ok(words(viewer).includes('A team lead approves this change.'));
});

test('the add comparison form shows the current setting, leaves it out, and starts only once complete', async () => {
  const choices = { specialists: ['concurrency'], efforts: ['low', 'medium', 'high', 'xhigh', 'max'], defaults: { daily_budget_usd: 25 },
    current: { concurrency: { alias: 'review', model: 'claude-opus-4-6', effort: 'xhigh', models: ['claude-opus-4-6', 'claude-sonnet-4-6'] } } };
  const posted = [];
  const ctx = { ...context({ api: async () => choices }), post: async (path, body) => { posted.push([path, body]); return {}; } };
  const block = models.addComparison(ctx);
  const [toggle] = buttonsOf(block);
  assert.equal(toggle.className || toggle.attributes.class, 'in-button is-quiet');
  // A browser reads the hidden attribute as the property. This small DOM keeps them apart.
  find(block, (node) => node.attributes?.id === 'in-add-comparison').hidden = true;
  await toggle.listeners.click();
  const start = buttonsOf(block).find((node) => words(node) === 'Start comparison');
  assert.ok(words(block).includes('spends at most $25 a day'));
  assert.equal(start.disabled, true);
  assert.ok(words(block).includes('Choose a reviewer first.'));
  const selects = tagged(block, 'select');
  const change = (node, value) => { node.value = value; node.listeners.change(); };
  change(selects[0], 'concurrency');
  assert.ok(words(block).includes('Concurrency runs Claude Opus 4.6 at xhigh effort now.'));
  const options = (node) => tagged(node, 'option').map((option) => option.attributes.value);
  const valueSelect = () => tagged(block, 'select').at(-1);
  assert.ok(!options(valueSelect()).includes('xhigh') && options(valueSelect()).includes('high'));
  assert.equal(start.disabled, true);
  assert.ok(words(block).includes('Choose what to change it to.'));
  change(valueSelect(), 'high');
  assert.equal(start.disabled, false);
  change(selects[1], 'model');
  assert.ok(!options(valueSelect()).includes('claude-opus-4-6') && options(valueSelect()).includes('claude-sonnet-4-6'));
  change(valueSelect(), 'claude-sonnet-4-6');
  await start.listeners.click();
  assert.deepEqual(posted[0][1], { specialist: 'concurrency', variable: 'model', variant: 'claude-sonnet-4-6' });
});

test('an admin may compare an alias on another provider, labeled with that provider', async () => {
  const choices = { specialists: ['concurrency'], efforts: ['high'], defaults: { daily_budget_usd: 25 },
    current: { concurrency: { alias: 'review', model: 'claude-opus-4-6', effort: 'xhigh', models: ['claude-opus-4-6'],
      aliases: [{ alias: 'fast', model: 'gateway-model', provider: 'anthropic' }] } } };
  const posted = [];
  const ctx = { ...context({ api: async () => choices }), post: async (path, body) => { posted.push([path, body]); return {}; } };
  const block = models.addComparison(ctx);
  find(block, (node) => node.attributes?.id === 'in-add-comparison').hidden = true;
  await buttonsOf(block)[0].listeners.click();
  const change = (node, value) => { node.value = value; node.listeners.change(); };
  const selects = tagged(block, 'select');
  change(selects[0], 'concurrency');
  change(selects[1], 'model');
  const valueSelect = tagged(block, 'select').at(-1);
  const option = tagged(valueSelect, 'option').find((node) => node.attributes.value === '@fast');
  assert.equal(words(option), 'Gateway Model on Anthropic API (fast)');
  change(valueSelect, '@fast');
  await buttonsOf(block).find((node) => words(node) === 'Start comparison').listeners.click();
  assert.deepEqual(posted[0][1], { specialist: 'concurrency', variable: 'model', alias: 'fast' });
});

const fixesScreen = await import(new URL('views/insights/fixes.js', STATIC));
const WAITING = {
  id: 'f1', title: 'Fix: Concurrency in app/src', state: 'waiting', kind: 'check', gate_id: 'g1', nonce: 'n1', may_decide: true,
  pattern: { label: 'concurrency', repo: 'acme/android-tree', repo_id: 'r1', area: 'app/src', flags: 23, days: 28 },
  diff: { files: 2, additions: 3, deletions: 1, more: true, lines: [['file', 'detekt.yml'], ['add', 'rule: on'], ['del', 'rule: off']] },
  has_patch: true, expires_at: '2026-11-20T12:00:00Z',
};

test('a waiting fix says what it fixes, how, and its first changed lines, and approves in place', async () => {
  const posted = [];
  let fail = null;
  const ctx = { ...context(), post: async (path, body) => { posted.push([path, body]); if (fail) throw fail; return {}; } };
  const [item] = fixesScreen.fixList([WAITING], 'waiting', ctx).children;
  assert.ok(words(item).includes('Concurrency in android-tree app/src, flagged 23 times in 4 weeks.'));
  assert.ok(words(item).includes('A new automated check, so the problem is caught before review.'));
  assert.ok(words(item).includes('2 files changed, 3 additions, 1 deletion'));
  const code = tagged(item, 'pre')[0];
  assert.deepEqual(code.children.map(words), ['detekt.yml', '+rule: on', '-rule: off']);
  // It scrolls sideways, so a keyboard reaches it and a screen reader names it.
  assert.deepEqual([code.attributes.tabindex, code.attributes.role, code.attributes['aria-label']], ['0', 'region', 'The first changed lines']);
  assert.ok(tagged(item, 'a').some((a) => a.attributes.href === '/api/teams/t1/insights/fixes/f1/patch' && words(a) === 'See the whole change'));
  assert.ok(words(item).includes('Waits for a decision until November 20'));
  assert.deepEqual(buttonsOf(item).map(words), ['Approve fix', 'Decline', 'Decline fix', 'Cancel']);
  fail = Object.assign(new Error('Forbidden'), { status: 403, payload: { error: 'step_up_required' } });
  await buttonsOf(item)[0].listeners.click();
  assert.ok(words(item).includes('Sign in again to approve.'));
  fail = null;
  await buttonsOf(item)[0].listeners.click();
  assert.deepEqual(posted.at(-1), ['/api/gates/g1/approve', { nonce: 'n1' }]);
  assert.ok(words(item).includes('Approved. Armature pushes it and opens a draft pull request in android-tree.'));
  assert.ok(!words(item).includes('Waits for a decision'));
  assert.deepEqual(buttonsOf(item), []);
});

test('declining a fix asks for an optional one-line reason and sends it as the gate note', async () => {
  const posted = [];
  const ctx = { ...context(), post: async (path, body) => { posted.push([path, body]); return {}; } };
  const [item] = fixesScreen.fixList([WAITING], 'waiting', ctx).children;
  const [, decline, confirm] = buttonsOf(item);
  decline.listeners.click();
  tagged(item, 'input')[0].value = '  Not this sprint.  ';
  await confirm.listeners.click();
  assert.deepEqual(posted, [['/api/gates/g1/reject', { nonce: 'n1', note: 'Not this sprint.' }]]);
  assert.ok(words(item).includes('Declined. Nothing reaches android-tree.'));
  assert.ok(!words(item).includes('Waits for a decision'));
  const [viewer] = fixesScreen.fixList([{ ...WAITING, nonce: undefined, may_decide: false }], 'waiting', ctx).children;
  assert.deepEqual(buttonsOf(viewer), []);
  assert.ok(words(viewer).includes('A team lead approves this fix.'));
});

test('in progress, done, and declined fixes say where each stands', () => {
  assert.equal(fixesScreen.doneWords({ state: 'merged', merged_at: '2026-10-11T00:00:00Z', flags_before: 23, flags_after: 4 }),
    'Merged October 11. Flagged 23 times in the 30 days before, 4 since.');
  assert.equal(fixesScreen.doneWords({ state: 'closed' }), 'Closed without merging.');
  assert.equal(fixesScreen.declinedWords({ state: 'declined', decided_by: 'mei', decided_at: '2026-11-02T00:00:00Z', decline_reason: 'Not now.' }),
    'Declined by mei on November 2. “Not now.”');
  assert.equal(fixesScreen.declinedWords({ state: 'expired', decided_at: '2026-11-02T00:00:00Z' }), 'Nobody decided by November 2, so it was dropped.');
  assert.equal(fixesScreen.progressWords({ state: 'approved' }), 'Approved. Armature is pushing it and opening the draft pull request.');
  const [opened] = fixesScreen.fixList([{ ...WAITING, state: 'opened', pr: { number: 958, url: 'https://github.com/acme/x/pull/958' }, updated_at: new Date().toISOString() }], 'in_progress', context()).children;
  assert.ok(words(opened).includes('Draft pull request #958 · Open as a draft, opened today.'));
});

const reportScreen = await import(new URL('views/insights/report.js', STATIC));
const { tile } = await import(new URL('views/insights/ui.js', STATIC));
const REPORT = {
  version: 1, day: '2026-11-16', zone: 'America/Denver', open_runs: 1,
  headline: {
    runs: { value: 160, previous: 140, avg_7d: 150 }, cost_usd: { value: 776.25, previous: 700, avg_7d: 650 }, failed: { value: 2, previous: 0, avg_7d: 1 }, finished: 158,
    by_workflow: [{ workflow: 'code-review', runs: 103, cost_usd: 605.75, p50: 600, p90: 959 }],
    models: [{ model: 'claude-opus-5-5', calls: 1267, cost_usd: 776.23 }],
    series: { days: ['2026-11-15', '2026-11-16'], workflows: ['code-review'], runs: [140, 160], cost_usd: [700, 776.25], runs_by_workflow: { 'code-review': [140, 160] }, spend_by_workflow: { 'code-review': [700, 776.25] } },
  },
  findings: {
    errors: [{ pattern: 'JSON parse failed at offset #', count: 5, runs: 5, workflows: ['code-review'] }],
    failing: [], slowest: [{ run_id: 'r1', workflow: 'code-review', subject: 'acme/app#12', seconds: 959 }], costliest: [], dry: { runs: 42, empty: 42, by_workflow: [{ workflow: 'code-review-next', runs: 42 }] },
    churn: { prs: 1, multi_round: 1, max_rounds: 6, findings: 3, blocking: 1, non_blocking: 2, repeats: 1, late: 1,
      top: [{ subject: 'acme/app#12', rounds: 6, runs: 6, title: '<img src=x onerror=alert(1)>', link: { repo: 'acme/app', number: 12, url: 'https://github.com/acme/app/pull/12' } },
        { subject: 'evil', rounds: 2, runs: 2, link: { repo: 'x', number: 1, url: 'javascript:alert(1)//github.com/a/b/pull/1' } }] },
  },
  learned: [{ section: 'headline', text: '160 runs, up 7% on the 7-day average of 150.' }],
  changes: { deploys: { lines: [{ at: '2026-11-16T10:09:00-07:00', line: 'live: be3bbbe' }], live_sha: 'be3bbbe' }, versions: [], config: [{ at: '2026-11-16T16:00:00+00:00', action: 'trigger.update', keys: ['config.cron'] }], fingerprint: { baseline: true, files: 12 } },
};

function reportContext(responses) {
  const asked = [];
  return { ctx: { ...context({ api: async (path) => { asked.push(path); return responses[path]; } }), team: null }, asked };
}

test('the daily report says when it builds while no report exists, and asks for no day', async () => {
  const { ctx, asked } = reportContext({ '/api/insights/daily-reports': { days: [], timezone: 'America/Denver', builds_after_hour: 2 } });
  const body = new FakeElement('div');
  await reportScreen.render(body, ctx);
  assert.deepEqual(asked, ['/api/insights/daily-reports']);
  assert.ok(words(body).includes('No report yet.'));
  assert.ok(words(body).includes('Today’s report builds after 2 AM America/Denver.'));
  assert.equal(reportScreen.emptyWords({ builds_after_hour: 0, timezone: 'UTC' }), 'Today’s report builds after midnight UTC.');
});

test('the daily report shows the newest day, its takeaways, and every section, as text only', async () => {
  const { ctx, asked } = reportContext({
    '/api/insights/daily-reports': { days: [{ day: '2026-11-16' }, { day: '2026-11-15' }], timezone: 'America/Denver', builds_after_hour: 2 },
    '/api/insights/daily-reports/2026-11-16': { day: '2026-11-16', data: REPORT },
  });
  const body = new FakeElement('div');
  await reportScreen.render(body, ctx);
  assert.deepEqual(asked, ['/api/insights/daily-reports', '/api/insights/daily-reports/2026-11-16']);
  const text = words(body);
  assert.ok(text.includes('160 runs, $776.25 spent, 2 runs failed.'));
  assert.ok(text.includes('Monday, November 16'));
  assert.ok(text.includes('160 runs, up 7% on the 7-day average of 150.'));
  assert.ok(text.includes('vs the day before'));
  assert.ok(text.includes('JSON parse failed at offset #'));
  assert.ok(text.includes('42 of 42 dry runs left no finding and no artifact.'));
  assert.ok(text.includes('acme/app#12 · <img src=x onerror=alert(1)>'));
  assert.ok(text.includes('trigger.update: config.cron'));
  assert.ok(text.includes('First report, so its 12 files are the baseline.'));
  const links = tagged(body, 'a').map((node) => node.attributes.href);
  assert.ok(links.includes('https://github.com/acme/app/pull/12'));
  assert.ok(!links.some((href) => /javascript/.test(href || '')));
  assert.ok(!tagged(body, 'img').length);
  const picker = tagged(body, 'select')[0];
  assert.deepEqual(tagged(picker, 'option').map((node) => node.attributes.value), ['2026-11-16', '2026-11-15']);
});

test('a report row from an older version, missing keys, still renders with empty sections', () => {
  const nodes = reportScreen.draw({ day: '2026-11-10' }, {}, { aborted: false });
  const holder = new FakeElement('div');
  holder.append(...nodes);
  const text = words(holder);
  for (const line of ['Nothing stood out.', 'No runs in the 8 days to this one.', 'No step failed with an error.', 'No review rounds were recorded on this day.', 'No deploys on this day.']) {
    assert.ok(text.includes(line), line);
  }
  assert.equal(reportScreen.answer({}), 'No numbers in this report.');
  assert.equal(reportScreen.pickDay([{ day: '2026-11-16' }], '2026-11-01'), '2026-11-16');
  assert.equal(reportScreen.prHref({ url: 'https://github.com/acme/app/pull/3' }), 'https://github.com/acme/app/pull/3');
  assert.equal(reportScreen.prHref({ url: 'https://github.com.evil.test/acme/app/pull/3' }), null);
  assert.equal(reportScreen.prHref({ url: 'https://github.com/acme/app/pull/3/../../x' }), null);
});

test('a tile can compare with a named period instead of a number of days', () => {
  const node = tile({ measure: { label: 'Runs', unit: 'count', digits: 0, better: 'up', change: 'percent', def: 'Runs.' }, value: 160, previous: 140, days: 1, period: 'the day before' });
  assert.ok(words(node).includes('vs the day before'));
  assert.ok(words(node).includes('compared with the day before'));
});
