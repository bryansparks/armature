import pytest
from pathlib import Path
from httpx import AsyncClient, ASGITransport

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def app():
    from armature.service.app import app
    return app


async def test_health_check(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


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


async def test_run_missing_spec(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/run", json={
            "spec_path": "/nonexistent/spec.yaml",
            "inputs": {},
        })
    assert response.status_code == 404
