// The bottom of every drill path: one pull request's timeline, its review rounds and flags, and its checks.
import { element } from '../../dom.js';
import { categoryName, changeTypeName, figureText, fixed, monthDay, plural, pushKindName, repoShort, specialistName } from './format.js';
import { page } from './index.js';
import { emptyLine, events, mark, section, skeletonRows, sortableTable } from './ui.js';
import { prAnswer } from './words.js';

export const QUESTION = 'What happened in this pull request?';

const STATE_WORDS = { approved: 'approved', changes_requested: 'asked for changes', commented: 'commented', dismissed: 'was dismissed' };
const FAILED = new Set(['failure', 'timed_out', 'startup_failure']);
const FLAG = {
  kept: ['ok', 'Kept'],
  dropped: ['warn', 'Dropped'],
};

const at = (iso) => new Date(iso || 0).getTime();

// Every dated thing that happened, oldest first.
export function timeline(data) {
  const pr = data.pr || {};
  const rows = [];
  if (pr.created_at) rows.push([pr.created_at, `Opened${pr.author ? ` by ${pr.author}` : ''}${pr.draft ? ' as a draft' : ''}.`]);
  for (const push of data.pushes || []) rows.push([push.at, `${push.armature ? 'Armature pushed' : 'Pushed'}: ${pushKindName(push.kind).toLowerCase()}.`, push.url || null]);
  (data.rounds || []).forEach((round, index) => {
    if (!round.at) return;
    const who = (round.reviews || []).map((review) => `${review.reviewer} ${STATE_WORDS[review.state] || review.state}`);
    rows.push([round.at, `Review round ${index + 1}${who.length ? `: ${who.join(', ')}` : ''}.`]);
  });
  for (const conflict of data.conflicts || []) rows.push([conflict.start, `Blocked by a merge conflict for ${figureText(conflict.hours, 'hours', conflict.hours < 10 ? 1 : 0)}.`]);
  for (const check of data.checks || []) if (FAILED.has(check.conclusion)) rows.push([check.completed_at || check.started_at, `Check ${check.name} ${check.conclusion === 'timed_out' ? 'timed out' : 'failed'}.`]);
  if (data.merge?.at) rows.push([data.merge.at, 'Merged.']);
  else if (pr.closed_at) rows.push([pr.closed_at, 'Closed without merging.']);
  for (const link of data.links || []) {
    if (!link.pr) continue;
    const text = link.direction === 'fixed_by' ? `${link.kind === 'revert' ? 'Reverted' : 'Fixed'} by #${link.pr.number} ${link.pr.title || ''}` : `${link.kind === 'revert' ? 'Reverts' : 'Fixes'} #${link.pr.number} ${link.pr.title || ''}`;
    rows.push([null, `${text.trim()}.`, { pr: link.pr }]);
  }
  return rows.filter(([when, text]) => text && (when || when === null)).sort((a, b) => (a[0] === null) - (b[0] === null) || at(a[0]) - at(b[0]));
}

export function render(body, ctx) {
  const [, first = '', number = ''] = ctx.args;
  const repo = number ? ctx.findRepo(first) : null;
  return page(body, ctx, {
    question: QUESTION, what: 'this pull request',
    skeleton: () => [section({ title: 'Timeline' }, skeletonRows(6))],
    load: async () => {
      try {
        if (number && !repo) return { missing: true };
        if (repo) return await ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/repos/${encodeURIComponent(repo.id)}/prs/${encodeURIComponent(number)}`);
        return await ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/prs/${encodeURIComponent(first)}`);
      } catch (err) {
        if (err.status === 404 || err.status === 400) return { missing: true };
        throw err;
      }
    },
    draw: (data, head) => {
      if (data.missing) {
        head.title.textContent = 'No such pull request';
        head.answerNode.replaceChildren('This pull request isn’t tracked for this team.');
        return element('p', { class: 'in-empty' }, element('a', { class: 'in-link', href: ctx.links.overview() }, 'Open the overview'));
      }
      const pr = data.pr || {};
      const repo = repoShort(pr.repo);
      const question = `What happened in ${repo} #${pr.number}?`;
      head.title.textContent = question;
      document.title = `${question} | Armature`;
      head.answerNode.replaceChildren(prAnswer(data));
      const size = [pr.additions != null ? `${fixed(pr.additions)} lines added` : '', pr.deletions != null ? `${fixed(pr.deletions)} removed` : ''].filter(Boolean).join(', ');
      head.scopeNode.replaceChildren([size, pr.size ? `size ${pr.size}` : '', pr.change_type ? changeTypeName(pr.change_type).toLowerCase() : ''].filter(Boolean).join(', ') + (size ? '. ' : ''),
        pr.url ? element('a', { class: 'in-link', href: pr.url }, 'Open on GitHub') : null);
      head.node.before(element('nav', { class: 'in-crumbs', 'aria-label': 'Breadcrumb' }, element('ol', {},
        element('li', {}, element('a', { href: ctx.links.overview() }, 'Insights')),
        pr.repo_id ? element('li', {}, element('a', { href: ctx.links.repo(pr.repo_id) }, repo)) : null,
        element('li', { 'aria-current': 'page' }, `#${pr.number}`))));
      const rows = timeline(data).map(([when, text, href]) => [when ? monthDay(when, false) : 'Later', text,
        href?.pr ? ctx.links.pr(href.pr) : href]);
      return [
        section({ title: 'Timeline' }, rows.length ? events(rows, 'in-timeline') : emptyLine('Nothing recorded yet.')),
        rounds(data, ctx),
        checks(data),
      ];
    },
  });
}

