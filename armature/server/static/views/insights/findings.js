// "What keeps getting flagged?" Top categories, one category over time, how often each reviewer's flags were
// dropped, and the dropped flags that became bugs.
import { element } from '../../dom.js';
import { trendChart } from './chart.js';
import { categoryDef, categoryName, delta, fixed, isNum, monthDay, plural, repoShort, specialistName } from './format.js';
import { daysFrom, page, repoToken } from './index.js';
import { changeMark, emptyLine, events, filters, mark, openLoadedDrawer, periodSelect, ranked, repoSelect, section, select, skeletonRows, sortableTable } from './ui.js';
import { findingsAnswer, prLabel, scopeLine } from './words.js';

export const QUESTION = 'What keeps getting flagged?';
const MORE_IS_BAD = { better: 'down', change: 'percent', unit: 'count', digits: 0 };

export function render(body, ctx) {
  const { query, links, repos } = ctx;
  const days = daysFrom(query);
  const repo = ctx.findRepo(query.get('repo'));
  const category = query.get('category') || '';
  const person = ctx.lead ? query.get('person') || '' : '';
  const params = new URLSearchParams({ days: String(days) });
  if (repo) params.set('repo', repo.id);
  if (category) params.set('category', category);
  if (person) params.set('person', person);
  const categoryHolder = element('span');
  const personHolder = element('span');
  const filterBar = filters(ctx.teamControl(), periodSelect(days, (value) => ctx.setQuery({ days: value })),
    repoSelect(repos, repo?.id || '', (value) => ctx.setQuery({ repo: repoToken(repos, ctx.findRepo(value)) })), categoryHolder, personHolder);

  return page(body, ctx, {
    question: QUESTION, filterBar, what: 'findings',
    skeleton: () => [section({ title: 'Top categories' }, skeletonRows(6))],
    load: async () => {
      const [data, people] = await Promise.all([
        ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/findings?${params}`),
        ctx.lead ? ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/people?days=90`).then((payload) => payload.rows || []).catch(() => []) : [],
      ]);
      return { data, people };
    },
    draw: ({ data, people }, head) => {
      const categories = data.categories || [];
      const options = categories.map((row) => [row.category, categoryName(row.category)]);
      if (category && !options.some(([id]) => id === category)) options.push([category, categoryName(category)]);
      categoryHolder.replaceChildren(select({ label: 'Category', value: category, options: [['', 'All categories'], ...options], onChange: (value) => ctx.setQuery({ category: value }) }));
      if (ctx.lead) {
        const choices = [...people].sort((a, b) => a.name.localeCompare(b.name)).map((row) => [row.id, row.name]);
        if (person && !choices.some(([id]) => id === person)) choices.push([person, 'Chosen person']);
        personHolder.replaceChildren(select({ label: 'Person', value: person, options: [['', 'Everyone'], ...choices], onChange: (value) => ctx.setQuery({ person: value }) }));
      }
      const personName = people.find((row) => row.id === person)?.name || '';
      const where = [repo?.name, personName ? `on ${personName}’s pull requests` : ''].filter(Boolean).join(' ');
      head.answerNode.replaceChildren(findingsAnswer(data, where));
      head.scopeNode.replaceChildren(scopeLine({ scope: data.scope, repos: repos.length, repoName: repo?.name || '' }));
      const prsLink = category ? element('a', { class: 'in-link', href: links.prs({ days, repo: repo?.id || '', category, person }) }, 'See pull requests') : null;
      return [
        topCategories(data, ctx, category),
        category ? overTime(data, ctx, category, prsLink) : null,
        specialists(data, days, ctx),
        becameBugs(data, ctx, days),
      ];
    },
  });
}

function topCategories(data, ctx, current) {
  const rows = (data.categories || []).filter((row) => row.count > 0 || row.category === current);
  const unlabeled = data.unlabeled || 0;
  const list = ranked(rows.map((row) => ({
    name: categoryName(row.category), def: categoryDef(row.category), amount: row.count, value: fixed(row.count),
    after: changeMark(delta(row.count, row.previous, MORE_IS_BAD, data.scope.days)), current: row.category === current,
    onClick: () => ctx.setQuery({ category: row.category === current ? '' : row.category }),
  })), { label: 'Top categories' });
  return section({
    title: 'Top categories', count: rows.length || null,
    takeaway: rows.length ? 'Select a category to filter the page and see it week by week.' : null,
  }, rows.length ? list : emptyLine(`Nothing was flagged in the last ${data.scope.days} days. Try the last 90 days.`),
  unlabeled ? element('p', { class: 'in-note' }, `${plural(unlabeled, 'flag')} ${unlabeled === 1 ? 'isn’t' : 'aren’t'} in a category yet. New flags are sorted into categories each night.`) : null);
}

