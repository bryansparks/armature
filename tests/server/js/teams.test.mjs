import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { teamPageHref } = await import(new URL('views/teams.js', STATIC));
const { parseHash } = await import(new URL('router.js', STATIC));

test('teamPageHref round-trips a team id, with a tab appended as tabs() does', () => {
  assert.equal(teamPageHref(7), '#/team/7');
  assert.equal(parseHash(teamPageHref(7)).name, 'team');
  assert.deepEqual(parseHash(teamPageHref(7)).args, ['7']);
  assert.deepEqual(parseHash(`${teamPageHref(7)}/providers`).args, ['7', 'providers']);
});

test('teamPageHref keeps a slash in the id inside one route segment', () => {
  const href = teamPageHref('a/b');
  assert.equal(href, '#/team/a%2Fb');
  assert.deepEqual(parseHash(`${href}/runners`).args, ['a/b', 'runners']);
});

test('newWorkflowHref carries the team scope through parseHash for the new workflow page to preselect', async () => {
  const { newWorkflowHref } = await import(new URL('views/teams.js', STATIC));
  const { findScope } = await import(new URL('scopes.js', STATIC));
  const parsed = parseHash(newWorkflowHref(7));
  assert.equal(parsed.name, 'new-workflow');
  const scopes = [{ scope_kind: 'instance', scope_id: null }, { scope_kind: 'team', scope_id: '7' }, { scope_kind: 'project', scope_id: '12' }];
  assert.equal(findScope(scopes, parsed.query.get('scope')), scopes[1]);
  assert.equal(findScope(scopes, 'project:12'), scopes[2]);
  assert.equal(findScope(scopes, 'team:8'), undefined);
  assert.equal(findScope(scopes, null), undefined);
});
