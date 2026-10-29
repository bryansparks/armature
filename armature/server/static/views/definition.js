import { api, ApiError, listOf } from '../api.js';
import { assistantPanel, describePanel, proposeBody, startedByCard, summaryView, testRunPanel } from '../assistant.js';
import { card, clear, confirmDialog, element, empty, errorBox, link, loading, meta, pageHeader, table, when } from '../dom.js';
import { codeEditor, debounce } from '../editor.js';
import { diffView } from '../diff.js';
import { frontMatter, SHOWN_FIELDS } from '../frontmatter.js';
import { navigate, protect, release } from '../guard.js';
import { hashFor } from '../router.js';
import { creatableScopes, findScope, sameScope, scopeKey } from '../scopes.js';
import { canManageTeam, currentSession, isAdmin } from '../session.js';
import { stepsPanel } from './steps.js';

export const KIND_LABEL = { role: 'Role', prompt: 'Prompt', mission: 'Mission' };
export const WORKFLOW_KIND_LABEL = { review: 'Review', fix: 'Fix', merge: 'Merge', port: 'Port', work: 'Work', other: 'Other' };

const ROLE_STARTER = `---
name: new-role
description: What this role looks for.
applies_to: [ios, android, backend]
model: review
tools: [Read, Grep, Glob]
mcp_servers: []
---

You are the reviewer for ...
`;

const NOTE_STARTER = `---
name: new-prompt
---

`;

const WORKFLOW_STARTER = `name: new-workflow
description: What this workflow does.
stages:
  - id: first
    adapter: ""
`;

export const WORKFLOW = {
  base: '/api/workflows', route: 'workflow', idField: 'workflow_id', format: 'YAML', liveValidate: true, steps: true, lint: true,
  testRun: true, describe: true, record: (payload) => payload?.workflow, title: (record) => record.name, noun: () => 'workflow',
  kinds: WORKFLOW_KIND_LABEL, kindLabel: (record) => WORKFLOW_KIND_LABEL[record.kind] || record.kind || null,
  listHash: () => '#/workflows', listLabel: 'All workflows', starter: () => WORKFLOW_STARTER,
};

export const DOCUMENT = {
  base: '/api/documents', route: 'document', idField: 'document_id', format: 'Markdown', liveValidate: false, frontMatter: true,
  record: (payload) => payload?.document, title: (record) => record.key || record.name, noun: (kind) => (KIND_LABEL[kind] || 'document').toLowerCase(),
  kindLabel: (record) => KIND_LABEL[record.kind] || record.kind, listHash: (record) => hashFor('roles', [record.kind || 'role']),
  listLabel: 'All roles and prompts', starter: (kind) => (kind === 'role' ? ROLE_STARTER : NOTE_STARTER.replace('new-prompt', `new-${kind || 'prompt'}`)),
};

const itemPath = (config, id, tail = '') => `${config.base}/${encodeURIComponent(id)}${tail}`;

export const rollbackBody = (number, base) => ({ version: number, note: `Roll back to version ${number}`, base_version: base });

export function conflictOf(err) {
  if (!(err instanceof ApiError && err.status === 409)) return null;
  return { current: err.payload?.current_version ?? null };
}

export async function teamNames() {
  const teams = await api('/api/teams').then((payload) => listOf(payload, 'teams')).catch(() => []);
  return new Map(teams.map((team) => [String(team.id), team.name]));
}

export const scopeLabel = (teamId, names) => (teamId ? names.get(String(teamId)) || 'Team' : 'Instance');

export function scopeText(record, names) {
  if (record.scope_name) return record.scope_name;
  if (record.scope_kind === 'instance') return 'Instance';
  return scopeLabel(record.team_id, names);
}

export function lintLabel(lintOk) {
  if (lintOk === true) return element('span', { class: 'state state-ok' }, 'Clean');
  if (lintOk === false) return element('span', { class: 'state state-bad' }, 'Issues');
  return element('span', { class: 'muted' }, 'Not linted');
}