function overTime(data, ctx, category, prsLink) {
  const weekly = data.weekly || {};
  const weeks = weekly.weeks || [];
  const series = Object.entries(weekly.repos || {}).map(([repoId, values]) => ({
    name: ctx.repoName(repoId) || 'Unknown repo', slot: ctx.slots.get(repoId) || 1, values, href: ctx.links.repo(repoId),
  })).sort((a, b) => a.slot - b.slot);
  const name = categoryName(category);
  if (!weeks.length || !series.length) return section({ title: `${name}, week by week`, link: prsLink }, emptyLine('Not enough history to draw this category yet.'));
  const totals = weeks.map((_, index) => series.reduce((sum, item) => sum + (Number(item.values[index]) || 0), 0));
  const recent = totals.slice(-4).reduce((sum, value) => sum + value, 0);
  const before = totals.slice(-8, -4).reduce((sum, value) => sum + value, 0);
  const words = recent === before ? `Flagged ${plural(recent, 'time')} in the last 4 weeks, the same as the 4 before.`
    : `Flagged ${plural(recent, 'time')} in the last 4 weeks, ${recent > before ? 'up' : 'down'} from ${fixed(before)} in the 4 before.`;
  return section({ title: `${name}, week by week`, takeaway: words, link: prsLink },
    trendChart({ dates: weeks, series, label: `${name} flags per week`, basis: 'Flags per week, by repo', legendLabel: 'Repos', format: (value) => (isNum(value) ? fixed(value) : '—') }));
}

function specialists(data, days, ctx) {
  const rows = data.specialists || [];
  const open = (row, opener) => openLoadedDrawer({
    title: `${specialistName(row.specialist)} review`, evidenceTitle: 'Its most recent dropped flags', what: 'its dropped flags',
    why: `Raised ${plural(row.raised, 'flag')} in the last ${days} days. ${shareText(row.kept_share)} kept, ${shareText(row.dropped_share)} dropped.`,
    load: () => ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/findings/specialists/${encodeURIComponent(row.specialist)}?days=${days}`),
    fill: (found) => ({
      why: `Raised ${plural(found.raised, 'flag')} in the last ${days} days, and ${plural(found.dropped, 'was', 'were')} dropped.`,
      evidence: (found.flags || []).length
        ? events(found.flags.map((flag) => [monthDay(flag.at, false), `${prLabel(flag.pr)}: ${flag.claim || 'no detail recorded'}`, ctx.links.pr(flag.pr)]))
        : element('p', { class: 'in-drawer-why' }, 'None of its flags were dropped in this period.'),
    }),
  }, opener);
  return section({ title: 'How often flags were dropped', takeaway: rows.length ? 'By reviewer. The last column matters most: a dropped flag that later turned into a bug.' : null },
    rows.length ? element('div', { class: 'in-table-gap' }, sortableTable({
      caption: `Flags by reviewer, last ${days} days.`, sort: { key: 'dropped_then_bug', dir: 'descending' }, rows,
      columns: [
        { key: 'specialist', label: 'Reviewer', wide: true, value: (row) => specialistName(row.specialist), cell: (row) => {
          const button = element('button', { type: 'button', class: 'in-cover', 'aria-haspopup': 'dialog' }, specialistName(row.specialist));
          button.addEventListener('click', () => open(row, button));
          return button;
        } },
        { key: 'raised', label: 'Flags raised', num: true, def: 'Flags this reviewer raised in the period.', cell: (row) => fixed(row.raised) },
        { key: 'kept_share', label: 'Kept', num: true, opt: true, def: 'Share of its flags the lead reviewer kept.', cell: (row) => shareText(row.kept_share) },
        { key: 'dropped_share', label: 'Dropped', num: true, opt: true, def: 'Share of its flags the lead reviewer dropped. The rest weren’t decided.', cell: (row) => shareText(row.dropped_share) },
        { key: 'dropped_then_bug', label: 'Dropped, then a bug', num: true, def: 'Dropped flags on pull requests that were later reverted or fixed in the same file.', cell: (row) => fixed(row.dropped_then_bug) },
      ],
    })) : emptyLine(`No flags were raised in the last ${days} days.`));
}

const shareText = (value) => (isNum(value) ? `${fixed(value * 100)}%` : '—');

function becameBugs(data, ctx, days) {
  const rows = data.dropped_became_bugs || [];
  const list = element('ul', { class: 'in-flags' }, rows.map((item) => element('li', {}, mark('bad', 'Became a bug'),
    element('span', { class: 'in-flag-claim' }, element('a', { class: 'in-text-link', href: ctx.links.pr(item.pr) }, `${prLabel(item.pr)} ${item.pr?.title || ''}`.trim())),
    element('span', { class: 'in-flag-meta' }, `${specialistName(item.flag?.specialist)} flagged “${item.flag?.claim || 'no detail'}” on ${monthDay(item.flag?.at)}. `,
      `${item.kind === 'revert' ? 'Reverted' : 'Fixed'} by #${item.bug_pr?.number}${item.bug_pr?.title ? ` ${item.bug_pr.title}` : ''}. `,
      item.flag?.url ? element('a', { href: item.flag.url }, `View ${item.flag.path ? repoShort(item.flag.path) : 'the line'}${isNum(item.flag.line) ? ` line ${item.flag.line}` : ''} on GitHub`) : null))));
  return section({ title: 'Dropped flags that became bugs', count: rows.length || null },
    rows.length ? list : emptyLine(`No dropped flags have led to a bug in the last ${days} days.`));
}
