import assert from 'node:assert/strict';
import { test } from 'node:test';

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { indentEdit, issueList, issuesByLine, issueText, lineCount, lineOfOffset, offsetOfLine, scrollTopForLine } = await import(new URL('editor.js', STATIC));
const { hasUnsaved, mayLeave, protect, release } = await import(new URL('guard.js', STATIC));
const { diffRows } = await import(new URL('diff.js', STATIC));
const { displayValue, fieldKind, fieldsFor, parseJson, readField } = await import(new URL('schema.js', STATIC));
const { addCard, adapterChoices, afterRender, cardFromStage, moveCard, removeCard, renameCard, renderPayload, stagePayload, stageProblems } = await import(new URL('stages.js', STATIC));
const { frontMatter } = await import(new URL('frontmatter.js', STATIC));
const { creatableScopes, mayCreate } = await import(new URL('scopes.js', STATIC));
const { aliasesFrom, aliasRows, providerBody } = await import(new URL('views/providers.js', STATIC));
const { scopeParam, settingText } = await import(new URL('views/settings.js', STATIC));
const { outputNames, paramRows } = await import(new URL('views/packs.js', STATIC));

const apply = (text, edit) => text.slice(0, edit.from) + edit.insert + text.slice(edit.to);

test('gutter math maps offsets and lines both ways', () => {
  const text = 'a: 1\nbb: 2\n\nccc: 3';
  assert.equal(lineCount(text), 4);
  assert.equal(lineCount(''), 1);
  assert.equal(lineOfOffset(text, 0), 1);
  assert.equal(lineOfOffset(text, 5), 2);
  assert.equal(lineOfOffset(text, text.length), 4);
  assert.equal(offsetOfLine(text, 2), 5);
  assert.equal(offsetOfLine(text, 2, 3), 7);
  assert.equal(offsetOfLine(text, 2, 99), 10);
  assert.equal(offsetOfLine(text, 99), 12);
  assert.equal(offsetOfLine(text, 0), 0);
  assert.equal(scrollTopForLine(1, 20, 300), 0);
  assert.equal(scrollTopForLine(51, 20, 300), 900);
});

test('issues sort by line, clamp past the end, and keep unplaced ones', () => {
  const issues = issueList([{ line: 9, col: 2, message: 'late', code: 'E2' }, { line: null, message: 'whole file' }], [{ line: 2, message: 'style' }], 5);
  assert.deepEqual(issues.map((issue) => [issue.line, issue.severity]), [[null, 'error'], [2, 'warning'], [5, 'error']]);
  assert.equal(issueText(issues[2]), 'Line 5, column 2: late (E2)');
  assert.equal(issueText(issues[0]), 'whole file');
  assert.equal(issueText({ line: 4, message: 'Pack field is frozen.', code: 'FROZEN_FIELD', path: 'stages[1].adapter' }), 'Line 4: Only an admin can change stages[1].adapter.');
  assert.equal(issueText({ line: null, message: 'Field is frozen.', code: 'FROZEN_FIELD' }), 'Field is frozen. (FROZEN_FIELD)');
  assert.equal(issueText({ line: null, message: 'Unsafe template.', code: 'UNSAFE_TEMPLATE', path: 'stages[0].skip_if' }), 'Unsafe template. (UNSAFE_TEMPLATE, at stages[0].skip_if)');
  const byLine = issuesByLine(issueList([{ line: 3, message: 'x' }], [{ line: 3, message: 'y' }, { line: 4, message: 'z' }]));
  assert.deepEqual([...byLine], [[3, 'error'], [4, 'warning']]);
  assert.deepEqual(issueList(undefined, null), []);
});

test('Tab inserts two spaces at the cursor', () => {
  const edit = indentEdit('ab', 1, 1);
  assert.deepEqual(edit, { from: 1, to: 1, insert: '  ', selStart: 3, selEnd: 3 });
  assert.equal(apply('ab', edit), 'a  b');
});

test('Tab on a selection indents each line and keeps it selected', () => {
  const text = 'one\ntwo\n\nthree\nfour';
  const end = text.indexOf('three') + 2;
  const edit = indentEdit(text, 1, end);
  assert.equal(apply(text, edit), '  one\n  two\n\n  three\nfour');
  assert.equal(edit.selStart, 3);
  assert.equal(edit.selEnd, end + 6);
});

