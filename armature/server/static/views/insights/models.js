// "Is the extra model spend worth it?" Spend, where it goes, comparisons, and recommendations waiting for approval.
import { element } from '../../dom.js';
import { kindLabel } from '../providers.js';
import { dotPlot } from './chart.js';
import { MEASURES, delta, figureText, fixed, isNum, modelName, plural, specialistName } from './format.js';
import { daysFrom, page } from './index.js';
import {
  changeMark, emptyState, filters, openLoadedDrawer, periodSelect, ranked, section, select, skeletonRows, sortableTable, tile,
} from './ui.js';
import { changeWords, comparisonQuestion, modelsAnswer, scopeLine, settingWords } from './words.js';

export const QUESTION = 'Is the extra model spend worth it?';
const SPEND_BETTER = { better: 'down', change: 'percent', unit: 'usd', digits: 2 };
const STATUS = { running: 'Running', paused: 'Paused', finished: 'Finished' };
const RESULT_DEF = 'A result counts only when the gap is wider than the gap between the review and a repeat run with unchanged settings, over enough pull requests.';

export function render(body, ctx) {
  const { query } = ctx;
  const days = daysFrom(query, 90);
  const specialist = query.get('specialist') || '';
  const holder = element('span');
  const filterBar = filters(ctx.teamControl(), periodSelect(days, (value) => ctx.setQuery({ days: value })), holder);
  const params = new URLSearchParams({ days: String(days), ...(specialist ? { specialist } : {}) });
  return page(body, ctx, {
    question: QUESTION, filterBar, what: 'model spend',
    skeleton: () => [
      element('div', { class: 'in-tiles in-tiles-3', 'aria-hidden': 'true' }, ['spend', 'comparison_spend', 'cost_per_pr'].map((id) => tile({ measure: MEASURES[id], value: undefined, days }))),
      section({ title: 'Where the spend goes' }, skeletonRows(5)),
    ],
    load: () => ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/models?${params}`),
    draw: (data, head) => {
      const names = (data.by_specialist || []).map((row) => row.name);
      if (specialist && !names.includes(specialist)) names.push(specialist);
      holder.replaceChildren(select({ label: 'Reviewer', value: specialist, options: [['', 'All reviewers'], ...names.map((name) => [name, specialistName(name)])], onChange: (value) => ctx.setQuery({ specialist: value }) }));
      head.answerNode.replaceChildren(modelsAnswer(data));
      head.scopeNode.replaceChildren(scopeLine({ scope: data.scope }));
      const pair = (key) => data[key] || {};
      const tiles = element('div', { class: 'in-tiles in-tiles-3' },
        tile({ measure: MEASURES.spend, value: pair('spend_usd').value ?? null, previous: pair('spend_usd').previous ?? null, days }),
        tile({ measure: MEASURES.comparison_spend, value: pair('comparison_spend_usd').value ?? null, previous: pair('comparison_spend_usd').previous ?? null, days, none: 'None yet' }),
        tile({ measure: MEASURES.cost_per_pr, value: pair('cost_per_reviewed_pr_usd').value ?? null, previous: pair('cost_per_reviewed_pr_usd').previous ?? null, days }));
      const spendList = (rows, label, nameOf) => ranked(rows.map((row) => ({
        name: nameOf(row.name), amount: row.spend_usd, value: figureText(row.spend_usd, 'usd', 2), after: changeMark(delta(row.spend_usd, row.previous, SPEND_BETTER, days)),
        onClick: label === 'By reviewer' ? () => ctx.setQuery({ specialist: row.name === specialist ? '' : row.name }) : null, current: label === 'By reviewer' && row.name === specialist,
      })), { label });
      const bySpecialist = data.by_specialist || [];
      const byModel = data.by_model || [];
      const where = (bySpecialist.length || byModel.length) ? element('div', { class: 'in-two' },
        element('div', {}, element('h3', { class: 'in-h3' }, 'By reviewer'), bySpecialist.length ? spendList(bySpecialist, 'By reviewer', specialistName) : emptyState('No spend by reviewer in this period.')),
        element('div', {}, element('h3', { class: 'in-h3' }, 'By model'), byModel.length ? spendList(byModel, 'By model', modelName) : emptyState('No spend by model in this period.')))
        : null;
      const comparisons = data.comparisons || [];
      const waiting = data.recommendations || [];
      return [
        tiles,
        where ? section({ title: 'Where the spend goes', takeaway: bySpecialist.length > 1 ? 'Select a reviewer to see only its spend.' : null }, where) : null,
        section({ title: 'Comparisons', takeaway: comparisons.length ? 'Select a comparison to see what each setting found.' : null },
          comparisons.length ? comparisonTable(comparisons, ctx) : explain(
            'No comparisons have run yet.',
            'A comparison reviews a sample of pull requests twice more, side by side: once with another model or effort level, for example concurrency at high effort instead of xhigh, and once unchanged. Each row will say what the change bought: as many real issues for less, more real issues, or a difference within normal variation.',
          ), ctx.lead ? addComparison(ctx) : null),
        section({ title: 'Recommendations waiting for approval', count: waiting.length || null },
          waiting.length ? recommendationList(waiting, comparisons, ctx) : explain(
            'Nothing is waiting for your approval.',
            'When a comparison shows a cheaper setting finds the same issues, the change appears here with its evidence. Nothing changes until you approve it.',
          )),
      ];
    },
  });
}

function explain(first, second) {
  return element('div', { class: 'in-explain' }, element('p', {}, first), element('p', {}, second));
}

const compared = (prs, needed) => (prs >= needed ? `${plural(prs, 'pull request')} compared.` : `${plural(prs, 'pull request')} compared of the ${fixed(needed)} a result needs.`);

function comparisonTable(rows, ctx) {
  const open = (row, opener) => openLoadedDrawer({
    title: comparisonQuestion(row), what: 'this comparison', why: `${row.result?.words || ''}. ${compared(row.prs, row.min_prs)}`,
    load: () => ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/models/comparisons/${encodeURIComponent(row.id)}`),
    fill: (detail) => comparisonDetail(row, detail),
  }, opener);
  return element('div', { class: 'in-table-scroll' }, sortableTable({
    caption: 'Comparisons.', rows, sort: { key: 'prs', dir: 'descending' },
    columns: [
      { key: 'question', label: 'Comparison', value: (row) => comparisonQuestion(row), cell: (row) => element('button', { type: 'button', class: 'in-cover', onclick: (clickEvent) => open(row, clickEvent.currentTarget) }, comparisonQuestion(row)) },
      { key: 'prs', label: 'Pull requests', num: true, def: 'Pull requests compared so far, and how many a result needs until then.', cell: (row) => (row.prs >= row.min_prs ? fixed(row.prs) : `${fixed(row.prs)} of ${fixed(row.min_prs)}`) },
      { key: 'result', label: 'Result', def: RESULT_DEF, value: (row) => row.result?.words || '', cell: (row) => row.result?.words || '' },
      { key: 'status', label: 'Status', opt: true, value: (row) => row.status, cell: (row) => STATUS[row.status] || row.status },
    ],
  }));
}

