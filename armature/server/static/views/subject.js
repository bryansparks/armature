import { backfill, RunActivity } from '../activity.js';
import { api, listOf } from '../api.js';
import { chatPanel } from '../conversation.js';
import { append, card, clear, element, empty, errorBox, link, loading, money, pageHeader, statusText, statusTone, table, when } from '../dom.js';
import { loadDisplay } from '../display.js';
import { every, subscribe } from '../live.js';
import { externalLink, externalLinks, outcomeSentence, riskBadge, threadRiskBadge } from '../outcome.js';
import { resultView } from '../result.js';
import { hashFor } from '../router.js';
import { tabIdFor } from '../runpage.js';
import { openTail } from '../tail.js';
import { runStatus, verdictChip, verdictTone } from './runs.js';

const PAGE_SIZE = 50;
const EVENTS_PAGE = 1000;
const POLL_MS = 15000;
const REDRAW_LIMIT = 5;
const REDRAW_RESET_MS = 60000;
const REDRAW_BASE_MS = 1000;
const REDRAW_MAX_MS = 30000;
const RUN_STATUSES = new Set(['queued', 'running', 'succeeded', 'failed', 'canceled', 'lost']);
const ACTIVE = new Set(['queued', 'running']);
const DISPOSITIONS = {
  keep: 'Kept', resolved: 'Resolved', drop_rebutted: 'Rebutted', act: 'Act on', defer: 'Deferred', block: 'Blocking',
  suggest: 'Suggested', change: 'Change', conflict: 'Conflict',
};
const DISPOSITION_TONE = { resolved: 'ok', drop_rebutted: 'ok', block: 'bad', keep: 'warn', act: 'warn', change: 'warn' };

const eventsPath = (runId, after) => `/api/runs/${encodeURIComponent(runId)}/events?after=${after}&limit=${EVENTS_PAGE}`;

// Apache refuses %2F in a path, so the slashes stay literal for the {subject:path} route.
export const subjectPath = (subject) => `/api/subjects/${String(subject).split('/').map(encodeURIComponent).join('/')}`;

export const subjectPageHref = (subject, tab) => hashFor('subject', tab ? [subject, tab] : [subject]);

// A web conversation's thread is web:<conversation id>.
export const webConversation = (subject) => (/^web:[0-9a-f-]{36}$/.test(String(subject)) ? String(subject).slice(4) : null);

export function statusLabel(status) {
  if (!status) return 'Unknown';
  if (status === 'merged') return 'Merged';
  if (RUN_STATUSES.has(status)) return statusText(status);
  return String(status).replace(/_/g, ' ');
}

export function statusCapsuleTone(status) {
  if (status === 'merged') return 'ok';
  if (RUN_STATUSES.has(status)) return statusTone(status);
  return verdictTone(status) || 'unknown';
}

function statusCapsule(status) {
  return element('span', { class: `state state-${statusCapsuleTone(status)}` }, statusLabel(status));
}

export function dispositionLabel(disposition) {
  if (!disposition) return 'Raised';
  return DISPOSITIONS[disposition] || String(disposition).replace(/_/g, ' ');
}

function dispositionChip(disposition, text = dispositionLabel(disposition)) {
  return element('span', { class: `chip ${DISPOSITION_TONE[disposition] || ''}`.trim() }, text);
}

export function totals(rows) {
  return rows.reduce((sum, row) => ({
    subjects: sum.subjects + 1, runs: sum.runs + Number(row.runs || 0), reviews: sum.reviews + Number(row.reviews || 0),
    fixes: sum.fixes + Number(row.fixes || 0), cost: row.cost_usd === null || row.cost_usd === undefined ? sum.cost : (sum.cost ?? 0) + Number(row.cost_usd),
  }), { subjects: 0, runs: 0, reviews: 0, fixes: 0, cost: null });
}

const plural = (count, one, many = `${one}s`) => `${count} ${count === 1 ? one : many}`;

export function workText(row) {
  const parts = [plural(Number(row.reviews || 0), 'review'), plural(Number(row.fixes || 0), 'fix', 'fixes')];
  const other = Number(row.runs || 0) - Number(row.reviews || 0) - Number(row.fixes || 0);
  if (other > 0) parts.push(plural(other, 'other run'));
  return parts.join(', ');
}

