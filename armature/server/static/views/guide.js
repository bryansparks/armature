import { INSTRUCTION_MAX } from '../assistant.js';
import { clear, element, errorBox, link, loading, pageHeader } from '../dom.js';
import { navigate } from '../guard.js';
import { hashFor } from '../router.js';
import { isAdmin, ledTeamIds } from '../session.js';
import { loadScopes, scopeSelect } from './definition.js';

const EXAMPLES = [
  {
    label: 'Example: Review a pull request',
    answers: {
      trigger: 'A pull request opens or gets new commits.',
      steps: '1. Read the changed files.\n2. Check them for bugs, security problems, and missing tests.\n3. Write the findings as one short list, most important first.\n4. Wait for a person to approve the comment.\n5. Post the comment on the pull request.',
      output: 'One review comment on the pull request.',
      never: 'Never push code, and never approve or merge the pull request.',
    },
  },
  {
    label: 'Example: Summarize a Jira ticket',
    answers: {
      trigger: 'Someone asks for a summary of a Jira ticket by its key.',
      steps: '1. Read the ticket, its comments, and any linked tickets.\n2. Pick out the goal, the current status, open questions, and blockers.\n3. Write a short summary a new teammate could follow.',
      output: 'A summary of five to ten lines, with open questions listed at the end.',
      never: 'Never change the ticket or post anything to it.',
    },
  },
];

export function composeDescription({ trigger = '', steps = '', output = '', never = '' } = {}) {
  const parts = [
    ['Starts when: ', trigger],
    ['Steps:\n', steps],
    ['Produces: ', output],
    ['Never: ', never],
  ].map(([heading, text]) => [heading, String(text ?? '').trim()]).filter(([, text]) => text);
  return parts.map(([heading, text]) => heading + text).join('\n\n').slice(0, INSTRUCTION_MAX);
}

function step(number, title, ...children) {
  const headingId = `guide-step-${number}`;
  return element('section', { class: 'card guide-step', 'aria-labelledby': headingId },
    element('h2', { class: 'inline', id: headingId }, element('span', { class: 'guide-number', 'aria-hidden': 'true' }, String(number)), title),
    ...children);
}

function field(id, label, hint, control) {
  control.id = id;
  control.setAttribute('aria-describedby', `${id}-hint`);
  return element('div', { class: 'guide-field' }, element('label', { for: id }, label), element('span', { class: 'guide-hint', id: `${id}-hint` }, hint), control);
}

