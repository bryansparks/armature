"""Call bundles, resolvers, and call-graph checks."""
import pytest

from armature.spec.calls import (
    BundleCallResolver, CallNotFound, CallResolver, MappingCallResolver, ResolvedCall,
    call_graph, write_call_bundle,
)
from armature.spec.loader import load_spec
from armature.spec.models import HarnessSpec, Stage, CallRef


def _callee(name: str, calls: list[str] = ()) -> str:
    stages = "".join(f"  - id: c{i}\n    call: {{workflow: {c}}}\n" for i, c in enumerate(calls))
    return (f"# armature: stored-workflow\nname: {name}\nstages:\n"
            + (stages or "  - id: s\n    gate: human\n"))


def test_bundle_round_trip_with_pins(tmp_path):
    write_call_bundle(tmp_path / "calls", [
        ResolvedCall("review", 2, _callee("review")),
        ResolvedCall("review", 3, _callee("review")),
    ], {"review": 3})
    resolver = BundleCallResolver(tmp_path / "calls")
    assert isinstance(resolver, CallResolver)
    assert resolver.resolve("review", None).version == 3
    assert resolver.resolve("review", 2).version == 2
    assert (tmp_path / "calls" / "review@2.yaml").is_file()


@pytest.mark.parametrize("name,version", [("absent", None), ("review", 9), ("../etc", 1)])
def test_bundle_resolver_raises_call_not_found(tmp_path, name, version):
    write_call_bundle(tmp_path / "calls", [ResolvedCall("review", 1, _callee("review"))], {"review": 1})
    with pytest.raises(CallNotFound):
        BundleCallResolver(tmp_path / "calls").resolve(name, version)


def test_write_call_bundle_rejects_path_names(tmp_path):
    with pytest.raises(ValueError, match="not a valid workflow name"):
        write_call_bundle(tmp_path, [ResolvedCall("../x", 1, "")], {})


def test_for_spec_uses_the_loaded_file_directory(tmp_path):
    path = tmp_path / "wf.yaml"
    path.write_text("name: wf\nstages:\n  - id: s\n    gate: human\n")
    assert BundleCallResolver.for_spec(load_spec(path)).root == tmp_path.resolve() / "calls"


def _root(*calls: str) -> HarnessSpec:
    return HarnessSpec(name="root", stages=[
        Stage(id=f"s{i}", call=CallRef(workflow=c)) for i, c in enumerate(calls)
    ])


def test_call_graph_collects_reachable_workflows():
    resolver = MappingCallResolver({
        ("a", 1): _callee("a", ["b"]),
        ("b", 1): _callee("b"),
    }, {"a": 1, "b": 1})
    calls, errors = call_graph(_root("a", "b"), resolver)
    assert errors == []
    assert sorted((c.workflow, c.version) for c in calls) == [("a", 1), ("b", 1)]


def test_call_graph_reports_cycles_and_missing():
    resolver = MappingCallResolver({
        ("a", 1): _callee("a", ["b"]),
        ("b", 1): _callee("b", ["a", "ghost"]),
    }, {"a": 1, "b": 1})
    _, errors = call_graph(_root("a"), resolver)
    codes = sorted(e.code for e in errors)
    assert codes == ["CALL_CYCLE", "CALL_UNRESOLVED"]
    cycle = next(e for e in errors if e.code == "CALL_CYCLE")
    assert cycle.message == "call cycle: root -> a@1 -> b@1 -> a@1"


def test_call_graph_reports_a_call_back_to_the_root():
    resolver = MappingCallResolver({("a", 1): _callee("a", ["root"]), ("root", 1): _callee("root")},
                                   {"a": 1, "root": 1})
    _, errors = call_graph(_root("a"), resolver)
    assert [e.code for e in errors] == ["CALL_CYCLE"]


@pytest.mark.parametrize("target", ["outside", "inside"])
def test_bundle_resolver_rejects_symlinked_callee(tmp_path, target):
    root = tmp_path / "calls"
    write_call_bundle(root, [ResolvedCall("real", 1, _callee("real"))], {"evil": 1})
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text(_callee("evil"))
    (root / "evil@1.yaml").symlink_to(outside if target == "outside" else root / "real@1.yaml")
    with pytest.raises(CallNotFound, match="not a plain file"):
        BundleCallResolver(root).resolve("evil", None)
