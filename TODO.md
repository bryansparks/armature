# Armature — TODO

## Feature Backlog (DeerFlow-inspired, implemented the Armature way)

See full design: `docs/plan-deerflow-features.md`

### Phase 1 — Quick wins ✅
- [x] **f) Sub-agent context isolation** — `isolated: true` flag on subagent stages; strips parent context to `signature.input` only
- [x] **b) Skills system** — `SkillDef` model + `skill_library:` to spec; PromptAssembler injects skill content per-stage
- [x] **c) `armature doctor`** — CLI health check; verifies packages, env vars, DB paths, optional spec; exits 1 on failure

### Phase 2 — Observability ✅
- [x] **d) LangFuse adapter** — auto-activates from LANGFUSE_PUBLIC/SECRET_KEY; PRE/POST_STAGE hooks → traces + spans
- [x] **d) LangSmith adapter** — auto-activates from LANGSMITH_API_KEY; PRE/POST_STAGE hooks → runs

### Phase 3 — Ecosystem integration ✅
- [x] **a) MCP server support** — `mcp_servers:` spec section; auto-registers `{server_name}.{tool_name}` in ToolRegistry; stdio + http + sse transports

### Phase 4 — Infrastructure ✅
- [x] **e) Sandbox isolation** — `sandbox: mode: docker`; wraps shell/file_write/file_read transparently; bind-mount workspace
- [x] **g) Messaging channel connectors** — `armature channels start`; ChannelSpec + MessageRouter; Telegram/Slack pattern routing

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
