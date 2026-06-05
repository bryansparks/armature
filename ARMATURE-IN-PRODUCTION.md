# Armature in Production

When every harness primitive is wired together, the whole is greater than the sum of its parts.

---

Most agentic frameworks hand you a set of primitives and leave the wiring to you. Safety is a middleware you bolt on. Fault tolerance is a try/except you write. Quality measurement is a dashboard you build separately, in a different system, maintained by a different team.

Armature's design premise is different. The primitives are already wired together. Safety rules compose with fan-out automatically. Checkpoint and continuation work across any stage topology. IHR reads from the same traces that governance rules write to. You do not integrate these features — you declare them in one YAML file and the harness does the integration.

The value of a harness is not any individual feature. The value is what happens when the features compound.

This document describes three production stacks — each one a natural composition of Armature features, each one solving a problem class that no single feature solves alone. A production deployment typically uses all three.

---

## The three stacks

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         Production Deployment                            │
│                                                                          │
│  ┌─────────────────────┐  ┌──────────────────────┐  ┌─────────────────┐ │
│  │  Stack 1            │  │  Stack 2             │  │  Stack 3        │ │
│  │  Enterprise         │  │  Reliable            │  │  Self-Improving │ │
│  │  Governance         │  │  Long-Running        │  │  Quality Loop   │ │
│  │                     │  │  Pipelines           │  │                 │ │
│  │  safety_rules       │  │  checkpoint          │  │  IHR            │ │
│  │  safety_mode:strict │  │  continuation        │  │  traces         │ │
│  │  gate: human        │  │  fan_out             │  │  evaluate:      │ │
│  │  rogue_signals      │  │  model_tiers         │  │  judge pattern  │ │
│  │                     │  │  on_fail.loop        │  │  quorum score   │ │
│  │  Defines            │  │                      │  │  armature       │ │
│  │  what agents        │  │  Ensures the         │  │  improve        │ │
│  │  can do             │  │  work completes      │  │                 │ │
│  │                     │  │                      │  │  Ensures the    │ │
│  └─────────────────────┘  └──────────────────────┘  │  output is      │ │
│                                                      │  worth running  │ │
│                                                      └─────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Stack 1: Enterprise Governance

**Features:** Safety rules + strict mode + human gates + rogue signal counter

---

Enterprise AI teams face a recurring problem. Agents are powerful, but their actions are hard to govern. You can read the code and understand what an agent *could* do; you cannot easily tell what it *did* do, or whether the constraints you intended are actually enforced. Traditional software has this problem too — which is why we invented firewalls, IAM policies, and audit logs. Agentic workflows need the same infrastructure.

Armature's governance stack answers this at three levels.

**What agents can do** is declared in `safety_rules:`. Each rule targets a tool, inspects an argument field, and takes a policy action — block, log, require approval. In strict mode, the rule list becomes an allowlist: anything not explicitly permitted is blocked by default. This is the IAM default-deny posture applied to tool calls. The rogue signal counter surfaces how often agents attempted something prohibited — zero means the workflow ran entirely within its declared policy, non-zero means something tried to exceed its bounds.

**Where humans must decide** is declared with `gate: human`. Gates are first-class stages in the workflow DAG. They pause execution, present context from upstream stages using Jinja2 templates, and collect structured approval or feedback. The gate result flows into downstream stages as a normal context variable — stages can branch on `{{ review_gate.approved }}` or incorporate `{{ review_gate.feedback }}` into their instructions. This is accountability encoded as structure, not as a comment in a prompt.

**The governance config is version-controlled YAML** alongside the workflow spec. Security teams can audit `safety_rules:` in a PR. Operations teams can grep for `gate: human` to find every approval checkpoint. The spec is the contract.

