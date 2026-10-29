import { actionsBar } from '../actions.js';
import { api, listOf } from '../api.js';
import { append, card, clear, duration, element, empty, errorBox, link, loading, money, pageHeader, statusChip, tabs, when } from '../dom.js';
import { loadDisplay, workflowLabel } from '../display.js';
import { applyStepEvent, branchText, describe, earlierPage, elapsedSeconds, FeedModel, groupItems, newestBefore, oneLine, stepKey, stepTree, summarizeInput, TERMINAL } from '../feed.js';
import { every, subscribe } from '../live.js';
import { renderMarkdown } from '../markdown.js';
import { hashFor } from '../router.js';
import { activeTab, artifactForTab, byteSize, downloadType, LOG_TAB, orderArtifacts, OUTPUT_TAB, outputText, prettyJson, progress, runTabs, summaryLinks } from '../runpage.js';
import { subjectLink, verdictChip } from './runs.js';

const PAGE = 1000;
const EARLIER_PAGE = 500;
const POLL_MS = 2000;
const REFRESH_MS = 1500;
const SETTLE_MS = 1500;
const ANNOUNCE_MS = 2000;
const FINDINGS_PAGE = 200;
const BOTTOM_SLACK = 24;
const LIVE_STATES = { live: 'Live', reconnecting: 'Reconnecting…', polling: 'Refreshing every 2 s', ended: 'Finished' };
const STEP_WORDS = { step_start: 'started', step_end: 'finished', step_failed: 'failed', step_skipped: 'skipped', retry: 'retrying' };

const runPath = (runId) => `/api/runs/${encodeURIComponent(runId)}`;
const eventsPath = (runId, after, limit) => `/api/runs/${encodeURIComponent(runId)}/events?after=${after}&limit=${limit}`;
const findingsPath = (runId, limit) => `/api/runs/${encodeURIComponent(runId)}/findings?limit=${limit}`;
const beforePath = (runId, before, limit) => `/api/runs/${encodeURIComponent(runId)}/events?before=${before}&limit=${limit}`;
const streamPath = (runId, after) => (after === null ? `/api/runs/${encodeURIComponent(runId)}/stream` : `/api/runs/${encodeURIComponent(runId)}/stream?after=${after}`);
const artifactsPath = (runId) => `/api/runs/${encodeURIComponent(runId)}/artifacts`;
const artifactPath = (runId, name) => `/api/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(name)}`;
const outputsPath = (runId) => `/api/runs/${encodeURIComponent(runId)}/outputs`;

const tokens = (value) => (Number.isFinite(Number(value)) ? Number(value).toLocaleString('en-US') : '—');

function labelled(label, valueNode) {
  return element('span', {}, element('span', { class: 'meta-label' }, `${label} `), valueNode);
}

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

function toolNode(item) {
  const call = item.payload;
  const result = item.result;
  const state = !result ? element('span', { class: 'tool-state muted' }, 'running')
    : result.is_error ? element('span', { class: 'tool-state bad' }, 'error') : element('span', { class: 'tool-state ok' }, 'done');
  const details = element('details', { class: `feed-item feed-tool${result?.is_error ? ' tone-bad' : ''}`, title: item.ts },
    element('summary', {}, element('span', { class: 'feed-label' }, call.tool || 'Tool'), ' ', element('span', { class: 'tool-input' }, summarizeInput(call.input)), ' ', state));
  details.addEventListener('toggle', () => {
    if (details.open && details.childElementCount === 1) fillTool(details, item);
  });
  return details;
}

function fillTool(details, item) {
  const call = item.payload;
  const result = item.result;
  details.append(element('div', { class: 'tool-body' },
    element('div', { class: 'tool-head' }, call.truncated ? 'Input (truncated)' : 'Input'),
    element('pre', {}, String(call.input ?? '')),
    result ? [
      element('div', { class: `tool-head${result.is_error ? ' bad' : ''}` }, `${result.is_error ? 'Error' : 'Result'}${result.truncated ? ' (truncated)' : ''}`),
      element('pre', {}, String(result.content ?? '')),
    ] : element('div', { class: 'tool-head muted' }, 'No result yet.')));
}

