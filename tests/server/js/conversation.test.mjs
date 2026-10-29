import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { answered, conversationPath, draftAfter, latestReply, speaker, waitForAnswer } = await import(new URL('conversation.js', STATIC));
const { webConversation } = await import(new URL('views/subject.js', STATIC));

const reply = (seq) => ({ seq, role: 'assistant', text: `reply ${seq}` });
const asked = (seq) => ({ seq, role: 'user', author: 'u1', text: 'hi' });

test('a turn has answered once nothing runs and a newer reply is there', () => {
  assert.equal(answered({ working: true, messages: [asked(1), reply(2)] }, 1), false);
  assert.equal(answered({ working: false, messages: [asked(1)] }, 1), false);
  assert.equal(answered({ working: false, messages: [reply(1), asked(2)] }, 2), false);
  assert.equal(answered({ working: false, messages: [asked(1), reply(2)] }, 1), true);
  assert.equal(latestReply({ messages: [reply(2), asked(3)] }).seq, 2);
});

test('waitForAnswer polls until the reply comes and gives up after the wait', async () => {
  const seen = [{ working: true, messages: [asked(1)] }, { working: false, messages: [asked(1), reply(2)] }];
  const answer = await waitForAnswer('c1', 1, { fetch: async () => ({ conversation: seen.shift() }), sleep: async () => {} });
  assert.equal(answer.messages.at(-1).seq, 2);
  let clock = 0;
  await assert.rejects(waitForAnswer('c1', 1, { fetch: async () => ({ conversation: { working: true, messages: [] } }),
    sleep: async () => {}, now: () => (clock += 10 * 60 * 1000) }), /No answer yet/);
});

test('speakers, paths, and which threads are web conversations', () => {
  assert.equal(speaker(asked(1), 'u1'), 'You');
  assert.equal(speaker({ role: 'user', author: 'U0SLACK' }, 'u1'), 'U0SLACK');
  assert.equal(speaker(reply(2), 'u1'), 'Armature');
  assert.equal(conversationPath('a/b', true), '/api/conversations/a%2Fb/messages');
  assert.equal(webConversation('web:0f6c2b7e-6a1d-4c55-9a8e-2f1d3c4b5a69'), '0f6c2b7e-6a1d-4c55-9a8e-2f1d3c4b5a69');
  assert.equal(webConversation('slack:C1:1.2'), null);
});

test('a draft shows only when the reply that brought it is newer than the message sent', () => {
  const conversation = { draft: { body: 'x', seq: 2, explanation: 'old' }, messages: [asked(1), reply(2), asked(3)] };
  assert.equal(draftAfter(conversation, 3), null);
  assert.deepEqual(draftAfter(conversation, 1), { body: 'x', seq: 2, explanation: 'reply 2' });
  assert.equal(draftAfter({ draft: { body: 'x' }, messages: [] }, 0), null);
  assert.equal(draftAfter({ messages: [] }, 0), null);
});
