import { api } from './api.js';
import { card, clear, element, errorBox, when } from './dom.js';

export const POLL_MS = 2000;
export const WAIT_MS = 15 * 60 * 1000;
export const TEXT_MAX = 8192;
const WHO = { assistant: 'Armature', system: 'Armature' };

export const conversationPath = (id, messages = false) => `/api/conversations/${encodeURIComponent(id)}${messages ? '/messages' : ''}`;

// A turn has answered once nothing runs and a message newer than the one sent is there.
export function answered(conversation, sentSeq) {
  if (!conversation || conversation.working) return false;
  return (conversation.messages || []).some((message) => message.role !== 'user' && message.seq > sentSeq);
}

export const latestReply = (conversation) => [...(conversation?.messages || [])].reverse().find((message) => message.role !== 'user') || null;

export function speaker(message, ownerId) {
  if (WHO[message.role]) return WHO[message.role];
  return message.author === ownerId ? 'You' : (message.author || 'Someone');
}

const pause = (ms, signal) => new Promise((resolve) => {
  const timer = setTimeout(resolve, ms);
  signal?.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
});

// Polls until the turn for the message at sentSeq has answered. The turn runs on a runner, never on the server.
export async function waitForAnswer(conversationId, sentSeq, { signal, fetch = api, now = Date.now, sleep = pause } = {}) {
  const started = now();
  while (!signal?.aborted) {
    const conversation = (await fetch(conversationPath(conversationId)))?.conversation;
    if (answered(conversation, sentSeq) || conversation?.state === 'closed') return conversation;
    if (now() - started > WAIT_MS) throw new Error('No answer yet. The runner may be busy, so look again in a while.');
    await sleep(POLL_MS, signal);
  }
  return null;
}

// A draft counts only when the reply that brought it is newer than the message sent, never an earlier turn's.
export function draftAfter(conversation, sentSeq) {
  const draft = conversation?.draft;
  if (!draft?.body || !(draft.seq > sentSeq)) return null;
  const reply = (conversation.messages || []).find((message) => message.seq === draft.seq);
  return { ...draft, explanation: reply?.text || draft.explanation };
}

// Sends to the workflow assistant and waits for its turn. The draft comes back as the proposal the panels show.
export async function askAssistant(body, { conversationId = null, signal } = {}) {
  const started = await api('/api/assist', { method: 'POST', body: conversationId ? { ...body, conversation_id: conversationId } : body });
  const conversation = await waitForAnswer(started.conversation.id, started.seq, { signal });
  if (!conversation) return null;
  const reply = latestReply(conversation);
  return { conversation, reply: reply?.text || '', proposal: draftAfter(conversation, started.seq) };
}

function messageItem(message, ownerId) {
  return element('li', { class: `chat-message chat-${message.role}` },
    element('div', { class: 'chat-head' }, element('strong', {}, speaker(message, ownerId)), ' ', element('span', { class: 'muted' }, when(message.created_at))),
    element('p', { class: 'pre-line' }, message.text),
    message.error ? element('p', { class: 'note bad' }, `Not sent to Slack: ${message.error}`) : null);
}

// A web conversation as a chat: its messages, and a box to write the next one while it is open.
export function chatPanel(conversationId, signal) {
  const list = element('ol', { class: 'chat-list', 'aria-live': 'polite', 'aria-label': 'Messages' });
  const state = element('p', { class: 'note', role: 'status' });
  const input = element('textarea', { id: `chat-${conversationId}`, rows: 3, maxlength: TEXT_MAX });
  const send = element('button', { type: 'button', class: 'primary' }, 'Send');
  const status = element('div', { role: 'status' });
  const form = element('div', { class: 'stacked-form' }, element('label', { class: 'stacked', for: input.id }, 'Your message', input), element('div', { class: 'wf-actions' }, send), status);
  let shown = null;

  function show(conversation) {
    shown = conversation;
    clear(list).append(...(conversation.messages || []).map((message) => messageItem(message, conversation.owner_id)));
    if (conversation.state === 'closed') state.textContent = 'This conversation is closed.';
    else state.textContent = conversation.working ? 'Armature is working on a reply…' : 'Waiting for your message.';
    form.hidden = conversation.state === 'closed' || conversation.kind !== 'web';
  }

  async function refresh() {
    try {
      show((await api(conversationPath(conversationId))).conversation);
    } catch (err) {
      clear(status).append(errorBox(err));
    }
  }

  send.addEventListener('click', async () => {
    const text = input.value.trim();
    if (!text) return;
    send.disabled = true;
    clear(status);
    try {
      const posted = (await api(conversationPath(conversationId, true), { method: 'POST', body: { text } })).conversation;
      input.value = '';
      show(posted);
      const sent = Math.max(...posted.messages.map((message) => message.seq));
      const answer = await waitForAnswer(conversationId, sent, { signal });
      if (answer) show(answer);
    } catch (err) {
      status.append(errorBox(err));
    } finally {
      send.disabled = false;
    }
  });

  refresh().then(async () => {
    if (!shown?.working) return;
    const answer = await waitForAnswer(conversationId, Math.max(0, ...shown.messages.map((message) => message.seq)), { signal }).catch(() => null);
    if (answer) show(answer);
  });
  return card(element('h2', { class: 'inline' }, 'Conversation'), list, state, form);
}
