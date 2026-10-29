// Where a run leads out to, and what its team lead decided, in plain words. Only `externalLink` touches the DOM.
import { isBlocking, roleOf, severityRank } from './activity.js';
import { element } from './dom.js';
import { summaryLinks } from './runpage.js';

const GITHUB_PULL = /^https:\/\/github\.com\/[^/]+\/[^/]+\/pull\/([1-9][0-9]{0,9})\/?$/;
const JIRA_ISSUE = /\/browse\/([A-Z][A-Z0-9_]+-[1-9][0-9]*)\/?$/;
const LINEAR_ISSUE = /^https:\/\/linear\.app\/[^/]+\/issue\/([A-Z][A-Z0-9]*-[1-9][0-9]*)/;
const KIND_ORDER = ['pull', 'issue', 'slack', 'other'];
const DROPPED = new Set(['drop', 'drop_rebutted', 'resolved']);
const DROP_REASONS = { drop_rebutted: 'Rebutted in the discussion.', resolved: 'Already resolved.', drop: 'Dropped.' };
const SENTENCES = { pr_opened: 'Pull request opened.', pushed: 'Fixes pushed to the pull request.', recommended: 'Changes recommended.',
  approved: 'Approved.', blocked: 'Blocked. Nothing was pushed.', noop: 'Nothing to do.' };
const RISK_TONE = { low: 'ok', medium: 'warn', high: 'bad' };

// Each link's kind, its label in full, and the few characters a table cell has room for.
function kindOf(label, url) {
  const pull = GITHUB_PULL.exec(url);
  if (pull && /pull request/i.test(label)) return { kind: 'pull', label: `${label} #${pull[1]}`, short: `#${pull[1]}` };
  const issue = JIRA_ISSUE.exec(url) || LINEAR_ISSUE.exec(url);
  if (issue) return { kind: 'issue', label: `${JIRA_ISSUE.test(url) ? 'Jira' : 'Linear'} ${issue[1]}`, short: issue[1] };
  if (/^issue$/i.test(label)) return { kind: 'issue', label, short: label };
  if (/^https:\/\/([a-z0-9-]+\.)*slack\.com\//i.test(url)) return { kind: 'slack', label: /slack/i.test(label) ? label : 'Slack thread', short: 'Slack' };
  return { kind: 'other', label, short: label };
}

// The pull request, issue, and Slack thread first, then anything else the runs reported.
export function externalLinks({ links = [] } = {}) {
  return summaryLinks({ links }).map(({ label, url }, index) => ({ ...kindOf(label, url), url, index }))
    .sort((left, right) => KIND_ORDER.indexOf(left.kind) - KIND_ORDER.indexOf(right.kind) || left.index - right.index)
    .map(({ index, ...item }) => item);
}

export function externalLink({ label, url }) {
  const outside = !url.startsWith('/');
  const anchor = element('a', { class: 'out-link', rel: outside ? 'noopener noreferrer' : null, target: outside ? '_blank' : null }, label,
    outside ? element('span', { class: 'visually-hidden' }, ' (opens in a new tab)') : null);
  // summaryLinks keeps only https and same-origin paths, which dom.js would otherwise drop.
  anchor.setAttribute('href', url);
  return anchor;
}

export const isDropped = (finding) => DROPPED.has(String(finding?.disposition || '')) || finding?.decision === 'drop';

export const dropReason = (finding) => (typeof finding?.reason === 'string' && finding.reason) || DROP_REASONS[finding?.disposition] || DROP_REASONS.drop;

const bySeverity = (left, right) => severityRank(left) - severityRank(right) || (left.seq ?? 0) - (right.seq ?? 0);

// The specialists who raised a finding, once each, in the order the lead listed them.
export const raisedBy = (finding) => (Array.isArray(finding?.raised_by)
  ? [...new Set(finding.raised_by.filter((role) => typeof role === 'string' && role.trim()).map((role) => role.trim()))] : []);

// The lead's findings, sorted into what blocks, what should be fixed, what was already fixed, and what it threw out.
export function decisionGroups(findings) {
  const groups = { blocking: [], shouldFix: [], fixed: [], dropped: [] };
  for (const finding of [...findings].sort(bySeverity)) {
    if (finding?.decision === 'fixed') groups.fixed.push(finding);
    else if (isDropped(finding)) groups.dropped.push(finding);
    else if (finding?.decision === 'block') groups.blocking.push(finding);
    else if (finding?.decision === 'suggest') groups.shouldFix.push(finding);
    else if (isBlocking(finding)) groups.blocking.push(finding);
    else groups.shouldFix.push(finding);
  }
  return groups;
}

// "Changes Recommended: 2 blocking, 3 should fix", or just the verdict when nothing is left to fix.
export function decisionHeadline(verdictLabel, { blocking, shouldFix }) {
  const counts = [blocking ? `${blocking} blocking` : null, shouldFix ? `${shouldFix} should fix` : null].filter(Boolean).join(', ');
  return counts ? `${verdictLabel}: ${counts}` : verdictLabel;
}

// The run's outcome in one sentence, with the lead's counts when it recommended changes. Empty for no outcome.
export function outcomeSentence(outcome, counts = null) {
  if (outcome === 'recommended' && counts) return `${decisionHeadline('Changes recommended', counts)}.`;
  return Object.hasOwn(SENTENCES, outcome ?? '') ? SENTENCES[outcome] : '';
}

// The risk chip's words, tone, and the first reason as its tooltip. Null when the server set no risk.
export function riskBadge(risk, reason = null) {
  if (!Object.hasOwn(RISK_TONE, risk ?? '')) return null;
  return { text: `${risk.charAt(0).toUpperCase()}${risk.slice(1)} risk`, tone: RISK_TONE[risk], title: reason || null };
}

// A thread's risk is its newest run's, so a run with none says unknown rather than looking low.
export const threadRiskBadge = (risk) => riskBadge(risk) || { text: 'Risk unknown', tone: '', title: null };

// What the decision says once the lead's findings are sorted. The lead's own counts stand in when it sent no findings.
export function decisionSummary(activity) {
  const decision = activity?.decision;
  if (!decision) return null;
  const lead = activity.findings.filter((finding) => !decision.role || roleOf(finding) === decision.role);
  const groups = decisionGroups(lead);
  const counts = lead.length || decision.findings === null
    ? { blocking: groups.blocking.length, shouldFix: groups.shouldFix.length }
    : { blocking: decision.blocking ?? 0, shouldFix: Math.max(0, decision.findings - (decision.blocking ?? 0)) };
  return { verdict: decision.verdict, role: decision.role, groups, counts };
}

// Everyone but the lead who reported back, in the order they started, with what each found.
export function specialists(activity) {
  const lead = activity?.decision?.role ?? null;
  return [...(activity?.roles?.values() || [])].filter((entry) => entry.role !== lead).map((entry) => ({
    role: entry.role, state: entry.state, count: entry.findings, review: entry.review, error: entry.error,
    started_at: entry.started_at, finished_at: entry.finished_at, step_key: entry.step_key,
    findings: activity.findings.filter((finding) => roleOf(finding) === entry.role).sort(bySeverity),
  }));
}