const usd = (value) => (isNum(value) ? figureText(value, 'usd', 2) : '—');
const minutes = (seconds) => (isNum(seconds) ? `${fixed(Number(seconds) / 60, 1)} min` : '—');
const settingOf = (row, side) => settingWords(row.variable, row[side]?.[row.variable === 'model' ? 'model' : 'effort']);
const SHIFT_WORDS = { recall: ['real issues found', (v) => `${fixed(Math.abs(v) * 100)} points`], cost_usd: ['cost per review', (v) => usd(Math.abs(v))] };

// How the repeat with unchanged settings differed from the review itself, which never decides the result.
export function shiftWords(shift) {
  // A shift that rounds to nothing on screen goes unsaid.
  const parts = Object.entries(SHIFT_WORDS).filter(([key]) => isNum(shift?.[key]?.mean))
    .map(([key, [what, size]]) => [size(Number(shift[key].mean)), Number(shift[key].mean) > 0 ? 'more' : 'less', what])
    .filter(([size]) => !/^\$?0(\.0+)?( points)?$/.test(size))
    .map(([size, way, what]) => `${size} ${way} in ${what}`);
  return parts.length ? `Running later, as a comparison, changed results by ${parts.join(' and ')} on its own. Both sides ran under those conditions, so it doesn’t affect the result.`
    : 'Running later as a comparison didn’t change the results.';
}

