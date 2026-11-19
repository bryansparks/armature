// The sentences Insights writes from data: answer lines, chart takeaways, and needs-attention items. Pure, so the
// node tests can check the wording.
import {
  MEASURES, STAGES, cap, categoryName, categoryPhrase, changeTypeName, dateRange, delta, figureText, firstName, fixed,
  halfBy, isNum, modelName, monthDay, plural, possessive, pushKindName, repoShort, specialistName,
} from './format.js';

const metricOf = (metrics, id) => (metrics || []).find((metric) => metric.id === id) || null;

// compare is false on a screen that shows no change from the previous period.
export function scopeLine({ scope, people = null, repos = null, repoName = '', compare = true }) {
  const parts = [];
  if (isNum(people) && people > 0) parts.push(plural(people, 'person', 'people'));
  if (repoName) parts.push(`${parts.length ? 'in ' : ''}${repoName}`);
  else if (isNum(repos) && repos > 0) parts.push(`${parts.length ? 'across ' : ''}${plural(repos, 'repo')}`);
  const range = dateRange(scope.start, scope.end);
  const head = parts.length ? `${parts.join(' ')}, ${range}.` : `${range}.`;
  return compare ? `${head} Changes compare with the ${scope.days} days before.` : head;
}

// "How is the team doing?"
export function overviewAnswer(data, repoName = '') {
  const days = data.scope.days;
  const merged = metricOf(data.metrics, 'merged');
  const ttm = metricOf(data.metrics, 'time_to_merge');
  const where = repoName ? ` in ${repoName}` : '';
  if (!merged || (!merged.value && !merged.previous)) return `No pull requests merged${where} in the last ${days} days.`;
  const relative = (now, before) => (isNum(now) && isNum(before) && before > 0 ? (now - before) / before : null);
  const shipping = relative(merged.value, merged.previous);
  const speed = ttm ? relative(ttm.value, ttm.previous) : null;
  let first;
  if (shipping === null) first = `${plural(merged.value, 'pull request')} merged${where}`;
  else if (Math.abs(shipping) < 0.05) first = 'Shipping is steady';
  else first = shipping > 0 ? 'Shipping is up' : 'Shipping slowed';
  if (speed !== null && speed <= -0.1) first += ' and pull requests merge faster than the previous period';
  else if (speed !== null && speed >= 0.1) first += ` and pull requests take longer to merge, ${figureText(ttm.value, 'days', 1)} at the median`;
  const sentences = [`${first}.`];
  const rising = (data.attention || []).find((item) => item.kind === 'rounds_rising');
  const rounds = metricOf(data.metrics, 'review_rounds');
  if (rising) {
    const why = rising.top_category ? `, mostly for ${categoryPhrase(rising.top_category)}` : '';
    sentences.push(`${repoShort(rising.repo)} pull requests need more review rounds${why}.`);
  } else if (rounds && isNum(rounds.value)) {
    const change = delta(rounds.value, rounds.previous, MEASURES.review_rounds, days);
    if (change && change.dir === 'down') sentences.push(`Review rounds fell to ${fixed(rounds.value, 1)} per pull request.`);
  }
  return sentences.join(' ');
}

const TAKE = {
  time_to_merge: (repo, a, b, dir, on) => `${possessive(repo)} 7-day median time to merge is ${figureText(b, 'days', 1)}, ${dir} from ${fixed(a, 1)} on ${on}.`,
  review_rounds: (repo, a, b, dir, on) => `${possessive(repo)} 7-day average is ${fixed(b, 1)} review rounds per pull request, ${dir} from ${fixed(a, 1)} on ${on}.`,
  conflict_hours: (repo, a, b, dir, on) => `${repo} spent ${figureText(b, 'hours', 0)} blocked by conflicts in the last 7 days, ${dir} from ${fixed(a, 0)} in the 7 days to ${on}.`,
  time_to_first_review: (repo, a, b, dir, on) => `${possessive(repo)} 7-day median wait for a first review is ${figureText(b, 'hours', 1)}, ${dir} from ${fixed(a, 1)} on ${on}.`,
  ci_failure_rate: (repo, a, b, dir, on) => `${fixed(b * 100)}% of checks in ${repo} failed in the last 7 days, ${dir} from ${fixed(a * 100)}% in the 7 days to ${on}.`,
};

