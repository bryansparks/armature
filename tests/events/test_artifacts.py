import json

import pytest

from armature.events.cli import main
from armature.events.emitter import (
    ARTIFACT_PART_BYTES,
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACT_PARTS,
    MAX_EVENT_BYTES,
    Emitter,
    artifact_parts,
)
from tests.events.support import DatagramReader


@pytest.fixture
def reader(monkeypatch):
    r = DatagramReader()
    monkeypatch.setenv("ARMATURE_EVENTS", r.endpoint)
    monkeypatch.setenv("ARMATURE_EVENTS_TOKEN", "tok-1")
    monkeypatch.delenv("ARMATURE_STEP_ID", raising=False)
    yield r
    r.close()


def _joined(events):
    assert {e["kind"] for e in events} == {"artifact"}
    ordered = sorted(events, key=lambda e: e["payload"]["part"])
    assert len({e["payload"]["upload"] for e in ordered}) == 1
    assert [e["payload"]["part"] for e in ordered] == list(range(ordered[0]["payload"]["parts"]))
    return "".join(e["payload"]["content"] for e in ordered)


def test_parts_fit_an_event_even_when_escaping_grows_them():
    for content in ("", "plain " * 50_000, "\x01" * 200_000, "é\"\\\n" * 100_000, "😀" * 200_000):
        parts = artifact_parts(content)
        assert "".join(parts) == content
        assert len(parts) <= MAX_ARTIFACT_PARTS
        for part in parts:
            assert len(json.dumps(part, ensure_ascii=False).encode("utf-8", "surrogatepass")) <= ARTIFACT_PART_BYTES


def test_cli_sends_text_with_the_step_from_the_environment(reader, monkeypatch):
    monkeypatch.setenv("ARMATURE_STEP_ID", "post")
    assert main(["artifact", "--name", "review.md", "--text", "# Review\n\nAll good."]) == 0
    [event] = reader.wait_for(1)
    assert event["step_id"] == "post"
    assert event["payload"] == {"name": "review.md", "media_type": "text/markdown", "upload": event["payload"]["upload"],
                                "part": 0, "parts": 1, "content": "# Review\n\nAll good."}


def test_cli_sends_a_large_file_in_parts(reader, tmp_path):
    body = json.dumps({"rows": [{"i": i, "text": "x" * 40} for i in range(6000)]})
    path = tmp_path / "out.json"
    path.write_text(body)
    assert main(["artifact", "--name", "out.json", "--type", "application/json", "--file", str(path),
                 "--step", "lead"]) == 0
    parts = len(artifact_parts(body))
    assert parts > 1
    events = reader.wait_for(parts)
    assert all(len(raw) <= MAX_EVENT_BYTES for raw in reader.raw)
    assert all(e["step_id"] == "lead" and e["payload"]["media_type"] == "application/json" for e in events)
    assert _joined(events) == body


@pytest.mark.parametrize("argv, message", [
    (["artifact", "--text", "x"], "needs --name"),
    (["artifact", "--name", "r.md"], "needs --name"),
    (["artifact", "--name", "../r.md", "--text", "x"], "artifact name"),
    (["artifact", "--name", "r.md", "--type", "text/html", "--text", "x"], "invalid choice"),
    (["artifact", "--name", "r.md", "--text", "x", "--file", "f"], "not allowed with"),
    (["artifact", "--name", "r.md", "--text", "x", "--field", "a=b"], "not --json or --field"),
    (["artifact", "--name", "r.md", "--file", "/nonexistent/x"], "could not read"),
    (["log", "--name", "r.md"], "go with the artifact kind"),
])
def test_cli_refuses_bad_arguments(reader, capsys, argv, message):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_cli_refuses_an_oversize_artifact(reader, tmp_path, capsys):
    path = tmp_path / "big.md"
    path.write_text("a" * (MAX_ARTIFACT_BYTES + 1))
    with pytest.raises(SystemExit):
        main(["artifact", "--name", "big.md", "--file", str(path)])
    assert "at most" in capsys.readouterr().err
    assert reader.events == []


def test_emitter_redacts_the_whole_artifact_before_cutting_it(reader, monkeypatch):
    monkeypatch.setenv("MY_SECRET", "hunter2-super-secret")
    monkeypatch.setenv("ARMATURE_REDACT_ENV", "MY_SECRET")
    content = "a" * (ARTIFACT_PART_BYTES - 5) + "hunter2-super-secret" + " Bearer abc.def"
    emitter = Emitter.from_env()
    assert emitter.artifact("notes.txt", content, "text/plain")
    emitter.close()
    joined = _joined(reader.wait_for(2))
    assert "hunter2" not in joined and "abc.def" not in joined
    assert joined.endswith("[redacted] Bearer [redacted]")


def test_null_emitter_and_closed_emitter_send_nothing(reader, monkeypatch):
    monkeypatch.delenv("ARMATURE_EVENTS")
    assert Emitter.from_env().artifact("r.md", "x") is False
    emitter = Emitter(reader.endpoint, "t")
    emitter.close()
    assert emitter.artifact("r.md", "x") is False
