import { actionsBar } from '../actions.js';
import { backfill, feedSlot, nowText, rosterLine, RunActivity, stageTrack } from '../activity.js';
import { api, listOf } from '../api.js';
import { append, attempt, card, clear, confirmDialog, duration, element, empty, errorBox, link, loading, money, pageHeader, statusChip, tabs, when } from '../dom.js';
import { loadDisplay, workflowLabel } from '../display.js';
import { applyStepEvent, branchText, describe, earlierPage, elapsedSeconds, FeedModel, groupItems, newestBefore, oneLine, stepKey, stepTree, summarizeInput, TERMINAL } from '../feed.js';
import { every, subscribe } from '../live.js';
import { renderMarkdown } from '../markdown.js';
import { decisionSummary, externalLink, externalLinks, outcomeSentence, riskBadge } from '../outcome.js';
import { findingCard, resultView } from '../result.js';
import { hashFor } from '../router.js';
import { activeTab, artifactForTab, byteSize, canCancel, canRerun, downloadType, LOG_TAB, orderArtifacts, OUTPUT_TAB, outputText, prettyJson, progress, rerunNote, rerunPending, runTabs, settledJob } from '../runpage.js';
import { currentSession } from '../session.js';
import { gateCard } from './gates.js';
import { isNewer, locationChip, runStatus, subjectHref } from './runs.js';
import { subjectPath } from './subject.js';

const PAGE = 1000;
const EARLIER_PAGE = 500;
const POLL_MS = 2000;
const RERUN_POLL_MS = 5000;
const RERUN_SETTLE_TRIES = 12;
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

function reviewDisclosure(review) {
  const details = element('details', { class: 'feed-review' }, element('summary', {}, 'Review'));
  details.addEventListener('toggle', () => {
    if (details.open && details.childElementCount === 1) details.append(element('div', { class: 'feed-review-body' }, renderMarkdown(review)));
  });
  return details;
}

function itemNode(item, showCost) {
  if (item.kind === 'tool_call') return toolNode(item);
  if (item.kind === 'finding') return element('div', { class: 'feed-item', title: item.ts }, findingCard(item.payload));
  if (item.kind === 'text') {
    return element('div', { class: 'feed-item feed-text', title: item.ts }, String(item.payload.text ?? ''),
      item.payload.truncated ? element('span', { class: 'muted' }, ' (truncated)') : null);
  }
  const { tone, label, text } = describe(item, { showCost });
  const line = element('div', { class: `feed-item feed-line tone-${tone}`, title: item.ts },
    element('span', { class: 'feed-label' }, label), ' ', element('span', {}, text));
  const review = item.kind === 'log' && typeof item.payload.review === 'string' && item.payload.review ? item.payload.review : null;
  return review ? element('div', { class: 'feed-item' }, line, reviewDisclosure(review)) : line;
}

function rosterNode(entry, now, reviews) {
  const line = rosterLine(entry, now);
  const key = `${entry.step_key}\u0000${entry.role}`;
  let review = null;
  if (entry.review) {
    review = element('details', { class: 'feed-review', 'data-key': key }, element('summary', {}, 'Review'));
    if (reviews.open.has(key)) {
      review.open = true;
      review.append(reviews.body(key, entry.review));
    }
    review.addEventListener('toggle', () => {
      if (review.open) reviews.open.add(key);
      else reviews.open.delete(key);
      if (review.open && review.childElementCount === 1) review.append(reviews.body(key, entry.review));
    });
  }
  return element('li', { class: `roster-row roster-${line.state}` },
    element('span', { class: 'roster-icon', 'aria-hidden': 'true' }, line.icon),
    element('span', { class: 'roster-role' }, entry.role),
    element('span', { class: 'roster-state' }, line.state === 'running' ? 'working' : line.label),
    line.detail ? element('span', { class: 'roster-detail' }, line.detail) : null,
    review);
}

