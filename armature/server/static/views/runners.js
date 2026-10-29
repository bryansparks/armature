import { api, listOf } from '../api.js';
import { card, clear, element, empty, errorBox, loading, pageHeader, statusChip, table, when } from '../dom.js';

export const RUNNERS_PATH = '/api/runners';
const WAITING = ['queued', 'provisioning'];

export function waitReason(job, now = Date.now()) {
  if (job.warning) return job.warning;
  if (job.status === 'provisioning') return 'Starting an environment.';
  if (job.not_before && new Date(job.not_before).getTime() > now) return `Waiting for a quiet period until ${when(job.not_before)}.`;
  if (job.concurrency_key) return `Waiting for a runner, one at a time for ${job.concurrency_key}.`;
  return 'Waiting for a runner.';
}

export function queueCells(job) {
  return [job.workflow || '—', statusChip(job.status), when(job.created_at), waitReason(job)];
}

export function runnerCells(runner) {
  const capabilities = Array.isArray(runner.capabilities) ? runner.capabilities : [];
  const name = runner.same_user
    ? [runner.name, ' ', element('span', { class: 'chip warn', title: 'Agent sessions run as the user that started this runner, unisolated.' }, 'Same user')]
    : runner.name;
  return [
    name,
    element('span', { class: `state state-${runner.online ? 'ok' : 'unknown'}` }, runner.online ? 'Online' : 'Offline'),
    capabilities.length ? capabilities.join(', ') : 'Only jobs that need nothing',
    runner.slots ? `${runner.running} of ${runner.slots}` : String(runner.running),
    runner.team ? `${runner.key} (${runner.team})` : `${runner.key} (instance)`,
    runner.last_seen_at ? when(runner.last_seen_at) : 'Never',
  ];
}

export async function render(root) {
  root.append(pageHeader('Runners'));
  root.append(element('p', { class: 'note' }, 'A runner is a host that runs jobs. It takes only the jobs whose needs match what it says it can do. Start one on a host with armature runner and a runner key.'));
  const slot = element('div', {}, loading());
  root.append(card(slot));
  let runners;
  try {
    runners = listOf(await api(RUNNERS_PATH), 'runners');
  } catch (err) {
    clear(slot).append(errorBox(err));
    return;
  }
  clear(slot).append(runners.length
    ? table(['Name', 'State', 'Can do', 'Running', 'Key', 'Last seen'], runners.map(runnerCells))
    : empty('No runner has connected yet.'));
  const queueSlot = element('div', {}, loading());
  root.append(element('h2', {}, 'Queue'), card(queueSlot));
  try {
    const waiting = (await Promise.all(WAITING.map((status) => api(`/api/jobs?${new URLSearchParams({ status })}`).then((payload) => listOf(payload, 'jobs')))))
      .flat().sort((left, right) => String(left.created_at).localeCompare(String(right.created_at)));
    clear(queueSlot).append(waiting.length ? table(['Workflow', 'State', 'Queued', 'Why it waits'], waiting.map(queueCells)) : empty('No job is waiting.'));
  } catch (err) {
    clear(queueSlot).append(errorBox(err));
  }
}