test('a selection ending at a line start leaves that line alone', () => {
  const text = 'one\ntwo\nthree';
  const edit = indentEdit(text, 0, 8);
  assert.equal(apply(text, edit), '  one\n  two\nthree');
});

test('Shift+Tab outdents by up to two spaces and does nothing at the margin', () => {
  const text = '    a\n b\nc';
  const edit = indentEdit(text, 0, text.length, true);
  assert.equal(apply(text, edit), '  a\nb\nc');
  assert.equal(edit.selStart, 0);
  assert.equal(edit.selEnd, text.length - 3);
  assert.equal(indentEdit('abc', 1, 1, true), null);
  const cursor = indentEdit('  abc', 1, 1, true);
  assert.equal(apply('  abc', cursor), 'abc');
  assert.equal(cursor.selStart, 0);
});

test('the dirty guard asks only when there are unsaved edits', async () => {
  release();
  let asked = 0;
  assert.equal(await mayLeave(async () => { asked += 1; return false; }), true);
  assert.equal(asked, 0);
  let dirty = true;
  protect(() => dirty);
  assert.equal(hasUnsaved(), true);
  assert.equal(await mayLeave(async () => { asked += 1; return false; }), false);
  assert.equal(hasUnsaved(), true);
  assert.equal(await mayLeave(async () => { asked += 1; return true; }), true);
  assert.equal(asked, 2);
  assert.equal(hasUnsaved(), false);
  protect(() => dirty);
  dirty = false;
  assert.equal(await mayLeave(async () => { asked += 1; return false; }), true);
  assert.equal(asked, 2);
  release();
});

test('diffRows numbers each side and treats header lines as meta', () => {
  const diff = '--- a/workflow.yml\n+++ b/workflow.yml\n@@ -1,3 +1,3 @@\n name: x\n-old: 1\n+new: 1\n+--- not a header\n stages: []\n';
  const rows = diffRows(diff);
  assert.deepEqual(rows.map((row) => [row.kind, row.old, row.new, row.sign]), [
    ['meta', null, null, ''], ['meta', null, null, ''], ['hunk', null, null, ''],
    ['ctx', 1, 1, ' '], ['del', 2, null, '-'], ['add', null, 2, '+'], ['add', null, 3, '+'], ['ctx', 3, 4, ' '],
  ]);
  assert.equal(rows[6].text, '--- not a header');
  assert.deepEqual(diffRows(['@@ -5 +5 @@', '-a', '+b']).map((row) => [row.old, row.new]), [[null, null], [5, null], [null, 5]]);
  assert.deepEqual(diffRows(''), []);
});

test('schema fields cover the supported kinds and fall back to JSON', () => {
  const schema = {
    type: 'object', required: ['prompt'],
    properties: {
      prompt: { type: 'string', title: 'Prompt', description: 'A prompt document' },
      rounds: { type: 'integer', default: 3 },
      ratio: { type: ['number', 'null'] },
      strict: { type: 'boolean' },
      model: { enum: ['review', 'lead'] },
      paths: { type: 'array', items: { type: 'string' } },
      matrix: { type: 'object' },
      either: { type: ['string', 'integer'] },
    },
  };
  const fields = fieldsFor(schema);
  assert.deepEqual(fields.map((field) => [field.name, field.kind, field.required]), [
    ['prompt', 'string', true], ['rounds', 'integer', false], ['ratio', 'number', false], ['strict', 'boolean', false],
    ['model', 'enum', false], ['paths', 'strings', false], ['matrix', 'json', false], ['either', 'json', false],
  ]);
  assert.equal(fields[0].label, 'Prompt');
  assert.equal(fields[1].default, 3);
  assert.deepEqual(fields[4].options, ['review', 'lead']);
  assert.equal(fieldKind({ type: 'array', items: { type: 'string', enum: ['a'] } }), 'json');
  assert.deepEqual(fieldsFor(null), []);
});