function itemNode(item, showCost) {
  if (item.kind === 'tool_call') return toolNode(item);
  if (item.kind === 'text') {
    return element('div', { class: 'feed-item feed-text', title: item.ts }, String(item.payload.text ?? ''),
      item.payload.truncated ? element('span', { class: 'muted' }, ' (truncated)') : null);
  }
  const { tone, label, text } = describe(item, { showCost });
  return element('div', { class: `feed-item feed-line tone-${tone}`, title: item.ts },
    element('span', { class: 'feed-label' }, label), ' ', element('span', {}, text));
}

function findingNode(finding) {
  const where = finding.path ? `${finding.path}${finding.line ? `:${finding.line}` : ''}` : '';
  return element('li', {},
    element('div', {}, element('span', { class: `chip ${finding.severity === 'high' || finding.severity === 'critical' ? 'bad' : 'warn'}` }, String(finding.severity || 'finding')), oneLine(finding.title || 'Finding', 200)),
    where ? element('code', { class: 'wrap-anywhere' }, where) : null,
    finding.body ? element('p', { class: 'finding-body' }, oneLine(finding.body, 400)) : null);
}

function artifactBody(artifact) {
  if (artifact.media_type === 'text/markdown') return renderMarkdown(artifact.content);
  const text = artifact.media_type === 'application/json' ? prettyJson(artifact.content) : artifact.content;
  return element('pre', { class: 'artifact-text' }, text);
}

// One artifact with its copy and download buttons. ``load`` fetches it again, as when a newer one replaces it.
function artifactView(runId, name, signal) {
  const body = element('div', { class: 'artifact-body' }, loading());
  const details = element('span', { class: 'muted artifact-meta' });
  const status = element('span', { class: 'muted', role: 'status' });
  const copyButton = element('button', { type: 'button', disabled: true }, 'Copy');
  const downloadButton = element('button', { type: 'button', disabled: true }, 'Download');
  let current = null;
  copyButton.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(current.content);
      setText(status, 'Copied.');
    } catch {
      setText(status, 'Could not copy. Use Download instead.');
    }
  });
  downloadButton.addEventListener('click', () => {
    const href = URL.createObjectURL(new Blob([current.content], { type: downloadType(current.media_type) }));
    const anchor = element('a', { download: name, hidden: true });
    anchor.href = href;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(href), 1000);
  });
  const node = card(element('div', { class: 'artifact-bar' }, element('span', { class: 'artifact-name mono' }, name), details,
    element('span', { class: 'artifact-actions' }, copyButton, downloadButton, status)), body);
  const load = async () => {
    try {
      const { artifact } = await api(artifactPath(runId, name));
      if (signal?.aborted) return;
      current = artifact;
      copyButton.disabled = false;
      downloadButton.disabled = false;
      setText(details, [artifact.media_type, byteSize(artifact.size), `saved ${when(artifact.created_at)}`, artifact.step_id ? `by step ${artifact.step_id}` : null].filter(Boolean).join(' · '));
      clear(body).append(artifactBody(artifact));
    } catch (err) {
      clear(body).append(errorBox(err));
    }
  };
  return { node, load };
}

