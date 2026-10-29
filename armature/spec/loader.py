import copy
import logging
from pathlib import Path
from typing import Any, Literal
import jinja2
from jinja2.sandbox import ImmutableSandboxedEnvironment
from pydantic import ValidationError
from ruamel.yaml import YAML, YAMLError
from armature.runtime.templating import text_env
from armature.spec.contract_io import is_typed, resolve_inputs
from armature.spec.models import CompiledAgent, HarnessSpec, SkillDef, ToolSafetyRule

_log = logging.getLogger(__name__)


class _KeepUndefined(jinja2.Undefined):
    """Preserves {{ expr }} for variables not in vars (runtime context placeholders).

    When load_spec renders the YAML as a Jinja2 template to substitute user-provided
    vars (e.g. {{ topic }}), any variable NOT in vars must be left untouched — it is
    a runtime expression that the engine will evaluate later (e.g. {{ planner.items }}).

    Without this, ChainableUndefined silently renders runtime expressions to '' and
    NativeEnvironment.from_string('').render() returns None, breaking fan-out stages.
    """

    def __str__(self) -> str:
        return "{{ " + (self._undefined_name or "") + " }}"

    def __getattr__(self, name: str) -> "_KeepUndefined":
        if name.startswith("_"):
            raise AttributeError(name)
        return _KeepUndefined(name=f"{self._undefined_name}.{name}")

    def __getitem__(self, key: object) -> "_KeepUndefined":
        return _KeepUndefined(name=f"{self._undefined_name}[{key!r}]")

    def __bool__(self) -> bool:
        return False  # {% if undefined_var %} blocks are skipped (falsy = not provided)

    def __iter__(self):
        return iter([])

    def __len__(self) -> int:
        return 0


def _template_env() -> ImmutableSandboxedEnvironment:
    """The shared load-time Jinja2 environment: preserves {{ expr }} for any
    variable not in vars (_KeepUndefined) so runtime placeholders survive."""
    return text_env(_KeepUndefined)


STORED_WORKFLOW_HEADER = "# armature: stored-workflow"
_MARKERS = ("{{", "{%", "{#")


def _scalar_markers(data: Any, counts: dict[str, int]) -> None:
    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(key, str) and any(m in key for m in _MARKERS):
                raise ValueError(f"TEMPLATE_OUTSIDE_SCALAR: mapping key {key!r} holds a template")
            _scalar_markers(val, counts)
    elif isinstance(data, list):
        for val in data:
            _scalar_markers(val, counts)
    elif isinstance(data, str):
        for marker in _MARKERS:
            counts[marker] += data.count(marker)


def _load_stored(raw: str, vars: dict | None) -> Any:
    """Parse first, then render vars into string scalars only, so no template can become YAML structure."""
    yaml = YAML()
    yaml.preserve_quotes = True
    data = yaml.load(raw)
    counts = dict.fromkeys(_MARKERS, 0)
    _scalar_markers(data, counts)
    for marker in _MARKERS:
        if raw.count(marker) > counts[marker]:
            raise ValueError(
                f"TEMPLATE_OUTSIDE_SCALAR: a stored workflow may hold {marker!r} only inside string values, "
                "never in a comment, key, or YAML syntax"
            )
    if vars:
        _render_scalars_once(data, vars)
    return data


def _declared_inputs(raw: str, render: str) -> list[dict]:
    """The spec's contracts.inputs entries, read before any input renders into it; [] when unreadable."""
    try:
        text = raw if render == "scalars" else _template_env().from_string(raw).render()
        data = YAML(typ="safe").load(text)
    except Exception:
        return []
    contracts = data.get("contracts") if isinstance(data, dict) else None
    inputs = contracts.get("inputs") if isinstance(contracts, dict) else None
    return [i for i in inputs if isinstance(i, dict)] if isinstance(inputs, list) else []


def _preparse(raw: str) -> Any:
    """The spec tree before any input renders into it: the raw YAML when it parses, else None."""
    try:
        return YAML(typ="safe").load(raw)
    except Exception:
        return None


