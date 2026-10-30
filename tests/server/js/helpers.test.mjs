import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { parseHash, hashFor } = await import(new URL('router.js', STATIC));
const { api, ApiError, basePath, configure, errorMessage, listOf, outcome, setCsrf } = await import(new URL('api.js', STATIC));
const { loginPath, navFor, returnHash, signInIntro, signInNotice, ssoLoginPath, ssoStepUpPath } = await import(new URL('session.js', STATIC));
const { secretFromOtpauth, tokenFromHash } = await import(new URL('otpauth.js', STATIC));
const { deleteKeyPath, keyCells, keysPath } = await import(new URL('views/runnerkeys.js', STATIC));

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  setCsrf(null);
  configure({ signedOut: () => {}, stepUp: async () => null });
});

function stubFetch(responses) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    const [status, payload] = responses.shift();
    return { status, json: async () => payload };
  };
  return calls;
}

test('parseHash defaults to the overview', () => {
  assert.deepEqual({ ...parseHash(''), query: String(parseHash('').query) }, { name: 'overview', args: [], query: '' });
  assert.equal(parseHash('#/').name, 'overview');
});

test('parseHash splits name, args, and query', () => {
  const parsed = parseHash('#/secrets/team%201?before=abc&status=failed');
  assert.equal(parsed.name, 'secrets');
  assert.deepEqual(parsed.args, ['team 1']);
  assert.equal(parsed.query.get('before'), 'abc');
  assert.equal(parsed.query.get('status'), 'failed');
});

test('parseHash keeps a malformed escape as written', () => {
  assert.deepEqual(parseHash('#/project/%E0%A4%A').args, ['%E0%A4%A']);
});

test('hashFor round-trips and drops empty params', () => {
  const hash = hashFor('runs', [], { team: 't/1', project: '', status: null, before: 'x y' });
  assert.equal(hash, '#/runs?team=t%2F1&before=x+y');
  const parsed = parseHash(hash);
  assert.equal(parsed.query.get('team'), 't/1');
  assert.equal(parsed.query.get('before'), 'x y');
  assert.deepEqual(parseHash(hashFor('project', ['a/b'])).args, ['a/b']);
});

test('a 401 from a credential check is an error, not a sign-out', async () => {
  for (const path of ['/api/login', '/api/step-up', '/api/account/password', '/api/invite/inspect', '/api/invite/accept']) {
    let signedOut = 0;
    configure({ signedOut: () => { signedOut += 1; } });
    setCsrf('tok');
    stubFetch([[401, { error: 'Password or code not accepted.' }]]);
    await assert.rejects(api(path, { method: 'POST', body: {} }), (err) => err instanceof ApiError && err.status === 401 && err.message === 'Password or code not accepted.');
    assert.equal(signedOut, 0, path);
  }
  assert.equal(outcome(401, {}, '/api/runs'), 'signed_out');
  assert.equal(outcome(401, {}, '/api/login'), 'error');
});

test('a wrong step-up proof shows its error without signing out', async () => {
  setCsrf('tok');
  let signedOut = 0;
  configure({ signedOut: () => { signedOut += 1; }, stepUp: async () => ({ password: 'pw', totp: '000000' }) });
  stubFetch([[403, { error: 'step_up_required' }], [401, { error: 'Password or code not accepted.' }]]);
  await assert.rejects(api('/api/secrets', { method: 'POST', body: {} }), (err) => err.status === 401);
  assert.equal(signedOut, 0);
});

test('ssoStepUpPath returns to the current view', () => {
  assert.equal(ssoStepUpPath('#/users?team=t'), '/auth/oidc/login?step_up=1&return=%23%2Fusers%3Fteam%3Dt');
  assert.equal(ssoStepUpPath(''), '/auth/oidc/login?step_up=1&return=%23%2F');
  assert.equal(ssoStepUpPath('javascript:x'), '/auth/oidc/login?step_up=1&return=%23%2F');
});

test('tokenFromHash accepts only a token shape', () => {
  assert.equal(tokenFromHash('#abcdefghijklmnopqrstuvwxyz_-0123'), 'abcdefghijklmnopqrstuvwxyz_-0123');
  assert.equal(tokenFromHash('#short'), '');
  assert.equal(tokenFromHash('#<script>alert(1)</script>xxxxxxxxxx'), '');
});

test('outcome classifies responses', () => {
  assert.equal(outcome(200, {}), 'ok');
  assert.equal(outcome(204, null), 'ok');
  assert.equal(outcome(401, { error: 'signed out' }), 'signed_out');
  assert.equal(outcome(403, { error: 'step_up_required' }), 'step_up');
  assert.equal(outcome(403, { error: 'forbidden' }), 'error');
  assert.equal(outcome(500, null), 'error');
});

