import asyncio
import json
from pathlib import Path
import typer
from armature.runtime.engine import Harness

app = typer.Typer(name="armature", help="ELF ecosystem agent harness runner", no_args_is_help=True)

channels_app = typer.Typer(name="channels", help="Manage messaging channel connectors")
app.add_typer(channels_app, name="channels")


@app.command()
def new(
    output: Path = typer.Argument(None, help="Output YAML file path (prompted if omitted)"),
):
    """Interactively create a new workflow spec (YAML)."""
    from armature.cli_wizard import run_wizard
    run_wizard(output_path=output)


_ROLE_COLORS: dict[str, str] = {
    "orchestrator": "magenta",
    "researcher": "green",
    "judge": "yellow",
    "critic": "red",
    "planner": "#60a5fa",
    "validator": "yellow",
}


def _print_run_header(
    spec,
    quiet: bool,
    parsed_inputs: dict | None = None,
    last_run: dict | None = None,
) -> None:
    if quiet:
        return

    from rich.console import Console
    from rich.table import Table
    from rich import box

    console = Console(highlight=False)

    normal = [s for s in spec.stages if not s.post_run]
    post_run_stages = [s for s in spec.stages if s.post_run]
    fan_outs = [s for s in normal if s.fan_out]

    stage_label = f"{len(normal)} stages"
    if fan_outs:
        stage_label += f", {len(fan_outs)} fan-out"
    if post_run_stages:
        stage_label += f", {len(post_run_stages)} post-run"

    # ── top rule with workflow identity ─────────────────────────────────
    safety_str = ""
    if spec.safety_mode and spec.safety_mode != "permissive":
        safety_str = f"  [bold yellow]safety: {spec.safety_mode}[/bold yellow]"
    title_text = (
        f"[bold]{spec.name}[/bold]"
        f"  [#888888]v{spec.version}[/#888888]"
        f"  [#888888]·[/#888888]  {stage_label}"
        f"{safety_str}"
    )
    console.print()
    console.rule(title_text, style="#888888")

    if spec.description:
        desc = spec.description.strip().replace("\n", " ")
        console.print(f"  [italic]{desc}[/italic]")

    console.print()

    # ── model tiers ─────────────────────────────────────────────────────
    tier_names = ["tiny", "small", "medium", "large", "frontier"]
    tiers = [(n, getattr(spec.model_tiers, n)) for n in tier_names if getattr(spec.model_tiers, n)]
    if spec.model_tiers.__pydantic_extra__:
        tiers += [(k, v) for k, v in spec.model_tiers.__pydantic_extra__.items() if v]
    if tiers:
        parts = "  ".join(
            f"[cyan]{name}[/cyan] [#888888]›[/#888888] {cfg.model.split('/')[-1] if '/' in cfg.model else cfg.model}"
            for name, cfg in tiers
        )
        provider = tiers[0][1].provider if tiers else ""
        console.print(f"  [#888888]Tiers[/#888888]    {parts}  [#888888]({provider})[/#888888]")

    # ── declared inputs with runtime values ──────────────────────────────
    _injected = {"run_id", "prior_run", "prior_research", "_prior_sources", "_memory", "_knowledge"}
    if spec.contracts and spec.contracts.inputs:
        user_inputs = [i.get("name", str(i)) for i in spec.contracts.inputs if i.get("name") not in _injected]
        if user_inputs:
            parts = []
            for name in user_inputs:
                val = (parsed_inputs or {}).get(name)
                if val is not None:
                    s = str(val)
                    display = (s[:48] + "…") if len(s) > 48 else s
                    parts.append(f"{name} [#888888]=[/#888888] [italic]\"{display}\"[/italic]")
                else:
                    parts.append(f"[#888888]{name}[/#888888]")
            console.print("  [#888888]Inputs[/#888888]   " + "  [#888888]·[/#888888]  ".join(parts))

    # ── last run timing ──────────────────────────────────────────────────
    if last_run:
        def _fmt_ago(s: float) -> str:
            if s < 60:
                return f"{int(s)}s ago"
            if s < 3600:
                return f"{int(s / 60)}m ago"
            if s < 86400:
                return f"{int(s / 3600)}h ago"
            return f"{int(s / 86400)}d ago"

        def _fmt_dur(s: float) -> str:
            if s < 60:
                return f"{s:.0f}s"
            m, sec = divmod(int(s), 60)
            return f"{m}m {sec}s"

        ago = _fmt_ago(last_run["ago_s"])
        dur = _fmt_dur(last_run["elapsed_s"])
        console.print(f"  [#888888]Last run[/#888888] {ago}  [#888888]·[/#888888]  completed in {dur}")

    # ── flags / extras ──────────────────────────────────────────────────
    extras = []
    if spec.tools:
        extras.append("tools: " + ", ".join(t.module for t in spec.tools))
    if spec.mcp_servers:
        extras.append(f"mcp: {len(spec.mcp_servers)}")
    if spec.continuation:
        n = len(spec.continuation.carry_forward)
        extras.append(f"continuation ({n} key{'s' if n != 1 else ''})")
    if spec.triggers:
        types = ", ".join(t.type for t in spec.triggers)
        extras.append(f"triggers ({types})")
    if spec.checkpoint:
        extras.append("checkpoint")
    if extras:
        console.print("  [#888888]Flags[/#888888]    " + "  [#888888]·[/#888888]  ".join(extras))

    # ── agent table ─────────────────────────────────────────────────────
    console.print()

    def _resolve_tier(stage):
        if not stage.role:
            return "—"
        if stage.role.model_tier:
            return stage.role.model_tier
        return getattr(spec.role_type_defaults, stage.role.type.value, "")

    table = Table(
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold #888888",
        padding=(0, 1),
        show_edge=False,
    )
    table.add_column("Stage", style="bold", no_wrap=True)
    table.add_column("Agent", no_wrap=True)
    table.add_column("Tier", style="cyan", no_wrap=True)
    table.add_column("Notes", style="#888888", no_wrap=True)

    for stage in normal + post_run_stages:
        tier = _resolve_tier(stage)

        if stage.role:
            rt = stage.role.type.value
            color = _ROLE_COLORS.get(rt, "")
            rt_markup = f"[{color}]({rt})[/{color}]" if color else f"[#888888]({rt})[/#888888]"
            agent = f"{stage.role.name} {rt_markup}"
        elif stage.tool_call:
            agent = "[#888888]tool_call[/#888888]"
        elif stage.gate:
            agent = "[#888888]human gate[/#888888]"
        elif stage.subagent_spec:
            agent = "[#888888]subagent[/#888888]"
        elif stage.adapter:
            agent = "[#888888]adapter[/#888888]"
        else:
            agent = "[#888888]—[/#888888]"

        notes = []
        if stage.fan_out:
            notes.append(f"fan-out \xd7{stage.fan_out}")
        if stage.skip_if:
            notes.append("conditional")
        if stage.post_run:
            notes.append("[yellow]post-run[/yellow]")

        table.add_row(stage.id, agent, tier, "  ·  ".join(notes) if notes else "")

    console.print(table)
    console.rule(style="#888888")


