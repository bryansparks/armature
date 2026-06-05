# Human-in-the-Loop Gates in Armature

A first-class stage type that pauses workflow execution until a human approves or provides feedback.

---

Human-in-the-loop is not a failure mode. It is not an escape hatch for when automation falls short. It is a deliberate design choice — made at spec-write time, not at runtime — for decisions that carry enough consequence that a human must be accountable for them. The gate encodes that accountability as a first-class stage in the workflow DAG, sitting between dependent stages the same way any other stage does.

The pattern is old. Approval workflows have existed in enterprise software for decades — email-based, ticket-based, DocuSign-based. What Armature contributes is the *expression* of the pattern: declaring a checkpoint in a YAML spec, having the harness pause execution at that point, collecting a structured approval or feedback response, and carrying that response into the downstream context where stages can act on it — all without writing orchestration code.

---

## The gate stage type

A stage becomes a human gate by setting `gate: human`. No `role:` field is needed. The gate is not an LLM call; it is a synchronous pause that blocks the workflow until a human responds.

```yaml
stages:
  - id: review_gate
    gate: human
    present: |
      Review required before proceeding.
    depends_on: [analyst]
```

The engine dispatches to `HumanGateNode`, which renders the `present:` message, prints it to stdout, and waits for input:

```
============================================================
HUMAN APPROVAL REQUIRED
============================================================
Review required before proceeding.
============================================================
Approve? [yes/no/feedback]:
```

If the operator types `yes`, `y`, or `approve`, the gate returns:

```json
{"approved": true, "feedback": null}
```

Any other response triggers a second prompt for a feedback string. The gate then returns:

```json
{"approved": false, "feedback": "the operator's feedback text"}
```

This dict becomes the gate stage's result in the context, referenced by downstream stages the same way any other stage result is.

---

## The `present:` field

`present:` is a Jinja2 template string. It can reference any variable in the workflow context at the time the gate executes — including results from upstream stages listed in `depends_on`.

```yaml
stages:
  - id: analyst
    role:
      type: analyst
      description: |
        Assess the risk level of this transaction.
        Return {"risk_level": "low|medium|high", "summary": "..."}.

  - id: review_gate
    gate: human
    present: |
      Analyst produced the following risk assessment:
      Risk level: {{ analyst.risk_level }}
      Summary:    {{ analyst.summary }}

      Please review and approve or provide feedback.
    depends_on: [analyst]
```

Jinja2 filters work in `present:` exactly as they do anywhere else in a spec:

```yaml
    present: |
      {{ review_each | length }} documents were reviewed.
      High-risk items: {{ review_each | selectattr('risk_level', 'eq', 'high') | list | length }}

      Flagged documents:
      {% for item in review_each | selectattr('risk_level', 'eq', 'high') | list %}
        - {{ item.doc_path }}: {{ item.summary }}
      {% endfor %}

      Approve to proceed to final report, or provide feedback to flag concerns.
```

The rendered message is what the human sees. A well-written `present:` gives the operator everything they need to make the decision — not a generic prompt that forces them to go look something up.

---

## Gate position in a pipeline

The gate is a node in the DAG like any other stage. It has `depends_on` (what must complete before the human is shown anything) and is itself listed in the `depends_on` of stages that must wait for the human's response.

```
  upstream stages        gate                downstream stages
  ───────────────        ────                ────────────────

  ┌─────────────┐        ┌──────────────┐    ┌──────────────┐
  │   analyst   │──────► │ review_gate  │──► │ final_report │
  └─────────────┘        │   (human)    │    └──────────────┘
                         │              │
  ┌─────────────┐        │ pauses here  │
  │ review_each │──────► │ until input  │
  └─────────────┘        └──────────────┘
```

The harness holds all downstream stages at the `depends_on` barrier until the gate returns. No polling, no callbacks — the gate's `await node.execute(context)` blocks until the human types a response.

---

## Usage pattern 1: Hard gate

A hard gate stops the workflow completely if not approved. Downstream stages skip via `skip_if`.

