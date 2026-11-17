// "How is the team doing?" Four numbers that drive one trend chart, what needs attention, and the people table.
import { element } from '../../dom.js';
import { chartSkeleton, trendChart } from './chart.js';
import { MEASURES, METRIC_KEYS, delta, fixed, isNum, metricFromKey, short } from './format.js';
import { daysFrom, page, repoToken } from './index.js';
import {
  actionLink, chevronRight, emptyLine, events, filters, link, mark, openDrawer, periodSelect, personCell, ranked, repoSelect, section,
  skeletonRows, sortableTable, tile, tileTabs,
} from './ui.js';
import { attentionWords, outsideCount, overviewAnswer, scopeLine, takeaway } from './words.js';

export const QUESTION = 'How is the team doing?';
const IDS = ['merged', 'time_to_merge', 'review_rounds', 'conflict_hours'];

export function render(body, ctx) {
  const { query, links, repos } = ctx;
  const days = daysFrom(query);
  const repo = ctx.findRepo(query.get('repo'));
  let metricId = metricFromKey(query.get('metric') || 'rounds', IDS) || 'review_rounds';
  const params = new URLSearchParams({ days: String(days) });
  if (repo) params.set('repo', repo.id);
  const filterBar = filters(ctx.teamControl(), periodSelect(days, (value) => ctx.setQuery({ days: value })),
    repoSelect(repos, repo?.id || '', (value) => ctx.setQuery({ repo: repoToken(repos, ctx.findRepo(value)) })));

  return page(body, ctx, {
    question: QUESTION, filterBar, what: 'the team overview',
    skeleton: () => [
      element('div', { class: 'in-tiles', 'aria-hidden': 'true' }, IDS.map((id) => tile({ measure: MEASURES[id], value: undefined, days }))),
      section({ title: MEASURES[metricId].label }, chartSkeleton()),
      section({ title: 'Needs attention' }, skeletonRows(4)),
    ],
    load: () => ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/overview?${params}`),
    draw: (data, head) => {
      const shown = (repo ? [repo] : [...repos]).sort((a, b) => (ctx.slots.get(a.id) || 9) - (ctx.slots.get(b.id) || 9));
      head.answerNode.replaceChildren(overviewAnswer(data, repo?.name || ''));
      head.scopeNode.replaceChildren(scopeLine({ scope: data.scope, people: Array.isArray(data.people) ? data.people.length : null, repos: repos.length, repoName: repo?.name || '' }));
      const metrics = new Map((data.metrics || []).map((metric) => [metric.id, metric]));
      const ids = IDS.filter((id) => metrics.has(id));
      if (!ids.includes(metricId)) metricId = ids[0];
      const teamSeries = (metric) => {
        const lists = Object.values(metric.series || {});
        return (data.dates || []).map((_, index) => {
          const values = lists.map((list) => list[index]).filter(isNum).map(Number);
          if (!values.length) return null;
          const total = values.reduce((sum, value) => sum + value, 0);
          return metric.id === 'merged' || metric.id === 'conflict_hours' ? total : total / values.length;
        });
      };
      const chartPanel = element('section', { class: 'in-section', id: 'in-chart-panel', role: 'tabpanel' });
      const drawChart = (id) => {
        const metric = metrics.get(id);
        const measure = MEASURES[id];
        const series = shown.map((item) => ({ name: item.name, slot: ctx.slots.get(item.id) || 1, values: metric.series?.[item.id] || [], href: links.repo(item.id, { days, metric: METRIC_KEYS[id] }) }));
        const heading = element('h2', { class: 'in-h2', id: 'in-chart-title' }, measure.label);
        chartPanel.setAttribute('aria-labelledby', `in-tile-${id}`);
        chartPanel.replaceChildren(
          element('div', { class: 'in-section-head' }, element('div', {}, heading, element('p', { class: 'in-takeaway' }, takeaway(id, series, { dates: data.dates, flat: delta(metric.value, metric.previous, measure, days)?.dir === 'flat' })))),
          trendChart({
            dates: data.dates || [], series, label: measure.label, basis: `${measure.basis}${shown.length > 1 ? ', by repo' : ''}`, legendLabel: 'Repos',
            format: (value) => short(value, measure.unit, measure.digits || (measure.unit === 'rounds' ? 1 : 0)),
            tickFormat: measure.unit === 'share' ? (value) => `${fixed(value * 100)}%` : null,
          }));
      };
      const { strip } = tileTabs({
        ids, selected: metricId,
        build: (id, selected) => {
          const metric = metrics.get(id);
          const outside = id === 'merged' && Array.isArray(data.people) ? outsideCount(metric.value, data.people.reduce((sum, row) => sum + (Number(row.merged) || 0), 0)) : 0;
          const ref = outside ? `Including ${fixed(outside)} by people outside the team` : null;
          return tile({ id: `in-tile-${id}`, measure: MEASURES[id], value: metric.value, previous: metric.previous, days, series: teamSeries(metric), selected, ref });
        },
        onSelect: (id) => {
          metricId = id;
          ctx.setQuery({ metric: METRIC_KEYS[id] }, { reload: false });
          drawChart(id);
        },
      });
      for (const node of strip.children) node.setAttribute('aria-controls', 'in-chart-panel');
      drawChart(metricId);
      const nodes = [strip, chartPanel, attentionSection(data, ctx)];
      if (Array.isArray(data.people)) nodes.push(peopleSection(data, ctx, days, repo));
      return nodes;
    },
  });
}

function attentionSection(data, ctx) {
  const all = data.attention || [];
  const items = all.map((item) => [item, attentionWords(item, { hrefs: ctx.links, canSeePeople: ctx.lead, all })]).filter(([, words]) => words);
  const list = element('ul', { class: 'in-attn' }, items.map(([, words]) => {
    const button = element('button', { type: 'button', 'aria-haspopup': 'dialog' },
      mark(words.icon, words.kindLabel), element('span', { class: 'in-attn-title' }, words.title), element('span', { class: 'in-attn-why' }, words.why),
      element('span', { class: 'in-attn-go' }, element('span', {}, words.go), chevronRight()));
    button.addEventListener('click', () => openDrawer({
      title: words.title, why: words.why, evidenceTitle: words.evidenceTitle,
      evidence: words.ranked?.length ? ranked(words.ranked.map(([name, amount]) => ({ name, amount, value: `${fixed(amount, amount % 1 ? 1 : 0)}${words.rankedSuffix || ''}` })))
        : words.events?.length ? events(words.events) : null,
      action: actionLink(words.link || []),
    }, button));
    return element('li', {}, button);
  }));
  return section({ title: 'Needs attention', count: items.length || null },
    items.length ? list : emptyLine(`Nothing needs attention in the last ${data.scope.days} days. Bugs traced to dropped flags, rising review rounds, conflict-prone files, and pull requests waiting on a review show up here.`));
}

function peopleSection(data, ctx, days, repo) {
  const people = data.people || [];
  const columns = [
    { key: 'name', label: 'Person', wide: true, cell: (row) => personCell(row.name, ctx.links.person(row, { days }), row.repo ? row.repo.split('/').pop() : '') },
    { key: 'merged', label: 'Merged', num: true, def: 'Pull requests this person merged in the period.', cell: (row) => fixed(row.merged) },
    { key: 'rounds', label: 'Review rounds', num: true, def: 'Average review rounds per merged pull request. Fewer is better.', cell: (row) => short(row.rounds, 'rounds', 1) },
    { key: 'reviews', label: 'Reviews given', num: true, opt: true, def: 'Reviews this person submitted on other people’s pull requests.', cell: (row) => fixed(row.reviews) },
    { key: 'first_review_hours', label: 'Time to first review', num: true, opt: true, def: 'Median time from opening a pull request to this person’s first review on it.', cell: (row) => short(row.first_review_hours, 'hours', 1) },
  ];
  return section({ title: 'People', count: people.length, link: link(ctx.links.people({ days, repo: repo?.id || '' }), 'Open People') },
    people.length ? element('div', { class: 'in-table-gap' }, sortableTable({ columns, rows: people, sort: { key: 'name', dir: 'ascending' }, caption: `People on the team, last ${days} days.` }))
      : emptyLine(`Nobody opened, merged, or reviewed a pull request in the last ${days} days.`));
}