// Every step in run order with its children under it. Failed steps start open, and output is drawn on first open.
function outputView(runId, signal, showCost) {
  const list = element('div', { class: 'output-list' });
  const findingsSlot = element('div', {});
  const opened = new Set();
  let outputs = {};
  let steps = [];
  const fill = (details, step, key) => {
    const { text, truncated } = outputText(outputs[key]);
    details.append(element('div', { class: 'output-body' },
      step.error ? element('div', { class: 'banner bad', role: 'alert' }, String(step.error)) : null,
      Object.hasOwn(outputs, key) ? [element('pre', { class: 'output-text' }, text), truncated ? element('p', { class: 'note' }, 'This output was cut short before it was stored.') : null]
        : element('p', { class: 'muted' }, 'No output recorded.')));
  };
  const draw = () => {
    clear(list);
    const ordered = stepTree(steps);
    if (!ordered.length) list.append(empty('No steps yet.'));
    for (const { step, key, depth } of ordered) {
      const failed = step.status === 'failed';
      const details = element('details', { class: `output-step depth-${Math.min(depth, 4)}${failed ? ' tone-bad' : ''}`, open: failed || opened.has(key) },
        element('summary', {}, statusChip(step.status), ' ', element('span', { class: 'step-name' }, step.step_id),
          element('span', { class: 'step-num' }, duration(elapsedSeconds(step.started_at, step.finished_at))),
          !showCost || step.cost_usd === null || step.cost_usd === undefined ? null : element('span', { class: 'step-num' }, money(step.cost_usd, 4)),
          branchText(step) ? element('span', { class: 'step-branches' }, branchText(step)) : null));
      if (details.open) fill(details, step, key);
      details.addEventListener('toggle', () => {
        if (details.open) opened.add(key);
        else opened.delete(key);
        if (details.open && details.childElementCount === 1) fill(details, step, key);
      });
      list.append(details);
    }
  };
  const refresh = async (freshSteps) => {
    steps = freshSteps;
    try {
      outputs = (await api(outputsPath(runId)))?.outputs || {};
    } catch (err) {
      clear(list).append(errorBox(err));
      return;
    }
    if (!signal?.aborted) draw();
  };
  const showFindings = async () => {
    try {
      const findings = listOf(await api(findingsPath(runId, FINDINGS_PAGE)), 'findings');
      if (!findings.length || signal?.aborted) return;
      clear(findingsSlot).append(card(element('h2', { class: 'inline' }, 'Findings'),
        element('ul', { class: 'finding-list', role: 'list' }, findings.map(findingNode)),
        findings.length >= FINDINGS_PAGE ? element('p', { class: 'note' }, `Showing the newest ${FINDINGS_PAGE}.`) : null));
    } catch {}
  };
  return { node: element('div', {}, card(element('h2', { class: 'visually-hidden' }, 'Steps and their output'), list), findingsSlot), refresh, showFindings };
}