```yaml
stages:
  - id: analyst
    role:
      type: analyst
      description: |
        Analyse the proposed contract changes.
        Return {"summary": "...", "risk_level": "low|medium|high"}.

  - id: approval_gate
    gate: human
    present: |
      Contract analysis complete.
      Risk level: {{ analyst.risk_level }}
      Summary: {{ analyst.summary }}

      Approve to send the contract for signature. Decline to cancel.
    depends_on: [analyst]

  - id: send_for_signature
    skip_if: "{{ approval_gate.approved == false }}"
    role:
      type: worker
      description: |
        Prepare the contract package and send for signature.
    depends_on: [approval_gate, analyst]

  - id: notify_team
    skip_if: "{{ approval_gate.approved == false }}"
    role:
      type: worker
      description: |
        Notify the team that the contract has been sent for signature.
    depends_on: [approval_gate]
```

If the operator declines, every downstream stage with `skip_if: "{{ approval_gate.approved == false }}"` is skipped. Nothing is sent. The workflow completes cleanly with those stages marked as skipped in the trace.

---

## Usage pattern 2: Soft gate with feedback loop

A soft gate does not stop the workflow on decline — it routes the operator's feedback to a downstream stage that uses it to revise its output.

```yaml
stages:
  - id: draft_writer
    role:
      type: worker
      description: |
        Write a first draft of the executive summary.
        {% if review_gate is defined and review_gate.approved == false %}
        The previous draft was not approved. Reviewer feedback:
        {{ review_gate.feedback }}
        Revise accordingly.
        {% endif %}
        Return {"draft": "..."}.
    depends_on: []

  - id: review_gate
    gate: human
    present: |
      Draft executive summary for your review:

      {{ draft_writer.draft }}

      Approve to finalize, or provide feedback to request revisions.
    depends_on: [draft_writer]

  - id: finalize
    skip_if: "{{ review_gate.approved == false }}"
    role:
      type: worker
      description: |
        The draft has been approved. Finalize the document for distribution.
        Draft: {{ draft_writer.draft }}
    depends_on: [review_gate, draft_writer]
```

In this pattern `review_gate.feedback` is a string the revision stage can incorporate directly into its prompt. The feedback flows as data — no special handling, no side channels. A more complete feedback loop would add a `condition:` or `skip_if:` on the draft stage to loop it when the gate declines, with continuation logic to re-run the workflow from the draft stage with the feedback in context.

---

## Usage pattern 3: Conditional escalation gate

An escalation gate only activates when certain conditions are met — for example, when a fan-out review found high-risk items. If the condition is not met, the gate is skipped and the workflow runs fully automated.

```yaml
stages:
  - id: list_documents
    role:
      type: researcher
      description: |
        List all documents pending review.
        Return {"documents": ["/path/to/doc.pdf", ...]}.

  - id: review_each
    fan_out: 10
    fan_in: list
    partition_source: "{{ list_documents.documents }}"
    partition_key: doc_path
    role:
      type: worker
      description: |
        Review this document for compliance issues.
        Document: {{ doc_path }}
        Return {"risk_level": "low|medium|high", "issues": [...], "summary": "..."}.
    depends_on: [list_documents]

  - id: escalation_gate
    gate: human
    skip_if: >-
      {{ review_each
         | selectattr('risk_level', 'eq', 'high')
         | list | length == 0 }}
    present: |
      {{ review_each | selectattr('risk_level', 'eq', 'high') | list | length }}
      high-risk document(s) found out of {{ review_each | length }} reviewed.

      High-risk items:
      {% for item in review_each | selectattr('risk_level', 'eq', 'high') | list %}
        - {{ item.doc_path }}: {{ item.summary }}
      {% endfor %}

      Approve to include these in the final report, or provide feedback to
      flag them for additional review before the report is generated.
    depends_on: [review_each]

  - id: final_report
    role:
      type: orchestrator
      description: |
        Produce a compliance summary from {{ review_each | length }} document reviews.
        All reviews: {{ review_each }}
        {% if escalation_gate is defined and escalation_gate.approved == false %}
        Note: High-risk items were escalated. Reviewer feedback: {{ escalation_gate.feedback }}
        {% endif %}
    depends_on: [review_each, escalation_gate]
```

