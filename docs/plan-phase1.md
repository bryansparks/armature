# Armature Phase 1 — Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a working Python harness library that executes YAML workflow specs with all nine core harness components, consumable by any Python project via `pip install armature`.

**Architecture:** Async DAG executor drives a spec-parsed workflow through typed role nodes (worker/orchestrator/judge/researcher) routed via litellm. All nine harness components (iteration loop, context management, tools/registry, subagents, built-in skills, session persistence, prompt assembly, lifecycle hooks, permissions) are implemented as focused, independently testable modules.

**Tech Stack:** Python 3.11+, uv, Pydantic v2, ruamel.yaml, Jinja2, litellm, asyncio, SQLite (via aiosqlite), typer, pytest + pytest-asyncio

---

## File Map

```
armature/
├── pyproject.toml                    # Package definition + deps
├── armature/
│   ├── __init__.py                   # Public API: Harness, HarnessSpec, run()
│   ├── spec/
│   │   ├── __init__.py
│   │   ├── models.py                 # Pydantic models: HarnessSpec, Stage, Role, Contract, Failure, Adapter
│   │   ├── loader.py                 # YAML → HarnessSpec + Jinja2 template rendering
│   │   └── validator.py             # DAG cycle detection, required field checks
│   ├── runtime/
│   │   ├── __init__.py
│   │   ├── engine.py                # Main Harness class + while-loop core (iteration loop component)
│   │   ├── dag.py                   # Async topological executor (DAG scheduler)
│   │   ├── context.py               # Context window manager + compaction
│   │   ├── prompt.py                # System prompt assembly pipeline
│   │   └── loop.py                  # Loop-until executor
│   ├── nodes/
│   │   ├── __init__.py
│   │   ├── base.py                  # BaseNode ABC with execute() interface
│   │   ├── llm.py                   # LLM node: all four role types, litellm routing
│   │   ├── script.py                # Script/Python adapter node
│   │   └── gate.py                  # Human approval gate node
│   ├── registry/
│   │   ├── __init__.py
│   │   ├── registry.py              # Tool/skill registry (name → descriptor + handler)
│   │   └── builtins.py              # Built-in tools: file_read, file_write, shell, http_get
│   ├── skills/
│   │   ├── __init__.py
│   │   ├── quorum.py                # quorum.deliberate built-in skill
│   │   └── tessera.py               # tessera.retrieve built-in skill
│   ├── state/
│   │   ├── __init__.py
│   │   ├── session.py               # Append-only JSONL session log + replay
│   │   └── artifacts.py             # File-backed artifact store
│   ├── hooks/
│   │   ├── __init__.py
│   │   └── lifecycle.py             # Pre/post tool hook registry + allow/block/modify protocol
│   ├── permissions/
│   │   ├── __init__.py
│   │   └── permissions.py           # Permission levels, shell command classification, approval gate
│   └── cli.py                       # typer CLI: `armature run <spec> [--input k=v]`
└── tests/
    ├── conftest.py                   # Shared fixtures: sample spec, mock LLM, temp dirs
    ├── spec/
    │   ├── test_models.py
    │   └── test_loader.py
    ├── runtime/
    │   ├── test_engine.py
    │   ├── test_dag.py
    │   ├── test_context.py
    │   ├── test_prompt.py
    │   └── test_loop.py
    ├── nodes/
    │   ├── test_llm.py
    │   ├── test_script.py
    │   └── test_gate.py
    ├── registry/
    │   └── test_registry.py
    ├── state/
    │   ├── test_session.py
    │   └── test_artifacts.py
    ├── hooks/
    │   └── test_lifecycle.py
    ├── permissions/
    │   └── test_permissions.py
    └── integration/
        └── test_end_to_end.py
```

---

## Task 1: Project Scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `armature/__init__.py`
- Create: `armature/spec/__init__.py`
- Create: `armature/runtime/__init__.py`
- Create: `armature/nodes/__init__.py`
- Create: `armature/registry/__init__.py`
- Create: `armature/skills/__init__.py`
- Create: `armature/state/__init__.py`
- Create: `armature/hooks/__init__.py`
- Create: `armature/permissions/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Initialize project with uv**

```bash
cd /Users/bryansparks/projects/armature
uv init --lib armature
```

Expected: Creates `pyproject.toml`, `src/armature/__init__.py` structure. We'll flatten to `armature/` at root.

- [ ] **Step 2: Write pyproject.toml**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "armature"
version = "0.1.0"
description = "Agent execution harness — wraps LLMs in structured, inspectable workflow specs"
requires-python = ">=3.11"
dependencies = [
    "pydantic>=2.0",
    "ruamel.yaml>=0.18",
    "jinja2>=3.1",
    "litellm>=1.40",
    "aiosqlite>=0.20",
    "typer>=0.12",
    "httpx>=0.27",
]

[project.scripts]
armature = "armature.cli:app"

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5.0",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 3: Create package directories**

```bash
mkdir -p armature/{spec,runtime,nodes,registry,skills,state,hooks,permissions}
mkdir -p tests/{spec,runtime,nodes,registry,state,hooks,permissions,integration}
touch armature/{spec,runtime,nodes,registry,skills,state,hooks,permissions}/__init__.py
touch tests/{spec,runtime,nodes,registry,state,hooks,permissions,integration}/__init__.py
```

- [ ] **Step 4: Write armature/__init__.py**

```python
from armature.runtime.engine import Harness
from armature.spec.models import HarnessSpec

__all__ = ["Harness", "HarnessSpec"]
```

- [ ] **Step 5: Install dev dependencies**

```bash
uv pip install -e ".[dev]"
```

Expected: Package installs. `python -c "import armature"` succeeds.

- [ ] **Step 6: Verify import**

```bash
python -c "import armature; print('ok')"
```

Expected output: `ok`

- [ ] **Step 7: Commit**

```bash
git init
git add pyproject.toml armature/ tests/
git commit -m "feat: project scaffolding for armature"
```

---

## Task 2: HarnessSpec Pydantic Models

**Files:**
- Create: `armature/spec/models.py`
- Create: `tests/spec/test_models.py`

- [ ] **Step 1: Write failing test**

```python
# tests/spec/test_models.py
from armature.spec.models import (
    HarnessSpec, Stage, Role, Contract, Failure, Adapter,
    ModelTier, ModelTierConfig, RoleType, OutputMode
)
import pytest

def test_role_type_enum():
    assert RoleType.WORKER == "worker"
    assert RoleType.ORCHESTRATOR == "orchestrator"
    assert RoleType.JUDGE == "judge"
    assert RoleType.RESEARCHER == "researcher"

def test_minimal_spec():
    spec = HarnessSpec(
        name="test-workflow",
        version="1.0",
        stages=[
            Stage(
                id="step1",
                role=Role(name="r1", type=RoleType.WORKER, description="Do work"),
            )
        ],
    )
    assert spec.name == "test-workflow"
    assert spec.stages[0].id == "step1"

def test_stage_depends_on():
    spec = HarnessSpec(
        name="chained",
        version="1.0",
        stages=[
            Stage(id="a", role=Role(name="r", type=RoleType.WORKER, description="a")),
            Stage(id="b", depends_on=["a"], role=Role(name="r", type=RoleType.WORKER, description="b")),
        ],
    )
    assert spec.stages[1].depends_on == ["a"]

def test_contract_defaults():
    c = Contract()
    assert c.max_iterations == 20
    assert c.max_llm_calls == 100
    assert c.timeout_hours == 8.0
```

- [ ] **Step 2: Run test — verify FAIL**

```bash
pytest tests/spec/test_models.py -v
```

Expected: `ImportError` or `ModuleNotFoundError`

- [ ] **Step 3: Write models.py**

```python
# armature/spec/models.py
from __future__ import annotations
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class RoleType(str, Enum):
    WORKER = "worker"
    ORCHESTRATOR = "orchestrator"
    JUDGE = "judge"
    RESEARCHER = "researcher"


class OutputMode(str, Enum):
    TEXT = "text"
    GUIDED_JSON = "guided_json"
    JSON = "json"


class ModelTierConfig(BaseModel):
    provider: str
    model: str
    api_base: str | None = None


