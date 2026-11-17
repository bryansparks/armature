// "How is Mei Chen doing?" One number per area, a 13-week trend against the team median, and the work behind it.
import { api } from '../../api.js';
import { confirmDialog, element } from '../../dom.js';
import { chartSkeleton, trendChart } from './chart.js';
import {
  categoryDef, categoryName, changeTypeName, fixed, isNum, monthDay, repoShort, short,
} from './format.js';
import { daysFrom, page } from './index.js';
import { VIEWS } from './people.js';
import {
  avatar, emptyLine, errorState, filters, noPermission, periodSelect, ranked, section, select, sortableTable, tile,
} from './ui.js';
import { personAnswer, scopeLine, teamMedianText } from './words.js';

export const QUESTION = 'How is this person doing?';

// The main measure of each area, shown as a tile and drawn as the trend.
export const AREAS = {
  shipping: { area: 'shipping', key: 'merged', label: 'Merged pull requests', unit: 'count', digits: 0, better: 'up', change: 'percent', trend: 'Merged pull requests per week', def: 'Pull requests this person merged in the period.' },
  quality: { area: 'quality', key: 'rounds_per_pr', label: 'Review rounds per pull request', unit: 'rounds', digits: 1, better: 'down', change: 'absolute', trend: 'Review rounds per pull request, by week', def: 'Average review rounds on this person’s merged pull requests. Fewer is better.' },
  review: { area: 'review', key: 'reviews_given', label: 'Reviews given', unit: 'count', digits: 0, better: 'up', change: 'percent', trend: 'Reviews given per week', def: 'Reviews this person submitted on other people’s pull requests.' },
  unblocking: { area: 'unblocking', key: 'hotspot_prs', label: 'Hotspot fixes', unit: 'count', digits: 0, better: 'up', change: 'percent', trend: 'Hotspot fixes per week', def: 'Merged pull requests that changed a file that keeps causing problems.' },
};

// The Person route takes a person id or a GitHub login.
export const isPersonId = (value) => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(String(value || ''));

const WAITING = { review: 'A review', author: 'The author', merge: 'Merge' };
const REVIEW_STATE = { approved: 'Approved', changes_requested: 'Asked for changes', commented: 'Commented', dismissed: 'Dismissed' };

export function render(body, ctx) {
  const { query, links } = ctx;
  const days = daysFrom(query);
  const personId = ctx.args[1] || '';
  const tab = AREAS[query.get('tab')] ? query.get('tab') : 'shipping';
  const filterBar = filters(ctx.teamControl(), periodSelect(days, (value) => ctx.setQuery({ days: value })));
  const who = avatar('');
  return page(body, ctx, {
    question: 'How is this person doing?', filterBar, who, what: 'this person',
    skeleton: () => [
      element('div', { class: 'in-tiles', 'aria-hidden': 'true' }, Object.values(AREAS).map((measure) => tile({ measure, value: undefined, days }))),
      section({ title: AREAS[tab].trend }, chartSkeleton()),
    ],
    load: async () => {
      try {
        if (isPersonId(personId)) return await ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/people/${encodeURIComponent(personId)}?days=${days}`);
        return await ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/people/by-login/${encodeURIComponent(personId)}?days=${days}`);
      } catch (err) {
        if (err.status === 403 || err.status === 404) return { denied: err.status };
        throw err;
      }
    },
    draw: (data, head) => {
      if (data.denied) {
        head.title.textContent = data.denied === 404 ? 'No such person' : 'How is this person doing?';
        head.answerNode.replaceChildren(data.denied === 404 ? 'This person isn’t on the team, or was merged into someone else.' : 'People pages are visible to team leads and to the person themself.');
        return data.denied === 404 ? element('p', { class: 'in-empty' }, element('a', { class: 'in-link', href: links.people({ days }) }, 'Open People'))
          : noPermission('Ask an admin for the team lead role to see other people’s pages.');
      }
      const name = data.person?.name || 'Unknown';
      const question = `How is ${name} doing?`;
      head.title.textContent = question;
      document.title = `${question} | Armature`;
      who.textContent = avatar(name).textContent;
      head.answerNode.replaceChildren(personAnswer(data));
      const repos = (data.person?.repos || []).map(repoShort);
      const scope = scopeLine({ scope: data.scope });
      head.scopeNode.replaceChildren(repos.length ? `Works in ${listWords(repos)}. ${scope}` : scope);
      const nodes = [tiles(data, days)];
      const id = data.person?.id || personId;
      // A page opened by id shows the readable login address once it is known.
      if (isPersonId(personId) && data.person?.login) {
        ctx.pathArgs = ['person', data.person.login];
        history.replaceState(null, '', `${location.pathname}${location.search}${ctx.links.person(data.person, Object.fromEntries(ctx.query))}`);
      }
      nodes.push(areaTabs(tab, data, ctx, days, id, { id, login: data.person?.login }));
      nodes.push(openSection(data, ctx));
      if (ctx.lead) nodes.push(adminSection(data, ctx, id));
      return nodes;
    },
  });
}

