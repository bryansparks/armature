// "How is everyone contributing?" Four views of the same people, never one combined score.
import { element } from '../../dom.js';
import { CHANGE_ORDER, changeTypeName, fixed, isNum, quantile, repoShort, short } from './format.js';
import { daysFrom, page, repoToken } from './index.js';
import { emptyLine, filters, noPermission, periodSelect, personCell, repoSelect, skeletonRows, sortableTable } from './ui.js';
import { peopleAnswer, scopeLine } from './words.js';

export const QUESTION = 'How is everyone contributing?';

export const VIEWS = [
  ['shipping', 'Shipping'],
  ['quality', 'Quality'],
  ['review', 'Review work'],
  ['unblocking', 'Unblocking'],
];

// Each view's columns: key, label, unit, digits, definition, and whether a phone keeps it.
export const COLUMNS = {
  shipping: [
    { key: 'merged', label: 'Merged', unit: 'count', def: 'Pull requests this person merged in the period.' },
    { key: 'by_type', label: 'By type', mix: true, opt: true, def: 'Merged pull requests by kind of change: bug fix, enhancement, new feature, maintenance.' },
    { key: 'size_weighted', label: 'Size-weighted', unit: 'count', def: 'Merged pull requests weighted by size: extra small 1, small 2, medium 3, large 5, extra large 8.' },
  ],
  quality: [
    { key: 'rounds_per_pr', label: 'Review rounds', unit: 'rounds', digits: 1, def: 'Average review rounds per merged pull request. Fewer is better.' },
    { key: 'kept_per_round', label: 'Flags kept per round', unit: 'rounds', digits: 2, def: 'Flags the lead reviewer kept, per review round on this person’s merged pull requests.' },
    { key: 'bugs_traced', label: 'Bugs traced', unit: 'count', opt: true, def: 'Later reverts or bug fixes traced back to this person’s merged pull requests.' },
  ],
  review: [
    { key: 'reviews_given', label: 'Reviews given', unit: 'count', def: 'Reviews this person submitted on other people’s pull requests.' },
    { key: 'lines_reviewed', label: 'Lines reviewed', unit: 'count', opt: true, def: 'Lines added and removed in the pull requests this person reviewed.' },
    { key: 'first_review_hours', label: 'Time to first review', unit: 'hours', digits: 1, opt: true, def: 'Median time from opening a pull request to this person’s first review of it.' },
    { key: 'substantive_comments', label: 'Substantive comments', unit: 'count', opt: true, def: 'Review comments about behavior or design, not style.' },
    { key: 'review_hours', label: 'Review hours', unit: 'hours', digits: 1, def: 'Estimated time spent reviewing, from first comment to submit, capped at 3 hours a review.' },
  ],
  unblocking: [
    { key: 'hotspot_prs', label: 'Hotspot fixes', unit: 'count', def: 'Merged pull requests that changed a file that keeps causing problems.' },
    { key: 'platform_prs', label: 'Platform maintenance', unit: 'count', def: 'Merged pull requests labeled as platform maintenance.' },
  ],
};

const MIX_SLOT = { 'bug-fix': 1, enhancement: 2, 'new-feature': 3, 'code-maintenance': 4, 'platform-maintenance': 7 };

export function mixBar(byType) {
  const entries = CHANGE_ORDER.map((id) => [id, Number(byType?.[id]) || 0]).filter(([, count]) => count > 0);
  const total = entries.reduce((sum, [, count]) => sum + count, 0);
  const label = entries.map(([id, count]) => `${changeTypeName(id)} ${count}`).join(', ') || 'None';
  const bar = element('span', { class: 'in-mix', role: 'img', 'aria-label': label, 'data-def': label });
  for (const [id, count] of entries) {
    const part = element('span', { class: MIX_SLOT[id] ? `f${MIX_SLOT[id]}` : 'in-key-median' });
    part.style.flex = `${count / total} 1 0`;
    bar.append(part);
  }
  return bar;
}

// A dot beside a value far from the team: filled above the 80th percentile, an outline below the 20th.
export function standing(value, values) {
  if (!isNum(value)) return null;
  const present = values.filter(isNum);
  if (present.length < 5) return null;
  const high = quantile(present, 0.8);
  const low = quantile(present, 0.2);
  if (high === low) return null;
  if (value > high) return 'high';
  if (value < low) return 'low';
  return null;
}

function cellFor(column, row, rows) {
  if (column.mix) return mixBar(row.measures?.by_type);
  const value = row.measures?.[column.key];
  const where = standing(value, rows.map((other) => other.measures?.[column.key]));
  const text = short(value, column.unit === 'count' || column.unit === 'rounds' ? '' : column.unit, column.digits ?? 0);
  if (!where) return text;
  const words = where === 'high' ? 'Above most of the team' : 'Below most of the team';
  return element('span', { 'data-def': words }, element('span', { class: `in-dot${where === 'low' ? ' is-low' : ''}`, 'aria-hidden': 'true' }), text,
    element('span', { class: 'visually-hidden' }, `, ${words.toLowerCase()}`));
}

