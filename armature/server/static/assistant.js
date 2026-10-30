import { api, ApiError, listOf } from './api.js';
import { askAssistant } from './conversation.js';
import { card, clear, confirmDialog, element, errorBox, loading, money, when } from './dom.js';
import { diffView } from './diff.js';
import { issueList, issueText } from './editor.js';
import { navigate } from './guard.js';
import { hashFor } from './router.js';

export const DEFAULT_ALIAS = 'lead';
export const INSTRUCTION_MAX = 4000;

// An instance object has no team, so the user picks whose provider to use or whose run it is.
export const needsTeam = (record) => !record?.team_id;

export function aliasChoices(providers) {
  const aliases = new Set();
  for (const provider of providers || []) for (const alias of Object.keys(provider?.aliases || {})) aliases.add(alias);
  return [...aliases].sort((first, second) => (first === DEFAULT_ALIAS ? -1 : second === DEFAULT_ALIAS ? 1 : first.localeCompare(second)));
}

export function assistBody({ kind, record, body, instruction, alias, teamId }) {
  const text = String(instruction ?? '').trim();
  if (!text) return { error: 'Say what to change.' };
  if (text.length > INSTRUCTION_MAX) return { error: `An instruction is at most ${INSTRUCTION_MAX} characters.` };
  if (needsTeam(record) && !teamId) return { error: 'Pick the team whose provider to use.' };
  const payload = { kind, id: record?.id, body: String(body ?? ''), instruction: text, alias: alias || DEFAULT_ALIAS };
  if (needsTeam(record)) payload.team_id = teamId;
  return { body: payload };
}

export function resultSummary(result) {
  const validation = result?.validation || {};
  const errors = validation.errors || [];
  const tries = result?.attempts > 1 ? ` after ${result.attempts} tries` : '';
  return {
    cost: result?.cost_usd === undefined ? '' : `Cost ${money(result.cost_usd, 4)}${tries}`,
    valid: Boolean(validation.ok),
    status: validation.ok ? 'The proposal validates.' : `The proposal has ${errors.length} ${errors.length === 1 ? 'error' : 'errors'}. You can still accept it and fix them.`,
  };
}

export function testRunBody({ record, fromRun, inputsText, missingEmpty, teamId }) {
  let inputs = null;
  const text = String(inputsText ?? '').trim();
  if (text) {
    try {
      inputs = JSON.parse(text);
    } catch {
      return { error: 'Inputs must be JSON, such as {"number": 42}.' };
    }
    if (!inputs || typeof inputs !== 'object' || Array.isArray(inputs)) return { error: 'Inputs must be a JSON object.' };
  }
  if (needsTeam(record) && !teamId) return { error: 'Pick the team this test run belongs to.' };
  const body = { mode: 'stub', from_run: fromRun || null, inputs, missing: missingEmpty ? 'empty' : 'fail' };
  if (needsTeam(record)) body.team_id = teamId;
  return { body };
}

export const runChoiceLabel = (run) => `${when(run.started_at)} · ${run.status || 'unknown'} · ${String(run.id).slice(0, 8)}`;

async function teamOptions() {
  const teams = await api('/api/teams').then((payload) => listOf(payload, 'teams')).catch(() => []);
  return teams.map((team) => element('option', { value: String(team.id) }, team.name));
}

function teamPicker(id, label) {
  const select = element('select', { id }, element('option', { value: '' }, 'Pick a team'));
  teamOptions().then((options) => select.append(...options));
  return { select, field: element('label', { class: 'stacked', for: id }, label, select) };
}

function issuesView(validation) {
  const issues = issueList(validation?.errors, validation?.warnings);
  if (!issues.length) return null;
  return element('ul', { class: 'code-issues', role: 'list', 'aria-label': 'Problems in the proposal' },
    issues.map((issue) => element('li', { class: `issue-${issue.severity}` }, issueText(issue))));
}