// What a fix, merge, or port run did, in a few words from what it reported.
export function outcomeText(run) {
  const fix = run.fix || {};
  const merge = run.merge || {};
  if (run.kind === 'fix') {
    const counts = [fix.applied !== undefined ? `${fix.applied} applied` : null, fix.skipped ? `${fix.skipped} skipped` : null].filter(Boolean).join(', ');
    return [fix.outcome ? String(fix.outcome).replace(/[_-]/g, ' ') : null, counts || null, fix.commit ? `commit ${fix.commit}` : null,
      fix.requester ? `asked by ${fix.requester}` : null].filter(Boolean).join(' · ') || run.verdict || statusLabel(run.status);
  }
  if (run.kind === 'merge') {
    const moved = merge.issue ? (merge.transitioned ? `${merge.issue} moved to ${merge.moved_to || 'the next status'}` : `${merge.issue} not moved`) : null;
    return [merge.merged_by ? `Merged by ${merge.merged_by}` : 'Merged', moved].filter(Boolean).join(' · ');
  }
  const fields = run.fields && typeof run.fields === 'object' ? Object.entries(run.fields).map(([name, value]) => `${name} ${value}`) : [];
  return [run.verdict, ...fields].filter(Boolean).join(' · ') || statusLabel(run.status);
}

const runHref = (run, artifact) => hashFor('run', artifact ? [run.id, tabIdFor(artifact)] : [run.id]);

// While a run is in progress, the thread's verdict is the last run's, so it says so and stays quiet.
function headerChips(detail, current) {
  if (!current || !ACTIVE.has(current.status)) {
    return [statusCapsule(detail.status), detail.status !== detail.latest_verdict ? verdictChip(detail.latest_verdict) : null, badgeChip(threadRiskBadge(detail.risk))];
  }
  return RUN_STATUSES.has(detail.status) ? [] : [element('span', { class: 'state state-unknown' }, `Last result: ${statusLabel(detail.status)}`)];
}

function linkRow(links) {
  const shown = externalLinks({ links });
  return shown.length ? element('nav', { class: 'run-links', 'aria-label': 'Links' }, shown.map(externalLink)) : null;
}

// The run a thread is about now: one in progress, else the newest.
export const currentRun = (runs) => [...runs].reverse().find((run) => ACTIVE.has(run.status)) || runs.at(-1) || null;

const runLabel = (run) => run.workflow_label || run.workflow || 'Run';

const badgeChip = (badge) => (badge ? element('span', { class: `chip ${badge.tone}`.trim(), title: badge.title }, badge.text) : null);

const riskChip = (risk, reason = null) => badgeChip(riskBadge(risk, reason));

function outcomeCapsule(run) {
  const status = runStatus(run, null);
  return element('span', { class: `state state-${status.tone}` }, status.text);
}

// What the run did in words, when that says more than its outcome capsule.
function moreThanOutcome(run) {
  const text = outcomeText(run);
  return text && text.toLowerCase() !== runStatus(run, null).text.toLowerCase() && text !== run.verdict ? text : '';
}

function runLine(run) {
  const sentence = outcomeSentence(run.outcome);
  const risk = riskChip(run.risk, run.risk_reason);
  return element('p', { class: 'run-line' }, element('strong', {}, runLabel(run)), sentence ? [' ', sentence] : null, risk ? [' ', risk] : null,
    ' · ', element('span', { title: when(run.started_at) }, `started ${when(run.started_at)}`),
    run.head ? [' · ', element('span', { class: 'mono' }, run.head)] : null, ' · ', link(runHref(run), 'Open the full run'),
    run.artifacts?.length ? [' · ', run.artifacts.map((name, index) => [index ? ', ' : '', link(runHref(run, name), `Read ${name}`)])] : null);
}

async function finishedResult(body, run, signal) {
  const view = resultView({ level: 3 });
  const fallback = element('p', { class: 'run-outcome' }, outcomeCapsule(run), ' ', element('span', { class: 'wrap-anywhere' }, moreThanOutcome(run)));
  body.append(runLine(run), view.node);
  const activity = new RunActivity();
  try {
    await backfill(activity, (after) => api(eventsPath(run.id, after)), { signal });
  } catch {}
  if (!signal.aborted && !view.draw(activity)) body.append(fallback);
}

// While a run is in progress its live log leads the page. When it ends, the page draws again with its result.
function nowSection(detail, display, signal, redraw) {
  const run = currentRun(detail.runs);
  if (!run) return null;
  const live = ACTIVE.has(run.status);
  const body = element('div', {});
  const section = element('section', { class: 'thread-now card', 'aria-labelledby': 'thread-now-heading' },
    element('h2', { id: 'thread-now-heading', tabindex: -1 }, live ? 'Running now' : 'Latest result'), body);
  if (live) {
    const tail = openTail({ runId: run.id, run, display, signal, drawnLive: true, label: runLabel(run), color: 'other', title: '',
      subject: null, onEnd: () => redraw() });
    body.append(tail.node);
  } else {
    finishedResult(body, run, signal);
  }
  return section;
}