export function render(root) {
  root.append(pageHeader('Create a workflow'),
    element('p', { class: 'guide-intro' }, 'Armature runs AI workflows that take several steps. You describe what you want in plain words, Claude drafts the workflow (the YAML), and you review it before anything runs.'));

  const accessBody = element('div', {}, loading());
  root.append(step(1, 'Check your access', accessBody));

  const inputs = {
    trigger: element('input', { type: 'text', maxlength: 300, placeholder: 'For example: a pull request opens.' }),
    steps: element('textarea', { rows: 6, maxlength: 2500, placeholder: '1. Read the changed files.\n2. Check them for problems.\n3. Post one comment with the findings.' }),
    output: element('input', { type: 'text', maxlength: 500, placeholder: 'For example: one comment on the pull request.' }),
    never: element('input', { type: 'text', maxlength: 500, placeholder: 'For example: never push code.' }),
  };
  const answers = () => Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value]));
  const exampleButtons = EXAMPLES.map((example) => element('button', { type: 'button', onclick: () => {
    for (const [key, value] of Object.entries(example.answers)) inputs[key].value = value;
    update();
  } }, example.label));
  root.append(step(2, 'Describe what it should do',
    element('p', { class: 'guide-prose' }, 'Answer in your own words. Claude turns your answers into the workflow.'),
    element('div', { class: 'guide-examples' }, exampleButtons),
    element('div', { class: 'stacked-form guide-fields' },
      field('guide-trigger', 'What starts it?', 'The event or request that kicks it off.', inputs.trigger),
      field('guide-steps', 'What should it do, step by step?', 'One step per line. Two or three steps is a good start.', inputs.steps),
      field('guide-output', 'What should it produce?', 'A comment, a report, a summary, or a change.', inputs.output),
      field('guide-never', 'What must it never do? (optional)', 'Limits Claude should build in.', inputs.never)),
    element('h3', { class: 'guide-subhead' }, 'Tips'),
    element('ul', { class: 'guide-list' },
      element('li', {}, 'Be specific about the output.'),
      element('li', {}, 'Say if a person should approve before anything goes out.'),
      element('li', {}, 'Start with two or three steps. You can add more later.'))));

  const draftButton = element('button', { type: 'button', class: 'primary', disabled: true, 'aria-describedby': 'guide-draft-reason' }, 'Draft it with Claude');
  const reason = element('span', { class: 'note', id: 'guide-draft-reason' });
  let scopeControl;
  let accessFailed = false;
  root.append(step(3, 'Let Claude draft it',
    element('div', { class: 'wf-actions' }, draftButton, reason),
    element('h3', { class: 'guide-subhead' }, 'What happens next'),
    element('ol', { class: 'guide-list' },
      element('li', {}, 'The New workflow page opens with your description filled in. Give the workflow a short name.'),
      element('li', {}, 'Press Send.'),
      element('li', {}, 'Claude may ask a question first. Answer it in the same box and press Send again.'),
      element('li', {}, "Read Claude's proposal. Show YAML changes shows the exact definition."),
      element('li', {}, 'Not right? Describe the fix under Refine this and press Send.'),
      element('li', {}, "Press Apply to save it. That opens the workflow's page.")),
    element('p', { class: 'note' }, 'Prefer to write YAML yourself? Use Start from YAML on the New workflow page.')));

  root.append(step(4, 'Test it safely',
    element('p', { class: 'guide-prose' }, "On the workflow's page, the Test run card has a Start test run button. It replays recorded step outputs, so nothing runs, posts, or pushes, and no model is called."),
    element('p', { class: 'guide-prose' }, 'A new workflow has no recorded outputs yet, so tick Treat steps with no recorded output as empty. Otherwise every step fails.'),
    element('p', { class: 'guide-prose' }, 'When you want it to run for real, ask an admin or team lead to set up a trigger.')));

  root.append(step(5, 'Change it later',
    element('p', { class: 'guide-prose' }, 'If you can edit the workflow, use Describe a change on its page. Every saved change is a new version you can go back to.')));

  function update() {
    let why = '';
    if (accessFailed) why = "Couldn't check your access. Reload the page to try again.";
    else if (scopeControl === undefined) why = 'Checking your access…';
    else if (!scopeControl) why = 'You need a project you can add workflows to.';
    else if (!inputs.steps.value.trim()) why = 'Fill in what it should do, step by step.';
    draftButton.disabled = Boolean(why);
    reason.textContent = why;
  }

  for (const input of Object.values(inputs)) input.addEventListener('input', update);
  draftButton.addEventListener('click', () => {
    if (!scopeControl || !inputs.steps.value.trim()) return;
    navigate(hashFor('new-workflow', [], { scope: scopeControl.select.value, describe: composeDescription(answers()) }));
  });

  update();
  loadScopes().then((all) => {
    const scopes = all.filter((scope) => scope.scope_kind === 'project');
    clear(accessBody);
    if (scopes.length) {
      scopeControl = scopeSelect('guide-scope', scopes, null);
      accessBody.append(element('label', { class: 'stacked guide-scope', for: scopeControl.select.id }, 'You can add workflows to:', scopeControl.select));
    } else {
      scopeControl = null;
      accessBody.append(isAdmin() || ledTeamIds().length
        ? element('p', { class: 'banner', role: 'note' }, "You don't have a project yet. Add one on the ", link(hashFor('projects'), 'Projects page'), ', then come back.')
        : element('p', { class: 'banner', role: 'note' }, "You can't add workflows yet. Ask an admin to make you a project lead on the project you'll work in."));
    }
    update();
  }).catch((err) => {
    accessFailed = true;
    clear(accessBody).append(errorBox(err));
    update();
  });
}