export function effectiveText(effectiveFor) {
  if (typeof effectiveFor === 'string' && effectiveFor) return effectiveFor;
  if (!Array.isArray(effectiveFor) || !effectiveFor.length) return '—';
  return effectiveFor.map((entry) => (typeof entry === 'string' ? entry : entry?.scope_name || entry?.name || entry?.id || '')).filter(Boolean).join(', ') || '—';
}

// Stored text is shown as text, never parsed as Markdown or HTML.
export function sourceCard(title, text, ...extras) {
  return card(element('div', { class: 'card-head' }, element('h2', { class: 'inline' }, title), ...extras), element('pre', { class: 'source', tabindex: 0 }, text ?? ''));
}

function fieldValue(value) {
  if (value === undefined) return element('span', { class: 'muted' }, 'Not set');
  if (Array.isArray(value)) return value.length ? element('span', { class: 'chips' }, value.map((item) => element('span', { class: 'chip mono' }, item))) : element('span', { class: 'muted' }, 'None');
  return element('span', { class: 'mono' }, value);
}

export function frontMatterView(body) {
  const parsed = frontMatter(body);
  if (!parsed.found) return element('p', { class: 'note' }, 'No front matter. A role starts with a line of ---, then its fields, then another ---.');
  if (parsed.error) return element('p', { class: 'note bad' }, parsed.error);
  return element('dl', { class: 'fields' }, SHOWN_FIELDS.map((key) => element('div', {}, element('dt', {}, key), element('dd', {}, fieldValue(parsed.fields[key])))));
}

export async function loadScopes() {
  const teams = await api('/api/teams').then((payload) => listOf(payload, 'teams')).catch(() => []);
  const led = teams.filter((team) => isAdmin() || canManageTeam(team.id));
  const projectLists = await Promise.all(led.map((team) => api(`/api/teams/${encodeURIComponent(team.id)}/projects`)
    .then((payload) => listOf(payload, 'projects')).catch(() => [])));
  return creatableScopes(currentSession(), teams, new Map(led.map((team, index) => [String(team.id), projectLists[index]])));
}

function scopeSelect(id, scopes, wanted) {
  const select = element('select', { id, required: true }, scopes.map((scope) => element('option', { value: scopeKey(scope) }, scope.label)));
  if (findScope(scopes, wanted)) select.value = wanted;
  return { select, chosen: () => findScope(scopes, select.value) };
}

export function kindSelect(id, labels, value) {
  const select = element('select', { id }, Object.entries(labels).map(([kind, label]) => element('option', { value: kind }, label)));
  select.value = value in labels ? value : 'other';
  return select;
}

function kindControl(config, record, editable, status, shown) {
  const select = kindSelect('workflow-kind', config.kinds, record.kind);
  select.disabled = !editable;
  select.addEventListener('change', async () => {
    clear(status);
    select.disabled = true;
    try {
      await api(itemPath(config, record.id, '/kind'), { method: 'POST', body: { kind: select.value } });
      record.kind = select.value;
      shown.textContent = config.kindLabel(record);
    } catch (err) {
      select.value = record.kind;
      status.append(errorBox(err));
    } finally {
      select.disabled = false;
    }
  });
  return element('div', { class: 'wf-actions' }, element('label', { for: select.id }, 'Kind'), select);
}