// The first and last present values of a series, with the index of the first.
const ends = (values) => {
  const present = (values || []).map((value, index) => [index, value]).filter(([, value]) => isNum(value));
  return present.length ? { first: present[0][0], a: Number(present[0][1]), b: Number(present[present.length - 1][1]) } : null;
};

// One sentence that says what the trend chart shows. series is [{ name, values }], dates the points' ISO dates.
// flat is true when the tile above says the period didn't change, so the two never disagree.
export function takeaway(metricId, series, { dates = [], flat = false } = {}) {
  const live = series.map((item) => ({ ...item, ends: ends(item.values) })).filter((item) => item.ends);
  if (!live.length) return 'Not enough data in this period to draw a trend.';
  if (metricId === 'merged') {
    const top = live.reduce((best, item) => (item.ends.b > best.ends.b ? item : best));
    if (live.length === 1) return `${top.name} merged ${plural(top.ends.b, 'pull request')} in the last 7 days.`;
    return `${top.name} merged the most, ${plural(top.ends.b, 'pull request')} in the last 7 days.`;
  }
  if (flat) return live.length === 1 ? `${live[0].name} held steady over the period.` : 'Steady over the period for the team as a whole.';
  const moved = live.map((item) => {
    const { a, b } = item.ends;
    const scale = Math.max(Math.abs(a), Math.abs(b), metricId === 'ci_failure_rate' ? 0.05 : 0.5);
    return { ...item, change: Math.abs(b - a) / scale };
  }).sort((x, y) => y.change - x.change);
  const top = moved[0];
  if (top.change < 0.1) return live.length === 1 ? `${top.name} held steady over the period.` : 'Every repo held steady over the period.';
  const { a, b, first } = top.ends;
  const on = monthDay(dates[first]) || 'the first day';
  const sentence = (TAKE[metricId] || TAKE.review_rounds)(top.name, a, b, b > a ? 'up' : 'down', on);
  const others = moved.slice(1);
  if (others.length && others.every((item) => item.change < 0.1)) return `${sentence} The other repos held steady.`;
  return sentence;
}

const ICON_OF = { dropped_flag_bug: 'bad', specialist_dropped_bugs: 'bad', rounds_rising: 'warn', conflict_file: 'warn', waiting_on_review: 'info' };
const KIND_LABEL = { bad: 'Bug', warn: 'Getting worse', info: 'Waiting on reviews' };
const TIMES = ['', '', 'twice as many as', 'three times more than', 'four times more than', 'five times more than'];

export const prLabel = (pr) => (pr ? `${repoShort(pr.repo)} #${pr.number}` : 'a pull request');

