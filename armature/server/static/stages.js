// The Steps form edits these cards. YAML stays the source of truth: /parse makes cards, /render writes them back.

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

export function cardFromStage(stage) {
  return {
    id: String(stage?.id ?? ''),
    prev_id: stage?.prev_id !== undefined ? stage.prev_id : stage?.id ?? null,
    adapter: stage?.adapter ?? '',
    depends_on: Array.isArray(stage?.depends_on) ? stage.depends_on.map(String) : [],
    with: isObject(stage?.with) ? { ...stage.with } : {},
    fan_out: stage?.fan_out ?? null,
    partition_source: stage?.partition_source ?? '',
    fail_as_value: Boolean(stage?.fail_as_value),
    skip_if: stage?.skip_if ?? '',
    line: stage?.line ?? null,
  };
}

export function stagePayload(card) {
  return {
    id: card.id,
    prev_id: card.prev_id ?? null,
    adapter: card.adapter || null,
    depends_on: [...card.depends_on],
    with: { ...card.with },
    fan_out: card.fan_out === '' ? null : card.fan_out ?? null,
    partition_source: card.partition_source || null,
    fail_as_value: Boolean(card.fail_as_value),
    skip_if: card.skip_if || null,
  };
}

export const renderPayload = (body, cards) => ({ body, stages: cards.map(stagePayload) });

export function moveCard(cards, index, delta) {
  const target = index + delta;
  if (target < 0 || target >= cards.length) return cards;
  const moved = [...cards];
  [moved[index], moved[target]] = [moved[target], moved[index]];
  return moved;
}

export function removeCard(cards, index) {
  const removedId = cards[index]?.id;
  return cards.filter((_, position) => position !== index)
    .map((card) => ({ ...card, depends_on: card.depends_on.filter((dependency) => dependency !== removedId) }));
}

export function renameCard(cards, index, newId) {
  const oldId = cards[index].id;
  return cards.map((card, position) => ({
    ...card,
    id: position === index ? newId : card.id,
    depends_on: card.depends_on.map((dependency) => (dependency === oldId ? newId : dependency)),
  }));
}

export function addCard(cards) {
  const taken = new Set(cards.map((card) => card.id));
  let number = cards.length + 1;
  while (taken.has(`step-${number}`)) number += 1;
  const previous = cards.at(-1);
  return [...cards, cardFromStage({ id: `step-${number}`, prev_id: null, depends_on: previous ? [previous.id] : [] })];
}

// Once the YAML holds the rendered ids, the next render must name those as prev_id.
export function afterRender(cards, sent) {
  const renamed = new Map(sent.filter((stage) => stage.prev_id !== null).map((stage) => [stage.prev_id, stage.id]));
  const added = new Set(sent.filter((stage) => stage.prev_id === null).map((stage) => stage.id));
  return cards.map((card) => {
    if (card.prev_id !== null && renamed.has(card.prev_id)) return { ...card, prev_id: renamed.get(card.prev_id) };
    if (card.prev_id === null && added.has(card.id)) return { ...card, prev_id: card.id };
    return card;
  });
}

export function stageProblems(cards) {
  const problems = [];
  const seen = new Set();
  cards.forEach((card, index) => {
    if (!card.id.trim()) problems.push(`Step ${index + 1} needs an ID.`);
    else if (seen.has(card.id)) problems.push(`Two steps are named ${card.id}.`);
    seen.add(card.id);
    if (card.depends_on.includes(card.id)) problems.push(`${card.id} cannot depend on itself.`);
  });
  return problems;
}

export function adapterChoices(catalog, own, current) {
  const choices = [];
  const seen = new Set();
  const add = (value, group) => {
    if (!value || seen.has(value)) return;
    seen.add(value);
    choices.push({ value, group });
  };
  for (const pack of catalog || []) for (const adapter of pack.adapters || []) add(adapter.ref, pack.name);
  for (const name of own || []) add(name, 'This workflow');
  add(current, 'Other');
  return choices;
}
