// Pure formatting for Insights. No DOM, so the node tests load it directly.

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

export const isNum = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));

export function fixed(value, digits = 0) {
  if (!isNum(value)) return '—';
  const rounded = Number(Number(value).toFixed(digits));
  return rounded.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function plural(count, one, many = `${one}s`) {
  return `${fixed(count)} ${Number(count) === 1 ? one : many}`;
}

export const cap = (text) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : '');

// How each measure reads on screen. better says which direction is good, by how the number moves.
export const MEASURES = {
  merged: {
    label: 'Merged pull requests', unit: 'count', digits: 0, better: 'up', change: 'percent', basis: 'Count over the previous 7 days',
    def: 'Pull requests merged into the default branch, counted on the day they merged.',
  },
  time_to_merge: {
    label: 'Median time to merge', unit: 'days', digits: 1, better: 'down', change: 'absolute', basis: 'Median over the previous 7 days, in days',
    def: 'Half of merged pull requests took less than this from opening to merge.',
  },
  review_rounds: {
    label: 'Review rounds per pull request', unit: 'rounds', digits: 1, better: 'down', change: 'absolute', basis: 'Average over the previous 7 days',
    def: 'How many times a pull request was reviewed and changed before approval. Fewer is better.',
  },
  conflict_hours: {
    label: 'Hours blocked by merge conflicts', unit: 'hours', digits: 0, better: 'down', change: 'percent', basis: 'Hours over the previous 7 days',
    def: 'Time open pull requests could not merge because of a conflict with the base branch. Counts against the codebase, not the author.',
  },
  time_to_first_review: {
    label: 'Time to first review', unit: 'hours', digits: 1, better: 'down', change: 'absolute', basis: 'Median over the previous 7 days, in hours',
    def: 'Half of pull requests opened in the period got their first human review sooner than this.',
  },
  ci_failure_rate: {
    label: 'CI failure rate', unit: 'share', digits: 0, better: 'down', change: 'points', basis: 'Share over the previous 7 days',
    def: 'Share of finished checks on the final commit of merged pull requests that failed or timed out.',
  },
  spend: {
    label: 'Review spend', unit: 'usd', digits: 2, better: 'down', change: 'percent',
    def: 'What model calls for reviews cost in the period, not counting cached calls.',
  },
  comparison_spend: {
    label: 'Spent on comparison runs', unit: 'usd', digits: 2, better: 'down', change: 'percent',
    def: 'What extra reviews run only to compare models or effort levels cost in the period.',
  },
  cost_per_pr: {
    label: 'Cost per reviewed pull request', unit: 'usd', digits: 2, better: 'down', change: 'percent',
    def: 'Review spend divided by the number of pull requests those reviews covered.',
  },
};

// Short names keep shared URLs readable: #/insights?metric=rounds.
export const METRIC_KEYS = { merged: 'merged', time_to_merge: 'merge-time', review_rounds: 'rounds', conflict_hours: 'conflicts', time_to_first_review: 'first-review', ci_failure_rate: 'ci' };
export const metricFromKey = (key, allowed) => allowed.find((id) => METRIC_KEYS[id] === key || id === key) || null;

const UNIT_WORDS = { days: ['day', 'days'], hours: ['hour', 'hours'] };

// A figure as its number and its unit, so the unit can sit smaller after the number.
export function figure(value, unit, digits = 0) {
  if (!isNum(value)) return { value: '—', unit: '' };
  if (unit === 'share') return { value: `${fixed(Number(value) * 100, digits)}%`, unit: '' };
  if (unit === 'usd') return { value: `$${fixed(value, digits)}`, unit: '' };
  const words = UNIT_WORDS[unit];
  const text = fixed(value, digits);
  return { value: text, unit: words ? words[text === '1' ? 0 : 1] : '' };
}

export function figureText(value, unit, digits = 0) {
  const { value: number, unit: words } = figure(value, unit, digits);
  return words ? `${number} ${words}` : number;
}

// Compact text for a cell or an axis: "4.0 h", "1.6 d", "12%", "$1.20".
export function short(value, unit, digits = 1) {
  if (!isNum(value)) return '—';
  if (unit === 'share') return `${fixed(Number(value) * 100, 0)}%`;
  if (unit === 'usd') return `$${fixed(value, 2)}`;
  if (unit === 'hours') return `${fixed(value, digits)} h`;
  if (unit === 'days') return `${fixed(value, digits)} d`;
  return fixed(value, digits);
}

