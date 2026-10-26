import { api, listOf } from './api.js';
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
    cost: `Cost ${money(result?.cost_usd ?? 0, 4)}${tries}`,
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
      showResult(await api('/api/assist', { method: 'POST', body }), askedBody);
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