test('errorMessage prefers the server text', () => {
  assert.equal(errorMessage(409, { error: 'Name taken.' }), 'Name taken.');
  assert.equal(errorMessage(502, null), 'Request failed (HTTP 502).');
});

test('listOf accepts a bare array or a keyed object', () => {
  assert.deepEqual(listOf([1], 'teams'), [1]);
  assert.deepEqual(listOf({ teams: [2] }, 'teams'), [2]);
  assert.deepEqual(listOf({ other: [] }, 'teams'), []);
  assert.deepEqual(listOf(null, 'teams'), []);
});

test('basePath reads the meta tag and drops a trailing slash', () => {
  const doc = { querySelector: () => ({ getAttribute: () => '/armature/' }) };
  assert.equal(basePath(doc), '/armature');
  assert.equal(basePath(undefined), '');
});

test('a POST fetches the CSRF token once and sends it', async () => {
  const calls = stubFetch([[200, { csrf: 'tok' }], [200, { ok: true }], [200, { ok: true }]]);
  await api('/api/teams', { method: 'POST', body: { name: 'a' } });
  await api('/api/teams', { method: 'POST', body: { name: 'b' } });
  assert.equal(calls[0].url, '/api/session');
  assert.equal(calls[1].init.headers['X-CSRF-Token'], 'tok');
  assert.equal(calls[1].init.credentials, 'same-origin');
  assert.equal(calls[1].init.body, '{"name":"a"}');
  assert.equal(calls.length, 3);
});

test('401 calls the signed-out handler and throws', async () => {
  let signedOut = 0;
  configure({ signedOut: () => { signedOut += 1; } });
  stubFetch([[401, { error: 'Sign in.' }]]);
  await assert.rejects(api('/api/runs'), (err) => err instanceof ApiError && err.status === 401);
  assert.equal(signedOut, 1);
});

test('step-up asks once, posts the proof, and retries', async () => {
  setCsrf('tok');
  let asked = 0;
  configure({ stepUp: async () => { asked += 1; return { password: 'pw', totp: '123456' }; } });
  const calls = stubFetch([[403, { error: 'step_up_required' }], [200, {}], [200, { saved: true }]]);
  assert.deepEqual(await api('/api/secrets', { method: 'POST', body: { name: 'K', value: 'v' } }), { saved: true });
  assert.equal(asked, 1);
  assert.equal(calls[1].url, '/api/step-up');
  assert.equal(calls[1].init.body, '{"password":"pw","totp":"123456"}');
  assert.equal(calls[2].url, '/api/secrets');
});

test('step-up is not retried twice', async () => {
  setCsrf('tok');
  configure({ stepUp: async () => ({ password: 'pw', totp: '1' }) });
  stubFetch([[403, { error: 'step_up_required' }], [200, {}], [403, { error: 'step_up_required' }]]);
  await assert.rejects(api('/api/secrets', { method: 'POST', body: {} }), (err) => err.status === 403);
});

test('a cancelled step-up changes nothing', async () => {
  setCsrf('tok');
  const calls = stubFetch([[403, { error: 'step_up_required' }]]);
  await assert.rejects(api('/api/users', { method: 'POST', body: {} }), /Cancelled/);
  assert.equal(calls.length, 1);
});

test('a network failure reads as unreachable', async () => {
  globalThis.fetch = async () => { throw new TypeError('offline'); };
  await assert.rejects(api('/api/session'), (err) => err.status === 0 && /reach/.test(err.message));
});

test('navFor hides what a role cannot use', () => {
  const lead = { user: { id: 1 }, memberships: [{ scope_kind: 'team', scope_id: 't', role: 'team_lead' }] };
  const viewer = { user: { id: 2 }, memberships: [{ scope_kind: 'project', scope_id: 'p', role: 'viewer' }] };
  assert.deepEqual(navFor({ user: { is_admin: true } }), ['overview', 'gates', 'runs', 'subjects', 'trends', 'teams', 'projects', 'status', 'workflows', 'workflow-guide', 'roles', 'packs', 'setup', 'users', 'secrets', 'runner-keys', 'runners', 'aws', 'webhooks', 'triggers', 'settings', 'account']);
  assert.deepEqual(navFor(lead), ['overview', 'gates', 'runs', 'subjects', 'trends', 'teams', 'projects', 'workflows', 'workflow-guide', 'roles', 'packs', 'users', 'secrets', 'webhooks', 'triggers', 'account']);
  assert.deepEqual(navFor(viewer), ['overview', 'gates', 'runs', 'subjects', 'trends', 'teams', 'projects', 'workflows', 'workflow-guide', 'roles', 'packs', 'account']);
  assert.deepEqual(navFor({ user: null }), []);
});

