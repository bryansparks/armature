"""Interactive wizard for generating Armature workflow YAML specs.

Invoked via: armature new [output_file]

Walks the user through every configurable section of HarnessSpec in order,
then writes a commented YAML file they can edit and run immediately.
"""
from __future__ import annotations

import textwrap
from io import StringIO
from pathlib import Path

# ---------------------------------------------------------------------------
# Provider catalogue — shown in selection menus
# ---------------------------------------------------------------------------

_PROVIDERS = ["anthropic", "openai", "openrouter", "ollama", "azure", "bedrock", "other"]

_PROVIDER_DEFAULTS: dict[str, dict] = {
    "anthropic": {"small": "claude-haiku-4-5-20251001", "frontier": "claude-sonnet-4-6"},
    "openai":    {"small": "gpt-4o-mini",               "frontier": "gpt-4o"},
    "openrouter":{"small": "anthropic/claude-haiku-4-5-20251001", "frontier": "anthropic/claude-opus-4-7"},
    "ollama":    {"small": "qwen2.5:7b",                "frontier": "qwen2.5:72b"},
}

_TIER_NAMES = ["tiny", "small", "medium", "large", "frontier"]
_ROLE_TYPES = ["worker", "researcher", "judge", "orchestrator"]
_OUTPUT_MODES = ["text", "json", "guided_json"]
_STAGE_TYPES = ["LLM (role)", "Script / command", "Human gate", "Subagent workflow"]
_FAN_IN_MODES = ["list", "merge", "first"]
_SAFETY_OPS = ["contains", "not_contains", "equals", "not_equals", "matches_regex", "truthy"]
_SAFETY_ACTIONS = ["block", "warn", "log"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _q():
    """Lazy import questionary so CLI stays importable without the extra."""
    try:
        import questionary
        return questionary
    except ImportError:
        raise SystemExit(
            "questionary is required for the wizard.\n"
            "Install it with:  pip install 'armature[wizard]'"
        )


def _console():
    try:
        from rich.console import Console
        return Console()
    except ImportError:
        return None


def _header(text: str) -> None:
    c = _console()
    if c:
        c.print(f"\n[bold cyan]── {text} ──[/bold cyan]")
    else:
        print(f"\n── {text} ──")


def _info(text: str) -> None:
    c = _console()
    if c:
        c.print(f"[dim]{text}[/dim]")
    else:
        print(text)


def _preview(yaml_text: str) -> None:
    c = _console()
    if c:
        from rich.syntax import Syntax
        c.print(Syntax(yaml_text, "yaml", theme="monokai", line_numbers=True))
    else:
        print(yaml_text)


def _ask(q, prompt: str, **kwargs):
    """Thin wrapper that keeps questionary calls readable."""
    return q.text(prompt, **kwargs).ask()


def _slug(s: str) -> str:
    """Convert display name to a safe YAML id."""
    return s.lower().replace(" ", "_").replace("-", "_")


# ---------------------------------------------------------------------------
# Section collectors — each returns a fragment dict
# ---------------------------------------------------------------------------

def _collect_metadata(q) -> dict:
    _header("Workflow Metadata")
    name = _ask(q, "Workflow name:", validate=lambda v: bool(v.strip()) or "Required")
    description = _ask(q, "Short description (optional):", default="")
    version = _ask(q, "Version string:", default="1.0")
    return {"name": name.strip(), "description": description.strip(), "version": version.strip()}


def _collect_tiers(q) -> list[dict]:
    """Returns list of {tier, provider, model, api_base, api_key_env, temperature, max_tokens}."""
    _header("Model Tiers")
    _info("Define named model slots (tiny/small/medium/large/frontier).")
    _info("You only need the tiers your workflow will actually use.")

    tiers = []
    while True:
        tier = q.select(
            "Add a tier (or Done):",
            choices=([t for t in _TIER_NAMES if t not in [x["tier"] for x in tiers]]
                     + ["─── Done ───"]),
        ).ask()
        if tier.startswith("───"):
            if not tiers:
                print("  At least one tier is required.")
                continue
            break

        provider = q.select(f"  Provider for '{tier}':", choices=_PROVIDERS).ask()
        default_model = _PROVIDER_DEFAULTS.get(provider, {}).get(
            tier if tier in ("small", "frontier") else "small", ""
        )
        model = _ask(q, f"  Model name:", default=default_model)

        api_base = ""
        if provider in ("ollama", "other"):
            api_base = _ask(q, "  API base URL:", default="http://localhost:11434" if provider == "ollama" else "")

        api_key_env = ""
        if q.confirm(f"  Use a custom env var for the API key?", default=False).ask():
            api_key_env = _ask(q, "  Env var name (e.g. OPENROUTER_API_KEY):")

        temperature = ""
        if q.confirm("  Set a default temperature for this tier?", default=False).ask():
            temperature = _ask(q, "  Temperature (0.0–1.0):", default="0.7")

        max_tokens = ""
        if q.confirm("  Set a default max_tokens for this tier?", default=False).ask():
            max_tokens = _ask(q, "  Max tokens:", default="2048")

        tiers.append({
            "tier": tier,
            "provider": provider,
            "model": model,
            "api_base": api_base,
            "api_key_env": api_key_env,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        print(f"  ✓ Added '{tier}' tier ({provider}/{model})")

    return tiers


def _collect_role_defaults(q, tiers: list[dict]) -> dict:
    _header("Role Type Defaults")
    _info("Map each role type to a tier. Stages that omit model_tier inherit this.")
    _info("Built-in: worker=small, orchestrator=frontier, judge=frontier, researcher=large")

    available_tiers = [t["tier"] for t in tiers]
    if q.confirm("Customize role type defaults?", default=False).ask():
        defaults = {}
        for role in _ROLE_TYPES:
            defaults[role] = q.select(
                f"  {role}:",
                choices=available_tiers,
                default=available_tiers[0],
            ).ask()
        return defaults
    return {}


def _collect_stages(q, tiers: list[dict]) -> tuple[list[dict], list[dict]]:
    """Returns (stages, adapters)."""
    _header("Stages")
    _info("Build the workflow DAG. Add stages one at a time.")
    _info("Each stage can depend on any previously defined stage.")

    stages = []
    adapters = []
    available_tiers = [t["tier"] for t in tiers]

    while True:
        choice = q.select(
            "Action:",
            choices=["Add a stage", "─── Done with stages ───"],
        ).ask()
        if choice.startswith("───"):
            if not stages:
                print("  At least one stage is required.")
                continue
            break

        stage_id = _ask(q, "  Stage id (snake_case):", validate=lambda v: bool(v.strip()) or "Required")
        stage_id = _slug(stage_id)
        stage_type = q.select("  Stage type:", choices=_STAGE_TYPES).ask()

        depends_on = []
        if stages:
            depends_on = q.checkbox(
                "  Depends on (select none for start node):",
                choices=[s["id"] for s in stages],
            ).ask()

        stage: dict = {"id": stage_id, "depends_on": depends_on}

        if stage_type.startswith("LLM"):
            role_name = _ask(q, "  Role display name:", default=stage_id.replace("_", " ").title())
            role_type = q.select("  Role type:", choices=_ROLE_TYPES).ask()
            description = _ask(
                q,
                "  Role description (what this agent does — edit later for quality):",
                default=f"Perform the {stage_id} task. Be specific and structured.",
            )
            output_mode = q.select("  Output mode:", choices=_OUTPUT_MODES).ask()
            model_tier_override = q.select(
                "  Model tier (or inherit from role_type_defaults):",
                choices=["inherit"] + available_tiers,
            ).ask()

            sig_keys = []
            if q.confirm("  Restrict context keys visible to this stage (signature.input)?", default=False).ask():
                _info("  Enter context keys this stage needs (e.g. 'topic', 'researcher'). Empty to finish.")
                while True:
                    key = _ask(q, "    Key name (blank to stop):", default="")
                    if not key.strip():
                        break
                    desc = _ask(q, f"    Description for '{key}':", default=f"The {key} value")
                    sig_keys.append((key.strip(), desc.strip()))

            on_fail_max = ""
            if q.confirm("  Enable retry on failure (on_fail.loop)?", default=False).ask():
                on_fail_max = _ask(q, "  Max retries:", default="2")

            role: dict = {
                "name": role_name,
                "type": role_type,
                "description": description,
            }
            if model_tier_override != "inherit":
                role["model_tier"] = model_tier_override

            stage["role"] = role
            stage["output_mode"] = output_mode
            if sig_keys:
                stage["signature"] = {"input": {k: d for k, d in sig_keys}}
            if on_fail_max:
                stage["on_fail"] = {"loop": {"stage": stage_id, "max": int(on_fail_max)}}

        elif stage_type.startswith("Script"):
            adapter_name = _slug(_ask(q, "  Adapter name:", default=f"{stage_id}_cmd"))
            cmd = _ask(q, "  Shell command:", default=f"echo 'Running {stage_id}'")
            timeout = _ask(q, "  Timeout (seconds):", default="60")
            stage["adapter"] = adapter_name
            adapters.append({
                "name": adapter_name,
                "type": "script",
                "cmd": cmd,
                "timeout": int(timeout),
            })

        elif stage_type.startswith("Human"):
            present_text = _ask(
                q,
                "  Gate message (use {{ stage_id.field }} for upstream output):",
                default=f"Please review the output before proceeding.",
            )
            stage["gate"] = "human"
            stage["present"] = present_text

        elif stage_type.startswith("Subagent"):
            spec_path = _ask(q, "  Child spec path:", default="workflows/child.yml")
            fan_out = _ask(q, "  Fan-out count (1 for single child):", default="1")
            stage["subagent_spec"] = spec_path
            if int(fan_out) > 1:
                fan_in = q.select("  Fan-in strategy:", choices=_FAN_IN_MODES).ask()
                stage["fan_out"] = int(fan_out)
                stage["fan_in"] = fan_in
                if q.confirm("  Partition a context list across children?", default=False).ask():
                    pk = _ask(q, "  Partition key (context list name):")
                    stage["partition_key"] = pk

        stages.append(stage)
        print(f"  ✓ Added stage '{stage_id}'")

    return stages, adapters


def _collect_safety_rules(q, adapters: list[dict]) -> list[dict]:
    _header("Safety Rules")
    if not adapters:
        _info("No script adapters defined — skipping safety rules.")
        return []
    if not q.confirm("Add declarative safety rules?", default=False).ask():
        return []

    adapter_names = [a["name"] for a in adapters]
    rules = []
    while True:
        tool = q.select(
            "  Rule applies to:",
            choices=adapter_names + ["─── Done ───"],
        ).ask()
        if tool.startswith("───"):
            break

        field = _ask(q, "  Condition field (e.g. 'cmd'):", default="cmd")
        op = q.select("  Operator:", choices=_SAFETY_OPS).ask()
        value = "" if op == "truthy" else _ask(q, "  Match value:")
        action = q.select("  Action:", choices=_SAFETY_ACTIONS).ask()
        message = _ask(q, "  Message:", default=f"{tool} {op} '{value}' is not permitted")

        rules.append({
            "tool": tool,
            "condition": {"field": field, "op": op, "value": value},
            "action": action,
            "message": message,
        })
        print(f"  ✓ Added {action} rule for '{tool}'")

    return rules


def _collect_memory(q, stages: list[dict]) -> dict | None:
    _header("Cross-Run Memory")
    if not q.confirm(
        "Enable cross-run memory? (captures stage outputs across runs)", default=False
    ).ask():
        return None

    stage_ids = [s["id"] for s in stages if "role" in s]
    captures = []
    while True:
        stage_id = q.select(
            "  Capture output from (or Done):",
            choices=stage_ids + ["─── Done ───"],
        ).ask()
        if stage_id.startswith("───"):
            if not captures:
                _info("  No captures configured.")
            break
        key = _ask(q, f"  Output key to capture from '{stage_id}':", default="content")
        max_entries = _ask(q, "  Max entries to keep:", default="5")
        captures.append({"stage": stage_id, "key": key, "max_entries": int(max_entries)})
        print(f"  ✓ Will capture '{stage_id}.{key}' (max {max_entries})")

    inject_as = _ask(q, "  Inject memories into context as:", default="_memory")
    return {"enabled": True, "capture": captures, "inject_as": inject_as}


# ---------------------------------------------------------------------------
# YAML generator — produces commented, human-readable output
# ---------------------------------------------------------------------------

def _indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line if line.strip() else line for line in text.splitlines())


def _wrap_description(text: str, indent: int) -> str:
    """Format a multi-line description as a YAML literal block."""
    pad = " " * indent
    lines = textwrap.wrap(text, width=72) if len(text) < 200 else text.splitlines()
    return "|\n" + "\n".join(f"{pad}{line}" for line in lines)


def generate_yaml(
    meta: dict,
    tiers: list[dict],
    role_defaults: dict,
    stages: list[dict],
    adapters: list[dict],
    safety_rules: list[dict],
    memory: dict | None,
) -> str:
    out = StringIO()

    def w(line: str = "") -> None:
        out.write(line + "\n")

    # ── Header ──
    w(f"# {'=' * 60}")
    w(f"# Armature Workflow: {meta['name']}")
    w(f"# Generated by: armature new")
    w(f"# Edit role descriptions and model choices before running.")
    w(f"# {'=' * 60}")
    w()
    w(f"name: {meta['name']}")
    w(f"version: \"{meta['version']}\"")
    if meta["description"]:
        w(f"description: {_wrap_description(meta['description'], 2)}")
    w()

    # ── Model tiers ──
    w("# ── Model Tiers ────────────────────────────────────────────")
    w("# Swap models here without touching individual stages.")
    w("model_tiers:")
    for t in tiers:
        w(f"  {t['tier']}:")
        w(f"    provider: {t['provider']}")
        w(f"    model: {t['model']}")
        if t["api_base"]:
            w(f"    api_base: {t['api_base']}")
        if t["api_key_env"]:
            w(f"    api_key_env: {t['api_key_env']}")
        if t["temperature"]:
            w(f"    temperature: {t['temperature']}")
        if t["max_tokens"]:
            w(f"    max_tokens: {t['max_tokens']}")
    w()

    # ── Role type defaults ──
    if role_defaults:
        w("# ── Role Type Defaults ─────────────────────────────────────")
        w("# Stages that omit model_tier inherit from here.")
        w("role_type_defaults:")
        for role, tier in role_defaults.items():
            w(f"  {role}: {tier}")
        w()
    else:
        w("# role_type_defaults: (using built-in: worker=small, judge/orchestrator=frontier, researcher=large)")
        w()

    # ── Adapters ──
    if adapters:
        w("# ── Adapters ────────────────────────────────────────────────")
        w("# Script/command stages reference adapters defined here.")
        w("adapters:")
        for a in adapters:
            w(f"  {a['name']}:")
            w(f"    name: {a['name']}")
            w(f"    type: {a['type']}")
            if a.get("cmd"):
                w(f"    cmd: \"{a['cmd']}\"")
            w(f"    timeout: {a['timeout']}")
        w()

    # ── Safety rules ──
    if safety_rules:
        w("# ── Safety Rules ────────────────────────────────────────────")
        w("# Evaluated before every adapter invocation. block/warn/log.")
        w("safety_rules:")
        for r in safety_rules:
            w(f"  - tool: {r['tool']}")
            w(f"    condition:")
            w(f"      field: {r['condition']['field']}")
            w(f"      op: {r['condition']['op']}")
            if r["condition"]["value"]:
                w(f"      value: \"{r['condition']['value']}\"")
            w(f"    action: {r['action']}")
            if r["message"]:
                w(f"    message: \"{r['message']}\"")
        w()

    # ── Memory ──
    if memory:
        w("# ── Cross-Run Memory ────────────────────────────────────────")
        w("# Captures stage outputs across runs and injects them at run start.")
        w(f"# Stored at: ~/.armature/memory/{meta['name']}.db")
        w("memory:")
        w(f"  enabled: {str(memory['enabled']).lower()}")
        if memory["capture"]:
            w("  capture:")
            for cap in memory["capture"]:
                w(f"    - stage: {cap['stage']}")
                w(f"      key: {cap['key']}")
                w(f"      max_entries: {cap['max_entries']}")
        w(f"  inject_as: {memory['inject_as']}")
        w()

    # ── Stages ──
    w("# ── Stages ─────────────────────────────────────────────────")
    w("# The workflow DAG. Execution order is resolved from depends_on.")
    w("stages:")
    for s in stages:
        w(f"  - id: {s['id']}")

        if "role" in s:
            role = s["role"]
            w(f"    role:")
            w(f"      name: {role['name']}")
            w(f"      type: {role['type']}")
            if "model_tier" in role:
                w(f"      model_tier: {role['model_tier']}")
            desc_block = _wrap_description(role["description"], 8)
            w(f"      description: {desc_block}")
            w(f"    output_mode: {s['output_mode']}")

            if s.get("output_mode") in ("json", "guided_json"):
                w("    # output_schema:")
                w("    #   type: object")
                w("    #   required: [field1, field2]")
                w("    #   properties:")
                w("    #     field1: { type: string }")
                w("    #     field2: { type: number }")

        elif "gate" in s:
            w(f"    gate: {s['gate']}")
            present = s.get("present", "Please review before continuing.")
            w(f"    present: |")
            for line in present.splitlines():
                w(f"      {line}")

        elif "adapter" in s:
            w(f"    adapter: {s['adapter']}")

        elif "subagent_spec" in s:
            w(f"    subagent_spec: {s['subagent_spec']}")
            if "fan_out" in s:
                w(f"    fan_out: {s['fan_out']}")
                w(f"    fan_in: {s['fan_in']}")
            if "partition_key" in s:
                w(f"    partition_key: {s['partition_key']}")

        if "signature" in s:
            w(f"    signature:")
            w(f"      input:")
            for k, d in s["signature"]["input"].items():
                w(f"        {k}: {d}")

        if "on_fail" in s:
            loop = s["on_fail"]["loop"]
            w(f"    on_fail:")
            w(f"      loop:")
            w(f"        stage: {loop['stage']}")
            w(f"        max: {loop['max']}")

        deps = s.get("depends_on", [])
        if deps:
            w(f"    depends_on: [{', '.join(deps)}]")
        else:
            w(f"    depends_on: []")
        w()

    return out.getvalue()


# ---------------------------------------------------------------------------
# Defaults banner
# ---------------------------------------------------------------------------

def _show_defaults_banner(c, defaults: dict, config_path) -> None:
    from armature.config import summarize
    bullets = summarize(defaults)
    if not bullets:
        return
    if c:
        c.print(f"[bold yellow]✓ Found defaults:[/bold yellow] [dim]{config_path}[/dim]")
        for b in bullets:
            c.print(f"[dim]{b}[/dim]")
    else:
        print(f"Found defaults: {config_path}")
        for b in bullets:
            print(b)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def run_wizard(output_path: Path | None = None) -> None:
    from armature import config as cfg_mod

    q = _q()
    c = _console()

    if c:
        c.print("\n[bold green]Armature Workflow Wizard[/bold green]")
        c.print("[dim]Answer the prompts to generate a workflow YAML spec.[/dim]")
        c.print("[dim]All inputs can be edited in the generated file.[/dim]\n")
    else:
        print("\n=== Armature Workflow Wizard ===\n")

    # ── Load defaults ──────────────────────────────────────────────────────
    saved_defaults = cfg_mod.load()
    use_defaults = False
    tiers: list[dict] = []
    role_defaults: dict = {}

    if saved_defaults:
        _show_defaults_banner(c, saved_defaults, cfg_mod.DEFAULT_CONFIG_PATH)
        use_defaults = q.confirm(
            "Use saved defaults for model configuration?", default=True
        ).ask()

        if use_defaults:
            if "model_tiers" in saved_defaults:
                tiers = cfg_mod.tiers_from_config(saved_defaults["model_tiers"])
                tier_names = [t["tier"] for t in tiers]
                _info(f"  Loaded tiers: {', '.join(tier_names)}")
            if "role_type_defaults" in saved_defaults:
                role_defaults = dict(saved_defaults["role_type_defaults"])
                _info(f"  Loaded role defaults: {role_defaults}")

    # ── Collect remaining sections ─────────────────────────────────────────
    try:
        meta = _collect_metadata(q)

        if not use_defaults or not tiers:
            tiers = _collect_tiers(q)
        else:
            _header("Model Tiers")
            _info("Using saved defaults (skipped).")

        if not use_defaults or not role_defaults:
            role_defaults = _collect_role_defaults(q, tiers)
        else:
            _header("Role Type Defaults")
            _info("Using saved defaults (skipped).")

        stages, adapters = _collect_stages(q, tiers)
        safety_rules = _collect_safety_rules(q, adapters)
        memory = _collect_memory(q, stages)
    except KeyboardInterrupt:
        print("\n\nWizard cancelled.")
        return

    yaml_text = generate_yaml(meta, tiers, role_defaults, stages, adapters, safety_rules, memory)

    _header("Generated Spec")
    _preview(yaml_text)

    # ── Save spec file ─────────────────────────────────────────────────────
    if output_path is None:
        default_name = f"{_slug(meta['name'])}.yml"
        raw = _ask(q, "\nSave to file:", default=default_name)
        output_path = Path(raw.strip()) if raw.strip() else Path(default_name)

    if q.confirm(f"Write to '{output_path}'?", default=True).ask():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(yaml_text, encoding="utf-8")
        if c:
            c.print(f"\n[bold green]✓ Saved:[/bold green] {output_path}")
            c.print(f"[dim]Run it with:[/dim]  armature run {output_path}")
        else:
            print(f"\nSaved: {output_path}")
            print(f"Run it with: armature run {output_path}")
    else:
        print("\nSpec not saved. Copy the preview above if you need it.")

    # ── Offer to save tier choices as new defaults ─────────────────────────
    _header("Save Defaults")
    should_save = q.confirm(
        "Save these model tiers as your defaults for future workflows?",
        default=not bool(saved_defaults),  # default yes when no config exists yet
    ).ask()

    if should_save:
        new_defaults: dict = {
            "model_tiers": cfg_mod.tiers_to_config(tiers),
        }
        if role_defaults:
            new_defaults["role_type_defaults"] = role_defaults
        saved_path = cfg_mod.save(new_defaults)
        if c:
            c.print(f"[bold green]✓ Defaults saved:[/bold green] [dim]{saved_path}[/dim]")
            c.print(f"[dim]Edit directly or run 'armature new' again to update.[/dim]")
        else:
            print(f"Defaults saved: {saved_path}")