// Where the run is: its steps and who is working on each, then the result once the lead decides.
// After the run ends with a decision, the result alone stands, since it says everything the roster did.
function whereView() {
  const node = element('section', { class: 'where', 'aria-label': 'Progress and result', hidden: true });
  const progressNode = element('div', { class: 'where-progress' });
  const result = resultView({ level: 2 });
  node.append(result.node, progressNode);
  const cache = new Map();
  const reviews = {
    open: new Set(),
    body: (key, text) => {
      if (!cache.has(key)) cache.set(key, element('div', { class: 'feed-review-body' }, renderMarkdown(text)));
      return cache.get(key);
    },
  };
  const openStages = new Map();
  // A tick skips the redraw while focus is inside, so a keyboard user keeps their place.
  const draw = (steps, activity, { tick = false, now = Date.now(), finished = false } = {}) => {
    if (tick && node.contains(document.activeElement)) return;
    const decided = result.draw(activity, { now });
    const track = decided && finished ? [] : stageTrack(steps, activity, now);
    node.hidden = !track.length && !decided;
    progressNode.hidden = !track.length;
    if (!track.length) return;
    const lastWithRoles = [...track].reverse().find((stage) => stage.roles.length)?.key;
    const anyCurrent = track.some((stage) => stage.current && stage.roles.length);
    append(clear(progressNode), [
      element('ol', { class: 'stage-track', 'aria-label': 'Steps' }, track.map((stage) => element('li', {
        class: `stage stage-${stage.state}${stage.current ? ' current' : ''}`, 'aria-current': stage.current ? 'step' : null, title: stage.error || null },
      element('span', { class: 'stage-icon', 'aria-hidden': 'true' }, stage.icon), ' ', element('span', { class: 'stage-name' }, stage.label),
      element('span', { class: 'visually-hidden' }, ` ${stage.state}`),
      stage.seconds && stage.state !== 'waiting' ? element('span', { class: 'stage-time' }, duration(stage.seconds)) : null))),
      track.filter((stage) => stage.headline).map((stage) => element('p', { class: 'where-headline' }, stage.headline)),
      track.filter((stage) => stage.roles.length > 1 || (stage.roles.length && !stage.headline)).map((stage) => {
        const open = openStages.has(stage.key) ? openStages.get(stage.key) : stage.current || (!anyCurrent && stage.key === lastWithRoles);
        const details = element('details', { class: 'roster', open },
          element('summary', {}, element('span', { class: 'stage-name' }, stage.label), ' ',
            element('span', { class: 'muted' }, [`${stage.done}/${stage.total} done`, stage.failed ? `${stage.failed} failed` : null,
              stage.findings ? `${stage.findings} ${stage.findings === 1 ? 'finding' : 'findings'}` : null].filter(Boolean).join(' · '))),
          element('ul', { class: 'roster-list', role: 'list' }, stage.roles.map((entry) => rosterNode(entry, now, reviews)),
            stage.waiting ? element('li', { class: 'roster-row roster-waiting' }, element('span', { class: 'roster-icon', 'aria-hidden': 'true' }, '…'),
              element('span', { class: 'roster-role' }, `${stage.waiting} more waiting`)) : null));
        details.addEventListener('toggle', () => openStages.set(stage.key, details.open));
        return details;
      })]);
  };
  return { node, draw };
}

function findingNode(finding) {
  const where = finding.path ? `${finding.path}${finding.line ? `:${finding.line}` : ''}` : '';
  return element('li', {},
    element('div', {}, element('span', { class: `chip ${finding.severity === 'high' || finding.severity === 'critical' ? 'bad' : 'warn'}` }, String(finding.severity || 'finding')), oneLine(finding.title || 'Finding', 200)),
    where ? element('code', { class: 'wrap-anywhere' }, where) : null,
    finding.body ? element('p', { class: 'finding-body' }, oneLine(finding.body, 400)) : null);
}

