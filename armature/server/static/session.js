let current = null;

export function setSession(sessionPayload) {
  current = sessionPayload;
}

export const currentSession = () => current;
export const user = () => current?.user ?? null;
export const isAdmin = () => Boolean(current?.user?.is_admin);
export const isSso = () => Boolean(current?.sso);
export const memberships = () => current?.memberships ?? [];
export const requiresTotp = () => Boolean(current?.auth?.totp);

export function ledTeamIds(sessionPayload = current) {
  return (sessionPayload?.memberships ?? []).filter((membership) => membership.scope_kind === 'team' && membership.role === 'team_lead').map((membership) => membership.scope_id);
}

export const canManageTeam = (teamId) => isAdmin() || ledTeamIds().includes(teamId);

// The server is the real gate. This only hides what a role cannot use.
export function navFor(sessionPayload) {
  if (!sessionPayload?.user) return [];
  if (sessionPayload.user.is_admin) return ['overview', 'gates', 'runs', 'subjects', 'trends', 'teams', 'projects', 'status', 'workflows', 'roles', 'packs', 'setup', 'users', 'secrets', 'runner-keys', 'runners', 'aws', 'webhooks', 'triggers', 'settings', 'account'];
  if (ledTeamIds(sessionPayload).length) return ['overview', 'gates', 'runs', 'subjects', 'trends', 'teams', 'projects', 'workflows', 'roles', 'packs', 'users', 'secrets', 'webhooks', 'triggers', 'account'];
  return ['overview', 'gates', 'runs', 'subjects', 'trends', 'teams', 'projects', 'workflows', 'roles', 'packs', 'account'];
}

// Step-up for a single sign-on account is a fresh sign-in at the provider, which returns to this page.
export function ssoStepUpPath(hash) {
  const back = /^#\//.test(hash || '') ? hash : '#/';
  return `/auth/oidc/login?step_up=1&return=${encodeURIComponent(back)}`;
}

// The reason stays in the server log. The page says only that single sign-on did not finish.
// Mirrors RETURN_HASH in routes_oidc.py, so a link from a pull request survives sign-in.
const RETURN_HASH = /^#\/[A-Za-z0-9/_?=&.%+-]{0,200}$/;

// The server's signed-out redirect to /login keeps the link's own hash, so that counts too.
export function returnHash(search, hash = '') {
  const back = new URLSearchParams(search || '').get('return') || hash || '';
  return RETURN_HASH.test(back) && back !== '#/' ? back : '';
}

export function loginPath(hash) {
  return RETURN_HASH.test(hash || '') && hash !== '#/' ? `/login?return=${encodeURIComponent(hash)}` : '/login';
}

export function ssoLoginPath(back) {
  return back ? `/auth/oidc/login?return=${encodeURIComponent(back)}` : '/auth/oidc/login';
}

export function signInIntro(methods = {}) {
  if (methods.password) return methods.totp ? 'Sign in with your username and password, plus your authenticator code once you have set one up.' : 'Sign in with your username and password.';
  if (methods.oidc) return 'Sign in with your organization account.';
  return 'No sign-in method is enabled on this server.';
}

export function signInNotice(search) {
  return new URLSearchParams(search || '').get('error') === 'sso' ? 'Single sign-on did not complete. Try again, or ask an admin for help.' : '';
}
