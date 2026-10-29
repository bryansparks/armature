import assert from 'node:assert/strict';
import { test } from 'node:test';

// Just enough DOM for dom.js: elements, attributes, listeners, and text nodes.
class FakeNode {}
class FakeText extends FakeNode {
  constructor(text) {
    super();
    this.text = text;
  }
}
class FakeElement extends FakeNode {
  constructor(tag) {
    super();
    this.tag = tag;
    this.attributes = {};
    this.children = [];
    this.className = '';
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  addEventListener() {}

  append(...nodes) {
    this.children.push(...nodes);
  }
}
globalThis.Node = FakeNode;
globalThis.document = { createElement: (tag) => new FakeElement(tag), createTextNode: (text) => new FakeText(text) };

const STATIC = new URL('../../../armature/server/static/', import.meta.url);
const { parseMarkdown, renderMarkdown, safeHref } = await import(new URL('markdown.js', STATIC));
const { activeTab, artifactForTab, byteSize, orderArtifacts, outputText, prettyJson, progress, runTabs, safeLinkUrl, summaryLinks, tabIdFor } = await import(new URL('runpage.js', STATIC));

function elements(node, found = []) {
  if (node instanceof FakeElement) {
    found.push(node);
    node.children.forEach((child) => elements(child, found));
  }
  return found;
}

const textOf = (node) => (node instanceof FakeText ? node.text : node.children.map(textOf).join(''));
const tags = (markdown) => elements(renderMarkdown(markdown)).map((node) => node.tag);
const strip = (blocks) => JSON.parse(JSON.stringify(blocks));

test('headings, paragraphs, rules, and setext headings', () => {
  assert.deepEqual(strip(parseMarkdown('# One ##\n\nSome *text*\nnext line\n\n---\n\nTwo\n===\n\n### C#')), [
    { type: 'heading', level: 1, children: [{ type: 'text', text: 'One' }] },
    { type: 'paragraph', children: [{ type: 'text', text: 'Some ' }, { type: 'em', children: [{ type: 'text', text: 'text' }] }, { type: 'text', text: '\nnext line' }] },
    { type: 'hr' },
    { type: 'heading', level: 1, children: [{ type: 'text', text: 'Two' }] },
    { type: 'heading', level: 3, children: [{ type: 'text', text: 'C#' }] },
  ]);
  assert.deepEqual(tags('# Title\n\ntext'), ['div', 'h2', 'p']);
  assert.equal(parseMarkdown('#hashtag')[0].type, 'paragraph');
});

test('bold, italic, strikethrough, inline code, escapes, and hard breaks', () => {
  const [paragraph] = parseMarkdown('**b** __u__ *i* _j_ ***bi*** ~~gone~~ `a <b>` \\*not\\* snake_case_name a  \nb');
  const kinds = paragraph.children.map((node) => node.type);
  assert.deepEqual(kinds.filter((kind) => kind !== 'text'), ['strong', 'strong', 'em', 'em', 'strong', 'del', 'code', 'break']);
  assert.equal(paragraph.children.find((node) => node.type === 'code').text, 'a <b>');
  assert.match(textOf(renderMarkdown('\\*not\\* snake_case_name')), /^\*not\* snake_case_name$/);
  assert.deepEqual(strip(parseMarkdown('*a **b** c*')[0].children), [{ type: 'em', children: [
    { type: 'text', text: 'a ' }, { type: 'strong', children: [{ type: 'text', text: 'b' }] }, { type: 'text', text: ' c' }] }]);
  assert.equal(textOf(renderMarkdown('2 * 3 * 4 and ** alone')), '2 * 3 * 4 and ** alone');
});

test('fenced code keeps its text and language and never parses inside', () => {
  const [code] = parseMarkdown('```swift\nlet a = "<script>"\n**not bold**\n```');
  assert.deepEqual(code, { type: 'code', lang: 'swift', text: 'let a = "<script>"\n**not bold**' });
  assert.deepEqual(parseMarkdown('~~~\nopen to the end'), [{ type: 'code', lang: '', text: 'open to the end' }]);
  assert.deepEqual(parseMarkdown('    indented\n    code'), [{ type: 'code', lang: '', text: 'indented\ncode' }]);
  const pre = elements(renderMarkdown('```js\nx\n```')).find((node) => node.tag === 'code');
  assert.equal(pre.attributes['data-lang'], 'js');
});

test('lists nest, number from their start, and carry task items', () => {
  const [list] = parseMarkdown('3. three\n4. four\n   - nested\n     - deeper\n5. five');
  assert.equal(list.ordered, true);
  assert.equal(list.start, 3);
  assert.equal(list.items.length, 3);
  const nested = list.items[1].children[1];
  assert.equal(nested.type, 'list');
  assert.equal(nested.items[0].children[1].type, 'list');
  assert.equal(elements(renderMarkdown('3. a\n4. b')).find((node) => node.tag === 'ol').attributes.start, '3');
  const [tasks] = parseMarkdown('- [x] done\n- [ ] open\n- plain\n  - two spaces under a bullet');
  assert.deepEqual(tasks.items.map((item) => item.task), [true, false, null]);
  assert.equal(tasks.items[2].children[1].type, 'list');
  const boxes = elements(renderMarkdown('- [x] done\n- [ ] open')).filter((node) => node.tag === 'input');
  assert.deepEqual(boxes.map((box) => [box.attributes.type, 'checked' in box.attributes, 'disabled' in box.attributes]), [['checkbox', true, true], ['checkbox', false, true]]);
  assert.equal(parseMarkdown('1. a\n\n2. b')[0].tight, false);
  assert.equal(parseMarkdown('- a\n- b')[0].tight, true);
  assert.deepEqual(tags('- a\n- b'), ['div', 'ul', 'li', 'li']);
});

test('blockquotes nest and pipe tables keep alignment and escaped pipes', () => {
  const [quote] = parseMarkdown('> outer\n> > inner\n> back');
  assert.equal(quote.type, 'blockquote');
  assert.equal(quote.children[1].type, 'blockquote');
  const [table] = parseMarkdown('| Role | Decision | Why |\n|:-----|:-------:|----:|\n| security | **block** | a \\| b |\n| beauty |');
  assert.deepEqual(table.align, ['left', 'center', 'right']);
  assert.equal(table.rows.length, 2);
  assert.equal(table.rows[0][2][0].text, 'a | b');
  assert.deepEqual(table.rows[1][1], []);
  const cells = elements(renderMarkdown('| a | b |\n|:-:|--:|\n| 1 | 2 |')).filter((node) => node.tag === 'td');
  assert.deepEqual(cells.map((cell) => cell.className), ['align-center', 'align-right']);
  assert.deepEqual(cells.map((cell) => cell.attributes['data-label']), ['a', 'b']);
  assert.equal(parseMarkdown('a | b\nnot a rule')[0].type, 'paragraph');
});

test('links keep only http, https, mailto, and same-page hashes', () => {
  assert.equal(safeHref('https://example.com/a?b=1'), 'https://example.com/a?b=1');
  assert.equal(safeHref('http://example.com'), 'http://example.com/');
  assert.equal(safeHref('mailto:a@b.co'), 'mailto:a@b.co');
  assert.equal(safeHref('#findings'), '#findings');
  for (const bad of ['javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'java\tscript:alert(1)', ' javascript:alert(1)', 'data:text/html,<script>alert(1)</script>',
    'vbscript:x', 'file:///etc/passwd', '//evil.example', 'relative/path', '', '#a b', 'https://exa\u0000mple.com']) {
    assert.equal(safeHref(bad), null, bad);
  }
  const anchors = (markdown) => elements(renderMarkdown(markdown)).filter((node) => node.tag === 'a');
  const [good] = anchors('[docs](https://example.com "Title") and more');
  assert.deepEqual(good.attributes, { rel: 'noopener noreferrer', title: 'Title', target: '_blank', href: 'https://example.com/' });
  assert.equal(anchors('[jump](#top)')[0].attributes.target, undefined);
  for (const markdown of ['[x](javascript:alert(1))', '[x](data:text/html;base64,PHNjcmlwdD4=)', '<javascript:alert(1)>', '[x](<javascript:alert(1)>)', '[x](JAVASCRIPT:alert(1))']) {
    assert.deepEqual(anchors(markdown), [], markdown);
  }
  assert.equal(textOf(renderMarkdown('[click me](javascript:alert(1))')), 'click me');
  assert.equal(textOf(renderMarkdown('<javascript:alert(1)>')), '<javascript:alert(1)>');
  const [auto, email, bare] = anchors('<https://a.example/x> <me@b.example> see https://c.example/p(q)).');
  assert.deepEqual([auto.attributes.href, email.attributes.href, bare.attributes.href], ['https://a.example/x', 'mailto:me@b.example', 'https://c.example/p(q)']);
});

test('raw HTML and images stay text', () => {
  const html = '<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n<a href="javascript:alert(1)">x</a>';
  const rendered = renderMarkdown(html);
  assert.deepEqual(elements(rendered).map((node) => node.tag), ['div', 'p', 'p', 'p']);
  assert.match(textOf(rendered), /<script>alert\(1\)<\/script>/);
  const image = renderMarkdown('![build log](https://example.com/x.png) and ![](data:image/png;base64,AAAA)');
  assert.deepEqual(elements(image).map((node) => node.tag), ['div', 'p']);
  assert.equal(textOf(image), 'build log (https://example.com/x.png) and data:image/png;base64,AAAA');
  for (const node of elements(renderMarkdown('# <b onclick=x>t</b>\n\n| <i> | x |\n|-|-|\n| <svg onload=1> | y |'))) {
    assert.ok(!['b', 'i', 'svg', 'script', 'img', 'iframe'].includes(node.tag));
    assert.ok(Object.keys(node.attributes).every((name) => !name.startsWith('on')));
  }
});

test('deep nesting and unmatched markers stay bounded', () => {
  const quotes = parseMarkdown(`${'>'.repeat(200)} deep`);
  let depth = 0;
  for (let node = quotes[0]; node?.type === 'blockquote'; node = node.children[0]) depth += 1;
  assert.ok(depth <= 21);
  const started = Date.now();
  parseMarkdown(`${'*a '.repeat(20000)}${'[x'.repeat(20000)}${'`'.repeat(5000)}`);
  assert.ok(Date.now() - started < 3000);
  assert.ok(renderMarkdown(`${'- '.repeat(100)}x`));
});

test('run tabs put review.md first, then Output and Log, and keep reserved names apart', () => {
  const names = orderArtifacts(['notes.txt', 'review.md', 'log', 'a.json', 'review.md']);
  assert.deepEqual(names, ['review.md', 'a.json', 'log', 'notes.txt']);
  assert.deepEqual(runTabs(names).map((tab) => tab.id), ['review.md', 'a.json', '~log', 'notes.txt', 'output', 'log']);
  assert.equal(tabIdFor('output'), '~output');
  assert.equal(artifactForTab('~log', names), 'log');
  assert.equal(artifactForTab('log', names), null);
  assert.equal(artifactForTab('~notes.txt', names), null);
  assert.equal(activeTab(undefined, names), 'review.md');
  assert.equal(activeTab('output', names), 'output');
  assert.equal(activeTab('missing.md', names), 'review.md');
  assert.equal(activeTab(undefined, []), 'log');
});

test('the progress strip says in plain words what the run is doing', () => {
  const steps = [
    { step_id: 'resolve', status: 'succeeded' },
    { step_id: 'reviews', status: 'running', branch_total: 9, branches_done: ['0', '1'] },
    { step_id: 'security', parent_step: 'reviews', status: 'failed' },
    { step_id: 'team_lead', status: 'queued' },
  ];
  assert.deepEqual(progress(steps), { done: 1, total: 3, failed: 1, running: ['Reviewing: 2 of 9 specialists done'], text: 'Reviewing: 2 of 9 specialists done · 1 failed' });
  assert.equal(progress([{ step_id: 'team_lead', status: 'running' }, { step_id: 'build_app', status: 'running', branch_total: 2 }]).text,
    'Team lead deciding · Build app: 0 of 2 parts done');
  assert.equal(progress([{ step_id: 'resolve', status: 'succeeded' }, { step_id: 'post', status: 'queued' }]).text, '1 of 2 steps done');
  assert.equal(progress([]).text, 'Starting');
});

test('outputs read as text, JSON pretty-prints, and links and sizes are vetted', () => {
  assert.deepEqual(outputText('plain'), { text: 'plain', truncated: false });
  assert.deepEqual(outputText({ a: [1] }), { text: '{\n  "a": [\n    1\n  ]\n}', truncated: false });
  assert.deepEqual(outputText({ _truncated: true, value: 'cut' }), { text: 'cut', truncated: true });
  assert.deepEqual(outputText({ _truncated: true, a: 1 }), { text: '{\n  "a": 1\n}', truncated: true });
  assert.equal(outputText(null).text, '');
  assert.equal(prettyJson('{"a":1}'), '{\n  "a": 1\n}');
  assert.equal(prettyJson('not json'), 'not json');
  assert.equal(safeLinkUrl('javascript:alert(1)'), null);
  assert.equal(safeLinkUrl('//evil.example'), null);
  assert.equal(safeLinkUrl('/runs?x=1'), '/runs?x=1');
  assert.equal(safeLinkUrl('http://plain.example'), null);
  assert.deepEqual(summaryLinks({ links: [{ label: 'PR', url: 'https://github.com/o/r/pull/1' }, { label: 'Bad', url: 'data:x' }, { url: 'https://no-label.example' }],
    post_url: 'https://github.com/o/r/pull/1#review' }), [{ label: 'PR', url: 'https://github.com/o/r/pull/1' }, { label: 'Posted result', url: 'https://github.com/o/r/pull/1#review' }]);
  assert.deepEqual([byteSize(10), byteSize(2048), byteSize(3 * 1024 * 1024)], ['10 B', '2.0 KB', '3.00 MB']);
});

test('hostile unterminated links stay fast', async () => {
  const { inline } = await import('../../../armature/server/static/markdown.js');
  for (const text of ['[a](x ('.repeat(150000), '[a](<'.repeat(200000), '['.repeat(1000000)]) {
    const started = Date.now();
    inline(text);
    assert.ok(Date.now() - started < 2000);
  }
});
