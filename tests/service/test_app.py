import pytest
from pathlib import Path
from httpx import AsyncClient, ASGITransport

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def app():
    from armature.service.app import app
    return app


# ── health ────────────────────────────────────────────────────────────────────

async def test_health_check(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body


# ── /run success ──────────────────────────────────────────────────────────────

async def test_run_workflow(app, tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/run", json={
            "spec_path": str(FIXTURES / "echo-workflow.yaml"),
            "inputs": {"message": "http-test"},
            "session_dir": str(tmp_path),
        })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "complete"
    assert "echo" in body["result"]
    assert body["result"]["echo"]["exit_code"] == 0


async def test_run_response_includes_run_id(app, tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/run", json={
            "spec_path": str(FIXTURES / "echo-workflow.yaml"),
            "inputs": {"message": "run-id-test"},
            "session_dir": str(tmp_path),
        })
    assert response.status_code == 200
    body = response.json()
    assert "run_id" in body
    assert body["run_id"]  # non-empty


async def test_run_without_session_dir(app):
    """session_dir is optional; service picks a default."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/run", json={
            "spec_path": str(FIXTURES / "echo-workflow.yaml"),
            "inputs": {"message": "no-session-dir"},
        })
    assert response.status_code == 200
    assert response.json()["status"] == "complete"


async def test_run_result_contains_all_stages(app, tmp_path):
    """Both stages of echo-workflow appear in the result."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/run", json={
            "spec_path": str(FIXTURES / "echo-workflow.yaml"),
            "inputs": {"message": "all-stages"},
            "session_dir": str(tmp_path),
        })
    assert response.status_code == 200
    result = response.json()["result"]
    assert "echo" in result
    assert "verify" in result


# ── /run errors ───────────────────────────────────────────────────────────────

async def test_run_missing_spec(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/run", json={
            "spec_path": "/nonexistent/spec.yaml",
            "inputs": {},
        })
    assert response.status_code == 404


async def test_run_invalid_spec_content_returns_500(app, tmp_path):
    """A spec that loads but fails validation returns 500."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: bad\nstages:\n  - id: s\n    depends_on: []\n")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/run", json={
            "spec_path": str(bad),
            "inputs": {},
            "session_dir": str(tmp_path / "session"),
        })
    assert response.status_code == 500


async def test_run_missing_required_input_returns_500(app, tmp_path):
    """Contract.inputs enforcement — missing required input gives 500."""
    import yaml
    spec = {
        "name": "wf",
        "stages": [{"id": "s", "tool_call": {"name": "noop"}, "depends_on": []}],
        "contracts": {"inputs": [{"name": "repo", "required": True}]},
    }
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.dump(spec))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/run", json={
            "spec_path": str(spec_path),
            "inputs": {},  # repo is missing
            "session_dir": str(tmp_path / "session"),
        })
    assert response.status_code == 500
    assert "repo" in response.json()["detail"]


async def test_health_returns_version(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.json()["version"] == "0.1.0"


async def test_run_spec_not_found_detail_contains_path(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/run", json={
            "spec_path": "/does/not/exist.yaml",
            "inputs": {},
        })
    assert response.status_code == 404
    assert "exist.yaml" in response.json()["detail"]


async def test_run_result_echo_stdout_contains_message(app, tmp_path):
    """The echo stage stdout should contain the message context variable."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/run", json={
            "spec_path": str(FIXTURES / "echo-workflow.yaml"),
            "inputs": {"message": "unique-payload-abc"},
            "session_dir": str(tmp_path),
        })
    assert response.status_code == 200
    echo_stdout = response.json()["result"]["echo"]["stdout"]
    assert "unique-payload-abc" in echo_stdout
