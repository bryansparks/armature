# Armature — TODO

## Feature Backlog (DeerFlow-inspired, implemented the Armature way)

See full design: `docs/plan-deerflow-features.md`

### Phase 1 — Quick wins
- [ ] **f) Sub-agent context isolation** — `isolated: true` flag on subagent stages; strips parent context to `signature.input` only (~50 lines, spec + engine)
- [ ] **b) Skills system** — wire up `role.skills: []` field; add `skill_library:` to spec; PromptAssembler injects skill content per-stage (~100 lines)
- [ ] **c) `armature doctor`** — new CLI health check command; verifies packages, env vars, DB paths, spec validity; exits 1 on failure for CI use (~200 lines)

### Phase 2 — Observability
- [ ] **d) LangFuse adapter** — auto-activates from env vars; registers PRE/POST_STAGE hooks; creates traces + spans with tokens, latency, quorum_score (~150 lines)
- [ ] **d) LangSmith adapter** — parallel implementation to LangFuse (~100 lines)

### Phase 3 — Ecosystem integration
- [ ] **a) MCP server support** — `mcp_servers:` spec section; auto-registers discovered tools as `{server_name}.{tool_name}` in ToolRegistry; supports stdio + http + sse transports (~400 lines)

### Phase 4 — Infrastructure
- [ ] **e) Sandbox isolation** — `sandbox:` spec section with `mode: docker`; wraps `shell`/`file_write`/`file_read` tools transparently; ephemeral containers per call, shared workspace bind mount (~300 lines)
- [ ] **g) Messaging channel connectors** — `armature channels start channel.yaml`; Telegram + Slack; pattern-based routing to workflow specs; embedded or HTTP mode (~600 lines)

---

## Open Source Release Checklist

## Must-fix before making the repo public

- [ ] **Add LICENSE file** — MIT is recommended for a framework. Without it, nobody can legally use the code.
- [ ] **Update `.gitignore`** — several internal/scratch files are untracked but not ignored:
  - `.playwright-mcp/` (playwright session logs)
  - `HARNESS-STATE-v3.md` (internal architecture notes)
  - `taking_stock.txt` (scratch notes)
  - `docs/superpowers/` (Claude Code plugin internals — must not be public)
  - `armature-*.png` (decide: add to `docs/images/` or gitignore)
- [ ] **Clean up `VISION.md`** — remove hardcoded `/Users/bryansparks/projects/armature` path and "Author: Bryan Sparks" personal reference. Consider converting to a public-facing `ARCHITECTURE.md`.
- [ ] **Audit `docs/` for internal content** — the planning docs below are construction notes, not user-facing. Exclude from the public repo (gitignore or delete):
  - `docs/plan-phase1.md` through `docs/plan-phase5.md` (8,000+ lines of dev planning)
  - `docs/deferred-research.md`
  - `docs/ARMATURE-AGENTCORE.md` (internal competitive analysis with personal references)
  - `docs/plan-gaps-agentcore.md`

## Nice-to-have for a credible open source release

- [ ] **`CONTRIBUTING.md`** — how to run tests (`pytest`), PR conventions, code style expectations
- [ ] **`.github/workflows/ci.yml`** — GitHub Actions: run `pytest` on push/PR (951 tests is instant credibility)
- [ ] **`CHANGELOG.md`** — even a minimal v0.1.0 entry covering the major feature areas
- [ ] **Organize images** — move `armature-*.png` into `docs/images/` and reference from README if keeping them

## Already in good shape

- `README.md` — solid intro with installation, concept, and quick start
- `pyproject.toml` — clean dependency definition with optional groups (`embeddings`, `service`, `wizard`, `telemetry`)
- `.env.example` — no real secrets, all placeholder values
- `examples/` — 3 working specs (`01_hello_world`, `02_research_pipeline`, `03_deliberation_standard`)
- `armature/templates/` — `six_thinking_hats.yml` is a strong out-of-box offering
- `USER-GUIDE.md` / `USER-GUIDE.html` — comprehensive, publication-quality
- `BUILD_FIRST_WORKFLOW.md` / `.html` — hands-on tutorial
- Test suite (951 tests) — publication-quality coverage
- `docker-compose.yml` + service wrapper — production-ready HTTP deployment path