// The proposal is shown as text and a diff, and goes into the editor only when the user accepts it.
export function assistantPanel({ kind, record, getBody, setBody }) {
  const idPrefix = `assist-${record.id}`;
  const instruction = element('textarea', { id: `${idPrefix}-instruction`, rows: 3, maxlength: INSTRUCTION_MAX, placeholder: 'Add a step that posts the summary to the team channel.' });
  const aliasSelect = element('select', { id: `${idPrefix}-alias` }, element('option', { value: DEFAULT_ALIAS }, DEFAULT_ALIAS));
  const team = needsTeam(record) ? teamPicker(`${idPrefix}-team`, 'Team whose provider to use') : null;
  const askButton = element('button', { type: 'button', class: 'primary' }, 'Ask');
  const result = element('div', { role: 'status' });

  async function loadAliases(teamId) {
    if (!teamId) return;
    try {
      const choices = aliasChoices(listOf(await api(`/api/teams/${encodeURIComponent(teamId)}/providers`), 'providers'));
      const options = (choices.length ? choices : [DEFAULT_ALIAS]).map((alias) => element('option', { value: alias }, alias));
      clear(aliasSelect).append(...options);
    } catch {}
  }
  if (team) team.select.addEventListener('change', () => loadAliases(team.select.value));
  else loadAliases(record.team_id);

  function showResult(proposal, askedBody) {
    const summary = resultSummary(proposal);
    const acceptButton = element('button', { type: 'button', class: 'primary' }, 'Accept into the editor');
    const discardButton = element('button', { type: 'button', onclick: () => clear(result) }, 'Discard');
    acceptButton.addEventListener('click', async () => {
      if (getBody() !== askedBody && !(await confirmDialog('Replace your newer edits?', 'The editor changed after you asked. Accepting replaces the editor with the proposal.', 'Replace'))) return;
      setBody(proposal.body);
      clear(result).append(element('p', { class: 'note' }, 'The proposal is in the editor. Save to keep it.'));
    });
    clear(result).append(
      element('h3', {}, 'Proposal'),
      element('p', { class: 'pre-line' }, proposal.explanation || 'No explanation given.'),
      element('p', { class: summary.valid ? 'note' : 'note bad' }, summary.status, ' ', summary.cost),
      issuesView(proposal.validation),
      diffView(proposal.diff, 'Proposed changes'),
      element('div', { class: 'wf-actions' }, acceptButton, discardButton));
  }

  askButton.addEventListener('click', async () => {
    const askedBody = getBody();
    const { body, error } = assistBody({ kind, record, body: askedBody, instruction: instruction.value, alias: aliasSelect.value, teamId: team?.select.value });
    if (error) {
      clear(result).append(errorBox({ message: error }));
      return;
    }
    askButton.disabled = true;
    clear(result).append(loading());
    try {
      const answer = await askAssistant(body);
      if (answer?.proposal) showResult(answer.proposal, askedBody);
      else clear(result).append(element('p', { class: 'pre-line' }, answer?.reply || 'No answer.'));
    } catch (err) {
      clear(result).append(errorBox(err));
    } finally {
      askButton.disabled = false;
    }
  });

  return card(element('h2', { class: 'inline' }, 'Ask the assistant'),
    element('p', { class: 'note' }, 'Describe a change in words. The assistant proposes a new version for you to review. Nothing is saved until you save.'),
    element('div', { class: 'stacked-form' },
      element('label', { class: 'stacked', for: instruction.id }, 'What to change', instruction),
      team?.field,
      element('label', { class: 'stacked', for: aliasSelect.id }, 'Model alias', aliasSelect)),
    element('div', { class: 'wf-actions' }, askButton),
    result);
}

