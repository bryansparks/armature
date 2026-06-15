# Build Your First Armature Workflow

A hands-on walkthrough. By the end you will have a working 6-agent research pipeline running on your machine, understand how agents hand data to each other, and know the two or three commands you will use every day.

No prior Armature experience needed. You need Python 3.11+, an Anthropic API key, and about 20 minutes.

---

## What we're building

A **deep research report generator**. You give it a topic; six specialized agents collaborate to produce a structured report:

```
                   ┌─────────────────────┐
                   │   scope_setter      │  orchestrator
                   │   (defines agenda)  │
                   └──────────┬──────────┘
                              │  depends_on: scope_setter
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
   ┌─────────────┐   ┌─────────────┐   ┌─────────────────┐
   │ background  │   │  evidence   │   │  counterpoint   │
   │ researcher  │   │  gatherer   │   │  finder         │
   └──────┬──────┘   └──────┬──────┘   └────────┬────────┘
          └─────────────────┼────────────────────┘
                            │  depends_on: all three researchers
                            ▼
                   ┌─────────────────────┐
                   │    synthesizer      │  worker
                   │  (weaves it all     │
                   │   into prose)       │
                   └──────────┬──────────┘
                              │  depends_on: synthesizer
                              ▼
                   ┌─────────────────────┐
                   │      editor         │  judge
                   │  (scores quality,   │
                   │   produces final)   │
                   └─────────────────────┘
```

The three middle-row researchers run **in parallel** — the engine fans them out automatically because none of them depend on each other, only on `scope_setter`. The synthesizer fans them back in.

---

## 1. Prerequisites

```bash
pip install armature
export ANTHROPIC_API_KEY=sk-ant-...    # paste your key here
```

Verify the install:

```bash
armature --help
```

---

## 2. Project setup

```bash
mkdir deep-research
cd deep-research
```

That's the whole setup. No Docker, no database, no config files. Armature is a Python library with a CLI — it starts immediately.

---

## 3. Using Claude Code to draft the spec

If you have Claude Code, this is the fastest path. Open a new session in `deep-research/`, load the user guide into context, and ask:

```
@USER-GUIDE.md

Help me create an Armature workflow spec called research.yml.
I want 6 stages:
  1. scope_setter (orchestrator) — takes a topic input and defines
     3-5 key research questions and an outline for the report
  2. background_researcher (researcher) — foundational context,
     history, key definitions
  3. evidence_gatherer (researcher) — concrete data points, studies,
     real-world examples
  4. counterpoint_finder (researcher) — criticisms, limitations,
     alternative viewpoints
  5. synthesizer (worker) — combines all three into structured prose
  6. editor (judge) — scores quality and produces the polished final

Use claude-haiku for researchers and workers, claude-sonnet for
orchestrator and judge. Output from synthesizer and editor should
be guided_json. Give the editor a quality_score field.
```

Claude Code will draft the full YAML. Review it, make any tweaks, save it as `research.yml`. Or skip Claude Code entirely and use the spec below.

---

## 4. The workflow spec

Create `research.yml`:

```yaml
name: deep-research
version: "1.0"
description: >
  Six-agent research pipeline. Scope setter defines the agenda;
  three researchers run in parallel; synthesizer weaves results
  into prose; editor judges quality and produces the final report.

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  large:
    provider: anthropic
    model: claude-sonnet-4-6

contracts:
  inputs:
    - name: topic
      type: string
      description: "What to research (e.g. 'impact of remote work on urban density')"
    - name: audience
      type: string
      description: "Who will read this — shapes depth and tone (e.g. 'general reader', 'policy maker')"

stages:

  # ── Stage 1: Orchestrator ────────────────────────────────────────────────────
  - id: scope_setter
    role:
      name: Scope Setter
      type: orchestrator
      model_tier: large
      description: |
        You are setting the research agenda for a report on: {{ topic }}
        Audience: {{ audience }}

        Define a focused research agenda. Output:
        - A one-sentence thesis statement
        - 3-5 specific research questions the report must answer
        - A proposed section outline (list of section titles)
        - Guidance for each of the three researcher roles:
            background_guidance: what foundational context to cover
            evidence_guidance: what specific data, studies, or examples to find
            counterpoint_guidance: what criticisms or limitations to investigate
    output_mode: guided_json
    output_schema:
      type: object
      required: [thesis, research_questions, outline, background_guidance,
                 evidence_guidance, counterpoint_guidance]
      properties:
        thesis:            {type: string}
        research_questions: {type: array, items: {type: string}}
        outline:           {type: array, items: {type: string}}
        background_guidance: {type: string}
        evidence_guidance:   {type: string}
        counterpoint_guidance: {type: string}
    depends_on: []

  # ── Stages 2-4: Parallel researchers ────────────────────────────────────────
  - id: background_researcher
    role:
      name: Background Researcher
      type: researcher
      model_tier: small
      description: |
        Research topic: {{ topic }}
        Your specific assignment: {{ scope_setter.background_guidance }}

        Produce foundational context: key definitions, history, major developments,
        and the landscape as it exists today. Be concrete and specific — no generalities.
        Organize your findings under clear sub-headings.
    output_mode: text
    depends_on: [scope_setter]

  - id: evidence_gatherer
    role:
      name: Evidence Gatherer
      type: researcher
      model_tier: small
      description: |
        Research topic: {{ topic }}
        Your specific assignment: {{ scope_setter.evidence_guidance }}

        Find concrete supporting material: studies, statistics, named examples,
        documented cases, expert quotes (attributed). Each claim should be specific
        enough that a reader could verify it. Organize by the relevant research
        question it answers.
    output_mode: text
    depends_on: [scope_setter]

  - id: counterpoint_finder
    role:
      name: Counterpoint Finder
      type: researcher
      model_tier: small
      description: |
        Research topic: {{ topic }}
        Your specific assignment: {{ scope_setter.counterpoint_guidance }}

        Steel-man the opposition. Find the strongest criticisms, documented
        failure modes, legitimate alternative viewpoints, and known limitations
        of the mainstream position. Do not dismiss these — a good report
        acknowledges complexity.
    output_mode: text
    depends_on: [scope_setter]

  # ── Stage 5: Synthesizer ─────────────────────────────────────────────────────
  - id: synthesizer
    role:
      name: Synthesizer
      type: worker
      model_tier: small
      description: |
        You are writing a research report on: {{ topic }}
        Audience: {{ audience }}

        Use the outline from scope_setter and weave together all three
        research streams into coherent prose. Do not just concatenate —
        integrate the evidence and counterpoints into each section naturally.

        Follow this outline: {{ scope_setter.outline }}

        You have access to:
        - Background research (scope_setter.background_guidance was the brief)
        - Evidence and examples
        - Counterpoints and criticisms

        Write complete, flowing paragraphs. Aim for substance over length.
    output_mode: guided_json
    output_schema:
      type: object
      required: [title, sections]
      properties:
        title:
          type: string
        sections:
          type: array
          items:
            type: object
            required: [heading, body]
            properties:
              heading: {type: string}
              body:    {type: string}
    depends_on: [background_researcher, evidence_gatherer, counterpoint_finder]

  # ── Stage 6: Editor / Judge ───────────────────────────────────────────────────
  - id: editor
    role:
      name: Editor
      type: judge
      model_tier: large
      description: |
        You are the final editor for a research report on: {{ topic }}
        Audience: {{ audience }}

        Review the synthesizer's draft. Assess it on:
        1. Thesis clarity — does each section serve the thesis?
        2. Evidence quality — are claims specific and believable?
        3. Balance — does it fairly represent counterpoints?
        4. Prose quality — is it clear, direct, engaging for the audience?

        Then produce the polished final report:
        - Keep strong sections as-is
        - Rewrite weak sections (explain what you changed and why)
        - Add a one-paragraph executive summary at the top
        - Add a "Key Takeaways" bullet list at the end (3-5 bullets)

        Your quality_score should reflect the DRAFT quality before your edits
        (so the improvement loop can track if prompts need work).
    output_mode: guided_json
    output_schema:
      type: object
      required: [quality_score, quality_notes, executive_summary,
                 sections, key_takeaways]
      properties:
        quality_score:
          type: number
          minimum: 0
          maximum: 1
          description: "Draft quality before edits (0=unusable, 1=publish-ready)"
        quality_notes:
          type: string
          description: "What the editor changed and why"
        executive_summary:
          type: string
        sections:
          type: array
          items:
            type: object
            required: [heading, body]
            properties:
              heading: {type: string}
              body:    {type: string}
        key_takeaways:
          type: array
          items: {type: string}
    depends_on: [synthesizer]
```

