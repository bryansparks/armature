import { api, listOf } from '../api.js';
import { card, element, empty, errorBox, meta, pageHeader, table } from '../dom.js';
import { fieldsFor } from '../schema.js';

const KIND_TEXT = { string: 'Text', number: 'Number', integer: 'Whole number', boolean: 'Yes or no', enum: 'One of', strings: 'List of text', json: 'JSON' };

export function paramRows(schema) {
  return fieldsFor(schema).map((field) => ({
    name: field.name,
    type: field.kind === 'enum' ? `One of ${field.options.join(', ')}` : KIND_TEXT[field.kind],
    required: field.required,
    default: field.default === undefined ? '' : JSON.stringify(field.default),
    description: field.description,
  }));
}

export const outputNames = (outputs) => Object.keys(outputs?.properties || {});

const listText = (value) => (Array.isArray(value) ? value.join(', ') : value && typeof value === 'object' ? Object.keys(value).join(', ') : value || '');

function adapterBlock(adapter) {
  const rows = paramRows(adapter.params);
  return element('section', { class: 'adapter' },
    element('h3', {}, element('span', { class: 'mono' }, adapter.ref)),
    adapter.description ? element('p', {}, adapter.description) : null,
    meta(adapter.timeout ? `Timeout ${adapter.timeout}s` : null, adapter.writes ? 'Writes' : 'Read-only',
      listText(adapter.secrets) ? `Secrets: ${listText(adapter.secrets)}` : null, outputNames(adapter.outputs).length ? `Outputs: ${outputNames(adapter.outputs).join(', ')}` : null),
    rows.length ? table(['Parameter', 'Type', 'Required', 'Default', 'Description'], rows.map((row) => [
      element('span', { class: 'mono' }, row.name), row.type, row.required ? 'Yes' : 'No', row.default ? element('span', { class: 'mono' }, row.default) : '—', row.description || '—',
    ])) : element('p', { class: 'note' }, 'No parameters.'));
}

export async function render(root) {
  root.append(pageHeader('Packs'));
  root.append(element('p', { class: 'note' }, 'Installed packs and the step types they provide. Only an admin installs or upgrades a pack, from the command line.'));
  let payload;
  try {
    payload = await api('/api/packs');
  } catch (err) {
    root.append(errorBox(err));
    return;
  }
  const errors = listOf(payload, 'errors');
  if (errors.length) {
    root.append(element('div', { class: 'banner bad', role: 'alert' }, element('p', {}, 'Some packs could not load.'),
      element('ul', {}, errors.map((problem) => element('li', {}, typeof problem === 'string' ? problem : `${problem.name || problem.pack || 'Pack'}: ${problem.message || problem.error || ''}`)))));
  }
  const packs = listOf(payload, 'packs');
  if (!packs.length) {
    root.append(card(empty('No packs installed.')));
    return;
  }
  for (const pack of packs) {
    root.append(card(element('h2', { class: 'inline' }, pack.name, ' ', element('span', { class: 'muted' }, pack.version || '')),
      pack.description ? element('p', {}, pack.description) : null,
      meta(pack.source ? element('span', { class: 'mono wrap-anywhere' }, pack.source) : null, pack.pinned_ref ? element('span', {}, 'Pinned to ', element('span', { class: 'mono' }, pack.pinned_ref)) : null),
      (pack.adapters || []).length ? (pack.adapters || []).map(adapterBlock) : empty('This pack has no step types.')));
  }
}