function rounds(data) {
  const list = data.rounds || [];
  if (!list.length) return section({ title: 'Review rounds' }, emptyLine('No reviews yet.'));
  return section({ title: 'Review rounds', count: list.length }, element('div', {}, list.map((round, index) => {
    const flags = round.flags || [];
    const reviews = round.reviews || [];
    const who = reviews.map((review, n) => [n ? ', ' : '', review.url ? element('a', { href: review.url }, review.reviewer) : review.reviewer, ` ${STATE_WORDS[review.state] || review.state}`]);
    return element('div', { class: 'in-round' },
      element('h3', {}, round.head_sha ? `Round ${index + 1}` : 'Not tied to a commit', round.at ? element('span', { class: 'in-sub' }, monthDay(round.at)) : null),
      reviews.length ? element('p', { class: 'in-round-reviews' }, who, '.') : null,
      (round.comments || []).length ? element('p', { class: 'in-round-reviews' }, `${plural(round.comments.length, 'review comment')}.`) : null,
      flags.length ? element('ul', { class: 'in-flags', 'aria-label': `Flags in round ${index + 1}` }, flags.map((flag) => {
        const [icon, word] = FLAG[flag.status] || ['dash', 'Not decided yet'];
        return element('li', {}, mark(icon, word), element('span', { class: 'in-flag-claim' }, flag.claim || 'No detail recorded'),
          element('span', { class: 'in-flag-meta' }, element('span', { class: `in-status is-${flag.status || 'open'}` }, word), `. ${specialistName(flag.specialist)} review, ${categoryName(flag.category).toLowerCase()}. `,
            flag.url ? element('a', { href: flag.url }, `${flag.path || 'Line'}${flag.line ? `:${flag.line}` : ''}`) : flag.path || ''));
      })) : element('p', { class: 'in-round-reviews' }, 'No flags in this round.'));
  })));
}

function checks(data) {
  const list = data.checks || [];
  const head = data.pr?.head_sha;
  const final = list.filter((check) => check.head_sha === head);
  const shown = final.length ? final : list;
  if (!shown.length) return null;
  const failed = shown.filter((check) => FAILED.has(check.conclusion)).length;
  return section({ title: 'Checks on the final commit', count: shown.length, takeaway: failed ? `${plural(failed, 'check')} failed.` : 'Every check passed.' },
    element('div', { class: 'in-table-gap' }, sortableTable({
      rowClass: '', caption: 'Checks on the final commit.', sort: { key: 'name', dir: 'ascending' }, rows: shown,
      columns: [
        { key: 'name', label: 'Check', wide: true },
        { key: 'conclusion', label: 'Result', cell: (check) => element('span', { class: 'in-person' }, mark(FAILED.has(check.conclusion) ? 'bad' : check.conclusion === 'success' ? 'ok' : 'dash', null), conclusionWord(check)) },
        { key: 'completed_at', label: 'Finished', num: true, opt: true, value: (check) => check.completed_at || '', cell: (check) => (check.completed_at ? monthDay(check.completed_at, false) : '—') },
      ],
    })));
}

const conclusionWord = (check) => ({ success: 'Passed', failure: 'Failed', timed_out: 'Timed out', startup_failure: 'Failed to start', cancelled: 'Cancelled', skipped: 'Skipped', neutral: 'Neutral' }[check.conclusion] || (check.status === 'completed' ? 'Finished' : 'Running'));