// The drawer: what each side found and spent under the same conditions, the environment shift, and each pull
// request as a dot. Its words come from the same result as the row.
export function comparisonDetail(row, detail) {
  const current = `Now: ${settingOf(row, 'baseline')}`;
  const variant = `Tried: ${settingOf(row, 'variant')}`;
  const sides = detail.sides || {};
  const lines = [
    ['Found by both', fixed(detail.found?.both), fixed(detail.found?.both)],
    ['Raised only by this side', fixed(detail.found?.only_current), fixed(detail.found?.only_variant)],
    ['Real issues found only by this side', fixed(detail.valid?.only_current), fixed(detail.valid?.only_variant)],
    ['Judged real', fixed(sides.current?.valid), fixed(sides.variant?.valid)],
    ['Cost per review', usd(sides.current?.cost_usd), usd(sides.variant?.cost_usd)],
    ['Time per review', minutes(sides.current?.latency_s), minutes(sides.variant?.latency_s)],
  ];
  const table = element('table', { class: 'in-table in-sides' },
    element('caption', { class: 'visually-hidden' }, 'What each setting found, and what it cost, run side by side.'),
    element('thead', {}, element('tr', {}, element('td', {}), element('th', { scope: 'col', class: 'num' }, current), element('th', { scope: 'col', class: 'num' }, variant))),
    element('tbody', {}, lines.map(([label, a, b]) => element('tr', {}, element('th', { scope: 'row' }, label), element('td', { class: 'num' }, a), element('td', { class: 'num' }, b)))));
  const rule = element('p', { class: 'in-note' }, 'Both sides reviewed each pull request again, side by side. A difference counts when its average across pull requests is more than twice its standard error.');
  const shift = detail.environment_shift ? element('p', { class: 'in-note' }, shiftWords(detail.environment_shift)) : null;
  const points = detail.points || [];
  const plot = points.length ? dotPlot({
    label: `Cost per review against real issues found, for each compared pull request, ${current.toLowerCase()} and ${variant.toLowerCase()}`,
    xLabel: 'Cost per review', yLabel: 'Real issues found', xFormat: usd, yFormat: (value) => fixed(value),
    series: [
      { name: current, slot: 1, points: points.map((point) => ({ label: point.pr || 'A pull request', x: point.current?.cost_usd, y: point.current?.valid })) },
      { name: variant, slot: 2, points: points.map((point) => ({ label: point.pr || 'A pull request', x: point.variant?.cost_usd, y: point.variant?.valid })) },
    ],
  }) : null;
  const skipped = Number(detail.arms?.skipped) || 0;
  return {
    why: `${detail.result?.words || ''}. ${compared(detail.prs, row.min_prs)}${skipped ? ` ${plural(skipped, 'comparison run')} skipped, for a budget, a cap, or a missing runner.` : ''}`,
    evidence: [element('div', { class: 'in-table-scroll' }, table), rule, shift, plot ? [element('h3', {}, 'Each pull request'), plot] : null].flat(),
  };
}

// The shell asks for the sign-in again on a step-up. If that couldn't happen, say so plainly.
export const failedWords = (verb, err) => (err?.payload?.error === 'step_up_required' && !/^Cancelled/.test(err?.message || '')
  ? `Sign in again to ${verb}.` : `Couldn’t ${verb} it. ${err?.message || ''}`.trim());