// Tabs that switch in place, so the editor keeps its text and undo history.
export function localTabs(label, entries) {
  const tablist = element('div', { class: 'tabs', role: 'tablist', 'aria-label': label });
  const panels = element('div');
  const buttons = entries.map((entry, index) => {
    const panelId = `local-panel-${entry.id}`;
    entry.panel.setAttribute('role', 'tabpanel');
    entry.panel.id = panelId;
    entry.panel.setAttribute('aria-labelledby', `local-tab-${entry.id}`);
    entry.panel.hidden = index !== 0;
    panels.append(entry.panel);
    return element('button', { type: 'button', role: 'tab', id: `local-tab-${entry.id}`, class: `tab${index === 0 ? ' active' : ''}`, 'aria-controls': panelId, 'aria-selected': String(index === 0), tabindex: index === 0 ? 0 : -1 }, entry.label);
  });
  const select = (index, shouldFocus) => {
    entries.forEach((entry, position) => {
      const isActive = position === index;
      entry.panel.hidden = !isActive;
      buttons[position].classList.toggle('active', isActive);
      buttons[position].setAttribute('aria-selected', String(isActive));
      buttons[position].tabIndex = isActive ? 0 : -1;
    });
    if (shouldFocus) buttons[index].focus();
    entries[index].onShow?.();
  };
  buttons.forEach((button, index) => button.addEventListener('click', () => select(index, false)));
  tablist.addEventListener('keydown', (keyEvent) => {
    const current = buttons.indexOf(document.activeElement);
    const moves = { ArrowRight: current + 1, ArrowLeft: current - 1, Home: 0, End: buttons.length - 1 };
    if (current < 0 || !(keyEvent.key in moves)) return;
    keyEvent.preventDefault();
    select((moves[keyEvent.key] + buttons.length) % buttons.length, true);
  });
  tablist.append(...buttons);
  return element('div', {}, tablist, panels);
}

// The diff shows what someone else saved, so the user can decide before writing over it.
function conflictBanner(config, record, from, current, { message, discardLabel, onDiscard, proceedLabel, onProceed }) {
  const discardButton = element('button', { type: 'button', onclick: onDiscard }, discardLabel);
  const proceedButton = element('button', { type: 'button', class: 'primary', disabled: true }, proceedLabel);
  proceedButton.addEventListener('click', onProceed);
  const diffSlot = element('div', {}, current ? loading() : null);
  const banner = element('div', { class: 'banner warn', role: 'alert' },
    element('p', {}, `This ${config.noun(record.kind)} changed since you opened it.${current ? ` Version ${current} is now current.` : ''} ${message}`),
    diffSlot, element('div', { class: 'wf-actions' }, discardButton, proceedButton));
  (async () => {
    if (current) {
      try {
        const query = new URLSearchParams({ from: String(from), to: String(current) });
        const { diff } = await api(`${itemPath(config, record.id, '/diff')}?${query}`);
        clear(diffSlot).append(element('p', { class: 'note' }, `What changed from version ${from}, which you opened, to version ${current}:`), diffView(diff, 'Changes made since you opened it'));
      } catch (err) {
        clear(diffSlot).append(errorBox(err));
      }
    }
    proceedButton.disabled = false;
  })();
  return banner;
}

