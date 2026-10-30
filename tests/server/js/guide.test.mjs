import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { composeDescription } = await import(new URL('views/guide.js', STATIC));
const { INSTRUCTION_MAX } = await import(new URL('assistant.js', STATIC));

test('a description has every answered part in order', () => {
  assert.equal(composeDescription({ trigger: 'A pull request opens.', steps: '1. Read it.\n2. Comment.', output: 'One comment.', never: 'Never push.' }),
    'Starts when: A pull request opens.\n\nSteps:\n1. Read it.\n2. Comment.\n\nProduces: One comment.\n\nNever: Never push.');
});

test('empty and blank parts are left out', () => {
  assert.equal(composeDescription({ trigger: '   ', steps: 'Read it.', output: '', never: undefined }), 'Steps:\nRead it.');
  assert.equal(composeDescription({}), '');
  assert.equal(composeDescription(), '');
});

test('answers are trimmed', () => {
  assert.equal(composeDescription({ trigger: '  A push. ', steps: '\n Build it. \n' }), 'Starts when: A push.\n\nSteps:\nBuild it.');
});

test('a description fits in one instruction', () => {
  assert.equal(composeDescription({ steps: 'a'.repeat(INSTRUCTION_MAX * 2) }).length, INSTRUCTION_MAX);
});