```yaml
name: vendor-contract-processor
version: "1.0"
mission: "Review incoming vendor contracts for compliance and approval."

safety_mode: strict

safety_rules:
  # Permitted tool surface: read contracts, write to the designated workspace
  - tool: read_file
    condition: {field: path, op: contains, value: "/contracts/incoming/"}
    action: allow

  - tool: write_file
    condition: {field: path, op: contains, value: "/workspace/reviews/"}
    action: allow

  # Hard block: no external HTTP calls
  - tool: http_post
    condition: {field: url, op: not_contains, value: "legal.internal.corp.com"}
    action: block
    message: "External HTTP calls are not permitted in this workflow."

  # Hard block: no bash commands containing destructive patterns
  - tool: bash
    condition: {field: cmd, op: contains, value: "rm -rf"}
    action: block
    message: "Destructive deletion is prohibited."

  # Human approval required for any irreversible operation not covered above
  - tool: "*"
    condition: {field: _tool_reversibility, op: equals, value: irreversible}
    action: require_approval
    message: "Irreversible operations require explicit human sign-off."

stages:
  - id: analyse
    role:
      type: worker
      model_tier: small
      description: |
        Review this vendor contract for compliance issues.
        Contract: {{ contract_text }}
        Return:
          {"risk_level": "low|medium|high",
           "flagged_clauses": [...],
           "requires_legal_review": true|false,
           "summary": "..."}

  - id: legal_review_gate
    gate: human
    present: |
      Contract analysis complete.

      Risk level: {{ analyse.risk_level }}
      Flagged clauses: {{ analyse.flagged_clauses | length }}
      Summary: {{ analyse.summary }}

      Approve to proceed to remediation recommendations.
      Provide feedback to request revisions before proceeding.
    depends_on: [analyse]

  - id: recommend
    skip_if: "{{ legal_review_gate.approved == false }}"
    role:
      type: worker
      model_tier: small
      description: |
        Produce remediation recommendations for the flagged clauses.
        Original analysis: {{ analyse }}
        Legal reviewer feedback: {{ legal_review_gate.feedback | default('none') }}
        Return {"recommendations": [{"clause": "...", "suggested_revision": "..."}]}.
    depends_on: [legal_review_gate]
```

**What you get:** A compliance-ready agentic pipeline. The security team can read the governance layer in five minutes without understanding LLMs. The `rogue_signals` field in the run summary tells them, per run, whether the agent stayed within bounds. A workflow with `rogue_signals: 0` on every run means the governance config is correctly calibrated. A workflow with `rogue_signals: 3` means something tried to do something it should not — and the trace records exactly which call, which rule, and which decision.

---

## Stack 2: Reliable Long-Running Pipelines

**Features:** Checkpoint + continuation + model tiers + fan-out + on_fail.loop

---

A nightly compliance audit processes 500 documents. It costs $30 in LLM calls. It takes 45 minutes. Without fault tolerance, a transient network error at minute 40 means starting over — a fresh $30 charge, another 45-minute wait, and a missed SLA. With the reliability stack, it resumes from the failed stage, runs the remaining documents, and produces the same output at a fraction of the cost.

The reliability stack is built from five features that each address one failure mode, and together cover the complete surface area of production failure.

**Checkpoint** saves completed stage outputs so a crashed run can resume rather than restart. Every stage that completes writes its result to the checkpoint store. If the run fails, re-running the same command picks up exactly where execution stopped — stages that already succeeded are not re-run.

**Continuation** is the cross-run memory layer. Where checkpoint handles intra-run recovery, continuation handles inter-run state. A nightly audit that remembers how many high-risk documents it found yesterday can compare today's count and surface a trend. The prior run's output is injected into the current run's context as a named variable — downstream stages can reference it like any other stage result.

**Model tiers** make cost control a first-class concern. Worker stages doing bulk processing use a small, cheap model. The final judge or report writer uses a frontier model where quality matters. This is not a performance optimization — it is the correct architecture. Most of the work in a pipeline does not require frontier capability; applying it uniformly to 500 documents is waste. Declaring `model_tier: small` for workers and `model_tier: frontier` for orchestrators is the right default.

**Fan-out** processes the 500 documents in parallel, bounded to a configurable concurrency limit. Per-item failures are isolated — one corrupted document returns `{"_fan_out_error": "..."}` and the rest of the batch completes normally. Downstream stages can filter errors and continue. One bad file does not kill a 500-document run.

**on_fail.loop** retries individual stages on transient failures — API rate limits, network timeouts — with exponential backoff. The retry is declared in the spec, not in application code. Importantly, `ToolBlocked` exceptions (from safety rules) are never retried. Policy violations are not transient errors.