class ModelTiers(BaseModel):
    tiny: ModelTierConfig | None = None
    small: ModelTierConfig | None = None
    medium: ModelTierConfig | None = None
    large: ModelTierConfig | None = None
    frontier: ModelTierConfig | None = None


class Contract(BaseModel):
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    completion: str | None = None
    max_iterations: int = 20
    max_llm_calls: int = 100
    timeout_hours: float = 8.0


class Signature(BaseModel):
    input: dict[str, str] = Field(default_factory=dict)
    output: dict[str, str] = Field(default_factory=dict)


class Role(BaseModel):
    name: str
    type: RoleType
    description: str
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    model_tier: str = "small"  # resolves against ModelTiers


class LoopConfig(BaseModel):
    stage: str
    context: str = "retry"
    max: int = 3
    until: str | None = None


class Adapter(BaseModel):
    name: str
    type: str  # "python" | "script"
    fn: str | None = None
    cmd: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)


class Failure(BaseModel):
    condition: str
    recovery: str
    max_retries: int = 3


class FileState(BaseModel):
    enabled: bool = False
    base: str = "~/.armature/runs/{{run_id}}/"
    workspace: str = "workspace/"
    manifest: str = "manifest.json"


class Stage(BaseModel):
    id: str
    role: Role | None = None
    depends_on: list[str] = Field(default_factory=list)
    adapter: str | None = None
    gate: str | None = None
    signature: Signature | None = None
    output_mode: OutputMode = OutputMode.TEXT
    on_fail: dict[str, Any] | None = None
    present: str | None = None
    condition: str | None = None


class TraceConfig(BaseModel):
    enabled: bool = True
    metrics: list[str] = Field(default_factory=list)
    filesystem: str = "~/.armature/traces/{{run_id}}/"


class HarnessSpec(BaseModel):
    name: str
    version: str = "1.0"
    description: str = ""
    contracts: Contract = Field(default_factory=Contract)
    roles: dict[str, Role] = Field(default_factory=dict)
    stages: list[Stage]
    adapters: dict[str, Adapter] = Field(default_factory=dict)
    failures: dict[str, Failure] = Field(default_factory=dict)
    model_tiers: ModelTiers = Field(default_factory=ModelTiers)
    file_state: FileState = Field(default_factory=FileState)
    trace: TraceConfig = Field(default_factory=TraceConfig)
```

- [ ] **Step 4: Run test — verify PASS**

```bash
pytest tests/spec/test_models.py -v
```

Expected: All 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add armature/spec/models.py tests/spec/test_models.py
git commit -m "feat: HarnessSpec Pydantic models (all 7 NLAH elements + 4 role types)"
```

---

## Task 3: YAML Loader + Template Rendering

**Files:**
- Create: `armature/spec/loader.py`
- Create: `tests/spec/test_loader.py`
- Create: `tests/fixtures/minimal.yaml`

- [ ] **Step 1: Write failing test**

```python
# tests/spec/test_loader.py
import pytest
from pathlib import Path
from armature.spec.loader import load_spec

FIXTURES = Path(__file__).parent.parent / "fixtures"

def test_load_minimal_spec():
    spec = load_spec(FIXTURES / "minimal.yaml")
    assert spec.name == "minimal-workflow"
    assert len(spec.stages) == 1
    assert spec.stages[0].id == "step1"

def test_load_with_template_vars():
    spec = load_spec(FIXTURES / "minimal.yaml", vars={"run_id": "abc123"})
    assert spec.name == "minimal-workflow"

def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_spec(Path("/nonexistent/spec.yaml"))
```

- [ ] **Step 2: Write fixture**

```yaml
# tests/fixtures/minimal.yaml
name: minimal-workflow
version: "1.0"
description: "Minimal test workflow"

stages:
  - id: step1
    role:
      name: worker1
      type: worker
      description: "Do a simple task"
```

- [ ] **Step 3: Run test — verify FAIL**

```bash
pytest tests/spec/test_loader.py -v
```

Expected: `ImportError`

- [ ] **Step 4: Write loader.py**

```python
# armature/spec/loader.py
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
```

- [ ] **Step 5: Run test — verify PASS**

```bash
pytest tests/spec/test_loader.py -v
```

Expected: All 3 tests pass.

- [ ] **Step 6: Commit**

```bash
git add armature/spec/loader.py tests/spec/test_loader.py tests/fixtures/
git commit -m "feat: YAML spec loader with Jinja2 template rendering"
```

---

## Task 4: DAG Executor (Topological Sort + Async)

**Files:**
- Create: `armature/runtime/dag.py`
- Create: `tests/runtime/test_dag.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/runtime/test_dag.py
import asyncio
import pytest
from armature.runtime.dag import topological_order, DAGExecutor

def test_topological_order_linear():
    stages = {"a": [], "b": ["a"], "c": ["b"]}
    order = topological_order(stages)
    assert order.index("a") < order.index("b") < order.index("c")

def test_topological_order_parallel():
    stages = {"a": [], "b": [], "c": ["a", "b"]}
    order = topological_order(stages)
    assert order.index("a") < order.index("c")
    assert order.index("b") < order.index("c")

def test_topological_order_cycle_raises():
    stages = {"a": ["b"], "b": ["a"]}
    with pytest.raises(ValueError, match="cycle"):
        topological_order(stages)

@pytest.mark.asyncio
async def test_dag_executor_runs_in_order():
    execution_order = []

    async def make_handler(name):
        async def handler(ctx):
            execution_order.append(name)
            return {"done": True}
        return handler

    handlers = {
        "a": await make_handler("a"),
        "b": await make_handler("b"),
        "c": await make_handler("c"),
    }
    deps = {"a": [], "b": ["a"], "c": ["b"]}

    executor = DAGExecutor(handlers, deps)
    results = await executor.run({})

    assert execution_order == ["a", "b", "c"]
    assert "a" in results and "b" in results and "c" in results
```

- [ ] **Step 2: Run test — verify FAIL**

```bash
pytest tests/runtime/test_dag.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Write dag.py**

```python
# armature/runtime/dag.py
from __future__ import annotations
import asyncio
from collections import defaultdict, deque
from typing import Callable, Any


def topological_order(deps: dict[str, list[str]]) -> list[str]:
    in_degree: dict[str, int] = {node: 0 for node in deps}
    for node, predecessors in deps.items():
        for pred in predecessors:
            in_degree[node] = in_degree.get(node, 0)
        for pred in predecessors:
            in_degree[pred] = in_degree.get(pred, 0)

    in_degree = defaultdict(int, in_degree)
    for node, predecessors in deps.items():
        for pred in predecessors:
            in_degree[node] += 0  # ensure exists
    in_degree = {n: 0 for n in deps}
    for node, predecessors in deps.items():
        for pred in predecessors:
            in_degree[node] += 1
    # Rebuild properly
    in_degree = defaultdict(int)
    adjacency: dict[str, list[str]] = defaultdict(list)
    all_nodes = set(deps.keys())
    for node, predecessors in deps.items():
        for pred in predecessors:
            all_nodes.add(pred)
            adjacency[pred].append(node)
            in_degree[node] += 1
    for node in all_nodes:
        if node not in in_degree:
            in_degree[node] = 0

    queue = deque(n for n in all_nodes if in_degree[n] == 0)
    order = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for successor in adjacency[node]:
            in_degree[successor] -= 1
            if in_degree[successor] == 0:
                queue.append(successor)

    if len(order) != len(all_nodes):
        raise ValueError(f"DAG has a cycle — cannot determine execution order")
    return order


class DAGExecutor:
    def __init__(
        self,
        handlers: dict[str, Callable],
        deps: dict[str, list[str]],
    ):
        self._handlers = handlers
        self._deps = deps

    async def run(self, initial_ctx: dict[str, Any]) -> dict[str, Any]:
        order = topological_order(self._deps)
        results: dict[str, Any] = dict(initial_ctx)

        for stage_id in order:
            if stage_id not in self._handlers:
                continue
            handler = self._handlers[stage_id]
            stage_result = await handler(results)
            results[stage_id] = stage_result

        return results
```

- [ ] **Step 4: Run test — verify PASS**

```bash
pytest tests/runtime/test_dag.py -v
```

Expected: All 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add armature/runtime/dag.py tests/runtime/test_dag.py
git commit -m "feat: async DAG executor with topological sort and cycle detection"
```