function listWords(items) {
  if (items.length < 3) return items.join(' and ');
  return `${items.slice(0, -1).join(', ')}, and ${items[items.length - 1]}`;
}

function tiles(data, days) {
  return element('div', { class: 'in-tiles' }, Object.values(AREAS).map((measure) => tile({
    measure, days, value: data[measure.area]?.[measure.key] ?? null, previous: data.previous?.[measure.area]?.[measure.key] ?? null,
    ref: teamMedianText(data.team_median?.[measure.area]?.[measure.key], measure.unit === 'count' || measure.unit === 'rounds' ? '' : measure.unit, measure.digits),
  })));
}

function areaTabs(current, data, ctx, days, personId, who) {
  const holder = element('div');
  const list = element('div', { class: 'in-views', role: 'tablist', 'aria-label': 'Areas' });
  const panel = element('div', { role: 'tabpanel', id: 'in-area-panel' });
  const choose = (id, focus = false) => {
    for (const node of list.children) {
      const on = node.dataset.view === id;
      node.setAttribute('aria-selected', String(on));
      node.tabIndex = on ? 0 : -1;
      if (on && focus) node.focus();
    }
    panel.setAttribute('aria-labelledby', `in-area-${id}`);
    panel.replaceChildren(...areaPanel(id, data, ctx, days, personId));
    ctx.setQuery({ tab: id === 'shipping' ? '' : id }, { reload: false });
  };
  for (const [id, label] of VIEWS) {
    const node = element('a', { href: ctx.links.person(who, { days, tab: id }), role: 'tab', id: `in-area-${id}`, 'data-view': id, 'aria-controls': 'in-area-panel' }, label);
    node.addEventListener('click', (clickEvent) => {
      clickEvent.preventDefault();
      choose(id);
    });
    list.append(node);
  }
  list.addEventListener('keydown', (keyEvent) => {
    const ids = VIEWS.map(([id]) => id);
    const index = ids.indexOf(list.querySelector('[aria-selected="true"]')?.dataset.view);
    const moves = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: ids.length - 1 };
    if (!(keyEvent.key in moves)) return;
    keyEvent.preventDefault();
    choose(ids[(moves[keyEvent.key] + ids.length) % ids.length], true);
  });
  holder.append(list, panel);
  for (const node of list.children) {
    const on = node.dataset.view === current;
    node.setAttribute('aria-selected', String(on));
    node.tabIndex = on ? 0 : -1;
  }
  panel.setAttribute('aria-labelledby', `in-area-${current}`);
  panel.append(...areaPanel(current, data, ctx, days, personId));
  return holder;
}

function areaPanel(id, data, ctx, days, personId) {
  const measure = AREAS[id];
  const name = data.person?.name || '';
  const weeks = data.trend?.weeks || [];
  const mine = data.trend?.person?.[id] || [];
  const team = data.trend?.team_median?.[id] || [];
  const format = (value) => short(value, '', measure.digits || (id === 'quality' ? 1 : 0));
  const out = [];
  if (weeks.length && (mine.some(isNum) || team.some(isNum))) {
    out.push(section({ title: measure.trend, takeaway: trendWords(measure, mine, team, name) },
      trendChart({
        dates: weeks, label: measure.trend, basis: 'Each point is one week, over the last 13 weeks', legendLabel: 'Series', format,
        series: [{ name, slot: 1, values: mine }, { name: 'Team median', slot: 'median', values: team }],
        dateFormat: (iso) => monthDay(iso, false),
      })));
  } else out.push(section({ title: measure.trend }, emptyLine('Not enough history to draw a trend yet.')));
  if (id === 'shipping') out.push(mergedSection(data, ctx, days));
  if (id === 'quality') out.push(flaggedSection(data, ctx, days, personId));
  if (id === 'review') out.push(reviewsSection(data, ctx, days));
  if (id === 'unblocking') out.push(hotspotSection(data, ctx, days));
  return out;
}

export function trendWords(measure, mine, team, name) {
  const recent = (values) => values.slice(-4).filter(isNum).map(Number);
  const avg = (values) => (values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null);
  const a = avg(recent(mine));
  const b = avg(recent(team));
  if (a === null || b === null) return null;
  const first = name.split(/\s+/)[0];
  const unit = measure.key === 'rounds_per_pr' ? 'rounds' : measure.key === 'merged' ? 'merged' : measure.key === 'reviews_given' ? 'reviews' : 'fixes';
  const per = unit === 'rounds' ? 'per pull request' : 'a week';
  const close = Math.abs(a - b) <= Math.max(0.15 * Math.max(a, b), 0.2);
  const compare = close ? 'close to the team median' : `${a > b ? 'above' : 'below'} the team median of ${fixed(b, 1)}`;
  return `Over the last 4 weeks, ${first} averaged ${fixed(a, 1)} ${unit === 'merged' ? 'merged' : unit} ${per}, ${compare}.`;
}

