# Adapter-Powered Teams: LoRA Skills for Armature Workflows

End-to-end guide for replacing skill text with LoRA adapters and keeping them fresh as your workflow runs.

---

## The idea

An Armature **team** is a YAML workflow spec. Each stage is a role. Roles can reference **skills** from the top-level `skill_library`. A skill is normally inline text injected into the system prompt.

A **LoRA adapter skill** replaces that inline text with a fine-tuned LoRA adapter. The adapter is trained to reproduce the skill's behavior in weight space. At runtime the adapter is loaded instead of the skill text, which:

- Cuts prefill tokens (the full skill text is no longer injected).
- Makes skills composable and versioned in the local registry.
- Allows skills to improve from high-quality execution traces.

This guide shows how to design, create, register, and continually refresh adapter-backed skills for a team.

---

## 1. Choose the candidate SLM tier

Adapters are trained for a specific **base model**. That model must match the model used by the tier that will load the adapter.

The usual candidate is the `small` tier (workers) because it is high-volume and cost-sensitive:

```yaml
model_tiers:
  small:
    provider: vllm                    # or ollama
    model: qwen/qwen2.5-7b
    adapter_support: dynamic            # REQUIRED for dynamic adapter loading
    api_base: http://localhost:8000

  frontier:
    provider: openrouter
    model: qwen/qwen3.6-27b
    api_key_env: OPENROUTER_API_KEY
```

For local development use Ollama:

```yaml
model_tiers:
  small:
    provider: ollama
    model: qwen2.5:7b
    adapter_support: dynamic
    api_base: http://localhost:11434
```

**Rules:**

- The adapter `base_model` must exactly match `model_tiers.small.model`.
- Only tiers with `adapter_support: dynamic` can load an adapter per request.
- If `adapter_support` is omitted, it defaults to `none` and the skill text is used.

---

## 2. Declare the skill with an adapter reference

```yaml
skill_library:
  tdd:
    id: tdd
    description: Test-driven development workflow
    content: |
      Follow test-driven development:
      1. Write a failing test.
      2. Write the minimal implementation.
      3. Refactor.
    adapter:
      name: tdd
      version: latest
      fallback: text
      inject_metadata: false
```

| Field | Meaning |
|---|---|
| `name` | Adapter name in the local registry. Usually the same as the skill id. |
| `version` | `latest` resolves the registry's promoted pointer, or set a concrete version like `3`. |
| `fallback` | `text` keeps `content` if the adapter is missing; `none` omits the skill; `fail` aborts. |
| `inject_metadata` | `true` appends "Active via adapter ..." to the prompt when loaded. |

Attach it to a role:

```yaml
stages:
  - id: coder
    role:
      name: TDD Coder
      type: worker
      skills: [tdd]
      description: Implement {{ feature }} using the attached TDD skill.
    depends_on: []
```

The `content` block is the **fallback** and the **teacher** for S2L training. Do not delete it.

---

## 3. Configure `adapter_factory`

The `adapter_factory` block tells `armature adapter create` how to train adapters for this team:

```yaml
adapter_factory:
  backend: local                # mock | s2l | trace | local | modal | together | runpod | replicate
  base_model: qwen/qwen2.5-7b   # must match the SLM tier model
  rank: 16
  alpha: 32
  target_modules: [q_proj, v_proj]
  use_dora: false
  continual_learning:
    enabled: false
    prior_version: latest
    orthogonality_lambda: 0.01
    freeze_old_routing: true
    init_delta_near_zero: true
  schedule:
    min_new_traces: 100
    max_age_days: 30
    quality_drift_threshold: 0.05
  promotion_policy:
    min_cng: 0.10
    min_hqs_delta: 0.02
    require_manual_review: false
  skills:
    tdd:
      backend: s2l
      base_model: qwen/qwen2.5-7b
```

**Key fields:**