export function recommendationList(items, comparisons, ctx) {
  const baselineOf = (id) => comparisons.find((row) => row.id === id)?.baseline || {};
  return element('ul', { class: 'in-recs' }, items.map((item) => {
    const status = element('p', { class: 'in-rec-done', 'aria-live': 'polite' });
    const evidence = item.evidence?.prs ? `Each of the ${plural(item.evidence.prs, 'pull request')} was reviewed twice more, side by side: once with the change and once unchanged.` : null;
    let actions = null;
    if (ctx.lead) {
      // A model change needs an admin, so a team lead sees why in place of the button.
      const approve = item.can_approve === false ? element('span', { class: 'in-admin-status' }, 'Needs an admin to approve')
        : element('button', { type: 'button', class: 'in-button' }, 'Approve change');
      const dismiss = element('button', { type: 'button', class: 'in-button is-quiet' }, 'Dismiss');
      actions = element('div', { class: 'in-actions' }, approve, dismiss);
      const act = async (verb, done) => {
        approve.disabled = true;
        dismiss.disabled = true;
        status.textContent = '';
        try {
          await ctx.post(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/models/recommendations/${encodeURIComponent(item.id)}/${verb}`);
          actions.replaceWith();
          status.textContent = done;
        } catch (err) {
          status.textContent = failedWords(verb, err);
          approve.disabled = false;
          dismiss.disabled = false;
        }
      };
      approve.addEventListener('click', () => act('approve', 'Approved. The change applies to new reviews.'));
      dismiss.addEventListener('click', () => act('dismiss', 'Dismissed. Nothing changed.'));
    } else {
      status.textContent = 'A team lead approves this change.';
    }
    return element('li', { class: 'in-rec' },
      element('p', { class: 'in-rec-sentence' }, item.sentence),
      element('p', { class: 'in-rec-change' }, changeWords(item.change, baselineOf(item.experiment_id))),
      evidence ? element('p', { class: 'in-rec-change' }, evidence) : null,
      actions, status);
  }));
}

// A team lead starts a comparison: a reviewer, what to change, and what to change it to. The current setting is
// shown and left out of the choices.
export function addComparison(ctx) {
  const status = element('p', { class: 'in-admin-status', 'aria-live': 'polite' });
  const form = element('div', { class: 'in-admin', id: 'in-add-comparison', hidden: true });
  const toggle = element('button', { type: 'button', class: 'in-button is-quiet', 'aria-expanded': 'false', 'aria-controls': 'in-add-comparison' }, 'Add comparison');
  const state = { specialist: '', variable: 'effort', variant: '' };
  let choices = null;
  const start = element('button', { type: 'button', class: 'in-button', disabled: true }, 'Start comparison');
  const hint = element('span', { class: 'in-admin-status' });
  const currentNode = element('p', {});
  const valueHolder = element('span');
  const ready = () => {
    start.disabled = !(state.specialist && state.variant);
    hint.textContent = !state.specialist ? 'Choose a reviewer first.' : !state.variant ? 'Choose what to change it to.' : '';
  };
  const drawValue = () => {
    const now = choices.current?.[state.specialist] || {};
    currentNode.textContent = state.specialist
      ? `${specialistName(state.specialist)} runs ${now.model ? modelName(now.model) : 'its alias’s model'} at ${now.effort ? `${now.effort} effort` : 'the default effort'} now.`
      : '';
    // An admin is also offered aliases on other providers, as @alias, which no model id can be.
    const options = state.variable === 'model'
      ? [...(now.models || []).filter((id) => id !== now.model).map((id) => [id, modelName(id)]),
        ...(now.aliases || []).map((item) => [`@${item.alias}`, `${modelName(item.model)} on ${kindLabel(item.provider)} (${item.alias})`])]
      : (choices.efforts || []).filter((id) => id !== now.effort).map((id) => [id, `${id} effort`]);
    state.variant = '';
    valueHolder.replaceChildren(select({ label: 'Change it to', visibleLabel: true, value: '', options: [['', 'Choose'], ...options], onChange: (value) => { state.variant = value; ready(); } }));
    ready();
  };
  const drawForm = () => {
    const budget = figureText(choices.defaults?.daily_budget_usd, 'usd', 0);
    form.replaceChildren(
      element('p', {}, `A sample of reviews runs twice more, side by side: once with the change and once unchanged. Each comparison spends at most ${budget} a day.`),
      element('div', { class: 'in-admin-row' },
        select({ label: 'Reviewer', visibleLabel: true, value: '', options: [['', 'Choose'], ...(choices.specialists || []).map((id) => [id, specialistName(id)])], onChange: (value) => { state.specialist = value; drawValue(); } }),
        select({ label: 'Compare', visibleLabel: true, value: state.variable, options: [['effort', 'Effort'], ['model', 'Model']], onChange: (value) => { state.variable = value; drawValue(); } }),
        valueHolder),
      currentNode,
      element('div', { class: 'in-admin-row' }, start, hint), status);
    drawValue();
  };
  start.addEventListener('click', async () => {
    start.disabled = true;
    status.textContent = '';
    try {
      const { variant, ...rest } = state;
      const body = variant.startsWith('@') ? { ...rest, alias: variant.slice(1) } : { ...state };
      await ctx.post(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/models/experiments`, body);
      await ctx.setQuery({});
    } catch (err) {
      status.textContent = failedWords('start', err);
      ready();
    }
  });
  toggle.addEventListener('click', async () => {
    const opening = form.hidden;
    form.hidden = !opening;
    toggle.setAttribute('aria-expanded', String(opening));
    if (!opening || choices) return;
    form.replaceChildren(element('span', { class: 'in-skel is-line' }));
    try {
      choices = await ctx.api(`/api/teams/${encodeURIComponent(ctx.team.id)}/insights/models/experiments`);
      drawForm();
    } catch (err) {
      form.replaceChildren(element('p', {}, `Couldn’t load the choices. ${err.message}`));
      choices = null;
    }
  });
  return element('div', { class: 'in-manage-block in-add' }, toggle, form);
}