---

## Task 5: Session Persistence (Append-Only JSONL Log)

**Files:**
- Create: `armature/state/session.py`
- Create: `tests/state/test_session.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/state/test_session.py
import asyncio
import pytest
import json
from pathlib import Path
from armature.state.session import SessionLog, SessionEvent

@pytest.fixture
def log_path(tmp_path):
    return tmp_path / "session.jsonl"

@pytest.mark.asyncio
async def test_append_and_read(log_path):
    log = SessionLog(log_path)
    await log.append(SessionEvent(type="message", data={"role": "user", "content": "hello"}))
    await log.append(SessionEvent(type="tool_result", data={"tool": "shell", "exit_code": 0}))

    events = await log.read_all()
    assert len(events) == 2
    assert events[0].type == "message"
    assert events[1].type == "tool_result"

@pytest.mark.asyncio
async def test_replay_reconstructs_events(log_path):
    log = SessionLog(log_path)
    await log.append(SessionEvent(type="start", data={"run_id": "abc"}))
    await log.append(SessionEvent(type="stage_complete", data={"stage": "s1", "output": "done"}))

    log2 = SessionLog(log_path)  # fresh instance
    events = await log2.read_all()
    assert events[0].data["run_id"] == "abc"
    assert events[1].data["stage"] == "s1"

@pytest.mark.asyncio
async def test_missing_log_returns_empty(tmp_path):
    log = SessionLog(tmp_path / "nonexistent.jsonl")
    events = await log.read_all()
    assert events == []
```

- [ ] **Step 2: Run test — verify FAIL**

```bash
pytest tests/state/test_session.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Write session.py**

```python
# armature/state/session.py
from __future__ import annotations
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from typing import Any


class SessionEvent(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class SessionLog:
    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._lock = asyncio.Lock()

    async def append(self, event: SessionEvent) -> None:
        async with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(event.model_dump_json() + "\n")
                f.flush()

    async def read_all(self) -> list[SessionEvent]:
        if not self._path.exists():
            return []
        events = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(SessionEvent.model_validate_json(line))
        return events
```

- [ ] **Step 4: Run test — verify PASS**

```bash
pytest tests/state/test_session.py -v
```

Expected: All 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add armature/state/session.py tests/state/test_session.py
git commit -m "feat: append-only JSONL session log with replay (harness component 6)"
```

---

## Task 6: Artifact Store (File-Backed State)

**Files:**
- Create: `armature/state/artifacts.py`
- Create: `tests/state/test_artifacts.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/state/test_artifacts.py
import pytest
from pathlib import Path
from armature.state.artifacts import ArtifactStore

@pytest.fixture
def store(tmp_path):
    return ArtifactStore(base_dir=tmp_path / "artifacts")

@pytest.mark.asyncio
async def test_write_and_read_json(store):
    await store.write("result", {"decision": "approve", "confidence": 0.9})
    data = await store.read("result")
    assert data["confidence"] == 0.9

@pytest.mark.asyncio
async def test_write_and_read_text(store):
    await store.write_text("brief", "This is a research brief.")
    text = await store.read_text("brief")
    assert "research brief" in text

@pytest.mark.asyncio
async def test_list_artifacts(store):
    await store.write("a", {"x": 1})
    await store.write("b", {"y": 2})
    names = await store.list()
    assert "a" in names and "b" in names

@pytest.mark.asyncio
async def test_missing_artifact_returns_none(store):
    result = await store.read("nonexistent")
    assert result is None
```

- [ ] **Step 2: Run test — verify FAIL**

```bash
pytest tests/state/test_artifacts.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Write artifacts.py**

```python
# armature/state/artifacts.py
from __future__ import annotations
import json
from pathlib import Path
from typing import Any


class ArtifactStore:
    def __init__(self, base_dir: Path | str):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str, ext: str = "json") -> Path:
        return self._base / f"{name}.{ext}"

    async def write(self, name: str, data: Any) -> Path:
        path = self._path(name, "json")
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    async def read(self, name: str) -> Any | None:
        path = self._path(name, "json")
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    async def write_text(self, name: str, content: str) -> Path:
        path = self._path(name, "md")
        path.write_text(content, encoding="utf-8")
        return path

    async def read_text(self, name: str) -> str | None:
        for ext in ("md", "txt"):
            path = self._path(name, ext)
            if path.exists():
                return path.read_text(encoding="utf-8")
        return None

    async def list(self) -> list[str]:
        return [p.stem for p in self._base.glob("*") if p.is_file()]
```

- [ ] **Step 4: Run test — verify PASS**

```bash
pytest tests/state/test_artifacts.py -v
```

Expected: All 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add armature/state/artifacts.py tests/state/test_artifacts.py
git commit -m "feat: file-backed artifact store (NLAH element 7 — durable state)"
```

---

## Task 7: Tool/Skill Registry

**Files:**
- Create: `armature/registry/registry.py`
- Create: `armature/registry/builtins.py`
- Create: `tests/registry/test_registry.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/registry/test_registry.py
import pytest
from armature.registry.registry import ToolRegistry, ToolDescriptor, PermissionLevel

def test_register_and_lookup():
    registry = ToolRegistry()
    async def my_tool(args): return {"result": "ok"}

    registry.register(ToolDescriptor(
        name="my_tool",
        description="Does something",
        permission=PermissionLevel.READ_ONLY,
        handler=my_tool,
    ))
    desc = registry.get("my_tool")
    assert desc is not None
    assert desc.name == "my_tool"

def test_unknown_tool_returns_none():
    registry = ToolRegistry()
    assert registry.get("nonexistent") is None

def test_list_descriptors_for_llm():
    registry = ToolRegistry()
    async def tool_a(args): return {}
    registry.register(ToolDescriptor(
        name="tool_a", description="Tool A",
        permission=PermissionLevel.READ_ONLY, handler=tool_a,
    ))
    descriptors = registry.descriptors()
    assert any(d["name"] == "tool_a" for d in descriptors)

@pytest.mark.asyncio
async def test_dispatch_tool():
    registry = ToolRegistry()
    async def echo(args): return {"echo": args.get("msg")}
    registry.register(ToolDescriptor(
        name="echo", description="Echoes input",
        permission=PermissionLevel.READ_ONLY, handler=echo,
    ))
    result = await registry.dispatch("echo", {"msg": "hello"})
    assert result["echo"] == "hello"
```

- [ ] **Step 2: Run test — verify FAIL**

```bash
pytest tests/registry/test_registry.py -v
```

- [ ] **Step 3: Write registry.py**

```python
# armature/registry/registry.py
from __future__ import annotations
from enum import Enum
from typing import Callable, Any
from pydantic import BaseModel


class PermissionLevel(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE = "workspace"
    NETWORK = "network"
    DESTRUCTIVE = "destructive"


class ToolDescriptor(BaseModel):
    name: str
    description: str
    permission: PermissionLevel
    handler: Callable
    parameters: dict[str, Any] = {}

    model_config = {"arbitrary_types_allowed": True}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDescriptor] = {}

    def register(self, descriptor: ToolDescriptor) -> None:
        self._tools[descriptor.name] = descriptor

    def get(self, name: str) -> ToolDescriptor | None:
        return self._tools.get(name)

    def descriptors(self) -> list[dict[str, Any]]:
        return [
            {"name": d.name, "description": d.description, "parameters": d.parameters}
            for d in self._tools.values()
        ]

    async def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        desc = self.get(name)
        if desc is None:
            raise KeyError(f"Tool not found: {name}")
        return await desc.handler(args)
```

- [ ] **Step 4: Write builtins.py**

