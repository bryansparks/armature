// A live tail of one run's newest events, at the top of a thread's page while the run is in progress.
import { backfill, compactWhere, isActivity, isBoundary, nowText, roleOf, roleState, RunActivity, stageTrack, verdictTone } from './activity.js';
import { api, listOf } from './api.js';
import { clear, duration, element, errorBox, link, statusChip } from './dom.js';
import { applyStepEvent, describe, elapsedSeconds, eventStepKey, newestBefore, oneLine, summarizeInput, TERMINAL } from './feed.js';
import { every, subscribe } from './live.js';
import { hashFor } from './router.js';
import { progress } from './runpage.js';

export const TAIL_CAP = 300;
export const TAIL_LOAD = 150;
const BACKFILL_PAGE = 1000;
const LOG_LINE_MAX = 2000;
const POLL_MS = 2000;
const SETTLE_MS = 1500;
const ANNOUNCE_MS = 2000;
const BOTTOM_SLACK = 24;
const BEAT_KEYS = new Set(['elapsed_s', 'stage']);
const LIVE_STATES = { live: 'Live', reconnecting: 'Reconnecting…', polling: 'Refreshing every 2 s', ended: 'Finished' };
const STEP_WORDS = { step_start: 'started', step_end: 'finished', step_failed: 'failed', step_skipped: 'skipped', retry: 'retrying' };

const runPath = (runId) => `/api/runs/${encodeURIComponent(runId)}`;
const beforePath = (runId, before, limit) => `/api/runs/${encodeURIComponent(runId)}/events?before=${before}&limit=${limit}`;
const afterPath = (runId, after, limit) => `/api/runs/${encodeURIComponent(runId)}/events?after=${after}&limit=${limit}`;
const streamPath = (runId, after) => (after === null ? `/api/runs/${encodeURIComponent(runId)}/stream` : `/api/runs/${encodeURIComponent(runId)}/stream?after=${after}`);

export function isHeartbeat(event) {
  if (event?.kind !== 'progress') return false;
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
  return typeof payload.elapsed_s === 'number' && Object.keys(payload).every((key) => BEAT_KEYS.has(key));
}

export const stillRunning = (beat) => (beat.step ? `${beat.step} · still running ${duration(beat.elapsed_s)}` : `Still running · ${duration(beat.elapsed_s)}`);

// Heartbeats update one line instead of adding rows, and the oldest rows go once there are more than ``cap``.
// A role's session activity updates that role's one "now" line, which goes when the role finishes. A step
// whose events carry roles loses its start and finish rows, since its roles' own lines say more.
export class TailModel {
  constructor(cap = TAIL_CAP) {
    this.cap = cap;
    this.rows = [];
    this.lastSeq = 0;
    this.beat = null;
    this.nextId = 0;
    this.now = new Map();
    this.roleSteps = new Set();
  }

  append(events) {
    const added = [];
    const hidden = [];
    for (const event of events) {
      const seq = Number(event?.seq);
      if (!Number.isFinite(seq) || seq <= this.lastSeq) continue;
      this.lastSeq = seq;
      const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
      if (isHeartbeat(event)) {
        this.beat = { elapsed_s: payload.elapsed_s, step: event.step_id || payload.stage || '' };
        continue;
      }
      this.beat = null;
      const kind = String(event.kind || 'unknown');
      const key = eventStepKey(event);
      const role = roleOf(payload);
      if (role) {
        if (!this.roleSteps.has(key)) {
          this.roleSteps.add(key);
          const boundaries = this.rows.filter((row) => row.step_key === key && isBoundary(row.kind));
          this.rows = this.rows.filter((row) => !boundaries.includes(row));
          hidden.push(...boundaries);
        }
        const id = `${key ?? ''}\u0000${role}`;
        if (isActivity(kind, payload)) {
          const text = nowText(kind, payload);
          if (text) this.now.set(id, { role, step_key: key, text });
          continue;
        }
        const state = kind === 'log' ? roleState(payload) : null;
        if (state === 'finished' || state === 'failed') this.now.delete(id);
      }
      if (isBoundary(kind) && this.roleSteps.has(key)) continue;
      const row = { id: ++this.nextId, seq, kind, step_key: key, ts: event.ts || null, payload };
      this.rows.push(row);
      added.push(row);
    }
    const dropped = [...hidden, ...this.rows.splice(0, Math.max(0, this.rows.length - this.cap))];
    const fresh = new Set(added);
    const gone = new Set(dropped);
    return { added: added.filter((row) => !gone.has(row)), removed: dropped.filter((row) => !fresh.has(row)).map((row) => row.id) };
  }

