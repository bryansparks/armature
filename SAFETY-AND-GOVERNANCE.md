# Safety and Governance in Armature

Governance-as-code for AI tool calls — declarative safety rules version-controlled alongside your workflow spec.

---

Every production system has a security boundary. Firewalls define which packets are allowed in and out. IAM policies define which identities can call which APIs. These controls are not written in application code; they are declared in configuration, reviewed separately, and enforced by infrastructure.

Agentic workflows need the same thing. An agent can call any tool its harness exposes — a bash shell, an HTTP endpoint, a database write. The question is not whether to constrain that; the question is where the constraint lives. Burying it in Python callbacks makes it invisible to reviewers and hard to audit. Armature's answer is to make it a first-class part of the spec.

`safety_rules:` is a list of `ToolSafetyRule` declarations on `HarnessSpec`. Rules are evaluated before every tool call, at the `PRE_TOOL` lifecycle hook, before any execution reaches the tool implementation. The workflow author writes rules in YAML, checks them into version control alongside the workflow, and every run is governed by exactly the rules in that file.

---

## Rule structure

Each rule is a `ToolSafetyRule` with four fields:

```python
class ToolSafetyRule(BaseModel):
    tool: str          # tool name, or "*" to match any tool
    condition: SafetyCondition
    action: Literal["block", "warn", "log", "require_approval", "allow"]
    message: str = ""
```

The `condition` describes which calls the rule applies to:

```python
class SafetyCondition(BaseModel):
    field: str   # argument field to inspect — e.g. "cmd", "path", "url"
    op: Literal["contains", "not_contains", "equals", "not_equals", "matches_regex", "truthy"]
    value: str = ""
```

The harness inspects the tool's argument dict. If the named `field` matches the `op`/`value` pair, the rule fires and the `action` is taken. If the field is absent, the condition does not match.

One special field is always available regardless of the tool's declared arguments: `_tool_reversibility`. When a tool is registered, it carries a reversibility attribute (`"reversible"` or `"irreversible"`). The harness injects this into the argument set before evaluation, so you can write rules that fire on any irreversible operation without naming individual tools.

---

## Hook execution flow

Every tool call passes through this sequence before the tool runs:

```
tool call requested
        │
        ▼
  PRE_TOOL hook
        │
        ▼
  evaluate rules (in order)
        │
        ├─ rule matches, action = "allow"       ──► ALLOW (stop evaluating)
        │
        ├─ rule matches, action = "block"        ──► BLOCK
        │                                              │
        │                                         raise ToolBlocked
        │                                         increment rogue counter
        │
        ├─ rule matches, action = "require_approval" ─► prompt user
        │                                              │
        │                                    ┌─── y ──┤
        │                                    │         └── n ──► BLOCK
        │                                    ▼                    │
        │                                  ALLOW             raise ToolBlocked
        │                                                    increment rogue counter
        │
        ├─ rule matches, action = "warn"     ──► warnings.warn(), continue
        │
        ├─ rule matches, action = "log"      ──► armature.safety logger, continue
        │
        └─ no rule matches
               │
         permissive mode ──► ALLOW
               │
         strict mode     ──► BLOCK, increment rogue counter
```

Rules are evaluated in the order they appear in the spec. Evaluation stops at the first `block`, `allow`, or user-denied `require_approval`. `warn` and `log` rules do not stop evaluation — they record and continue to the next rule.

---

## The four actions

### `block`

The tool call does not happen. The harness raises `ToolBlocked`, which is caught at the stage level. `ToolBlocked` is never retried — it is a policy decision, not a transient error. The rogue signal counter is incremented.

Use `block` for things that must never happen under any circumstances in this workflow: destructive commands, writes outside the designated workspace, calls to external services that are out of scope.

### `warn`

The harness calls `warnings.warn(...)` and the tool call proceeds. Nothing is stopped.

Use `warn` during development when you want visibility into calls that look suspicious but are not yet certain to be wrong. Warnings appear in logs without breaking the run. Promote to `block` once you have confirmed the pattern should be prohibited.

### `log`

The harness writes a structured log line to the `armature.safety` logger at INFO level. The tool call proceeds.

Use `log` for calls you want to audit after the fact — legal/compliance requirements, post-run analysis, building a baseline of what the workflow actually does before writing stricter rules.

### `require_approval`

The harness prints the tool name and its arguments to stdout and prompts the operator: `Allow? [y/N]`. If the operator types `y`, the call proceeds. Any other response raises `ToolBlocked` and increments the rogue signal counter.

Use `require_approval` for operations that are legitimate but consequential enough that a human should confirm each instance: modifying system files, sending external notifications, deleting records.

---

## The `allow` action and rule ordering

`allow` is an affirmative pass-through — it stops rule evaluation and permits the call immediately, regardless of what later rules would say. Its primary use is in strict mode (see below), where it carves out explicit exceptions to the blanket block.

In permissive mode, `allow` is useful when a broad rule (wildcard or regex) would otherwise catch calls you want to explicitly permit:

```yaml
safety_rules:
  - tool: bash
    condition:
      field: cmd
      op: contains
      value: "/workspace"
    action: allow
    message: "Workspace operations are always permitted."

  - tool: bash
    condition:
      field: _tool_reversibility
      op: equals
      value: irreversible
    action: require_approval
    message: "Irreversible bash operations require approval."
```