// Turns one attention item from the API into what the list and the drawer say. hrefs builds the links. Unknown
// kinds return null, so a new server-side rule never shows half-worded.
export function attentionWords(item, { hrefs, canSeePeople = false, all = [] }) {
  const icon = ICON_OF[item.kind];
  if (!icon) return null;
  const base = { kind: item.kind, icon, kindLabel: KIND_LABEL[icon] };
  if (item.kind === 'dropped_flag_bug') {
    const reverted = item.link === 'revert';
    const category = item.flag?.category ? categoryPhrase(item.flag.category) : 'a problem';
    return {
      ...base,
      title: reverted ? 'A merged pull request was reverted' : 'A merged pull request needed a bug fix',
      why: `${prLabel(item.pr)} was ${reverted ? 'reverted' : 'fixed'} by #${item.bug_pr?.number}. The review flagged ${category}, and the flag was dropped.`,
      go: 'See pull request',
      evidenceTitle: 'What happened',
      events: [
        ['Flagged', `${specialistName(item.flag?.specialist)} review: ${item.flag?.claim || 'no detail recorded'}`],
        ['Dropped', 'The flag was dropped before merge.'],
        ['Merged', `#${item.pr?.number} ${item.pr?.title || ''}`.trim()],
        [reverted ? 'Reverted' : 'Fixed', `#${item.bug_pr?.number} ${item.bug_pr?.title || ''}${item.bug_pr?.author ? `, by ${item.bug_pr.author}` : ''}`.trim()],
      ],
      link: [`Open #${item.pr?.number}`, hrefs.pr(item.pr)],
    };
  }
  if (item.kind === 'specialist_dropped_bugs') {
    const shown = specialistName(item.specialist);
    const name = shown === shown.toUpperCase() ? shown : shown.toLowerCase();
    const related = all.filter((other) => other.kind === 'dropped_flag_bug' && other.flag?.specialist === item.specialist);
    return {
      ...base,
      title: `Dropped ${name} flags led to bugs`,
      why: `${plural(item.count, 'flag')} from the ${name} review ${item.count === 1 ? 'was' : 'were'} dropped and later traced to a bug.`,
      go: 'See findings',
      evidenceTitle: related.length ? 'Pull requests with a dropped flag that became a bug' : '',
      events: related.map((other) => [prLabel(other.pr), other.flag?.claim || other.pr?.title || '']),
      link: ['See how often flags were dropped', hrefs.findings({})],
    };
  }
  if (item.kind === 'rounds_rising') {
    const repo = repoShort(item.repo);
    const why = item.top_category ? ` Most extra rounds were flagged for ${categoryPhrase(item.top_category)}.` : '';
    return {
      ...base,
      title: `${repo} pull requests need more review rounds`,
      why: `${fixed(item.value, 1)} rounds per pull request, up from ${fixed(item.previous, 1)}.${why}`,
      go: 'See findings',
      evidenceTitle: (item.categories || []).length ? `Flagged after the first review in ${repo}` : '',
      ranked: (item.categories || []).map((row) => [categoryName(row.category), row.count]),
      link: [`Open ${repo} findings`, hrefs.findings({ repo: item.repo_id })],
    };
  }
  if (item.kind === 'conflict_file') {
    const ratio = item.next_hours > 0 ? item.hours / item.next_hours : null;
    const compare = ratio === null ? 'the only file with conflicts in this period'
      : ratio < 2 ? 'more than any other file' : ratio < 6 ? `${TIMES[Math.floor(ratio)]} any other file` : `${Math.floor(ratio)} times more than any other file`;
    return {
      ...base,
      kindLabel: 'Blocked',
      title: `${item.path} keeps causing merge conflicts`,
      why: `${figureText(item.hours, 'hours', 0)} blocked across ${plural(item.prs, 'pull request')}, ${compare}.`,
      go: 'See repo',
      evidenceTitle: `Hours blocked by file, ${repoShort(item.repo)}`,
      ranked: (item.files || []).map((row) => [row.path, row.hours]),
      rankedSuffix: ' h',
      link: [`Open ${repoShort(item.repo)}`, hrefs.repo(item.repo_id)],
    };
  }
  if (item.kind === 'waiting_on_review') {
    const prs = item.prs || [];
    const oldest = prs[0];
    return {
      ...base,
      title: item.person ? `${item.person} is waiting on reviews` : `${plural(prs.length, 'pull request')} ${prs.length === 1 ? 'is' : 'are'} waiting on a first review`,
      why: item.person ? `${plural(prs.length, 'pull request')} ${prs.length === 1 ? 'has' : 'have'} gone more than 2 days without a review.`
        : `${prs.length === 1 ? 'It has' : 'Each has'} gone more than 2 days without a review.`,
      go: 'See pull requests',
      evidenceTitle: 'Waiting for a first review',
      events: prs.map((pr) => [plural(Math.floor(pr.waiting_days || 0), 'day'), `#${pr.number} ${pr.title || ''}`.trim(), hrefs.pr(pr)]),
      link: canSeePeople && item.person_id ? [`Open ${possessive(firstName(item.person))} page`, hrefs.person({ id: item.person_id, login: item.login })] : oldest ? [`Open #${oldest.number}`, hrefs.pr(oldest)] : null,
    };
  }
  return null;
}