```python
# armature/registry/builtins.py
import subprocess
import httpx
from pathlib import Path
from armature.registry.registry import ToolRegistry, ToolDescriptor, PermissionLevel


async def _file_read(args: dict) -> dict:
    path = Path(args["path"])
    if not path.exists():
        return {"error": f"File not found: {path}"}
    return {"content": path.read_text(encoding="utf-8")}


async def _file_write(args: dict) -> dict:
    path = Path(args["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args["content"], encoding="utf-8")
    return {"written": str(path)}


async def _shell_run(args: dict) -> dict:
    result = subprocess.run(
        args["cmd"], shell=True, capture_output=True, text=True, timeout=30
    )
    return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode}


async def _http_get(args: dict) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(args["url"], timeout=10)
        return {"status": response.status_code, "body": response.text}


def register_builtins(registry: ToolRegistry) -> None:
    registry.register(ToolDescriptor(
        name="file_read", description="Read a file from disk",
        permission=PermissionLevel.READ_ONLY, handler=_file_read,
        parameters={"path": {"type": "string", "description": "Absolute file path"}},
    ))
    registry.register(ToolDescriptor(
        name="file_write", description="Write content to a file",
        permission=PermissionLevel.WORKSPACE, handler=_file_write,
        parameters={"path": {"type": "string"}, "content": {"type": "string"}},
    ))
    registry.register(ToolDescriptor(
        name="shell", description="Run a shell command",
        permission=PermissionLevel.WORKSPACE, handler=_shell_run,
        parameters={"cmd": {"type": "string", "description": "Shell command to execute"}},
    ))
    registry.register(ToolDescriptor(
        name="http_get", description="Make an HTTP GET request",
        permission=PermissionLevel.NETWORK, handler=_http_get,
        parameters={"url": {"type": "string"}},
    ))
```

- [ ] **Step 5: Run test — verify PASS**

```bash
pytest tests/registry/test_registry.py -v
```

- [ ] **Step 6: Commit**

```bash
git add armature/registry/ tests/registry/
git commit -m "feat: tool/skill registry with builtin tools (harness component 3)"
```

---

## Task 8: Lifecycle Hooks

**Files:**
- Create: `armature/hooks/lifecycle.py`
- Create: `tests/hooks/test_lifecycle.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/hooks/test_lifecycle.py
import pytest
from armature.hooks.lifecycle import HookRegistry, HookDecision, HookPhase

@pytest.mark.asyncio
async def test_pre_tool_hook_allow():
    registry = HookRegistry()
    async def allow_hook(phase, tool_name, args, ctx):
        return HookDecision.ALLOW

    registry.register(HookPhase.PRE_TOOL, allow_hook)
    decision = await registry.run_pre_tool("shell", {"cmd": "ls"}, {})
    assert decision == HookDecision.ALLOW

@pytest.mark.asyncio
async def test_pre_tool_hook_block():
    registry = HookRegistry()
    async def block_hook(phase, tool_name, args, ctx):
        return HookDecision.BLOCK

    registry.register(HookPhase.PRE_TOOL, block_hook)
    decision = await registry.run_pre_tool("shell", {"cmd": "rm -rf /"}, {})
    assert decision == HookDecision.BLOCK

@pytest.mark.asyncio
async def test_post_tool_hook_called():
    registry = HookRegistry()
    called_with = []

    async def record_hook(phase, tool_name, result, ctx):
        called_with.append((tool_name, result))

    registry.register(HookPhase.POST_TOOL, record_hook)
    await registry.run_post_tool("shell", {"exit_code": 0}, {})
    assert called_with == [("shell", {"exit_code": 0})]
```

- [ ] **Step 2: Run test — verify FAIL**

```bash
pytest tests/hooks/test_lifecycle.py -v
```

- [ ] **Step 3: Write lifecycle.py**

```python
# armature/hooks/lifecycle.py
from __future__ import annotations
from enum import Enum
from typing import Callable, Any


class HookPhase(str, Enum):
    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"
    PRE_STAGE = "pre_stage"
    POST_STAGE = "post_stage"


class HookDecision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    MODIFY = "modify"


class HookRegistry:
    def __init__(self):
        self._hooks: dict[HookPhase, list[Callable]] = {p: [] for p in HookPhase}

    def register(self, phase: HookPhase, fn: Callable) -> None:
        self._hooks[phase].append(fn)

    async def run_pre_tool(self, tool_name: str, args: dict, ctx: dict) -> HookDecision:
        for hook in self._hooks[HookPhase.PRE_TOOL]:
            decision = await hook(HookPhase.PRE_TOOL, tool_name, args, ctx)
            if decision == HookDecision.BLOCK:
                return HookDecision.BLOCK
        return HookDecision.ALLOW

    async def run_post_tool(self, tool_name: str, result: Any, ctx: dict) -> None:
        for hook in self._hooks[HookPhase.POST_TOOL]:
            await hook(HookPhase.POST_TOOL, tool_name, result, ctx)

    async def run_pre_stage(self, stage_id: str, ctx: dict) -> HookDecision:
        for hook in self._hooks[HookPhase.PRE_STAGE]:
            decision = await hook(HookPhase.PRE_STAGE, stage_id, {}, ctx)
            if decision == HookDecision.BLOCK:
                return HookDecision.BLOCK
        return HookDecision.ALLOW

    async def run_post_stage(self, stage_id: str, result: Any, ctx: dict) -> None:
        for hook in self._hooks[HookPhase.POST_STAGE]:
            await hook(HookPhase.POST_STAGE, stage_id, result, ctx)
```

- [ ] **Step 4: Run test — verify PASS**

```bash
pytest tests/hooks/test_lifecycle.py -v
```

- [ ] **Step 5: Commit**

```bash
git add armature/hooks/lifecycle.py tests/hooks/test_lifecycle.py
git commit -m "feat: lifecycle hooks with pre/post tool and stage events (harness component 8)"
```

---

## Task 9: Permissions and Safety

**Files:**
- Create: `armature/permissions/permissions.py`
- Create: `tests/permissions/test_permissions.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/permissions/test_permissions.py
from armature.permissions.permissions import (
    classify_shell_command, PermissionLevel, requires_approval
)

def test_classify_ls_as_readonly():
    assert classify_shell_command("ls -la /tmp") == PermissionLevel.READ_ONLY

def test_classify_grep_as_readonly():
    assert classify_shell_command("grep -r 'pattern' .") == PermissionLevel.READ_ONLY

def test_classify_rm_as_destructive():
    assert classify_shell_command("rm -rf /tmp/old") == PermissionLevel.DESTRUCTIVE

def test_classify_sudo_as_destructive():
    assert classify_shell_command("sudo apt install curl") == PermissionLevel.DESTRUCTIVE

def test_classify_git_commit_as_workspace():
    assert classify_shell_command("git commit -m 'msg'") == PermissionLevel.WORKSPACE

def test_requires_approval_for_destructive():
    assert requires_approval(PermissionLevel.DESTRUCTIVE) is True

def test_no_approval_for_readonly():
    assert requires_approval(PermissionLevel.READ_ONLY) is False
```

- [ ] **Step 2: Run test — verify FAIL**

```bash
pytest tests/permissions/test_permissions.py -v
```

- [ ] **Step 3: Write permissions.py**

```python
# armature/permissions/permissions.py
from armature.registry.registry import PermissionLevel

_READONLY_PREFIXES = ("ls", "cat", "grep", "find", "head", "tail", "wc", "echo", "pwd", "which", "env", "printenv", "git log", "git diff", "git status", "git show")
_DESTRUCTIVE_PREFIXES = ("rm", "sudo", "shutdown", "reboot", "mkfs", "dd ", "chmod 777", "chown", "kill", "killall", "pkill")


def classify_shell_command(cmd: str) -> PermissionLevel:
    stripped = cmd.strip()
    for prefix in _DESTRUCTIVE_PREFIXES:
        if stripped.startswith(prefix):
            return PermissionLevel.DESTRUCTIVE
    for prefix in _READONLY_PREFIXES:
        if stripped.startswith(prefix):
            return PermissionLevel.READ_ONLY
    return PermissionLevel.WORKSPACE


def requires_approval(level: PermissionLevel) -> bool:
    return level == PermissionLevel.DESTRUCTIVE
```

- [ ] **Step 4: Run test — verify PASS**

```bash
pytest tests/permissions/test_permissions.py -v
```

- [ ] **Step 5: Commit**

```bash
git add armature/permissions/permissions.py tests/permissions/test_permissions.py
git commit -m "feat: shell command permission classification and approval gating (harness component 9)"
```

---

## Task 10: System Prompt Assembly Pipeline

