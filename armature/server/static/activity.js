// Where a run is: who is working on each step, what each of them found, and what was decided.
// Built only from step ids, a `role` on events, `finding` events, and a log line with a `verdict`.
import { duration } from './dom.js';
import { elapsedSeconds, eventStepKey, oneLine, stepKey, summarizeInput } from './feed.js';

export const ACTIVITY = new Set(['tool_call', 'tool_result', 'text', 'session_start', 'session_end', 'cost']);
const BOUNDARY = new Set(['step_start', 'step_end']);
const FILE_TOOLS = new Set(['Read', 'Edit', 'Write', 'NotebookEdit']);
const NOW_MAX = 120;
const SEVERITY_ORDER = ['critical', 'blocking', 'high', 'medium', 'low', 'info'];
export const ICONS = { done: '✓', running: '⟳', waiting: '…', failed: '✕', skipped: '–' };

export const roleOf = (payload) => (typeof payload?.role === 'string' && payload.role.trim() ? payload.role.trim() : null);
export const isBoundary = (kind) => BOUNDARY.has(kind);
export const isActivity = (kind, payload) => ACTIVITY.has(kind) && roleOf(payload) !== null;
export const capitalize = (text) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : '');

// started, finished, or failed, from a role's log line: its state field, else its level and first word.
export function roleState(payload) {
  const state = String(payload?.state || '');
  if (state === 'started' || state === 'finished' || state === 'failed') return state;
  if (payload?.level === 'error') return 'failed';
  const word = String(payload?.message || '').trim().toLowerCase().split(/\s/)[0];
  return word === 'finished' || word === 'started' ? word : null;
}

const basename = (path) => String(path).split('/').filter(Boolean).at(-1) || String(path);

// What a role is doing now, as one line, or null when the event says nothing new.
export function nowText(kind, payload) {
  if (kind === 'tool_call') {
    const tool = payload.tool || 'Tool';
    const input = summarizeInput(payload.input);
    if (FILE_TOOLS.has(tool) && input) return oneLine(`${tool} ${basename(input)}`, NOW_MAX);
    return oneLine(input ? `${tool}: ${input}` : tool, NOW_MAX);
  }
  if (kind === 'text' && payload.text) return oneLine(`thinking: ${payload.text}`, NOW_MAX);
  if (kind === 'session_start') return 'starting';
  return null;
}

export const isBlocking = (finding) => finding?.blocking === true || ['critical', 'blocking'].includes(String(finding?.severity || '').toLowerCase());

export function severityRank(finding) {
  const index = SEVERITY_ORDER.indexOf(String(finding?.severity || '').toLowerCase());
  return (isBlocking(finding) ? 0 : 10) + (index < 0 ? SEVERITY_ORDER.length : index);
}

// The badge text for a finding: blocking first, then its severity.
export function findingBadge(finding) {
  const severity = finding?.severity ? capitalize(String(finding.severity)) : '';
  if (isBlocking(finding)) return severity && severity.toLowerCase() !== 'blocking' ? `Blocking · ${severity}` : 'Blocking';
  return severity || 'Finding';
}

export const findingWhere = (finding) => (finding?.path ? `${finding.path}${finding.line ? `:${finding.line}` : ''}` : '');

export function verdictTone(verdict) {
  const text = String(verdict || '').toLowerCase();
  if (/\b(not approved|rejected|failed|blocked)\b/.test(text)) return 'bad';
  if (/^approved?\b/.test(text) && !/change|suggest/.test(text)) return 'ok';
  return 'warn';
}

export class RunActivity {
  constructor() {
    this.roles = new Map();
    this.findings = [];
    this.decision = null;
    this.roleSteps = new Set();
    this.lastSeq = 0;
  }

  // Events oldest first. True when anything a reader sees changed.
  apply(events) {
    let changed = false;
    for (const event of events) {
      const seq = Number(event?.seq);
      if (!Number.isFinite(seq) || seq <= this.lastSeq) continue;
      this.lastSeq = seq;
      if (this.add(event)) changed = true;
    }
    return changed;
  }

  add(event) {
    const kind = String(event.kind || '');
    const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
    const role = roleOf(payload);
    const key = eventStepKey(event);
    if (kind === 'finding') {
      this.findings.push({ ...payload, seq: Number(event.seq), ts: event.ts || null, step_key: key });
      return true;
    }
    if (kind === 'log' && typeof payload.verdict === 'string' && payload.verdict) {
      this.decision = { verdict: payload.verdict, message: payload.message ? String(payload.message) : payload.verdict, role, step_key: key, ts: event.ts || null,
        findings: Number.isFinite(Number(payload.findings)) ? Number(payload.findings) : null, blocking: Number.isFinite(Number(payload.blocking)) ? Number(payload.blocking) : null };
    }
    if (!role) return kind === 'log' && Boolean(payload.verdict);
    this.roleSteps.add(key);
    const id = `${key ?? ''}\u0000${role}`;
    let entry = this.roles.get(id);
    if (!entry) {
      entry = { role, step_key: key, state: 'working', started_at: event.ts || null, finished_at: null, now: null, message: null, findings: null, review: null, error: null };
      this.roles.set(id, entry);
    }
    if (kind === 'log') {
      const state = roleState(payload);
      const message = payload.message === undefined || payload.message === null ? null : String(payload.message);
      if (state === 'started') {
        Object.assign(entry, { state: 'working', started_at: event.ts || entry.started_at, finished_at: null, error: null });
      } else if (state === 'finished') {
        Object.assign(entry, { state: 'finished', finished_at: event.ts || null, now: null });
        if (Number.isFinite(Number(payload.findings)) && payload.findings !== null && payload.findings !== '') entry.findings = Number(payload.findings);
        if (typeof payload.review === 'string') entry.review = payload.review;
      } else if (state === 'failed') {
        Object.assign(entry, { state: 'failed', finished_at: event.ts || null, now: null, error: message });
      }
      if (message !== null) entry.message = message;
      return true;
    }
    if (entry.state !== 'working') return false;
    const now = nowText(kind, payload);
    if (now === null || now === entry.now) return false;
    entry.now = now;
    return true;
  }