// The newest review when the thread's latest run is something else, such as a merge, so its findings stay in view.
export const latestReview = (runs, current) => (current && current.kind !== 'review'
  ? [...runs].reverse().find((run) => run.kind === 'review' && run !== current) || null : null);

function reviewSection(detail, current, signal) {
  const review = latestReview(detail.runs, current);
  if (!review) return null;
  const body = element('div', {});
  finishedResult(body, review, signal);
  return element('section', { class: 'thread-review card', 'aria-labelledby': 'thread-review-heading' },
    element('h2', { id: 'thread-review-heading' }, 'Latest review'), body);
}

// Everything under the header, top to bottom. Absent sections are left out, never drawn as "null".
export function threadParts(detail, display, signal, redraw) {
  const current = currentRun(detail.runs);
  const latest = detail.runs.at(-1);
  return [element('p', { class: 'crumb' }, link(hashFor('subjects'), 'Pull requests')),
    pageHeader(detail.title || detail.subject, ...headerChips(detail, current)),
    linkRow(detail.links || []),
    element('div', { class: 'meta' },
      element('span', { class: 'mono wrap-anywhere' }, detail.subject),
      detail.repo ? element('span', {}, detail.repo) : null,
      latest ? element('span', {}, `Last activity ${when(latest.started_at)}`) : null,
      link(hashFor('runs', [], { thread: detail.subject, every: '1' }), detail.runs.length === 1 ? '1 run' : `${detail.runs.length} runs`)),
    nowSection(detail, display, signal, redraw) || empty('No runs.'), reviewSection(detail, current, signal),
    earlierRuns(detail, current), findingHistory(detail)].filter(Boolean);
}

function earlierRuns(detail, current) {
  const ordered = detail.runs;
  const earlier = ordered.filter((run) => run !== current).reverse();
  if (!earlier.length) return null;
  const later = (run) => ordered[ordered.indexOf(run) + 1];
  return element('details', { class: 'card earlier-runs' }, element('summary', {}, `Earlier runs (${earlier.length})`),
    element('p', { class: 'note' }, 'Each later run replaces what the one before it found, so these are usually out of date.'),
    element('ul', { class: 'earlier-list', role: 'list' }, earlier.map((run) => element('li', {},
      element('div', { class: 'earlier-head' }, link(runHref(run), when(run.started_at)), element('span', {}, runLabel(run)),
        outcomeCapsule(run), riskChip(run.risk, run.risk_reason), run.mode ? element('span', { class: 'chip' }, run.mode) : null,
        run.subject && run.subject !== detail.subject ? element('span', { class: 'muted mono wrap-anywhere' }, run.subject) : null),
      element('p', { class: 'note earlier-what wrap-anywhere' }, [moreThanOutcome(run),
        later(run) ? `Superseded by the run started ${when(later(run).started_at)}.` : null].filter(Boolean).join(' · '))))));
}

function findingHistory(detail) {
  const findings = detail.findings || [];
  if (!findings.length) return null;
  return element('details', { class: 'card' }, element('summary', {}, `Finding history (${findings.length})`),
    table(['Finding', 'Now', 'History'], findings.map((finding) => [
      element('div', { class: 'wrap-anywhere' }, finding.title || finding.finding_key,
        finding.path ? element('div', { class: 'note mono wrap-anywhere' }, finding.line ? `${finding.path}:${finding.line}` : finding.path) : null),
      dispositionChip(finding.current_disposition),
      element('span', { class: 'chips' }, finding.history.map((event) => link(hashFor('run', [event.run_id]),
        dispositionChip(event.disposition, `${dispositionLabel(event.disposition)} ${when(event.started_at)}`)))),
    ])),
    element('p', { class: 'note' }, 'One row per finding across every review, newest first. History shows how each review judged it.'));
}

// When the page may draw itself again: at once the first time, then backing off, and not at all after
// REDRAW_LIMIT redraws in a row, so a run the database and the live stream disagree about can't loop.
export function nextRedraw(previous, now) {
  const count = now - previous.last > REDRAW_RESET_MS ? 0 : previous.count;
  if (count >= REDRAW_LIMIT) return null;
  return { count: count + 1, last: now, delay: count ? Math.min(REDRAW_MAX_MS, REDRAW_BASE_MS * 2 ** count) : 0 };
}

// A started run belongs on this page when its subject or its thread is the page's subject.
export const joinsPage = (started, subject) => started.subject === subject || started.thread === subject;

