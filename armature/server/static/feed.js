// The run feed's state, kept free of the DOM so the node tests can drive it.
import { money, statusText } from './dom.js';

export const NODE_CAP = 2000;
export const TERMINAL = new Set(['succeeded', 'failed', 'canceled', 'lost']);

const ONE_LINE = 140;
const INPUT_KEYS = ['command', 'file_path', 'path', 'pattern', 'url', 'query', 'description', 'prompt'];
const INPUT_KEY_TEXT = new RegExp(`"(${INPUT_KEYS.join('|')})"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)`);

export function oneLine(text, max = ONE_LINE) {
  const flat = String(text ?? '').replace(/\s+/g, ' ').trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

function unescapeJson(text) {
  try {
    return JSON.parse(`"${text}"`);
  } catch {
    return text;
  }
}

// Tool input arrives as JSON text, often cut short, so a key is also read from the raw text.
export function summarizeInput(input) {
  if (input === null || input === undefined) return '';
  const text = typeof input === 'string' ? input : JSON.stringify(input);
  let parsed = null;
  try {
    parsed = JSON.parse(text);
  } catch {}
  if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
    const key = INPUT_KEYS.find((name) => typeof parsed[name] === 'string' && parsed[name]);
    if (key) return oneLine(parsed[key]);
  } else if (!parsed) {
    const match = INPUT_KEY_TEXT.exec(text);
    if (match) return oneLine(unescapeJson(match[2]));
  }
  return oneLine(text);
}

export function elapsedSeconds(startedAt, finishedAt, now = Date.now()) {
  if (!startedAt) return null;
  const start = new Date(startedAt).getTime();
  const end = finishedAt ? new Date(finishedAt).getTime() : now;
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
  return Math.max(0, (end - start) / 1000);
}

const STEP_STATUS = { step_start: 'running', retry: 'running', step_end: 'succeeded', step_failed: 'failed', step_skipped: 'skipped' };
const BRANCH_DONE = new Set(['step_end', 'step_failed', 'step_skipped']);

// A fan-out step reports each branch with `branch`, and the total with `branches` as a count or a list.
function noteBranches(step, event) {
  const payload = event.payload || {};
  const total = Array.isArray(payload.branches) ? payload.branches.length : Number(payload.branches);
  if (Number.isFinite(total) && total > 0) step.branch_total = total;
  if (payload.branch === undefined || payload.branch === null) return false;
  step.branches_done ||= [];
  const branch = String(payload.branch);
  if (BRANCH_DONE.has(event.kind) && !step.branches_done.includes(branch)) step.branches_done.push(branch);
  if (step.status !== 'running' && !TERMINAL.has(step.status)) step.status = 'running';
  return true;
}

// Unique per run: a child step's id can repeat under different parents.
export const stepKey = (step) => step.step_key || (step.parent_step ? `${step.parent_step}/${step.step_id}` : step.step_id);

export function eventStepKey(event) {
  if (!event?.step_id) return null;
  const parent = event.parent_step ?? event.payload?.parent_step;
  return parent ? `${parent}/${event.step_id}` : event.step_id;
}

export function applyStepEvent(steps, event) {
  const status = STEP_STATUS[event?.kind];
  const key = eventStepKey(event);
  if (!status || !key) return null;
  let step = steps.find((candidate) => stepKey(candidate) === key);
  if (!step) {
    step = { step_key: key, step_id: event.step_id, status: 'queued', started_at: null, finished_at: null, cost_usd: null, error: null };
    const parent = event.parent_step ?? event.payload?.parent_step;
    if (parent) step.parent_step = parent;
    steps.push(step);
  }
  if (noteBranches(step, event)) {
    step.started_at = step.started_at || event.ts;
    return step;
  }
  step.status = status;
  if (event.kind === 'step_start') {
    step.started_at = step.started_at || event.ts;
    step.finished_at = null;
  } else if (event.kind !== 'retry') {
    step.finished_at = event.ts;
  }
  if (event.kind === 'step_failed') step.error = event.payload?.reason ?? event.payload?.error ?? null;
  return step;
}

export function branchText(step) {
  if (step?.status !== 'running') return '';
  const done = step.branches_done?.length || 0;
  if (step.branch_total) return `${done}/${step.branch_total} branches done`;
  return done ? `${done} branches done` : '';
}

