import { api, listOf } from '../api.js';
import { card, clear, element, empty, errorBox, link, loading, money, pageHeader, statusChip, statusText, statusTone, table, tabs, when } from '../dom.js';
import { loadDisplay } from '../display.js';
import { hashFor } from '../router.js';
import { summaryLinks, tabIdFor } from '../runpage.js';
import { verdictChip, verdictTone } from './runs.js';

const PAGE_SIZE = 50;
const TABS = [{ id: 'runs', label: 'Runs' }, { id: 'findings', label: 'Findings' }, { id: 'activity', label: 'Activity' }];
const RUN_STATUSES = new Set(['queued', 'running', 'succeeded', 'failed', 'canceled', 'lost']);
const ACTIVITY_KINDS = new Set(['fix', 'merge', 'port', 'work']);
const DISPOSITIONS = {
  keep: 'Kept', resolved: 'Resolved', drop_rebutted: 'Rebutted', act: 'Act on', defer: 'Deferred', block: 'Blocking',
  suggest: 'Suggested', change: 'Change', conflict: 'Conflict',
};
const DISPOSITION_TONE = { resolved: 'ok', drop_rebutted: 'ok', block: 'bad', keep: 'warn', act: 'warn', change: 'warn' };

// Apache refuses %2F in a path, so the slashes stay literal for the {subject:path} route.
export const subjectPath = (subject) => `/api/subjects/${String(subject).split('/').map(encodeURIComponent).join('/')}`;

export const subjectPageHref = (subject, tab) => hashFor('subject', tab ? [subject, tab] : [subject]);

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

export const activityRuns = (runs) => runs.filter((run) => ACTIVITY_KINDS.has(run.kind));

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

function linkRow(links) {
  return element('div', { class: 'run-links' }, summaryLinks({ links }).map(({ label, url }) => {
    const anchor = element('a', { rel: 'noopener noreferrer', target: url.startsWith('/') ? null : '_blank' }, label);
    // summaryLinks keeps only https and same-origin paths, which dom.js would otherwise drop.
    anchor.setAttribute('href', url);
    return anchor;
  }));
}

function runsTab(panel, detail, showCost) {
  const runs = [...detail.runs].reverse();
  const cost = runs.reduce((sum, run) => (run.cost_usd === null || run.cost_usd === undefined ? sum : (sum ?? 0) + Number(run.cost_usd)), null);
  panel.append(card(runs.length ? table(['Started', 'Run', 'Status', 'Head', ...(showCost ? [['Cost', 'num']] : []), 'Report'], runs.map((run) => [
    link(runHref(run), when(run.started_at)),
    element('span', {}, run.workflow_label || run.workflow || '—', ' ', verdictChip(run.verdict), run.mode ? element('span', { class: 'chip' }, run.mode) : null),
    statusChip(run.status),
    run.head ? element('span', { class: 'mono' }, run.head) : '—',
    ...(showCost ? [money(run.cost_usd)] : []),
    run.artifacts?.length ? element('span', { class: 'chips' }, run.artifacts.map((name) => link(runHref(run, name), name))) : '—',
  ])) : empty('No runs.'),
  element('p', { class: 'note' }, [plural(runs.length, 'run'), !showCost || cost === null ? null : `${money(cost)} in total`].filter(Boolean).join(', '),
    '. A pull request is reviewed again on every push, so several reviews is normal.')));
}

function findingsTab(panel, detail) {
  const findings = detail.findings || [];
  panel.append(card(findings.length ? table(['Finding', 'Now', 'History'], findings.map((finding) => [
    element('div', { class: 'wrap-anywhere' }, finding.title || finding.finding_key,
      finding.path ? element('div', { class: 'note mono wrap-anywhere' }, finding.line ? `${finding.path}:${finding.line}` : finding.path) : null),
    dispositionChip(finding.current_disposition),
    element('span', { class: 'chips' }, finding.history.map((event) => link(hashFor('run', [event.run_id]),
      dispositionChip(event.disposition, `${dispositionLabel(event.disposition)} ${when(event.started_at)}`)))),
  ])) : empty('No review recorded a finding for this subject.'),
  element('p', { class: 'note' }, 'One row per finding across every review, newest first. History shows how each review judged it.')));
}

function activityTab(panel, detail) {
  const runs = activityRuns(detail.runs).reverse();
  panel.append(card(runs.length ? table(['When', 'What', 'Outcome', 'Status'], runs.map((run) => [
    link(runHref(run), when(run.started_at)), run.workflow_label || run.workflow || '—',
    element('span', { class: 'wrap-anywhere' }, outcomeText(run)), statusChip(run.status),
  ])) : empty('No fix, merge, or port ran for this subject.')));
}

async function detailPage(root, subject, requestedTab) {
  const active = TABS.some((tab) => tab.id === requestedTab) ? requestedTab : 'runs';
  let detail;
  let display;
  try {
    [detail, display] = await Promise.all([api(subjectPath(subject)).then((payload) => payload?.subject), loadDisplay()]);
  } catch (err) {
    root.append(element('p', { class: 'crumb' }, link(hashFor('subjects'), 'Pull requests')), pageHeader(subject), errorBox(err));
    return;
  }
  const latest = detail.runs[detail.runs.length - 1];
  root.append(element('p', { class: 'crumb' }, link(hashFor('subjects'), 'Pull requests')),
    pageHeader(detail.title || detail.subject, statusCapsule(detail.status), detail.status !== detail.latest_verdict ? verdictChip(detail.latest_verdict) : null),
    element('div', { class: 'meta' },
      element('span', { class: 'mono wrap-anywhere' }, detail.subject),
      detail.repo ? element('span', {}, detail.repo) : null,
      latest ? element('span', {}, `Last activity ${when(latest.started_at)}`) : null,
      link(hashFor('runs', [], { subject: detail.subject }), 'All runs')),
    linkRow(detail.links || []));
  const { tablist, panel } = tabs(subjectPageHref(subject), TABS, active, { label: 'Subject sections' });
  root.append(tablist, panel);
  if (active === 'findings') findingsTab(panel, detail);
  else if (active === 'activity') activityTab(panel, detail);
  else runsTab(panel, detail, display.show_cost);
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

export async function render(root, { name, args, query }) {
  if (name === 'subject' && args[0]) return detailPage(root, args[0], args[1]);
  return listPage(root, query);
}
