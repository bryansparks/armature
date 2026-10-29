// A run's result as people read it: the lead's decision sorted into what blocks, what to fix, and what it
// dropped, then what each specialist found. The run page and the thread page both draw it.
import { capitalize, findingBadge, findingWhere, isBlocking, verdictTone } from './activity.js';
import { duration, element } from './dom.js';
import { elapsedSeconds } from './feed.js';
import { renderMarkdown } from './markdown.js';
import { decisionHeadline, decisionSummary, dropReason, raisedBy, specialists } from './outcome.js';
import { verdictText } from './views/runs.js';

const TONE_ICON = { ok: '✓', warn: '!', bad: '✕' };
const FEW_FINDINGS = 3;

const findingKey = (finding) => [finding.role, finding.path, finding.line, finding.claim || finding.title].join('|');

const suggestionOf = (finding) => [finding.suggestion, finding.fix].find((value) => typeof value === 'string' && value.trim()) || '';

// A finding with its badge in words, where it is, the claim, the explanation, and any suggested change.
export function findingCard(finding, { reason = null } = {}) {
  const blocking = isBlocking(finding);
  const where = findingWhere(finding);
  const body = finding.body ? String(finding.body) : '';
  const suggestion = suggestionOf(finding);
  const raisers = raisedBy(finding);
  return element('article', { class: `finding-card${blocking && !reason ? ' blocking' : ''}` },
    element('div', { class: 'finding-head' },
      element('span', { class: `chip ${reason ? '' : blocking ? 'bad' : finding.severity === 'high' || finding.severity === 'critical' ? 'warn' : ''}`.trim() }, findingBadge(finding)),
      where ? element('code', { class: 'finding-where wrap-anywhere' }, where) : null,
      raisers.length ? element('span', { class: 'finding-by' }, element('span', { class: 'visually-hidden' }, 'Raised by '),
        raisers.map((role) => element('span', { class: 'chip', title: `Raised by ${role}` }, capitalize(role)))) : null),
    element('p', { class: 'finding-claim' }, finding.claim || finding.title || 'Finding'),
    reason ? element('p', { class: 'finding-reason' }, element('span', { class: 'meta-label' }, 'Why dropped: '), reason) : null,
    body && body !== (finding.claim || finding.title) ? element('div', { class: 'finding-text' }, renderMarkdown(body)) : null,
    suggestion ? element('div', { class: 'finding-suggestion' }, element('div', { class: 'meta-label' }, 'Suggested change'), renderMarkdown(suggestion)) : null);
}

function group(title, findings, { open = true, dropped = false, heading }) {
  if (!findings.length) return null;
  const cards = element('div', { class: 'finding-cards' }, findings.map((finding) => findingCard(finding, { reason: dropped ? dropReason(finding) : null })));
  if (open) return element('section', { class: 'decision-group' }, element(heading, { class: 'group-title' }, `${title} (${findings.length})`), cards);
  return element('details', { class: 'decision-group' }, element('summary', { class: 'group-title' }, `${title} (${findings.length})`), cards);
}

function specialistRow(entry, now, open) {
  const seconds = elapsedSeconds(entry.started_at, entry.finished_at, now);
  const found = entry.state === 'failed' ? 'failed'
    : entry.state === 'working' ? 'still working'
      : entry.count === null || entry.count === undefined ? (entry.findings.length ? `${entry.findings.length} findings` : 'finished')
        : entry.count === 0 ? 'no findings' : entry.count === 1 ? '1 finding' : `${entry.count} findings`;
  const details = element('details', { class: `specialist specialist-${entry.state}`, open },
    element('summary', {}, element('span', { class: 'specialist-role' }, capitalize(entry.role)), ' ',
      element('span', { class: entry.state === 'failed' ? 'bad' : 'muted' }, [found, seconds && entry.state !== 'working' ? duration(seconds) : null].filter(Boolean).join(' · '))));
  const fill = () => {
    if (!details.open || details.childElementCount > 1) return;
    details.append(element('div', { class: 'specialist-body' },
      entry.error ? element('p', { class: 'bad' }, entry.error) : null,
      entry.findings.length ? element('div', { class: 'finding-cards' }, entry.findings.map((finding) => findingCard(finding))) : null,
      entry.review ? renderMarkdown(entry.review) : null,
      !entry.error && !entry.findings.length && !entry.review ? element('p', { class: 'muted' }, 'Nothing reported.') : null));
  };
  details.addEventListener('toggle', fill);
  fill();
  return element('li', {}, details);
}

const foundBy = (entry) => entry.count ?? entry.findings.length;

// With a few findings in all, the specialists who found them start open, so they read without a click.
export const startsOpen = (entry, people) => foundBy(entry) > 0 && people.reduce((sum, other) => sum + foundBy(other), 0) <= FEW_FINDINGS;

// Draws once per change in what the run reported. ``level`` is the heading level of the verdict.
export function resultView({ level = 2 } = {}) {
  const node = element('div', { class: 'result', hidden: true });
  const heading = `h${level}`;
  const subheading = `h${Math.min(level + 1, 6)}`;
  let drawn = null;
  const draw = (activity, { now = Date.now() } = {}) => {
    const summary = decisionSummary(activity);
    const people = specialists(activity);
    const key = `${activity.lastSeq}|${Boolean(summary)}`;
    if (key === drawn) return Boolean(summary);
    if (node.contains(document.activeElement) && drawn !== null) return Boolean(summary);
    drawn = key;
    node.hidden = !summary;
    if (!summary) return false;
    const tone = verdictTone(summary.verdict);
    const { groups } = summary;
    node.replaceChildren(
      element('div', { class: `decision tone-${tone}` },
        element(heading, { class: 'decision-verdict' }, element('span', { 'aria-hidden': 'true' }, TONE_ICON[tone]), ' ',
          decisionHeadline(verdictText(summary.verdict), summary.counts)),
        element('p', { class: 'decision-counts' }, [summary.role ? `Decided by the ${summary.role}` : 'Decided', people.length ? `from ${people.length} specialists' reviews` : null].filter(Boolean).join(' ')),
        group('Blocking', groups.blocking, { heading: subheading }),
        group('Should fix', groups.shouldFix, { heading: subheading }),
        group('Fixed', groups.fixed, { open: false, heading: subheading }),
        group('Dropped', groups.dropped, { open: false, dropped: true, heading: subheading }),
        !groups.blocking.length && !groups.shouldFix.length && !groups.fixed.length && !groups.dropped.length ? element('p', { class: 'muted' }, 'Nothing to fix.') : null),
      people.length ? element('section', { class: 'specialists' }, element(subheading, {}, 'Specialists'),
        element('ul', { class: 'specialist-list', role: 'list' }, people.map((entry) => specialistRow(entry, now, startsOpen(entry, people))))) : null,
      rawToggle(activity));
    return true;
  };
  return { node, draw };
}

function rawToggle(activity) {
  const details = element('details', { class: 'raw-data' }, element('summary', {}, 'Raw data'));
  details.addEventListener('toggle', () => {
    if (details.open && details.childElementCount === 1) {
      details.append(element('pre', { class: 'output-text' }, JSON.stringify({ decision: activity.decision, findings: activity.findings }, null, 2)));
    }
  });
  return details;
}
