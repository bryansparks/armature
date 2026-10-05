# Cloud transport and provider seams

The missions transport — the code that executes missions against a remote
provider (state, launch, settle, sweep) — lives in `armature/transport/`
behind the optional `cloud` extra. This doc records the boundary that decides
where new code goes, the extras contract, the module map, and the seams a
second provider (Fly/Modal/K8s/…) would implement.

## The boundary rule

> **Policy is grammar; mechanism is transport.**
> Any control that would appear in the governance memo's mechanism table —
> budgets, gates, state transitions, settle semantics, name validation, audit
> records — belongs in armature (the public engine), so it follows the work to any
> provider. Anything that exists only because a specific provider offers it — IAM
> boundaries, KMS gates, SNS topics, CloudTrail, billing alarms, task-definition
> conventions — belongs in the deployment repo (armature-dispatch).

The rule stops repo placement from being decided per-feature. A new gate or
budget control lands here, in the public suite; a new IAM boundary or topic
convention lands in the deployment.

## The extras contract

```
pip install armature-agents        # gains nothing cloud-side: no boto3, and
                                   # `import armature` never touches armature/transport
pip install armature-agents[cloud]  # adds boto3 and unlocks armature/transport
```

- The default install stays AWS-free and import-clean; `tests/transport/test_boundary.py`
  enforces it in CI (an AST scan for boto3/botocore/moto outside `transport/`,
  plus a fresh-interpreter proof that `import armature` reaches neither).
- `pip install armature-agents[cloud-dev]` adds `moto` for the test suite.
- Without boto3, `armature.transport.sweep.main()` fails with an actionable
  install instruction (`pip install armature-agents[cloud]`), not a bare
  `ModuleNotFoundError`. Modules are imported directly
  (`from armature.transport.s3work import S3WorkStore`) — no package-level
  re-exports, so a missing extra surfaces at the exact import that needs it.

## Module map

| Module | What it owns |
|---|---|
| `naming` | Deterministic sweep job ids (`sweep-<mission>~<unit>~<attempt>` — the `~` separators sit outside the id grammar so the triple is unambiguous; never-2PC) and hostile-id validation (`validate_work_names`) — mission and unit ids reach S3 keys, job-id prefixes, and task env, so every entry point validates. |
| `s3io` | S3 helpers and the readers of the engine's own `jobs/<id>/results/<run>/…` results layout (`list_run_ids`, `latest_receipt`) — the layout mirrors `ResultsWriter` output verbatim, so the readers live in the engine. |
| `s3work` | `S3WorkStore` — the S3 implementation of the existing `WorkStore` protocol (`armature/state/work.py`). Owns the `work/<mission>/` key layout: `<unit_id>.json` records (byte-identical to `LocalWorkStore`'s), `mission.yml`, and mission-scoped `transitions/`. |
| `settle` | Runner step 4.7 (inject the work-unit record as `context['work_unit']` for `mission_source: work_unit` specs) and step 7.6 (settle: meter cost, apply closure, move to done/failed/retry_pending) — the same semantics the local executor uses. |
| `workops` | The submit gates (validate doc → validate names → ensure record → state gate → spec∪record requires → attempts → unit + mission budgets; the mission doc rides along only after the gates pass) and `start_work_unit` launch bookkeeping (same-job idempotent). |
| `sweep` | The flock sweep: per-mission pass, repair pass (orphan rule), readiness → submit/notify/hold, exit-1-on-errors alarm contract. The launch target is *injected* — see the seam below. |

## The provider seam

The extension point a second provider would implement. Documented here;
deliberately not formalized in code yet.

- **State store** — the `WorkStore` protocol (`armature/state/work.py`).
  Local and S3 implementations exist today.
- **Launch target** — the compute primitive: given a packaged workflow +
  inputs + env overrides, launch a run and identify it. Today: the deployment
  repo's ECS RunTask path, injected into the sweep as the `launch` callable
  (`ARMATURE_SWEEP_LAUNCH`, `module:function`). The engine holds the launch
  *policy* (deterministic job ids, attempt-idempotent bookkeeping, hold
  semantics); the callable is mechanism.
- **Secret store** — resolve declared secret names at run time. Today: SSM,
  in the deployment repo.
- **Alert sink** — notify a human. Today: SNS, passed to the sweep as
  `topic_arn` (notify-only — an executor never acts in human-led scope).
- **Job/id registry** — the results layout (`jobs/<id>/results/...`), which
  mirrors armature's `ResultsWriter` output verbatim; `armature.transport.s3io`
  owns the readers.

A second provider implements the five seams and reuses every engine control
untouched. The trigger to formalize a `LaunchTarget` protocol in code is a
second provider or a second consumer of launch semantics appearing.

## Env contracts

The sweep's entrypoint (`python -m armature.transport.sweep`) reads:

| Var | Set by | Meaning |
|---|---|---|
| `DISPATCH_BUCKET` | the task definition | the work store + jobs bucket |
| `DISPATCH_INPUTS_JSON` | the cron rule's inputs | sweep inputs — e.g. `{"dry_run": true}` |
| `FLOCK_ALERTS_TOPIC_ARN` | the task definition | the alert sink for ready human-led units |
| `ARMATURE_SWEEP_LAUNCH` | the task definition | `module:function` naming the launch callable (e.g. `dispatch.ops:run_workflow`) |
| `DISPATCH_CLUSTER` | the task definition | the ECS cluster the repair pass queries for task state (default: `dispatch`) |

Exit 1 on any sweep error so the adapter's nonzero exit fails the cron run and
the ECS-failure alarm path fires; the summary JSON rides stdout (parsed via the
adapter's `parse: json`) and stderr.