  nowLines() {
    return [...this.now.values()];
  }
}

export function tailLine(row, { showCost = true } = {}) {
  const payload = row.payload || {};
  const who = roleOf(payload);
  if (who && row.kind === 'log' && payload.message !== undefined && payload.message !== null) {
    const tone = payload.level === 'error' ? 'bad' : payload.verdict ? verdictTone(payload.verdict) : 'muted';
    return { tone, label: null, text: oneLine(payload.message, 400), mono: false, who };
  }
  const logged = payload.message ?? payload.line;
  if (row.kind === 'log' && logged !== undefined && logged !== null) {
    const text = String(logged);
    return { tone: 'muted', label: payload.stream ? String(payload.stream) : 'log',
      text: text.length > LOG_LINE_MAX ? `${text.slice(0, LOG_LINE_MAX - 1)}…` : text, mono: true, who };
  }
  if (row.kind === 'text') return { tone: 'text', label: 'Text', text: oneLine(payload.text, 400), mono: false, who };
  if (row.kind === 'tool_call') return { tone: 'muted', label: payload.tool || 'Tool', text: summarizeInput(payload.input), mono: false, who };
  return { ...describe(row, { showCost }), mono: false, who };
}

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

// Says a run ended when it was seen running and then ending. A run already over when the tail opens counts only
// when the page drew it as running (``drawnLive``), and the page's redraw backoff bounds how often that repeats.
export function endNotice(onEnd, { drawnLive = false } = {}) {
  let wasLive = false;
  let loaded = false;
  let told = false;
  const tell = (run) => {
    if (told) return false;
    told = true;
    onEnd(run);
    return true;
  };
  return {
    opened: (run) => {
      wasLive = !TERMINAL.has(run.status);
      return !wasLive && drawnLive ? tell(run) : false;
    },
    loaded: () => { loaded = true; },
    // The end arrived among the events the tail loaded on opening, so it ended before anyone watched.
    ending: () => { if (!loaded) wasLive = false; },
    ended: (run) => (wasLive && loaded ? tell(run) : false),
  };
}