export function testRunPanel(record) {
  const idPrefix = `test-${record.id}`;
  const runSelect = element('select', { id: `${idPrefix}-from` }, element('option', { value: '' }, 'No recorded outputs'));
  const inputs = element('textarea', { id: `${idPrefix}-inputs`, class: 'mono', rows: 3, placeholder: '{"number": 42}' });
  const missing = element('input', { type: 'checkbox', id: `${idPrefix}-missing` });
  const team = needsTeam(record) ? teamPicker(`${idPrefix}-team`, 'Team the run belongs to') : null;
  const startButton = element('button', { type: 'button', class: 'primary' }, 'Start test run');
  const status = element('div', { role: 'status' });

  const query = new URLSearchParams({ workflow: record.name, with_outputs: '1', limit: '20' });
  api(`/api/runs?${query}`).then((payload) => {
    runSelect.append(...listOf(payload, 'runs').map((run) => element('option', { value: String(run.id) }, runChoiceLabel(run))));
  }).catch(() => {});

  startButton.addEventListener('click', async () => {
    clear(status);
    const { body, error } = testRunBody({ record, fromRun: runSelect.value, inputsText: inputs.value, missingEmpty: missing.checked, teamId: team?.select.value });
    if (error) {
      status.append(errorBox({ message: error }));
      return;
    }
    startButton.disabled = true;
    try {
      const started = await api(`/api/workflows/${encodeURIComponent(record.id)}/test`, { method: 'POST', body });
      navigate(hashFor('run', [started.run_id]));
    } catch (err) {
      status.append(errorBox(err));
      startButton.disabled = false;
    }
  });

  return card(element('h2', { class: 'inline' }, 'Test run'),
    element('p', { class: 'note' }, 'Replays recorded step outputs in place of every step, so nothing runs, posts, or pushes, and no model is called. A step with no recorded output fails unless you treat it as empty.'),
    element('div', { class: 'stacked-form' },
      element('label', { class: 'stacked', for: runSelect.id }, 'Recorded outputs from', runSelect),
      team?.field,
      element('label', { class: 'stacked', for: inputs.id }, 'Inputs as JSON (optional)', inputs),
      element('label', { for: missing.id }, missing, ' Treat steps with no recorded output as empty')),
    element('div', { class: 'wf-actions' }, startButton),
    status);
}

export const HISTORY_MAX = 5;
export const EXPLANATION_MAX = 4000;
export const NOTE_MAX = 500;
export const CONFLICT_TEXT = 'The workflow changed since Claude proposed this; ask again.';

export const STEP_KINDS = {
  llm: { icon: '◆', label: 'Model' },
  script: { icon: '▶', label: 'Script' },
  tool: { icon: '⚒', label: 'Tool' },
  gate: { icon: '⏸', label: 'Approval' },
  subagent: { icon: '↳', label: 'Workflow' },
  other: { icon: '•', label: 'Step' },
};

export const MARKERS = {
  added: { icon: '+', text: 'Added' },
  removed: { icon: '−', text: 'Removed' },
  changed: { icon: '~', text: 'Changed' },
  renamed: { icon: '→', text: 'Renamed' },
};

const FIELD_WORDS = { label: 'name', kind: 'kind', what: 'what it does', after: 'order', fan_out: 'fan-out', output: 'output', optional: 'optional', retry: 'retries', details: 'details' };

export const stepKind = (kind) => STEP_KINDS[kind] || STEP_KINDS.other;

export function stepRows(described, changes) {
  const changed = new Map((changes?.changed || []).map((entry) => [entry.id, entry.fields || []]));
  const renamedFrom = new Map((changes?.renamed || []).map((entry) => [entry.to, entry.from]));
  const added = new Set(changes?.added || []);
  const rows = (list) => (list || []).map((step) => {
    let marker = null;
    if (added.has(step.id)) marker = 'added';
    else if (renamedFrom.has(step.id)) marker = 'renamed';
    else if (changed.has(step.id)) marker = 'changed';
    return { step, marker, fields: changed.get(step.id) || [], renamedFrom: renamedFrom.get(step.id) || null };
  });
  return { steps: rows(described?.steps), postRun: rows(described?.post_run), removed: [...(changes?.removed || [])] };
}

export function markerText(row) {
  if (!row?.marker) return null;
  const base = MARKERS[row.marker].text;
  if (row.marker === 'renamed') return `${base} from ${row.renamedFrom}`;
  if (row.marker === 'changed' && row.fields?.length) return `${base}: ${row.fields.map((field) => FIELD_WORDS[field] || field).join(', ')}`;
  return base;
}