---

## 5. Validate it first

Before spending API budget, check for spec errors:

```bash
armature validate research.yml
```

You should see:

```
✓ 'deep-research' is valid (6 stages)
```

If there are errors, the validator tells you exactly which stage and what's wrong. Fix, re-validate, repeat.

---

## 6. Run it

```bash
armature run research.yml \
  --input topic="the four-day work week" \
  --input audience="HR leadership at mid-sized companies"
```

You will see live progress as stages complete:

```
Running: deep-research
  → scope_setter (llm) [Scope Setter]
  ✓ scope_setter (4.2s)
  → background_researcher (llm) [Background Researcher]
  → evidence_gatherer (llm) [Evidence Gatherer]
  → counterpoint_finder (llm) [Counterpoint Finder]
  ✓ background_researcher (6.1s)
  ✓ evidence_gatherer (5.8s)
  ✓ counterpoint_finder (7.2s)
  → synthesizer (llm) [Synthesizer]
  ✓ synthesizer (9.4s)
  → editor (llm) [Editor]
  ✓ editor (11.1s)

Done in 38.8s — 6 ran, 0 skipped, 0 resumed, 0 failed
```

Notice that `background_researcher`, `evidence_gatherer`, and `counterpoint_finder` all start at the same time — that's the parallel fan-out. Total runtime is dominated by the slowest of the three, not all three added together.

---

## 7. Save the output

```bash
armature run research.yml \
  --input topic="the four-day work week" \
  --input audience="HR leadership at mid-sized companies" \
  --output report.json \
  --quiet
```

`report.json` contains every stage's output. The part you care about:

```python
import json

result = json.load(open("report.json"))
report = result["editor"]

print(f"Quality score (draft): {report['quality_score']:.0%}")
print()
print("=== EXECUTIVE SUMMARY ===")
print(report["executive_summary"])
print()
for section in report["sections"]:
    print(f"\n## {section['heading']}")
    print(section["body"])
print("\n=== KEY TAKEAWAYS ===")
for t in report["key_takeaways"]:
    print(f"  • {t}")
```

---

## 8. How the agents actually talk to each other

This is the most important thing to understand. There is **one shared context dict** per run. It starts with your inputs:

```python
context = {
    "topic": "the four-day work week",
    "audience": "HR leadership at mid-sized companies",
}
```

After `scope_setter` runs, its output is added:

```python
context["scope_setter"] = {
    "thesis": "The four-day work week improves output quality...",
    "research_questions": ["What does the evidence say about...", ...],
    "outline": ["Introduction", "The Evidence Base", ...],
    "background_guidance": "Cover the history of the 40-hour week...",
    "evidence_guidance": "Focus on the Iceland, Microsoft Japan, and...",
    "counterpoint_guidance": "Address industries where it doesn't work...",
}
```

The three researchers run in parallel. Each one sees `scope_setter` (because they `depends_on: [scope_setter]`) and their own role description includes `{{ scope_setter.background_guidance }}` etc. — so each researcher gets their specific marching orders from the orchestrator, not generic instructions.

After all three complete:

```python
context["background_researcher"] = {"content": "The 40-hour standard week was..."}
context["evidence_gatherer"]     = {"content": "Iceland's 2015-2019 trials showed..."}
context["counterpoint_finder"]   = {"content": "Manufacturing and healthcare present..."}
```

The `synthesizer` sees all of this in its context automatically — it `depends_on` all three researchers. It doesn't need any special wiring; the engine puts every accumulated key in its system prompt under `## Current Context`.

The `editor` sees everything, including `synthesizer.sections`, and produces the polished final.

**You never write data-passing code.** The DAG structure plus `depends_on` is all you declare.

---

## 9. Extend it: add memory across runs

