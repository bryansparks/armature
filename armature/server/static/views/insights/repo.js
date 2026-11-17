// "Where is time going in this repo?" The median pull request's stages, four numbers with a trend, problem files,
// and how branches were updated.
import { element } from '../../dom.js';
import { chartSkeleton, trendChart } from './chart.js';
import {
  MEASURES, METRIC_KEYS, delta, figureText, fixed, isNum, metricFromKey, monthDay, plural, pushKindDef, pushKindName, short,
} from './format.js';
import { daysFrom, page, repoToken } from './index.js';
import {
  changeMark, emptyLine, events, filters, openLoadedDrawer, periodSelect, ranked, repoSelect, section, select, sortableTable, tile, tileTabs,
} from './ui.js';
import { repoAnswer, scopeLine, stageBlocks, takeaway } from './words.js';

export const QUESTION = 'Where is time going?';
const MORE_IS_WORSE = { better: 'down', change: 'percent', unit: 'count', digits: 0 };
const IDS = ['time_to_first_review', 'review_rounds', 'conflict_hours', 'ci_failure_rate'];
const RANKS = [['combined', 'Rank by all three'], ['findings', 'Rank by flags'], ['conflict_hours', 'Rank by conflict hours'], ['churn', 'Rank by changes']];

const hoursText = (hours) => (hours >= 48 ? figureText(hours / 24, 'days', 1) : figureText(hours, 'hours', hours < 10 ? 1 : 0));

// Pull requests as dated, linked rows for a drawer. when picks the left column.
function prEvents(prs, ctx, when) {
  return events(prs.map((item) => [when(item), `#${item.number} ${item.title || ''}`.trim(), ctx.links.pr(item)]));
}

const STAGE_SENTENCE = {
  waiting_first_review: (hours) => `This pull request waited ${hours} for a first review.`,
  in_review: (hours) => `This pull request spent ${hours} in review rounds.`,
  waiting_on_ci: (hours) => `This pull request waited ${hours} on CI after approval.`,
  approved_to_merged: (hours) => `This pull request waited ${hours} between approval and merge.`,
};

function stageBar(blocks, stageHref) {
  const total = blocks.reduce((sum, block) => sum + block.hours, 0);
  const label = blocks.map((block) => `${block.name} ${hoursText(block.hours)}`).join(', ');
  const bar = element('div', { class: 'in-stages', role: 'img', 'aria-label': `The median pull request’s time by stage: ${label}.` });
  for (const block of blocks) {
    if (!(block.hours > 0) || !total) continue;
    const share = block.hours / total;
    const node = element('a', { class: `in-stage st${block.step}`, href: stageHref(block.key), 'data-def': `${STAGE_SENTENCE[block.key](hoursText(block.hours))} That is ${fixed(share * 100)}% of its time. ${block.def} Select to see the pull requests that spent longest here.` },
      share > 0.06 ? short(block.hours, 'hours', block.hours < 10 ? 1 : 0) : '');
    node.style.flex = `${share} 1 0`;
    bar.append(node);
  }
  const legend = element('ul', { class: 'in-legend in-stage-legend', 'aria-label': 'Stages' }, blocks.map((block) => element('li', {},
    element('span', { class: `in-key in-key-block st${block.step}`, 'aria-hidden': 'true' }), block.name,
    element('span', { class: 'in-legend-value' }, short(block.hours, 'hours', block.hours < 10 ? 1 : 0)))));
  return [bar, legend];
}

// Files with the score that both orders the list and sizes each bar. Combined adds each measure scaled to its largest.
export function rankFiles(rows, rankBy) {
  const max = (key) => Math.max(1e-9, ...rows.map((row) => Number(row[key]) || 0));
  const score = (row) => (rankBy === 'combined' ? ['findings', 'conflict_hours', 'churn'].reduce((sum, key) => sum + (Number(row[key]) || 0) / max(key), 0) : Number(row[rankBy]) || 0);
  return rows.map((row) => ({ row, score: score(row) })).sort((a, b) => b.score - a.score);
}

