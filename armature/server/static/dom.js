// Server data reaches the page only through createElement and text nodes, never innerHTML.

const SAFE_HREF = /^(#|https:\/\/|\.\/)/;

export function element(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === 'class') node.className = value;
    else if (name.startsWith('on') && typeof value === 'function') node.addEventListener(name.slice(2), value);
    else if (name === 'href' && !(typeof value === 'string' && SAFE_HREF.test(value))) continue;
    else node.setAttribute(name, value === true ? '' : String(value));
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function money(amountUsd, digits = 2) {
  if (amountUsd === null || amountUsd === undefined) return '—';
  const amount = Number(amountUsd);
  if (!Number.isFinite(amount)) return '—';
  return `$${amount.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

export function duration(seconds) {
  let wholeSeconds = Number(seconds);
  if (!Number.isFinite(wholeSeconds) || wholeSeconds <= 0) return '—';
  wholeSeconds = Math.round(wholeSeconds);
  if (wholeSeconds < 60) return `${wholeSeconds}s`;
  if (wholeSeconds < 3600) return `${Math.floor(wholeSeconds / 60)}m ${wholeSeconds % 60}s`;
  return `${Math.floor(wholeSeconds / 3600)}h ${Math.floor((wholeSeconds % 3600) / 60)}m`;
}

export function secondsBetween(startTime, endTime) {
  if (!startTime || !endTime) return null;
  return (new Date(endTime).getTime() - new Date(startTime).getTime()) / 1000;
}

export function ago(timestamp) {
  if (!timestamp) return '—';
  const secondsAgo = (Date.now() - new Date(timestamp).getTime()) / 1000;
  if (secondsAgo < 60) return `${Math.max(0, Math.round(secondsAgo))}s ago`;
  if (secondsAgo < 3600) return `${Math.floor(secondsAgo / 60)}m ago`;
  if (secondsAgo < 86400) return `${Math.floor(secondsAgo / 3600)}h ago`;
  return `${Math.floor(secondsAgo / 86400)}d ago`;
}

export function when(timestamp) {
  if (!timestamp) return '—';
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return '—';
  const pad = (number) => String(number).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

const STATUS_TONE = {
  succeeded: 'ok', success: 'ok', done: 'ok', completed: 'ok',
  failed: 'bad', error: 'bad', died: 'bad', lost: 'warn',
  running: 'running', queued: 'queued', canceled: 'unknown', cancelled: 'unknown', waiting: 'warn',
  approved: 'ok', rejected: 'bad', expired: 'unknown',
};

export const statusTone = (status) => STATUS_TONE[status] || 'unknown';

export const statusText = (status) => (status ? status.charAt(0).toUpperCase() + status.slice(1).replace(/_/g, ' ') : 'Unknown');

export function statusChip(status) {
  return element('span', { class: `state state-${statusTone(status)}` }, statusText(status));
}

export function link(href, text, attributes = {}) {
  return element('a', { href, ...attributes }, text);
}

export function pageHeader(title, ...extras) {
  return element('header', { class: 'page' }, element('h1', {}, title), ...extras);
}

export function tableRow(headers, row) {
  return element('tr', {}, row.map((cell, columnIndex) => {
    const [label, cellClass] = Array.isArray(headers[columnIndex]) ? headers[columnIndex] : [headers[columnIndex], null];
    return element('td', { class: cellClass, 'data-label': typeof label === 'string' && label ? label : null }, cell);
  }));
}

export function table(headers, rows) {
  const tableNode = element('table');
  const head = element('thead', {}, element('tr', {}, headers.map((header) => {
    const [label, cellClass] = Array.isArray(header) ? header : [header, null];
    return element('th', { class: cellClass, scope: 'col' }, label);
  })));
  const body = element('tbody');
  for (const row of rows) body.append(tableRow(headers, row));
  tableNode.append(head, body);
  return tableNode;
}

export function card(...children) {
  return element('div', { class: 'card' }, ...children);
}

export function meta(...items) {
  const shown = items.flat().filter((item) => item !== null && item !== undefined && item !== '' && item !== false);
  return element('div', { class: 'meta' }, shown.map((item) => element('span', {}, item)));
}

let shouldFocusActiveTab = false;

// Each tab is its own hash route, so it is linkable and the back button works. Activation follows focus.
export function tabs(baseHash, tabList, activeId, { label = 'Sections' } = {}) {
  const tabIds = tabList.map((tab) => tab.id);
  const tablist = element('div', { class: 'tabs', role: 'tablist', 'aria-label': label });
  const panel = element('div', { class: 'tabpanel', role: 'tabpanel', id: 'panel-active', 'aria-labelledby': `tab-${activeId}`, tabindex: 0 });
  for (const tab of tabList) {
    const isActive = tab.id === activeId;
    const tabLink = element('a', {
      href: `${baseHash}/${encodeURIComponent(tab.id)}`, role: 'tab', id: `tab-${tab.id}`, 'aria-selected': String(isActive), 'aria-controls': isActive ? 'panel-active' : null,
      tabindex: isActive ? 0 : -1, class: `tab${isActive ? ' active' : ''}`,
    }, tab.label);
    tabLink.addEventListener('click', () => {
      shouldFocusActiveTab = true;
    });
    tablist.append(tabLink);
  }
  tablist.addEventListener('keydown', (keyEvent) => {
    const currentIndex = tabIds.indexOf(activeId);
    const moves = { ArrowRight: currentIndex + 1, ArrowLeft: currentIndex - 1, Home: 0, End: tabIds.length - 1 };
    if (!(keyEvent.key in moves)) return;
    keyEvent.preventDefault();
    const nextIndex = (moves[keyEvent.key] + tabIds.length) % tabIds.length;
    shouldFocusActiveTab = true;
    location.hash = `${baseHash}/${encodeURIComponent(tabIds[nextIndex])}`;
  });
  const focusActive = shouldFocusActiveTab;
  shouldFocusActiveTab = false;
  // On a phone the tab row scrolls sideways, so a deep-linked tab could start out of view.
  requestAnimationFrame(() => {
    const activeTab = tablist.querySelector('[aria-selected="true"]');
    if (!activeTab) return;
    if (activeTab.offsetLeft + activeTab.offsetWidth > tablist.clientWidth) tablist.scrollLeft = activeTab.offsetLeft - 16;
    if (focusActive) activeTab.focus();
  });
  return { tablist, panel };
}

export function empty(text) {
  return element('div', { class: 'empty' }, text);
}

export function loading() {
  return element('div', { class: 'loading', role: 'status' }, 'Loading…');
}

export function errorBox(err) {
  return element('div', { class: 'error-box', role: 'alert' }, err?.message || 'Something went wrong.');
}

// Runs one change, shows its error in status, and keeps the button off while it runs.
export async function attempt(status, button, change) {
  clear(status);
  if (button) button.disabled = true;
  try {
    return (await change()) ?? true;
  } catch (err) {
    status.append(errorBox(err));
    return null;
  } finally {
    if (button) button.disabled = false;
  }
}

export function confirmDialog(title, consequence, verb) {
  return new Promise((resolve) => {
    const cancelButton = element('button', { type: 'button' }, 'Cancel');
    const confirmButton = element('button', { type: 'button', class: 'primary' }, verb);
    const dialog = element('dialog', { class: 'confirm', 'aria-labelledby': 'confirm-title' },
      element('h2', { id: 'confirm-title' }, title), element('p', {}, consequence), element('div', { class: 'dialog-actions' }, cancelButton, confirmButton));
    const close = (isConfirmed) => {
      dialog.close();
      dialog.remove();
      resolve(isConfirmed);
    };
    cancelButton.addEventListener('click', () => close(false));
    confirmButton.addEventListener('click', () => close(true));
    dialog.addEventListener('cancel', (cancelEvent) => {
      cancelEvent.preventDefault();
      close(false);
    });
    document.body.append(dialog);
    dialog.showModal();
    cancelButton.focus();
  });
}

// Asks for the password and a fresh code, once, for a change the server wants confirmed.
export function stepUpDialog() {
  return new Promise((resolve) => {
    const passwordInput = element('input', { type: 'password', id: 'step-up-password', autocomplete: 'current-password', required: true });
    const codeInput = element('input', { type: 'text', id: 'step-up-code', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: 6, pattern: '[0-9]{6}', required: true });
    const cancelButton = element('button', { type: 'button' }, 'Cancel');
    const form = element('form', { class: 'stacked-form' },
      element('label', { class: 'stacked', for: passwordInput.id }, 'Password', passwordInput),
      element('label', { class: 'stacked', for: codeInput.id }, 'Authenticator code', codeInput),
      element('div', { class: 'dialog-actions' }, cancelButton, element('button', { type: 'submit', class: 'primary' }, 'Confirm')));
    const dialog = element('dialog', { class: 'confirm', 'aria-labelledby': 'step-up-title' },
      element('h2', { id: 'step-up-title' }, 'Confirm it is you'),
      element('p', {}, 'This change needs your password and a fresh code from your authenticator.'), form);
    const close = (value) => {
      passwordInput.value = '';
      dialog.close();
      dialog.remove();
      resolve(value);
    };
    cancelButton.addEventListener('click', () => close(null));
    form.addEventListener('submit', (submitEvent) => {
      submitEvent.preventDefault();
      close({ password: passwordInput.value, totp: codeInput.value.replace(/\s+/g, '') });
    });
    dialog.addEventListener('cancel', (cancelEvent) => {
      cancelEvent.preventDefault();
      close(null);
    });
    document.body.append(dialog);
    dialog.showModal();
    passwordInput.focus();
  });
}
