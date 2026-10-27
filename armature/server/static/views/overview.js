import { api, listOf } from '../api.js';
import { ago, card, clear, duration, element, empty, errorBox, link, loading, money, pageHeader, secondsBetween, statusChip, table, when } from '../dom.js';
import { hours, loadDisplay, workflowLabel } from '../display.js';
import { every, subscribe } from '../live.js';
import { hashFor } from '../router.js';

const POLL_MS = 15000;
const REFRESH_AFTER_MS = 3000;
const ALERT_TONE = { failed: 'warn', lost: 'warn', credential: 'bad' };

export function teamPicker(teams, selected, onPick) {
  if (teams.length < 2) return null;
  const select = element('select', { id: 'team-picker' }, element('option', { value: '' }, 'All my teams'),
    teams.map((team) => element('option', { value: team.id }, team.name)));
  select.value = teams.some((team) => team.id === selected) ? selected : '';
  select.addEventListener('change', () => onPick(select.value));
  return element('label', { class: 'team-picker', for: select.id }, 'Team', select);
}

function figure(value, caption) {
  return element('div', { class: 'figure' }, element('div', { class: 'figure-value' }, value), element('div', { class: 'figure-caption' }, caption));
}

const agentTime = (seconds) => (seconds > 0 ? duration(seconds) : '0s');

function elapsed(run) {
  if (!run.finished_at) return run.status === 'running' ? `${duration(secondsBetween(run.started_at, new Date().toISOString()))} so far` : '—';
  return duration(secondsBetween(run.started_at, run.finished_at));
}

// A link to the run, or with ``open`` a button that calls it. ``details`` replaces the started and by lines.
export function runningCard(run, display, { open = null, color = null, details = null } = {}) {
  const parts = [
    element('span', { class: 'running-name' }, color ? element('span', { class: `swatch sw-${color}`, 'aria-hidden': 'true' }) : null, workflowLabel(display, run.workflow)),
    statusChip(run.status),
    details ?? [element('span', { class: 'muted' }, `Started ${ago(run.started_at)}`), run.triggered_by ? element('span', { class: 'muted' }, `by ${run.triggered_by}`) : null],
  ];
  if (!open) return element('a', { class: 'running-run', href: hashFor('run', [run.id]) }, parts);
  return element('button', { type: 'button', class: 'running-run', 'data-run-id': String(run.id), onclick: open }, parts);
}

const DISMISSED_KEY = 'armature.dismissed-alerts';

export const alertKey = (alert) => `${alert.kind}:${alert.workflow || ''}`;

// A dismissal holds only while the alert's latest run is the same, so a new failure brings it back.
export function visibleAlerts(alerts, dismissed) {
  return alerts.filter((alert) => !alert.run_id || dismissed[alertKey(alert)] !== alert.run_id);
}

export function keptDismissals(alerts, dismissed) {
  const live = new Set(alerts.map(alertKey));
  return Object.fromEntries(Object.entries(dismissed).filter(([key]) => live.has(key)));
}

function loadDismissed() {
  try {
    const saved = JSON.parse(localStorage.getItem(DISMISSED_KEY) || '{}');
    return saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : {};
  } catch {
    return {};
  }
}

function saveDismissed(dismissed) {
  try {
    localStorage.setItem(DISMISSED_KEY, JSON.stringify(dismissed));
  } catch {}
}

function alertBanner(alert, onDismiss) {
  const dismiss = alert.run_id
    ? element('button', { type: 'button', class: 'banner-dismiss', 'aria-label': `Dismiss: ${alert.message}`, title: 'Dismiss' }, '×')
    : null;
  dismiss?.addEventListener('click', () => onDismiss(alert));
  return element('div', { class: `banner ${ALERT_TONE[alert.kind] || 'warn'}`, role: alert.kind === 'credential' ? 'alert' : null },
    element('span', {}, alert.message, alert.run_id ? [' ', link(hashFor('run', [alert.run_id]), 'Open the latest run')] : null),
    dismiss);
}