def _input_free_tree(raw: str) -> Any:
    """A text spec rendered with no inputs and parsed, or None when either step fails."""
    try:
        return YAML(typ="safe").load(_template_env().from_string(raw).render())
    except Exception:
        return None


def _input_free_call_targets(raw: str) -> list[tuple] | None:
    """Call targets of a text spec rendered with no inputs, to tell whether an input chooses one."""
    tree = _input_free_tree(raw)
    return _call_targets(tree) if tree is not None else None


def _stage_maps(data: Any) -> list[dict]:
    stages = data.get("stages") if isinstance(data, dict) else None
    return [s for s in stages if isinstance(s, dict)] if isinstance(stages, list) else []


def _call_targets(data: Any) -> list[tuple]:
    return [(s.get("id"), s["call"].get("workflow"), s["call"].get("version"))
            for s in _stage_maps(data) if isinstance(s.get("call"), dict)]


def _reject_templated_call_targets(data: Any) -> None:
    for stage_id, workflow, version in _call_targets(data):
        if any(isinstance(v, str) and any(m in v for m in _MARKERS) for v in (workflow, version)):
            raise ValueError(
                f"CALL_INVALID_WORKFLOW: stage '{stage_id}' call target holds a template; "
                "a call names a fixed workflow and version"
            )


def _with_blocks(data: Any) -> dict[Any, Any]:
    """Each stage's with: block and each agent: mapping, which only the run-time render may render."""
    blocks = {}
    for s in _stage_maps(data):
        if "with" in s:
            blocks[(s.get("id"), "with")] = copy.deepcopy(s["with"])
        if isinstance(s.get("agent"), dict):
            blocks[(s.get("id"), "agent")] = copy.deepcopy(s["agent"])
    return blocks


def _restore_with_blocks(data: Any, blocks: dict[Any, Any]) -> None:
    for stage in _stage_maps(data):
        for (stage_id, key), block in blocks.items():
            if stage.get("id") == stage_id:
                stage[key] = copy.deepcopy(block)


def _adapter_maps(data: Any) -> dict[Any, dict]:
    adapters = data.get("adapters") if isinstance(data, dict) else None
    return {k: a for k, a in adapters.items() if isinstance(a, dict)} if isinstance(adapters, dict) else {}


def _adapter_cmds(data: Any) -> dict[Any, Any]:
    return {name: a.get("cmd") for name, a in _adapter_maps(data).items()}


_RUNTIME_RENDERED = ("skip_if", "condition", "partition_source", "present")


def _render_scalars_once(data: Any, vars: dict) -> None:
    """Render vars into string scalars, except the fields the run time renders, so an input renders once."""
    spots = []
    for s in _stage_maps(data):
        on_fail = s.get("on_fail")
        spots += [(s, key) for key in _RUNTIME_RENDERED]
        spots += [(s.get("role"), "description"), (s.get("tool_call"), "args"), (s.get("loop"), "until"),
                  (on_fail.get("loop") if isinstance(on_fail, dict) else None, "until")]
    contracts = data.get("contracts") if isinstance(data, dict) else None
    outputs = contracts.get("outputs") if isinstance(contracts, dict) else None
    spots += [(o, "from") for o in outputs] if isinstance(outputs, list) else []
    spots += [(a, "cmd") for a in _adapter_maps(data).values()]
    held = [(m, key, m[key]) for m, key in spots if isinstance(m, dict) and key in m]
    for mapping, key, _ in held:
        mapping[key] = None
    _render_string_scalars(data, _template_env(), vars)
    for mapping, key, value in held:
        mapping[key] = value