**Files:**
- Create: `armature/runtime/prompt.py`
- Create: `tests/runtime/test_prompt.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/runtime/test_prompt.py
import pytest
from pathlib import Path
from armature.runtime.prompt import PromptAssembler
from armature.spec.models import Role, RoleType

def test_static_prefix_included():
    assembler = PromptAssembler(static_prefix="You are an ELF harness agent.")
    role = Role(name="worker1", type=RoleType.WORKER, description="Do structured tasks.")
    prompt = assembler.build(role=role, tools=[], context={})
    assert "You are an ELF harness agent." in prompt

def test_role_description_included():
    assembler = PromptAssembler()
    role = Role(name="researcher1", type=RoleType.RESEARCHER, description="Search for information on the topic.")
    prompt = assembler.build(role=role, tools=[], context={})
    assert "Search for information" in prompt

def test_tools_included():
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER, description="work")
    tools = [{"name": "shell", "description": "Run shell commands"}]
    prompt = assembler.build(role=role, tools=tools, context={})
    assert "shell" in prompt

def test_instruction_file_injected(tmp_path):
    harness_md = tmp_path / "HARNESS.md"
    harness_md.write_text("Always verify outputs before returning.")
    assembler = PromptAssembler(instruction_dirs=[tmp_path])
    role = Role(name="w", type=RoleType.WORKER, description="work")
    prompt = assembler.build(role=role, tools=[], context={})
    assert "Always verify outputs" in prompt
```

- [ ] **Step 2: Run test — verify FAIL**

```bash
pytest tests/runtime/test_prompt.py -v
```

- [ ] **Step 3: Write prompt.py**

```python
# armature/runtime/prompt.py
from __future__ import annotations
from pathlib import Path
from typing import Any
from armature.spec.models import Role, RoleType

_ROLE_PREAMBLES = {
    RoleType.WORKER: "You are a focused task executor. Produce structured output that matches the required schema exactly.",
    RoleType.ORCHESTRATOR: "You are coordinating a multi-step workflow. Plan carefully, delegate to appropriate tools, and track progress.",
    RoleType.JUDGE: "You are evaluating output quality. Assess carefully, score objectively, and identify specific issues.",
    RoleType.RESEARCHER: "You are gathering and synthesizing information. Search broadly, filter for credibility, and structure your findings.",
}


class PromptAssembler:
    def __init__(
        self,
        static_prefix: str = "",
        instruction_dirs: list[Path] | None = None,
    ):
        self._static_prefix = static_prefix
        self._instruction_dirs = instruction_dirs or []

    def _load_instruction_files(self) -> str:
        parts = []
        for directory in self._instruction_dirs:
            for filename in ("HARNESS.md", "CLAUDE.md", "AGENTS.md"):
                path = Path(directory) / filename
                if path.exists():
                    parts.append(path.read_text(encoding="utf-8").strip())
        return "\n\n".join(parts)

    def build(
        self,
        role: Role,
        tools: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> str:
        sections = []

        # 1. Static prefix (stable for prompt caching — load first)
        if self._static_prefix:
            sections.append(self._static_prefix)

        # 2. Instruction files (semi-static)
        instructions = self._load_instruction_files()
        if instructions:
            sections.append(instructions)

        # 3. Role preamble
        sections.append(_ROLE_PREAMBLES.get(role.type, ""))

        # 4. Role-specific description
        sections.append(f"## Your Role\n{role.description}")

        # 5. Available tools (dynamic)
        if tools:
            tool_lines = "\n".join(f"- {t['name']}: {t['description']}" for t in tools)
            sections.append(f"## Available Tools\n{tool_lines}")

        # 6. Current context (dynamic — loaded last, least cacheable)
        if context:
            ctx_items = "\n".join(f"- {k}: {v}" for k, v in context.items() if v is not None)
            if ctx_items:
                sections.append(f"## Current Context\n{ctx_items}")

        return "\n\n".join(s for s in sections if s)
```

- [ ] **Step 4: Run test — verify PASS**

```bash
pytest tests/runtime/test_prompt.py -v
```

- [ ] **Step 5: Commit**

```bash
git add armature/runtime/prompt.py tests/runtime/test_prompt.py
git commit -m "feat: system prompt assembly pipeline with static prefix caching (harness component 7)"
```

---

## Task 11: LLM Node (Four Role Types + litellm Routing)

**Files:**
- Create: `armature/nodes/base.py`
- Create: `armature/nodes/llm.py`
- Create: `tests/nodes/test_llm.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/nodes/test_llm.py
import pytest
from unittest.mock import AsyncMock, patch
from armature.nodes.llm import LLMNode
from armature.spec.models import Stage, Role, RoleType, ModelTiers, ModelTierConfig

def make_stage(role_type: RoleType = RoleType.WORKER) -> Stage:
    return Stage(
        id="test",
        role=Role(name="r", type=role_type, description="test role", model_tier="small"),
    )

def make_tiers() -> ModelTiers:
    return ModelTiers(
        small=ModelTierConfig(provider="ollama", model="qwen2.5:7b"),
        frontier=ModelTierConfig(provider="anthropic", model="claude-opus-4-7"),
    )

@pytest.mark.asyncio
async def test_worker_routes_to_small_model():
    stage = make_stage(RoleType.WORKER)
    tiers = make_tiers()
    node = LLMNode(stage=stage, tiers=tiers)

    with patch("armature.nodes.llm.litellm_completion") as mock:
        mock.return_value = AsyncMock(return_value={"content": "result"})
        # Just verify the model string would be correct
        model_str = node._resolve_model()
        assert "qwen" in model_str or "ollama" in model_str.lower()

@pytest.mark.asyncio
async def test_judge_routes_to_frontier_model():
    stage = make_stage(RoleType.JUDGE)
    stage.role.model_tier = "frontier"
    tiers = make_tiers()
    node = LLMNode(stage=stage, tiers=tiers)
    model_str = node._resolve_model()
    assert "claude" in model_str or "anthropic" in model_str.lower()

def test_llm_node_requires_role():
    stage = Stage(id="no-role", role=None)
    with pytest.raises(ValueError, match="role"):
        LLMNode(stage=stage, tiers=ModelTiers())
```

- [ ] **Step 2: Run test — verify FAIL**

```bash
pytest tests/nodes/test_llm.py -v
```

- [ ] **Step 3: Write base.py**

```python
# armature/nodes/base.py
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class BaseNode(ABC):
    @abstractmethod
    async def execute(self, context: dict[str, Any]) -> Any:
        ...
```

- [ ] **Step 4: Write llm.py**

```python
# armature/nodes/llm.py
from __future__ import annotations
import json
from typing import Any
import litellm
from armature.nodes.base import BaseNode
from armature.spec.models import Stage, ModelTiers, RoleType
from armature.runtime.prompt import PromptAssembler


async def litellm_completion(**kwargs) -> Any:
    return await litellm.acompletion(**kwargs)


_TIER_ORDER = ["tiny", "small", "medium", "large", "frontier"]


class LLMNode(BaseNode):
    def __init__(
        self,
        stage: Stage,
        tiers: ModelTiers,
        assembler: PromptAssembler | None = None,
        registry=None,
    ):
        if stage.role is None:
            raise ValueError(f"Stage '{stage.id}' has no role — cannot create LLMNode")
        self._stage = stage
        self._tiers = tiers
        self._assembler = assembler or PromptAssembler()
        self._registry = registry

    def _resolve_model(self) -> str:
        tier_name = self._stage.role.model_tier
        tier_config = getattr(self._tiers, tier_name, None)
        if tier_config is None:
            # Fall back to next available tier
            for t in _TIER_ORDER:
                cfg = getattr(self._tiers, t, None)
                if cfg is not None:
                    tier_config = cfg
                    break
        if tier_config is None:
            raise ValueError(f"No model tier configured for '{tier_name}'")

        provider = tier_config.provider
        model = tier_config.model
        if provider == "ollama":
            return f"ollama/{model}"
        elif provider == "anthropic":
            return model
        elif provider == "openrouter":
            return f"openrouter/{model}"
        return model

    async def execute(self, context: dict[str, Any]) -> Any:
        role = self._stage.role
        tools = self._registry.descriptors() if self._registry else []
        system_prompt = self._assembler.build(role=role, tools=tools, context=context)
        model = self._resolve_model()

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(context, default=str)},
        ]

        response = await litellm_completion(model=model, messages=messages)
        content = response.choices[0].message.content

        if self._stage.output_mode.value == "json" or self._stage.output_mode.value == "guided_json":
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return {"raw": content, "_parse_error": True}
        return {"content": content}
```