// parent_step names the parent's step_id. A top-level step with that id wins over a nested one.
function parentKeys(steps) {
  const keyOfId = new Map();
  for (const step of steps) {
    const current = keyOfId.get(step.step_id);
    if (!current || (!current.topLevel && !step.parent_step)) keyOfId.set(step.step_id, { key: stepKey(step), topLevel: !step.parent_step });
  }
  const parents = new Map();
  for (const step of steps) {
    const key = stepKey(step);
    const parent = step.parent_step ? keyOfId.get(step.parent_step)?.key : null;
    parents.set(key, parent && parent !== key ? parent : null);
  }
  return parents;
}

// Parents come before their children. A child whose parent is unknown sits at the top. Keys are step keys.
export function stepTree(steps) {
  const parents = parentKeys(steps);
  const children = new Map();
  for (const step of steps) {
    const parent = parents.get(stepKey(step));
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(step);
  }
  const ordered = [];
  const seen = new Set();
  const walk = (parent, depth) => {
    for (const step of children.get(parent) || []) {
      const key = stepKey(step);
      if (seen.has(key)) continue;
      seen.add(key);
      ordered.push({ step, key, depth, parent });
      walk(key, depth + 1);
    }
  };
  walk(null, 0);
  for (const step of steps) if (!seen.has(stepKey(step))) ordered.push({ step, key: stepKey(step), depth: 0, parent: null });
  return ordered;
}

export function descendants(steps, key) {
  const parents = parentKeys(steps);
  const keys = new Set([key]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const [child, parent] of parents) {
      if (parent && keys.has(parent) && !keys.has(child)) {
        keys.add(child);
        grew = true;
      }
    }
  }
  return keys;
}

const COALESCE = {
  progress: (item, payload) => { item.payload = payload; },
  dropped: (item, payload) => { item.payload = { ...payload, count: (Number(item.payload.count) || 0) + (Number(payload.count) || 0) }; },
};

export class FeedModel {
  constructor({ cap = NODE_CAP } = {}) {
    this.cap = cap;
    this.nextId = 0;
    this.reset();
  }

  reset() {
    this.items = [];
    this.calls = new Map();
    this.lastSeq = 0;
    this.detached = false;
    this.missed = 0;
    this.startSeq = null;
  }

  get firstSeq() {
    return this.items.length ? this.items[0].seq : this.lastSeq + 1;
  }

  get hasEarlier() {
    return this.firstSeq > (this.startSeq ?? 1);
  }

  // A page with prev_before null reached the run's first event.
  noteStart(page, events) {
    if (page?.prev_before === null) this.startSeq = Number(events[0]?.seq ?? this.firstSeq);
  }

  // Live events, oldest first. Returns what the renderer must add, refresh, and remove.
  append(events) {
    const changes = { added: [], updated: new Set(), removed: [] };
    for (const event of events) {
      const seq = Number(event?.seq);
      if (!Number.isFinite(seq) || seq <= this.lastSeq) continue;
      this.lastSeq = seq;
      if (this.detached) {
        this.missed += 1;
        continue;
      }
      this.add(event, changes);
    }
    const added = new Set(changes.added);
    const gone = new Set();
    const excess = this.items.length - this.cap;
    if (excess > 0) {
      for (const item of this.items.splice(0, excess)) {
        gone.add(item);
        this.forget(item);
        if (!added.has(item)) changes.removed.push(item.id);
      }
    }
    return {
      added: changes.added.filter((item) => !gone.has(item)),
      updated: [...changes.updated].filter((item) => !added.has(item) && !gone.has(item)),
      removed: changes.removed,
    };
  }

  // An earlier page, oldest first. Keeps the cap by dropping the newest, which leaves the feed behind live.
  prepend(events) {
    const head = new FeedModel({ cap: Infinity });
    head.nextId = this.nextId;
    head.append(events.filter((event) => Number(event?.seq) < this.firstSeq));
    this.nextId = head.nextId;
    for (const [toolUseId, call] of head.calls) if (!this.calls.has(toolUseId)) this.calls.set(toolUseId, call);
    this.items = [...head.items, ...this.items];
    if (this.items.length > this.cap) {
      for (const item of this.items.splice(this.cap)) this.forget(item);
      this.detached = true;
      this.lastSeq = this.items.at(-1).lastSeq;
    }
  }