function versionsPanel(config, record, editable, initial) {
  const body = element('div');
  const compareSlot = element('div');
  const viewSlot = element('div');
  const status = element('div', { role: 'status' });
  let versions = initial.versions;
  let current = initial.current;

  async function view(number) {
    clear(viewSlot).append(loading());
    try {
      const { version } = await api(itemPath(config, record.id, `/versions/${encodeURIComponent(number)}`));
      const closeButton = element('button', { type: 'button', class: 'link-button', onclick: () => clear(viewSlot) }, 'Close');
      clear(viewSlot).append(sourceCard(`Version ${number}`, version?.body, closeButton));
      viewSlot.querySelector('pre')?.focus();
    } catch (err) {
      clear(viewSlot).append(errorBox(err));
    }
  }

  async function rollBack(number, button, base = current) {
    if (base === current && !(await confirmDialog(`Roll back to version ${number}?`, `This saves a new version with the text of version ${number}. No version is deleted.`, 'Roll back'))) return;
    clear(status);
    button.disabled = true;
    try {
      await api(itemPath(config, record.id, '/rollback'), { method: 'POST', body: rollbackBody(number, base) });
      navigate(hashFor(config.route, [record.id]));
    } catch (err) {
      button.disabled = false;
      const conflict = conflictOf(err);
      if (!conflict) {
        status.append(errorBox(err));
        return;
      }
      const newest = conflict.current;
      status.append(conflictBanner(config, record, current, newest, {
        message: `Review the change below, then reload or roll back to version ${number} anyway.`,
        discardLabel: 'Reload', onDiscard: () => navigate(location.hash),
        proceedLabel: `Roll back over version ${newest || 'the newest'}`, onProceed: () => rollBack(number, button, newest || current),
      }));
    }
  }

  function compareForm() {
    if (versions.length < 2) return null;
    const options = () => versions.map((version) => element('option', { value: String(version.version) }, `Version ${version.version}`));
    const fromSelect = element('select', { id: 'diff-from' }, options());
    const toSelect = element('select', { id: 'diff-to' }, options());
    const numbers = versions.map((version) => version.version).sort((first, second) => second - first);
    fromSelect.value = String(numbers[1]);
    toSelect.value = String(numbers[0]);
    const compareButton = element('button', { type: 'submit' }, 'Compare');
    const result = element('div');
    const form = element('form', { class: 'wf-actions' }, element('label', { for: fromSelect.id }, 'From'), fromSelect,
      element('label', { for: toSelect.id }, 'To'), toSelect, compareButton);
    form.addEventListener('submit', async (submitEvent) => {
      submitEvent.preventDefault();
      clear(result).append(loading());
      compareButton.disabled = true;
      try {
        const query = new URLSearchParams({ from: fromSelect.value, to: toSelect.value });
        const { diff } = await api(`${itemPath(config, record.id, '/diff')}?${query}`);
        clear(result).append(diffView(diff, `Changes from version ${fromSelect.value} to ${toSelect.value}`));
      } catch (err) {
        clear(result).append(errorBox(err));
      } finally {
        compareButton.disabled = false;
      }
    });
    return element('div', { class: 'compare' }, element('h3', {}, 'Compare versions'), form, result);
  }

  function draw() {
    clear(body).append(versions.length ? table(['Version', 'Author', 'Saved', 'Note', ''], versions.map((version) => {
      const viewButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `View version ${version.version}` }, 'View');
      viewButton.addEventListener('click', () => view(version.version));
      const actions = [viewButton];
      if (editable && version.version !== current) {
        const rollButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `Roll back to version ${version.version}` }, 'Roll back');
        rollButton.addEventListener('click', () => rollBack(version.version, rollButton));
        actions.push(' ', rollButton);
      }
      return [version.version === current ? `${version.version} (current)` : String(version.version), version.author || '—', when(version.created_at),
        element('span', { class: 'pre-line' }, version.note || ''), element('span', { class: 'row-actions' }, actions)];
    })) : empty('No versions yet.'));
    clear(compareSlot).append(compareForm());
  }

  draw();
  return {
    root: element('div', {}, card(element('h2', { class: 'inline' }, 'Versions'), status, body, compareSlot), viewSlot),
    update(next) {
      versions = next.versions;
      current = next.current;
      draw();
    },
  };
}

function scopeDialog(title, consequence, verb, scopes, nameLabel, nameValue) {
  return new Promise((resolve) => {
    const { select, chosen } = scopeSelect('dialog-scope', scopes);
    const nameInput = element('input', { type: 'text', id: 'dialog-name', maxlength: 100, spellcheck: 'false', autocapitalize: 'none' });
    nameInput.value = nameValue || '';
    const cancelButton = element('button', { type: 'button' }, 'Cancel');
    const form = element('form', { class: 'stacked-form' },
      element('label', { class: 'stacked', for: select.id }, 'Copy into', select),
      element('label', { class: 'stacked', for: nameInput.id }, nameLabel, nameInput),
      element('div', { class: 'dialog-actions' }, cancelButton, element('button', { type: 'submit', class: 'primary' }, verb)));
    const dialog = element('dialog', { class: 'confirm', 'aria-labelledby': 'scope-dialog-title' },
      element('h2', { id: 'scope-dialog-title' }, title), element('p', {}, consequence), form);
    const close = (value) => {
      dialog.close();
      dialog.remove();
      resolve(value);
    };
    cancelButton.addEventListener('click', () => close(null));
    form.addEventListener('submit', (submitEvent) => {
      submitEvent.preventDefault();
      close({ scope: chosen(), name: nameInput.value.trim() });
    });
    dialog.addEventListener('cancel', (cancelEvent) => {
      cancelEvent.preventDefault();
      close(null);
    });
    document.body.append(dialog);
    dialog.showModal();
    select.focus();
  });
}