function prLink(pr, ctx) {
  return element('a', { class: 'in-cover', href: ctx.links.pr(pr) }, pr.title || `#${pr.number}`);
}

function mergedSection(data, ctx, days) {
  const rows = data.merged_prs || [];
  return section({ title: 'Merged pull requests', count: rows.length },
    rows.length ? element('div', { class: 'in-table-gap' }, sortableTable({
      caption: `Pull requests merged in the last ${days} days.`, sort: { key: 'merged_at', dir: 'descending' }, rows,
      columns: [
        { key: 'title', label: 'Pull request', wide: true, cellClass: 'in-title', cell: (pr) => [prLink(pr, ctx), element('span', { class: 'in-sub' }, `${repoShort(pr.repo)} #${pr.number}`)] },
        { key: 'change_type', label: 'Type', opt: true, cellClass: 'in-nowrap', def: 'The kind of change: bug fix, enhancement, new feature, or maintenance.', cell: (pr) => changeTypeName(pr.change_type) },
        { key: 'size', label: 'Size', opt: true, def: 'Lines changed: XS under 10, S under 30, M under 100, L under 500, XL 500 or more.', value: (pr) => ['XS', 'S', 'M', 'L', 'XL'].indexOf(pr.size), cell: (pr) => pr.size || '—' },
        { key: 'rounds', label: 'Rounds', num: true, def: 'Review rounds before merge.', cell: (pr) => fixed(pr.rounds) },
        { key: 'merged_at', label: 'Merged', num: true, value: (pr) => pr.merged_at || '', cell: (pr) => monthDay(pr.merged_at, false) },
      ],
    })) : emptyLine(`No pull requests merged in the last ${days} days.`));
}

function flaggedSection(data, ctx, days, personId) {
  const rows = data.flagged_categories || [];
  return section({ title: 'What gets flagged on their pull requests', takeaway: rows.length ? 'Select a category to see the flags.' : null },
    rows.length ? ranked(rows.slice(0, 10).map((row) => ({
      name: categoryName(row.category), def: categoryDef(row.category), amount: row.count, value: fixed(row.count),
      href: ctx.links.findings({ days, person: personId, category: row.category }),
    }))) : emptyLine(`Nothing was flagged on their pull requests in the last ${days} days.`));
}

function reviewsSection(data, ctx, days) {
  const rows = data.reviews || [];
  return section({ title: 'Reviews given', count: rows.length },
    rows.length ? element('div', { class: 'in-table-gap' }, sortableTable({
      caption: `Reviews given in the last ${days} days.`, sort: { key: 'at', dir: 'descending' }, rows,
      columns: [
        { key: 'title', label: 'Pull request', wide: true, cellClass: 'in-title', value: (row) => row.pr?.title || '', cell: (row) => [prLink(row.pr || {}, ctx), element('span', { class: 'in-sub' }, `${repoShort(row.pr?.repo)} #${row.pr?.number}, by ${row.pr?.author || 'unknown'}`)] },
        { key: 'state', label: 'Outcome', opt: true, cell: (row) => (row.url ? element('a', { class: 'in-text-link', href: row.url }, REVIEW_STATE[row.state] || row.state || '—') : REVIEW_STATE[row.state] || '—') },
        { key: 'comments', label: 'Comments', num: true, def: 'Comments in this review. Substantive ones are about behavior or design, not style.', value: (row) => (row.comments || []).length,
          cell: (row) => {
            const all = (row.comments || []).length;
            const real = (row.comments || []).filter((comment) => comment.substantive).length;
            return real ? `${fixed(all)}, ${fixed(real)} substantive` : fixed(all);
          } },
        { key: 'at', label: 'Date', num: true, value: (row) => row.at || '', cell: (row) => monthDay(row.at, false) },
      ],
    })) : emptyLine(`No reviews given in the last ${days} days.`));
}

function hotspotSection(data, ctx, days) {
  const rows = data.hotspot_prs || [];
  return section({ title: 'Fixes to files that keep causing problems', count: rows.length },
    rows.length ? element('ul', { class: 'in-events' }, rows.map((pr) => element('li', {}, element('span', { class: 'when' }, `#${pr.number}`),
      element('span', {}, element('a', { href: ctx.links.pr(pr) }, pr.title || `#${pr.number}`), element('span', { class: 'in-sub' }, repoShort(pr.repo))))))
      : emptyLine(`No merged pull requests touched a hotspot file in the last ${days} days.`));
}