When no high-risk items exist, `escalation_gate` is skipped entirely — `skip_if` evaluates truthy and the harness bypasses the stage. The workflow completes without any human interaction. When high-risk items are found, the gate activates. The human reviews the flagged items and either approves (the final report runs as normal) or declines with feedback (the final report receives that feedback as context and can note the escalation).

This is the core value of the conditional gate: automation handles the routine case completely; the gate activates only when human judgment is warranted. The threshold for what constitutes "warranted" is declared in the YAML spec by the workflow author — not discovered at runtime by the system.

---

## Referencing gate results downstream

The gate's return value is a dict stored under the gate's stage ID in the workflow context, identical to how any other stage result is stored.

| Expression | Value |
|------------|-------|
| `{{ approval_gate.approved }}` | `true` or `false` |
| `{{ approval_gate.feedback }}` | feedback string, or `null` if approved |

Both fields are always present regardless of the response. Downstream Jinja2 expressions can use them in `skip_if`, `condition`, `present:`, and role `description:` fields without null-checking — `feedback` is `null` on approval, which is falsy in Jinja2.

---

## Gates vs. safety rules

Armature has two distinct mechanisms for involving humans in a workflow. They address different problems and operate at different levels.

**Safety rules** (`safety_rules:` at the spec level) are **reactive**. They fire when an agent attempts a tool call that matches a defined pattern — for example, an `rm -rf` command or a network request to a production endpoint. The rule intercepts the action *after* the agent has decided to take it and *before* the harness allows it to execute. A `require_approval` action on a safety rule presents a confirmation prompt inline, at the moment of the dangerous action.

**Human gates** (`gate: human` stages) are **proactive**. They are scheduled checkpoints the workflow author decided — at spec-write time — must involve human judgment. They are not triggered by the content of an LLM response; they always fire when execution reaches that stage (unless `skip_if` evaluates truthy). They are not about catching dangerous actions; they are about requiring human accountability for a decision before the workflow proceeds.

The distinction matters for workflow design:

- Use a **safety rule** when you want to protect against an agent doing something it should not do — a guardrail around tool invocation.
- Use a **human gate** when you want to require human sign-off on a decision the workflow was always going to make — a deliberate checkpoint in a planned process.

Both can appear in the same spec. A compliance workflow might have a human gate before sending a report externally and a safety rule preventing the report-sender tool from writing to certain paths without approval. They compose without conflict.

---

## Service layer considerations

The current `HumanGateNode` implementation uses stdin/stdout: it prints to the terminal and reads a line of input. This is correct for CLI-driven workflows where a human is at the keyboard watching the run.

Production deployments that run workflows as background jobs — triggered by a scheduler, a webhook, or a queue — cannot block on stdin. In those environments the gate needs a service layer: a mechanism to pause the workflow, persist its state, notify a human through some channel (email, Slack, a web UI), and resume the workflow when the human responds through that channel.

Armature's architecture supports this extension. `HumanGateNode.execute` is an async method; swapping the stdin/stdout implementation for one that writes an approval request to a database and suspends until a callback arrives is a node-level change. The stage model, the DAG execution engine, and the downstream context handling are unchanged. The workflow spec author writes `gate: human` either way — the service layer is an infrastructure concern below the spec.

For teams running Armature in production, the immediate options are:

1. **Run workflows interactively** — the simplest path; the human running `armature run` handles gates in real time.
2. **Wrap the process** — a thin service starts the armature process in a PTY, monitors for the gate prompt pattern, sends the prompt to an approval system, and pipes the response back.
3. **Subclass `HumanGateNode`** — implement an async approval service and register it in the engine's dispatch table. No changes to the spec model or DAG logic.

The stdin/stdout implementation is not a limitation of the concept. It is the correct implementation for the current use case and an honest baseline that makes the service contract legible: receive a message, return an approval struct.

---

*The human gate is a first-class stage because human judgment is a first-class input to the workflows that need it. The YAML spec declares where judgment is required; the harness ensures execution stops there and carries the human's response forward as data.*