def _version_callback(value: bool) -> None:
    if value:
        from armature import __version__
        typer.echo(f"armature {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Armature — multi-agent workflow engine."""


def parse_inputs(raw: list[str]) -> dict:
    result = {}
    for item in raw:
        if "=" not in item:
            typer.echo(f"Invalid input format '{item}' — use key=value", err=True)
            raise typer.Exit(1)
        k, _, v = item.partition("=")
        result[k.strip()] = v.strip()
    return result


def _make_on_event(quiet: bool, fan_out_ids: set | None = None):
    """Return an on_event callback that prints live progress."""
    if quiet:
        return None

    import sys
    from rich.console import Console
    console = Console(highlight=False)
    err_console = Console(stderr=True, highlight=False)

    _fo_ids = fan_out_ids or set()
    # Per-fan-out-stage state: {stage_id: {started, done, times, line_len}}
    _fan: dict = {}
    _active: list = [None]  # [current_fan_stage_id | None]

    def _update_fan_line(sid: str) -> None:
        st = _fan[sid]
        started, done = st["started"], st["done"]
        in_flight = started - done
        times = st["times"]
        avg = f"  avg {sum(times)/len(times):.1f}s" if times else ""
        line = f"  ⟳ {sid}  {done}/{started} done  {in_flight} in-flight{avg}"
        pad = max(0, st["line_len"] - len(line))
        sys.stdout.write(f"\r{line}" + " " * pad)
        sys.stdout.flush()
        st["line_len"] = len(line)

    def _finalize_fan() -> None:
        sid = _active[0]
        if sid is None:
            return
        st = _fan.get(sid, {})
        # Clear the line then print final summary
        sys.stdout.write("\r" + " " * (st.get("line_len", 0) + 4) + "\r")
        sys.stdout.flush()
        done = st.get("done", 0)
        times = st.get("times", [])
        avg = f"  [dim]avg {sum(times)/len(times):.1f}s[/dim]" if times else ""
        console.print(f"  [green]✓[/green] [bold]{sid}[/bold] [dim]\xd7{done}[/dim]{avg}")
        _active[0] = None

    def on_event(event_type: str, data: dict) -> None:
        stage = data.get("stage", "")

        # Finalize any in-progress fan-out when a different stage fires
        if _active[0] is not None and stage != _active[0]:
            _finalize_fan()

        if event_type == "stage_start":
            if stage in _fo_ids:
                if stage not in _fan:
                    _fan[stage] = {"started": 0, "done": 0, "times": [], "line_len": 0}
                _fan[stage]["started"] += 1
                _active[0] = stage
                _update_fan_line(stage)
                return
            kind = data.get("kind", "?")
            role = f" [#888888]{data['role']}[/#888888]" if data.get("role") else ""
            console.print(f"  [cyan]→[/cyan] [bold]{stage}[/bold] [#888888]({kind})[/#888888]{role}")

        elif event_type == "stage_complete":
            if stage in _fo_ids and _active[0] == stage:
                try:
                    elapsed = float(data.get("elapsed_s", 0))
                except (TypeError, ValueError):
                    elapsed = 0.0
                _fan[stage]["done"] += 1
                _fan[stage]["times"].append(elapsed)
                _update_fan_line(stage)
                return
            console.print(f"  [green]✓[/green] {stage} [#888888]({data['elapsed_s']}s)[/#888888]")

        elif event_type == "stage_skipped":
            reason = data.get("reason", "")
            console.print(f"  [#888888]- {stage} (skipped: {reason})[/#888888]")

        elif event_type == "stage_resumed":
            console.print(f"  [yellow]↩[/yellow] {stage} [#888888][resumed from checkpoint][/#888888]")

        elif event_type == "stage_failed":
            if _active[0] == stage:
                _finalize_fan()
            err_console.print(f"  [red]✗[/red] [bold]{stage}[/bold] [[red]{data['type']}[/red]]: {data['reason'][:80]}")

        elif event_type == "retry_attempt":
            console.print(f"  [yellow]⟳[/yellow] {stage} retry {data['attempt']}/{data['max']} [#888888]{data['reason'][:60]}[/#888888]")

        elif event_type == "run_summary":
            if _active[0] is not None:
                _finalize_fan()
            rogue = data.get("rogue_signals", 0)
            failed = data["stages_failed"]
            rogue_str = f", [bold red]{rogue} blocked[/bold red]" if rogue else ""
            failed_str = f"[bold red]{failed} failed[/bold red]" if failed else f"[#888888]{failed} failed[/#888888]"
            console.print(
                f"\n[bold]Done[/bold] in [bold]{data['elapsed_s']}s[/bold] — "
                f"[green]{data['stages_ran']} ran[/green]  "
                f"[#888888]{data['stages_skipped']} skipped[/#888888]  "
                f"{failed_str}"
                f"{rogue_str}"
            )

    return on_event


def _find_primary_stage(spec) -> str | None:
    """Return the terminal non-post-run stage ID (the DAG leaf with deepest dependencies)."""
    normal = [s for s in spec.stages if not s.post_run]
    if not normal:
        return None
    depended_on = {dep for s in normal for dep in (s.depends_on or [])}
    leaves = [s for s in normal if s.id not in depended_on]
    if not leaves:
        return normal[-1].id
    return max(leaves, key=lambda s: len(s.depends_on or [])).id


def _show_primary_output(spec, result: dict, quiet: bool) -> None:
    if quiet or not result:
        return
    stage_id = _find_primary_stage(spec)
    if not stage_id:
        return
    stage_out = result.get(stage_id)
    if not stage_out:
        return

    if isinstance(stage_out, dict) and "content" in stage_out:
        text = str(stage_out["content"]).strip()
    elif isinstance(stage_out, str):
        text = stage_out.strip()
    elif isinstance(stage_out, (list, dict)):
        text = json.dumps(stage_out, indent=2, default=str)
    else:
        text = str(stage_out).strip()

    if not text:
        return

    from rich.console import Console
    from rich.panel import Panel
    console = Console(highlight=False)
    truncated = (text[:500] + "\n[#888888]…[/#888888]") if len(text) > 500 else text
    console.print()
    console.print(Panel(
        truncated,
        title=f"[bold]{stage_id}[/bold]",
        subtitle="[#888888]primary output[/#888888]",
        border_style="#888888",
        padding=(0, 1),
    ))


def _save_last_result(workflow_name: str, result: dict) -> None:
    try:
        save_dir = Path.home() / ".armature" / "last"
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / f"{workflow_name}.json").write_text(
            json.dumps(result, indent=2, default=str)
        )
    except Exception:
        pass


@app.command()
def validate(
    spec: Path = typer.Argument(..., help="Path to workflow spec YAML"),
):
    """Validate a workflow spec file and report all errors."""
    if not spec.exists():
        typer.echo(f"Spec file not found: {spec}", err=True)
        raise typer.Exit(1)

    from armature.spec.loader import load_spec
    from armature.spec.validator import validate_spec

    try:
        loaded = load_spec(spec)
    except Exception as exc:
        typer.echo(f"Failed to parse spec: {exc}", err=True)
        raise typer.Exit(1)

    all_issues = validate_spec(loaded, strict=False)
    hard_errors = [e for e in all_issues if e.severity == "error"]
    warnings = [e for e in all_issues if e.severity == "warning"]

    if warnings:
        typer.echo(f"⚠ {len(warnings)} warning(s):")
        for w in warnings:
            stage_label = f"  stage='{w.stage_id}'" if w.stage_id else ""
            typer.echo(f"  [{w.code}]{stage_label}: {w.message}")

    if not hard_errors:
        from armature.spec.risk import compute_spec_risk
        risk = compute_spec_risk(loaded)
        typer.echo(f"✓ '{loaded.name}' is valid ({len(loaded.stages)} stages)")
        typer.echo(f"  Risk: {risk.tier.upper()} [score={risk.score}]")
        for factor in risk.factors:
            sign = "+" if factor.delta >= 0 else ""
            typer.echo(f"    {sign}{factor.delta}  {factor.label}")
        return

    typer.echo(f"✗ '{loaded.name}' has {len(hard_errors)} validation error(s):\n", err=True)
    for e in hard_errors:
        stage_label = f"  stage='{e.stage_id}'" if e.stage_id else ""
        typer.echo(f"  [{e.code}]{stage_label}: {e.message}", err=True)
    raise typer.Exit(1)