// Where this run sits in its thread, and a pointer to the newest run when a later one replaced it.
async function threadNote(slot, run, signal) {
  const thread = run.thread || run.subject;
  let detail;
  try {
    detail = (await api(subjectPath(thread)))?.subject;
  } catch {
    return;
  }
  const others = (detail?.runs || []).filter((other) => String(other.id) !== String(run.id));
  if (signal?.aborted || !others.length) return;
  const newer = others.filter((other) => isNewer(other, run));
  slot.append(newer.length
    ? element('div', { class: 'banner warn' }, `${newer.length === 1 ? 'A later run' : `${newer.length} later runs`} replaced this one, so its result may be out of date. `,
      link(subjectHref(thread), 'See the latest'))
    : element('p', { class: 'note' }, `This is the latest of ${others.length + 1} runs on `, link(subjectHref(thread), detail.title || thread), '.'));
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

  // Header: what it is, how it came out, where it leads, then the details nobody needs first.
  const workflowName = workflowLabel(display, run.workflow);
  const thread = run.thread || run.subject;
  const titleText = element('span', {}, run.summary?.title || workflowName || 'Run');
  const chipSlot = element('span', {});
  const locationSlot = element('span', {});
  const liveNote = element('span', { class: 'live-state muted', role: 'status' }, 'Connecting…');
  const projectText = element('span', {}, '—');
  const durationText = element('span', { class: 'mono' });
  const costText = element('span', { class: 'mono' });
  const tokenText = element('span', { class: 'mono' });
  const extraMeta = element('span', {});
  const fieldsSlot = element('span', { class: 'run-fields' });
  const linksSlot = element('nav', { class: 'run-links', 'aria-label': 'Links' });
  const sentenceText = element('p', { class: 'run-outcome', hidden: true });
  const progressText = element('p', { class: 'run-progress' });
  const errorSlot = element('div', {});
  const threadSlot = element('div', {});
  const actions = actionsBar(runId);
  let job = detail?.job || null;
  const cancelSlot = element('div', { class: 'run-actions' });
  const gatesSlot = element('div', { class: 'run-gates' });
  const drawGates = async () => {
    let found;
    try {
      found = listOf(await api(`${runPath(runId)}/gates`), 'gates');
    } catch {
      return;
    }
    if (!signal?.aborted) gatesSlot.replaceChildren(...found.map((gate) => gateCard(gate, drawGates)));
  };
  const drawCancel = () => {
    clear(cancelSlot);
    if (TERMINAL.has(run.status)) return;
    if (job?.cancel_requested_at) {
      cancelSlot.append(element('p', { class: 'muted' }, 'Cancel requested. The run stops when its runner next checks in.'));
      return;
    }
    if (!canCancel(job, run, currentSession())) return;
    const button = element('button', { type: 'button' }, 'Cancel run');
    const status = element('span', { role: 'status' });
    button.addEventListener('click', async () => {
      if (!(await confirmDialog('Cancel this run?', 'Its runner stops it and the run ends as canceled. It cannot be resumed.', 'Cancel run'))) return;
      const answer = await attempt(status, button, () => api(`${runPath(runId)}/cancel`, { method: 'POST', body: {} }));
      if (answer?.job) {
        job = { ...job, ...answer.job };
        drawCancel();
      }
    });
    cancelSlot.append(button, status);
  };
  const rerunSlot = element('div', { class: 'run-actions' });
  let shownRerun = null;
  let rerunPoller = null;
  const followRerun = () => {
    rerunPoller?.stop();
    rerunPoller = every(RERUN_POLL_MS, async () => {
      const fresh = await api(runPath(runId));
      if (fresh?.run) run = { ...run, can_rerun: fresh.run.can_rerun, rerun: fresh.run.rerun };
      drawRerun();
      return rerunPending(run.rerun);
    }, signal);
  };
  // A run ends before its runner lets go of the job, so whether it may be rerun is known only once the job settles.
  const settleRerun = () => {
    let tries = 0;
    every(RERUN_POLL_MS, async () => {
      tries += 1;
      const fresh = await api(runPath(runId));
      if (fresh?.run) run = { ...run, can_rerun: fresh.run.can_rerun, rerun: fresh.run.rerun };
      if (fresh && 'job' in fresh) job = fresh.job;
      drawRerun();
      return !settledJob(job) && tries < RERUN_SETTLE_TRIES;
    }, signal);
  };
  const drawRerun = () => {
    const note = rerunNote(run.rerun);
    const key = JSON.stringify([canRerun(run), note]);
    if (key === shownRerun) return;
    shownRerun = key;
    clear(rerunSlot);
    if (note) rerunSlot.append(note.runId ? link(hashFor('run', [note.runId]), note.text) : element('p', { class: 'muted' }, note.text));
    if (!canRerun(run)) return;
    const button = element('button', { type: 'button' }, 'Rerun');
    const status = element('span', { role: 'status' });
    button.addEventListener('click', async () => {
      if (!(await confirmDialog('Rerun this run?', 'A new run of this workflow starts at its current version, with the same inputs and trigger. This run stays as it is.', 'Rerun'))) return;
      const answer = await attempt(status, button, () => api(`${runPath(runId)}/rerun`, { method: 'POST', body: {} }));
      if (!answer?.job) return;
      run = { ...run, can_rerun: false, rerun: answer.job };
      drawRerun();
      followRerun();
    });
    rerunSlot.append(button, status);
  };
  const heading = pageHeader('', chipSlot, locationSlot, run.dry_run ? element('span', { class: 'chip warn', title: 'Recorded outputs stood in for every step.' }, 'Dry run') : null, liveNote);
  heading.querySelector('h1').append(titleText);
  root.append(element('p', { class: 'crumb' }, link(hashFor('runs'), 'Runs'), thread ? [' / ', link(subjectHref(thread), thread)] : null), heading,
    sentenceText, linksSlot,
    element('div', { class: 'meta' },
      run.summary?.title ? labelled('Workflow', element('span', {}, workflowName)) : null,
      labelled('Started', element('span', { title: run.started_at || null }, when(run.started_at))),
      labelled('Took', durationText), extraMeta),
    threadSlot, actions.node, cancelSlot, rerunSlot, gatesSlot, progressText, errorSlot);
  const where = whereView();
  const activity = new RunActivity();
  let activityReady = false;
  let activityQueue = [];
  let whereFrame = 0;
  const drawWhere = () => {
    if (!whereFrame) {
      whereFrame = requestAnimationFrame(() => {
        whereFrame = 0;
        where.draw(steps, activity, { finished: TERMINAL.has(run.status) });
        drawSentence();
      });
    }
  };
  const feedActivity = (events) => {
    if (!activityReady) activityQueue.push(...events);
    else if (activity.apply(events)) drawWhere();
  };
  root.append(where.node);
  actions.refresh();
  if (rerunPending(run.rerun)) followRerun();
  if (thread) threadNote(threadSlot, run, signal);
  if (run.team_id) {
    api(`/api/teams/${encodeURIComponent(run.team_id)}/projects`).then((payload) => {
      const project = listOf(payload, 'projects').find((candidate) => String(candidate.id) === String(run.project_id));
      if (project) setText(projectText, project.name);
    }).catch(() => {});
  }

  let shownStatus = null;
  let shownSummary = null;
  const runOutcome = () => run.outcome || run.summary?.outcome || null;
  const drawSentence = () => {
    const text = outcomeSentence(runOutcome(), decisionSummary(activity)?.counts);
    sentenceText.hidden = !text;
    setText(sentenceText, text);
  };
  let shownLocation = null;
  const drawSummary = () => {
    const summary = run.summary && typeof run.summary === 'object' ? run.summary : {};
    const key = JSON.stringify(summary);
    if (key === shownSummary) return;
    shownSummary = key;
    setText(titleText, summary.title || workflowName || 'Run');
    const fields = summary.fields && typeof summary.fields === 'object' ? Object.entries(summary.fields) : [];
    append(clear(fieldsSlot), fields.map(([name, value]) => labelled(name, element('span', {}, String(value)))));
    const links = externalLinks({ links: [...(Array.isArray(run.links) ? run.links : []), ...(Array.isArray(summary.links) ? summary.links : [])] });
    linksSlot.hidden = !links.length;
    append(clear(linksSlot), links.map(externalLink));
  };
  const updateHeader = () => {
    const outcome = runStatus({ status: run.status, verdict: run.summary?.verdict, outcome: runOutcome() }, null);
    const risk = riskBadge(run.risk, Array.isArray(run.risk_reasons) ? run.risk_reasons[0]?.text : null);
    if (`${run.status}|${outcome.text}|${risk?.text}|${risk?.title}` !== shownStatus) {
      if (run.status !== shownStatus?.split('|')[0]) {
        actions.refresh();
        drawGates();
      }
      shownStatus = `${run.status}|${outcome.text}|${risk?.text}|${risk?.title}`;
      append(clear(chipSlot), [element('span', { class: `state state-${outcome.tone}` }, outcome.text),
        risk ? element('span', { class: `chip ${risk.tone}`, title: risk.title }, risk.text) : null]);
      drawCancel();
    }
    drawRerun();
    drawSummary();
    drawSentence();
    const placed = JSON.stringify(run.location || null);
    if (placed !== shownLocation) {
      shownLocation = placed;
      append(clear(locationSlot), [locationChip(run.location)]);
    }
    const isOpen = !TERMINAL.has(run.status);
    progressText.hidden = !isOpen;
    setText(durationText, duration(elapsedSeconds(run.started_at, run.finished_at || (isOpen ? null : run.started_at))));
    setText(costText, money(run.cost_usd, 4));
    setText(tokenText, `${tokens(run.input_tokens)} in · ${tokens(run.output_tokens)} out`);
    clear(extraMeta);
    if (run.rerun_of) extraMeta.append(link(hashFor('run', [run.rerun_of]), 'Rerun of an earlier run'));
    if (run.cancelled_by) extraMeta.append(` Canceled by ${run.cancelled_by}`);
    const errorText = run.error ? oneLine(run.error, 600) : '';
    if (errorSlot.textContent !== errorText) clear(errorSlot).append(errorText ? element('div', { class: 'banner bad', role: 'alert' }, errorText) : null);
  };
  const syncProgress = () => {
    setText(progressText, progress(steps).text);
    drawWhere();
  };
  updateHeader();
  drawCancel();
  syncProgress();
  backfill(activity, (after) => api(eventsPath(runId, after, PAGE)), { signal }).catch(() => {}).finally(() => {
    if (signal?.aborted) return;
    activityReady = true;
    activity.apply(activityQueue.sort((left, right) => left.seq - right.seq));
    activityQueue = [];
    drawWhere();
  });

  // Tabs
  const baseHash = hashFor('run', [runId]);
  let tabRow = tabs(baseHash, runTabs(names), active, { label: 'Run sections' });
  const panel = tabRow.panel;
  root.append(tabRow.tablist, panel, element('details', { class: 'card run-details' }, element('summary', {}, 'Run details'),
    element('div', { class: 'meta' },
      labelled('Project', projectText),
      labelled('Triggered by', element('span', {}, [run.triggered_by || '—', run.trigger_kind ? ` (${run.trigger_kind})` : ''].join(''))),
      display.show_cost ? labelled('Cost', costText) : null, labelled('Tokens', tokenText), fieldsSlot)));
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
  // A step whose events carry roles shows its roles' lines, with each role's session activity collapsed and no start or finish rows.
  const roleSteps = new Set();
  const boundaryNodes = new Map();
  const activityBoxes = new WeakMap();
  const markRoleStep = (key) => {
    if (roleSteps.has(key)) return;
    roleSteps.add(key);
    for (const node of boundaryNodes.get(key) || []) node.hidden = true;
    boundaryNodes.delete(key);
  };
  const activityBox = (section, role) => {
    if (!activityBoxes.has(section)) activityBoxes.set(section, new Map());
    const boxes = activityBoxes.get(section);
    if (!boxes.has(role)) {
      const label = element('span', { class: 'feed-label' });
      const latest = element('span', { class: 'feed-activity-now' });
      const body = element('div', { class: 'feed-activity-body' });
      const details = element('details', { class: 'feed-item feed-activity' }, element('summary', {}, label, ' ', latest), body);
      section.append(details);
      boxes.set(role, { label, latest, body, count: 0 });
    }
    return boxes.get(role);
  };
  const putInto = (section, item, node) => {
    const { slot, role } = feedSlot(item);
    if (role) markRoleStep(item.step_key);
    if (slot === 'activity') {
      const box = activityBox(section, role);
      box.body.append(node);
      box.count += 1;
      setText(box.label, `${role} · activity (${box.count.toLocaleString('en-US')})`);
      const latest = nowText(item.kind, item.payload);
      if (latest) setText(box.latest, latest);
    } else {
      section.append(node);
      if (slot === 'boundary' && roleSteps.has(item.step_key)) node.hidden = true;
      else if (slot === 'boundary') boundaryNodes.set(item.step_key, [...(boundaryNodes.get(item.step_key) || []), node]);
    }
    nodes.set(item.id, node);
  };
  const place = (item, node) => {
    const last = feedList.lastElementChild;
    const group = last && last.dataset.step === (item.step_key ?? '') ? last : feedList.appendChild(groupNode(item.step_key));
    putInto(group, item, node);
  };
  const renderAll = () => {
    clear(feedList);
    nodes.clear();
    boundaryNodes.clear();
    const fragment = document.createDocumentFragment();
    for (const group of groupItems(model.items)) {
      const section = groupNode(group.step_key);
      for (const item of group.items) putInto(section, item, itemNode(item, display.show_cost));
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
        const group = node?.closest('.feed-group');
        const box = node?.parentElement?.classList.contains('feed-activity-body') ? node.parentElement : null;
        node?.remove();
        if (box && !box.childElementCount) box.parentElement.remove();
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
    feedActivity(events);
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
  const ticker = setInterval(() => {
    updateHeader();
    where.draw(steps, activity, { tick: true, finished: TERMINAL.has(run.status) });
  }, 1000);
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
      drawWhere();
      outputSoon();
      if (!settledJob(job)) settleRerun();
    }, SETTLE_MS);
  };
  signal?.addEventListener('abort', () => {
    clearInterval(ticker);
    cancelAnimationFrame(whereFrame);
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
    if (!settledJob(job)) settleRerun();
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
