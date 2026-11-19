// "What fixes are waiting on me?" Proposed fixes for patterns that keep getting flagged.
import { url } from '../../api.js';
import { element } from '../../dom.js';
import { categoryName, daysAgo, fixed, isNum, monthDay, plural, repoShort } from './format.js';
import { page } from './index.js';
import { failedWords } from './models.js';
import { filters, section, select, skeletonRows } from './ui.js';
import { fixesAnswer } from './words.js';

export const QUESTION = 'What fixes are waiting on me?';

export const STATUSES = [
  ['waiting', 'Waiting for you', 'Waiting', 'New fixes are proposed each Monday from the past week’s flags.'],
  ['in_progress', 'In progress', 'In progress', 'No approved fixes are in progress. An approved fix opens as a draft pull request and stays here until it merges.'],
  ['done', 'Done', 'Done', 'No fixes have merged yet. Each merged fix will show how often its pattern was flagged before and since.'],
  ['declined', 'Declined', 'Declined', 'No fixes were declined.'],
];

export const KIND_WORDS = {
  check: 'A new automated check, so the problem is caught before review.',
  sweep: 'A one-time cleanup of the places it was flagged.',
  guidance: 'A change to the review guidance for this repo.',
  author_note: 'Guidance for authors, since no check can catch it.',
};

const where = (pattern) => [repoShort(pattern?.repo), pattern?.area].filter(Boolean).join(' ');
const ago = (iso) => (daysAgo(iso) === 'Today' ? 'today' : `${daysAgo(iso)} ago`);

// What the fix is for, with the pattern linked to Findings filtered to it.
function fixesLine(item, ctx) {
  const pattern = item.pattern || {};
  const href = ctx.links.findings({ category: pattern.label, ...(pattern.repo_id ? { repo: pattern.repo_id } : {}) });
  const count = isNum(pattern.flags) ? `, flagged ${plural(pattern.flags, 'time')} in ${fixed((pattern.days || 28) / 7)} weeks` : '';
  return element('p', { class: 'in-fix-what' }, element('a', { class: 'in-link', href }, categoryName(pattern.label)),
    ` in ${where(pattern) || 'this repo'}${count}.`);
}

// The stored patch, served as plain text, since nothing reaches GitHub before approval.
export const patchHref = (ctx, item) => url(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/fixes/${encodeURIComponent(item.id)}/patch`);

export function diffPreview(diff, href) {
  if (!diff || !Array.isArray(diff.lines)) return null;
  const stats = `${plural(diff.files || 0, 'file')} changed, ${plural(diff.additions || 0, 'addition')}, ${plural(diff.deletions || 0, 'deletion')}`;
  const lines = diff.lines.map(([kind, text]) => element('span', { class: `in-diff-line is-${kind}` }, kind === 'add' ? `+${text}` : kind === 'del' ? `-${text}` : text));
  // Long lines scroll sideways, so the block takes focus and has a name for keyboard and screen reader users.
  return element('div', { class: 'in-diff' },
    element('p', { class: 'in-diff-stats' }, stats),
    element('pre', { class: 'in-diff-code', tabindex: '0', role: 'region', 'aria-label': 'The first changed lines' }, lines),
    href ? patchLink(href, diff.more ? 'See the whole change' : 'See the change as text') : null);
}

// element() drops a link that starts with the server's base path, and this one the server built, so it is set
// directly.
function patchLink(href, text) {
  const link = element('a', { class: 'in-link' }, text);
  link.setAttribute('href', href);
  return link;
}

function decideActions(item, ctx, status, expiry) {
  const approve = element('button', { type: 'button', class: 'in-button' }, 'Approve fix');
  const decline = element('button', { type: 'button', class: 'in-button is-quiet' }, 'Decline');
  const actions = element('div', { class: 'in-actions' }, approve, decline);
  const reasonId = `in-fix-reason-${item.id}`;
  const reason = element('input', { type: 'text', id: reasonId, class: 'in-input', maxlength: '200' });
  const confirm = element('button', { type: 'button', class: 'in-button' }, 'Decline fix');
  const back = element('button', { type: 'button', class: 'in-button is-quiet' }, 'Cancel');
  const asking = element('div', { class: 'in-fix-decline', hidden: true },
    element('label', { class: 'in-select-label', for: reasonId }, 'Why decline? One line, optional'), reason,
    element('div', { class: 'in-actions' }, confirm, back));
  const buttons = [approve, decline, confirm, back];
  const act = async (verb, note, done) => {
    buttons.forEach((button) => { button.disabled = true; });
    status.textContent = '';
    try {
      const gate = encodeURIComponent(item.gate_id);
      const path = verb === 'approve' ? `/api/gates/${gate}/approve` : `/api/gates/${gate}/reject`;
      await ctx.post(path, { nonce: item.nonce, ...(note ? { note } : {}) });
      actions.replaceWith();
      asking.replaceWith();
      expiry?.replaceWith();
      status.textContent = done;
    } catch (err) {
      status.textContent = failedWords(verb === 'approve' ? 'approve' : 'decline', err);
      buttons.forEach((button) => { button.disabled = false; });
    }
  };
  const repo = repoShort(item.pattern?.repo) || 'the repo';
  approve.addEventListener('click', () => act('approve', null, `Approved. Armature pushes it and opens a draft pull request in ${repo}.`));
  decline.addEventListener('click', () => { actions.hidden = true; asking.hidden = false; });
  back.addEventListener('click', () => { asking.hidden = true; actions.hidden = false; });
  confirm.addEventListener('click', () => act('reject', String(reason.value || '').trim(), `Declined. Nothing reaches ${repo}.`));
  return [actions, asking];
}

function waitingItem(item, ctx) {
  const status = element('p', { class: 'in-rec-done', 'aria-live': 'polite' });
  const expiry = item.expires_at ? element('p', { class: 'in-fix-meta' }, `Waits for a decision until ${monthDay(item.expires_at)}, then it’s dropped.`) : null;
  const actions = item.may_decide && item.nonce ? decideActions(item, ctx, status, expiry) : null;
  if (!actions) status.textContent = 'A team lead approves this fix.';
  return element('li', { class: 'in-fix' },
    element('h3', { class: 'in-fix-title' }, item.title),
    fixesLine(item, ctx),
    item.kind && KIND_WORDS[item.kind] ? element('p', { class: 'in-fix-kind' }, KIND_WORDS[item.kind]) : null,
    diffPreview(item.diff, item.has_patch ? patchHref(ctx, item) : null),
    expiry, actions, status);
}

function prLink(item) {
  const what = item.state === 'opened' ? 'Draft pull request' : 'Pull request';
  return item.pr?.url ? element('a', { class: 'in-link', href: item.pr.url }, `${what} #${item.pr.number}`) : null;
}