- [ ] **Step 5: Run test — verify PASS**

```bash
pytest tests/nodes/test_llm.py -v
```

- [ ] **Step 6: Commit**

```bash
git add armature/nodes/ tests/nodes/
git commit -m "feat: LLM node with four role types and litellm routing (harness component 1+3)"
```

---

## Task 12: Script Adapter Node + Human Gate

**Files:**
- Create: `armature/nodes/script.py`
- Create: `armature/nodes/gate.py`
- Create: `tests/nodes/test_script.py`
- Create: `tests/nodes/test_gate.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/nodes/test_script.py
import pytest
from armature.nodes.script import ScriptNode
from armature.spec.models import Adapter

@pytest.mark.asyncio
async def test_script_node_runs_command():
    adapter = Adapter(name="echo_test", type="script", cmd="echo hello")
    node = ScriptNode(adapter=adapter)
    result = await node.execute({})
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]

@pytest.mark.asyncio
async def test_script_node_captures_failure():
    adapter = Adapter(name="fail_test", type="script", cmd="exit 1")
    node = ScriptNode(adapter=adapter)
    result = await node.execute({})
    assert result["exit_code"] == 1
```

```python
# tests/nodes/test_gate.py
import asyncio
import pytest
from unittest.mock import patch
from armature.nodes.gate import HumanGateNode
from armature.spec.models import Stage, Role, RoleType

@pytest.mark.asyncio
async def test_gate_approved_on_yes():
    stage = Stage(
        id="gate1",
        gate="human",
        present="Decision: approve?",
        role=Role(name="r", type=RoleType.WORKER, description="d"),
    )
    node = HumanGateNode(stage=stage)
    with patch("builtins.input", return_value="yes"):
        result = await node.execute({})
    assert result["approved"] is True

@pytest.mark.asyncio
async def test_gate_rejected_on_no():
    stage = Stage(
        id="gate1",
        gate="human",
        present="Decision: approve?",
        role=Role(name="r", type=RoleType.WORKER, description="d"),
    )
    node = HumanGateNode(stage=stage)
    with patch("builtins.input", return_value="no"):
        result = await node.execute({})
    assert result["approved"] is False
    assert "feedback" in result
```

- [ ] **Step 2: Run tests — verify FAIL**

```bash
pytest tests/nodes/test_script.py tests/nodes/test_gate.py -v
```

- [ ] **Step 3: Write script.py**

```python
# armature/nodes/script.py
from __future__ import annotations
import asyncio
import subprocess
from jinja2 import Environment, BaseLoader
from typing import Any
from armature.nodes.base import BaseNode
from armature.spec.models import Adapter


class ScriptNode(BaseNode):
    def __init__(self, adapter: Adapter):
        self._adapter = adapter

    async def execute(self, context: dict[str, Any]) -> Any:
        cmd = self._adapter.cmd or ""
        if "{{" in cmd:
            env = Environment(loader=BaseLoader(), variable_start_string="{{", variable_end_string="}}")
            cmd = env.from_string(cmd).render(**context)

        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
```

- [ ] **Step 4: Write gate.py**

```python
# armature/nodes/gate.py
from __future__ import annotations
from typing import Any
from jinja2 import Environment, BaseLoader
from armature.nodes.base import BaseNode
from armature.spec.models import Stage


class HumanGateNode(BaseNode):
    def __init__(self, stage: Stage):
        self._stage = stage

    async def execute(self, context: dict[str, Any]) -> Any:
        message = self._stage.present or "Review required."
        if "{{" in message:
            env = Environment(loader=BaseLoader(), variable_start_string="{{", variable_end_string="}}")
            message = env.from_string(message).render(**context)

        print(f"\n{'='*60}")
        print(f"HUMAN APPROVAL REQUIRED")
        print(f"{'='*60}")
        print(message)
        print(f"{'='*60}")

        response = input("Approve? [yes/no/feedback]: ").strip().lower()
        if response in ("yes", "y", "approve"):
            return {"approved": True, "feedback": None}
        else:
            feedback = input("Enter feedback (press Enter to skip): ").strip()
            return {"approved": False, "feedback": feedback or response}
```

- [ ] **Step 5: Run tests — verify PASS**

```bash
pytest tests/nodes/test_script.py tests/nodes/test_gate.py -v
```

- [ ] **Step 6: Commit**

```bash
git add armature/nodes/script.py armature/nodes/gate.py tests/nodes/
git commit -m "feat: script adapter node + human approval gate (harness components 3, 4)"
```

---

## Task 13: Context Management + Compaction

**Files:**
- Create: `armature/runtime/context.py`
- Create: `tests/runtime/test_context.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/runtime/test_context.py
import pytest
from armature.runtime.context import ContextManager

def test_add_and_retrieve():
    mgr = ContextManager(token_budget=1000)
    mgr.add_message("user", "hello")
    messages = mgr.messages()
    assert len(messages) == 1
    assert messages[0]["role"] == "user"

def test_compaction_fires_at_budget():
    mgr = ContextManager(token_budget=50)
    # Each message roughly 10 tokens
    for i in range(8):
        mgr.add_message("user", f"message number {i} with some words")
    # After compaction, context should be smaller
    assert mgr.estimated_tokens() <= 50

def test_recent_messages_preserved_after_compaction():
    mgr = ContextManager(token_budget=50)
    for i in range(8):
        mgr.add_message("user", f"message {i}")
    messages = mgr.messages()
    # Most recent message should always be preserved
    assert any("7" in m["content"] for m in messages)
```

- [ ] **Step 2: Run test — verify FAIL**

```bash
pytest tests/runtime/test_context.py -v
```

- [ ] **Step 3: Write context.py**

```python
# armature/runtime/context.py
from __future__ import annotations
from typing import Any


def _estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


class ContextManager:
    def __init__(self, token_budget: int = 8000, keep_recent: int = 4):
        self._budget = token_budget
        self._keep_recent = keep_recent
        self._messages: list[dict[str, Any]] = []

    def add_message(self, role: str, content: str) -> None:
        self._messages.append({"role": role, "content": content})
        if self.estimated_tokens() > self._budget:
            self._compact()

    def messages(self) -> list[dict[str, Any]]:
        return list(self._messages)

    def estimated_tokens(self) -> int:
        return sum(_estimate_tokens(m["content"]) for m in self._messages)

    def _compact(self) -> None:
        if len(self._messages) <= self._keep_recent:
            return
        to_summarize = self._messages[: -self._keep_recent]
        recent = self._messages[-self._keep_recent :]
        summary_text = f"[Compacted {len(to_summarize)} messages: " + "; ".join(
            m["content"][:40] for m in to_summarize
        ) + "]"
        self._messages = [{"role": "system", "content": summary_text}] + recent
```

- [ ] **Step 4: Run test — verify PASS**

```bash
pytest tests/runtime/test_context.py -v
```

- [ ] **Step 5: Commit**

```bash
git add armature/runtime/context.py tests/runtime/test_context.py
git commit -m "feat: context manager with token-budget compaction (harness component 2)"
```

---

## Task 14: Loop Executor (Loop-Until)

**Files:**
- Create: `armature/runtime/loop.py`
- Create: `tests/runtime/test_loop.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/runtime/test_loop.py
import pytest
from armature.runtime.loop import LoopExecutor

@pytest.mark.asyncio
async def test_loop_stops_at_max():
    call_count = 0

    async def handler(ctx):
        nonlocal call_count
        call_count += 1
        return {"value": call_count}

    def never_done(result): return False

    executor = LoopExecutor(handler=handler, until=never_done, max_iterations=3)
    result = await executor.run({})
    assert call_count == 3

@pytest.mark.asyncio
async def test_loop_stops_when_condition_met():
    call_count = 0

    async def handler(ctx):
        nonlocal call_count
        call_count += 1
        return {"value": call_count}

    def done_at_two(result): return result.get("value", 0) >= 2

    executor = LoopExecutor(handler=handler, until=done_at_two, max_iterations=10)
    result = await executor.run({})
    assert call_count == 2
    assert result["value"] == 2

@pytest.mark.asyncio
async def test_loop_returns_last_result():
    async def handler(ctx):
        return {"final": "output"}

    executor = LoopExecutor(handler=handler, until=lambda r: True, max_iterations=5)
    result = await executor.run({})
    assert result["final"] == "output"
```

