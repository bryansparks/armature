# Contributing to Armature

## Getting started

```bash
git clone https://github.com/bryansparks/armature
cd armature
pip install -e ".[dev]"
```

## Running the tests

```bash
./run_tests.sh          # all 1,388 tests
./run_tests.sh -k auth  # filter by name
./run_tests.sh --cov armature --cov-report=term-missing  # with coverage
```

All tests must pass before a PR is merged. CI runs on Python 3.11 and 3.12.

## Development practices

**Test-driven.** Write the failing test first, then implement. Every new function needs a test that was watched to fail before the code existed.

**No production code before a failing test.** The one exception is exploratory spikes — throw them away and rewrite test-first.

**Minimal implementation.** Write the simplest code that passes the test. Don't add abstractions, error handling, or features the tests don't require.

**No comments that explain what.** Well-named identifiers do that. Only comment when the *why* is non-obvious: a hidden constraint, a workaround for a specific bug, behavior that would surprise a reader.

## PR conventions

- Keep PRs focused: one feature or fix per PR
- Match commit style: `feat:`, `fix:`, `test:`, `docs:`, `refactor:`
- Include tests for every change
- Update `CHANGELOG.md` under `[Unreleased]`
- The PR description is where implementation rationale lives — not in code comments

## Project structure

```
armature/           Core library
  runtime/          Harness engine and DAG executor
  spec/             Pydantic models for workflow specs
  state/            Trace store, memory store, session log
  nodes/            Stage executors (LLM, script, subagent)
  registry/         Tool registry and built-in tools
  hooks/            Lifecycle hooks and safety enforcement
  synthesis/        Self-improvement loop (SelfImproveRunner)
  report/           Rich dashboard and aggregation
  sandbox/          Docker sandbox provider
tests/              Mirrors armature/ directory structure
examples/           Working workflow YAML specs
docs/               Architecture, integration, and user guides
```

## Adding a new tool

1. Write a handler: `async def my_tool(args: dict) -> dict`
2. Register it with a `ToolDescriptor` (name, description, permission, handler, reversibility)
3. Add it to `register_builtins()` in `armature/registry/builtins.py`
4. Tests live in `tests/registry/`

## Adding a new CLI command

1. Add a `@app.command()` function to `armature/cli.py`
2. Tests live in `tests/cli/` (use `typer.testing.CliRunner`)

## Spec format questions

See `USER-GUIDE.md` for the full spec reference and `ARCHITECTURE.md` for the design rationale behind each component.
