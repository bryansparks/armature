// No DOM access at import time, so the node tests can load this module.

let csrfToken = null;
const handlers = { signedOut: () => {}, stepUp: async () => null };

export class ApiError extends Error {
  constructor(status, message, payload = null) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

export function basePath(doc = globalThis.document) {
  const content = doc?.querySelector?.('meta[name="armature-base"]')?.getAttribute('content') || '';
  return content.replace(/\/+$/, '');
}

export const url = (path) => `${basePath()}${path}`;

export function setCsrf(token) {
  csrfToken = token || null;
}

export function configure({ signedOut, stepUp } = {}) {
  if (signedOut) handlers.signedOut = signedOut;
  if (stepUp) handlers.stepUp = stepUp;
}

// A 401 from these means a wrong password, code, or invite, not an ended session.
const CREDENTIAL_PATHS = /^\/api\/(login|step-up|account\/|invite\/)/;

export function outcome(status, payload, path = '') {
  if (status >= 200 && status < 300) return 'ok';
  if (status === 401) return CREDENTIAL_PATHS.test(path) ? 'error' : 'signed_out';
  if (status === 403 && payload?.error === 'step_up_required') return 'step_up';
  return 'error';
}

export function errorMessage(status, payload) {
  if (typeof payload?.error === 'string' && payload.error) return payload.error;
  return `Request failed (HTTP ${status}).`;
}

export async function api(path, { method = 'GET', body, retried = false } = {}) {
  const headers = { Accept: 'application/json' };
  if (method !== 'GET') {
    if (!csrfToken) csrfToken = (await api('/api/session')).csrf;
    headers['X-CSRF-Token'] = csrfToken;
    headers['Content-Type'] = 'application/json';
  }
  let response;
  try {
    response = await fetch(url(path), { method, headers, credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, 'Could not reach the server.');
  }
  const payload = await response.json().catch(() => null);
  const result = outcome(response.status, payload, path);
  if (result === 'ok') return payload;
  if (result === 'signed_out') handlers.signedOut();
  if (result === 'step_up' && !retried) {
    const proof = await handlers.stepUp();
    if (!proof) throw new ApiError(403, 'Cancelled. Nothing changed.', payload);
    await api('/api/step-up', { method: 'POST', body: proof, retried: true });
    return api(path, { method, body, retried: true });
  }
  throw new ApiError(response.status, errorMessage(response.status, payload), payload);
}

// The API may send a bare array or wrap it in an object keyed by the noun.
export function listOf(payload, key) {
  if (Array.isArray(payload)) return payload;
  return Array.isArray(payload?.[key]) ? payload[key] : [];
}