test('readField types each input and refuses bad ones', () => {
  const field = (kind, extra = {}) => ({ name: 'x', label: 'X', kind, required: false, options: null, ...extra });
  assert.deepEqual(readField(field('integer'), '4'), { value: 4 });
  assert.ok(readField(field('integer'), '4.5').error);
  assert.deepEqual(readField(field('number'), ' 2.5 '), { value: 2.5 });
  assert.ok(readField(field('number'), 'abc').error);
  assert.deepEqual(readField(field('number'), '{{ assignments.count }}'), { value: '{{ assignments.count }}' });
  assert.deepEqual(readField(field('integer'), ''), { value: undefined });
  assert.ok(readField(field('string', { required: true }), '  ').error);
  assert.deepEqual(readField(field('boolean'), true), { value: true });
  assert.deepEqual(readField(field('enum', { options: [1, 2] }), '2'), { value: 2 });
  assert.ok(readField(field('enum', { options: ['a'] }), 'b').error);
  assert.deepEqual(readField(field('strings'), 'a\n\n b \n'), { value: ['a', 'b'] });
  assert.deepEqual(readField(field('json'), '{"a": [1]}'), { value: { a: [1] } });
  assert.ok(readField(field('json'), '{a}').error);
  assert.equal(displayValue(field('strings'), ['a', 'b']), 'a\nb');
  assert.equal(displayValue(field('json'), { a: 1 }), '{\n  "a": 1\n}');
  assert.equal(displayValue(field('boolean', { default: true }), undefined), true);
  assert.equal(displayValue(field('integer'), null), '');
});

test('parseJson reports where it failed', () => {
  assert.deepEqual(parseJson('[1, 2]'), { value: [1, 2] });
  const failed = parseJson('{\n  "a": 1,\n}');
  assert.ok(failed.error);
  assert.equal(failed.line, 3);
  assert.equal(failed.col, 1);
});

const PARSED = [
  { id: 'resolve', adapter: 'github-review/resolve-pr', depends_on: [], with: {}, fan_out: null, partition_source: null, fail_as_value: false, skip_if: null, line: 4 },
  { id: 'reviews', adapter: 'github-review/role-review', depends_on: ['resolve'], with: { model: 'review' }, fan_out: 9, partition_source: '{{ resolve.jobs }}', fail_as_value: false, skip_if: null, line: 6 },
  { id: 'lead', adapter: 'github-review/team-lead', depends_on: ['reviews'], with: { prompt: 'prompt/team-lead' }, fan_out: null, partition_source: null, fail_as_value: true, skip_if: '{{ resolve.skip }}', line: 12 },
];
const withoutLine = ({ line, ...rest }) => ({ id: rest.id, prev_id: rest.id, ...rest });

test('stage cards round-trip the parse shape into the render payload', () => {
  const cards = PARSED.map(cardFromStage);
  assert.deepEqual(cards.map(stagePayload), PARSED.map(withoutLine));
  assert.deepEqual(renderPayload('name: x\n', cards), { body: 'name: x\n', stages: PARSED.map(withoutLine) });
  assert.equal(cards[2].line, 12);
  const sparse = cardFromStage({ id: 'only' });
  assert.deepEqual(stagePayload(sparse), { id: 'only', prev_id: 'only', adapter: null, depends_on: [], with: {}, fan_out: null, partition_source: null, fail_as_value: false, skip_if: null });
  cards[1].with.model = 'lead';
  assert.equal(PARSED[1].with.model, 'review');
});

test('moving, renaming, removing, and adding cards keep dependencies whole', () => {
  const cards = PARSED.map(cardFromStage);
  assert.deepEqual(moveCard(cards, 2, -1).map((card) => card.id), ['resolve', 'lead', 'reviews']);
  assert.equal(moveCard(cards, 0, -1), cards);
  const renamed = renameCard(cards, 0, 'fetch');
  assert.deepEqual(renamed.map((card) => [card.id, card.depends_on]), [['fetch', []], ['reviews', ['fetch']], ['lead', ['reviews']]]);
  assert.deepEqual(removeCard(cards, 1).map((card) => [card.id, card.depends_on]), [['resolve', []], ['lead', []]]);
  const added = addCard(cards);
  assert.equal(added.length, 4);
  assert.equal(added[3].id, 'step-4');
  assert.deepEqual(added[3].depends_on, ['lead']);
  assert.equal(addCard([cardFromStage({ id: 'step-1' })])[1].id, 'step-2');
  assert.deepEqual(addCard([])[0].depends_on, []);
  assert.equal(added[3].prev_id, null);
  assert.equal(renamed[0].prev_id, 'resolve');
});

