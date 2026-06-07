from pathlib import Path
import jinja2
from jinja2 import Environment, BaseLoader
from ruamel.yaml import YAML
from armature.spec.models import HarnessSpec


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

    return HarnessSpec.model_validate(data)