```yaml
name: nightly-compliance-audit
version: "1.0"
mission: "Nightly batch review of all incoming documents for regulatory compliance."

checkpoint: true

continuation:
  carry_forward:
    - key: final_report.high_risk_count
    - key: final_report.escalation_count
  inject_as: prior_run

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  frontier:
    provider: anthropic
    model: claude-opus-4-7

stages:
  - id: list_documents
    role:
      type: researcher
      model_tier: small
      description: |
        List all documents in /contracts/incoming that arrived today.
        Return {"documents": ["/contracts/incoming/doc1.pdf", ...]}.

  - id: review_each
    fan_out: 20
    fan_in: list
    partition_source: "{{ list_documents.documents }}"
    partition_key: doc_path
    inject_file_as: doc_content
    on_fail:
      loop: {max: 3, backoff_s: 5.0}
    role:
      type: worker
      model_tier: small
      description: |
        Review this document for compliance issues.
        Document: {{ doc_path }}
        Content: {{ doc_content }}
        Return:
          {"issues": [{"clause": "...", "severity": "low|medium|high"}],
           "risk_level": "low|medium|high",
           "requires_escalation": true|false}
    depends_on: [list_documents]

  - id: final_report
    role:
      type: orchestrator
      model_tier: frontier
      description: |
        Produce the nightly compliance summary from {{ review_each | length }} document reviews.

        Today's results:
          High-risk documents: {{ review_each | selectattr('risk_level', 'eq', 'high') | list | length }}
          Requiring escalation: {{ review_each | selectattr('requires_escalation') | list | length }}
          Processing errors: {{ review_each | selectattr('_fan_out_error', 'defined') | list | length }}

        Compared to prior run:
          Prior high-risk count: {{ prior_run.high_risk_count | default('no prior data') }}
          Trend: {{ 'improving' if (review_each | selectattr('risk_level', 'eq', 'high') | list | length) < (prior_run.high_risk_count | default(999)) else 'worsening or stable' }}

        Return a structured compliance summary with trend analysis and recommended escalations.
    depends_on: [review_each]
```

**What you get:** A pipeline that runs to completion at scale, recovers from transient failures, costs what it should cost, and remembers what it found last time. The operations team can see `checkpoint: true` in the spec and know that a 3am failure does not require a 3am restart from scratch. The finance team can see `model_tier: small` on the workers and `model_tier: frontier` only on the final report — and understand why the audit costs $30 instead of $300.

---

## Stack 3: Self-Improving Quality Loop

**Features:** IHR + traces + evaluate criteria + judge pattern + quorum scoring + armature improve

---

The hardest problem in production AI is degradation — workflows that work well on day one but silently drift as edge cases accumulate, model behavior shifts, or input data changes shape. Traditional software has error rates. Agentic workflows have IHR.

IHR — the Implicit Harness Rating — is a composite quality score computed over accumulated trace data. It aggregates five signals: output validity, success rate, quorum consensus from judge stages, latency, and escalation-free execution. It answers the question that matters: is this workflow actually working?

The quality loop closes the feedback cycle automatically. Every run produces traces. Judges emit confidence scores (quorum scores) that the harness extracts and records. `evaluate:` criteria on individual stages run LLM-powered assertions after each execution — semantic acceptance tests that catch failures `output_schema` cannot express. When IHR falls below a target, `armature improve` reads the failure signatures, diagnoses which stages are degrading and why, and proposes targeted YAML revisions. Safe changes (description enrichment, model tier upgrades, retry config) are applied automatically. Structural changes (adding stages, modifying safety rules) are written to a `.pending.yaml` file for human review.

The optimizer is accountable. Every revision it proposes comes with a falsifiable prediction: which failure signatures it expects to resolve. The next cycle verifies those predictions. If the optimizer consistently misses its targets, that is visible in the improvement log — a rising drift score signals that the system is oscillating rather than converging.

```yaml
name: contract-risk-assessment
version: "1.0"
mission: "Assess legal and financial risk in vendor contracts with high confidence."

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  medium:
    provider: anthropic
    model: claude-sonnet-4-6
  frontier:
    provider: anthropic
    model: claude-opus-4-7

stages:
  - id: analyst
    role:
      type: worker
      model_tier: small
      description: |
        Analyse this contract section for legal and financial risk.
        Contract: {{ contract_text }}
        Previous attempt context: {{ _last_result | default('none') }}
        Return:
          {"risk_level": "low|medium|high",
           "key_clauses": [...],
           "financial_exposure": "...",
           "confidence": 0.0-1.0}
    output_schema:
      required: [risk_level, key_clauses, financial_exposure]
    on_fail:
      loop:
        max: 3
        until: "{{ confidence >= 0.7 }}"
        backoff_s: 2.0
    evaluate:
      - "Output references specific clause numbers or section headings"
      - "Risk level is explicitly justified by evidence in key_clauses"
      - "Financial exposure estimate includes a quantitative range where data permits"
      - "No contradictions between risk_level and financial_exposure magnitude"

  - id: quality_judge
    role:
      type: judge
      model_tier: medium
      description: |
        Evaluate the quality and accuracy of this contract risk analysis.
        Original contract: {{ contract_text }}
        Analysis: {{ analyst }}

        Return:
          {"score": 0.0-1.0,
           "confidence": 0.0-1.0,
           "verdict": "pass|fail",
           "feedback": "specific issues or confirmation of quality"}
    output_schema:
      required: [score, confidence, verdict, feedback]
    depends_on: [analyst]

  - id: escalate_if_uncertain
    skip_if: "{{ quality_judge.confidence >= 0.75 and quality_judge.verdict == 'pass' }}"
    gate: human
    present: |
      Quality judge confidence is {{ quality_judge.confidence }} — below threshold.
      Verdict: {{ quality_judge.verdict }}

      Contract analysis:
        Risk level: {{ analyst.risk_level }}
        Key clauses: {{ analyst.key_clauses }}
        Financial exposure: {{ analyst.financial_exposure }}

      Judge feedback: {{ quality_judge.feedback }}

      Approve to use this analysis, or provide feedback to request revision.
    depends_on: [quality_judge]

  - id: final_assessment
    role:
      type: orchestrator
      model_tier: frontier
      description: |
        Produce the final risk assessment for this contract.
        Analysis: {{ analyst }}
        Quality review: {{ quality_judge }}
        Human escalation result: {{ escalate_if_uncertain | default({'approved': true, 'feedback': null}) }}
        Return a structured final assessment with risk rating, financial exposure, and recommended actions.
    depends_on: [quality_judge]
```

