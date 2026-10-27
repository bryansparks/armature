// A live tail of one run's newest events, opened from the Live block on the Runs page.
import { api, listOf } from './api.js';
import { clear, duration, element, errorBox, link, statusChip } from './dom.js';
import { applyStepEvent, describe, elapsedSeconds, eventStepKey, newestBefore, oneLine, summarizeInput, TERMINAL } from './feed.js';
import { every, subscribe } from './live.js';
import { hashFor } from './router.js';
import { progress } from './runpage.js';

export const TAIL_CAP = 300;
export const TAIL_LOAD = 150;
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

export const stillRunning = (beat) => `Still running${beat.step ? ` ${beat.step}` : ''} · ${duration(beat.elapsed_s)}`;

// Heartbeats update one line instead of adding rows, and the oldest rows go once there are more than ``cap``.
export class TailModel {
  constructor(cap = TAIL_CAP) {
    this.cap = cap;
    this.rows = [];
    this.lastSeq = 0;
    this.beat = null;
    this.nextId = 0;
  }

  append(events) {
    const added = [];
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
      const row = { id: ++this.nextId, seq, kind: String(event.kind || 'unknown'), step_key: eventStepKey(event), ts: event.ts || null, payload };
      this.rows.push(row);
      added.push(row);
    }
    const dropped = this.rows.splice(0, Math.max(0, this.rows.length - this.cap));
    const fresh = new Set(added);
    const gone = new Set(dropped);
    return { added: added.filter((row) => !gone.has(row)), removed: dropped.filter((row) => !fresh.has(row)).map((row) => row.id) };
  }
}

export function tailLine(row, { showCost = true } = {}) {
  const payload = row.payload || {};
  const logged = payload.message ?? payload.line;
  if (row.kind === 'log' && logged !== undefined && logged !== null) {
    const text = String(logged);
    return { tone: 'muted', label: payload.stream ? String(payload.stream) : 'log',
      text: text.length > LOG_LINE_MAX ? `${text.slice(0, LOG_LINE_MAX - 1)}…` : text, mono: true };
  }
  if (row.kind === 'text') return { tone: 'text', label: 'Text', text: oneLine(payload.text, 400), mono: false };
  if (row.kind === 'tool_call') return { tone: 'muted', label: payload.tool || 'Tool', text: summarizeInput(payload.input), mono: false };
  return { ...describe(row, { showCost }), mono: false };
}

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

export function openTail({ runId, run: seed = {}, label, color, title, subject, display, signal: viewSignal, onClose = () => {} }) {
  const controller = new AbortController();
  const signal = controller.signal;
  const stopWithView = () => close();
  viewSignal?.addEventListener('abort', stopWithView);
  let run = { ...seed };
  let steps = [];

  const chipSlot = element('span', {}, statusChip(run.status));
  const elapsedText = element('span', { class: 'mono' });
  const liveNote = element('span', { class: 'muted' }, 'Connecting…');
  const closeButton = element('button', { type: 'button', class: 'tail-close' }, 'Close');
  const progressText = element('p', { class: 'run-progress' });
  const list = element('div', { class: 'tail-list' });
  const box = element('div', { class: 'tail-box', tabindex: 0, role: 'region', 'aria-label': 'Newest events' }, list);
  const beatLine = element('p', { class: 'tail-beat', hidden: true });
  const jumpButton = element('button', { type: 'button', class: 'jump', hidden: true }, 'Jump to latest');
  const announcer = element('div', { class: 'visually-hidden', role: 'status' });
  const node = element('section', { class: 'tail-panel', id: 'runs-tail', role: 'region', 'aria-label': `Log of ${label}`, tabindex: -1 },
    element('div', { class: 'tail-head' },
      element('h2', { class: 'tail-title' }, element('span', { class: `swatch sw-${color}`, 'aria-hidden': 'true' }), label), closeButton),
    element('div', { class: 'tail-meta' }, element('span', { class: 'tail-name' }, title), subject, chipSlot, elapsedText, liveNote,
      link(hashFor('run', [runId]), 'Open full run')),
    progressText, element('div', { class: 'tail-frame' }, box, jumpButton), beatLine, announcer);

  let shownStatus = run.status;
  const drawHeader = () => {
    if (run.status !== shownStatus) {
      shownStatus = run.status;
      clear(chipSlot).append(statusChip(run.status));
    }
    const isOpen = !TERMINAL.has(run.status);
    setText(elapsedText, duration(elapsedSeconds(run.started_at, run.finished_at || (isOpen ? null : run.started_at))));
  };
  const syncProgress = () => setText(progressText, progress(steps).text);
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
    return element('div', { class: `tail-row tone-${line.tone}${line.mono ? ' tail-log' : ''}`, title: row.ts },
      row.step_key ? element('span', { class: 'tail-step' }, row.step_key) : null,
      element('span', { class: 'feed-label' }, line.label), ' ', element('span', { class: 'tail-text' }, line.text));
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
    beatLine.hidden = !model.beat;
    if (model.beat) setText(beatLine, stillRunning(model.beat));
    if (following) toBottom();
    jumpButton.hidden = following;
  };

  let settled = false;
  let stream = null;
  let poller = null;
  let settleTimer = 0;
  const ticker = setInterval(drawHeader, 1000);
  const settleSoon = () => {
    if (settled) return;
    settled = true;
    settleTimer = setTimeout(() => {
      stream?.close();
      poller?.stop();
      clearInterval(ticker);
      setLive('ended');
      drawHeader();
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
      const lastSeq = run.last_seq === undefined || run.last_seq === null ? NaN : Number(run.last_seq);
      if (Number.isFinite(lastSeq)) {
        const events = listOf(await api(beforePath(runId, newestBefore(lastSeq), TAIL_LOAD)), 'events');
        if (signal.aborted) return;
        sideEffects(events, { live: false });
        draw(model.append(events));
      }
      loading = false;
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
    document.removeEventListener('keydown', onKey);
    clearInterval(ticker);
    clearTimeout(announceTimer);
    clearTimeout(settleTimer);
    cancelAnimationFrame(frame);
    node.remove();
    if (!viewSignal?.aborted) onClose();
  }
  function onKey(keyEvent) {
    if (keyEvent.key !== 'Escape' || keyEvent.defaultPrevented) return;
    if (node.contains(document.activeElement) || document.activeElement === document.body) close();
  }
  closeButton.addEventListener('click', close);
  document.addEventListener('keydown', onKey);
  start();

  return {
    node,
    runId: String(runId),
    close,
    focus: () => node.focus(),
    update: (summary) => {
      run = { ...run, ...summary };
      drawHeader();
      if (TERMINAL.has(run.status)) settleSoon();
    },
  };
}
