export const DEFAULT_VIEW = 'overview';

function decode(part) {
  try {
    return decodeURIComponent(part);
  } catch {
    return part;
  }
}

export function parseHash(hash) {
  const raw = String(hash || '').replace(/^#\/?/, '');
  const queryAt = raw.indexOf('?');
  const pathPart = queryAt < 0 ? raw : raw.slice(0, queryAt);
  const parts = pathPart.split('/').filter(Boolean).map(decode);
  return { name: parts[0] || DEFAULT_VIEW, args: parts.slice(1), query: new URLSearchParams(queryAt < 0 ? '' : raw.slice(queryAt + 1)) };
}

export function hashFor(name, args = [], params = {}) {
  const path = [name, ...args].map((part) => encodeURIComponent(String(part))).join('/');
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== null && value !== undefined && value !== '') query.set(key, value);
  return `#/${path}${query.size ? `?${query}` : ''}`;
}