// A path with a break opportunity after each slash, so a long path wraps at its folders on a phone.
export function breakablePath(path) {
  const parts = String(path || '').split('/');
  return parts.flatMap((part, index) => (index < parts.length - 1 ? [`${part}/`, element('wbr')] : [part]));
}

function hotspotList(rows, rankBy, onOpen) {
  const value = (row) => {
    if (rankBy === 'findings') return plural(row.findings, 'flag');
    if (rankBy === 'conflict_hours') return `${fixed(row.conflict_hours, row.conflict_hours % 1 ? 1 : 0)} h`;
    if (rankBy === 'churn') return plural(row.churn, 'change');
    return `${plural(row.findings, 'flag')}, ${fixed(row.conflict_hours)} h, ${plural(row.churn, 'change')}`;
  };
  return ranked(rankFiles(rows, rankBy).map(({ row, score }) => ({ name: breakablePath(row.path), amount: score, value: value(row), onClick: (clickEvent) => onOpen(row, clickEvent.currentTarget) })),
    { className: 'in-ranked-path', label: 'Files that keep causing problems' });
}

export function render(body, ctx) {
  const { query, links, repos } = ctx;
  const days = daysFrom(query);
  const token = ctx.args[1] || '';
  const repo = ctx.findRepo(token) || ctx.findRepo(ctx.lastRepo()) || repos[0];
  if (!token || repo.id !== ctx.findRepo(token)?.id) history.replaceState(null, '', `${location.pathname}${location.search}${links.repo(repo.id, Object.fromEntries(query))}`);
  ctx.rememberRepo(repo.id);
  ctx.pathArgs = ['repo', repoToken(repos, repo)];
  let metricId = metricFromKey(query.get('metric') || '', IDS) || 'time_to_first_review';
  let rankBy = RANKS.some(([id]) => id === query.get('rank')) ? query.get('rank') : 'combined';
  const question = `Where is time going in ${repo.name}?`;
  const filterBar = filters(ctx.teamControl(), periodSelect(days, (value) => ctx.setQuery({ days: value })),
    repoSelect(repos, repo.id, (value) => { location.hash = links.repo(value, { days }); }, { all: null }));

  return page(body, ctx, {
    question, filterBar, what: `${repo.name}`,
    skeleton: () => [
      section({ title: 'Time to merge' }, element('div', { class: 'in-stages', 'aria-hidden': 'true' }, element('span', { class: 'in-skel is-line' }))),
      element('div', { class: 'in-tiles', 'aria-hidden': 'true' }, IDS.map((id) => tile({ measure: MEASURES[id], value: undefined, days }))),
      section({ title: MEASURES[metricId].label }, chartSkeleton()),
    ],
    load: () => ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/repos/${encodeURIComponent(repo.id)}?days=${days}`),
    draw: (data, head) => {
      document.title = `${question} | Armature`;
      head.answerNode.replaceChildren(repoAnswer(data));
      head.scopeNode.replaceChildren(scopeLine({ scope: data.scope, repoName: data.repo?.full_name || repo.full_name }));
      const nodes = [timeToMerge(data, ctx, days)];

      const tiles = new Map((data.tiles || []).map((item) => [item.id, item]));
      const ids = IDS.filter((id) => tiles.has(id));
      if (ids.length) {
        if (!ids.includes(metricId)) metricId = ids[0];
        const panel = element('section', { class: 'in-section', id: 'in-chart-panel', role: 'tabpanel' });
        const slot = ctx.slots.get(repo.id) || 1;
        const drawChart = (id) => {
          const measure = MEASURES[id];
          const series = [{ name: repo.name, slot, values: tiles.get(id).series || [] }];
          panel.setAttribute('aria-labelledby', `in-tile-${id}`);
          panel.replaceChildren(element('div', { class: 'in-section-head' }, element('div', {},
            element('h2', { class: 'in-h2' }, measure.label),
            element('p', { class: 'in-takeaway' }, takeaway(id, series, { dates: data.dates, flat: delta(tiles.get(id).value, tiles.get(id).previous, measure, days)?.dir === 'flat' })))),
          trendChart({
            dates: data.dates || [], series, label: `${measure.label} in ${repo.name}`, basis: measure.basis,
            format: (value) => short(value, measure.unit, measure.unit === 'share' ? 0 : 1),
            tickFormat: measure.unit === 'share' ? (value) => `${fixed(value * 100)}%` : null,
          }));
        };
        const { strip } = tileTabs({
          ids, selected: metricId,
          build: (id, selected) => {
            const item = tiles.get(id);
            return tile({ id: `in-tile-${id}`, measure: MEASURES[id], value: item.value, previous: item.previous, days, series: item.series, selected });
          },
          onSelect: (id) => {
            metricId = id;
            ctx.setQuery({ metric: METRIC_KEYS[id] }, { reload: false });
            drawChart(id);
          },
        });
        for (const node of strip.children) node.setAttribute('aria-controls', 'in-chart-panel');
        drawChart(metricId);
        nodes.push(strip, panel);
      }

      const hot = data.hotspots || [];
      const listHolder = element('div');
      const openFile = (row, opener) => openLoadedDrawer({
        title: row.path, why: `In ${repo.name} over the last ${days} days.`, what: 'this file',
        load: () => ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/repos/${encodeURIComponent(repo.id)}/files?${new URLSearchParams({ path: row.path, days: String(days) })}`),
        fill: (file) => ({
          evidence: [
            element('h3', {}, 'Why it is listed'),
            events([
              ['Flags', [`${plural(file.findings?.value, 'flag or review comment', 'flags and review comments')}`, ' ', changeMark(delta(file.findings?.value, file.findings?.previous, MORE_IS_WORSE, days))]],
              ['Conflicts', [`${figureText(file.conflict_hours?.value, 'hours', 0)} blocked by merge conflicts`, ' ', changeMark(delta(file.conflict_hours?.value, file.conflict_hours?.previous, { ...MORE_IS_WORSE, unit: 'hours' }, days))]],
              ['Changes', [`Changed in ${plural(file.churn?.value, 'merged pull request')}`, ' ', changeMark(delta(file.churn?.value, file.churn?.previous, MORE_IS_WORSE, days))]],
            ]),
            (file.weekly?.weeks || []).length ? [element('h3', {}, 'Flags per week'), trendChart({
              dates: file.weekly.weeks, label: `Flags on ${row.path} per week`, basis: 'Last 12 weeks', format: (value) => (isNum(value) ? fixed(value) : '—'),
              series: [{ name: 'Flags', slot: ctx.slots.get(repo.id) || 1, values: file.weekly.findings || [] }],
            })] : null,
            (file.weekly?.conflict_hours || []).some((value) => Number(value) > 0) ? [element('h3', {}, 'Hours blocked by conflicts per week'), trendChart({
              dates: file.weekly.weeks, label: `Hours ${row.path} was blocked by conflicts per week`, basis: 'Last 12 weeks', format: (value) => short(value, 'hours', 0),
              series: [{ name: 'Hours blocked', slot: ctx.slots.get(repo.id) || 1, values: file.weekly.conflict_hours }],
            })] : null,
            (file.prs || []).length ? [element('h3', {}, 'Recent pull requests that changed it'), prEvents(file.prs.slice(0, 10), ctx, (item) => monthDay(item.merged_at || item.created_at, false))] : null,
          ].flat(),
          action: file.url ? element('a', { class: 'in-button', href: file.url }, 'Open the file on GitHub') : null,
        }),
      }, opener);
      const drawList = () => listHolder.replaceChildren(hotspotList(hot, rankBy, openFile));
      drawList();
      const rankSelect = select({ label: 'Rank files by', value: rankBy, options: RANKS, onChange: (value) => { rankBy = value; ctx.setQuery({ rank: value === 'combined' ? '' : value }, { reload: false }); drawList(); } });
      nodes.push(section({
        title: 'Files that keep causing problems',
        takeaway: hot.length ? 'Ranked by flags, hours blocked by conflicts, and how often the file changed. Select a file for the detail.' : null,
        extra: hot.length ? rankSelect : null,
      }, hot.length ? listHolder : emptyLine(`No file in ${repo.name} drew flags, conflicts, or repeated changes in the last ${days} days.`)));

      const kinds = data.push_kinds || [];
      const total = kinds.reduce((sum, row) => sum + row.count, 0);
      nodes.push(section({ title: 'How branches were updated', count: total || null, takeaway: total ? 'Every push to an open pull request, by what it did.' : null },
        kinds.length ? element('div', { class: 'in-table-gap' }, sortableTable({
          caption: `Pushes to pull requests in ${repo.name}, last ${days} days.`, sort: { key: 'count', dir: 'descending' }, rows: kinds,
          columns: [
            { key: 'kind', label: 'Kind of update', wide: true, value: (row) => pushKindName(row.kind),
              cell: (row) => element('a', { class: 'in-cover', href: links.prs({ repo: repo.id, days, push_class: row.kind || 'unclassified' }), 'data-def': pushKindDef(row.kind) }, pushKindName(row.kind)) },
            { key: 'count', label: 'Pushes', num: true, def: 'Pushes of this kind in the period.', cell: (row) => fixed(row.count) },
            { key: 'share', label: 'Share', num: true, def: 'Share of all pushes in the period.', cell: (row) => (isNum(row.share) ? `${fixed(row.share * 100)}%` : '—') },
          ],
        })) : emptyLine(`No pushes to open pull requests in ${repo.name} in the last ${days} days.`)));
      return nodes;
    },
  });
}

