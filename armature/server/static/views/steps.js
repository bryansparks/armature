import { api, listOf } from '../api.js';
import { clear, confirmDialog, element, empty, errorBox, loading } from '../dom.js';
import { issueList, issueText } from '../editor.js';
import { fieldInput, fieldsFor, parseJson } from '../schema.js';
import { addCard, adapterChoices, afterRender, cardFromStage, moveCard, removeCard, renameCard, renderPayload, stageProblems } from '../stages.js';

let catalogRequest = null;

function catalog() {
  catalogRequest ??= api('/api/packs').then((payload) => listOf(payload, 'packs')).catch((err) => {
    catalogRequest = null;
    throw err;
  });
  return catalogRequest;
}

// Each change goes through /render, so the YAML the server writes is what gets saved.
export function stepsPanel({ getBody, setBody }) {
  const root = element('div', { class: 'steps' });
  const status = element('div', { role: 'status' });
  const list = element('ol', { class: 'stage-list', role: 'list' });
  const addButton = element('button', { type: 'button' }, 'Add step');
  let cards = [];
  let ownAdapters = [];
  let packs = [];
  let adapters = new Map();
  let queue = Promise.resolve();

  function commit(next, { redraw = false } = {}) {
    const problems = stageProblems(next);
    clear(status);
    if (problems.length) {
      status.append(element('div', { class: 'error-box', role: 'alert' }, problems.join(' ')));
      return false;
    }
    cards = next;
    if (redraw) draw();
    queue = queue.then(async () => {
      try {
        const payload = renderPayload(getBody(), cards);
        const rendered = await api('/api/workflows/render', { method: 'POST', body: payload });
        if (typeof rendered?.body === 'string') setBody(rendered.body);
        cards = afterRender(cards, payload.stages);
      } catch (err) {
        status.append(errorBox(err));
      }
    });
    return true;
  }

  function adapterSelect(card, index) {
    const choices = adapterChoices(packs, ownAdapters, card.adapter);
    const select = element('select', { id: `stage-${index}-adapter` }, element('option', { value: '' }, 'Pick a step type'));
    for (const group of [...new Set(choices.map((choice) => choice.group))]) {
      select.append(element('optgroup', { label: group }, choices.filter((choice) => choice.group === group).map((choice) => element('option', { value: choice.value }, choice.value))));
    }
    select.value = card.adapter || '';
    select.addEventListener('change', () => {
      const next = [...cards];
      next[index] = { ...cards[index], adapter: select.value };
      commit(next, { redraw: true });
    });
    return select;
  }

  function withFields(card, index) {
    const adapter = adapters.get(card.adapter);
    const fields = adapter?.params ? fieldsFor(adapter.params) : null;
    const fieldset = element('fieldset', { class: 'stage-with' }, element('legend', {}, 'Parameters'));
    if (adapter?.description) fieldset.append(element('p', { class: 'note' }, adapter.description));
    if (fields?.length) {
      for (const field of fields) {
        fieldset.append(fieldInput(field, card.with[field.name], `stage-${index}-with`, (value) => {
          const nextWith = { ...cards[index].with };
          if (value === undefined) delete nextWith[field.name];
          else nextWith[field.name] = value;
          const next = [...cards];
          next[index] = { ...cards[index], with: nextWith };
          commit(next);
        }));
      }
      return fieldset;
    }
    if (fields) {
      fieldset.append(element('p', { class: 'note' }, 'This step type takes no parameters.'));
      return fieldset;
    }
    const id = `stage-${index}-with-json`;
    const errorSlot = element('span', { class: 'field-error', id: `${id}-error`, role: 'alert' });
    const textarea = element('textarea', { id, class: 'mono', rows: 4, spellcheck: 'false', 'aria-describedby': `${id}-error` });
    textarea.value = JSON.stringify(card.with, null, 2);
    textarea.addEventListener('change', () => {
      const parsed = parseJson(textarea.value.trim() || '{}');
      const isObject = !parsed.error && parsed.value !== null && typeof parsed.value === 'object' && !Array.isArray(parsed.value);
      errorSlot.textContent = isObject ? '' : 'Parameters must be a JSON object.';
      if (!isObject) return;
      const next = [...cards];
      next[index] = { ...cards[index], with: parsed.value };
      commit(next);
    });
    fieldset.append(element('div', { class: 'field' }, element('label', { for: id }, card.adapter ? 'Parameters as JSON. No schema is listed for this step type.' : 'Parameters as JSON'), textarea, errorSlot));
    return fieldset;
  }

  function textField(card, index, name, label, { numeric = false } = {}) {
    const id = `stage-${index}-${name}`;
    const input = element('input', { type: 'text', id, spellcheck: 'false', autocapitalize: 'none', inputmode: numeric ? 'numeric' : null });
    input.value = card[name] ?? '';
    const errorSlot = element('span', { class: 'field-error', role: 'alert' });
    input.addEventListener('change', () => {
      const raw = input.value.trim();
      if (name === 'id') {
        if (!commit(renameCard(cards, index, raw), { redraw: true })) input.value = cards[index].id;
        return;
      }
      let value = raw;
      if (numeric && raw !== '') {
        value = Number(raw);
        if (!Number.isInteger(value) || value < 1) {
          errorSlot.textContent = `${label} must be a whole number of 1 or more.`;
          return;
        }
      }
      errorSlot.textContent = '';
      const next = [...cards];
      next[index] = { ...cards[index], [name]: numeric && raw === '' ? null : value };
      commit(next);
    });
    return element('div', { class: 'field' }, element('label', { for: id }, label), input, errorSlot);
  }

  function dependsField(card, index) {
    const others = cards.filter((other) => other.id !== card.id && other.id);
    const fieldset = element('fieldset', { class: 'stage-depends' }, element('legend', {}, 'Runs after'));
    if (!others.length) {
      fieldset.append(element('p', { class: 'note' }, 'No other steps yet.'));
      return fieldset;
    }
    others.forEach((other, position) => {
      const id = `stage-${index}-dep-${position}`;
      const checkbox = element('input', { type: 'checkbox', id, checked: card.depends_on.includes(other.id) });
      checkbox.addEventListener('change', () => {
        const current = cards[index].depends_on.filter((dependency) => dependency !== other.id);
        const order = cards.map((each) => each.id);
        const depends = checkbox.checked ? [...current, other.id].sort((first, second) => order.indexOf(first) - order.indexOf(second)) : current;
        const next = [...cards];
        next[index] = { ...cards[index], depends_on: depends };
        commit(next);
      });
      fieldset.append(element('label', { class: 'check', for: id }, checkbox, ' ', element('span', { class: 'mono' }, other.id)));
    });
    return fieldset;
  }

  function stageCard(card, index) {
    const iconButton = (text, label, disabled, onClick) => element('button', { type: 'button', class: 'link-button', 'aria-label': label, disabled, onclick: onClick }, text);
    const failId = `stage-${index}-fail`;
    const failBox = element('input', { type: 'checkbox', id: failId, checked: card.fail_as_value });
    failBox.addEventListener('change', () => {
      const next = [...cards];
      next[index] = { ...cards[index], fail_as_value: failBox.checked };
      commit(next);
    });
    const head = element('div', { class: 'stage-head' },
      element('h3', {}, element('span', { class: 'muted' }, `${index + 1}. `), element('span', { class: 'mono' }, card.id || 'Unnamed step')),
      element('span', { class: 'row-actions' },
        iconButton('Up', `Move ${card.id} up`, index === 0, () => commit(moveCard(cards, index, -1), { redraw: true })),
        iconButton('Down', `Move ${card.id} down`, index === cards.length - 1, () => commit(moveCard(cards, index, 1), { redraw: true })),
        iconButton('Remove', `Remove ${card.id}`, false, async () => {
          if (await confirmDialog(`Remove ${card.id}?`, 'Steps that ran after it no longer wait for it.', 'Remove')) commit(removeCard(cards, index), { redraw: true });
        })));
    return element('li', { class: 'stage-card card' }, head,
      element('div', { class: 'stage-grid' },
        textField(card, index, 'id', 'ID'),
        element('div', { class: 'field' }, element('label', { for: `stage-${index}-adapter` }, 'Step type'), adapterSelect(card, index)),
        textField(card, index, 'fan_out', 'Parallel branches', { numeric: true }),
        textField(card, index, 'partition_source', 'Branch over'),
        textField(card, index, 'skip_if', 'Skip if'),
        element('div', { class: 'field field-check' }, element('label', { for: failId }, failBox, ' Keep going when this step fails'))),
      dependsField(card, index), withFields(card, index));
  }

  function draw() {
    clear(list);
    cards.forEach((card, index) => list.append(stageCard(card, index)));
    if (!cards.length) list.append(element('li', {}, empty('No steps yet.')));
  }

  addButton.addEventListener('click', () => {
    if (commit(addCard(cards), { redraw: true })) list.lastElementChild?.querySelector('input')?.focus();
  });

  async function show() {
    clear(root).append(loading());
    try {
      packs = await catalog().catch(() => []);
      adapters = new Map(packs.flatMap((pack) => (pack.adapters || []).map((adapter) => [adapter.ref, adapter])));
      const parsed = await api('/api/workflows/parse', { method: 'POST', body: { body: getBody() } });
      const errors = issueList(parsed?.errors || []);
      ownAdapters = parsed?.adapters || [];
      clear(root);
      if (errors.length && !(parsed?.stages || []).length) {
        root.append(element('div', { class: 'banner bad', role: 'alert' }, element('p', {}, 'The YAML has to parse before its steps can be edited here. Fix it on the YAML tab.'),
          element('ul', {}, errors.map((issue) => element('li', {}, issueText(issue))))));
        return;
      }
      cards = (parsed?.stages || []).map(cardFromStage);
      root.append(element('p', { class: 'note' }, 'Each change here rewrites the YAML on the YAML tab. Reordering or removing steps can move a comment that sits above a step.'), status, list, element('div', { class: 'wf-actions' }, addButton));
      draw();
    } catch (err) {
      clear(root).append(errorBox(err));
    }
  }

  return { root, show };
}