export function outputText(output) {
  if (!output || output === 'text') return 'Returns text';
  if (output === 'structured') return 'Returns structured data';
  return `Returns ${String(output).replace(/^structured: /, '')}`;
}

export function stepNotes(step) {
  const notes = [];
  if (step.after?.length) notes.push(`After ${step.after.join(', ')}`);
  if (step.fan_out) notes.push(`Runs ${step.fan_out}`);
  notes.push(outputText(step.output));
  if (step.optional) notes.push('Optional: the run goes on if it fails');
  if (step.retry) notes.push(step.retry);
  return notes;
}

export function inputsText(inputs) {
  if (!inputs?.length) return 'none';
  return inputs.map((declared) => (declared.required ? `${declared.name} (required)` : declared.name)).join(', ');
}

export function startedByText(starter) {
  const events = starter.events?.length ? starter.events.join(', ') : 'any event';
  const states = [];
  if (starter.shadow) states.push('shadow: records only');
  if (!starter.enabled) states.push('off');
  return `${starter.webhook} on ${events}${states.length ? ` (${states.join(', ')})` : ''}`;
}

export function pushHistory(history, instruction, explanation) {
  const turn = { instruction: String(instruction ?? '').slice(0, INSTRUCTION_MAX), explanation: String(explanation ?? '').slice(0, EXPLANATION_MAX) };
  return [...(history || []), turn].slice(-HISTORY_MAX);
}

export function applyNote(history) {
  const text = (history || []).map((turn) => turn.instruction).join(' Then: ').replace(/\s+/g, ' ').trim();
  return text.length > NOTE_MAX ? `${text.slice(0, NOTE_MAX - 1)}…` : text;
}

export function proposeBody({ record, baseVersion, instruction, history, draft, name, scope }) {
  const text = String(instruction ?? '').trim();
  if (!text) return { error: 'Describe what you want first.' };
  if (text.length > INSTRUCTION_MAX) return { error: `A description is at most ${INSTRUCTION_MAX} characters.` };
  const body = { kind: 'workflow', instruction: text, alias: DEFAULT_ALIAS };
  if (record?.id) {
    body.workflow_id = record.id;
    body.base_version = baseVersion;
  } else {
    const named = String(name ?? '').trim();
    if (!named) return { error: 'Give the workflow a name first.' };
    if (!scope) return { error: 'Pick where the workflow belongs.' };
    body.name = named;
    body.scope_kind = scope.scope_kind;
    if (scope.scope_id) body.scope_id = scope.scope_id;
  }
  if (history?.length) body.history = history.slice(-HISTORY_MAX);
  if (typeof draft === 'string') body.draft = draft;
  return { body };
}

export const elapsedText = (seconds) => `${Math.max(0, Math.floor(seconds))}s`;

function kindBadge(kind) {
  const shown = stepKind(kind);
  return element('span', { class: 'step-kind' }, element('span', { 'aria-hidden': 'true' }, shown.icon), ` ${shown.label}`);
}

function markerBadge(row) {
  const text = markerText(row);
  if (!text) return null;
  return element('span', { class: `step-marker marker-${row.marker}` }, element('span', { 'aria-hidden': 'true' }, MARKERS[row.marker].icon), ` ${text}`);
}

function stepItem(row) {
  const { step } = row;
  return element('li', { class: `step-item${row.marker ? ` marker-${row.marker}` : ''}` },
    element('div', { class: 'step-head' }, markerBadge(row), kindBadge(step.kind), element('strong', { class: 'step-title' }, step.label)),
    element('p', { class: 'step-what' }, step.what),
    element('p', { class: 'note step-notes' }, stepNotes(step).join(' · ')));
}