// The page is built apart and swapped in whole, so a redraw keeps the scroll position and never shows a blank page.
async function detailPage(root, subject, signal, redraws = { count: 0, last: 0 }) {
  const lifetime = new AbortController();
  signal?.addEventListener('abort', () => lifetime.abort());
  // The chat outlives redraws, so a message being written survives a run starting.
  const conversationId = webConversation(subject);
  const chat = redraws.chat || (conversationId ? chatPanel(conversationId, signal) : null);
  let detail;
  let display;
  try {
    [detail, display] = await Promise.all([api(subjectPath(subject)).then((payload) => payload?.subject), loadDisplay()]);
  } catch (err) {
    root.replaceChildren(element('p', { class: 'crumb' }, link(hashFor('subjects'), 'Pull requests')), pageHeader(subject), chat || errorBox(err));
    return;
  }
  if (lifetime.signal.aborted) return;
  let redrawn = false;
  const redraw = () => {
    if (redrawn || lifetime.signal.aborted) return;
    const next = nextRedraw(redraws, Date.now());
    if (!next) return;
    redrawn = true;
    setTimeout(() => {
      if (signal?.aborted) return;
      lifetime.abort();
      detailPage(root, subject, signal, { ...next, chat });
    }, next.delay);
  };
  const parts = threadParts(detail, display, lifetime.signal, redraw);
  if (chat) parts.splice(2, 0, chat);
  const fresh = append(document.createDocumentFragment(), parts);
  const hadFocus = redraws.count > 0 && root.contains(document.activeElement);
  const scrolled = window.scrollY;
  root.replaceChildren(fresh);
  if (redraws.count) window.scrollTo(0, scrolled);
  if (hadFocus) root.querySelector('#thread-now-heading')?.focus({ preventScroll: true });
  // A run that starts on this thread while the page is open takes the lead.
  const known = new Set(detail.runs.map((run) => String(run.id)));
  subscribe('/api/stream/runs', {
    events: { run: (started) => {
      if (joinsPage(started, detail.subject) && !known.has(String(started.id))) redraw();
    } },
    onFallback: () => every(POLL_MS, async () => {
      const polled = (await api(subjectPath(subject)))?.subject;
      if (polled?.runs?.some((run) => !known.has(String(run.id)))) redraw();
      return !lifetime.signal.aborted;
    }, lifetime.signal),
  }, { signal: lifetime.signal });
}

async function listPage(root, query) {
  const q = query.get('q') || '';
  const team = query.get('team') || '';
  root.append(pageHeader('Pull requests'));
  const search = element('input', { type: 'search', name: 'q', placeholder: 'Pull request, issue, or title', 'aria-label': 'Search pull requests and issues' });
  search.value = q;
  const form = element('form', { class: 'filters', role: 'search', 'aria-label': 'Search pull requests' }, search, element('button', { type: 'submit' }, 'Search'),
    q ? link(hashFor('subjects', [], { team }), 'Clear') : null);
  form.addEventListener('submit', (submitEvent) => {
    submitEvent.preventDefault();
    location.hash = hashFor('subjects', [], { q: search.value.trim(), team });
  });
  const body = element('div', {}, loading());
  root.append(form, body);
  const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
  if (q) params.set('q', q);
  if (team) params.set('team', team);
  if (query.get('before')) params.set('before', query.get('before'));
  let page;
  let display;
  try {
    [page, display] = await Promise.all([api(`/api/subjects?${params}`), loadDisplay()]);
  } catch (err) {
    clear(body).append(errorBox(err));
    return;
  }
  const rows = listOf(page, 'subjects');
  const sum = totals(rows);
  clear(body).append(card(rows.length ? table(['Title', 'Status', 'Work', ...(display.show_cost ? [['Cost', 'num']] : []), ['Last activity', 'nowrap']], rows.map((row) => [
    element('div', { class: 'wrap-anywhere' }, link(subjectPageHref(row.subject), row.title || row.subject),
      row.title ? element('div', { class: 'muted mono wrap-anywhere' }, row.subject) : null),
    statusCapsule(row.status), workText(row), ...(display.show_cost ? [money(row.cost_usd)] : []), when(row.last_activity),
  ])) : empty(q ? 'Nothing matches that search.' : 'No run names a pull request or issue yet.'),
  rows.length ? element('p', { class: 'note' }, [plural(sum.subjects, 'subject'), plural(sum.reviews, 'review'), plural(sum.fixes, 'fix', 'fixes'),
    !display.show_cost || sum.cost === null ? null : `${money(sum.cost)} on this page`].filter(Boolean).join(', ')) : null));
  const pager = element('nav', { class: 'pager', 'aria-label': 'Pages' });
  if (query.get('before')) pager.append(link(hashFor('subjects', [], { q, team }), 'Newest'));
  if (page.next_before) pager.append(link(hashFor('subjects', [], { q, team, before: page.next_before }), 'Older'));
  if (pager.firstChild) body.append(pager);
}

export async function render(root, { name, args, query, signal }) {
  if (name === 'subject' && args[0]) return detailPage(root, args[0], signal);
  return listPage(root, query);
}
