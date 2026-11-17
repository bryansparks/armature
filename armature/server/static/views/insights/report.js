// "What did Armature learn yesterday?" The instance-wide daily report, for admins. Old rows may lack keys, so every
// read has a fallback.
import { chartFigure, stackedBars } from '../../charts.js';
import { duration, element, money } from '../../dom.js';
import { loadDisplay, workflowColor, workflowLabel } from '../../display.js';
import { hashFor } from '../../router.js';
import { fixed, isNum, monthDay, plural } from './format.js';
import { page } from './index.js';
import { emptyLine, emptyState, events, filters, ranked, section, select, skeletonRows, sortableTable, tile } from './ui.js';

export const QUESTION = 'What did Armature learn yesterday?';
const LIST = '/api/insights/daily-reports';
const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const SECTION_WORDS = { headline: 'Runs', findings: 'Findings', churn: 'Review' };
const GITHUB_PR = /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/[0-9]{1,9}$/;

const MEASURES = {
  runs: { label: 'Runs', unit: 'count', digits: 0, better: 'up', change: 'percent', def: 'Runs started on the day, every workflow and team.' },
  cost: { label: 'Cost', unit: 'usd', digits: 2, better: 'down', change: 'percent', def: 'What the day’s runs cost, from each run’s own total.' },
  failed: { label: 'Failed or lost', unit: 'count', digits: 0, better: 'down', change: 'absolute', def: 'Runs started on the day that failed or stopped reporting.' },
  open: { label: 'Still open at build', unit: 'count', digits: 0, better: 'down', change: 'absolute', def: 'Runs from the day still running when the report was built, so the day’s numbers may grow.' },
};

const obj = (value) => (value && typeof value === 'object' && !Array.isArray(value) ? value : {});
const list = (value) => (Array.isArray(value) ? value : []);
const took = (seconds) => (isNum(seconds) && seconds > 0 ? duration(seconds) : '—');
const hourWords = (hour) => (hour === 0 ? 'midnight' : hour < 12 ? `${hour} AM` : hour === 12 ? 'noon' : `${hour - 12} PM`);
const clock = (iso) => (typeof iso === 'string' && iso.length >= 16 ? iso.slice(11, 16) : '');

export function longDay(day) {
  const at = new Date(`${day}T00:00:00Z`);
  return Number.isNaN(at.getTime()) ? String(day || '') : `${WEEKDAYS[at.getUTCDay()]}, ${monthDay(day)}`;
}

// The server builds the link from only the repo and number. Anything else in an old row is dropped.
export function prHref(link) {
  const { url } = obj(link);
  return typeof url === 'string' && GITHUB_PR.test(url) ? url : null;
}

export const pickDay = (days, wanted) => (days.some((entry) => entry.day === wanted) ? wanted : days[0]?.day || null);

export function emptyWords(listing) {
  return `Today’s report builds after ${hourWords(Number(listing?.builds_after_hour) || 0)} ${listing?.timezone || ''}`.trim() + '.';
}

export function answer(data) {
  const head = obj(data.headline);
  const runs = obj(head.runs).value;
  if (!isNum(runs)) return 'No numbers in this report.';
  const failed = obj(head.failed).value;
  return `${plural(runs, 'run')}, ${money(obj(head.cost_usd).value)} spent, ${plural(failed || 0, 'run')} failed.`;
}

function tiles(data) {
  const head = obj(data.headline);
  const pick = (key) => obj(head[key]);
  const one = (measure, stat, format) => tile({
    measure, value: stat.value, previous: stat.previous, days: 1, period: 'the day before', ref: isNum(stat.avg_7d) ? `7-day average ${format(stat.avg_7d)}` : null,
  });
  return element('div', { class: 'in-tiles' },
    one(MEASURES.runs, pick('runs'), (value) => fixed(value, 1)), one(MEASURES.cost, pick('cost_usd'), (value) => money(value)),
    one(MEASURES.failed, pick('failed'), (value) => fixed(value, 1)),
    tile({ measure: MEASURES.open, value: data.open_runs ?? null, previous: null, days: 1, period: 'the day before' }));
}