export function summaryParts(described, { changes = null, heading = 'What it does', action = null } = {}) {
  const rows = stepRows(described, changes);
  const parts = [element('div', { class: 'card-head' }, element('h2', { class: 'inline' }, heading), action)];
  if (described?.error) parts.push(element('p', { class: 'note bad' }, described.error));
  if (described?.description) parts.push(element('p', {}, described.description));
  parts.push(element('p', { class: 'note' }, `Inputs: ${inputsText(described?.inputs)}`));
  if (rows.steps.length) parts.push(element('ol', { class: 'steps-list', 'aria-label': 'Steps' }, rows.steps.map(stepItem)));
  else if (!described?.error) parts.push(element('p', { class: 'note' }, 'No steps yet.'));
  if (rows.postRun.length) parts.push(element('h3', {}, 'After the run'), element('ol', { class: 'steps-list', 'aria-label': 'After the run' }, rows.postRun.map(stepItem)));
  if (rows.removed.length) {
    parts.push(element('h3', {}, 'Removed steps'), element('ul', { class: 'steps-list', 'aria-label': 'Removed steps' },
      rows.removed.map((id) => element('li', { class: 'step-item marker-removed' }, element('div', { class: 'step-head' }, markerBadge({ marker: 'removed' }), element('strong', { class: 'step-title' }, id))))));
  }
  return parts;
}

export const summaryView = (described, options) => card(...summaryParts(described, options));

export function startedByCard(starters) {
  const list = starters || [];
  return card(element('h2', { class: 'inline' }, 'Started by'),
    list.length ? element('ul', { class: 'started-by' }, list.map((starter) => element('li', {}, startedByText(starter))))
      : element('p', { class: 'note' }, 'No webhook rule starts this workflow.'));
}