test('prev_id names the stage as the YAML has it, through renames and adds', () => {
  let cards = renameCard(addCard(PARSED.map(cardFromStage)), 0, 'fetch');
  const sent = renderPayload('', cards).stages;
  assert.deepEqual(sent.map((stage) => [stage.id, stage.prev_id]), [['fetch', 'resolve'], ['reviews', 'reviews'], ['lead', 'lead'], ['step-4', null]]);
  cards = afterRender(cards, sent);
  assert.deepEqual(cards.map((card) => [card.id, card.prev_id]), [['fetch', 'fetch'], ['reviews', 'reviews'], ['lead', 'lead'], ['step-4', 'step-4']]);
  const later = renameCard(cards, 0, 'get');
  assert.deepEqual(afterRender(later, renderPayload('', cards).stages).map((card) => [card.id, card.prev_id]).slice(0, 1), [['get', 'fetch']]);
  const twice = renameCard(renameCard(PARSED.map(cardFromStage), 0, 'a'), 0, 'b');
  assert.deepEqual(renderPayload('', twice).stages.map((stage) => [stage.id, stage.prev_id]), [['b', 'resolve'], ['reviews', 'reviews'], ['lead', 'lead']]);
  const failed = renameCard(cards, 0, 'again');
  assert.equal(renderPayload('', failed).stages[0].prev_id, 'fetch');
});

test('stageProblems catches empty, duplicate, and self-referencing ids', () => {
  assert.deepEqual(stageProblems(PARSED.map(cardFromStage)), []);
  const cards = [cardFromStage({ id: 'a' }), cardFromStage({ id: 'a' }), cardFromStage({ id: '' }), cardFromStage({ id: 'b', depends_on: ['b'] })];
  assert.deepEqual(stageProblems(cards), ['Two steps are named a.', 'Step 3 needs an ID.', 'b cannot depend on itself.']);
});

test('adapterChoices lists pack adapters, then the workflow\'s own, then the current one', () => {
  const catalog = [{ name: 'github-review', adapters: [{ ref: 'github-review/resolve-pr' }, { ref: 'github-review/team-lead' }] }];
  assert.deepEqual(adapterChoices(catalog, ['local-script', 'github-review/team-lead'], 'legacy/thing'), [
    { value: 'github-review/resolve-pr', group: 'github-review' }, { value: 'github-review/team-lead', group: 'github-review' },
    { value: 'local-script', group: 'This workflow' }, { value: 'legacy/thing', group: 'Other' },
  ]);
  assert.deepEqual(adapterChoices(null, null, ''), []);
});

test('frontMatter reads flow lists, block lists, quotes, and comments', () => {
  const text = '---\nname: security\nmodel: review   # an alias\napplies_to: [ios, "android", backend]\ntools:\n  - Read\n  - "Bash(git -C:*)"\nmcp_servers: []\n---\n\nYou are the reviewer.';
  const parsed = frontMatter(text);
  assert.deepEqual(parsed.fields, { name: 'security', model: 'review', applies_to: ['ios', 'android', 'backend'], tools: ['Read', 'Bash(git -C:*)'], mcp_servers: [] });
  assert.equal(parsed.bodyLine, 10);
  assert.equal(frontMatter('# Just a prompt').found, false);
  assert.ok(frontMatter('---\nname: x\n').error);
  assert.deepEqual(frontMatter('---\nname: "a # b"\n---').fields, { name: 'a # b' });
});

test('creatableScopes offers only what the user leads', () => {
  const teams = [{ id: 't1', name: 'Mobile' }, { id: 't2', name: 'Web' }];
  const projects = new Map([['t1', [{ id: 'p1', name: 'ios' }]], ['t2', [{ id: 'p2', name: 'site' }]]]);
  const lead = { user: { id: 'u' }, memberships: [{ scope_kind: 'team', scope_id: 't1', role: 'team_lead' }, { scope_kind: 'project', scope_id: 'p9', scope_name: 'Other app', role: 'project_lead' }, { scope_kind: 'project', scope_id: 'p2', role: 'viewer' }] };
  assert.deepEqual(creatableScopes(lead, teams, projects).map((scope) => [scope.scope_kind, scope.scope_id, scope.label]), [
    ['team', 't1', 'Mobile'], ['project', 'p1', 'Mobile / ios'], ['project', 'p9', 'Other app'],
  ]);
  const admin = creatableScopes({ user: { is_admin: true }, memberships: [] }, teams, projects);
  assert.deepEqual(admin.map((scope) => scope.scope_id), [null, 't1', 'p1', 't2', 'p2']);
  assert.deepEqual(creatableScopes({ user: { id: 'v' }, memberships: [{ scope_kind: 'project', scope_id: 'p2', role: 'contributor' }] }, teams, projects), []);
  assert.equal(mayCreate(lead), true);
  assert.equal(mayCreate({ user: { id: 'v' }, memberships: [{ scope_kind: 'team', scope_id: 't1', role: 'viewer' }] }), false);
});