function forkButton(config, record, status) {
  const button = element('button', { type: 'button' }, 'Customize for…');
  button.addEventListener('click', async () => {
    clear(status);
    button.disabled = true;
    const scopes = (await loadScopes()).filter((scope) => !(record.scope_kind && sameScope(scope, record)));
    button.disabled = false;
    if (!scopes.length) {
      status.append(errorBox({ message: 'You do not lead a team or project to copy this into.' }));
      return;
    }
    const noun = config.noun(record.kind);
    const choice = await scopeDialog(`Customize ${config.title(record)}`, `This makes your own copy of the ${noun}. Runs in that team or project use the copy, and this one stays as it is.`,
      'Make a copy', scopes, 'Name of the copy', config.title(record));
    if (!choice?.scope) return;
    button.disabled = true;
    try {
      const forked = await api(itemPath(config, record.id, '/fork'), {
        method: 'POST', body: { scope_kind: choice.scope.scope_kind, scope_id: choice.scope.scope_id, name: choice.name || undefined },
      });
      const copy = config.record(forked) || forked;
      navigate(hashFor(config.route, [copy.id, 'edit']));
    } catch (err) {
      status.append(errorBox(err));
      button.disabled = false;
    }
  });
  return button;
}

function deleteButton(config, record, status) {
  const button = element('button', { type: 'button', class: 'link-button' }, 'Delete');
  button.addEventListener('click', async () => {
    const name = config.title(record);
    if (!(await confirmDialog(`Delete ${name}?`, `Runs that use this ${config.noun(record.kind)} can no longer find it.`, 'Delete'))) return;
    clear(status);
    button.disabled = true;
    try {
      await api(itemPath(config, record.id, '/delete'), { method: 'POST', body: {} });
      release();
      navigate(config.listHash(record));
    } catch (err) {
      status.append(errorBox(err));
      button.disabled = false;
    }
  });
  return button;
}

function sourceEditor(config, { initial, recordId, onSave }) {
  let validation = 0;
  const frontSlot = config.frontMatter ? element('div', {}, frontMatterView(initial)) : null;
  const drawFront = frontSlot ? debounce(() => clear(frontSlot).append(frontMatterView(editor.value())), 250) : null;
  const validateSoon = config.liveValidate ? debounce(() => validate(), 600) : null;
  const editor = codeEditor({
    value: initial, label: `${config.format} source`, onSave,
    onInput: () => {
      validateSoon?.();
      drawFront?.();
    },
  });
  async function validate() {
    if (!editor.root.isConnected) return;
    const sequence = ++validation;
    try {
      const body = { body: editor.value() };
      if (recordId) body[config.idField] = recordId;
      const result = await api(`${config.base}/validate`, { method: 'POST', body });
      if (sequence === validation) editor.setIssues(result?.validation?.errors, result?.validation?.warnings);
    } catch (err) {
      if (sequence === validation) editor.setIssues([], [{ line: null, message: `Could not check the ${config.format}: ${err.message}` }]);
    }
  }
  const stopValidating = () => {
    validation += 1;
    validateSoon?.cancel();
  };
  let content = editor.root;
  if (config.steps) {
    const steps = stepsPanel({ getBody: () => editor.value(), setBody: (text) => editor.setValue(text) });
    content = localTabs('Editor', [{ id: 'yaml', label: 'YAML', panel: element('div', {}, editor.root) }, { id: 'steps', label: 'Steps', panel: steps.root, onShow: steps.show }]);
  }
  if (config.liveValidate && initial.trim()) validate();
  const parts = [frontSlot ? card(element('h2', { class: 'inline' }, 'Front matter'), frontSlot) : null, content];
  return { editor, parts, stopValidating };
}

