// The run page's tabs, progress strip, and output text, kept free of the DOM so the node tests can drive them.
import { branchText } from './feed.js';

export const OUTPUT_TAB = 'output';
export const LOG_TAB = 'log';
const RESERVED = new Set([OUTPUT_TAB, LOG_TAB]);
const FIRST_ARTIFACT = 'review.md';
const DONE = new Set(['succeeded', 'skipped', 'failed', 'canceled', 'lost']);
const TRUNCATED = '_truncated';

// An artifact may be named like a built-in tab, so its tab id then takes a prefix no artifact name can start with.
export const tabIdFor = (name) => (RESERVED.has(name) ? `~${name}` : name);

export function artifactForTab(tabId, names) {
  const name = String(tabId ?? '').replace(/^~/, '');
  return names.includes(name) && tabIdFor(name) === tabId ? name : null;
}

export function orderArtifacts(names) {
  const unique = [...new Set(names.filter((name) => typeof name === 'string' && name))];
  return unique.sort((left, right) => (left === FIRST_ARTIFACT ? -1 : right === FIRST_ARTIFACT ? 1 : left.localeCompare(right)));
}

export function runTabs(names) {
  return [...names.map((name) => ({ id: tabIdFor(name), label: name })), { id: OUTPUT_TAB, label: 'Output' }, { id: LOG_TAB, label: 'Log' }];
}

// The first artifact when the run has any, else the log.
export function activeTab(requested, names) {
  if (requested === OUTPUT_TAB || requested === LOG_TAB || artifactForTab(requested, names)) return requested;
  return names.length ? tabIdFor(names[0]) : LOG_TAB;
}

export function progress(steps) {
  const top = (steps || []).filter((step) => !step.parent_step);
  const done = top.filter((step) => DONE.has(step.status)).length;
  const failed = (steps || []).filter((step) => step.status === 'failed').length;
  const running = top.filter((step) => step.status === 'running').map((step) => {
    const branches = branchText(step);
    return branches ? `${step.step_id} (${branches})` : step.step_id;
  });
  const parts = [top.length ? `${done}/${top.length} steps done` : 'No steps yet'];
  if (running.length) parts.push(`running ${running.join(', ')}`);
  if (failed) parts.push(`${failed} failed`);
  return { done, total: top.length, failed, running, text: parts.join(' · ') };
}

// A stored output as readable text: strings as they are, anything else as indented JSON.
export function outputText(output) {
  if (output === null || output === undefined) return { text: '', truncated: false };
  let value = output;
  let truncated = false;
  if (value && typeof value === 'object' && !Array.isArray(value) && Object.hasOwn(value, TRUNCATED)) {
    truncated = true;
    const { [TRUNCATED]: _, ...rest } = value;
    value = Object.keys(rest).length === 1 && Object.hasOwn(rest, 'value') ? rest.value : rest;
  }
  return { text: typeof value === 'string' ? value : JSON.stringify(value, null, 2), truncated };
}

export function prettyJson(text) {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return String(text ?? '');
  }
}

// https links, or paths on this server. Anything else from a run's summary is left out.
export function safeLinkUrl(value) {
  if (typeof value !== 'string' || !value || /[\s\u0000-\u001f\u007f\\]/.test(value)) return null;
  if (value.startsWith('/')) return value.startsWith('//') ? null : value;
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'https:' && parsed.hostname ? parsed.href : null;
  } catch {
    return null;
  }
}

export function summaryLinks(summary) {
  const links = Array.isArray(summary?.links) ? summary.links : [];
  const shown = [];
  const seen = new Set();
  for (const candidate of [...links, summary?.post_url ? { label: 'Posted result', url: summary.post_url } : null]) {
    const href = safeLinkUrl(candidate?.url);
    if (!href || seen.has(href) || typeof candidate.label !== 'string' || !candidate.label) continue;
    seen.add(href);
    shown.push({ label: candidate.label, url: href });
  }
  return shown;
}

export function byteSize(bytes) {
  const count = Number(bytes);
  if (!Number.isFinite(count) || count < 0) return '—';
  if (count < 1024) return `${count} B`;
  if (count < 1024 * 1024) return `${(count / 1024).toFixed(1)} KB`;
  return `${(count / 1024 / 1024).toFixed(2)} MB`;
}

export function downloadType(mediaType) {
  return `${mediaType || 'text/plain'};charset=utf-8`;
}