// The sequential stages of the median pull request. CI runs inside the time after approval, so it splits that span.
export function stageBlocks(stages) {
  if (!stages?.pr) return [];
  const afterApproval = Number(stages.approved_to_merged) || 0;
  const ci = Math.min(afterApproval, Number(stages.waiting_on_ci) || 0);
  const label = (key) => STAGES.find(([id]) => id === key);
  return [
    ['waiting_first_review', Number(stages.waiting_first_review) || 0, label('waiting_first_review')[1], label('waiting_first_review')[2]],
    ['in_review', Number(stages.in_review) || 0, label('in_review')[1], label('in_review')[2]],
    ['waiting_on_ci', ci, label('waiting_on_ci')[1], label('waiting_on_ci')[2]],
    ['approved_to_merged', afterApproval - ci, 'Approved, waiting to merge', 'From approval and green checks to the merge.'],
  ].map(([key, hours, name, def], index) => ({ key, hours, name, def, step: index + 1 }));
}

const STAGE_WORDS = {
  waiting_first_review: 'waiting for a first review', in_review: 'review rounds', waiting_on_ci: 'waiting on CI', approved_to_merged: 'the wait between approval and merge',
};

// "Where is time going in android-tree?" The biggest stage is the median pull request's; the waits are period medians.
export function repoAnswer(data) {
  const name = data.repo?.name || repoShort(data.repo?.full_name);
  const blocks = stageBlocks(data.stages);
  if (!blocks.length) return `No pull requests merged in ${name} in the last ${data.scope.days} days.`;
  const biggest = [...blocks].sort((a, b) => b.hours - a.hours)[0];
  const tile = (id) => (data.tiles || []).find((item) => item.id === id);
  const first = tile('time_to_first_review');
  const rounds = tile('review_rounds');
  const detail = [];
  if (first && isNum(first.value)) detail.push(`wait ${figureText(first.value, 'hours', 1)} for a first review at the median`);
  if (rounds && isNum(rounds.value)) detail.push(`average ${fixed(rounds.value, 1)} review rounds`);
  return `Most of the median pull request’s time goes to ${STAGE_WORDS[biggest.key]}.${detail.length ? ` Across the period, pull requests ${detail.join(' and ')}.` : ''}`;
}

// "How is everyone contributing?" The wording changes with the view, and never ranks a person.
export function peopleAnswer(data, { teamMerged = null } = {}) {
  const rows = data.rows || [];
  if (!rows.length) return `Nobody opened, merged, or reviewed a pull request in the last ${data.scope.days} days.`;
  const values = (key) => rows.map((row) => row.measures?.[key]);
  const total = (key) => values(key).filter(isNum).reduce((sum, value) => sum + Number(value), 0);
  const people = plural(rows.length, 'person', 'people');
  if (data.view === 'shipping') {
    const merged = total('merged');
    const half = halfBy(values('merged'));
    const spread = merged && half && half <= rows.length * 0.3 ? ` Half of them came from ${plural(half, 'person', 'people')}.` : ' The work is spread across the team.';
    const outside = outsideCount(teamMerged, merged);
    const extra = outside ? ` Another ${fixed(outside)} came from people outside the team.` : '';
    return `${cap(people)} merged ${plural(merged, 'pull request')}.${merged ? spread : ''}${extra}`;
  }
  if (data.view === 'quality') {
    const mid = data.team_median?.rounds_per_pr;
    if (!isNum(mid)) return 'No merged pull requests to measure in this period.';
    const high = rows.filter((row) => isNum(row.measures?.rounds_per_pr) && row.measures.rounds_per_pr > mid * 1.5).length;
    const traced = total('bugs_traced');
    const tail = high ? ` ${plural(high, 'person’s', 'people’s')} pull requests needed half again as many.` : ' Nobody is far from it.';
    return `Pull requests needed ${fixed(mid, 1)} review rounds at the team median.${tail}${traced ? ` ${plural(traced, 'bug')} traced back to merged work.` : ''}`;
  }
  if (data.view === 'review') {
    const given = total('reviews_given');
    const half = halfBy(values('reviews_given'));
    const uneven = given && half && half <= rows.length * 0.3 ? ` Review work is uneven: ${plural(half, 'person', 'people')} gave half of all reviews.` : '';
    return `${cap(people)} gave ${plural(given, 'review')}.${uneven}`;
  }
  const hot = total('hotspot_prs');
  const platform = total('platform_prs');
  const who = rows.filter((row) => row.measures?.hotspot_prs > 0).length;
  if (!hot && !platform) return 'No pull requests touched the files that keep causing problems, and none were platform maintenance.';
  return `${plural(who, 'person', 'people')} worked on files that keep causing problems, in ${plural(hot, 'pull request')}. ${plural(platform, 'pull request')} went to platform maintenance.`;
}