def _print_provider_error(exc: Exception) -> bool:
    """Translate common LLM-provider failures into a concise, actionable message.

    Returns True if the error was recognized and a friendly message printed
    (caller should exit non-zero); False if the caller should re-raise so genuine
    bugs still surface a full traceback.
    """
    try:
        import litellm
    except Exception:  # pragma: no cover - litellm always present at runtime
        litellm = None  # type: ignore

    name = type(exc).__name__
    msg = str(exc) or ""
    first_line = next((ln for ln in msg.splitlines() if ln.strip()), name)

    def _types(*attrs):
        if litellm is None:
            return tuple()
        return tuple(t for t in (getattr(litellm, a, None) for a in attrs) if isinstance(t, type))

    auth_types = _types("AuthenticationError", "PermissionDeniedError")
    if (auth_types and isinstance(exc, auth_types)) or "api key" in msg.lower() or "api_key" in msg.lower():
        typer.echo(
            "\n✗ No valid API key for the model provider.\n"
            "  Set the key that matches your spec's model_tiers provider, e.g.:\n"
            "      export ANTHROPIC_API_KEY=sk-...     # or OPENAI_API_KEY / GEMINI_API_KEY\n"
            "  No key? Run entirely locally with Ollama — see the “No API key?” section in the README.",
            err=True,
        )
        return True

    conn_types = _types("APIConnectionError", "Timeout", "APITimeoutError", "ServiceUnavailableError")
    if conn_types and isinstance(exc, conn_types):
        typer.echo(
            f"\n✗ Could not reach the model provider ({name}).\n"
            f"  {first_line}\n"
            "  If you're using Ollama, make sure it's running (`ollama serve`) and the model is pulled.",
            err=True,
        )
        return True

    rate_types = _types("RateLimitError")
    if rate_types and isinstance(exc, rate_types):
        typer.echo(f"\n✗ Provider rate limit reached. {first_line}", err=True)
        return True

    return False


@app.command()
def run(
    spec: Path = typer.Argument(..., help="Path to workflow spec YAML"),
    inputs: list[str] = typer.Option([], "--input", "-i", help="Input values as key=value"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate spec without executing"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress progress output"),
    output_file: Path = typer.Option(None, "--output", "-o", help="Write result JSON to file"),
    force: bool = typer.Option(False, "--force", help="Ignore checkpoint and rerun all stages"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Disable LLM response cache"),
    registry_dir: Path | None = typer.Option(None, "--registry", help="Override adapter registry directory"),
    auto_improve: bool = typer.Option(False, "--auto-improve", help="Analyze traces and auto-apply spec improvements when HQS < 0.75"),
):
    """Run a workflow from a YAML spec file."""
    if not spec.exists():
        typer.echo(f"Spec file not found: {spec}", err=True)
        raise typer.Exit(1)

    parsed_inputs = parse_inputs(inputs)

    from armature.spec.validator import SpecValidationError
    from armature.adapters.registry import AdapterRegistry

    try:
        harness = Harness.from_spec(
            spec,
            vars=parsed_inputs,
            use_cache=not no_cache,
            adapter_registry=AdapterRegistry(base_dir=registry_dir) if registry_dir else None,
        )
    except SpecValidationError as exc:
        typer.echo(f"Spec validation failed:\n{exc}", err=True)
        raise typer.Exit(1)

    if dry_run:
        typer.echo(f"✓ Spec '{harness.name}' is valid ({len(harness._spec.stages)} stages)")
        typer.echo("Dry run — no execution.")
        return

    async def _run():
        last_run_info = None
        if not quiet:
            try:
                from datetime import datetime, timezone
                await harness._traces.init()
                last_rid = await harness._traces.latest_run_id(harness._spec.name)
                if last_rid:
                    prior_traces = await harness._traces.query_by_run(last_rid)
                    if prior_traces:
                        first_ts = datetime.fromisoformat(prior_traces[0].timestamp)
                        if first_ts.tzinfo is None:
                            first_ts = first_ts.replace(tzinfo=timezone.utc)
                        ago_s = (datetime.now(timezone.utc) - first_ts).total_seconds()
                        total_s = sum(t.latency_ms for t in prior_traces) / 1000
                        last_run_info = {"ago_s": ago_s, "elapsed_s": total_s}
            except Exception:
                pass
        _print_run_header(harness._spec, quiet, parsed_inputs, last_run_info)
        fan_out_ids = {s.id for s in harness._spec.stages if s.fan_out}
        harness._on_event = _make_on_event(quiet, fan_out_ids=fan_out_ids)
        return await harness.run(parsed_inputs, force=force)

    try:
        result = asyncio.run(_run())
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        if _print_provider_error(exc):
            raise typer.Exit(1)
        raise

    _show_primary_output(harness._spec, result, quiet)
    _save_last_result(harness._spec.name, result)

    if not quiet:
        from rich.console import Console
        Console(highlight=False).print(
            f"\n  [#888888]→[/#888888]  [bold]armature last {spec}[/bold]"
            f"  [#888888]to re-read this output  [/#888888]"
            f" [#888888]·[/#888888]  [bold]armature dashboard {spec}[/bold]"
            f"  [#888888]for history and metrics[/#888888]"
        )

    result_json = json.dumps(result, indent=2, default=str)
    if output_file:
        output_file.write_text(result_json)
        if not quiet:
            typer.echo(f"Result written to {output_file}")
    elif quiet:
        # quiet mode = machine-readable; emit full JSON for piping/scripting
        typer.echo(result_json)
    # interactive mode: primary output panel + armature last already cover this

    if auto_improve:
        from armature.synthesis.improve import SelfImproveRunner

        if not quiet:
            typer.echo("\nAuto-improve: analyzing traces...")

        async def _improve():
            improve_runner = SelfImproveRunner(spec, target_hqs=0.75)
            return await improve_runner.analyze()

        try:
            report = asyncio.run(_improve())
        except Exception as exc:
            if _print_provider_error(exc):
                typer.echo("Auto-improve skipped — spec unchanged.", err=True)
            else:
                typer.echo(f"Auto-improve error: {exc}", err=True)
            return

        if not report.needs_improvement:
            typer.echo("Auto-improve: workflow is healthy — no improvement needed.")
        elif report.applied:
            typer.echo(f"Auto-improve: spec updated → {spec}")
        elif report.requires_review:
            typer.echo(f"Auto-improve: structural changes require review → {report.pending_path}")
        else:
            typer.echo("Auto-improve: refiner could not produce a valid revision.")


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind host"),
    port: int = typer.Option(8080, "--port", "-p", help="Bind port"),
    specs_dir: Path = typer.Option(None, "--specs-dir", help="Directory of workflow specs to register"),
):
    """Start the Armature HTTP service."""
    try:
        import uvicorn
        from armature.service.app import build_app
        from armature.service.registry import WorkflowRegistry
    except ImportError:
        typer.echo("FastAPI/uvicorn not installed. Run: pip install 'armature[service]'", err=True)
        raise typer.Exit(1)

    registry = WorkflowRegistry()
    if specs_dir:
        if not specs_dir.is_dir():
            typer.echo(f"specs-dir not found: {specs_dir}", err=True)
            raise typer.Exit(1)
        registry.load_dir(specs_dir)
        typer.echo(f"Registered {len(registry.list_all())} workflow(s) from {specs_dir}")

    fastapi_app = build_app(registry=registry)
    typer.echo(f"Starting Armature service on {host}:{port}")
    uvicorn.run(fastapi_app, host=host, port=port)