export function render(body, ctx) {
  const { query, links, repos } = ctx;
  const days = daysFrom(query);
  const repo = ctx.findRepo(query.get('repo'));
  const view = VIEWS.some(([id]) => id === query.get('view')) ? query.get('view') : 'shipping';
  const filterBar = filters(ctx.teamControl(), periodSelect(days, (value) => ctx.setQuery({ days: value })),
    repoSelect(repos, repo?.id || '', (value) => ctx.setQuery({ repo: repoToken(repos, ctx.findRepo(value)) })));
  if (!ctx.lead) {
    return page(body, ctx, {
      question: QUESTION, what: 'people', load: async () => null,
      draw: (_, head) => {
        head.answerNode.replaceChildren('People pages are visible to team leads.');
        return noPermission('Ask an admin for the team lead role to see how everyone contributes.');
      },
    });
  }
  return page(body, ctx, {
    question: QUESTION, filterBar, what: 'people',
    skeleton: () => [viewTabs(view, ctx, days, repo), skeletonRows(8)],
    load: async () => {
      const scope = { days: String(days), ...(repo ? { repo: repo.id } : {}) };
      const [data, overview] = await Promise.all([
        ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/people?${new URLSearchParams({ ...scope, view })}`),
        view === 'shipping' ? ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/overview?${new URLSearchParams(scope)}`).catch(() => null) : null,
      ]);
      return { ...data, teamMerged: (overview?.metrics || []).find((metric) => metric.id === 'merged')?.value ?? null };
    },
    draw: (data, head) => {
      head.answerNode.replaceChildren(peopleAnswer(data, { teamMerged: data.teamMerged }));
      head.scopeNode.replaceChildren(scopeLine({ scope: data.scope, people: (data.rows || []).length, repos: repos.length, repoName: repo?.name || '', compare: false }));
      const rows = data.rows || [];
      const columns = COLUMNS[view] || COLUMNS.shipping;
      const table = sortableTable({
        caption: `People on the team by ${VIEWS.find(([id]) => id === view)[1].toLowerCase()}, last ${days} days.`,
        sort: { key: columns[0].key, dir: 'descending' }, rows,
        columns: [
          { key: 'name', label: 'Person', wide: true, cell: (row) => personCell(row.name, links.person(row, { days, tab: view }), row.repo ? repoShort(row.repo) : '') },
          ...columns.map((column) => ({
            key: column.key, label: column.label, num: !column.mix, opt: column.opt, def: column.def, sortable: column.mix ? false : undefined,
            value: (row) => row.measures?.[column.key] ?? null, cell: (row) => cellFor(column, row, rows),
          })),
        ],
        foot: (column) => {
          if (column.key === 'name') return 'Team median';
          const spec = columns.find((item) => item.key === column.key);
          if (!spec || spec.mix) return '';
          const value = data.team_median?.[column.key];
          return short(value, spec.unit === 'count' || spec.unit === 'rounds' ? '' : spec.unit, spec.digits ?? (Number(value) % 1 ? 1 : 0));
        },
      });
      const nodes = [viewTabs(view, ctx, days, repo)];
      if (!rows.length) nodes.push(emptyLine(`Nobody opened, merged, or reviewed a pull request in the last ${days} days. Try the last 90 days.`));
      else {
        nodes.push(element('div', { class: 'in-table-gap' }, table));
        if (view === 'shipping') nodes.push(element('ul', { class: 'in-legend in-mix-legend in-opt', 'aria-label': 'Kinds of change' }, CHANGE_ORDER.filter((id) => id !== 'unclassified').map((id) => element('li', {},
          element('span', { class: `in-key in-key-block f${MIX_SLOT[id]}`, 'aria-hidden': 'true' }), changeTypeName(id)))));
        nodes.push(element('p', { class: 'in-note' }, 'A filled dot marks a value above most of the team, an outline one below most of the team. ',
          isNum(data.armature_reviews) && data.armature_reviews ? `Armature also gave ${fixed(data.armature_reviews)} reviews, which aren’t counted here.` : ''));
      }
      return nodes;
    },
  });
}

function viewTabs(view, ctx, days, repo) {
  const list = element('div', { class: 'in-views', role: 'tablist', 'aria-label': 'Views' }, VIEWS.map(([id, label]) => {
    const tab = element('a', { href: ctx.links.people({ days, repo: repo?.id || '', view: id }), role: 'tab', 'aria-selected': String(id === view), tabindex: id === view ? 0 : -1, 'data-view': id }, label);
    tab.addEventListener('click', (clickEvent) => {
      clickEvent.preventDefault();
      ctx.setQuery({ view: id === 'shipping' ? '' : id });
    });
    return tab;
  }));
  list.addEventListener('keydown', (keyEvent) => {
    const ids = VIEWS.map(([id]) => id);
    const index = ids.indexOf(view);
    const moves = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: ids.length - 1 };
    if (!(keyEvent.key in moves)) return;
    keyEvent.preventDefault();
    const next = ids[(moves[keyEvent.key] + ids.length) % ids.length];
    ctx.setQuery({ view: next === 'shipping' ? '' : next }).then(() => document.querySelector(`.in-views [data-view="${next}"]`)?.focus());
  });
  return list;
}

