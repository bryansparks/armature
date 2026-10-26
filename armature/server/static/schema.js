import { element } from './dom.js';

const SIMPLE = new Set(['string', 'number', 'integer', 'boolean']);

export function fieldKind(property) {
  if (Array.isArray(property?.enum) && property.enum.length && property.enum.every((option) => ['string', 'number'].includes(typeof option))) return 'enum';
  const types = Array.isArray(property?.type) ? property.type.filter((type) => type !== 'null') : [property?.type];
  if (types.length !== 1) return 'json';
  const [type] = types;
  if (SIMPLE.has(type)) return type;
  if (type === 'array' && property.items?.type === 'string' && !property.items.enum) return 'strings';
  return 'json';
}

export function fieldsFor(schema) {
  const required = new Set(Array.isArray(schema?.required) ? schema.required : []);
  return Object.entries(schema?.properties || {}).map(([name, property]) => ({
    name, label: property?.title || name, description: property?.description || '', required: required.has(name),
    kind: fieldKind(property), options: fieldKind(property) === 'enum' ? property.enum : null, default: property?.default,
  }));
}

// Where JSON.parse failed, as a line and column, read from the position V8 reports.
export function parseJson(text) {
  try {
    return { value: JSON.parse(text) };
  } catch (err) {
    const at = /position (\d+)/.exec(err.message);
    if (!at) return { error: err.message, line: null, col: null };
    const before = text.slice(0, Number(at[1])).split('\n');
    return { error: err.message, line: before.length, col: before.at(-1).length + 1 };
  }
}

const isTemplate = (raw) => typeof raw === 'string' && raw.includes('{{');

// The typed value for a field's raw input. Undefined means leave the key out.
export function readField(field, raw) {
  if (field.kind === 'boolean') return { value: Boolean(raw) };
  const text = typeof raw === 'string' ? raw : String(raw ?? '');
  if (text.trim() === '') return field.required ? { error: `${field.label} is required.` } : { value: undefined };
  if (isTemplate(text) && field.kind !== 'json') return { value: text.trim() };
  if (field.kind === 'number' || field.kind === 'integer') {
    const number = Number(text.trim());
    if (!Number.isFinite(number)) return { error: `${field.label} must be a number.` };
    if (field.kind === 'integer' && !Number.isInteger(number)) return { error: `${field.label} must be a whole number.` };
    return { value: number };
  }
  if (field.kind === 'enum') {
    const option = field.options.find((candidate) => String(candidate) === text);
    return option === undefined ? { error: `${field.label} must be one of the listed values.` } : { value: option };
  }
  if (field.kind === 'strings') return { value: text.split('\n').map((line) => line.trim()).filter(Boolean) };
  if (field.kind === 'json') {
    const parsed = parseJson(text);
    return parsed.error ? { error: `${field.label} is not valid JSON: ${parsed.error}` } : { value: parsed.value };
  }
  return { value: text };
}

export function displayValue(field, value) {
  if (field.kind === 'boolean') return Boolean(value ?? field.default);
  if (value === undefined || value === null) return '';
  if (field.kind === 'strings' && Array.isArray(value)) return value.join('\n');
  if (field.kind === 'json') return JSON.stringify(value, null, 2);
  return String(value);
}

export function fieldInput(field, value, idPrefix, onCommit) {
  const id = `${idPrefix}-${field.name}`;
  const errorSlot = element('span', { class: 'field-error', id: `${id}-error`, role: 'alert' });
  const shown = displayValue(field, value);
  let input;
  if (field.kind === 'boolean') {
    input = element('input', { type: 'checkbox', id, checked: shown });
  } else if (field.kind === 'enum') {
    input = element('select', { id }, field.required ? null : element('option', { value: '' }, '—'),
      field.options.map((option) => element('option', { value: String(option) }, String(option))));
    input.value = shown;
  } else if (field.kind === 'strings' || field.kind === 'json') {
    input = element('textarea', { id, class: 'mono', rows: Math.min(8, Math.max(2, shown.split('\n').length)), spellcheck: 'false' });
    input.value = shown;
  } else {
    const numeric = field.kind === 'number' || field.kind === 'integer';
    input = element('input', { type: 'text', id, inputmode: numeric && !isTemplate(shown) ? 'decimal' : null, spellcheck: 'false', autocapitalize: 'none' });
    input.value = shown;
  }
  input.setAttribute('aria-describedby', `${id}-error`);
  input.addEventListener('change', () => {
    const result = readField(field, field.kind === 'boolean' ? input.checked : input.value);
    errorSlot.textContent = result.error || '';
    input.setAttribute('aria-invalid', result.error ? 'true' : 'false');
    if (!result.error) onCommit(result.value);
  });
  const hint = [field.kind === 'strings' ? 'One per line.' : '', field.description].filter(Boolean).join(' ');
  const labelText = `${field.label}${field.required ? ' (required)' : ''}`;
  if (field.kind === 'boolean') {
    return element('div', { class: 'field field-check' }, element('label', { for: id }, input, ' ', labelText), hint ? element('span', { class: 'note' }, hint) : null, errorSlot);
  }
  return element('div', { class: 'field' }, element('label', { for: id }, labelText), input, hint ? element('span', { class: 'note' }, hint) : null, errorSlot);
}
