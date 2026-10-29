from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING, Any
from armature.nodes.base import BaseNode
from armature.spec.calls import CallResolver, load_callee
from armature.spec.contract_io import project_outputs, resolve_inputs, typed_outputs
from armature.spec.models import HarnessSpec, Stage

if TYPE_CHECKING:
    from armature.events import Emitter


def render_call_with(values: Any, context: dict[str, Any]) -> Any:
    """Render with: for a call. A lone reference keeps its value as is, so a string is never
    parsed into a bool or list; resolve_inputs does the coercion."""
    from armature.packs.params import is_template, render_template

    if isinstance(values, dict):
        return {k: render_call_with(v, context) for k, v in values.items()}
    if isinstance(values, list):
        return [render_call_with(v, context) for v in values]
    if not is_template(values):
        return values
    return render_template(values, context, native=False)


def final_stage_id(spec: HarnessSpec) -> str | None:
    """The last non-post_run stage the spec declares, whose output a call returns without typed outputs."""
    normal = [s.id for s in spec.stages if not s.post_run]
    return normal[-1] if normal else None


class CallNode(BaseNode):
    """Runs a stored workflow named by a `call:` stage as an isolated child run.

    The callee sees only its rendered `with:` inputs, coerced to the types it
    declares. Its result is its typed `contracts.outputs`, or its final stage's
    output when it declares none.
    """

    def __init__(
        self,
        stage: Stage,
        resolver: CallResolver,
        session_dir: Path | None = None,
        emitter: Emitter | None = None,
        parent_step: str | None = None,
        engine_key: tuple[str, str] | None = None,
    ):
        if stage.call is None:
            raise ValueError(f"Stage '{stage.id}' has no call")
        self._stage = stage
        self._resolver = resolver
        self._session_dir = session_dir
        self._emitter = emitter
        self._parent_step = parent_step
        self._engine_key = engine_key
        self.total_cost_usd = 0.0
        self.resolved_version: int | None = None

    def _child_dir(self, context: dict[str, Any]) -> Path | None:
        if self._session_dir is None:
            return None
        child_dir = self._session_dir / f"stage_{self._stage.id}"
        if self._stage.loop is not None:
            info = context.get(self._stage.loop.iteration_var)
            if isinstance(info, dict) and isinstance(info.get("num"), int):
                child_dir = child_dir / f"iter_{info['num']}"
        idx = context.get("_fan_out_item_index")
        child_dir = child_dir / f"child_{idx if isinstance(idx, int) and idx >= 0 else 0}"
        child_dir.mkdir(parents=True, exist_ok=True)
        return child_dir

    async def execute(self, context: dict[str, Any]) -> Any:
        from armature.runtime.engine import Harness

        ref = self._stage.call
        resolved = self._resolver.resolve(ref.workflow, ref.version)
        self.resolved_version = resolved.version
        # Loaded with templates inert: carried values reach the callee only as
        # context, so a value holding "{{ ... }}" is never rendered as a template.
        spec = load_callee(resolved)
        inputs = resolve_inputs(spec.contracts.inputs, render_call_with(self._stage.with_ or {}, context))
        child = Harness(
            spec=spec,
            session_dir=self._child_dir(context),
            emitter=self._emitter,
            parent_step=self._parent_step,
            call_resolver=self._resolver,
            engine_key=self._engine_key,
        )
        try:
            results = await child.run(inputs)
        finally:
            self.total_cost_usd += getattr(child, "total_cost_usd", 0.0)
        if typed_outputs(spec.contracts.outputs):
            return project_outputs(spec.contracts.outputs, results)
        final = final_stage_id(spec)
        return results.get(final) if final is not None else {}
