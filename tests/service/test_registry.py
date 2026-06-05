"""Tests for WorkflowRegistry and the /workflows API routes."""
import pytest
import yaml
from pathlib import Path
from httpx import AsyncClient, ASGITransport

from armature.service.registry import WorkflowRegistry
from armature.spec.models import HarnessSpec, Stage, Role, RoleType


def _write_spec(tmp_path: Path, name: str, description: str = "") -> Path:
    spec = {
        "name": name,
        "version": "1.0",
        "description": description,
        "stages": [
            {"id": "s1", "role": {"name": "r", "type": "worker", "description": "do"}}
        ],
    }
    p = tmp_path / f"{name}.yaml"
    p.write_text(yaml.dump(spec))
    return p


# ── WorkflowRegistry unit tests ──────────────────────────────────────────────

def test_registry_load_dir_indexes_by_name(tmp_path):
    _write_spec(tmp_path, "alpha")
    _write_spec(tmp_path, "beta")
    reg = WorkflowRegistry()
    reg.load_dir(tmp_path)
    assert reg.get("alpha") is not None
    assert reg.get("beta") is not None


def test_registry_get_returns_none_for_unknown():
    reg = WorkflowRegistry()
    assert reg.get("nonexistent") is None


def test_registry_list_all_returns_metadata(tmp_path):
    _write_spec(tmp_path, "gamma", description="a workflow")
    reg = WorkflowRegistry()
    reg.load_dir(tmp_path)
    items = reg.list_all()
    assert len(items) == 1
    assert items[0]["name"] == "gamma"
    assert items[0]["description"] == "a workflow"
    assert items[0]["stages"] == 1


def test_registry_register_adds_spec():
    reg = WorkflowRegistry()
    spec = HarnessSpec(
        name="inline",
        stages=[Stage(id="s1", role=Role(name="r", type=RoleType.WORKER, description="do"))],
    )
    reg.register(spec)
    assert reg.get("inline") is not None


def test_registry_skips_invalid_files(tmp_path):
    (tmp_path / "bad.yaml").write_text("this: is: not: valid: yaml: [[[")
    _write_spec(tmp_path, "good")
    reg = WorkflowRegistry()
    reg.load_dir(tmp_path)
    assert reg.get("good") is not None
    assert len(reg.list_all()) == 1


# ── /workflows HTTP route tests ───────────────────────────────────────────────

def _make_app(tmp_path: Path) -> "fastapi.FastAPI":
    """Build a test FastAPI app with a pre-loaded registry."""
    from armature.service.app import build_app
    _write_spec(tmp_path, "wf-one", description="first")
    _write_spec(tmp_path, "wf-two", description="second")
    reg = WorkflowRegistry()
    reg.load_dir(tmp_path)
    return build_app(registry=reg)


async def test_get_workflows_lists_all(tmp_path):
    app = _make_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/workflows")
    assert resp.status_code == 200
    names = {w["name"] for w in resp.json()}
    assert {"wf-one", "wf-two"} == names


async def test_get_workflow_by_name(tmp_path):
    app = _make_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/workflows/wf-one")
    assert resp.status_code == 200
    assert resp.json()["name"] == "wf-one"
    assert resp.json()["description"] == "first"


async def test_get_workflow_unknown_returns_404(tmp_path):
    app = _make_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/workflows/does-not-exist")
    assert resp.status_code == 404


async def test_post_workflow_run_returns_result(tmp_path):
    from unittest.mock import AsyncMock, patch
    app = _make_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with patch("armature.service.app.Harness") as MockHarness:
            instance = MockHarness.return_value
            instance.run = AsyncMock(return_value={"answer": "42"})
            instance._run_id = "abc123"
            resp = await client.post("/workflows/wf-one/run", json={"inputs": {"q": "test"}})
    assert resp.status_code == 200
    assert resp.json()["status"] == "complete"
    assert resp.json()["result"]["answer"] == "42"


async def test_post_workflow_run_unknown_returns_404(tmp_path):
    app = _make_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/workflows/ghost/run", json={"inputs": {}})
    assert resp.status_code == 404