export async function render(root, { args, signal }) {
  const runId = args[0];
  if (!runId) {
    root.append(pageHeader('Run'), empty('No run given.'));
    return;
  }
  let detail;
  let artifactList;
  let display;
  try {
    [detail, artifactList, display] = await Promise.all([api(runPath(runId)), api(artifactsPath(runId)).catch(() => ({ artifacts: [] })), loadDisplay()]);
  } catch (err) {
    root.append(pageHeader('Run'), errorBox(err));
    return;
  }
  let run = detail?.run || {};
  let steps = Array.isArray(detail?.steps) ? detail.steps.map((step) => ({ ...step })) : [];
  let names = orderArtifacts(listOf(artifactList, 'artifacts').map((artifact) => artifact.name));
  const active = activeTab(args[1], names);

  // Header
  const workflowName = workflowLabel(display, run.workflow);
  const titleText = element('span', {}, run.summary?.title || workflowName || 'Run');
  const chipSlot = element('span', {}, statusChip(run.status));
  const verdictSlot = element('span', {});
  const liveNote = element('span', { class: 'live-state muted', role: 'status' }, 'Connecting…');
  const projectText = element('span', {}, '—');
  const durationText = element('span', { class: 'mono' });
  const costText = element('span', { class: 'mono' });
  const tokenText = element('span', { class: 'mono' });
  const extraMeta = element('span', {});
  const fieldsSlot = element('span', { class: 'run-fields' });
  const linksSlot = element('div', { class: 'run-links' });
  const progressText = element('p', { class: 'run-progress' });
  const errorSlot = element('div', {});
  const actions = actionsBar(runId);
  const heading = pageHeader('', chipSlot, verdictSlot, run.dry_run ? element('span', { class: 'chip warn', title: 'Recorded outputs stood in for every step.' }, 'Dry run') : null, liveNote);
  heading.querySelector('h1').append(titleText);
  root.append(element('p', { class: 'crumb' }, link(hashFor('runs'), 'Runs')), heading,
    element('div', { class: 'meta' },
      run.subject ? labelled('Subject', subjectLink(run.subject)) : null,
      run.summary?.title ? labelled('Workflow', element('span', {}, workflowName)) : null,
      labelled('Project', projectText),
      labelled('Triggered by', element('span', {}, [run.triggered_by || '—', run.trigger_kind ? ` (${run.trigger_kind})` : ''].join(''))),
      labelled('Started', element('span', { title: run.started_at || null }, when(run.started_at))),
      labelled('Duration', durationText), display.show_cost ? labelled('Cost', costText) : null, labelled('Tokens', tokenText), fieldsSlot, extraMeta),
    linksSlot, actions.node, progressText, errorSlot);
  actions.refresh();
  if (run.team_id) {
    api(`/api/teams/${encodeURIComponent(run.team_id)}/projects`).then((payload) => {
      const project = listOf(payload, 'projects').find((candidate) => String(candidate.id) === String(run.project_id));
      if (project) setText(projectText, project.name);
    }).catch(() => {});
  }

  let shownStatus = run.status;
  let shownSummary = null;
  const drawSummary = () => {
    const summary = run.summary && typeof run.summary === 'object' ? run.summary : {};
    const key = JSON.stringify(summary);
    if (key === shownSummary) return;
    shownSummary = key;
    setText(titleText, summary.title || workflowName || 'Run');
    append(clear(verdictSlot), [verdictChip(summary.verdict)]);
    const fields = summary.fields && typeof summary.fields === 'object' ? Object.entries(summary.fields) : [];
    append(clear(fieldsSlot), fields.map(([name, value]) => labelled(name, element('span', {}, String(value)))));
    append(clear(linksSlot), summaryLinks(summary).map(({ label, url }) => {
      const anchor = element('a', { rel: 'noopener noreferrer', target: url.startsWith('/') ? null : '_blank' }, label);
      // dom.js keeps to https and hash links, and summaryLinks has already vetted this one.
      anchor.setAttribute('href', url);
      return anchor;
    }));
  };
  const updateHeader = () => {
    if (run.status !== shownStatus) {
      shownStatus = run.status;
      clear(chipSlot).append(statusChip(run.status));
      actions.refresh();
    }
    drawSummary();
    const isOpen = !TERMINAL.has(run.status);
    setText(durationText, duration(elapsedSeconds(run.started_at, run.finished_at || (isOpen ? null : run.started_at))));
    setText(costText, money(run.cost_usd, 4));
    setText(tokenText, `${tokens(run.input_tokens)} in · ${tokens(run.output_tokens)} out`);
    clear(extraMeta);
    if (run.rerun_of) extraMeta.append(link(hashFor('run', [run.rerun_of]), 'Rerun of an earlier run'));
    if (run.cancelled_by) extraMeta.append(` Canceled by ${run.cancelled_by}`);
    const errorText = run.error ? oneLine(run.error, 600) : '';
    if (errorSlot.textContent !== errorText) clear(errorSlot).append(errorText ? element('div', { class: 'banner bad', role: 'alert' }, errorText) : null);
  };
  const syncProgress = () => setText(progressText, progress(steps).text);
  updateHeader();
  syncProgress();

  // Tabs
  const baseHash = hashFor('run', [runId]);
  let tabRow = tabs(baseHash, runTabs(names), active, { label: 'Run sections' });
  const panel = tabRow.panel;
  root.append(tabRow.tablist, panel);
  const activeArtifact = artifactForTab(active, names);
  const artifactTab = activeArtifact ? artifactView(runId, activeArtifact, signal) : null;
  const output = active === OUTPUT_TAB ? outputView(runId, signal, display.show_cost) : null;
  const onArtifact = (payload) => {
    if (typeof payload?.name !== 'string') return;
    if (payload.name === activeArtifact) artifactTab.load();
    if (names.includes(payload.name)) return;
    names = orderArtifacts([...names, payload.name]);
    const fresh = tabs(baseHash, runTabs(names), active, { label: 'Run sections' });
    tabRow.tablist.replaceWith(fresh.tablist);
    tabRow = { tablist: fresh.tablist, panel };
  };
  let outputTimer = 0;
  const outputSoon = () => {
    if (output && !outputTimer) {
      outputTimer = setTimeout(() => {
        outputTimer = 0;
        output.refresh(steps);
      }, REFRESH_MS);
    }
  };

  // Log
  const announcer = element('div', { class: 'visually-hidden', role: 'log', 'aria-live': 'polite' });
  const earlierButton = element('button', { type: 'button', class: 'link-button' }, 'Load earlier');
  const earlierRow = element('div', { class: 'feed-earlier', hidden: true }, earlierButton);
  const feedList = element('div', { class: 'feed-list' });
  const feedBox = element('div', { class: 'feed', tabindex: 0, role: 'region', 'aria-label': 'Run log' }, earlierRow, feedList);
  const jumpButton = element('button', { type: 'button', class: 'jump', hidden: true }, 'Jump to latest');

  if (artifactTab) {
    panel.append(artifactTab.node);
    artifactTab.load();
  } else if (output) {
    panel.append(output.node);
    output.refresh(steps);
    if (!names.length) output.showFindings();
  } else {
    panel.append(card(element('div', { class: 'feed-frame' }, feedBox, jumpButton), announcer));
  }

  const model = new FeedModel();
  const nodes = new Map();
  let following = true;
  const isAtBottom = () => feedBox.scrollHeight - feedBox.scrollTop - feedBox.clientHeight <= BOTTOM_SLACK;
  const toBottom = () => {
    feedBox.scrollTop = feedBox.scrollHeight;
  };
  const updateChrome = () => {
    earlierRow.hidden = !model.hasEarlier;
    jumpButton.hidden = following && !model.detached;
    setText(jumpButton, model.detached && model.missed ? `Jump to latest (${model.missed.toLocaleString('en-US')} new)` : 'Jump to latest');
  };
  feedBox.addEventListener('scroll', () => {
    following = isAtBottom() && !model.detached;
    updateChrome();
  });

  const groupNode = (stepId) => element('section', { class: 'feed-group', 'data-step': stepId ?? '' }, element('h3', { class: 'feed-step' }, stepId || 'Run'));
  const place = (item, node) => {
    const last = feedList.lastElementChild;
    const group = last && last.dataset.step === (item.step_key ?? '') ? last : feedList.appendChild(groupNode(item.step_key));
    group.append(node);
    nodes.set(item.id, node);
  };
  const renderAll = () => {
    clear(feedList);
    nodes.clear();
    const fragment = document.createDocumentFragment();
    for (const group of groupItems(model.items)) {
      const section = groupNode(group.step_key);
      for (const item of group.items) {
        const node = itemNode(item, display.show_cost);
        section.append(node);
        nodes.set(item.id, node);
      }
      fragment.append(section);
    }
    feedList.append(fragment);
    updateChrome();
  };
  const applyChanges = ({ added, updated, removed }) => {
    if (removed.length) {
      const before = feedList.scrollHeight;
      for (const id of removed) {
        const node = nodes.get(id);
        nodes.delete(id);
        const group = node?.parentElement;
        node?.remove();
        if (group && group.childElementCount <= 1) group.remove();
      }
      if (!following) feedBox.scrollTop -= before - feedList.scrollHeight;
    }
    for (const item of updated) {
      const old = nodes.get(item.id);
      if (!old) continue;
      const fresh = itemNode(item, display.show_cost);
      if (old.open) {
        fresh.open = true;
        fillTool(fresh, item);
      }
      const hadFocus = old.contains(document.activeElement);
      old.replaceWith(fresh);
      nodes.set(item.id, fresh);
      if (hadFocus) fresh.querySelector('summary')?.focus();
    }
    for (const item of added) place(item, itemNode(item, display.show_cost));
  };

  // Screen readers hear step and run changes at most every ANNOUNCE_MS, never each event.
  let announceTimer = 0;
  let pendingAnnouncements = [];
  const announce = (text) => {
    if (active !== LOG_TAB) return;
    pendingAnnouncements.push(text);
    if (announceTimer) return;
    announceTimer = setTimeout(() => {
      announceTimer = 0;
      announcer.append(element('p', {}, pendingAnnouncements.slice(-3).join(' ')));
      pendingAnnouncements = [];
      while (announcer.childElementCount > 5) announcer.firstElementChild.remove();
    }, ANNOUNCE_MS);
  };

  let refreshTimer = 0;
  let lastRefresh = 0;
  let settleSoon = () => {};
  const refresh = async () => {
    refreshTimer = 0;
    lastRefresh = Date.now();
    try {
      const fresh = await api(runPath(runId));
      if (signal?.aborted) return;
      if (fresh?.run) run = { ...run, ...fresh.run };
      if (Array.isArray(fresh?.steps)) {
        const known = new Map(steps.map((step) => [stepKey(step), step]));
        steps = fresh.steps.map((step) => ({ branch_total: known.get(stepKey(step))?.branch_total, branches_done: known.get(stepKey(step))?.branches_done, ...step }));
      }
      updateHeader();
      syncProgress();
      if (TERMINAL.has(run.status)) settleSoon();
    } catch {}
  };
  const refreshSoon = () => {
    if (!refreshTimer) refreshTimer = setTimeout(refresh, Math.max(0, REFRESH_MS - (Date.now() - lastRefresh)));
  };

  // Only the log replays history. Every tab follows what happens after the page loads.
  const isLog = active === LOG_TAB;
  const lastSeq = run.last_seq === undefined ? NaN : Number(run.last_seq);
  const hasBefore = Number.isFinite(lastSeq);
  let sideSeq = !isLog && hasBefore ? lastSeq : 0;
  const sideEffects = (events, { live }) => {
    let stepsChanged = false;
    for (const event of events) {
      const seq = Number(event?.seq);
      if (!Number.isFinite(seq) || seq <= sideSeq) continue;
      sideSeq = seq;
      const payload = event.payload || {};
      if (applyStepEvent(steps, event)) {
        stepsChanged = true;
        if (live) announce(`Step ${event.step_id} ${STEP_WORDS[event.kind]}.`);
      }
      if (live && event.kind === 'artifact') onArtifact(payload);
      if (event.kind === 'run_end' && payload.status) {
        run = { ...run, status: payload.status, error: payload.error ?? run.error, finished_at: run.finished_at || event.ts };
        if (live) announce(`Run ${payload.status}.`);
        updateHeader();
      }
      if (live && ['step_end', 'step_failed', 'cost', 'run_end'].includes(event.kind)) refreshSoon();
      if (live && ['step_start', 'step_end', 'step_failed', 'step_skipped'].includes(event.kind)) outputSoon();
    }
    if (stepsChanged) syncProgress();
  };

  let phase = isLog ? 'loading' : 'live';
  let queue = [];
  let frame = 0;
  const flush = () => {
    frame = 0;
    if (phase !== 'live' || !queue.length) return;
    const batch = queue.sort((left, right) => left.seq - right.seq);
    queue = [];
    sideEffects(batch, { live: true });
    applyChanges(model.append(batch));
    if (following) toBottom();
    updateChrome();
  };
  let streamSeq = 0;
  const seenSeq = () => Math.max(sideSeq, streamSeq);
  const receive = (events) => {
    for (const event of events) streamSeq = Math.max(streamSeq, Number(event?.seq) || 0);
    if (!isLog) {
      sideEffects(events, { live: true });
      return;
    }
    queue.push(...events);
    if (phase === 'live' && !frame) frame = requestAnimationFrame(flush);
  };

  const fetchAfter = async (after, sink) => {
    for (;;) {
      const page = await api(eventsPath(runId, after, PAGE));
      if (signal?.aborted) return;
      const events = listOf(page, 'events');
      sink(events);
      const next = page?.next_after ?? events.at(-1)?.seq;
      if (events.length < PAGE || next === null || next === undefined || Number(next) <= after) return;
      after = Number(next);
    }
  };
  let catching = null;
  const catchUp = () => {
    catching ||= fetchAfter(seenSeq(), receive).catch(() => {}).finally(() => { catching = null; });
    return catching;
  };

  let stream = null;
  let poller = null;
  let settled = false;
  const setLive = (state) => setText(liveNote, LIVE_STATES[state] || '');
  const ticker = setInterval(updateHeader, 1000);
  settleSoon = () => {
    if (settled) return;
    settled = true;
    setTimeout(async () => {
      await catchUp();
      stream?.close();
      poller?.stop();
      clearInterval(ticker);
      setLive('ended');
      updateHeader();
      outputSoon();
    }, SETTLE_MS);
  };
  signal?.addEventListener('abort', () => {
    clearInterval(ticker);
    clearTimeout(refreshTimer);
    clearTimeout(announceTimer);
    clearTimeout(outputTimer);
    cancelAnimationFrame(frame);
    stream?.close();
    poller?.stop();
  });

  const startPolling = () => {
    poller = every(POLL_MS, async () => {
      await catchUp();
      await refresh();
      return !settled;
    }, signal);
  };
  // The server sends `reconnect` and closes when this page falls behind, so the stream reopens from the last seq seen.
  const openStream = (after) => {
    stream?.close();
    stream = subscribe(streamPath(runId, after), {
      events: {
        'run-event': (event) => receive([event]),
        run: (summary) => {
          run = { ...run, ...summary };
          updateHeader();
          if (TERMINAL.has(run.status)) settleSoon();
        },
        reconnect: () => {
          if (!settled) openStream(seenSeq());
        },
      },
      onState: setLive,
      onOpen: () => {
        if (phase === 'live') catchUp();
      },
      onFallback: startPolling,
    }, { signal });
  };

  const finishLoad = () => {
    phase = 'live';
    renderAll();
    following = true;
    requestAnimationFrame(() => {
      toBottom();
      updateChrome();
    });
    if (queue.length && !frame) frame = requestAnimationFrame(flush);
  };
  const loadTail = async (after) => {
    phase = 'loading';
    await fetchAfter(after, (events) => {
      sideEffects(events, { live: false });
      model.append(events);
    });
    finishLoad();
  };
  const loadNewest = async (before) => {
    phase = 'loading';
    const page = await api(beforePath(runId, before, PAGE));
    const events = listOf(page, 'events');
    sideEffects(events, { live: false });
    model.append(events);
    model.noteStart(page, events);
    finishLoad();
  };
  if (isLog) {
    try {
      if (hasBefore) await loadNewest(newestBefore(lastSeq));
      else await loadTail(0);
    } catch (err) {
      feedList.append(errorBox(err));
      phase = 'live';
    }
  }
  if (signal?.aborted) return;
  if (TERMINAL.has(run.status)) {
    settled = true;
    clearInterval(ticker);
    setLive('ended');
  } else {
    openStream(hasBefore ? Math.max(lastSeq, seenSeq()) : null);
  }

  earlierButton.addEventListener('click', async () => {
    const { after, limit } = earlierPage(model.firstSeq, EARLIER_PAGE);
    if (!hasBefore && !limit) return;
    earlierButton.disabled = true;
    try {
      const page = await api(hasBefore ? beforePath(runId, model.firstSeq, EARLIER_PAGE) : eventsPath(runId, after, limit));
      const events = listOf(page, 'events');
      const before = feedList.scrollHeight;
      const top = feedBox.scrollTop;
      model.prepend(events);
      model.noteStart(page, events);
      renderAll();
      feedBox.scrollTop = top + feedList.scrollHeight - before;
      if (model.detached) following = false;
      updateChrome();
    } catch (err) {
      earlierRow.append(errorBox(err));
    } finally {
      earlierButton.disabled = false;
    }
  });
  jumpButton.addEventListener('click', async () => {
    if (model.detached) {
      model.reset();
      await (hasBefore ? loadNewest(newestBefore(seenSeq())) : loadTail(Math.max(0, sideSeq - PAGE))).catch(() => {});
    }
    following = true;
    toBottom();
    updateChrome();
    feedBox.focus();
  });
}