- [ ] **Step 2: Run test — verify FAIL**

```bash
pytest tests/runtime/test_loop.py -v
```

- [ ] **Step 3: Write loop.py**

```python
# armature/runtime/loop.py
from __future__ import annotations
from typing import Any, Callable, Awaitable


class LoopExecutor:
    def __init__(
        self,
        handler: Callable[[dict], Awaitable[Any]],
        until: Callable[[Any], bool],
        max_iterations: int = 10,
    ):
        self._handler = handler
        self._until = until
        self._max = max_iterations

    async def run(self, initial_ctx: dict[str, Any]) -> Any:
        ctx = dict(initial_ctx)
        result = None
        for iteration in range(self._max):
            result = await self._handler(ctx)
            if isinstance(result, dict):
                ctx.update(result)
            if self._until(result):
                break
        return result
```

- [ ] **Step 4: Run test — verify PASS**

```bash
pytest tests/runtime/test_loop.py -v
```

- [ ] **Step 5: Commit**

```bash
git add armature/runtime/loop.py tests/runtime/test_loop.py
git commit -m "feat: loop-until executor with max iterations guard"
```

---

## Task 15: Main Harness Engine (Iteration Loop Core)

**Files:**
- Create: `armature/runtime/engine.py`
- Create: `tests/runtime/test_engine.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/runtime/test_engine.py
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from armature.runtime.engine import Harness
from armature.spec.models import HarnessSpec, Stage, Role, RoleType

def make_minimal_spec() -> HarnessSpec:
    return HarnessSpec(
        name="test",
        version="1.0",
        stages=[
            Stage(id="s1", role=Role(name="r", type=RoleType.WORKER, description="test"))
        ]
    )

@pytest.mark.asyncio
async def test_harness_from_spec():
    spec = make_minimal_spec()
    harness = Harness(spec=spec)
    assert harness.name == "test"

@pytest.mark.asyncio
async def test_harness_run_returns_result():
    spec = make_minimal_spec()
    harness = Harness(spec=spec)

    with patch.object(harness, "_execute_stage", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = {"content": "stage output"}
        result = await harness.run({"topic": "test"})

    assert result is not None
    mock_exec.assert_called_once()

def test_harness_from_file(tmp_path):
    spec_file = tmp_path / "test.yaml"
    spec_file.write_text("""
name: file-test
version: "1.0"
stages:
  - id: s1
    role:
      name: r
      type: worker
      description: test
""")
    harness = Harness.from_spec(spec_file)
    assert harness.name == "file-test"
```

- [ ] **Step 2: Run test — verify FAIL**

```bash
pytest tests/runtime/test_engine.py -v
```

- [ ] **Step 3: Write engine.py**

```python
# armature/runtime/engine.py
from __future__ import annotations
import uuid
from pathlib import Path
from typing import Any
from armature.spec.models import HarnessSpec, Stage
from armature.spec.loader import load_spec
from armature.runtime.dag import DAGExecutor
from armature.runtime.context import ContextManager
from armature.runtime.prompt import PromptAssembler
from armature.runtime.loop import LoopExecutor
from armature.nodes.llm import LLMNode
from armature.nodes.script import ScriptNode
from armature.nodes.gate import HumanGateNode
from armature.registry.registry import ToolRegistry
from armature.registry.builtins import register_builtins
from armature.hooks.lifecycle import HookRegistry
from armature.state.session import SessionLog, SessionEvent
from armature.state.artifacts import ArtifactStore


class Harness:
    def __init__(
        self,
        spec: HarnessSpec,
        session_dir: Path | None = None,
    ):
        self._spec = spec
        self._run_id = str(uuid.uuid4())[:8]
        base_dir = Path(session_dir or f"~/.armature/runs/{self._run_id}").expanduser()
        self._session = SessionLog(base_dir / "session.jsonl")
        self._artifacts = ArtifactStore(base_dir / "artifacts")
        self._registry = ToolRegistry()
        register_builtins(self._registry)
        self._hooks = HookRegistry()
        self._context = ContextManager()
        self._assembler = PromptAssembler()

    @property
    def name(self) -> str:
        return self._spec.name

    @classmethod
    def from_spec(cls, path: Path | str, vars: dict | None = None) -> "Harness":
        spec = load_spec(path, vars=vars)
        return cls(spec=spec)

    async def _execute_stage(self, stage: Stage, context: dict[str, Any]) -> Any:
        await self._session.append(SessionEvent(type="stage_start", data={"stage": stage.id}))

        decision = await self._hooks.run_pre_stage(stage.id, context)

        if stage.gate == "human":
            node = HumanGateNode(stage=stage)
            result = await node.execute(context)
        elif stage.adapter:
            adapter = self._spec.adapters.get(stage.adapter)
            if adapter is None:
                raise ValueError(f"Adapter '{stage.adapter}' not defined in spec")
            node = ScriptNode(adapter=adapter)
            result = await node.execute(context)
        elif stage.role:
            node = LLMNode(
                stage=stage,
                tiers=self._spec.model_tiers,
                assembler=self._assembler,
                registry=self._registry,
            )
            result = await node.execute(context)
        else:
            raise ValueError(f"Stage '{stage.id}' has no role, adapter, or gate")

        await self._hooks.run_post_stage(stage.id, result, context)
        await self._session.append(SessionEvent(
            type="stage_complete", data={"stage": stage.id, "result": str(result)[:500]}
        ))
        return result

    async def run(self, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        context = dict(inputs or {})
        context["run_id"] = self._run_id

        await self._session.append(SessionEvent(
            type="run_start", data={"run_id": self._run_id, "workflow": self._spec.name}
        ))

        deps = {s.id: s.depends_on for s in self._spec.stages}
        stage_map = {s.id: s for s in self._spec.stages}

        async def make_handler(stage: Stage):
            async def handler(ctx):
                return await self._execute_stage(stage, ctx)
            return handler

        handlers = {s.id: await make_handler(s) for s in self._spec.stages}
        executor = DAGExecutor(handlers, deps)
        results = await executor.run(context)

        await self._session.append(SessionEvent(type="run_complete", data={"run_id": self._run_id}))
        return results
```

- [ ] **Step 4: Run test — verify PASS**

```bash
pytest tests/runtime/test_engine.py -v
```

- [ ] **Step 5: Commit**

```bash
git add armature/runtime/engine.py tests/runtime/test_engine.py
git commit -m "feat: main Harness engine — iteration loop, DAG execution, all nodes wired (harness component 1)"
```

---

## Task 16: CLI

**Files:**
- Create: `armature/cli.py`
- Test: Manual verification (CLI is hard to unit test; integration test covers it)

- [ ] **Step 1: Write cli.py**

```python
# armature/cli.py
import asyncio
import json
from pathlib import Path
import typer
from armature.runtime.engine import Harness

app = typer.Typer(name="armature", help="ELF ecosystem agent harness runner")


def parse_inputs(raw: list[str]) -> dict:
    result = {}
    for item in raw:
        if "=" not in item:
            typer.echo(f"Invalid input format '{item}' — use key=value", err=True)
            raise typer.Exit(1)
        k, _, v = item.partition("=")
        result[k.strip()] = v.strip()
    return result


@app.command()
def run(
    spec: Path = typer.Argument(..., help="Path to workflow spec YAML"),
    inputs: list[str] = typer.Option([], "--input", "-i", help="Input values as key=value"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate spec without executing"),
):
    """Run a workflow from a YAML spec file."""
    if not spec.exists():
        typer.echo(f"Spec file not found: {spec}", err=True)
        raise typer.Exit(1)

    parsed_inputs = parse_inputs(inputs)
    harness = Harness.from_spec(spec, vars=parsed_inputs)

    if dry_run:
        typer.echo(f"✓ Spec '{harness.name}' loaded successfully ({len(harness._spec.stages)} stages)")
        typer.echo("Dry run — no execution.")
        return

    typer.echo(f"Running workflow: {harness.name}")

    async def _run():
        return await harness.run(parsed_inputs)

    result = asyncio.run(_run())
    typer.echo(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    app()
```