| Field | Purpose |
|---|---|
| `backend` | How the adapter is trained. `mock` is for tests; `local` uses PEFT/MLX/Unsloth; `s2l` trains from the skill document; `trace` trains from exported traces. |
| `base_model` | Must match the model of the tier that will load the adapter. |
| `rank` / `alpha` | LoRA hyperparameters. |
| `target_modules` | Which transformer modules to adapt. |
| `use_dora` | Use Weight-Decomposed Low-Rank Adaptation (DoRA). |
| `continual_learning` | C-LoRA-style updates from the prior adapter version instead of retraining from scratch. |
| `schedule` | When to retrain automatically (trace count, age, quality drift). |
| `promotion_policy` | Gate for promoting a new adapter version to `latest`. |
| `skills` | Per-skill overrides (backend, base model, rank, DoRA, continual learning). |

---

## 4. Create an adapter from a skill document

The fastest way to get a skill-backed adapter is the **Skill-to-LoRA (S2L)** backend. It synthesizes supervised fine-tuning examples from the skill text and trains a small adapter offline.

```bash
armature adapter create \
  --spec my_team.yml \
  --skill tdd \
  --backend s2l
```

What happens:

1. `load_spec("my_team.yml")` resolves the skill and `adapter_factory` config.
2. `S2LSkillAdapterFactory` generates training examples from the skill `content`.
3. The configured trainer (PEFT, MLX, Unsloth, or mock) fine-tunes the adapter.
4. The artifact is registered under `~/.armature/adapters/tdd/` as version `1` and promoted to `latest`.

For tests without ML dependencies:

```bash
armature adapter create \
  --spec my_team.yml \
  --skill tdd \
  --backend mock
```

---

## 5. Run the team with the adapter

Validate and run as usual:

```bash
armature validate my_team.yml
armature run my_team.yml --input feature="add login"
```

When the `coder` stage runs on the `small` tier:

1. The engine resolves `tdd` from `skill_library`.
2. It looks up adapter `tdd@latest` in the registry.
3. Because the tier has `adapter_support: dynamic`, it passes the adapter path to vLLM/Ollama and omits the skill text from the prompt.

If the adapter is missing, `fallback: text` keeps the original skill text in the prompt.

---

## 6. Improve the adapter from execution traces

The long-term value comes from exporting high-quality traces and retraining the adapter on the judge/accepted outputs.

### 6.1 Export high-quality traces

```bash
armature export-traces \
  --workflow my_team \
  --output training.jsonl \
  --format chat \
  --min-score 0.85 \
  --role-types worker,judge
```

This produces SFT training examples from stages whose outputs scored well.

### 6.2 Train a new adapter version from traces

For a one-shot training job:

```bash
armature adapter create \
  --spec my_team.yml \
  --traces training.jsonl \
  --backend trace \
  --name tdd
```

The registry creates version `2` for `tdd`. Version `1` remains available.

For a continual update that starts from the prior `latest` version, evaluates the new version, and only promotes it if it passes a policy:

```bash
armature adapter update tdd training.jsonl \
  --eval-spec my_team.yml \
  --eval-input feature="add login" \
  --eval-stage judge \
  --min-score 0.05
```

This derives the adapter hyperparameters from the current `latest` version, trains a new version, runs `armature adapter eval`, and promotes the new version only when the eval delta is at least `0.05`.

### 6.3 Evaluate before promoting

```bash
armature adapter eval tdd my_team.yml \
  --input feature="add login" \
  --stage-id judge
```

This runs the workflow twice — once with the candidate adapter and once without — and reports the HQS/quorum difference on the target stage.

### 6.4 Promote the winner

Manual promotion:

```bash
armature adapter promote tdd 2
```

Policy-gated promotion (only promote if `validation_score >= 0.75`):

```bash
armature adapter promote tdd 2 --min-score 0.75
```

Bypass the policy with `--force`:

```bash
armature adapter promote tdd 2 --force
```

