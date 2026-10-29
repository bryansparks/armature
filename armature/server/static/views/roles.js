import { api, listOf } from '../api.js';
import { card, empty, errorBox, link, pageHeader, table, tabs, when } from '../dom.js';
import { hashFor } from '../router.js';
import { mayCreate } from '../scopes.js';
import { currentSession } from '../session.js';
import { detailPage, DOCUMENT, KIND_LABEL, newPage, scopeText, teamNames } from './definition.js';

const KINDS = [{ id: 'role', label: 'Roles' }, { id: 'prompt', label: 'Prompts' }, { id: 'mission', label: 'Missions' }, { id: 'skill', label: 'Skills' }];
const EMPTY = { role: 'No roles yet.', prompt: 'No prompts yet.', mission: 'No missions yet.', skill: 'No skills yet.' };

async function list(root, kindArg) {
  const kind = KINDS.some((candidate) => candidate.id === kindArg) ? kindArg : KINDS[0].id;
  root.append(pageHeader('Roles and prompts', mayCreate(currentSession())
    ? link(hashFor('new-document', [kind]), `New ${KIND_LABEL[kind].toLowerCase()}`, { class: 'btn' }) : null));
  const { tablist, panel } = tabs('#/roles', KINDS, kind, { label: 'Kind' });
  root.append(tablist, panel);
  let documents;
  try {
    documents = listOf(await api(`/api/documents?kind=${encodeURIComponent(kind)}`), 'documents');
  } catch (err) {
    panel.append(errorBox(err));
    return;
  }
  const names = await teamNames();
  panel.append(card(documents.length ? table([KIND_LABEL[kind], 'Scope', ['Version', 'num'], 'Updated'], documents.map((doc) => [
    link(hashFor('document', [doc.id]), doc.key || doc.name), scopeText(doc, names), String(doc.current_version), when(doc.updated_at),
  ])) : empty(EMPTY[kind])));
}

export async function render(root, { name, args }) {
  if (name === 'new-document') return newPage(root, DOCUMENT, KIND_LABEL[args[0]] ? args[0] : 'role');
  if (name === 'document' && args[0]) return detailPage(root, DOCUMENT, args[0], args[1]);
  return list(root, args[0]);
}
