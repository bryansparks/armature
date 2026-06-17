import logging
from pathlib import Path
import jinja2
from jinja2 import Environment, BaseLoader
from ruamel.yaml import YAML
from armature.spec.models import CompiledAgent, HarnessSpec, SkillDef

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


def load_spec(path: Path | str, vars: dict | None = None) -> HarnessSpec:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Spec not found: {path}")

    raw = path.read_text(encoding="utf-8")

    if vars:
        env = Environment(
            loader=BaseLoader(),
            variable_start_string="{{",
            variable_end_string="}}",
            undefined=_KeepUndefined,
        )
        template = env.from_string(raw)
        raw = template.render(**(vars or {}))

    yaml = YAML()
    yaml.preserve_quotes = True
    data = yaml.load(raw)

    spec = HarnessSpec.model_validate(data)
    _resolve_agent_references(spec, path.parent)
    return spec


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
        if stage.agent is None:
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