@app.command()
def optimize(
    spec: Path = typer.Argument(..., help="Path to the workflow spec to optimize"),
    trace_db: Path = typer.Option(
        Path("~/.armature/traces.db").expanduser(),
        "--traces",
        help="Path to trace database",
    ),
    apply: bool = typer.Option(False, "--apply", help="Apply the proposed diff if accepted"),
    model: str = typer.Option(None, "--model", help="LLM for the optimizer (default: anthropic/claude-opus-4-7; override with ARMATURE_REFINER_MODEL env var)"),
):
    """Run the Meta-Harness optimizer on a workflow spec."""
    if not spec.exists():
        typer.echo(f"Spec not found: {spec}", err=True)
        raise typer.Exit(1)

    from armature.optimizer.runner import OptimizerRunner

    async def _run():
        runner = OptimizerRunner(target_spec_path=spec, trace_db_path=trace_db, model_override=model)
        return await runner.optimize()

    typer.echo(f"Analyzing traces for: {spec.name}")
    try:
        result = asyncio.run(_run())
    except typer.Exit:
        raise
    except Exception as exc:
        if _print_provider_error(exc):
            raise typer.Exit(1)
        raise

    if result is None:
        typer.echo("Not enough trace data to optimize. Run more workflows first.")
        return

    typer.echo(f"\nOptimizer result (accepted={result.accepted}, score={result.score:.2f}):")
    typer.echo(f"Rationale: {result.rationale}")
    typer.echo(f"\nProposed diff:\n{result.proposed_diff}")

    if result.accepted and apply:
        from armature.optimizer.runner import OptimizerRunner
        ok, msg = OptimizerRunner.apply_diff(spec, result.proposed_diff)
        typer.echo(f"\n{'Applied' if ok else 'Apply failed'}: {msg}")
    elif result.accepted and not apply:
        typer.echo("\nProposal accepted — re-run with --apply to patch the spec file.")


@app.command()
def report(
    run_id: str = typer.Option(None, "--run-id", help="Run ID to report on"),
    workflow: str = typer.Option(None, "--workflow", help="Workflow name — resolves to the most recent run for that workflow"),
    output_file: Path = typer.Option(None, "--output-file", help="Write report to file (.md or .html); suppresses terminal output"),
    traces: Path = typer.Option(None, "--traces", help="Path to traces.db (default: ~/.armature/traces.db)"),
    evals: Path = typer.Option(None, "--evals", help="Path to evaluations database"),
    knowledge: Path = typer.Option(None, "--knowledge", help="Path to knowledge database"),
    session_log: Path = typer.Option(None, "--session-log", help="Path to session.jsonl"),
):
    """Print a human-readable report for a completed workflow run.

    Identify the run by workflow name or exact run ID:

      armature report --workflow launchpad
      armature report --run-id abc123

    Save to a file (terminal output suppressed):

      armature report --workflow launchpad --output-file launchpad.md
      armature report --workflow launchpad --output-file launchpad.html
    """
    from armature.reporting import load_report_data
    from armature.report.run_report import render_run_report, render_run_report_markdown, render_run_report_html
    from armature.state.traces import TraceStore

    if not run_id and not workflow:
        typer.echo("Provide --workflow <name> or --run-id <id>.", err=True)
        raise typer.Exit(1)

    if output_file:
        ext = output_file.suffix.lower()
        if ext not in (".md", ".html", ".htm"):
            typer.echo(f"Unsupported extension '{ext}'. Use .md or .html", err=True)
            raise typer.Exit(1)

    resolved_traces = traces or Path("~/.armature/traces.db").expanduser()

    async def _resolve_run_id() -> str | None:
        if run_id:
            return run_id
        store = TraceStore(resolved_traces)
        await store.init()
        resolved = await store.latest_run_id(workflow)
        if resolved is None:
            typer.echo(
                f"No runs found for workflow '{workflow}' in {resolved_traces}.",
                err=True,
            )
        return resolved

    resolved_id = asyncio.run(_resolve_run_id())
    if resolved_id is None:
        raise typer.Exit(1)

    resolved_session = session_log or Path(f"~/.armature/runs/{resolved_id}/session.jsonl").expanduser()

    async def _load():
        return await load_report_data(
            run_id=resolved_id,
            traces_db=resolved_traces,
            evals_db=evals,
            knowledge_db=knowledge,
            session_log=resolved_session,
        )

    data = asyncio.run(_load())
    if data is None:
        typer.echo(
            f"No traces found for run_id='{resolved_id}'.\n"
            f"  Looked in: {resolved_traces}",
            err=True,
        )
        raise typer.Exit(1)

    if output_file:
        ext = output_file.suffix.lower()
        if ext == ".md":
            content = render_run_report_markdown(data)
        else:
            content = render_run_report_html(data)
        output_file.write_text(content, encoding="utf-8")
        typer.echo(f"Report written to {output_file}")
    else:
        render_run_report(data)