Now `tdd@latest` points to version `2`. The next `armature run` picks it up automatically.

### 6.5 Merge multiple skill adapters into one

When several skills each have their own adapter, you can merge them into a single artifact that preserves all of their low-rank updates:

```bash
armature adapter merge skill-a@latest skill-b@latest \
  --name combined \
  --base-model qwen/qwen2.5-7b
```

`MergedAdapterFactory` loads the source `adapter.safetensors` files and adds the corresponding `lora_A`/`lora_B` tensors (and DoRA magnitude vectors if present). The sources must share the same base model, rank, alpha, target modules, and DoRA setting.

---

## 7. Continual / periodic retraining loop

For a team that runs continuously, set up a loop:

```yaml
adapter_factory:
  backend: trace
  base_model: qwen/qwen2.5-7b
  continual_learning:
    enabled: true
    prior_version: latest
    orthogonality_lambda: 0.01
    freeze_old_routing: true
    init_delta_near_zero: true
  schedule:
    min_new_traces: 100
    max_age_days: 7
    quality_drift_threshold: 0.05
```

A periodic job (cron, GitHub Action, `armature watch`, or your own scheduler) runs:

```bash
# 1. Export new traces since last retrain
armature export-traces \
  --workflow my_team \
  --output training.jsonl \
  --min-score 0.85

# 2. Continually update, evaluate, and conditionally promote in one command
armature adapter update tdd training.jsonl \
  --eval-spec my_team.yml \
  --eval-input feature="add login" \
  --eval-stage judge \
  --min-score 0.05
```

With `continual_learning.enabled: true`, the trainer resolves the prior `latest` version, validates that it is compatible, and uses it as a warm start. A production implementation loads the prior LoRA weights, freezes the old routing matrix `R_old`, and trains a near-zero `R_delta` for the new trace batch while regularizing with `λ ||A^T · R_delta||_F²`. This reduces catastrophic forgetting when the team accumulates new examples over time.

---

## 8. Per-role or per-stage adapter assignment

A team can have multiple skills, each backed by its own adapter:

```yaml
skill_library:
  tdd:
    id: tdd
    description: Test-driven development
    content: |
      ...
    adapter:
      name: tdd
      version: latest
  security_review:
    id: security_review
    description: Security hardening checklist
    content: |
      ...
    adapter:
      name: security
      version: latest

stages:
  - id: coder
    role:
      type: worker
      skills: [tdd]
      ...

  - id: security_guard
    role:
      type: judge
      skills: [security_review]
      ...
```

Each adapter is trained and promoted independently. A stage only loads the adapters for the skills it declares.

---

## 9. Multi-team adapter sharing

By default the registry is `~/.armature/adapters`. Teams can share a registry by passing `--registry`:

```bash
armature run team_a.yml --registry /shared/adapters --input topic="..."
armature adapter create --spec team_b.yml --skill security --registry /shared/adapters
```

Or set the registry per spec by configuring `adapter_factory` to point at a shared path in a production deployment.

---

## 10. Production checklist

Before putting an adapter-powered team into production:

- [ ] The SLM tier model and `adapter_factory.base_model` are identical.
- [ ] The tier has `adapter_support: dynamic`.
- [ ] Every adapter-backed skill has `content` or `path` for fallback.
- [ ] `fallback` is set to the desired policy (`text`, `none`, or `fail`).
- [ ] The adapter exists in the registry and `armature validate` passes.
- [ ] A dry-run with the adapter active succeeds:
      `armature run my_team.yml --input feature="test" --dry-run`
- [ ] A baseline run without the adapter is recorded for comparison.
- [ ] `adapter_factory.promotion_policy` is configured (or manual promotion is documented).
- [ ] Retraining cadence is defined: nightly, weekly, or trigger-based.
- [ ] `continual_learning` is enabled for long-running teams that accumulate traces.
- [ ] `use_dora` is enabled if the target model/provider supports DoRA and quality gains justify cost.