function editPane(config, record, version, status, onSaved) {
  let baseVersion = record.current_version;
  let saving = false;
  const conflictSlot = element('div');
  const noteInput = element('input', { type: 'text', id: 'save-note', maxlength: 500, placeholder: 'What changed and why' });
  const saveButton = element('button', { type: 'button', class: 'primary' }, 'Save');
  const { editor, parts, stopValidating } = sourceEditor(config, { initial: version?.body ?? '', recordId: record.id, onSave: () => save() });
  protect(() => editor.isDirty());

  function showConflict(current) {
    clear(conflictSlot).append(conflictBanner(config, record, baseVersion, current, {
      message: 'Review the change below, then reload it or save your edits over it.',
      discardLabel: 'Discard my edits and reload',
      onDiscard: async () => {
        if (!(await confirmDialog('Discard your edits?', 'Your unsaved changes are lost and the current version opens.', 'Discard'))) return;
        release();
        navigate(hashFor(config.route, [record.id, 'edit']));
      },
      proceedLabel: current ? `Save over version ${current}` : 'Save over it',
      onProceed: () => save(current),
    }));
  }

  async function save(overBase = baseVersion) {
    if (saving) return;
    saving = true;
    saveButton.disabled = true;
    clear(status);
    stopValidating();
    try {
      const result = await api(itemPath(config, record.id), { method: 'POST', body: { body: editor.value(), note: noteInput.value.trim(), base_version: overBase } });
      baseVersion = result?.version?.version ?? overBase + 1;
      editor.markClean();
      editor.setIssues(result?.validation?.errors, result?.validation?.warnings);
      noteInput.value = '';
      clear(conflictSlot);
      status.append(element('p', { class: 'note' }, `Saved version ${baseVersion}.`));
      onSaved();
    } catch (err) {
      if (conflictOf(err)) {
        showConflict(conflictOf(err).current);
      } else if (err instanceof ApiError && err.status === 422) {
        editor.setIssues(err.payload?.validation?.errors, err.payload?.validation?.warnings);
        status.append(errorBox({ message: 'Not saved. Fix the errors listed under the editor.' }));
      } else status.append(errorBox(err));
    } finally {
      saving = false;
      saveButton.disabled = false;
    }
  }

  saveButton.addEventListener('click', () => save());
  const assistant = assistantPanel({ kind: config.route, record, getBody: () => editor.value(), setBody: (text) => editor.setValue(text) });
  const saveBar = element('div', { class: 'wf-actions save-bar' }, element('label', { for: noteInput.id }, 'Note'), noteInput, saveButton,
    link(hashFor(config.route, [record.id]), 'Cancel'));
  return element('div', {}, conflictSlot, ...parts, saveBar, assistant);
}

let savedNotice = null;

const CHANGE_EXAMPLE = 'For example: after the review step, post a short summary to the team channel, and skip draft pull requests.';
const NEW_EXAMPLE = 'For example: when a pull request opens, review it for security problems, then post the findings as one comment.';