  add(event, changes) {
    const kind = String(event.kind || 'unknown');
    const stepKeyOf = eventStepKey(event);
    const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
    const seq = Number(event.seq);
    if (kind === 'tool_result' && payload.tool_use_id && this.calls.has(payload.tool_use_id)) {
      const call = this.calls.get(payload.tool_use_id);
      call.result = payload;
      changes.updated.add(call);
      return;
    }
    const last = this.items.at(-1);
    if (last && COALESCE[kind] && last.kind === kind && last.step_key === stepKeyOf) {
      COALESCE[kind](last, payload);
      last.lastSeq = seq;
      last.count += 1;
      changes.updated.add(last);
      return;
    }
    const item = { id: ++this.nextId, seq, lastSeq: seq, ts: event.ts || null, kind, step_key: stepKeyOf, payload, result: null, count: 1 };
    this.items.push(item);
    if (kind === 'tool_call' && payload.tool_use_id) this.calls.set(payload.tool_use_id, item);
    changes.added.push(item);
  }

  forget(item) {
    if (item.kind === 'tool_call' && this.calls.get(item.payload.tool_use_id) === item) this.calls.delete(item.payload.tool_use_id);
  }
}

// Consecutive items of one step key share a group, so a step that runs twice gets two groups in order.
export function groupItems(items) {
  const groups = [];
  for (const item of items) {
    const last = groups.at(-1);
    if (last && last.step_key === item.step_key) last.items.push(item);
    else groups.push({ step_key: item.step_key, items: [item] });
  }
  return groups;
}

export const newestBefore = (lastSeq) => Number(lastSeq) + 1;

// The page after this one, for loading earlier events by seq when the server has no before param.
export function earlierPage(firstSeq, size) {
  const after = Math.max(0, firstSeq - 1 - size);
  return { after, limit: Math.max(0, firstSeq - 1 - after) };
}

function count(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString('en-US') : '0';
}

function costText(payload, showCost) {
  const usage = payload.usage && typeof payload.usage === 'object' ? payload.usage : {};
  const inputTokens = payload.input_tokens ?? usage.input_tokens;
  const outputTokens = payload.output_tokens ?? usage.output_tokens;
  const cost = showCost ? payload.cost_usd ?? payload.total_cost_usd : null;
  return [payload.model, `${count(inputTokens)} in, ${count(outputTokens)} out`, cost === undefined || cost === null ? null : money(cost, 4)].filter(Boolean).join(' · ');
}

function stepEndTone(payload) {
  return payload.status && payload.status !== 'succeeded' ? 'bad' : 'ok';
}

// One line for any event the feed does not draw specially. Unknown kinds fall through to the payload.
export function describe(item, { showCost = true } = {}) {
  const payload = item.payload || {};
  const lines = {
    run_start: () => ['muted', 'Run', 'Started'],
    run_end: () => [TERMINAL.has(payload.status) && payload.status !== 'succeeded' ? 'bad' : 'ok', 'Run',
      [`Ended: ${statusText(payload.status)}`, showCost && payload.cost_usd !== undefined && payload.cost_usd !== null ? money(payload.cost_usd) : null, payload.error ? oneLine(payload.error) : null].filter(Boolean).join(' · ')],
    step_start: () => ['muted', 'Step', 'Started'],
    step_end: () => [stepEndTone(payload), 'Step', 'Finished'],
    step_failed: () => ['bad', 'Step', `Failed${payload.error ? `: ${oneLine(payload.error)}` : ''}`],
    step_skipped: () => ['muted', 'Step', `Skipped${payload.reason ? `: ${oneLine(payload.reason)}` : ''}`],
    retry: () => ['warn', 'Retry', `Attempt ${payload.attempt ?? '?'}`],
    log: () => ['muted', 'Log', oneLine(payload.message ?? JSON.stringify(payload), 400)],
    cost: () => ['muted', showCost ? 'Cost' : 'Usage', costText(payload, showCost)],
    finding: () => [payload.severity === 'high' || payload.severity === 'critical' ? 'bad' : 'warn', statusText(String(payload.severity || 'finding')),
      `${oneLine(payload.title || 'Finding')}${payload.path ? ` at ${payload.path}${payload.line ? `:${payload.line}` : ''}` : ''}`],
    session_start: () => ['muted', 'Session', `Started${payload.model ? ` on ${payload.model}` : ''}`],
    session_end: () => [payload.is_error ? 'bad' : 'muted', 'Session', `Ended${payload.status ? ` (${payload.status})` : ''}`],
    error: () => ['bad', 'Error', oneLine(payload.message ?? JSON.stringify(payload), 400)],
    dropped: () => ['warn', 'Skipped', `${count(payload.count)} updates skipped`],
    tool_result: () => [payload.is_error ? 'bad' : 'muted', payload.tool || 'Result', oneLine(payload.content)],
  };
  const [tone, label, text] = (lines[item.kind] || (() => ['muted', item.kind, oneLine(JSON.stringify(payload))]))();
  return { tone, label, text: payload.truncated ? `${text} (truncated)` : text };
}