def load_spec(path: Path | str, vars: dict | None = None, *,
              render: Literal["text", "scalars"] | None = None) -> HarnessSpec:
    """Load a spec, rendering ``vars`` into it.

    ``render="text"`` renders the whole file before parsing, as plain specs
    always have. ``render="scalars"``, or a first line of exactly
    ``# armature: stored-workflow``, parses first and renders string values only.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Spec not found: {path}")

    raw = path.read_text(encoding="utf-8")
    if render is None:
        render = "scalars" if raw.split("\n", 1)[0].rstrip("\r") == STORED_WORKFLOW_HEADER else "text"

    decls = _declared_inputs(raw, render) if "inputs" in raw else []
    if any(is_typed(d) for d in decls):
        vars = resolve_inputs(decls, vars or {}, enforce_required=False, scalars_only=True)

    pre = _preparse(raw)
    _reject_templated_call_targets(pre)
    before = _call_targets(pre) if pre is not None else (
        _input_free_call_targets(raw) if render == "text" and "call:" in raw else None)
    free = _input_free_tree(raw) if render == "text" and vars else None
    if render == "text" and vars and free is None and "cmd" in raw:
        raise ValueError("ADAPTER_CMD_INPUT: the spec does not parse with inputs left out, "
                         "so its adapter cmds cannot be checked for inputs")

    if render == "scalars":
        data = _load_stored(raw, vars)
    else:
        if vars:
            raw = _template_env().from_string(raw).render(**vars)
        yaml = YAML()
        yaml.preserve_quotes = True
        data = yaml.load(raw)

    if vars:
        # Input values render into the spec once at load; a with: renders at run
        # time instead, so an input holding "{{ ... }}" is never rendered twice.
        if pre is not None:
            _restore_with_blocks(data, _with_blocks(pre))
        if before is not None and _call_targets(data) != before:
            raise ValueError("CALL_INVALID_WORKFLOW: a call target may not depend on inputs")
        if free is not None and _adapter_cmds(data) != _adapter_cmds(free):
            raise ValueError("ADAPTER_CMD_INPUT: an adapter or its cmd may not depend on inputs; "
                             "read values from ARMATURE_WITH or ARMATURE_CONTEXT instead")

    spec = HarnessSpec.model_validate(data)
    _resolve_pack_adapters(spec)
    _resolve_context_layers(spec, path.parent)
    _resolve_agent_references(spec, path.parent)
    _resolve_subagent_specs(spec, path.parent)
    spec._source_dir = str(path.parent.resolve())
    return spec


def _render_string_scalars(data: Any, env: ImmutableSandboxedEnvironment, vars: dict) -> None:
    """Render carried context into string scalars of an already-parsed spec tree.

    Values only — dict keys and YAML structure are untouched, so a multiline or
    otherwise hostile carried value can never alter the spec's shape. Scalars
    without template markers skip rendering entirely (fast path, and keeps
    plain strings byte-identical).
    """
    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, str):
                if "{{" in val or "{%" in val:
                    data[key] = env.from_string(val).render(**vars)
            else:
                _render_string_scalars(val, env, vars)
    elif isinstance(data, list):
        for i, val in enumerate(data):
            if isinstance(val, str):
                if "{{" in val or "{%" in val:
                    data[i] = env.from_string(val).render(**vars)
            else:
                _render_string_scalars(val, env, vars)


def load_child_spec(path: Path | str, vars: dict | None = None) -> HarnessSpec:
    """Load a subagent child spec: parse with templates inert, then render
    carried context into string scalars only.

    load_spec renders the whole YAML text before parsing — fine for author-time
    inputs. A child spec's vars are the parent's RUNTIME context: rich, often
    multiline stage outputs. Rendering those into raw YAML text before parsing
    lets a value alter the YAML structure — observed live on 2026-09-21: a
    {{ }} template in a COMMENT spliced multiline carried content past the '#'
    and broke the parse on loop iteration 2 (research-round.yaml line 372).

    Child specs must therefore parse with templates inert. Structural
    templating from carried context (a template emitting YAML structure) is
    unsupported and fails loudly here rather than mangling silently. Runtime
    placeholders ({{ upstream.key }} the engine renders per-stage) survive via
    _KeepUndefined, same as load_spec.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Spec not found: {path}")
    return load_child_spec_text(path.read_text(encoding="utf-8"), path.parent, path.name, vars)