function dayCharts(data, display, signal) {
  const series = obj(obj(data.headline).series);
  const days = list(series.days);
  const order = list(series.workflows);
  if (!days.length) return emptyLine('No runs in the 8 days to this one.');
  const build = (map) => order.filter((workflow) => Array.isArray(obj(map)[workflow])).map((workflow) => ({
    key: workflow, label: workflowLabel(display, workflow), color: workflowColor(display, workflow, order), values: map[workflow],
  }));
  const figure = (title, map, format, summary) => {
    const items = build(map);
    return chartFigure({
      title, summary, signal, legend: items,
      headers: ['Day', ...items.map((item) => [item.label, 'num'])],
      rows: days.map((day, index) => [day, ...items.map((item) => format(item.values[index]))]),
      draw: stackedBars({ days, series: items, format, label: `${title}. ${summary}` }),
    });
  };
  const runs = list(series.runs);
  const cost = list(series.cost_usd);
  return element('div', { class: 'chart-grid' },
    figure('Runs per day', series.runs_by_workflow, (value) => fixed(value), `${fixed(runs.at(-1))} runs on the day, ${fixed(runs.slice(0, -1).reduce((a, b) => a + (Number(b) || 0), 0))} in the 7 days before.`),
    figure('Cost per day', series.spend_by_workflow, (value) => money(value), `${money(cost.at(-1))} on the day.`));
}

function workflowTable(data, display) {
  const rows = list(obj(data.headline).by_workflow);
  if (!rows.length) return emptyLine('No runs on this day.');
  return element('div', { class: 'in-table-scroll' }, sortableTable({
    caption: 'Runs, cost, and run time by workflow.', rowClass: '', sort: { key: 'cost_usd', dir: 'descending' }, rows,
    columns: [
      { key: 'workflow', label: 'Workflow', cell: (row) => workflowLabel(display, row.workflow) },
      { key: 'runs', label: 'Runs', num: true, cell: (row) => fixed(row.runs) },
      { key: 'cost_usd', label: 'Cost', num: true, cell: (row) => money(row.cost_usd) },
      { key: 'p50', label: 'Median', num: true, cell: (row) => took(row.p50) },
      { key: 'p90', label: '90th percentile', num: true, cell: (row) => took(row.p90) },
    ],
  }));
}

function modelList(data) {
  const models = list(obj(data.headline).models);
  if (!models.length) return emptyLine('No model calls on this day.');
  return ranked(models.map((model) => ({ name: model.model, amount: model.cost_usd, value: money(model.cost_usd), after: ` · ${plural(model.calls, 'call')}` })), { label: 'Cost by model' });
}

const runLink = (row) => (row.run_id ? hashFor('run', [row.run_id]) : null);

function findingSections(data, display) {
  const found = obj(data.findings);
  const errors = list(found.errors);
  const failing = list(found.failing);
  const runRows = (rows, value) => (rows.length ? events(rows.map((row) => [value(row), `${workflowLabel(display, row.workflow)}${row.subject ? ` · ${row.subject}` : ''}`, runLink(row)])) : emptyLine('No finished runs.'));
  const dry = obj(found.dry);
  return [
    section({ title: 'Errors', count: errors.length || null, takeaway: 'Step errors grouped by their first line, with ids and numbers folded.' },
      errors.length ? element('div', { class: 'in-table-scroll' }, sortableTable({
        caption: 'Step errors by pattern.', rowClass: '', sort: { key: 'count', dir: 'descending' }, rows: errors,
        columns: [
          { key: 'pattern', label: 'Error', cell: (row) => element('span', { class: 'mono' }, row.pattern) },
          { key: 'count', label: 'Times', num: true, cell: (row) => fixed(row.count) },
          { key: 'runs', label: 'Runs', num: true, cell: (row) => fixed(row.runs) },
          { key: 'workflows', label: 'Workflows', sortable: false, cell: (row) => list(row.workflows).map((name) => workflowLabel(display, name)).join(', ') },
        ],
      })) : emptyLine('No step failed with an error.')),
    section({ title: 'Failing workflows' }, failing.length
      ? ranked(failing.map((row) => ({ name: workflowLabel(display, row.workflow), amount: row.failed, value: `${fixed(row.failed)} of ${fixed(row.runs)}` })), { label: 'Failed runs by workflow' })
      : emptyLine('No workflow had a failed run.')),
    section({ title: 'Slowest runs' }, runRows(list(found.slowest), (row) => took(row.seconds))),
    section({ title: 'Costliest runs' }, runRows(list(found.costliest), (row) => money(row.cost_usd))),
    section({ title: 'Dry runs that produced nothing', takeaway: isNum(dry.runs) ? `${fixed(dry.empty || 0)} of ${plural(dry.runs, 'dry run')} left no finding and no artifact.` : null },
      list(dry.by_workflow).length ? ranked(list(dry.by_workflow).map((row) => ({ name: workflowLabel(display, row.workflow), amount: row.runs, value: fixed(row.runs) })), { label: 'Empty dry runs by workflow' })
        : emptyLine(dry.runs ? 'Every dry run produced something.' : 'No dry runs.')),
  ];
}

