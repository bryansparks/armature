import { api, listOf } from '../api.js';
import { chartFigure, lines, stackedBars } from '../charts.js';
import { card, clear, duration, element, empty, errorBox, loading, money, pageHeader, table } from '../dom.js';
import { hours, loadDisplay, workflowColor, workflowLabel } from '../display.js';
import { hashFor } from '../router.js';
import { teamPicker } from './overview.js';

const RANGES = [7, 30, 90];
const DURATION_SERIES = 4;
const ROLES_SHOWN = 15;
const wholeDollars = (value) => `$${Math.round(Number(value) || 0).toLocaleString('en-US')}`;
const minutes = (value) => (value === null || value === undefined ? null : Number(value) / 60);

const sum = (values) => values.reduce((total, value) => total + (Number(value) || 0), 0);
const percent = (value) => `${Math.round(Number(value) * 100)}%`;
const shortDuration = (seconds) => (seconds > 0 ? duration(seconds) : '0s');

export function largest(seriesMap) {
  let best = null;
  for (const [key, values] of Object.entries(seriesMap || {})) {
    const total = sum(values);
    if (total > 0 && (!best || total > best.total)) best = { key, total };
  }
  return best;
}

function workflowSeries(map, order, display) {
  return order.filter((workflow) => map?.[workflow]).map((workflow) => ({
    key: workflow, label: workflowLabel(display, workflow), color: workflowColor(display, workflow, order), values: map[workflow],
  }));
}

function perDayTable(days, series, format) {
  return { headers: ['Day', ...series.map((item) => [item.label, 'num'])], rows: days.map((day, index) => [day, ...series.map((item) => format(item.values[index]))]) };
}

function stackedFigure({ title, days, series, format, tickFormat = format, summary, signal }) {
  return chartFigure({
    title, summary, signal, legend: series, ...perDayTable(days, series, format),
    draw: stackedBars({ days, series, format, tickFormat, label: `${title}. ${summary}` }),
  });
}