@app.command()
def replay(
    run_id: str = typer.Argument(..., help="Run ID to display"),
    traces: Path = typer.Option(None, "--traces", help="Path to traces.db (default: ~/.armature/traces.db)"),
):
    """Display a recorded run stage-by-stage from the TraceStore."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    resolved_traces = traces or Path("~/.armature/traces.db").expanduser()

    from armature.state.traces import TraceStore

    async def _load():
        store = TraceStore(resolved_traces)
        await store.init()
        records = await store.query_by_run(run_id)
        hqs_result = await store.compute_hqs(run_id) if records else None
        return records, hqs_result

    records, hqs_result = asyncio.run(_load())

    if not records:
        typer.echo(f"No traces found for run_id='{run_id}' in {resolved_traces}", err=True)
        raise typer.Exit(1)

    t = Table(show_header=True, header_style="bold", border_style="dim", expand=True)
    t.add_column("Stage", style="bold")
    t.add_column("Role")
    t.add_column("Model", style="dim")
    t.add_column("Latency", justify="right")
    t.add_column("Status", justify="center")
    t.add_column("Quorum", justify="right")
    t.add_column("Outputs (truncated)", style="dim")

    for r in records:
        status = "[green]✓[/green]" if r.success else "[red]✗[/red]"
        quorum = f"{r.quorum_score:.2f}" if r.quorum_score is not None else "—"
        out_str = json.dumps(r.outputs, default=str)
        if len(out_str) > 80:
            out_str = out_str[:77] + "..."
        t.add_row(r.stage_id, r.role_type, r.model, f"{r.latency_ms:.0f}ms", status, quorum, out_str)

    console.print(f"\n[bold]Replay[/bold]: run [cyan]{run_id}[/cyan]  ({len(records)} stages)\n")
    console.print(t)

    if hqs_result:
        console.print(
            f"\n[bold]HQS[/bold]: [cyan]{hqs_result.hqs:.3f}[/cyan]  "
            f"(valid={hqs_result.output_valid_rate:.0%}  "
            f"success={hqs_result.success_rate:.0%}  "
            f"n={hqs_result.n_traces})\n"
        )


@app.command(name="export-traces")
def export_traces(
    workflow: str = typer.Option(..., "--workflow", "-w", help="Workflow name to export traces for"),
    output: Path = typer.Option(..., "--output", "-o", help="Output JSONL file path"),
    traces_db: Path = typer.Option(
        None, "--traces", help="Path to traces.db (default: ~/.armature/traces.db)"
    ),
    format: str = typer.Option("chat", "--format", "-f", help="Format: chat | alpaca | sharegpt | dpo"),
    min_score: float = typer.Option(0.85, "--min-score", help="Minimum quorum score for chosen traces"),
    rejected_max_score: float = typer.Option(0.30, "--rejected-max-score", help="Max quorum score for DPO rejected traces"),
    role_types: str = typer.Option(None, "--role-types", help="Comma-separated role types to include (e.g. judge,researcher)"),
    system_prompt: str = typer.Option(None, "--system-prompt", help="Override the system/instruction field in all records"),
    limit: int = typer.Option(1000, "--limit", help="Maximum traces to fetch"),
):
    """Export high-quality traces as SFT or DPO training data (JSONL).

    Formats:
      chat      OpenAI ChatML — system/user/assistant messages (default; Qwen compatible)
      alpaca    Stanford Alpaca — instruction/input/output
      sharegpt  ShareGPT — human/gpt conversation pairs
      dpo       DPO/GRPO — chosen/rejected pairs for preference training
    """
    from armature.state.traces import TraceStore
    from armature.state.export import TraceExporter

    db_path = traces_db or Path("~/.armature/traces.db").expanduser()
    if not db_path.exists():
        typer.echo(f"Traces database not found: {db_path}", err=True)
        raise typer.Exit(1)

    role_list = [r.strip() for r in role_types.split(",")] if role_types else None
    store = TraceStore(db_path)
    exporter = TraceExporter(store)

    async def _run():
        if format == "dpo":
            return await exporter.export_dpo(
                workflow,
                output,
                chosen_min_score=min_score,
                rejected_max_score=rejected_max_score,
                system_prompt=system_prompt,
                limit=limit,
            )
        return await exporter.export(
            workflow,
            output,
            format=format,  # type: ignore[arg-type]
            min_quorum_score=min_score,
            role_types=role_list,
            system_prompt=system_prompt,
            limit=limit,
        )

    summary = asyncio.run(_run())
    typer.echo(
        f"Exported {summary.total_exported} record{'s' if summary.total_exported != 1 else ''} "
        f"→ {summary.output_path}  [{summary.format} format, min_score={summary.min_quorum_score}]"
    )


@app.command()
def improve(
    spec: Path = typer.Argument(..., help="Path to the workflow spec to improve"),
    trace_db: Path = typer.Option(
        None, "--traces", help="Path to traces database (default: ~/.armature/traces.db)"
    ),
    model: str = typer.Option(None, "--model", help="LLM for the SpecRefiner (default: auto-detected from the spec's top tier; override with ARMATURE_REFINER_MODEL env var)"),
    target_hqs: float = typer.Option(0.90, "--target-hqs", help="HQS threshold below which improvement is triggered"),
    min_traces: int = typer.Option(3, "--min-traces", help="Minimum traces required before analysis"),
    apply: bool = typer.Option(True, "--apply/--no-apply", help="Auto-apply proposed spec (default: apply)"),
    log: Path = typer.Option(None, "--log", help="Path to improvement log JSONL (default: <spec>.improve_log.jsonl)"),
):
    """Analyze traces and propose/apply a targeted spec improvement."""
    if not spec.exists():
        typer.echo(f"Spec not found: {spec}", err=True)
        raise typer.Exit(1)

    from armature.synthesis.improve import SelfImproveRunner

    db_path = trace_db or Path("~/.armature/traces.db").expanduser()

    async def _run():
        runner = SelfImproveRunner(
            spec,
            db_path,
            model=model,
            target_hqs=target_hqs,
            min_traces=min_traces,
            auto_apply=apply,
            log_path=log,
        )
        return await runner.analyze()

    typer.echo(f"Analyzing: {spec.name}")
    try:
        report = asyncio.run(_run())
    except typer.Exit:
        raise
    except Exception as exc:
        if _print_provider_error(exc):
            raise typer.Exit(1)
        raise

    typer.echo(f"  traces: {report.n_traces}  HQS: {f'{report.hqs_before:.3f}' if report.hqs_before is not None else 'n/a'}  needs_improvement: {report.needs_improvement}")

    if report.n_traces == 0:
        typer.echo("No traces found — run the workflow first.")
        return

    if not report.needs_improvement:
        typer.echo("Workflow is healthy — no improvement needed.")
        return

    if report.proposed_spec is None:
        typer.echo("Refiner could not produce a valid revised spec.", err=True)
        return

    if report.applied:
        typer.echo(f"Applied revised spec → {spec}")
    else:
        typer.echo("Proposed revision available (--no-apply was set — spec not written).")

    if report.diagnostics:
        typer.echo("Failure signatures:")
        for d in report.diagnostics:
            detail = f" — {d.details}" if d.details else ""
            typer.echo(f"  [{d.stage_id}] {d.code.value}{detail}")

    if report.log_path:
        typer.echo(f"Log: {report.log_path}")


@app.command()
def export(
    spec: Path = typer.Argument(..., help="Path to workflow spec YAML"),
    target: str = typer.Option("hermes", "--target", "-t", help="Export target platform (hermes)"),
    output: Path = typer.Option(Path("."), "--output", "-o", help="Output directory for the bundle"),
):
    """Export a HarnessSpec to an external agent platform format."""
    if not spec.exists():
        typer.echo(f"Spec file not found: {spec}", err=True)
        raise typer.Exit(1)

    if target != "hermes":
        typer.echo(f"Unknown target '{target}'. Supported: hermes", err=True)
        raise typer.Exit(1)

    from armature.spec.loader import load_spec
    from armature.emitters.hermes import HermesEmitter

    try:
        loaded = load_spec(spec)
    except Exception as exc:
        typer.echo(f"Failed to parse spec: {exc}", err=True)
        raise typer.Exit(1)

    bundle_dir = HermesEmitter().emit(loaded, output)
    typer.echo(f"Hermes-agent bundle written to: {bundle_dir}")


@app.command()
def watch(
    spec: Path = typer.Argument(..., help="Path to workflow spec YAML"),
    host: str = typer.Option("0.0.0.0", "--host", help="Host for webhook listener"),
    port: int = typer.Option(8081, "--port", "-p", help="Port for webhook triggers"),
    traces: Path = typer.Option(None, "--traces", help="Path to traces SQLite database"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress run output"),
    tune: bool = typer.Option(False, "--tune", help="Run tune daemon that optimizes the spec between triggers (skeleton)"),
):
    """Run trigger listeners for a spec. Blocks until Ctrl-C."""
    if not spec.exists():
        typer.echo(f"Spec file not found: {spec}", err=True)
        raise typer.Exit(1)

    from armature.spec.loader import load_spec
    from armature.service.triggers import TriggerDispatcher

    try:
        loaded = load_spec(spec)
    except Exception as exc:
        typer.echo(f"Failed to parse spec: {exc}", err=True)
        raise typer.Exit(1)

    if not loaded.triggers:
        typer.echo("No triggers defined in spec — nothing to watch.", err=True)
        raise typer.Exit(1)

    if tune:
        # Skeleton: tune daemon would periodically improve the spec from traces.
        typer.echo("Tune daemon mode is a skeleton — full implementation pending.")
        raise typer.Exit(0)

    async def _run_fn(payload: dict) -> None:
        harness = Harness(spec=loaded, traces_db=traces)
        result = await harness.run({"trigger_payload": payload})
        if not quiet:
            typer.echo(f"Run complete: {list(result.keys())}")

    dispatcher = TriggerDispatcher()
    typer.echo(f"Watching {len(loaded.triggers)} trigger(s). Press Ctrl-C to stop.")
    try:
        asyncio.run(dispatcher.run_forever(loaded, _run_fn, host=host, port=port))
    except KeyboardInterrupt:
        typer.echo("Watch stopped.")


@app.command()
def doctor(
    spec: Path = typer.Option(None, "--spec", help="Optional spec file to validate"),
):
    """Check environment health: packages, env vars, and optional spec validity."""
    import importlib
    import os

    all_ok = True

    # ── Package checks ───────────────────────────────────────────────────────
    required_packages = ["litellm", "pydantic", "jinja2", "ruamel.yaml", "aiosqlite", "typer"]
    optional_packages = {
        "sentence_transformers": "embeddings",
        "fastapi": "service",
        "questionary": "wizard",
        "opentelemetry": "telemetry",
        "langfuse": "langfuse",
        "langsmith": "langsmith",
        "mcp": "mcp",
    }

    typer.echo("Packages:")
    for pkg in required_packages:
        mod = pkg.replace("-", "_")
        try:
            importlib.import_module(mod)
            typer.echo(f"  ✓ {pkg}")
        except ImportError:
            typer.echo(f"  ✗ {pkg} (MISSING — required)", err=True)
            all_ok = False

    for mod, group in optional_packages.items():
        try:
            importlib.import_module(mod)
            typer.echo(f"  ✓ {mod} (optional: {group})")
        except ImportError:
            typer.echo(f"  - {mod} (optional: {group} — not installed)")

    # ── Env var checks ───────────────────────────────────────────────────────
    typer.echo("\nEnv vars:")
    api_keys = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"]
    any_key = any(os.environ.get(k) for k in api_keys)
    for key in api_keys:
        val = os.environ.get(key)
        status = "✓" if val else "-"
        typer.echo(f"  {status} {key}")
    if not any_key:
        typer.echo("  ! No LLM API key set — LLM stages will fail", err=True)

    # ── DB directory ─────────────────────────────────────────────────────────
    typer.echo("\nData paths:")
    db_dir = Path("~/.armature").expanduser()
    if db_dir.exists():
        typer.echo(f"  ✓ {db_dir}")
    else:
        typer.echo(f"  - {db_dir} (will be created on first run)")

    # ── Spec validation (optional) ───────────────────────────────────────────
    if spec is not None:
        typer.echo(f"\nSpec: {spec}")
        if not spec.exists():
            typer.echo(f"  ✗ File not found: {spec}", err=True)
            all_ok = False
        else:
            from armature.spec.loader import load_spec
            from armature.spec.validator import validate_spec
            try:
                loaded = load_spec(spec)
            except Exception as exc:
                typer.echo(f"  ✗ Parse error: {exc}", err=True)
                all_ok = False
            else:
                errors = validate_spec(loaded, strict=False)
                if errors:
                    for e in errors:
                        typer.echo(f"  ✗ [{e.code}]: {e.message}", err=True)
                    all_ok = False
                else:
                    typer.echo(f"  ✓ '{loaded.name}' is valid ({len(loaded.stages)} stages)")

    if not all_ok:
        raise typer.Exit(1)


@app.command()
def last(
    spec: Path = typer.Argument(..., help="Path to workflow spec YAML"),
    stage: str = typer.Option(None, "--stage", "-s", help="Stage to show (default: primary output stage)"),
    full: bool = typer.Option(False, "--full", "-f", help="Show complete output without truncation"),
):
    """Show the primary output from the most recent run of a workflow."""
    from armature.spec.loader import load_spec
    try:
        loaded = load_spec(spec)
    except Exception as exc:
        typer.echo(f"Failed to load spec: {exc}", err=True)
        raise typer.Exit(1)

    save_path = Path.home() / ".armature" / "last" / f"{loaded.name}.json"
    if not save_path.exists():
        typer.echo(
            f"No saved result for '{loaded.name}'.\n"
            f"Run the workflow first:  armature run {spec} --input ...",
            err=True,
        )
        raise typer.Exit(1)

    result = json.loads(save_path.read_text())
    target_id = stage or _find_primary_stage(loaded)
    if not target_id or target_id not in result:
        typer.echo(f"Stage '{target_id}' not found in last run result.", err=True)
        raise typer.Exit(1)

    stage_out = result[target_id]
    if isinstance(stage_out, dict) and "content" in stage_out:
        text = str(stage_out["content"]).strip()
    elif isinstance(stage_out, str):
        text = stage_out.strip()
    elif isinstance(stage_out, (list, dict)):
        text = json.dumps(stage_out, indent=2, default=str)
    else:
        text = str(stage_out).strip()

    limit = 2000
    if not full and len(text) > limit:
        display = text[:limit] + f"\n\n[#888888]… {len(text) - limit} more characters — use --full to see all[/#888888]"
    else:
        display = text

    from rich.console import Console
    from rich.panel import Panel
    Console(highlight=False).print(Panel(
        display,
        title=f"[bold]{target_id}[/bold]  [#888888]·[/#888888]  [#888888]{loaded.name}[/#888888]",
        border_style="#888888",
        padding=(0, 1),
    ))


@app.command()
def explain(
    spec: Path = typer.Argument(..., help="Path to workflow spec YAML"),
):
    """Explain what a workflow does in plain English using an LLM."""
    if not spec.exists():
        typer.echo(f"Spec file not found: {spec}", err=True)
        raise typer.Exit(1)

    from armature.spec.loader import load_spec
    try:
        loaded = load_spec(spec)
    except Exception as exc:
        typer.echo(f"Failed to load spec: {exc}", err=True)
        raise typer.Exit(1)

    # Pick the smallest available model to keep cost low
    tier_names = ["small", "tiny", "medium", "large", "frontier"]
    model_cfg = None
    for t in tier_names:
        cfg = getattr(loaded.model_tiers, t, None)
        if cfg:
            model_cfg = cfg
            break
    if not model_cfg and loaded.model_tiers.__pydantic_extra__:
        model_cfg = next(iter(loaded.model_tiers.__pydantic_extra__.values()), None)

    if not model_cfg:
        typer.echo("No model tier configured in spec.", err=True)
        raise typer.Exit(1)

    inputs = [i.get("name", str(i)) for i in (loaded.contracts.inputs if loaded.contracts else [])]
    stage_lines = []
    for s in loaded.stages:
        if s.role:
            desc = (s.role.description or "").strip().replace("\n", " ")[:100]
            stage_lines.append(f"  - {s.id} ({s.role.type.value}): {s.role.name} — {desc}")
        elif s.tool_call:
            stage_lines.append(f"  - {s.id}: calls tool {s.tool_call.name}" + (f" ×{s.fan_out} parallel" if s.fan_out else ""))
        else:
            stage_lines.append(f"  - {s.id}: {s.id}")

    prompt = (
        f"You are explaining an Armature multi-agent workflow to a new user.\n\n"
        f"Workflow: {loaded.name}\n"
        f"Description: {loaded.description or '(none)'}\n"
        f"Mission: {(loaded.mission or '(none)').strip()[:300]}\n"
        f"Inputs required: {', '.join(inputs) or 'none'}\n\n"
        f"Stages:\n{chr(10).join(stage_lines)}\n\n"
        f"Write a friendly plain-English explanation:\n"
        f"1. One sentence — what does this workflow accomplish?\n"
        f"2. What does the user need to provide as input?\n"
        f"3. Walk through each stage in one sentence — what does it do and why?\n"
        f"4. What does the user get at the end?\n\n"
        f"Be concrete, jargon-free, and use the actual stage and agent names."
    )

    import litellm
    import os

    api_key_env = getattr(model_cfg, "api_key_env", None)
    api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    litellm_model = f"{model_cfg.provider}/{model_cfg.model}" if model_cfg.provider else model_cfg.model

    from rich.console import Console
    from rich.panel import Panel
    console = Console(highlight=False)
    short_model = model_cfg.model.split("/")[-1]
    console.print(f"\n  [#888888]Explaining[/#888888] [bold]{loaded.name}[/bold] [#888888]via {short_model}…[/#888888]")

    try:
        response = litellm.completion(
            model=litellm_model,
            messages=[{"role": "user", "content": prompt}],
            api_key=api_key or None,
            max_tokens=1000,
            temperature=0.3,
        )
        explanation = response.choices[0].message.content.strip()
    except Exception as exc:
        typer.echo(f"LLM call failed: {exc}", err=True)
        raise typer.Exit(1)

    console.print()
    console.print(Panel(
        explanation,
        title=f"[bold]{loaded.name}[/bold]  [#888888]explained[/#888888]",
        border_style="#888888",
        padding=(0, 1),
    ))


@channels_app.command("start")
def channels_start(
    spec_file: Path = typer.Argument(..., help="Path to channel spec YAML"),
):
    """Start channel connectors from a spec file."""
    if not spec_file.exists():
        typer.echo(f"Channel spec not found: {spec_file}", err=True)
        raise typer.Exit(1)

    from ruamel.yaml import YAML
    from armature.channels.models import ChannelSpec
    import pydantic

    yaml_parser = YAML()
    try:
        with open(spec_file) as fh:
            data = dict(yaml_parser.load(fh))
        spec = ChannelSpec.model_validate(data)
    except pydantic.ValidationError as exc:
        typer.echo(f"Invalid channel spec: {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"Failed to load spec: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Channel spec '{spec.name}' loaded — {len(spec.channels)} channel(s).")
    typer.echo("(Live channel server not yet implemented — spec validated successfully.)")


@app.command()
def demo() -> None:
    """Run the live quality witness — test suite + capability overview for new developers."""
    from armature.demo import run_demo
    run_demo()


@app.command()
def dashboard(
    spec: Path = typer.Argument(None, help="Workflow spec YAML (extracts workflow name)"),
    workflow: str = typer.Option(None, "--workflow", "-w", help="Workflow name (alternative to spec path)"),
    traces_db: Path = typer.Option(None, "--traces", help="Path to traces.db (default: ~/.armature/traces.db)"),
    improve_log: Path = typer.Option(None, "--log", help="Path to improvement log JSONL"),
    last: int = typer.Option(200, "--last", help="Number of most recent traces to aggregate"),
    watch: bool = typer.Option(False, "--watch", is_flag=True, help="Auto-refresh every --interval seconds"),
    interval: float = typer.Option(5.0, "--interval", help="Refresh interval in seconds for --watch mode"),
    format: str = typer.Option("terminal", "--format", "-f", help="Output format: terminal | json"),
):
    """Show a Rich multi-panel workflow health dashboard aggregated across runs."""
    try:
        from rich.console import Console
    except ImportError:
        typer.echo("Rich is required for the dashboard. Install it: pip install rich", err=True)
        raise typer.Exit(1)

    from armature.report.loader import load_dashboard_data
    from armature.report.layout import render_terminal, render_terminal_watch, render_json

    # Resolve workflow name
    wf_name = workflow
    if wf_name is None and spec is not None:
        if not spec.exists():
            typer.echo(f"Spec not found: {spec}", err=True)
            raise typer.Exit(1)
        from armature.spec.loader import load_spec
        try:
            loaded = load_spec(spec)
            wf_name = loaded.name
        except Exception as exc:
            typer.echo(f"Could not parse spec: {exc}", err=True)
            raise typer.Exit(1)

    if wf_name is None:
        typer.echo("Provide a spec path or --workflow <name>.", err=True)
        raise typer.Exit(1)

    db = traces_db or Path("~/.armature/traces.db").expanduser()

    async def _load():
        return await load_dashboard_data(
            wf_name,
            traces_db=db,
            improve_log=improve_log,
            last_n=last,
        )

    if format == "json":
        data = asyncio.run(_load())
        import json as _json
        typer.echo(_json.dumps(render_json(data), indent=2))
        return

    console = Console()

    if watch:
        def _loader():
            return asyncio.run(_load())
        render_terminal_watch(_loader, interval=interval, console=console)
    else:
        data = asyncio.run(_load())
        render_terminal(data, console=console)


adapter_app = typer.Typer(name="adapter", help="Create and manage LoRA adapters")
app.add_typer(adapter_app, name="adapter")


@adapter_app.command("create")
def adapter_create(
    spec: Path = typer.Option(..., "--spec", help="Path to workflow spec YAML"),
    skill: str | None = typer.Option(None, "--skill", "-s", help="Skill ID to convert to an adapter"),
    traces: Path | None = typer.Option(None, "--traces", "-t", help="Path to exported trace JSONL"),
    name: str | None = typer.Option(None, "--name", "-n", help="Adapter name (defaults to skill ID or traces stem)"),
    backend: str | None = typer.Option(None, "--backend", "-b", help="Adapter backend to use"),
    registry_dir: Path | None = typer.Option(None, "--registry", help="Override adapter registry directory"),
    role_type: str | None = typer.Option(None, "--role-type", help="Filter trace examples by role type"),
    stage_id: str | None = typer.Option(None, "--stage-id", help="Filter trace examples by stage ID"),
):
    """Create a LoRA adapter from a skill document or exported traces."""
    from armature.adapters.backends.mock import MockAdapterFactory
    from armature.adapters.backends.s2l import S2LSkillAdapterFactory
    from armature.adapters.backends.trace import TraceAdapterFactory
    from armature.adapters.factory import AdapterRequest
    from armature.adapters.registry import AdapterRegistry
    from armature.spec.loader import load_spec

    if not spec.exists():
        typer.echo(f"Spec file not found: {spec}", err=True)
        raise typer.Exit(code=1)

    if skill is None and traces is None:
        typer.echo("Either --skill or --traces is required", err=True)
        raise typer.Exit(code=1)
    if skill is not None and traces is not None:
        typer.echo("Use only one of --skill or --traces", err=True)
        raise typer.Exit(code=1)

    harness_spec = load_spec(spec)
    factory_cfg = harness_spec.adapter_factory
    chosen_backend = backend or (factory_cfg.backend if factory_cfg else "mock")
    base_model = _resolve_adapter_base_model(harness_spec)
    rank = factory_cfg.rank if factory_cfg else 16
    alpha = factory_cfg.alpha if factory_cfg else 32
    target_modules = list(factory_cfg.target_modules if factory_cfg else ["q_proj", "v_proj"])
    use_dora = factory_cfg.use_dora if factory_cfg else False
    cl_cfg = factory_cfg.continual_learning if factory_cfg else None
    continual_learning = cl_cfg.enabled if cl_cfg else False
    prior_adapter_version = cl_cfg.prior_version if cl_cfg else None

    registry = AdapterRegistry(base_dir=registry_dir) if registry_dir else AdapterRegistry()
    if chosen_backend == "mock":
        factory = MockAdapterFactory(registry=registry)
    elif chosen_backend == "s2l":
        factory = S2LSkillAdapterFactory(registry=registry)
    elif chosen_backend == "trace":
        factory = TraceAdapterFactory(registry=registry)
    else:
        typer.echo(f"Unsupported adapter backend '{chosen_backend}'", err=True)
        raise typer.Exit(code=1)

    if skill is not None:
        if skill not in harness_spec.skill_library:
            typer.echo(f"Skill '{skill}' not found in spec.skill_library", err=True)
            raise typer.Exit(code=1)
        skill_def = harness_spec.skill_library[skill]
        request = AdapterRequest(
            name=name or skill,
            base_model=base_model,
            skill=skill_def,
            rank=rank,
            alpha=alpha,
            target_modules=target_modules,
            use_dora=use_dora,
            continual_learning=continual_learning,
            prior_adapter_version=prior_adapter_version,
        )
    else:
        if not traces.exists():
            typer.echo(f"Traces file not found: {traces}", err=True)
            raise typer.Exit(code=1)
        extra = {}
        if role_type:
            extra["role_type"] = role_type
        if stage_id:
            extra["stage_id"] = stage_id
        request = AdapterRequest(
            name=name or traces.stem,
            base_model=base_model,
            traces_path=traces,
            rank=rank,
            alpha=alpha,
            target_modules=target_modules,
            use_dora=use_dora,
            continual_learning=continual_learning,
            prior_adapter_version=prior_adapter_version,
            extra=extra,
        )

    try:
        job = asyncio.run(_poll_adapter_job(factory, request))
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"Created adapter {job.metadata.name}@{job.metadata.version} "
        f"at {job.artifact_path}"
    )


async def _poll_adapter_job(factory, request):
    job = await factory.submit(request)
    while job.status not in ("done", "failed"):
        await asyncio.sleep(0.05)
        job = await factory.poll(job)
    if job.status == "failed":
        raise RuntimeError("Adapter creation failed:\n" + "\n".join(job.logs))
    return job


def _resolve_adapter_base_model(harness_spec) -> str:
    if harness_spec.adapter_factory and harness_spec.adapter_factory.base_model:
        return harness_spec.adapter_factory.base_model
    for tier_name in ("small", "medium", "large", "frontier", "tiny"):
        tier_cfg = getattr(harness_spec.model_tiers, tier_name, None)
        if tier_cfg is not None and tier_cfg.model:
            return tier_cfg.model
    raise typer.BadParameter("No adapter_factory.base_model configured and no model tiers defined")


@adapter_app.command("list")
def adapter_list(
    name: str | None = typer.Option(None, "--name", "-n", help="Filter by adapter name"),
    registry_dir: Path | None = typer.Option(None, "--registry", help="Override adapter registry directory"),
):
    """List registered adapters and their versions."""
    from armature.adapters.registry import AdapterRegistry

    registry = AdapterRegistry(base_dir=registry_dir) if registry_dir else AdapterRegistry()
    rows = list(registry.list(name))
    if not rows:
        typer.echo("No adapters found.")
        return
    for metadata, artifact_dir in rows:
        typer.echo(f"{metadata.name}@{metadata.version}  {metadata.base_model}  {artifact_dir}")


@adapter_app.command("register")
def adapter_register(
    name: str = typer.Argument(..., help="Adapter name"),
    version: str = typer.Argument(..., help="Adapter version"),
    path: Path = typer.Argument(..., help="Path to adapter artifact directory"),
    base_model: str = typer.Option(..., "--base-model", help="Base model the adapter was trained on"),
    registry_dir: Path | None = typer.Option(None, "--registry", help="Override adapter registry directory"),
    rank: int = typer.Option(16, "--rank", help="LoRA rank"),
    alpha: int = typer.Option(32, "--alpha", help="LoRA alpha"),
    backend: str = typer.Option("manual", "--backend", help="Backend that produced the adapter"),
):
    """Register a pre-trained adapter artifact in the local registry."""
    from armature.adapters.manifest import AdapterMetadata
    from armature.adapters.registry import AdapterRegistry

    if not path.exists() or not path.is_dir():
        typer.echo(f"Artifact directory not found: {path}", err=True)
        raise typer.Exit(code=1)

    metadata = AdapterMetadata(
        name=name,
        version=version,
        base_model=base_model,
        rank=rank,
        alpha=alpha,
        backend=backend,
    )
    registry = AdapterRegistry(base_dir=registry_dir) if registry_dir else AdapterRegistry()
    registry.register(metadata, path)
    typer.echo(f"Registered {name}@{version}")


@adapter_app.command("promote")
def adapter_promote(
    name: str = typer.Argument(..., help="Adapter name"),
    version: str = typer.Argument(..., help="Version to promote to latest"),
    registry_dir: Path | None = typer.Option(None, "--registry", help="Override adapter registry directory"),
):
    """Promote an adapter version to `latest`."""
    from armature.adapters.registry import AdapterRegistry

    registry = AdapterRegistry(base_dir=registry_dir) if registry_dir else AdapterRegistry()
    try:
        registry.promote(name, version)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Promoted {name}@{version} to latest")


@adapter_app.command("merge")
def adapter_merge(
    refs: list[str] = typer.Argument(..., help="Source adapters as name@version"),
    name: str = typer.Option(..., "--name", "-n", help="Name for the merged adapter"),
    base_model: str | None = typer.Option(None, "--base-model", help="Base model (defaults to first source adapter's base model)"),
    rank: int = typer.Option(16, "--rank", help="LoRA rank"),
    alpha: int = typer.Option(32, "--alpha", help="LoRA alpha"),
    registry_dir: Path | None = typer.Option(None, "--registry", help="Override adapter registry directory"),
):
    """Merge multiple registered adapters into a single artifact."""
    from armature.adapters.backends.merge import MergedAdapterFactory
    from armature.adapters.factory import AdapterRequest
    from armature.adapters.registry import AdapterRegistry

    registry = AdapterRegistry(base_dir=registry_dir) if registry_dir else AdapterRegistry()

    if len(refs) < 2:
        typer.echo("At least two source adapters are required", err=True)
        raise typer.Exit(code=1)

    resolved_base = base_model
    if resolved_base is None:
        first_name, first_version = refs[0].split("@", 1)
        resolved_base = registry.get(first_name, first_version).metadata.base_model

    factory = MergedAdapterFactory(registry=registry)
    request = AdapterRequest(
        name=name,
        base_model=resolved_base,
        rank=rank,
        alpha=alpha,
        target_modules=["q_proj", "v_proj"],
        use_dora=False,
        continual_learning=False,
        prior_adapter_version=None,
        extra={"adapter_refs": refs},
    )

    try:
        job = asyncio.run(_poll_adapter_job(factory, request))
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"Merged adapter {job.metadata.name}@{job.metadata.version} "
        f"at {job.artifact_path}"
    )


@adapter_app.command("eval")
def adapter_eval(
    name: str = typer.Argument(..., help="Adapter name"),
    spec: Path = typer.Argument(..., help="Path to workflow spec YAML"),
    version: str | None = typer.Option(None, "--version", "-v", help="Adapter version (defaults to latest)"),
    stage_id: str | None = typer.Option(None, "--stage-id", help="Stage to score (defaults to first judge/leaf)"),
    input_kv: list[str] = typer.Option([], "--input", help="Runtime inputs as key=value"),
    registry_dir: Path | None = typer.Option(None, "--registry", help="Override adapter registry directory"),
):
    """Evaluate an adapter by comparing workflow runs with and without it."""
    from armature.adapters.eval import evaluate_adapter
    from armature.adapters.registry import AdapterRegistry

    if not spec.exists():
        typer.echo(f"Spec file not found: {spec}", err=True)
        raise typer.Exit(code=1)

    registry = AdapterRegistry(base_dir=registry_dir) if registry_dir else AdapterRegistry()
    inputs = parse_inputs(input_kv)

    try:
        result = asyncio.run(evaluate_adapter(registry, name, version, spec, inputs, stage_id))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"Evaluated {result.adapter_name}@{result.adapter_version}: "
        f"with={result.with_adapter_score}, without={result.without_adapter_score}, "
        f"delta={result.delta}"
    )


if __name__ == "__main__":
    app()
