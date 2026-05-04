from pathlib import Path
from jinja2 import Environment, BaseLoader
from ruamel.yaml import YAML
from armature.spec.models import HarnessSpec


def load_spec(path: Path | str, vars: dict | None = None) -> HarnessSpec:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Spec not found: {path}")

    raw = path.read_text(encoding="utf-8")

    if vars:
        env = Environment(loader=BaseLoader(), variable_start_string="{{", variable_end_string="}}")
        template = env.from_string(raw)
        raw = template.render(**(vars or {}))

    yaml = YAML()
    yaml.preserve_quotes = True
    data = yaml.load(raw)

    return HarnessSpec.model_validate(data)