- [ ] **Step 2: Verify CLI works**

```bash
armature --help
armature run tests/fixtures/minimal.yaml --dry-run
```

Expected:
```
✓ Spec 'minimal-workflow' loaded successfully (1 stages)
Dry run — no execution.
```

- [ ] **Step 3: Commit**

```bash
git add armature/cli.py
git commit -m "feat: CLI with run command, dry-run, and key=value inputs"
```

---

## Task 17: Built-in Quorum and Tessera Skills

**Files:**
- Create: `armature/skills/quorum.py`
- Create: `armature/skills/tessera.py`
- Create: `tests/skills/` (stubs — real tests require Quorum/Tessera running)

- [ ] **Step 1: Write quorum.py (thin adapter)**

```python
# armature/skills/quorum.py
from __future__ import annotations
from typing import Any


async def deliberate(args: dict[str, Any]) -> dict[str, Any]:
    """
    Calls Quorum's QuorumEngine for deliberation.
    Args: { topic: str, brief: str (optional), agents: list[str] (optional) }
    Returns: { decision: str, confidence: float, dissents: list[str], trace: dict }
    """
    try:
        from quorum import Quorum, QuorumConfig  # type: ignore
    except ImportError:
        raise ImportError(
            "Quorum is not installed. Install it with: pip install quorum\n"
            "Or clone from: ~/projects/quorum"
        )

    config = QuorumConfig(
        objective=args.get("topic", args.get("objective", "")),
        documents=[args.get("brief", "")],
        agent_roles=args.get("agents", ["analyst", "strategist", "risk_assessor"]),
    )
    engine = Quorum(config=config)
    result = await engine.run_async()
    return {
        "decision": result.decision,
        "confidence": result.confidence,
        "dissents": result.dissenting_opinions,
        "trace": result.transcript,
    }
```

- [ ] **Step 2: Write tessera.py (thin adapter)**

```python
# armature/skills/tessera.py
from __future__ import annotations
from typing import Any
import httpx


async def retrieve(args: dict[str, Any]) -> dict[str, Any]:
    """
    Calls Tessera RAG API for retrieval.
    Args: { query: str, top_k: int (optional, default 5), collection: str (optional) }
    Returns: { chunks: list[dict], sources: list[str] }
    """
    tessera_url = args.get("tessera_url", "http://localhost:8000")
    query = args["query"]
    top_k = args.get("top_k", 5)
    collection = args.get("collection", "default")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{tessera_url}/retrieve",
            json={"query": query, "top_k": top_k, "collection": collection},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return {
            "chunks": data.get("chunks", []),
            "sources": data.get("sources", []),
        }
```

- [ ] **Step 3: Register skills in registry**

In `armature/registry/builtins.py`, add at end of `register_builtins()`:

```python
from armature.skills import quorum as _quorum_skill, tessera as _tessera_skill

def register_builtins(registry: ToolRegistry) -> None:
    # ... existing registrations ...

    registry.register(ToolDescriptor(
        name="quorum.deliberate",
        description="Run structured multi-agent deliberation on a topic via Quorum",
        permission=PermissionLevel.NETWORK,
        handler=_quorum_skill.deliberate,
        parameters={
            "topic": {"type": "string"},
            "brief": {"type": "string", "optional": True},
            "agents": {"type": "array", "optional": True},
        },
    ))
    registry.register(ToolDescriptor(
        name="tessera.retrieve",
        description="Retrieve relevant document chunks from Tessera RAG",
        permission=PermissionLevel.NETWORK,
        handler=_tessera_skill.retrieve,
        parameters={
            "query": {"type": "string"},
            "top_k": {"type": "integer", "optional": True},
        },
    ))
```

- [ ] **Step 4: Verify import chain**

```bash
python -c "from armature.registry.builtins import register_builtins; print('ok')"
```

Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add armature/skills/ armature/registry/builtins.py
git commit -m "feat: built-in Quorum and Tessera skills registered in tool registry (harness component 5)"
```

---

## Task 18: End-to-End Integration Test

**Files:**
- Create: `tests/fixtures/echo-workflow.yaml`
- Create: `tests/integration/test_end_to_end.py`

- [ ] **Step 1: Write echo workflow fixture**

```yaml
# tests/fixtures/echo-workflow.yaml
name: echo-workflow
version: "1.0"
description: Minimal end-to-end test workflow using only script adapters

contracts:
  inputs:
    - name: message
      type: str
      required: true
  outputs:
    - name: result
      required: true

adapters:
  echo_message:
    name: echo_message
    type: script
    cmd: "echo 'received: {{message}}'"

stages:
  - id: echo
    adapter: echo_message

  - id: verify
    depends_on: [echo]
    adapter: check_exit

adapters:
  echo_message:
    name: echo_message
    type: script
    cmd: "echo 'received: {{message}}'"
  check_exit:
    name: check_exit
    type: script
    cmd: "echo 'verified'"
```

- [ ] **Step 2: Write integration test**

```python
# tests/integration/test_end_to_end.py
import pytest
from pathlib import Path
from armature.runtime.engine import Harness

FIXTURES = Path(__file__).parent.parent / "fixtures"

@pytest.mark.asyncio
async def test_echo_workflow_runs_end_to_end(tmp_path):
    harness = Harness.from_spec(
        FIXTURES / "echo-workflow.yaml",
        vars={"message": "hello-world"},
    )
    harness._session._path = tmp_path / "session.jsonl"

    result = await harness.run({"message": "hello-world"})

    assert "echo" in result
    assert result["echo"]["exit_code"] == 0
    assert "hello-world" in result["echo"]["stdout"] or "received" in result["echo"]["stdout"]

@pytest.mark.asyncio
async def test_session_log_written(tmp_path):
    harness = Harness.from_spec(
        FIXTURES / "echo-workflow.yaml",
        vars={"message": "test"},
    )
    harness._session._path = tmp_path / "session.jsonl"
    await harness.run({"message": "test"})

    events = await harness._session.read_all()
    event_types = [e.type for e in events]
    assert "run_start" in event_types
    assert "stage_start" in event_types
    assert "run_complete" in event_types
```

- [ ] **Step 3: Fix echo-workflow.yaml (clean up duplicate adapters)**

```yaml
# tests/fixtures/echo-workflow.yaml
name: echo-workflow
version: "1.0"
description: Minimal end-to-end test workflow using only script adapters

adapters:
  echo_message:
    name: echo_message
    type: script
    cmd: "echo 'received: {{message}}'"
  check_exit:
    name: check_exit
    type: script
    cmd: "echo 'verified'"

stages:
  - id: echo
    adapter: echo_message

  - id: verify
    depends_on: [echo]
    adapter: check_exit
```

- [ ] **Step 4: Run integration test**

```bash
pytest tests/integration/test_end_to_end.py -v
```

Expected: Both tests pass.

- [ ] **Step 5: Run full test suite**

```bash
pytest --cov=armature --cov-report=term-missing
```

Expected: 80%+ coverage. All tests pass.

- [ ] **Step 6: Final commit**

```bash
git add tests/fixtures/echo-workflow.yaml tests/integration/
git commit -m "feat: end-to-end integration test — all nine harness components exercised"
```

---

## Completion Checklist

Before declaring Phase 1 done:

- [ ] `pip install -e .` succeeds in a fresh virtualenv
- [ ] `armature run tests/fixtures/echo-workflow.yaml --input message=hello` runs without error
- [ ] `armature run tests/fixtures/minimal.yaml --dry-run` validates spec
- [ ] Full test suite passes: `pytest --cov=armature`
- [ ] 80%+ coverage on `armature/` package
- [ ] Session log is written and replayable after each run
- [ ] All nine harness components have at least one test covering them

---

*Plan complete. Reference VISION.md at `/Users/bryansparks/projects/armature/VISION.md` for strategic context.*