function timeToMerge(data, ctx, days) {
  const ttm = data.time_to_merge || {};
  const blocks = stageBlocks(data.stages);
  if (!isNum(ttm.value) || !blocks.length) {
    return section({ title: 'Time to merge' }, emptyLine(`No pull requests merged in the last ${days} days, so there is nothing to split by stage.`));
  }
  const pr = data.stages.pr;
  const change = delta(ttm.value, ttm.previous, MEASURES.time_to_merge, days);
  const conflicts = Number(data.stages.blocked_by_conflicts) || 0;
  const total = blocks.reduce((sum, block) => sum + block.hours, 0);
  const figureLine = element('p', { class: 'in-figure', 'data-def': MEASURES.time_to_merge.def, tabindex: 0 },
    element('strong', {}, fixed(ttm.value, 1)), ` ${fixed(ttm.value, 1) === '1.0' ? 'day' : 'days'} median time to merge`);
  const changeLine = change
    ? element('p', { class: 'in-delta' }, change.dir === 'flat' ? element('span', { class: 'in-change is-flat' }, 'No change') : changeMark(change), element('span', {}, `vs previous ${days} days`))
    : element('p', { class: 'in-delta' }, 'Not enough history to compare yet.');
  const note = element('p', { class: 'in-note' }, 'Split by stage for the median pull request, ',
    element('a', { class: 'in-link', href: ctx.links.pr(pr) }, `#${pr.number} ${pr.title || ''}`.trim()), `, which took ${hoursText(total)}.`,
    conflicts > 0 ? ` It was also blocked by a merge conflict for ${hoursText(conflicts)} along the way.` : '');
  return section({ title: 'Time to merge' }, figureLine, changeLine, note, ...stageBar(blocks, (stage) => ctx.links.prs({ repo: data.repo?.id || data.scope.repo_id, days, stage })));
}
