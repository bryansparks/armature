// Reads the flat YAML front matter roles use, for display only. The server parses it for real.

export const SHOWN_FIELDS = ['name', 'applies_to', 'model', 'tools', 'mcp_servers'];

function unquote(value) {
  const text = value.trim();
  if (text.length >= 2 && (text[0] === '"' || text[0] === "'") && text.at(-1) === text[0]) return text.slice(1, -1);
  return text;
}

function stripComment(value) {
  let quote = null;
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    if (quote) {
      if (char === quote) quote = null;
    } else if (char === '"' || char === "'") quote = char;
    else if (char === '#' && (index === 0 || /\s/.test(value[index - 1]))) return value.slice(0, index);
  }
  return value;
}

function flowList(value) {
  const inner = value.trim().replace(/^\[/, '').replace(/\]$/, '');
  const items = [];
  let current = '';
  let quote = null;
  for (const char of inner) {
    if (quote) {
      if (char === quote) quote = null;
      current += char;
    } else if (char === '"' || char === "'") {
      quote = char;
      current += char;
    } else if (char === ',') {
      items.push(current);
      current = '';
    } else current += char;
  }
  items.push(current);
  return items.map(unquote).filter((item) => item !== '');
}

export function frontMatter(text) {
  const lines = String(text ?? '').split('\n');
  if (lines[0].trim() !== '---') return { found: false, fields: {} };
  const end = lines.findIndex((line, index) => index > 0 && line.trim() === '---');
  if (end < 0) return { found: true, fields: {}, error: 'The front matter has no closing ---.' };
  const fields = {};
  let listKey = null;
  for (const line of lines.slice(1, end)) {
    if (/^\s*(#.*)?$/.test(line)) continue;
    const item = /^\s*-\s+(.*)$/.exec(line);
    if (item && listKey) {
      fields[listKey].push(unquote(stripComment(item[1])));
      continue;
    }
    const pair = /^([A-Za-z_][\w-]*)\s*:(.*)$/.exec(line);
    if (!pair) continue;
    const value = stripComment(pair[2]).trim();
    listKey = value === '' ? pair[1] : null;
    if (value === '') fields[pair[1]] = [];
    else fields[pair[1]] = value.startsWith('[') ? flowList(value) : unquote(value);
  }
  return { found: true, fields, bodyLine: end + 2 };
}