export function progressWords(item) {
  if (item.state === 'opened') return `Open as a draft, opened ${ago(item.updated_at)}.`;
  if (item.state === 'approved') return 'Approved. Armature is pushing it and opening the draft pull request.';
  return `Armature is writing this fix. Started ${ago(item.created_at)}.`;
}

export function doneWords(item) {
  if (item.state === 'closed') return 'Closed without merging.';
  const counts = isNum(item.flags_before) ? ` Flagged ${plural(item.flags_before, 'time')} in the 30 days before, ${fixed(item.flags_after || 0)} since.` : '';
  return `Merged ${monthDay(item.merged_at)}.${counts}`;
}

export function declinedWords(item) {
  if (item.state === 'expired') return `Nobody decided by ${monthDay(item.decided_at)}, so it was dropped.`;
  if (item.state === 'failed') return `Armature couldn’t make this fix. ${item.failure_reason || ''}`.trim();
  const who = item.decided_by ? ` by ${item.decided_by}` : '';
  return `Declined${who} on ${monthDay(item.decided_at)}.${item.decline_reason ? ` “${item.decline_reason}”` : ''}`;
}

function otherItem(item, ctx, status) {
  const text = status === 'in_progress' ? progressWords(item) : status === 'done' ? doneWords(item) : declinedWords(item);
  const pr = prLink(item);
  return element('li', { class: 'in-fix' },
    element('h3', { class: 'in-fix-title' }, item.title),
    fixesLine(item, ctx),
    element('p', { class: 'in-fix-meta' }, pr, pr ? ' · ' : null, text));
}

export function fixList(items, status, ctx) {
  return element('ul', { class: 'in-fixes' }, items.map((item) => (status === 'waiting' ? waitingItem(item, ctx) : otherItem(item, ctx, status))));
}

export function render(body, ctx) {
  const status = STATUSES.some(([id]) => id === ctx.query.get('status')) ? ctx.query.get('status') : 'waiting';
  const filterBar = filters(ctx.teamControl(), select({
    label: 'Status', value: status, options: STATUSES.map(([id, , label]) => [id, label]), onChange: (value) => ctx.setQuery({ status: value === 'waiting' ? '' : value }),
  }));
  return page(body, ctx, {
    question: QUESTION, filterBar, what: 'fixes',
    skeleton: () => skeletonRows(3),
    load: () => ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/fixes?status=${encodeURIComponent(status)}`),
    draw: (data, head) => {
      head.answerNode.replaceChildren(fixesAnswer(data));
      head.scopeNode.replaceChildren('A fix is proposed when the same pattern is flagged again and again. It can be a new automated check, a one-time cleanup, or a change to review or author guidance.');
      const [, title, , empty] = STATUSES.find(([id]) => id === status);
      const items = data[status] || [];
      return section({ title, count: items.length || null }, items.length ? fixList(items, status, ctx)
        : element('div', { class: 'in-explain' }, element('p', {}, empty),
          status === 'waiting' ? element('p', {}, element('a', { class: 'in-link', href: ctx.links.findings() }, 'See what keeps getting flagged')) : null));
    },
  });
}