// Pull requests merged by authors who aren't listed as people: bots and outside contributors.
export function outsideCount(total, listed) {
  if (!isNum(total) || !isNum(listed)) return 0;
  return Math.max(0, Math.round(Number(total) - Number(listed)));
}

const TYPE_PLURAL = {
  'bug-fix': 'bug fixes', enhancement: 'enhancements', 'new-feature': 'new features', 'code-maintenance': 'maintenance', 'platform-maintenance': 'platform maintenance',
};

// "How is Mei Chen doing?"
export function personAnswer(data) {
  const name = firstName(data.person?.name);
  const shipping = data.shipping || {};
  const quality = data.quality || {};
  const merged = shipping.merged || 0;
  const types = Object.entries(shipping.by_type || {}).filter(([id]) => id !== 'unclassified').sort((a, b) => b[1] - a[1]);
  const top = types[0] && types[0][1] * 2 > merged ? `, mostly ${TYPE_PLURAL[types[0][0]] || changeTypeName(types[0][0]).toLowerCase()}` : '';
  const repo = data.person?.repo ? ` in ${repoShort(data.person.repo)}` : '';
  const sentences = [];
  if (merged) sentences.push(`${name} shipped ${plural(merged, 'pull request')}${top}${repo}.`);
  else sentences.push(`${name} merged no pull requests in the last ${data.scope.days} days.`);
  const mid = data.team_median?.quality?.rounds_per_pr;
  if (isNum(quality.rounds_per_pr)) {
    const compare = isNum(mid) ? (quality.rounds_per_pr > mid * 1.1 ? `, above the team median of ${fixed(mid, 1)}` : quality.rounds_per_pr < mid * 0.9 ? `, below the team median of ${fixed(mid, 1)}` : ', close to the team median') : '';
    const flagged = data.flagged_categories?.[0];
    const why = flagged && isNum(mid) && quality.rounds_per_pr > mid * 1.1 ? `, most often flagged for ${categoryPhrase(flagged.category)}` : '';
    sentences.push(`${possessive(name)} pull requests needed ${fixed(quality.rounds_per_pr, 1)} review rounds${compare}${why}.`);
  }
  const reviews = data.review?.reviews_given;
  if (isNum(reviews) && reviews > 0) sentences.push(`${name} gave ${plural(reviews, 'review')}.`);
  return sentences.join(' ');
}

const COUNT_WORDS = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine'];
const countWord = (n) => COUNT_WORDS[n] || fixed(n);

// "What keeps getting flagged?"
export function findingsAnswer(data, repoName = '') {
  const days = data.scope.days;
  const where = repoName ? ` in ${repoName}` : '';
  const categories = data.categories || [];
  const chosen = data.category ? categories.find((row) => row.category === data.category) : null;
  const top = chosen || categories[0];
  if (!top || !top.count) {
    if (data.category) return `${categoryName(data.category)} wasn’t flagged${where} in the last ${days} days.`;
    return `Nothing was flagged${where} in the last ${days} days.`;
  }
  const change = isNum(top.previous) && top.previous !== top.count ? `, ${top.count > top.previous ? 'up' : 'down'} from ${fixed(top.previous)}` : '';
  if (chosen) return `${categoryName(top.category)} was flagged ${plural(top.count, 'time')}${where} in the last ${days} days${change}.`;
  const next = categories[1];
  const tops = repoName ? [] : data.top_by_repo || [];
  const leads = tops.filter((row) => row.category === top.category).length;
  let tail = next && next.count ? ` ${categoryName(next.category)} comes next with ${fixed(next.count)}.` : '';
  if (tops.length > 1 && leads) tail = leads === tops.length ? ' It is the top category in every repo.' : ` It is the top category in ${countWord(leads)} of ${countWord(tops.length)} repos.`;
  return `${categoryName(top.category)} is flagged most: ${plural(top.count, 'time')}${where} in the last ${days} days${change}.${tail}`;
}