The first rule fires first for any bash call touching `/workspace`, allows it, and stops evaluation — the second rule never sees it. Rule order is meaningful.

---

## Strict mode

`safety_mode: permissive` is the default. Unmatched tool calls are allowed.

`safety_mode: strict` inverts this. Any tool call that reaches the end of the rule list without matching an explicit `allow` rule is blocked. The harness returns `HookDecision.BLOCK` and increments the rogue signal counter.

Strict mode is the IAM default-deny posture applied to agentic workflows. The workflow can only do what the rules explicitly permit. Everything else is blocked by default.

```yaml
safety_mode: strict

safety_rules:
  # Carve out the tools this workflow is allowed to use
  - tool: http_get
    condition:
      field: url
      op: contains
      value: "api.internal.company.com"
    action: allow

  - tool: write_file
    condition:
      field: path
      op: contains
      value: "/workspace/output/"
    action: allow

  - tool: read_file
    condition:
      field: path
      op: truthy
    action: allow

  # Anything not covered above is blocked automatically
  # — no explicit block rule needed
```

In strict mode, the effective policy is:
- Matched allow → proceed
- Matched block/warn/log/require_approval → action taken, evaluation continues
- No match → blocked, rogue counter incremented

This is a meaningful difference from permissive mode, where no match means allow. The strict default-deny means new tools added to the registry are automatically blocked until the author writes an explicit allow rule for them.

---

## The rogue signal counter

Every `ToolBlocked` event — whether from an explicit `block` rule, a user-denied `require_approval`, or a strict-mode default-deny — increments a `RogueSignalCounter` on the harness instance. At the end of the run, the total is emitted in the `run_summary` event:

```json
{
  "run_id": "abc123",
  "workflow": "document-processor",
  "elapsed_s": 14.2,
  "stages_total": 5,
  "stages_ran": 5,
  "stages_skipped": 0,
  "stages_failed": 0,
  "rogue_signals": 3
}
```

A `rogue_signals` value of 0 means the workflow ran entirely within its declared policy — every tool call was permitted. A non-zero value means the workflow attempted something the rules prohibited. Three rogue signals in one run means the agent tried to do something out-of-bounds three separate times.

This is the agentic equivalent of a firewall's blocked-packet count. The signal answers: how often did the agents attempt things they were not supposed to do? In a well-tuned workflow the number should be zero. Elevated counts indicate that either the rules need refinement (something legitimate is being caught) or the agent's behavior needs adjustment (it is genuinely attempting out-of-scope actions).

The counter accumulates across all tool calls for the entire run, including every individual execution within fan-out stages.

---

## A complete spec example

```yaml
name: data-pipeline
version: "1.0"
mission: "Process customer records from the ingest queue and write outputs to the designated workspace."

safety_mode: strict

safety_rules:
  # Permitted: read from the ingest path
  - tool: read_file
    condition:
      field: path
      op: contains
      value: "/data/ingest/"
    action: allow

  # Permitted: write to the workspace output path
  - tool: write_file
    condition:
      field: path
      op: contains
      value: "/workspace/output/"
    action: allow

  # Hard block: no deletions of any kind
  - tool: bash
    condition:
      field: cmd
      op: contains
      value: "rm "
    action: block
    message: "Deletion commands are not permitted in this workflow."

  # Hard block: no calls outside the internal API
  - tool: http_post
    condition:
      field: url
      op: not_contains
      value: "api.internal.company.com"
    action: block
    message: "External HTTP calls are not permitted."

  # Permitted: internal API calls
  - tool: http_post
    condition:
      field: url
      op: contains
      value: "api.internal.company.com"
    action: allow

  # Require approval for anything irreversible not caught above
  - tool: "*"
    condition:
      field: _tool_reversibility
      op: equals
      value: irreversible
    action: require_approval
    message: "All irreversible operations require explicit approval."

stages:
  - id: ingest
    ...
```

---

## When to use which action

| Situation | Action |
|-----------|--------|
| Must never happen — policy violation | `block` |
| Should never happen but confirm intent | `require_approval` |
| Probably fine, want visibility during development | `warn` |
| Need an audit trail for compliance | `log` |
| Explicit allowlist entry in strict mode | `allow` |

In practice, a production workflow uses `block` for hard prohibitions, `require_approval` for high-consequence operations, `log` for auditing, and `allow` rules to build the explicit permit list when running under strict mode. `warn` is mostly a development tool — a stepping stone toward `block` once you have confirmed a pattern is always wrong.

---

## Composing with other Armature features

Safety rules compose with the rest of the harness without configuration:

- **Fan-out** — rules apply independently to each concurrent fan-out execution; one blocked call does not cancel the batch, but its rogue signal is counted
- **Subagent specs** — each child workflow spec carries its own `safety_rules` and `safety_mode`; the parent's rules do not automatically apply to child runs
- **`on_fail.loop`** — a `ToolBlocked` exception is never retried; the retry loop only applies to transient failures, not policy violations
- **Trace capture** — blocked tool calls are recorded in the run trace with their reason, giving you a per-call audit log alongside the aggregate rogue signal count

---

*Safety rules are the governance layer of the agentic workflow. They are not application logic — they are policy. Keeping them in YAML, next to the spec, means they are visible, reviewable, and version-controlled. The rogue signal counter turns that policy into an observable: you always know whether agents stayed within bounds.*