// Claude proposes, the user reads the plain-words summary, and only Apply saves.
export function describePanel({ idPrefix, label, placeholder, fields = null, request, apply, initial = '' }) {
  const instruction = element('textarea', { id: `${idPrefix}-instruction`, rows: 10, maxlength: INSTRUCTION_MAX, placeholder, 'aria-describedby': `${idPrefix}-hint` });
  instruction.value = String(initial).slice(0, INSTRUCTION_MAX);
  const sendButton = element('button', { type: 'button', class: 'primary' }, 'Send');
  const cancelButton = element('button', { type: 'button', hidden: true }, 'Cancel');
  const working = element('div', { role: 'status' });
  const proposalSlot = element('div');
  let history = [];
  let current = null;
  let conversationId = null;
  let sequence = 0;
  let timer = null;
  let busy = [];

  function settle() {
    clearInterval(timer);
    timer = null;
    cancelButton.hidden = true;
    for (const button of busy) button.disabled = false;
    busy = [];
  }

  function reset() {
    sequence += 1;
    settle();
    history = [];
    current = null;
    conversationId = null;
    clear(proposalSlot);
    clear(working);
  }

  async function ask(text, refining, buttons) {
    const { body, error } = request({ instruction: text, history: refining ? history : [], draft: refining ? current?.body : null });
    if (error) {
      clear(working).append(errorBox({ message: error }));
      return;
    }
    const mine = ++sequence;
    const started = Date.now();
    const seconds = element('span', { 'aria-hidden': 'true' }, elapsedText(0));
    clear(working).append(element('p', { class: 'note drafting' }, 'Claude is drafting… ', seconds));
    timer = setInterval(() => { seconds.textContent = elapsedText((Date.now() - started) / 1000); }, 1000);
    busy = buttons;
    for (const button of busy) button.disabled = true;
    cancelButton.hidden = false;
    try {
      const answer = await askAssistant(body, { conversationId: refining ? conversationId : null });
      if (mine !== sequence || !answer) return;
      conversationId = answer.conversation.id;
      if (!answer.proposal) {
        clear(working).append(element('p', { class: 'pre-line proposal-explanation' }, answer.reply || 'No answer.'));
        return;
      }
      history = pushHistory(refining ? history : [], text, answer.proposal.explanation);
      current = answer.proposal;
      clear(working);
      showProposal(answer.proposal);
    } catch (err) {
      if (mine === sequence) clear(working).append(errorBox(err));
    } finally {
      if (mine === sequence) settle();
    }
  }

  function showProposal(proposal) {
    const outcome = resultSummary(proposal);
    const applyButton = element('button', { type: 'button', class: 'primary' }, 'Apply');
    const discardButton = element('button', { type: 'button' }, 'Discard');
    const applyStatus = element('div', { role: 'status' });
    const refine = element('textarea', { id: `${idPrefix}-refine`, rows: 3, maxlength: INSTRUCTION_MAX, placeholder: 'For example: also skip draft pull requests.' });
    const refineButton = element('button', { type: 'button' }, 'Send');
    const sendRefine = () => ask(refine.value, true, [refineButton, applyButton, sendButton]);
    refineButton.addEventListener('click', sendRefine);
    refine.addEventListener('keydown', (keyEvent) => {
      if (keyEvent.key === 'Enter' && (keyEvent.metaKey || keyEvent.ctrlKey)) sendRefine();
    });
    discardButton.addEventListener('click', () => {
      reset();
      instruction.focus();
    });
    applyButton.addEventListener('click', async () => {
      clear(applyStatus);
      applyButton.disabled = true;
      try {
        await apply(proposal, applyNote(history));
        reset();
        instruction.value = '';
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) applyStatus.append(errorBox({ message: CONFLICT_TEXT }));
        else if (err instanceof ApiError && err.status === 422) applyStatus.append(errorBox({ message: 'Not saved. The proposal has errors. Refine it, or edit the YAML.' }), issuesView(err.payload?.validation));
        else applyStatus.append(errorBox(err));
      } finally {
        applyButton.disabled = false;
      }
    });
    const provider = proposal.team_name ? ` Used ${proposal.team_name}'s provider.` : '';
    const disclosure = element('details', { class: 'yaml-changes' }, element('summary', {}, 'Show YAML changes'), diffView(proposal.diff, 'YAML changes'));
    clear(proposalSlot).append(card(
      element('h2', { class: 'inline' }, "Claude's proposal"),
      element('p', { class: 'pre-line proposal-explanation' }, proposal.explanation || 'No explanation given.'),
      element('p', { class: outcome.valid ? 'note' : 'note bad' }, outcome.status, outcome.cost ? ` ${outcome.cost}.` : '', provider),
      issuesView(proposal.validation),
      proposal.summary ? element('section', { class: 'proposal-steps', 'aria-label': 'Steps after the change' }, summaryParts(proposal.summary, { changes: proposal.changes, heading: 'Steps after the change' })) : null,
      disclosure,
      element('div', { class: 'wf-actions' }, applyButton, discardButton),
      applyStatus,
      element('div', { class: 'stacked-form refine' }, element('label', { class: 'stacked', for: refine.id }, 'Refine this', refine)),
      element('div', { class: 'wf-actions' }, refineButton)));
    proposalSlot.querySelector('h2')?.setAttribute('tabindex', '-1');
    proposalSlot.querySelector('h2')?.focus();
  }

  // After a question from Claude, the next message answers it in the same conversation.
  const send = () => ask(instruction.value, Boolean(conversationId && !current), [sendButton]);
  sendButton.addEventListener('click', send);
  instruction.addEventListener('keydown', (keyEvent) => {
    if (keyEvent.key === 'Enter' && (keyEvent.metaKey || keyEvent.ctrlKey)) send();
  });
  cancelButton.addEventListener('click', () => {
    sequence += 1;
    settle();
    clear(working).append(element('p', { class: 'note' }, 'Cancelled. Nothing was saved.'));
  });

  const prompt = card(
    element('label', { class: 'stacked prompt-label', for: instruction.id }, label, instruction),
    fields,
    element('div', { class: 'wf-actions' }, sendButton, cancelButton,
      element('span', { class: 'note', id: `${idPrefix}-hint` }, 'Cmd or Ctrl + Enter sends. Nothing is saved until you apply the proposal.')),
    working);
  return { root: element('div', { class: 'describe' }, prompt, proposalSlot), isDirty: () => instruction.value.trim() !== '' || current !== null, focus: () => instruction.focus() };
}
