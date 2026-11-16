import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { NAV_LABELS, NAV_PARENT, NAV_SECTIONS, sectionFor, tabsFor, visibleSections } = await import(new URL('nav.js', STATIC));

const EVERY_VIEW = ['overview', 'gates', 'runs', 'subjects', 'trends', 'teams', 'projects', 'status', 'workflows', 'workflow-guide', 'roles', 'packs',
  'setup', 'users', 'secrets', 'runner-keys', 'runners', 'aws', 'webhooks', 'triggers', 'settings'];

test('every sidebar view is in exactly one section', () => {
  const placed = NAV_SECTIONS.flatMap(({ views }) => views);
  assert.deepEqual([...placed].sort(), [...EVERY_VIEW].sort());
  for (const view of placed) assert.ok(NAV_LABELS[view], `${view} has a label`);
});

test('a view maps to its section', () => {
  assert.equal(sectionFor('gates', EVERY_VIEW).id, 'inbox');
  assert.equal(sectionFor('webhooks', EVERY_VIEW).id, 'events');
  assert.equal(sectionFor('runner-keys', EVERY_VIEW).id, 'settings');
});

test('every detail route maps to its parent section', () => {
  const expected = { team: 'settings', project: 'settings', run: 'activity', subject: 'activity', workflow: 'workflows', resident: 'workflows',
    'new-workflow': 'workflows', document: 'library', 'new-document': 'library', webhook: 'events', trigger: 'workflows' };
  assert.deepEqual(Object.keys(expected).sort(), Object.keys(NAV_PARENT).sort());
  for (const [name, id] of Object.entries(expected)) assert.equal(sectionFor(name, EVERY_VIEW)?.id, id, name);
});

test('every detail parent is in a section', () => {
  const placed = new Set(NAV_SECTIONS.flatMap(({ views }) => views));
  for (const parent of Object.values(NAV_PARENT)) assert.ok(placed.has(parent), parent);
});

test('account and unknown names have no section', () => {
  assert.equal(sectionFor('account', [...EVERY_VIEW, 'account']), null);
  assert.equal(sectionFor('nope', EVERY_VIEW), null);
});

test('a section lists only allowed views, in order', () => {
  const section = sectionFor('workflows', ['triggers', 'workflows']);
  assert.deepEqual(section, { id: 'workflows', title: 'Workflows', icon: 'flow', views: ['workflows', 'triggers'] });
});

test('a section with no allowed view is null', () => {
  assert.equal(sectionFor('setup', ['overview', 'gates']), null);
  assert.equal(sectionFor('webhooks', []), null);
  assert.deepEqual(visibleSections(['runs']).map(({ id }) => id), ['activity']);
});

test('the sidebar links to the first allowed view of a section', () => {
  const workflows = visibleSections(['triggers', 'overview']).find(({ id }) => id === 'workflows');
  assert.equal(workflows.views[0], 'triggers');
});

test('the landing page is the first page of its section', async () => {
  const { DEFAULT_VIEW } = await import(new URL('router.js', STATIC));
  assert.equal(sectionFor(DEFAULT_VIEW, EVERY_VIEW).views[0], DEFAULT_VIEW);
});

test('tabs show only on a section page with more than one allowed view', () => {
  assert.equal(tabsFor('run', EVERY_VIEW), null);
  assert.equal(tabsFor('webhooks', EVERY_VIEW), null);
  assert.equal(tabsFor('runs', ['runs', 'gates']), null);
  assert.deepEqual(tabsFor('runs', EVERY_VIEW), [
    { view: 'runs', label: 'Runs', current: true },
    { view: 'subjects', label: 'Pull requests', current: false },
    { view: 'trends', label: 'Trends', current: false },
  ]);
});