// The change from the previous period: direction of the number, whether that is good, and the words for it.
// period names what previous covers when it isn't the previous `days` days, such as "the day before".
export function delta(value, previous, measure, days, period = `the previous ${days} days`) {
  if (!isNum(value) || !isNum(previous) || !measure) return null;
  const now = Number(value);
  const before = Number(previous);
  let amount;
  let spoken;
  if (measure.change === 'percent' && before >= 10) {
    const pct = Math.round((Math.abs(now - before) / before) * 100);
    amount = pct ? `${pct}%` : '';
    spoken = `${pct} percent`;
  } else if (measure.change === 'points') {
    const points = Math.round(Math.abs(now - before) * 100);
    amount = points ? plural(points, 'point') : '';
    spoken = amount;
  } else {
    const digits = measure.change === 'percent' ? measure.digits : Math.max(1, measure.digits);
    const diff = Math.abs(now - before);
    amount = Number(Number(diff).toFixed(digits)) ? figureText(diff, measure.unit, digits) : '';
    spoken = amount;
  }
  if (!amount) return { dir: 'flat', good: null, text: 'No change', sr: `No change compared with ${period}.` };
  const dir = now > before ? 'up' : 'down';
  const good = dir === measure.better;
  return { dir, good, text: amount, sr: `${cap(dir)} ${spoken} compared with ${period}, which is ${good ? 'better' : 'worse'}.` };
}

export function monthDay(iso, long = true) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const month = MONTHS[date.getUTCMonth()];
  return `${long ? month : month.slice(0, 3)} ${date.getUTCDate()}`;
}

export function dateRange(startIso, endIso) {
  const start = new Date(startIso);
  const end = new Date(endIso);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return '';
  const sameYear = start.getUTCFullYear() === end.getUTCFullYear();
  const from = `${monthDay(startIso)}${sameYear ? '' : `, ${start.getUTCFullYear()}`}`;
  const to = `${monthDay(endIso)}${sameYear ? '' : `, ${end.getUTCFullYear()}`}`;
  return `${from} to ${to}`;
}

export function daysAgo(iso, nowMs = Date.now()) {
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return '';
  const days = Math.floor((nowMs - at) / 86400000);
  if (days <= 0) return 'Today';
  return days === 1 ? '1 day' : `${days} days`;
}

export function initials(name) {
  const parts = String(name || '?').trim().split(/\s+/).filter(Boolean);
  const letters = parts.length > 1 ? [parts[0], parts[parts.length - 1]] : parts;
  return letters.map((part) => Array.from(part)[0] || '').join('').toUpperCase() || '?';
}

export const firstName = (name) => String(name || '').trim().split(/\s+/)[0] || String(name || '');

export const repoShort = (fullName) => String(fullName || '').split('/').pop();

export function possessive(name) {
  return /s$/i.test(name) ? `${name}’` : `${name}’s`;
}

// Categories as a manager would say them, with the one-line definition for hover.
export const CATEGORIES = {
  correctness: ['Correctness', 'Wrong results, wrong conditions, off-by-one errors, broken logic.'],
  'crash-safety': ['Crash safety', 'Force unwraps, null dereferences, unchecked casts, index out of range.'],
  concurrency: ['Concurrency', 'Races, work on the wrong thread, unawaited work, ordering, cancellation.'],
  'lifecycle-memory': ['Lifecycle and memory', 'Retain cycles, leaks, work outliving its owner, observers left behind.'],
  'error-handling': ['Error handling', 'Swallowed errors, missing failure paths, bad fallbacks, silent failure.'],
  'security-privacy': ['Security and privacy', 'Secrets, personal data, sign-in and permissions, injection, logging sensitive data.'],
  'data-contract': ['Data contracts', 'API request and response shapes, storage, migrations, serialization.'],
  performance: ['Performance', 'Repeated work, heavy work on the main thread, slow queries, render cost, startup.'],
  'ui-layout': ['Layout and screen states', 'Layout, loading, empty, and error states, design system misuse.'],
  accessibility: ['Accessibility', 'Labels, traits, text size, contrast, focus order.'],
  localization: ['Localization', 'Hard-coded strings, plurals, formatting, right-to-left layout.'],
  testing: ['Tests', 'Missing or weak tests, tests that can’t fail, flaky tests.'],
  'api-misuse': ['API misuse', 'Wrong use of a platform or library API, deprecated APIs.'],
  compatibility: ['Compatibility', 'OS version availability, device differences, backward compatibility.'],
  simplicity: ['Unneeded complexity', 'Extra complexity, duplicated or reinvented code, dead code.'],
  conventions: ['Repo conventions', 'Rules the repo documents: naming, structure, style.'],
  architecture: ['Architecture', 'Wrong layer, coupling, module boundaries, ownership.'],
  'build-dependencies': ['Build and dependencies', 'Build configuration, dependency versions, project files, CI.'],
  observability: ['Logging and metrics', 'Gaps in logging, analytics, metrics, and crash reporting.'],
  other: ['Other', 'Flags that fit none of the other categories.'],
};

const humanize = (id) => cap(String(id || '').replace(/[-_]+/g, ' ').trim());

export const categoryName = (id) => (id ? CATEGORIES[id]?.[0] || humanize(id) : 'Not yet categorized');
export const categoryDef = (id) => CATEGORIES[id]?.[1] || '';
export const categoryPhrase = (id) => {
  const name = categoryName(id);
  return name === 'API misuse' ? name : name.toLowerCase();
};