After a few runs on related topics, you might want each new report to be aware of what previous reports concluded. Add memory to the spec:

```yaml
# Add this at the top level of research.yml:
memory:
  enabled: true
  capture:
    - stage: editor
      key: key_takeaways
      max_entries: 5         # keep the last 5 runs' takeaways
  inject_as: _memory

# Then in scope_setter's description, add:
# {% if _memory %}
# Prior research on related topics found:
# {{ _memory.editor.key_takeaways }}
# Avoid repeating those insights unless this run adds new nuance.
# {% endif %}
```

Now each run builds on the last. The scope setter knows what you've already established.

---

## 10. Extend it: add a human review gate

Before the editor finalizes, you might want a human to read the draft and flag concerns:

```yaml
# Add after synthesizer, before editor:
  - id: draft_review
    gate: human
    present: |
      ## Draft ready for review

      Topic: {{ topic }}
      Draft quality (self-assessed): see synthesizer output

      EXECUTIVE SUMMARY PREVIEW:
      {{ synthesizer.sections[0].body }}

      Approve to proceed to final editing, or provide feedback
      for the synthesizer to incorporate.
    depends_on: [synthesizer]

# Then change editor's depends_on:
  - id: editor
    depends_on: [draft_review]   # was: [synthesizer]
```

When you run now, the workflow pauses after the synthesizer and asks:

```
Approve? [yes/no/feedback]:
```

Type `yes` to continue. Type feedback and the editor will see `draft_review.feedback` in its context.

---

## 11. Try different topics

The spec works on any research topic. Some good tests:

```bash
# Narrow, technical
armature run research.yml \
  --input topic="vector database indexing strategies" \
  --input audience="senior engineers choosing a vector DB"

# Broad, social
armature run research.yml \
  --input topic="charter school outcomes in urban districts" \
  --input audience="school board members"

# Business
armature run research.yml \
  --input topic="platform pricing strategies for SaaS" \
  --input audience="early-stage startup founders"
```

---

## 12. Run the improvement loop

After 3+ runs, check the workflow's health:

```bash
armature improve research.yml
```

This computes the HQS (Harness Quality Score) from your trace history, diagnoses failure signatures (low quality scores, schema errors, slow stages), and proposes targeted spec revisions. If the HQS is below 0.90, it rewrites the weak prompts and applies the change automatically.

---

## 13. What to build next

You have a working 6-agent pipeline. Here is where to take it:

| Idea | What to change |
|------|----------------|
| Add web search | Register an `http_get` tool; add `tools: [http_get]` to each researcher stage so they can actually fetch URLs |
| Export for fine-tuning | `armature export-traces --workflow deep-research --output training.jsonl` after 10+ runs |
| Parallelize more | Add a 4th researcher (e.g., `trend_spotter`) — just add the stage with `depends_on: [scope_setter]` |
| Make it a service | `armature serve` exposes it over HTTP; post `{"spec": "research.yml", "inputs": {...}}` |
| Auto-run nightly | Wrap in a cron job or GitHub Action; each run accumulates traces for the improvement loop |

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'armature'`**
You're not in the right virtualenv. `pip install armature` in the active env.

**`AuthenticationError` from litellm**
`ANTHROPIC_API_KEY` is not set or is incorrect. `echo $ANTHROPIC_API_KEY` to check.

**Stage fails with schema error**
The model returned JSON that didn't match `output_schema`. Armature automatically escalates to the `large` tier and retries — you'll see a `retry_attempt` event. If it keeps failing, relax the `required` fields in the schema or add `on_fail: {loop: {max: 2}}` to that stage.

**Runs are slow**
The three parallel researchers all use `claude-haiku` (fast, cheap). If you want faster results, set `model_tier: small` on `scope_setter` and `editor` too — though response quality will drop. The main latency is the sequential chain: scope_setter (4s) → parallel batch (~7s) → synthesizer (9s) → editor (11s).

**Output looks shallow**
Enrich the role descriptions. Each researcher's `description` is their system prompt — the more specific the brief, the more specific the output. `scope_setter.background_guidance`, `.evidence_guidance`, and `.counterpoint_guidance` are the levers; they flow from orchestrator to researcher automatically.