function openSection(data, ctx) {
  const rows = data.open_prs || [];
  return section({ title: 'Open pull requests', count: rows.length },
    rows.length ? element('div', { class: 'in-table-gap' }, sortableTable({
      caption: 'Open pull requests.', sort: { key: 'age_days', dir: 'descending' }, rows,
      columns: [
        { key: 'title', label: 'Pull request', wide: true, cellClass: 'in-title', cell: (pr) => [prLink(pr, ctx), element('span', { class: 'in-sub' }, `${repoShort(pr.repo)} #${pr.number}`)] },
        { key: 'age_days', label: 'Open for', num: true, cell: (pr) => (Math.floor(pr.age_days || 0) === 1 ? '1 day' : `${fixed(Math.floor(pr.age_days || 0))} days`) },
        { key: 'rounds', label: 'Rounds so far', num: true, opt: true, cell: (pr) => fixed(pr.rounds) },
        { key: 'waiting_on', label: 'Waiting on', cell: (pr) => WAITING[pr.waiting_on] || '—' },
      ],
    })) : emptyLine('No open pull requests.'));
}

function adminSection(data, ctx, personId) {
  const name = data.person?.name || 'this person';
  const status = element('p', { class: 'in-admin-status', 'aria-live': 'polite' });
  const pickerHolder = element('span', {}, element('span', { class: 'in-skel' }));
  let target = '';
  let rowsById = new Map();
  const mergeButton = element('button', { type: 'button', class: 'in-button is-quiet', disabled: true }, 'Merge');
  const deleteButton = element('button', { type: 'button', class: 'in-button is-danger' }, `Delete ${name}`);
  const loadPeople = () => ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/people?days=90`).then((payload) => {
    const others = (payload.rows || []).filter((row) => row.id !== personId).sort((a, b) => a.name.localeCompare(b.name));
    rowsById = new Map(others.map((row) => [row.id, row]));
    pickerHolder.replaceChildren(select({
      label: `Merge ${name} into`, value: '', options: [['', 'Choose the right person'], ...others.map((row) => [row.id, row.name])],
      onChange: (value) => { target = value; mergeButton.disabled = !value; },
    }));
  }).catch((err) => pickerHolder.replaceChildren(errorState('the people list', err, null)));
  mergeButton.addEventListener('click', async () => {
    if (!target) return;
    const into = pickerHolder.querySelector('select')?.selectedOptions?.[0]?.textContent || 'the other person';
    if (!await confirmDialog(`Merge ${name} into ${into}?`, `${possessiveName(name)} GitHub accounts, pull requests, and reviews move to ${into}, and ${name} is removed. This can’t be undone.`, 'Merge')) return;
    mergeButton.disabled = true;
    try {
      await api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/people/${encodeURIComponent(personId)}/merge`, { method: 'POST', body: { into: target } });
      location.hash = ctx.links.person(rowsById.get(target) || target);
    } catch (err) {
      status.textContent = `Couldn’t merge. ${err.message}`;
      mergeButton.disabled = false;
    }
  });
  deleteButton.addEventListener('click', async () => {
    if (!await confirmDialog(`Delete ${name}?`, `${name} and their GitHub accounts are removed from the team. Their pull requests and reviews stay, but no longer count for anyone. This can’t be undone.`, 'Delete')) return;
    deleteButton.disabled = true;
    try {
      await api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/people/${encodeURIComponent(personId)}/delete`, { method: 'POST', body: {} });
      location.hash = ctx.links.people();
    } catch (err) {
      status.textContent = `Couldn’t delete. ${err.message}`;
      deleteButton.disabled = false;
    }
  });
  const panel = element('div', { class: 'in-admin', id: 'in-manage-record', hidden: true },
    element('p', {}, `If ${name} also appears under a second GitHub account, merge that record into the right person.`),
    element('div', { class: 'in-admin-row' }, pickerHolder, mergeButton),
    element('p', {}, 'Delete removes the person from the team. You confirm it is you before anything changes.'),
    element('div', { class: 'in-admin-row' }, deleteButton), status);
  let loaded = false;
  const toggle = element('button', { type: 'button', class: 'in-manage', 'aria-expanded': 'false', 'aria-controls': 'in-manage-record' }, 'Manage record');
  toggle.addEventListener('click', () => {
    const open = panel.hidden;
    panel.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    if (open && !loaded) {
      loaded = true;
      loadPeople();
    }
  });
  return element('div', { class: 'in-section in-manage-block' }, toggle, panel);
}

const possessiveName = (name) => (/s$/i.test(name) ? `${name}’` : `${name}’s`);