// "Is the extra model spend worth it?"
export function modelsAnswer(data) {
  const days = data.scope.days;
  const spend = data.spend_usd || {};
  if (!isNum(spend.value) || !spend.value) return `No review spend in the last ${days} days.`;
  const change = delta(spend.value, spend.previous, MEASURES.spend, days);
  const moved = change && change.dir !== 'flat' ? `, ${change.dir} ${change.text} on the ${days} days before` : '';
  const perPr = isNum(data.cost_per_reviewed_pr_usd?.value) ? ` That is ${figureText(data.cost_per_reviewed_pr_usd.value, 'usd', 2)} per reviewed pull request.` : '';
  const waiting = data.recommendations || [];
  const more = waiting.length > 1 ? ` ${plural(waiting.length - 1, 'more recommendation')} ${waiting.length === 2 ? 'waits' : 'wait'} for approval.` : '';
  const verdict = waiting.length ? ` ${cap(String(waiting[0].sentence || '').trim())}${more}`
    : (data.comparisons || []).length ? '' : ' No comparison runs have finished yet, so there is no recommendation.';
  return `Reviews cost ${figureText(spend.value, 'usd', 2)}${moved}.${perPr}${verdict}`;
}

// What a setting reads as: an effort level, or a model's name. None is the review's default effort.
export function settingWords(variable, value) {
  if (variable === 'model') return value ? modelName(value) : 'the current model';
  return value ? `${value} effort` : 'the default effort';
}

// The same model on two providers is told apart by alias: "Gateway Model (fast)".
const sameModel = (variant, baseline) => Boolean(variant?.alias && variant.model && variant.model === baseline?.model);
const aliasedName = (model, alias) => `${modelName(model)} (${alias})`;

// "Concurrency at high effort instead of xhigh effort", or "Simplicity on Claude Sonnet 4.6 instead of Claude Opus 4.6".
export function comparisonQuestion(row) {
  const key = row.variable === 'model' ? 'model' : 'effort';
  const joiner = key === 'model' ? 'on' : 'at';
  if (key === 'model' && sameModel(row.variant, row.baseline)) {
    return `${specialistName(row.specialist)} on ${aliasedName(row.variant.model, row.variant.alias)} instead of ${aliasedName(row.baseline.model, row.baseline.alias)}`;
  }
  return `${specialistName(row.specialist)} ${joiner} ${settingWords(key, row.variant?.[key])} instead of ${settingWords(key, row.baseline?.[key])}`;
}

// The exact change a recommendation applies: "Concurrency: effort from the default to high".
export function changeWords(change, baseline = {}) {
  const key = change?.model ? 'model' : 'effort';
  if (key === 'model' && sameModel(change, baseline)) {
    return `${specialistName(change.specialist)}: model from ${aliasedName(baseline.model, baseline.alias)} to ${aliasedName(change.model, change.alias)}`;
  }
  const from = key === 'model' ? modelName(baseline?.model) : baseline?.effort || 'the default';
  const to = key === 'model' ? modelName(change.model) : change?.effort;
  return `${specialistName(change?.specialist)}: ${key} from ${from} to ${to}`;
}