export async function render(root, { query, signal }) {
  const team = query.get('team') || '';
  const [teams, display] = await Promise.all([api('/api/teams').then((payload) => listOf(payload, 'teams')).catch(() => []), loadDisplay()]);
  const liveNote = element('span', { class: 'live-state muted', role: 'status' });
  root.append(pageHeader('Overview', teamPicker(teams, team, (picked) => { location.hash = hashFor('overview', [], { team: picked }); }), liveNote));
  const alertArea = element('div', { class: 'alerts' });
  const runningBand = element('div', { class: 'running-band', 'aria-live': 'polite' });
  const body = element('div', {}, loading());
  root.append(alertArea, element('h2', {}, 'Running now'), runningBand, body);

  const scope = new URLSearchParams(team ? { team } : {});
  const path = `/api/overview?${scope}`;
  const running = new Map();
  const drawRunning = () => {
    const shown = [...running.values()].sort((left, right) => String(right.started_at).localeCompare(String(left.started_at)));
    clear(runningBand).append(shown.length ? element('div', { class: 'running-list' }, shown.map((run) => runningCard(run, display))) : card(empty('Nothing is running.')));
  };

  const draw = (overview) => {
    running.clear();
    for (const run of overview.running) running.set(String(run.id), run);
    drawRunning();
    let dismissed = keptDismissals(overview.alerts, loadDismissed());
    saveDismissed(dismissed);
    const drawAlerts = () => clear(alertArea).append(...visibleAlerts(overview.alerts, dismissed).map((alert) => alertBanner(alert, (gone) => {
      dismissed = { ...dismissed, [alertKey(gone)]: gone.run_id };
      saveDismissed(dismissed);
      drawAlerts();
    })));
    drawAlerts();
    const { counts, spend, time } = overview;
    const figures = [
      figure(String(counts.running), 'running now'),
      figure(String(counts.runs_24h), 'runs, last 24 hours'),
      figure(String(counts.failed_24h), 'failed or lost, last 24 hours'),
      display.show_cost && spend ? figure(money(spend.today), `spent today, 7-day average ${money(spend.avg_7d)}`) : null,
      figure(String(overview.reviews_today), 'code reviews today'),
      figure(String(overview.reevaluations_today), 're-evaluations today'),
    ];
    const timeFigures = [
      figure(agentTime(time.agent_today), 'agent time today'),
      figure(agentTime(time.agent_7d), 'agent time, 7 days'),
      ...(display.show_man_hours ? [figure(hours(time.man_hours_today), 'man hours today'), figure(hours(time.man_hours_7d), 'man hours, 7 days'), figure(hours(time.man_hours_all), 'man hours, all time')] : []),
    ];
    const headers = ['Started', 'Workflow', 'Status', ['Time', 'num'], ...(display.show_cost ? [['Cost', 'num']] : [])];
    const recentRows = overview.recent.map((run) => [
      element('span', { title: run.started_at || null }, when(run.started_at)), link(hashFor('run', [run.id]), workflowLabel(display, run.workflow)),
      statusChip(run.status), elapsed(run), ...(display.show_cost ? [money(run.cost_usd)] : []),
    ]);
    clear(body).append(
      element('h2', {}, 'Today'), element('div', { class: 'figures' }, figures),
      element('h2', {}, 'Time'), element('div', { class: 'figures' }, timeFigures),
      display.show_man_hours ? element('p', { class: 'note' }, `Man hours are agent time multiplied by ${display.human_multiplier}. Days start at midnight in ${overview.timezone}.`) : null,
      element('h2', {}, 'Recent'), card(overview.recent.length ? table(headers, recentRows) : empty('No runs yet.'), element('p', { class: 'note' }, link(hashFor('runs', [], { team }), 'All runs'), ' · ', link(hashFor('trends', [], { team }), 'Trends'))));
  };

  const refresh = async () => {
    try {
      draw(await api(path));
    } catch (err) {
      clear(body).append(errorBox(err));
    }
  };
  await refresh();

  let refreshTimer = 0;
  signal?.addEventListener('abort', () => clearTimeout(refreshTimer));
  const receive = (run) => {
    if (!run || run.id === undefined || run.id === null) return;
    if (run.status === 'running') running.set(String(run.id), run);
    else running.delete(String(run.id));
    drawRunning();
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      if (!signal?.aborted) refresh();
    }, REFRESH_AFTER_MS);
  };
  const states = { live: 'Live', reconnecting: 'Reconnecting…', polling: 'Refreshing every 15 s' };
  subscribe(`/api/stream/runs?${scope}`, {
    events: { run: receive },
    onState: (state) => { liveNote.textContent = states[state] || ''; },
    onFallback: () => every(POLL_MS, refresh, signal),
  }, { signal });
}
