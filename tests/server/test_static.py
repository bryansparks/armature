import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "armature" / "server" / "static"
JS_TESTS = Path(__file__).resolve().parent / "js"
PAGES = ["index.html", "login.html", "invite.html"]
SVG_NAMESPACE = "http://www.w3.org/2000/svg"

CONTRACT = [
    "GET /api/session",
    "POST /api/login",
    "GET /auth/oidc/login",
    "POST /api/logout",
    "POST /api/step-up",
    "POST /api/account/password",
    "POST /api/invite/inspect",
    "POST /api/invite/accept",
    "GET /api/teams",
    "POST /api/teams",
    "GET /api/teams/{id}/projects",
    "POST /api/teams/{id}/projects",
    "GET /api/projects/{id}",
    "GET /api/users",
    "POST /api/users",
    "POST /api/users/{id}",
    "POST /api/users/{id}/invite",
    "GET /api/users/lookup",
    "POST /api/memberships",
    "POST /api/memberships/{id}/delete",
    "GET /api/teams/{id}/secrets",
    "POST /api/teams/{id}/secrets",
    "POST /api/teams/{id}/secrets/{name}/delete",
    "GET /api/secrets",
    "POST /api/secrets",
    "POST /api/secrets/{name}/delete",
    "GET /api/teams/{id}/runner-keys",
    "POST /api/teams/{id}/runner-keys",
    "POST /api/teams/{id}/runner-keys/{kid}/delete",
    "GET /api/runner-keys",
    "POST /api/runner-keys",
    "POST /api/runner-keys/{id}/delete",
    "GET /api/runs",
    "GET /api/runs/{id}",
    "GET /api/runs/{id}/events",
    "GET /api/runs/{id}/stream",
    "GET /api/runs/{id}/findings",
    "GET /api/runs/{id}/artifacts",
    "GET /api/runs/{id}/artifacts/{name}",
    "GET /api/runs/{id}/outputs",
    "GET /api/stream/runs",
    "GET /api/workflows",
    "GET /api/workflows/{id}",
    "GET /api/workflows/{id}/versions/{version}",
    "GET /api/workflows/{id}/diff",
    "POST /api/workflows/validate",
    "POST /api/workflows/parse",
    "POST /api/workflows/render",
    "POST /api/workflows",
    "POST /api/workflows/{id}",
    "POST /api/workflows/{id}/fork",
    "POST /api/workflows/{id}/rollback",
    "POST /api/workflows/{id}/delete",
    "POST /api/workflows/{id}/test",
    "POST /api/assist",
    "GET /api/documents",
    "GET /api/documents/{id}",
    "GET /api/documents/{id}/versions/{version}",
    "GET /api/documents/{id}/diff",
    "POST /api/documents/validate",
    "POST /api/documents",
    "POST /api/documents/{id}",
    "POST /api/documents/{id}/fork",
    "POST /api/documents/{id}/rollback",
    "POST /api/documents/{id}/delete",
    "GET /api/packs",
    "GET /api/teams/{id}/providers",
    "POST /api/teams/{id}/providers",
    "POST /api/providers/{id}",
    "POST /api/providers/{id}/delete",
    "GET /api/settings",
    "POST /api/settings",
    "POST /api/settings/delete",
    "GET /api/display",
    "GET /api/overview",
    "GET /api/trends",
    "GET /api/runs/{id}/actions",
    "POST /api/runs/{id}/actions/{action}",
    "GET /api/operations",
    "GET /api/status",
    "GET /api/subjects",
    "GET /api/subjects/{subject}",
]
CONTRACT_PATTERNS = [re.compile("^" + re.sub(r"\\\{\w+\\\}", "[^/]+", re.escape(entry.split(" ", 1)[1])) + "$") for entry in CONTRACT]

IMPORT = re.compile(r"""(?:\bfrom\s+|\bimport\s*\(\s*)['"](\.{1,2}/[^'"]+)['"]""")
HTML_REF = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']+)["']""")
API_LITERAL = re.compile(r"""['"`](/(?:api|auth)/[^'"`]*)['"`]""")
SINKS = re.compile(r"\.(?:innerHTML|outerHTML)\s*[+]?=|insertAdjacentHTML|document\.write")


def _static_files(*suffixes):
    return sorted(path for path in STATIC.rglob("*") if path.suffix in suffixes)


def _page_refs(page):
    return [ref for ref in HTML_REF.findall((STATIC / page).read_text()) if not ref.startswith("data:")]


def test_pages_exist():
    for page in PAGES:
        assert (STATIC / page).is_file()


@pytest.mark.parametrize("page", PAGES)
def test_page_references_exist(page):
    refs = _page_refs(page)
    assert refs
    for ref in refs:
        assert ref.startswith("static/"), f"{page} references {ref} outside static/"
        assert (STATIC / ref.removeprefix("static/")).is_file(), f"{page} references missing {ref}"


def test_module_imports_exist():
    seen = set()
    pending = [STATIC / ref.removeprefix("static/") for page in PAGES for ref in _page_refs(page) if ref.endswith(".js")]
    while pending:
        module = pending.pop().resolve()
        if module in seen:
            continue
        seen.add(module)
        for spec in IMPORT.findall(module.read_text()):
            target = (module.parent / spec).resolve()
            assert target.is_file(), f"{module.relative_to(STATIC)} imports missing {spec}"
            assert STATIC.resolve() in target.parents
            pending.append(target)
    unreached = {path.resolve() for path in _static_files(".js")} - seen
    assert not unreached, f"modules nothing loads: {sorted(str(p) for p in unreached)}"


@pytest.mark.parametrize("path", _static_files(".js", ".html"), ids=lambda path: str(path.relative_to(STATIC)))
def test_no_html_sinks(path):
    assert not SINKS.findall(path.read_text())


@pytest.mark.parametrize("path", _static_files(".js", ".html", ".css"), ids=lambda path: str(path.relative_to(STATIC)))
def test_no_external_urls(path):
    text = path.read_text().replace(SVG_NAMESPACE, "")
    assert not re.search(r"https?://", text)
    assert "url(" not in text or path.suffix != ".css"
    if path.suffix == ".html":
        assert not [ref for ref in HTML_REF.findall(text) if ref.startswith("//")]


def test_api_paths_are_in_the_contract():
    used = set()
    for path in _static_files(".js"):
        for literal in API_LITERAL.findall(path.read_text()):
            used.add(re.sub(r"\$\{[^}]*\}", "x", literal).split("?", 1)[0])
    assert "/api/session" in used
    unknown = sorted(path for path in used if not any(pattern.match(path) for pattern in CONTRACT_PATTERNS))
    assert not unknown, f"paths not in the API contract: {unknown}"


def _node():
    return shutil.which("node") or next((path for path in ["/opt/homebrew/bin/node", "/usr/local/bin/node"] if Path(path).is_file()), None)


def test_js_helpers_under_node():
    node = _node()
    if not node:
        pytest.skip("node is not installed")
    tests = sorted(str(path) for path in JS_TESTS.glob("*.test.mjs"))
    result = subprocess.run([node, "--test", *tests], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
