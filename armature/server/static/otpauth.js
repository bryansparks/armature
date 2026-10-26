// No DOM access at import time, so the node tests can load this module.

export function secretFromOtpauth(uri) {
  const queryAt = String(uri || '').indexOf('?');
  if (queryAt < 0) return '';
  return new URLSearchParams(String(uri).slice(queryAt + 1)).get('secret') || '';
}

export function tokenFromHash(hash) {
  const token = String(hash || '').replace(/^#/, '');
  return /^[A-Za-z0-9_-]{20,64}$/.test(token) ? token : '';
}