const PUSH_PHRASE = {
  rebase: 'rebased', merge_from_base: 'updated by a merge from main', force_rewrite: 'force pushed with new content',
  fix_after_review: 'with fixes pushed after review', new_scope: 'with new work pushed', armature_fix: 'with an automatic fix from Armature',
  unclassified: 'with a push not yet classified',
};
const STAGE_ORDER = {
  waiting_first_review: 'longest wait for a first review first', in_review: 'longest in review rounds first', waiting_on_ci: 'longest wait on CI first',
  approved_to_merged: 'longest wait between approval and merge first', blocked: 'longest blocked by conflicts first',
};

// "Which pull requests?" The answer names every filter, so a shared link says what it lists.
export function prsAnswer(data, { repoName = '', personName = '' } = {}) {
  const total = isNum(data.total) ? Number(data.total) : (data.prs || []).length;
  const where = repoName ? ` in ${repoName}` : '';
  const by = personName ? ` by ${personName}` : '';
  const flag = data.category ? ` with a ${categoryPhrase(data.category)} flag` : '';
  let what = 'opened or merged';
  let order = 'newest first';
  if (data.stage) {
    what = 'merged';
    order = STAGE_ORDER[data.stage] || 'longest first';
  } else if (data.push_class) what = PUSH_PHRASE[data.push_class] || `with a ${pushKindName(data.push_class).toLowerCase()} push`;
  const days = data.scope.days;
  if (!total) return `No pull requests ${what}${where}${by}${flag} in the last ${days} days.`;
  return `${total === 1 ? 'One pull request' : `${fixed(total)} pull requests`} ${what}${where}${by}${flag} in the last ${days} days, ${order}.`;
}

// "What fixes are waiting on me?"
export function fixesAnswer(data) {
  const items = data.waiting || [];
  const waiting = items.length;
  if (!waiting) return 'Nothing is waiting for you.';
  // Someone who can't decide is told who does, not that the fixes wait for them.
  const whom = items.some((item) => item.may_decide) ? 'your approval' : 'a team lead';
  const first = `${waiting === 1 ? 'One fix is' : `${plural(waiting, 'fix', 'fixes')} are`} waiting for ${whom}.`;
  const labels = new Set(items.map((item) => item.pattern?.label));
  const repos = new Set(items.map((item) => item.pattern?.repo));
  if (labels.size !== 1 || repos.size !== 1 || !items[0].pattern?.label || !items[0].pattern?.repo) return first;
  const who = waiting === 1 ? 'It targets' : waiting === 2 ? 'Both target' : 'All target';
  return `${first} ${who} ${categoryPhrase(items[0].pattern.label)} in ${repoShort(items[0].pattern.repo)}.`;
}

// "What happened in tree-service #4127?"
export function prAnswer(data) {
  const pr = data.pr || {};
  const flags = (data.rounds || []).flatMap((round) => round.flags || []);
  const kept = flags.filter((flag) => flag.status === 'kept').length;
  const dropped = flags.filter((flag) => flag.status === 'dropped').length;
  const by = pr.author ? `, by ${pr.author}` : '';
  const first = `${pr.title || 'Untitled'}${by}.`;
  const rounds = plural(pr.rounds || 0, 'review round');
  let state;
  if (pr.merged_at) state = `Merged ${monthDay(pr.merged_at)} after ${rounds}`;
  else if (pr.state === 'closed') state = `Closed without merging after ${rounds}`;
  else state = `Open since ${monthDay(pr.created_at)}, ${rounds} so far`;
  const flagText = flags.length ? `, with ${plural(flags.length, 'flag')}${kept || dropped ? ` (${[kept ? `${fixed(kept)} kept` : '', dropped ? `${fixed(dropped)} dropped` : ''].filter(Boolean).join(', ')})` : ''}` : '';
  const bug = (data.links || []).find((link) => link.direction === 'fixed_by');
  const bugText = bug ? ` It was later ${bug.kind === 'revert' ? 'reverted' : 'fixed'} by #${bug.pr?.number}.` : '';
  return `${first} ${state}${flagText}.${bugText}`;
}

export const teamMedianText = (value, unit, digits) => (isNum(value) ? `Team median ${figureText(value, unit, Number(value) % 1 ? Math.max(1, digits) : 0)}` : 'No team median yet');
