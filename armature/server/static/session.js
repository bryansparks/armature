let current = null;

export function setSession(sessionPayload) {
  current = sessionPayload;
}

export const currentSession = () => current;
export const user = () => current?.user ?? null;
export const isAdmin = () => Boolean(current?.user?.is_admin);
export const isSso = () => Boolean(current?.sso);
export const memberships = () => current?.memberships ?? [];

export function ledTeamIds(sessionPayload = current) {
  return (sessionPayload?.memberships ?? []).filter((membership) => membership.scope_kind === 'team' && membership.role === 'team_lead').map((membership) => membership.scope_id);
}

export const canManageTeam = (teamId) => isAdmin() || ledTeamIds().includes(teamId);

// The server is the real gate. This only hides what a role cannot use.
export function navFor(sessionPayload) {
  if (!sessionPayload?.user) return [];
  if (sessionPayload.user.is_admin) return ['overview', 'runs', 'subjects', 'trends', 'teams', 'status', 'workflows', 'roles', 'packs', 'users', 'secrets', 'runner-keys', 'settings', 'account'];
  if (ledTeamIds(sessionPayload).length) return ['overview', 'runs', 'subjects', 'trends', 'teams', 'workflows', 'roles', 'packs', 'users', 'secrets', 'account'];
  return ['overview', 'runs', 'subjects', 'trends', 'teams', 'workflows', 'roles', 'packs', 'account'];
}

// Step-up for a single sign-on account is a fresh sign-in at the provider, which returns to this page.
export function ssoStepUpPath(hash) {
  const back = /^#\//.test(hash || '') ? hash : '#/';
  return `/auth/oidc/login?step_up=1&return=${encodeURIComponent(back)}`;
}

// The reason stays in the server log. The page says only that single sign-on did not finish.
export function signInNotice(search) {
  return new URLSearchParams(search || '').get('error') === 'sso' ? 'Single sign-on did not complete. Try again, or ask an admin for help.' : '';
}
