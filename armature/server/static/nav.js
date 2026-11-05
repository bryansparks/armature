export const NAV_LABELS = {
  overview: 'Overview', gates: 'Waiting for you', runs: 'Runs', subjects: 'Pull requests', trends: 'Trends', teams: 'Teams', projects: 'Projects',
  status: 'Status', workflows: 'Workflows', 'workflow-guide': 'Create a workflow', roles: 'Roles and prompts', packs: 'Packs', setup: 'Setup',
  users: 'Users', secrets: 'Secrets', 'runner-keys': 'Runner keys', runners: 'Runners', aws: 'AWS', webhooks: 'Webhooks', triggers: 'Triggers',
  settings: 'Settings',
};

export const NAV_PARENT = { team: 'teams', project: 'projects', run: 'runs', subject: 'subjects', workflow: 'workflows', resident: 'workflows', 'new-workflow': 'workflows', document: 'roles', 'new-document': 'roles', webhook: 'webhooks', trigger: 'triggers' };

export const NAV_SECTIONS = [
  { id: 'inbox', title: 'Inbox', icon: 'inbox', views: ['overview', 'gates'] },
  { id: 'activity', title: 'Activity', icon: 'pulse', views: ['runs', 'subjects', 'trends'] },
  { id: 'workflows', title: 'Workflows', icon: 'flow', views: ['workflows', 'workflow-guide', 'triggers'] },
  { id: 'events', title: 'Events', icon: 'hook', views: ['webhooks'] },
  { id: 'library', title: 'Library', icon: 'doc', views: ['roles', 'packs'] },
  { id: 'settings', title: 'Settings', icon: 'gear', views: ['setup', 'users', 'teams', 'projects', 'secrets', 'runner-keys', 'runners', 'aws', 'settings', 'status'] },
];

export function visibleSections(allowed) {
  return NAV_SECTIONS.map((section) => ({ ...section, views: section.views.filter((view) => allowed.includes(view)) }))
    .filter((section) => section.views.length);
}

export function sectionFor(name, allowed) {
  const view = NAV_PARENT[name] || name;
  return visibleSections(allowed).find((section) => section.views.includes(view)) || null;
}

export function tabsFor(name, allowed) {
  const section = sectionFor(name, allowed);
  if (!section?.views.includes(name) || section.views.length < 2) return null;
  return section.views.map((view) => ({ view, label: NAV_LABELS[view], current: view === name }));
}