export async function render(root, { query, signal }) {
  const team = query.get('team') || '';
  const days = RANGES.includes(Number(query.get('days'))) ? Number(query.get('days')) : 30;
  const [teams, display] = await Promise.all([api('/api/teams').then((payload) => listOf(payload, 'teams')).catch(() => []), loadDisplay()]);
  const rangeSelect = element('select', { id: 'trend-range' }, RANGES.map((range) => element('option', { value: range }, `Last ${range} days`)));
  rangeSelect.value = String(days);
  rangeSelect.addEventListener('change', () => { location.hash = hashFor('trends', [], { team, days: rangeSelect.value }); });
  root.append(pageHeader('Trends', teamPicker(teams, team, (picked) => { location.hash = hashFor('trends', [], { team: picked, days }); }),
    element('label', { class: 'team-picker', for: rangeSelect.id }, 'Range', rangeSelect)));
  const body = element('div', {}, loading());
  root.append(body);

  let trend;
  try {
    trend = await api(`/api/trends?${new URLSearchParams({ days: String(days), ...(team ? { team } : {}) })}`);
  } catch (err) {
    clear(body).append(errorBox(err));
    return;
  }
  const order = trend.workflows;
  const dayList = trend.days;
  const totalRuns = sum(Object.values(trend.runs_by_workflow).map(sum));
  if (!totalRuns) {
    clear(body).append(card(empty(`No runs in the last ${days} days.`)));
    return;
  }
  const figures = [];

  if (display.show_cost && trend.spend_by_workflow) {
    const series = workflowSeries(trend.spend_by_workflow, order, display);
    const top = largest(trend.spend_by_workflow);
    const total = sum(series.map((item) => sum(item.values)));
    figures.push(stackedFigure({
      title: 'Spend per day', days: dayList, series, format: (value) => money(value), tickFormat: wholeDollars, signal,
      summary: `${money(total)} over ${days} days${top ? `, most on ${workflowLabel(display, top.key)} at ${money(top.total)}` : ''}.`,
    }));
  }

  const runSeries = workflowSeries(trend.runs_by_workflow, order, display);
  const busiest = largest(trend.runs_by_workflow);
  figures.push(stackedFigure({
    title: 'Runs per day', days: dayList, series: runSeries, format: (value) => String(Math.round(Number(value) || 0)), signal,
    summary: `${totalRuns} runs over ${days} days${busiest ? `, ${busiest.total} of them ${workflowLabel(display, busiest.key)}` : ''}.`,
  }));

  const finished = sum(trend.failure_rate.map((day) => day.finished));
  const failed = sum(trend.failure_rate.map((day) => day.failed));
  const rates = trend.failure_rate.map((day) => day.rate);
  figures.push(chartFigure({
    title: 'Failure rate', signal,
    summary: finished ? `${failed} of ${finished} finished runs failed or were lost, ${percent(failed / finished)} overall.` : 'No run finished in this range.',
    headers: ['Day', ['Finished', 'num'], ['Failed or lost', 'num'], ['Rate', 'num']],
    rows: dayList.map((day, index) => [day, String(trend.failure_rate[index].finished), String(trend.failure_rate[index].failed), rates[index] === null ? '—' : percent(rates[index])]),
    draw: lines({ days: dayList, series: [{ label: 'Failure rate', color: 'bad', values: rates }], format: percent, max: 1, label: 'Failure rate per day' }),
  }));

  const timed = order.filter((workflow) => trend.duration_p50_p90_by_workflow[workflow]?.n).slice(0, DURATION_SERIES);
  if (timed.length) {
    const series = timed.map((workflow) => ({ key: workflow, label: workflowLabel(display, workflow), color: workflowColor(display, workflow, order), values: trend.duration_p50_p90_by_workflow[workflow].daily_p50.map(minutes) }));
    figures.push(chartFigure({
      title: 'Typical run time (median per day)', signal, legend: series,
      summary: timed.map((workflow) => {
        const stat = trend.duration_p50_p90_by_workflow[workflow];
        return `${workflowLabel(display, workflow)}: median ${shortDuration(stat.p50)}, 90th percentile ${shortDuration(stat.p90)} over ${stat.n} runs.`;
      }).join(' '),
      headers: ['Workflow', ['Runs', 'num'], ['Median', 'num'], ['90th percentile', 'num']],
      rows: Object.entries(trend.duration_p50_p90_by_workflow).filter(([, stat]) => stat.n).map(([workflow, stat]) => [workflowLabel(display, workflow), String(stat.n), shortDuration(stat.p50), shortDuration(stat.p90)]),
      draw: lines({ days: dayList, series, format: (value) => shortDuration(value * 60), tickFormat: (value) => `${Math.round(value)}m`, label: 'Median run time per day by workflow' }),
    }));
  }

  const verdicts = Object.keys(trend.verdicts_per_day);
  if (verdicts.length) {
    const series = verdicts.map((verdict, index) => ({ key: verdict, label: verdict, color: `s${(index % 8) + 1}`, values: trend.verdicts_per_day[verdict] }));
    figures.push(stackedFigure({
      title: 'Review verdicts per day', days: dayList, series, format: (value) => String(Math.round(Number(value) || 0)), signal,
      summary: series.map((item) => `${sum(item.values)} ${item.label}`).join(', ') + '.',
    }));
  }

  if (display.show_man_hours && trend.man_hours_per_day) {
    const total = sum(trend.man_hours_per_day);
    const series = [{ key: 'hours', label: 'Man hours', color: 's1', values: trend.man_hours_per_day }];
    figures.push(stackedFigure({
      title: 'Man hours per day', days: dayList, series, format: hours, signal,
      summary: `${hours(total)} over ${days} days, from ${hours(sum(trend.agent_hours_per_day))} of agent time at ${display.human_multiplier}x.`,
    }));
  }

  const tables = [];
  if (trend.slowest_steps.length) {
    tables.push(element('h2', {}, 'Slowest steps'), card(table(['Workflow', 'Step', ['Runs', 'num'], ['Median', 'num'], ['90th percentile', 'num']],
      trend.slowest_steps.map((step) => [workflowLabel(display, step.workflow), element('span', { class: 'mono' }, step.step), String(step.n), shortDuration(step.p50), shortDuration(step.p90)]))));
  }
  if (trend.cost_by_role.length) {
    tables.push(element('h2', {}, 'Review roles'), card(table(['Role', ['Sessions', 'num'], ...(display.show_cost ? [['Cost', 'num']] : []), ['Median', 'num'], ['90th percentile', 'num']],
      trend.cost_by_role.slice(0, ROLES_SHOWN).map((role) => [element('span', { class: 'mono' }, role.role), String(role.n), ...(display.show_cost ? [money(role.cost)] : []), shortDuration(role.p50), shortDuration(role.p90)])),
      trend.cost_by_role.length > ROLES_SHOWN ? element('p', { class: 'note' }, `The top ${ROLES_SHOWN} of ${trend.cost_by_role.length} roles.`) : null));
  }
  clear(body).append(element('p', { class: 'note' }, `Days start at midnight in ${trend.timezone}.`), element('div', { class: 'chart-grid' }, figures), ...tables);
}