// The plain-words view: describe a change, read Claude's proposal, apply it. The YAML stays one click away.
function describedView(config, record, detail, editable) {
  const editYaml = editable ? link(hashFor(config.route, [record.id, 'edit']), 'Edit YAML', { class: 'btn small' }) : null;
  const parts = [];
  if (editable) {
    const panel = describePanel({
      idPrefix: `describe-${record.id}`, label: 'Describe a change', placeholder: CHANGE_EXAMPLE,
      request: (args) => proposeBody({ record, baseVersion: record.current_version, ...args }),
      apply: async (proposal, note) => {
        const saved = await api(itemPath(config, record.id), { method: 'POST', body: { body: proposal.body, note, base_version: proposal.base_version ?? record.current_version } });
        savedNotice = `Saved as version ${saved?.version?.version ?? record.current_version + 1}.`;
        release();
        navigate(hashFor(config.route, [record.id]));
      },
    });
    protect(panel.isDirty);
    parts.push(panel.root);
  }
  parts.push(summaryView(detail.summary, { action: editYaml }), startedByCard(detail.started_by),
    card(element('details', {}, element('summary', {}, 'Show YAML'), element('pre', { class: 'source', tabindex: 0 }, detail.version?.body ?? ''))));
  if (config.testRun) parts.push(testRunPanel(record));
  return parts;
}

function describeNew(config, { nameInput, chosen, kindInput, fields, onYaml }) {
  const panel = describePanel({
    idPrefix: 'describe-new', label: 'Describe the workflow', placeholder: NEW_EXAMPLE, fields,
    request: (args) => proposeBody({ name: nameInput.value, scope: chosen(), ...args }),
    apply: async (proposal, note) => {
      const scope = chosen();
      const body = { name: nameInput.value.trim(), scope_kind: scope.scope_kind, scope_id: scope.scope_id, body: proposal.body, note };
      if (kindInput) body.kind = kindInput.value;
      const created = await api(config.base, { method: 'POST', body });
      release();
      navigate(hashFor(config.route, [(config.record(created) || created).id]));
    },
  });
  protect(() => panel.isDirty() || nameInput.value.trim() !== '');
  const yamlButton = element('button', { type: 'button', class: 'link-button' }, 'Start from YAML');
  yamlButton.addEventListener('click', onYaml);
  return element('div', {}, panel.root, element('p', { class: 'note' }, 'Prefer to write it yourself? ', yamlButton));
}

export async function detailPage(root, config, id, mode) {
  let payload;
  try {
    payload = await api(itemPath(config, id));
  } catch (err) {
    root.append(pageHeader('Not found'), errorBox(err));
    return;
  }
  const record = config.record(payload);
  const version = payload.version;
  const editable = Boolean(payload.editable ?? record.editable);
  const canFork = Boolean(payload.can_fork ?? record.can_fork);
  const editing = mode === 'edit' && editable;
  const names = await teamNames();
  const status = element('div', { role: 'status' });
  const versionText = element('span', {}, `Version ${record.current_version}`);
  const actions = element('div', { class: 'wf-actions' });
  if (editable && !editing && !config.describe) actions.append(link(hashFor(config.route, [record.id, 'edit']), 'Edit', { class: 'btn' }));
  if (canFork && !editable) actions.append(forkButton(config, record, status));
  if (editable) actions.append(deleteButton(config, record, status));
  const kindText = config.kindLabel(record) ? element('span', {}, config.kindLabel(record)) : null;
  root.append(pageHeader(config.title(record)), meta(kindText, scopeText(record, names), versionText,
    config.lint ? lintLabel(record.lint_ok) : null, effectiveText(record.effective_for ?? payload.effective_for) !== '—' ? `Used by ${effectiveText(record.effective_for ?? payload.effective_for)}` : null, link(config.listHash(record), config.listLabel)));
  if (actions.childNodes.length) root.append(actions);
  if (config.kinds) root.append(kindControl(config, record, editable, status, kindText));
  root.append(status);
  if (savedNotice) status.append(element('p', { class: 'note' }, savedNotice));
  savedNotice = null;
  if (mode === 'edit' && !editable) root.append(element('p', { class: 'banner' }, 'You can read this but not change it.'));

  const versions = versionsPanel(config, record, editable, { versions: listOf(payload, 'versions'), current: record.current_version });
  async function refreshVersions() {
    try {
      const fresh = await api(itemPath(config, id));
      const freshRecord = config.record(fresh);
      versionText.textContent = `Version ${freshRecord.current_version}`;
      versions.update({ versions: listOf(fresh, 'versions'), current: freshRecord.current_version });
    } catch {}
  }

  if (editing) root.append(editPane(config, record, version, status, refreshVersions));
  else if (!version) root.append(card(empty('This has no saved version.')));
  else if (config.describe) root.append(...describedView(config, record, payload, editable));
  else {
    if (config.frontMatter) root.append(card(element('h2', { class: 'inline' }, 'Front matter'), frontMatterView(version.body)));
    root.append(sourceCard(config.format, version.body));
    if (config.testRun) root.append(testRunPanel(record));
  }
  root.append(versions.root);
}