  // Roles of a step and of the steps nested under it, in the order they first appeared.
  rolesOf(key) {
    return [...this.roles.values()].filter((entry) => entry.step_key === key || String(entry.step_key ?? '').startsWith(`${key}/`));
  }

  // What the finished roles reported finding, before any lead weighs it.
  reportedFindings() {
    let total = 0;
    for (const entry of this.roles.values()) if (entry.state === 'finished' && entry.findings) total += entry.findings;
    return total;
  }
}

// Pages forward through a run's events into ``activity``. ``fetchPage(after)`` resolves to `{events, next_after}`.
export async function backfill(activity, fetchPage, { maxPages = 20, signal } = {}) {
  let after = 0;
  for (let page = 0; page < maxPages; page += 1) {
    const body = await fetchPage(after);
    if (signal?.aborted) return;
    const events = Array.isArray(body?.events) ? body.events : [];
    activity.apply(events);
    const next = body?.next_after;
    if (next === null || next === undefined || !events.length || Number(next) <= after) return;
    after = Number(next);
  }
}

const STAGE_STATE = { succeeded: 'done', failed: 'failed', running: 'running', skipped: 'skipped', step_skipped: 'skipped' };

export const stageLabel = (stepId) => String(stepId || '').replace(/[_-]+/g, ' ').trim() || 'step';

export function rosterLine(entry, now = Date.now()) {
  const seconds = elapsedSeconds(entry.started_at, entry.finished_at, now);
  if (entry.state === 'failed') return { state: 'failed', icon: ICONS.failed, label: 'failed', detail: entry.error || entry.message || '' };
  if (entry.state === 'finished') {
    const found = entry.findings === null ? 'finished' : entry.findings === 0 ? 'no findings' : entry.findings === 1 ? '1 finding' : `${entry.findings} findings`;
    return { state: 'done', icon: ICONS.done, label: found, detail: seconds ? duration(seconds) : '' };
  }
  return { state: 'running', icon: ICONS.running, label: 'working', detail: [seconds ? duration(seconds) : null, entry.now].filter(Boolean).join(' · ') };
}

// One line for a single role at work: "Team lead is weighing 9 reviews (12 findings so far)".
function headline(roles, activity) {
  const working = roles.filter((entry) => entry.state === 'working');
  if (roles.length !== 1 || working.length !== 1 || !working[0].message) return null;
  const { role, message } = working[0];
  const soFar = activity.reportedFindings();
  const tail = soFar ? ` (${soFar === 1 ? '1 finding' : `${soFar} findings`} so far)` : '';
  return /^\w+ing\b/.test(message) ? `${capitalize(role)} is ${message}${tail}` : `${capitalize(role)}: ${message}${tail}`;
}

// The run's top-level steps in order, each with its roles. ``steps`` comes from the run and its step events.
export function stageTrack(steps, activity, now = Date.now()) {
  const top = (steps || []).filter((step) => !step.parent_step);
  return top.map((step) => {
    const key = stepKey(step);
    const roles = activity.rolesOf(key);
    const total = Number(step.branch_total) || 0;
    const done = roles.filter((entry) => entry.state === 'finished').length;
    const failed = roles.filter((entry) => entry.state === 'failed').length;
    const state = STAGE_STATE[step.status] || 'waiting';
    return {
      key, step_id: step.step_id, label: stageLabel(step.step_id), state, icon: ICONS[state],
      current: state === 'running',
      seconds: elapsedSeconds(step.started_at, step.finished_at, now),
      roles, done, failed,
      waiting: roles.length && total > roles.length ? total - roles.length : 0,
      total: Math.max(total, roles.length),
      findings: roles.reduce((sum, entry) => sum + (entry.findings || 0), 0),
      headline: state === 'running' ? headline(roles, activity) : null,
      error: step.error || null,
    };
  });
}

// The stage to talk about now: a running one with roles, any running one, else the last that did anything.
export function currentStage(track) {
  return track.find((stage) => stage.current && stage.roles.length) || track.find((stage) => stage.current)
    || [...track].reverse().find((stage) => stage.state !== 'waiting') || null;
}

// "reviews · 6/9 done: security ✓ 2, beauty ⟳, …" for a panel header.
export function compactWhere(track, now = Date.now()) {
  const stage = currentStage(track);
  if (!stage) return '';
  const head = `${stage.label} ${stage.icon}`;
  if (stage.headline) return `${head} · ${stage.headline}`;
  if (!stage.roles.length) return stage.seconds ? `${head} · ${duration(stage.seconds)}` : head;
  const chips = stage.roles.map((entry) => {
    const line = rosterLine(entry, now);
    if (line.state === 'done') return `${entry.role} ${ICONS.done}${entry.findings ? ` ${entry.findings}` : ''}`;
    return `${entry.role} ${line.icon}`;
  });
  if (stage.waiting) chips.push(`${stage.waiting} more waiting`);
  return `${head} · ${stage.done}/${stage.total} done: ${chips.join(', ')}`;
}

// Which of the run log's sections an item goes in: a role's collapsed activity, a step boundary, or the main lines.
export function feedSlot(item) {
  const role = roleOf(item?.payload);
  if (role && ACTIVITY.has(item.kind)) return { slot: 'activity', role };
  if (BOUNDARY.has(item?.kind)) return { slot: 'boundary', role: null };
  return { slot: 'line', role };
}