After ten runs, the quality loop activates:

```bash
armature improve contract-risk-assessment.yaml
```

The optimizer reads the accumulated traces. If `analyst` has been returning outputs that fail `evaluate:` criteria, it enriches the description with more explicit formatting instructions. If `quality_judge` quorum scores are consistently low, it adds specific evaluation dimensions to the judge's description. If `analyst` is hitting its retry loop on most runs, it upgrades the model tier or relaxes the `until:` threshold. The spec file is updated in place. The next run starts from the improved spec. The cycle continues until IHR stabilizes above target.

**What you get:** A workflow that improves itself. The quality team can read `evaluate:` criteria in the spec and understand exactly what semantic properties are being tested. They can watch `armature report contract-risk-assessment.yaml` and see IHR trend upward over time. The improvement log answers, for every revision, what changed, when, why, and whether it worked.

---

## All three, together

A full production deployment does not choose between these stacks. It uses all three.

```
Governance stack     →  defines what agents can do
Reliability stack    →  ensures the workflow runs to completion at scale
Quality stack        →  ensures the output is worth running at all
```

These compose without configuration. Safety rules apply inside fan-out executions automatically — each parallel worker is individually governed. Traces capture every stage in the pipeline, including each of the 500 fan-out workers, giving IHR a full evidence base. Human gates can appear anywhere in the DAG: after a reliability stage's fan-in, before the final report, or conditionally when quality judge confidence is low. Checkpoint and continuation work regardless of whether IHR is healthy or degraded.

The integration is not accidental. It is the consequence of building a harness rather than a library. When all the primitives share a common execution model — the same context dict, the same lifecycle hooks, the same trace capture path — features compose for free. You do not wire them together. You declare what you want and the harness handles the rest.

---

## The spec as the source of truth for production AI governance

Every decision made in a production Armature deployment is expressed in YAML and checked into version control alongside the workflow.

The security team reviews `safety_rules:` and `safety_mode:` in a PR. They can tell exactly which tool calls are permitted, which require approval, and which are hard-blocked — without reading Python, without understanding the LLM, without running the system. The governance config is readable by anyone who can read a config file.

The operations team reads `checkpoint: true` and `on_fail.loop`. They know that a failed run can be restarted, that transient API failures will be retried with backoff, and that individual fan-out failures will not abort the batch. Fault tolerance is visible in the spec, not buried in exception handling.

The quality team reads `evaluate:` criteria and watches IHR in the improvement log. They can see, for every stage, what semantic properties are being tested, and they can watch the optimizer's revision history to understand what broke and what fixed it.

Non-engineers — product managers, compliance officers, legal reviewers — can read the workflow spec and understand what the system does. `gate: human` tells them where humans are in the loop. `skip_if:` tells them when stages are conditional. The DAG structure from `depends_on:` tells them the execution order. They may not understand every Jinja2 expression, but the structure of the workflow — the governance, the checkpoints, the quality gates — is plain to read.

This is not a documentation strategy. It is an architecture choice. When the spec is the source of truth, there is no gap between what the system is supposed to do and what it does. Security audits review the same file that the harness executes. Quality reports reference the same traces that the optimizer reads. The human gate that a legal reviewer approves is the same gate declared in the YAML.

Most agentic frameworks require you to build governance, fault tolerance, and quality measurement separately — in code, per workflow, by engineers. Armature requires you to declare them — in YAML, once, readable by anyone.

That is the value of a harness.

---

*Armature — the spec is the source of truth.*