export const CHANGE_TYPES = {
  'bug-fix': 'Bug fix', enhancement: 'Enhancement', 'new-feature': 'New feature', 'code-maintenance': 'Maintenance',
  'platform-maintenance': 'Platform maintenance', unclassified: 'Not yet classified',
};
export const CHANGE_ORDER = ['bug-fix', 'enhancement', 'new-feature', 'code-maintenance', 'platform-maintenance', 'unclassified'];
export const changeTypeName = (id) => CHANGE_TYPES[id || 'unclassified'] || humanize(id);

export const PUSH_KINDS = {
  rebase: ['Rebase', 'The branch was replayed onto a newer base with the same changes.'],
  merge_from_base: ['Merge from main', 'The base branch was merged into the pull request.'],
  force_rewrite: ['Force push with new content', 'History was rewritten and the changes differ from before.'],
  fix_after_review: ['Fix after review', 'New commits pushed after a review asked for changes.'],
  new_scope: ['New work', 'New commits that add to the change, not answering a review.'],
  armature_fix: ['Automatic fix', 'A fix Armature pushed on its own.'],
};
export const pushKindName = (id) => (id ? PUSH_KINDS[id]?.[0] || humanize(id) : 'Not yet classified');
export const pushKindDef = (id) => (id ? PUSH_KINDS[id]?.[1] || '' : 'Pushes the classifier hasn’t reached yet.');

const ACRONYMS = { ui: 'UI', api: 'API', ci: 'CI', ux: 'UX' };
export const specialistName = (id) => ACRONYMS[String(id || '').toLowerCase()] || humanize(id) || 'Unknown';

const MODEL_WORDS = { gpt: 'GPT', ai: 'AI', xl: 'XL' };

// "claude-opus-4-6" reads "Claude Opus 4.6", "gemini-2.5-pro" reads "Gemini 2.5 Pro". A date suffix is dropped.
export function modelName(id) {
  const parts = String(id || '').split(/[-_\s]+/).filter(Boolean);
  if (parts.length > 1 && /^\d{8}$/.test(parts[parts.length - 1])) parts.pop();
  const words = [];
  for (const part of parts) {
    const previous = words[words.length - 1];
    if (/^\d+$/.test(part) && previous && /^\d+(\.\d+)*$/.test(previous)) words[words.length - 1] = `${previous}.${part}`;
    else words.push(MODEL_WORDS[part.toLowerCase()] || (/^\d/.test(part) ? part : cap(part)));
  }
  return words.join(' ') || 'Unknown model';
}

export const STAGES = [
  ['waiting_first_review', 'Waiting for first review', 'From opening to the first human review.'],
  ['in_review', 'In review rounds', 'From the first review to the last approval.'],
  ['blocked_by_conflicts', 'Blocked by conflicts', 'Hours the pull request couldn’t merge because of a conflict. This overlaps the other stages.'],
  ['waiting_on_ci', 'Waiting on CI', 'From approval to the last check finishing on the final commit.'],
  ['approved_to_merged', 'Approved to merged', 'From the last approval to the merge.'],
];

// Indexes for the x-axis labels: first, last, and evenly between, never crowded.
export function tickIndexes(count, slots) {
  if (count <= 0) return [];
  if (count === 1) return [0];
  const n = Math.max(2, Math.min(slots, count));
  const out = [];
  for (let i = 0; i < n; i++) out.push(Math.round((i * (count - 1)) / (n - 1)));
  return [...new Set(out)];
}

export function median(values) {
  const sorted = values.filter(isNum).map(Number).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

export function quantile(values, q) {
  const sorted = values.filter(isNum).map(Number).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const at = (sorted.length - 1) * q;
  const low = Math.floor(at);
  const high = Math.ceil(at);
  return sorted[low] + (sorted[high] - sorted[low]) * (at - low);
}

// The fewest people whose share reaches half of the total, for "3 people gave half of all reviews".
export function halfBy(values) {
  const sorted = values.filter(isNum).map(Number).sort((a, b) => b - a);
  const total = sorted.reduce((sum, value) => sum + value, 0);
  if (!total) return 0;
  let running = 0;
  for (let i = 0; i < sorted.length; i++) {
    running += sorted[i];
    if (running * 2 >= total) return i + 1;
  }
  return sorted.length;
}

export function sortRows(rows, key, dir, valueOf = (row, column) => row[column]) {
  const sign = dir === 'ascending' ? 1 : -1;
  return [...rows].sort((a, b) => {
    const av = valueOf(a, key);
    const bv = valueOf(b, key);
    const aMissing = av === null || av === undefined;
    const bMissing = bv === null || bv === undefined;
    if (aMissing || bMissing) return aMissing === bMissing ? 0 : aMissing ? 1 : -1;
    const result = typeof av === 'string' ? av.localeCompare(bv) : av - bv;
    return result * sign;
  });
}