def load_child_spec_text(raw: str, base_dir: Path, label: str, vars: dict | None = None) -> HarnessSpec:
    """load_child_spec for spec text already in hand, such as a called workflow from a call bundle."""
    yaml = YAML()
    yaml.preserve_quotes = True
    try:
        data = yaml.load(raw)
    except YAMLError as exc:
        raise ValueError(
            f"child spec '{label}' does not parse with templates inert — "
            f"carried context must not be able to alter YAML structure; "
            f"rewrite the spec so it is valid YAML before rendering: {exc}"
        ) from exc

    _reject_templated_call_targets(data)
    if vars:
        blocks = _with_blocks(data)
        _render_scalars_once(data, vars)
        _restore_with_blocks(data, blocks)

    try:
        spec = HarnessSpec.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"child spec '{label}' is invalid with templates inert — "
            f"structural templating from carried context is not supported for "
            f"child specs (render values inside string fields instead): {exc}"
        ) from exc
    _resolve_pack_adapters(spec)
    _resolve_context_layers(spec, base_dir)
    _resolve_agent_references(spec, base_dir)
    _resolve_subagent_specs(spec, base_dir)
    spec._source_dir = str(Path(base_dir).resolve())
    return spec


def _resolve_pack_adapters(spec: HarnessSpec, catalog=None) -> None:
    """Add each installed pack adapter a stage names as ``pack/adapter`` to spec.adapters.

    An adapter the spec defines itself wins. A ref no pack provides is left for
    validate_spec to report as UNKNOWN_PACK_ADAPTER.
    """
    refs = sorted({s.adapter for s in spec.stages
                   if s.adapter and "/" in s.adapter and s.adapter not in spec.adapters})
    if not refs:
        return
    if catalog is None:
        from armature.packs import catalog as load_catalog
        catalog = load_catalog()
    for ref in refs:
        found = catalog.resolve(ref)
        if found is not None:
            pack, adapter = found
            spec.adapters[ref] = adapter.to_adapter(ref, pack.dir)


def resolve_spec_ref(ref: str, base_dir: Path) -> Path | None:
    """Resolve a spec file reference: absolute as-is; otherwise spec-dir
    first, process cwd as fallback.

    Returns the first existing candidate, or None when no candidate exists —
    callers decide whether that's fatal (the loader leaves the ref unstamped
    so runtime surfaces its usual error; the package builder fails the build).
    """
    p = Path(ref)
    if p.is_absolute():
        return p if p.exists() else None
    for cand in (base_dir / ref, Path.cwd() / ref):
        if cand.exists():
            return cand
    return None


def _resolve_subagent_specs(spec: HarnessSpec, base_dir: Path) -> None:
    """Stamp each subagent stage with the resolved child-spec path.

    Unlike context-layer src: and agent_library paths (spec-dir only), a
    subagent_spec historically resolved against process cwd at run time, so
    resolution keeps cwd as a fallback for back-compat. Stamping at load time
    — when the spec's own directory is known — makes child workflows findable
    regardless of cwd (packaged runs, `armature run` from another directory).
    The ref itself is preserved as written; unresolvable or still-templated
    refs are left unstamped and fail at run time as before.
    """
    for stage in spec.stages:
        ref = stage.subagent_spec
        if ref is None or stage.subagent_spec_path is not None:
            continue
        if "{{" in ref:
            continue
        resolved = resolve_spec_ref(ref, base_dir)
        if resolved is not None:
            stage.subagent_spec_path = str(resolved.resolve())


def _cond_key(rule: ToolSafetyRule) -> tuple:
    """Identity for dedup: (tool, condition). None condition = unconditional.

    model_dump_json() gives a hashable, deterministic string so conditional
    rules can live in a set (model_dump() returns a dict, which is unhashable).
    """
    cond = rule.condition.model_dump_json() if rule.condition else None
    return (rule.tool, cond)