test('runner key paths pick the team or the instance', () => {
  assert.equal(keysPath('team 1'), '/api/teams/team%201/runner-keys');
  assert.equal(keysPath(null), '/api/runner-keys');
  assert.equal(deleteKeyPath('t', 'k/1'), '/api/teams/t/runner-keys/k%2F1/delete');
  assert.equal(deleteKeyPath(null, 'k'), '/api/runner-keys/k/delete');
});

test('runner key cells say when a key was never used', () => {
  const cells = keyCells({ name: 'host', created_at: '2026-10-23T12:00:00Z', created_by: null, last_used_at: null });
  assert.equal(cells[0], 'host');
  assert.match(cells[1], /^2026-10-2\d \d\d:\d\d$/);
  assert.deepEqual(cells.slice(2), ['—', 'Never']);
  assert.notEqual(keyCells({ name: 'h', created_at: null, last_used_at: '2026-10-23T12:00:00Z' })[3], 'Never');
});

test('secretFromOtpauth reads the secret parameter', () => {
  assert.equal(secretFromOtpauth('otpauth://totp/Armature:me?secret=ABC234&issuer=Armature'), 'ABC234');
  assert.equal(secretFromOtpauth('otpauth://totp/x'), '');
  assert.equal(secretFromOtpauth(undefined), '');
});

test('signInNotice is generic for a failed single sign-on', () => {
  assert.equal(signInNotice('?error=sso'), 'Single sign-on did not complete. Try again, or ask an admin for help.');
  assert.equal(signInNotice('?error=sso&detail=token%20expired'), 'Single sign-on did not complete. Try again, or ask an admin for help.');
  assert.equal(signInNotice('?error=other'), '');
  assert.equal(signInNotice(''), '');
});

test('signInIntro mentions the authenticator only when the server requires one', () => {
  assert.equal(signInIntro({ password: true }), 'Sign in with your username and password.');
  assert.match(signInIntro({ password: true, totp: true }), /authenticator code/);
  assert.equal(signInIntro({ oidc: true }), 'Sign in with your organization account.');
  assert.equal(signInIntro({}), 'No sign-in method is enabled on this server.');
});

test('sign-in keeps the view a link pointed at', () => {
  assert.equal(loginPath('#/run/d59ade37-59ff/log'), '/login?return=%23%2Frun%2Fd59ade37-59ff%2Flog');
  assert.equal(loginPath('#/'), '/login');
  assert.equal(loginPath(''), '/login');
  assert.equal(loginPath('#/x"><script>'), '/login');
  assert.equal(returnHash('?return=%23%2Frun%2Fabc%2Flog'), '#/run/abc/log');
  assert.equal(returnHash('?return=https%3A%2F%2Fevil.test'), '');
  assert.equal(returnHash('?return=%2F%2Fevil.test'), '');
  assert.equal(returnHash(''), '');
  assert.equal(returnHash('', '#/run/abc/log'), '#/run/abc/log');
  assert.equal(returnHash('?return=%23%2Fruns', '#/run/abc/log'), '#/runs');
  assert.equal(returnHash('', '#/x"><script>'), '');
  assert.equal(returnHash('', '#/'), '');
  assert.equal(ssoLoginPath('#/run/abc/log'), '/auth/oidc/login?return=%23%2Frun%2Fabc%2Flog');
  assert.equal(ssoLoginPath(''), '/auth/oidc/login');
});

const { alertKey, hiddenAlerts, visibleAlerts } = await import(new URL('views/overview.js', STATIC));

test('a dismissed alert stays hidden for good, even after newer runs fail', () => {
  const alert = { kind: 'failed', workflow: 'code-review', run_id: 'r1', message: '10 runs of code-review failed' };
  const lost = { kind: 'lost', run_id: 'r9', message: '2 runs were lost' };
  assert.equal(alertKey(alert), 'failed:code-review');
  assert.deepEqual(visibleAlerts([alert], {}), [alert]);
  assert.deepEqual(visibleAlerts([alert], { 'failed:code-review': true }), []);
  assert.deepEqual(visibleAlerts([{ ...alert, run_id: 'r2' }], { 'failed:code-review': 'r1' }), []);
  assert.deepEqual(visibleAlerts([alert, lost], { 'failed:code-review': true }), [lost]);
  assert.deepEqual(hiddenAlerts([alert, lost], { 'failed:code-review': true }), [alert]);
});