test('provider aliases and body keep only what the kind uses', () => {
  assert.deepEqual(aliasRows({ review: 'sonnet', lead: 'opus' }), [{ alias: 'review', model: 'sonnet' }, { alias: 'lead', model: 'opus' }]);
  assert.deepEqual(aliasesFrom([{ alias: ' review ', model: 'sonnet' }, { alias: '', model: '' }]), { aliases: { review: 'sonnet' } });
  assert.ok(aliasesFrom([{ alias: 'a', model: 'x' }, { alias: 'a', model: 'y' }]).error);
  assert.ok(aliasesFrom([{ alias: '', model: 'x' }]).error);
  assert.ok(aliasesFrom([{ alias: 'a', model: ' ' }]).error);
  const { body } = providerBody('bedrock', { region: 'us-east-1', api_base: 'stale' }, [{ alias: 'lead', model: 'opus' }], 'AWS_TOKEN', { region: 'old', credential_secret: 'OLD', profile_hint: 'kept' });
  assert.deepEqual(body, { kind: 'bedrock', config: { profile_hint: 'kept', region: 'us-east-1' }, aliases: { lead: 'opus' }, credential_secret: 'AWS_TOKEN' });
  assert.deepEqual(providerBody('anthropic', {}, [], '').body, { kind: 'anthropic', config: {}, aliases: {}, credential_secret: null });
  assert.ok(providerBody('nope', {}, [], '').error);
});

test('settings scope and value helpers', () => {
  assert.equal(scopeParam('*'), 'instance');
  assert.equal(scopeParam(null), 'instance');
  assert.equal(scopeParam('t1'), 't1');
  assert.equal(settingText({ a: [1] }), '{\n  "a": [\n    1\n  ]\n}');
  assert.equal(settingText(undefined), 'null');
});

test('pack params render as rows', () => {
  assert.deepEqual(paramRows({ required: ['prompt'], properties: { prompt: { type: 'string', description: 'Doc id' }, model: { enum: ['review', 'lead'], default: 'review' } } }), [
    { name: 'prompt', type: 'Text', required: true, default: '', description: 'Doc id' },
    { name: 'model', type: 'One of review, lead', required: false, default: '"review"', description: '' },
  ]);
  assert.deepEqual(paramRows(undefined), []);
  assert.deepEqual(outputNames({ type: 'object', properties: { findings: {}, synced: {} } }), ['findings', 'synced']);
  assert.deepEqual(outputNames(['old']), []);
  assert.deepEqual(outputNames(undefined), []);
});

test('Used by names each scope an override applies to', async () => {
  const { effectiveText } = await import(new URL('views/definition.js', STATIC));
  assert.equal(effectiveText([{ scope_kind: 'team', scope_id: 't1', scope_name: 'Mobile' }, { scope_kind: 'project', scope_id: 'p1', scope_name: 'android-tree' }]), 'Mobile, android-tree');
  assert.equal(effectiveText([]), '—');
  assert.equal(effectiveText(undefined), '—');
});

test('rollback names the version shown as current and reads a conflict like save does', async () => {
  const { conflictOf, rollbackBody } = await import(new URL('views/definition.js', STATIC));
  const { ApiError } = await import(new URL('api.js', STATIC));
  assert.deepEqual(rollbackBody(2, 5), { version: 2, note: 'Roll back to version 2', base_version: 5 });
  assert.deepEqual(conflictOf(new ApiError(409, 'conflict', { error: 'conflict', current_version: 6 })), { current: 6 });
  assert.deepEqual(conflictOf(new ApiError(409, 'conflict')), { current: null });
  assert.equal(conflictOf(new ApiError(422, 'invalid', {})), null);
  assert.equal(conflictOf(new Error('network')), null);
});