export async function newPage(root, config, kind, wantedScope) {
  const noun = config.noun(kind);
  root.append(pageHeader(`New ${noun}`));
  const scopes = await loadScopes();
  if (!scopes.length) {
    root.append(card(empty(`You can add a ${noun} only to a team or project you lead.`)));
    return;
  }
  const status = element('div', { role: 'status' });
  const nameInput = element('input', { type: 'text', id: 'new-name', required: true, maxlength: 100, spellcheck: 'false', autocapitalize: 'none', autocomplete: 'off' });
  const { select, chosen } = scopeSelect('new-scope', scopes, wantedScope);
  const kindInput = config.kinds ? kindSelect('new-kind', config.kinds, 'other') : null;
  const noteInput = element('input', { type: 'text', id: 'new-note', maxlength: 500, placeholder: 'Why it exists' });
  const createButton = element('button', { type: 'button', class: 'primary' }, `Add ${noun}`);
  let creating = false;
  const fields = element('div', { class: 'stacked-form' },
    element('label', { class: 'stacked', for: nameInput.id }, 'Name', nameInput),
    element('label', { class: 'stacked', for: select.id }, 'Belongs to', select),
    kindInput ? element('label', { class: 'stacked', for: kindInput.id }, 'Kind', kindInput) : null);
  const slot = element('div');
  root.append(slot);
  if (config.describe) {
    slot.append(describeNew(config, { nameInput, chosen, kindInput, fields, onYaml: () => {
      clear(slot);
      startFromYaml();
      nameInput.focus();
    } }));
    return;
  }
  startFromYaml();

  function startFromYaml() {
    const { editor, parts, stopValidating } = sourceEditor(config, { initial: config.starter(kind), recordId: null, onSave: () => create() });
    protect(() => editor.isDirty() || nameInput.value.trim() !== '');

    async function create() {
      if (creating) return;
      clear(status);
      const name = nameInput.value.trim();
      if (!name) {
        status.append(errorBox({ message: 'Give it a name.' }));
        nameInput.focus();
        return;
      }
      const scope = chosen();
      creating = true;
      createButton.disabled = true;
      stopValidating();
      try {
        const body = { name, scope_kind: scope.scope_kind, scope_id: scope.scope_id, body: editor.value(), note: noteInput.value.trim() };
        if (kind) Object.assign(body, { kind, key: name });
        if (kindInput) body.kind = kindInput.value;
        const created = await api(config.base, { method: 'POST', body });
        release();
        navigate(hashFor(config.route, [(config.record(created) || created).id]));
      } catch (err) {
        if (err instanceof ApiError && err.status === 422) {
          editor.setIssues(err.payload?.validation?.errors, err.payload?.validation?.warnings);
          status.append(errorBox({ message: 'Not added. Fix the errors listed under the editor.' }));
        } else status.append(errorBox(err));
      } finally {
        creating = false;
        createButton.disabled = false;
      }
    }

    createButton.addEventListener('click', create);
    slot.append(card(fields), status, ...parts,
      element('div', { class: 'wf-actions save-bar' }, element('label', { for: noteInput.id }, 'Note'), noteInput, createButton, link(config.listHash({ kind }), 'Cancel')));
  }
}
