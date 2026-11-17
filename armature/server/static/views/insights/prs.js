// "Which pull requests?" The list behind a stage, a kind of update, a category, or a person, each row leading to the
// pull request's page.
import { element } from '../../dom.js';
import { STAGES, fixed, isNum, monthDay, repoShort, short } from './format.js';
import { daysFrom, page, repoToken } from './index.js';
import { emptyLine, filters, periodSelect, repoSelect, section, skeletonRows, sortableTable } from './ui.js';
import { prsAnswer, scopeLine } from './words.js';

export const QUESTION = 'Which pull requests?';
const STAGE_KEYS = ['waiting_first_review', 'in_review', 'waiting_on_ci', 'approved_to_merged', 'blocked'];

export const stageLabel = (key) => (key === 'blocked' ? 'Blocked by conflicts' : STAGES.find(([id]) => id === key)?.[1] || key);

// The API filters this screen understands, read from the URL. Unknown stages are dropped.
export function listFilters(query) {
  const stage = STAGE_KEYS.includes(query.get('stage')) ? query.get('stage') : '';
  return { stage, push_class: stage ? '' : query.get('push_class') || '', category: query.get('category') || '', person: query.get('person') || '' };
}

export function render(body, ctx) {
  const { query, links, repos } = ctx;
  const days = daysFrom(query);
  const repo = ctx.findRepo(query.get('repo'));
  const chosen = listFilters(query);
  const params = new URLSearchParams({ days: String(days) });
  if (repo) params.set('repo', repo.id);
  for (const [key, value] of Object.entries(chosen)) if (value && (key !== 'person' || ctx.lead)) params.set(key, value);
  const filterBar = filters(ctx.teamControl(), periodSelect(days, (value) => ctx.setQuery({ days: value })),
    repoSelect(repos, repo?.id || '', (value) => ctx.setQuery({ repo: repoToken(repos, ctx.findRepo(value)) })));
  return page(body, ctx, {
    question: QUESTION, filterBar, what: 'the pull requests',
    skeleton: () => [section({ title: 'Pull requests' }, skeletonRows(8))],
    load: () => ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/prs?${params}`),
    draw: (data, head) => {
      const prs = data.prs || [];
      const personName = chosen.person ? prs.find((item) => item.author)?.author || '' : '';
      head.answerNode.replaceChildren(prsAnswer(data, { repoName: repo?.name || '', personName }));
      head.scopeNode.replaceChildren(scopeLine({ scope: data.scope, repos: repo ? null : repos.length, repoName: repo?.full_name || '', compare: false }));
      const clear = chosen.stage || chosen.push_class || chosen.category || chosen.person ? element('a', { class: 'in-link', href: links.prs({ days, repo: repo?.id || '' }) }, 'Show all pull requests') : null;
      if (!prs.length) return section({ title: 'Pull requests', link: clear }, emptyLine(`Try the last 90 days${repo ? ' or all repos' : ''}.`));
      const hasAuthor = prs.some((item) => item.author);
      const columns = [
        { key: 'title', label: 'Pull request', wide: true, cellClass: 'in-title', value: (item) => item.title || '',
          cell: (item) => [element('a', { class: 'in-cover', href: links.pr(item) }, item.title || `#${item.number}`), element('span', { class: 'in-sub' }, `${repoShort(item.repo)} #${item.number}`)] },
        hasAuthor ? { key: 'author', label: 'Author', opt: true, cellClass: 'in-nowrap', cell: (item) => item.author || '—' } : null,
        chosen.stage ? { key: 'stage_hours', label: stageLabel(chosen.stage), num: true, def: 'Hours this pull request spent in the stage.', cell: (item) => short(item.stage_hours, 'hours', Number(item.stage_hours) < 10 ? 1 : 0) } : null,
        { key: 'rounds', label: 'Rounds', num: true, opt: Boolean(chosen.stage), def: 'Review rounds before merge, or so far.', cell: (item) => (isNum(item.rounds) ? fixed(item.rounds) : '—') },
        { key: 'date', label: 'Merged or opened', num: true, value: (item) => item.merged_at || item.created_at || '', cell: (item) => monthDay(item.merged_at || item.created_at, false) },
      ].filter(Boolean);
      const more = isNum(data.total) && data.total > prs.length ? element('p', { class: 'in-note' }, `Showing the first ${fixed(prs.length)} of ${fixed(data.total)}.`) : null;
      return section({ title: 'Pull requests', count: data.total ?? prs.length, link: clear },
        element('div', { class: 'in-table-gap' }, sortableTable({
          caption: 'Pull requests.', rows: prs, columns, sort: chosen.stage ? { key: 'stage_hours', dir: 'descending' } : { key: 'date', dir: 'descending' },
        })), more);
    },
  });
}