function churnSection(data) {
  const review = obj(obj(data.findings).churn);
  const top = list(review.top);
  if (!review.prs && !review.findings) return section({ title: 'Review churn' }, emptyLine('No review rounds were recorded on this day.'));
  const facts = element('p', { class: 'in-note' },
    `${plural(review.prs || 0, 'pull request')} reviewed, ${fixed(review.multi_round || 0)} past round 1, up to ${plural(review.max_rounds || 0, 'round')}. `,
    `Of ${plural(review.findings || 0, 'finding')}, ${fixed(review.blocking || 0)} blocked, ${fixed(review.non_blocking || 0)} didn’t, ${fixed(review.repeats || 0)} repeated an earlier one, and ${fixed(review.late || 0)} were late catches.`);
  const rows = top.map((pr) => {
    const href = prHref(pr.link);
    const name = pr.title ? `${pr.subject} · ${pr.title}` : pr.subject || 'Unknown pull request';
    return { name, href, amount: pr.rounds, value: plural(pr.rounds, 'round') };
  });
  return section({ title: 'Review churn', takeaway: 'A late catch is a finding first raised in round 2 or later. A repeat restates an earlier one.' },
    facts, rows.length ? ranked(rows, { label: 'Rounds by pull request' }) : null);
}

function changeSections(data, display) {
  const changes = obj(data.changes);
  const deploys = obj(changes.deploys);
  const lines = list(deploys.lines);
  const versions = list(changes.versions);
  const config = list(changes.config);
  const prints = obj(changes.fingerprint);
  const moved = ['added', 'removed', 'changed'].flatMap((kind) => list(prints[kind]).map((path) => [kind, path]));
  return [
    section({ title: 'Deploys', takeaway: deploys.live_sha ? `Live: ${deploys.live_sha}` : null },
      lines.length ? events(lines.map((line) => [clock(line.at), line.line])) : emptyLine('No deploys on this day.')),
    section({ title: 'Workflow and document versions', count: versions.length || null },
      versions.length ? events(versions.map((v) => [clock(v.at), `${v.kind === 'workflow' ? workflowLabel(display, v.name) : v.name} version ${v.version}${v.author ? ` by ${v.author}` : ''}${v.note ? `: ${v.note}` : ''}`]))
        : emptyLine('No workflow or document changed.')),
    section({ title: 'Triggers, webhook rules, and settings', takeaway: 'Only the names of what changed.' },
      config.length ? events(config.map((c) => [clock(c.at), `${c.action}${c.team ? ` in ${c.team}` : ''}${c.actor ? ` by ${c.actor}` : ''}: ${list(c.keys).join(', ') || 'no keys recorded'}`]))
        : emptyLine('No trigger, webhook rule, or setting changed.')),
    section({ title: 'Configured files' },
      prints.baseline ? emptyLine(`First report, so its ${plural(prints.files || 0, 'file')} are the baseline.`)
        : moved.length ? events(moved.map(([kind, path]) => [kind, path])) : emptyLine('No configured file changed since the last report.')),
  ];
}

export function draw(data, display, signal) {
  const learned = list(data.learned).filter((item) => item && item.text);
  return [
    tiles(data),
    section({ title: 'What Armature learned', takeaway: 'Each line cites the numbers behind it.' },
      learned.length ? events(learned.map((item) => [SECTION_WORDS[item.section] || '', item.text])) : emptyLine('Nothing stood out.')),
    section({ title: 'Runs and cost' }, dayCharts(data, display, signal)),
    section({ title: 'Workflows' }, workflowTable(data, display)),
    section({ title: 'Models', takeaway: 'A breakdown of model calls. The cost above comes from the runs.' }, modelList(data)),
    ...findingSections(data, display),
    churnSection(data),
    ...changeSections(data, display),
  ];
}

export function render(body, ctx) {
  return page(body, ctx, {
    question: QUESTION, what: 'the daily report',
    skeleton: () => skeletonRows(6),
    load: async () => {
      const [listing, display] = await Promise.all([ctx.api(LIST), loadDisplay()]);
      const days = list(listing?.days);
      const day = pickDay(days, ctx.query.get('day'));
      const found = day ? await ctx.api(`${LIST}/${encodeURIComponent(day)}`) : null;
      return { listing, days, day, found, display };
    },
    draw: ({ listing, days, day, found, display }, head) => {
      if (!found) {
        head.answerNode.replaceChildren('No report yet.');
        return emptyState(emptyWords(listing));
      }
      const data = obj(found.data);
      head.node.append(filters(select({
        label: 'Day', value: day, options: days.map((entry) => [entry.day, longDay(entry.day)]), onChange: (value) => ctx.setQuery({ day: value }),
      })));
      head.answerNode.replaceChildren(answer(data));
      head.scopeNode.replaceChildren(`${longDay(day)}, every team and workflow, the day in ${data.zone || listing.timezone || 'the server’s zone'}. Cost always shows here.`);
      return draw(data, display, ctx.signal);
    },
  });
}