---

## Example: complete TDD team spec

See `examples/07_lora_adapter.yml` for a runnable mock-backed example. The production version only changes `backend`, `base_model`, and the provider:

```yaml
name: tdd_team
version: "1.0"
description: Worker uses a LoRA adapter trained from the TDD skill and trace data.

model_tiers:
  small:
    provider: vllm
    model: qwen/qwen2.5-7b
    adapter_support: dynamic
    api_base: http://localhost:8000

  frontier:
    provider: openrouter
    model: qwen/qwen3.6-27b
    api_key_env: OPENROUTER_API_KEY

role_type_defaults:
  worker: small
  orchestrator: frontier
  judge: frontier

adapter_factory:
  backend: trace
  base_model: qwen/qwen2.5-7b
  rank: 16
  alpha: 32
  target_modules: [q_proj, v_proj]
  use_dora: false
  continual_learning:
    enabled: true
    prior_version: latest
    orthogonality_lambda: 0.01
    freeze_old_routing: true
    init_delta_near_zero: true
  schedule:
    min_new_traces: 100
    max_age_days: 7
    quality_drift_threshold: 0.05
  promotion_policy:
    min_cng: 0.10
    min_hqs_delta: 0.02
    require_manual_review: false

skill_library:
  tdd:
    id: tdd
    description: Test-driven development workflow
    content: |
      Follow test-driven development:
      1. Write a failing test.
      2. Write the minimal implementation.
      3. Refactor.
    adapter:
      name: tdd
      version: latest
      fallback: text
      inject_metadata: false

stages:
  - id: planner
    role:
      name: Planner
      type: orchestrator
      description: Plan implementation steps for {{ feature }}.
    output_mode: guided_json
    output_schema:
      type: object
      required: [steps]
      properties:
        steps: {type: array, items: {type: string}}
    depends_on: []

  - id: coder
    role:
      name: TDD Coder
      type: worker
      skills: [tdd]
      description: Implement {{ feature }} using the TDD skill and plan {{ planner.steps }}.
    output_mode: text
    depends_on: [planner]

  - id: judge
    role:
      name: Quality Judge
      type: judge
      description: Score whether the coder's plan follows TDD.
    output_mode: guided_json
    output_schema:
      type: object
      required: [follows_tdd, confidence]
      properties:
        follows_tdd: {type: boolean}
        confidence: {type: number, minimum: 0.0, maximum: 1.0}
    depends_on: [coder]
```

Operational commands for this team:

```bash
# Bootstrap the first adapter from the skill document
armature adapter create --spec tdd_team.yml --skill tdd --backend s2l

# Run the team
armature run tdd_team.yml --input feature="add login"

# Weekly retrain, evaluate, and conditionally promote from high-quality traces
armature export-traces --workflow tdd_team --output tdd_training.jsonl --min-score 0.85
armature adapter update tdd tdd_training.jsonl \
  --eval-spec tdd_team.yml \
  --eval-input feature="add login" \
  --eval-stage judge \
  --min-score 0.05

# Or do it manually
armature adapter create --spec tdd_team.yml --traces tdd_training.jsonl --name tdd
armature adapter eval tdd tdd_team.yml --input feature="add login" --stage-id judge
armature adapter promote tdd <new_version> --min-score 0.05

# Merge skill adapters when several skills back one stage
armature adapter merge tdd@latest security@latest --name combined
```

---

## Further reading

- `examples/07_lora_adapter.yml` — runnable mock-backed example
- `docs/ARMATURE-SPEC-REF.md` — all adapter fields and valid values
- `docs/USER-GUIDE.md` §4.2 — LoRA adapter-backed skills
- `docs/MODEL-TIERS.md` — cost-aware tier design
- `docs/HQS-AND-SELF-IMPROVEMENT.md` — trace quality and the improvement loop
