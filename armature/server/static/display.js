import { api } from './api.js';

export const DISPLAY_DEFAULTS = { show_cost: true, show_man_hours: true, human_multiplier: 1, timezone: 'America/Denver', workflow_labels: {}, hidden_actors: [] };
export const SERIES_COLORS = ['s1', 's2', 's3', 's4', 's5', 's6', 's7', 's8'];
export const OTHER = 'other';

export async function loadDisplay() {
  try {
    return { ...DISPLAY_DEFAULTS, ...((await api('/api/display'))?.display || {}) };
  } catch {
    return { ...DISPLAY_DEFAULTS };
  }
}

const entryFor = (display, workflow) => (Object.hasOwn(display?.workflow_labels || {}, workflow) ? display.workflow_labels[workflow] || {} : {});

export function workflowLabel(display, workflow) {
  if (workflow === OTHER) return 'Other';
  return entryFor(display, workflow).label || workflow || 'Unnamed workflow';
}

// A configured color wins. Otherwise the series takes the slot for its place in the server's order.
export function workflowColor(display, workflow, order = []) {
  if (workflow === OTHER) return OTHER;
  const configured = entryFor(display, workflow).color;
  if (SERIES_COLORS.includes(configured)) return configured;
  const position = order.indexOf(workflow);
  return position >= 0 && position < SERIES_COLORS.length ? SERIES_COLORS[position] : OTHER;
}

export function hours(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  return `${Number(value).toLocaleString('en-US', { maximumFractionDigits: 1 })} h`;
}
