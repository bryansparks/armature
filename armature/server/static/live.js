import { url } from './api.js';

export const MAX_FAILURES = 3;

function parse(data) {
  try {
    return JSON.parse(data);
  } catch {
    return null;
  }
}

// EventSource retries and resumes from Last-Event-ID on its own. After MAX_FAILURES in a row it gives up and the caller polls.
export function subscribe(path, { events, onState = () => {}, onOpen = () => {}, onFallback = () => {} }, { signal } = {}) {
  if (typeof EventSource !== 'function') {
    onState('polling');
    onFallback();
    return { close() {} };
  }
  const source = new EventSource(url(path));
  let failures = 0;
  let closed = false;
  const close = () => {
    closed = true;
    source.close();
  };
  for (const [name, handler] of Object.entries(events)) {
    source.addEventListener(name, (message) => {
      const data = parse(message.data);
      if (data && !closed) handler(data);
    });
  }
  source.addEventListener('open', () => {
    failures = 0;
    onState('live');
    onOpen();
  });
  source.addEventListener('error', () => {
    if (closed) return;
    failures += 1;
    if (failures >= MAX_FAILURES || source.readyState === EventSource.CLOSED) {
      close();
      onState('polling');
      onFallback();
    } else {
      onState('reconnecting');
    }
  });
  signal?.addEventListener('abort', close);
  return { close };
}

export function every(milliseconds, task, signal) {
  let timer = 0;
  let stopped = false;
  const tick = async () => {
    if (stopped) return;
    let keepGoing = true;
    try {
      keepGoing = (await task()) !== false;
    } catch {}
    if (!stopped && keepGoing) timer = setTimeout(tick, milliseconds);
  };
  const stop = () => {
    stopped = true;
    clearTimeout(timer);
  };
  signal?.addEventListener('abort', stop);
  timer = setTimeout(tick, milliseconds);
  return { stop };
}