def _merge_agent_safety(spec: HarnessSpec, bundle: CompiledAgent) -> None:
    """Merge the bundle's block rules into spec.safety_rules.

    Approach 2 (agent block rules are a non-overridable floor):
      - workflow `allow` on a blocked tool is dropped (it would short-circuit
        past the block; safety evaluation is first-match-wins in list order).
      - agent block rules are prepended so they fire first.
      - dedup by (tool, condition) so an existing unconditional workflow block
        isn't duplicated, and so multiple references to the same agent are
        idempotent. A condition-scoped workflow block coexists with the agent's
        unconditional block (the unconditional one dominates, ordered first).
    """
    agent_blocks = [r for r in bundle.safety_rules if r.action == "block"]
    if not agent_blocks:
        return
    blocked = {r.tool for r in agent_blocks}
    kept = [r for r in spec.safety_rules
            if not (r.tool in blocked and r.action == "allow")]
    existing = {_cond_key(r) for r in kept if r.action == "block"}
    new = [r for r in agent_blocks if _cond_key(r) not in existing]
    spec.safety_rules = new + kept


def _resolve_context_layers(spec: HarnessSpec, base_dir: Path) -> None:
    """Inline each layer's src: file into content (src kept for provenance).

    Fail closed at load time — a missing file is an authoring error,
    not a runtime surprise.
    """
    for layer in spec.context_layers:
        if layer.content is not None or layer.src is None:
            continue
        src_path = (base_dir / layer.src).resolve()
        if not src_path.is_relative_to(base_dir.resolve()):
            raise ValueError(
                f"ContextLayer '{layer.name}' src '{layer.src}' resolves outside the spec "
                f"directory {base_dir.resolve()} (SRC_PATH_ESCAPE) — refusing to read"
            )
        if not src_path.is_file():
            raise FileNotFoundError(
                f"SRC_FILE_NOT_FOUND: context layer '{layer.name}' src "
                f"'{layer.src}' not found (looked in {base_dir})"
            )
        layer.content = src_path.read_text(encoding="utf-8")


def _resolve_agent_references(spec: HarnessSpec, base_dir: Path) -> None:
    """Resolve agent_library references in each stage, merging role + skills in place.

    For every stage with `agent` set:
    1. Load the referenced CompiledAgent bundle from agent_library[agent].path.
    2. Merge the bundle's skill_library into spec.skill_library (existing keys win).
    3. Copy the bundle's role onto stage.role and clear stage.agent.

    Raises FileNotFoundError if a bundle path does not exist.
    Raises ValueError if a stage references an unknown agent_library key.
    """
    if not spec.agent_library:
        return

    yaml = YAML()
    yaml.preserve_quotes = True
    compiled: dict[str, CompiledAgent] = {}
    for agent_id, agent_ref in spec.agent_library.items():
        agent_path = (base_dir / agent_ref.path).resolve()
        if not agent_path.exists():
            raise FileNotFoundError(
                f"Agent bundle not found: {agent_path} "
                f"(agent_library['{agent_id}'].path = '{agent_ref.path}')"
            )
        data = yaml.load(agent_path.read_text(encoding="utf-8"))
        compiled[agent_id] = CompiledAgent.model_validate(data)

    for stage in spec.stages:
        if not isinstance(stage.agent, str):
            continue
        if stage.agent not in compiled:
            raise ValueError(
                f"Stage '{stage.id}' references unknown agent '{stage.agent}'; "
                f"defined agents: {sorted(compiled)}"
            )
        bundle = compiled[stage.agent]
        agent_dir = (base_dir / spec.agent_library[stage.agent].path).resolve().parent

        for skill_id, skill_def in bundle.skill_library.items():
            if skill_id in spec.skill_library:
                _log.warning(
                    "Skill '%s' from agent '%s' conflicts with an existing skill in "
                    "spec.skill_library; keeping the existing definition.",
                    skill_id, stage.agent,
                )
                continue
            if skill_def.path is not None:
                skill_def = SkillDef.model_validate(
                    {**skill_def.model_dump(), "path": str((agent_dir / skill_def.path).resolve())}
                )
            spec.skill_library[skill_id] = skill_def

        stage.role = bundle.role
        stage.agent = None
        _merge_agent_safety(spec, bundle)
