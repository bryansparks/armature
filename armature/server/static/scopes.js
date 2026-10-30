import { ledTeamIds } from './session.js';

const PROJECT_EDITORS = new Set(['project_lead', 'team_lead']);

export const sameScope = (first, second) => first.scope_kind === second.scope_kind && String(first.scope_id ?? '') === String(second.scope_id ?? '');

export const scopeKey = (scope) => `${scope.scope_kind}:${scope.scope_id ?? ''}`;

export const findScope = (scopes, key) => scopes.find((scope) => scopeKey(scope) === key);

// Where this user may create or fork a definition. The server checks again on every write.
export function creatableScopes(sessionPayload, teams = [], projectsByTeam = new Map()) {
  const isAdmin = Boolean(sessionPayload?.user?.is_admin);
  const led = new Set(ledTeamIds(sessionPayload).map(String));
  const scopes = isAdmin ? [{ scope_kind: 'instance', scope_id: null, label: 'Default (all teams)' }] : [];
  for (const team of teams) {
    if (!isAdmin && !led.has(String(team.id))) continue;
    scopes.push({ scope_kind: 'team', scope_id: String(team.id), label: team.name });
    for (const project of projectsByTeam.get(String(team.id)) || []) {
      scopes.push({ scope_kind: 'project', scope_id: String(project.id), label: `${team.name} / ${project.name}` });
    }
  }
  for (const membership of sessionPayload?.memberships || []) {
    if (membership.scope_kind !== 'project' || !PROJECT_EDITORS.has(membership.role)) continue;
    const scope = { scope_kind: 'project', scope_id: String(membership.scope_id), label: membership.scope_name || 'Project' };
    if (!scopes.some((known) => sameScope(known, scope))) scopes.push(scope);
  }
  return scopes;
}

export function mayCreate(sessionPayload) {
  if (sessionPayload?.user?.is_admin) return true;
  return (sessionPayload?.memberships || []).some((membership) => membership.role === 'team_lead'
    || (membership.scope_kind === 'project' && PROJECT_EDITORS.has(membership.role)));
}