export function openTail({ runId, run: seed = {}, label, color, title, subject, display, signal: viewSignal, drawnLive = false, onEnd = () => {} }) {
  const controller = new AbortController();
  const signal = controller.signal;
  const stopWithView = () => close();
  viewSignal?.addEventListener('abort', stopWithView);
  let run = { ...seed };
  let steps = [];
  const notice = endNotice(onEnd, { drawnLive });

  const chipSlot = element('span', {}, statusChip(run.status));
  const elapsedText = element('span', { class: 'mono' });
  const liveNote = element('span', { class: 'muted' }, 'Connecting…');
  const progressText = element('p', { class: 'run-progress' });
  const whereText = element('p', { class: 'run-where', hidden: true });
  const nowList = element('div', { class: 'tail-now', hidden: true });
  const list = element('div', { class: 'tail-list' });
  const box = element('div', { class: 'tail-box', tabindex: 0, role: 'region', 'aria-label': 'Newest events' }, list);
  const beatLine = element('p', { class: 'tail-beat', hidden: true });
  const jumpButton = element('button', { type: 'button', class: 'jump', hidden: true }, 'Jump to latest');
  const announcer = element('div', { class: 'visually-hidden', role: 'status' });
  const node = element('section', { class: 'tail-panel', role: 'region', 'aria-label': `Log of ${label}` },
    element('div', { class: 'tail-head' },
      element('h3', { class: 'tail-title' }, element('span', { class: `swatch sw-${color}`, 'aria-hidden': 'true' }), label)),
    element('div', { class: 'tail-meta' }, element('span', { class: 'tail-name' }, title), subject, chipSlot, elapsedText, liveNote,
      link(hashFor('run', [runId]), 'Open full run')),
    progressText, whereText, element('div', { class: 'tail-frame' }, box, jumpButton), nowList, beatLine, announcer);

  let shownStatus = run.status;
  const drawHeader = () => {
    if (run.status !== shownStatus) {
      shownStatus = run.status;
      clear(chipSlot).append(statusChip(run.status));
    }
    const isOpen = !TERMINAL.has(run.status);
    setText(elapsedText, duration(elapsedSeconds(run.started_at, run.finished_at || (isOpen ? null : run.started_at))));
  };
  const activity = new RunActivity();
  let activityReady = false;
  let activityQueue = [];
  const syncWhere = () => {
    const text = compactWhere(stageTrack(steps, activity));
    whereText.hidden = !text;
    setText(whereText, text);
  };
  const syncProgress = () => {
    setText(progressText, progress(steps).text);
    syncWhere();
  };
  const feedActivity = (events) => {
    if (!activityReady) activityQueue.push(...events);
    else if (activity.apply(events)) syncWhere();
  };
  const setLive = (state) => setText(liveNote, LIVE_STATES[state] || '');
  drawHeader();
  syncProgress();

  let announceTimer = 0;
  let pending = [];
  const announce = (text) => {
    pending.push(text);
    if (announceTimer) return;
    announceTimer = setTimeout(() => {
      announceTimer = 0;
      setText(announcer, pending.slice(-3).join(' '));
      pending = [];
    }, ANNOUNCE_MS);
  };

  const model = new TailModel();
  const nodes = new Map();
  let following = true;
  const isAtBottom = () => box.scrollHeight - box.scrollTop - box.clientHeight <= BOTTOM_SLACK;
  const toBottom = () => {
    box.scrollTop = box.scrollHeight;
  };
  box.addEventListener('scroll', () => {
    following = isAtBottom();
    jumpButton.hidden = following;
  });
  jumpButton.addEventListener('click', () => {
    following = true;
    toBottom();
    jumpButton.hidden = true;
    box.focus();
  });

  const rowNode = (row) => {
    const line = tailLine(row, { showCost: display.show_cost });
    const who = line.who || row.step_key;
    return element('div', { class: `tail-row tone-${line.tone}${line.mono ? ' tail-log' : ''}`, title: row.ts },
      who ? element('span', { class: line.who ? 'tail-step tail-role' : 'tail-step' }, who) : null,
      line.label ? [element('span', { class: 'feed-label' }, line.label), ' '] : null, element('span', { class: 'tail-text' }, line.text));
  };
  const drawNow = () => {
    const lines = model.nowLines();
    nowList.hidden = !lines.length;
    clear(nowList).append(...lines.map((entry) => element('p', { class: 'tail-now-line' },
      element('span', { class: 'tail-step tail-role' }, entry.role), ' ', element('span', { class: 'tail-text' }, entry.text))));
  };
  const draw = ({ added, removed }) => {
    if (removed.length) {
      const before = list.scrollHeight;
      for (const id of removed) {
        nodes.get(id)?.remove();
        nodes.delete(id);
      }
      if (!following) box.scrollTop -= before - list.scrollHeight;
    }
    const fragment = document.createDocumentFragment();
    for (const row of added) {
      const rowElement = rowNode(row);
      nodes.set(row.id, rowElement);
      fragment.append(rowElement);
    }
    list.append(fragment);
    drawNow();
    beatLine.hidden = !model.beat;
    if (model.beat) setText(beatLine, stillRunning(model.beat));
    if (following) toBottom();
    jumpButton.hidden = following;
  };

  let settled = false;
  let stream = null;
  let poller = null;
  let settleTimer = 0;
  const ticker = setInterval(() => {
    drawHeader();
    syncWhere();
  }, 1000);
  const settleSoon = () => {
    if (settled) return;
    settled = true;
    notice.ending();
    settleTimer = setTimeout(() => {
      stream?.close();
      poller?.stop();
      clearInterval(ticker);
      setLive('ended');
      drawHeader();
      notice.ended(run);
    }, SETTLE_MS);
  };

  const sideEffects = (events, { live }) => {
    let stepsChanged = false;
    for (const event of events) {
      if (Number(event?.seq) <= model.lastSeq) continue;
      if (applyStepEvent(steps, event)) {
        stepsChanged = true;
        if (live) announce(`Step ${event.step_id} ${STEP_WORDS[event.kind]}.`);
      }
      const payload = event.payload || {};
      if (event.kind === 'run_end' && payload.status) {
        run = { ...run, status: payload.status, finished_at: run.finished_at || event.ts };
        if (live) announce(`Run ${payload.status}.`);
        drawHeader();
        settleSoon();
      }
    }
    if (stepsChanged) syncProgress();
  };

  let loading = true;
  let queue = [];
  let frame = 0;
  const flush = () => {
    frame = 0;
    if (loading || !queue.length || signal.aborted) return;
    const batch = queue.sort((left, right) => left.seq - right.seq);
    queue = [];
    sideEffects(batch, { live: true });
    feedActivity(batch);
    draw(model.append(batch));
  };
  const receive = (events) => {
    queue.push(...events);
    if (!loading && !frame) frame = requestAnimationFrame(flush);
  };
  const takeRun = (detail) => {
    if (detail?.run) run = { ...run, ...detail.run };
    if (Array.isArray(detail?.steps)) steps = detail.steps.map((step) => ({ ...step }));
    drawHeader();
    syncProgress();
  };

  const startPolling = () => {
    poller = every(POLL_MS, async () => {
      receive(listOf(await api(afterPath(runId, model.lastSeq, TAIL_LOAD)), 'events'));
      takeRun(await api(runPath(runId)));
      if (TERMINAL.has(run.status)) settleSoon();
      return !settled && !signal.aborted;
    }, signal);
  };
  // The server sends `reconnect` and closes when the panel falls behind, so the stream reopens from the last seq seen.
  const openStream = (after) => {
    stream?.close();
    stream = subscribe(streamPath(runId, after), {
      events: {
        'run-event': (event) => receive([event]),
        run: (summary) => {
          run = { ...run, ...summary };
          drawHeader();
          if (TERMINAL.has(run.status)) settleSoon();
        },
        reconnect: () => {
          if (!settled) openStream(model.lastSeq);
        },
      },
      onState: setLive,
      onFallback: startPolling,
    }, { signal });
  };

  const start = async () => {
    try {
      const detail = await api(runPath(runId));
      if (signal.aborted) return;
      takeRun(detail);
      notice.opened(run);
      const lastSeq = run.last_seq === undefined || run.last_seq === null ? NaN : Number(run.last_seq);
      if (Number.isFinite(lastSeq)) {
        const events = listOf(await api(beforePath(runId, newestBefore(lastSeq), TAIL_LOAD)), 'events');
        if (signal.aborted) return;
        sideEffects(events, { live: false });
        feedActivity(events);
        draw(model.append(events));
      }
      loading = false;
      notice.loaded();
      backfill(activity, (after) => api(afterPath(runId, after, BACKFILL_PAGE)), { signal }).catch(() => {}).finally(() => {
        if (signal.aborted) return;
        activityReady = true;
        activity.apply(activityQueue.sort((left, right) => left.seq - right.seq));
        activityQueue = [];
        syncWhere();
      });
      if (queue.length) frame = requestAnimationFrame(flush);
      if (TERMINAL.has(run.status)) {
        settled = true;
        clearInterval(ticker);
        setLive('ended');
      } else {
        openStream(Number.isFinite(lastSeq) ? lastSeq : null);
      }
    } catch (err) {
      if (!signal.aborted) list.append(errorBox(err));
    }
  };

  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    controller.abort();
    viewSignal?.removeEventListener('abort', stopWithView);
    clearInterval(ticker);
    clearTimeout(announceTimer);
    clearTimeout(settleTimer);
    cancelAnimationFrame(frame);
  }
  start();

  return { node, close };
}
