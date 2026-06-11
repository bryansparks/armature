from __future__ import annotations
from enum import Enum
from pydantic import BaseModel
from armature.state.traces import TraceRecord

_LOW_CONFIDENCE_THRESHOLD = 0.30
_HIGH_ESCALATION_THRESHOLD = 2


class DiagnosticCode(str, Enum):
    STAGE_FAILED = "stage_failed"
    OUTPUT_INVALID = "output_invalid"
    LOW_CONFIDENCE = "low_confidence"
    HIGH_ESCALATION = "high_escalation"
    POSTCONDITION_FAILED = "postcondition_failed"
    LOW_SKILL_ACTIVATION = "low_skill_activation"


class TerminalCause(str, Enum):
    EXECUTION_ERROR = "execution_error"
    SCHEMA_VALIDATION = "schema_validation"
    LOW_CONFIDENCE = "low_confidence"
    SCHEMA_ESCALATION = "schema_escalation"
    POSTCONDITION = "postcondition"
    PROMPT_WEAK = "prompt_weak"


class CausalStatus(str, Enum):
    SPEC_PROBLEM = "spec_problem"
    MODEL_PROBLEM = "model_problem"
    TOOL_PROBLEM = "tool_problem"


class FailureMechanism(str, Enum):
    TIMEOUT = "timeout"
    RUNTIME_ERROR = "runtime_error"
    SCHEMA_TOO_STRICT = "schema_too_strict"
    MODEL_UNDERPOWERED = "model_underpowered"
    JUDGE_UNCERTAIN = "judge_uncertain"
    TIER_INSUFFICIENT = "tier_insufficient"
    TOOL_VIOLATION = "tool_violation"
    PROMPT_MISSING_INSTRUCTION = "prompt_missing_instruction"


class CausalAttribution(BaseModel):
    terminal_cause: TerminalCause
    causal_status: CausalStatus
    mechanism: FailureMechanism


class DiagnosticResult(BaseModel):
    code: DiagnosticCode
    stage_id: str
    details: str = ""
    causal_attribution: CausalAttribution | None = None


def _attr_stage_failed(error_type: str | None) -> CausalAttribution:
    if error_type and "Timeout" in error_type:
        return CausalAttribution(
            terminal_cause=TerminalCause.EXECUTION_ERROR,
            causal_status=CausalStatus.SPEC_PROBLEM,
            mechanism=FailureMechanism.TIMEOUT,
        )
    return CausalAttribution(
        terminal_cause=TerminalCause.EXECUTION_ERROR,
        causal_status=CausalStatus.MODEL_PROBLEM,
        mechanism=FailureMechanism.RUNTIME_ERROR,
    )


def _attr_output_invalid(escalation_count: int) -> CausalAttribution:
    if escalation_count >= _HIGH_ESCALATION_THRESHOLD:
        return CausalAttribution(
            terminal_cause=TerminalCause.SCHEMA_VALIDATION,
            causal_status=CausalStatus.MODEL_PROBLEM,
            mechanism=FailureMechanism.MODEL_UNDERPOWERED,
        )
    return CausalAttribution(
        terminal_cause=TerminalCause.SCHEMA_VALIDATION,
        causal_status=CausalStatus.SPEC_PROBLEM,
        mechanism=FailureMechanism.SCHEMA_TOO_STRICT,
    )


class DiagnosticAnalyzer:
    def __init__(self, traces: list[TraceRecord]) -> None:
        self._traces = traces

    def analyze(self) -> list[DiagnosticResult]:
        results: list[DiagnosticResult] = []
        for t in self._traces:
            if not t.success and t.error_type != "PostconditionFailed":
                results.append(DiagnosticResult(
                    code=DiagnosticCode.STAGE_FAILED,
                    stage_id=t.stage_id,
                    details=t.error_type or "",
                    causal_attribution=_attr_stage_failed(t.error_type),
                ))
            if not t.output_valid:
                results.append(DiagnosticResult(
                    code=DiagnosticCode.OUTPUT_INVALID,
                    stage_id=t.stage_id,
                    details="output failed schema validation",
                    causal_attribution=_attr_output_invalid(t.escalation_count),
                ))
            if (
                t.role_type == "judge"
                and t.quorum_score is not None
                and t.quorum_score < _LOW_CONFIDENCE_THRESHOLD
            ):
                results.append(DiagnosticResult(
                    code=DiagnosticCode.LOW_CONFIDENCE,
                    stage_id=t.stage_id,
                    details=f"confidence={t.quorum_score:.2f}",
                    causal_attribution=CausalAttribution(
                        terminal_cause=TerminalCause.LOW_CONFIDENCE,
                        causal_status=CausalStatus.MODEL_PROBLEM,
                        mechanism=FailureMechanism.JUDGE_UNCERTAIN,
                    ),
                ))
            if t.escalation_count >= _HIGH_ESCALATION_THRESHOLD:
                results.append(DiagnosticResult(
                    code=DiagnosticCode.HIGH_ESCALATION,
                    stage_id=t.stage_id,
                    details=f"escalation_count={t.escalation_count}",
                    causal_attribution=CausalAttribution(
                        terminal_cause=TerminalCause.SCHEMA_ESCALATION,
                        causal_status=CausalStatus.MODEL_PROBLEM,
                        mechanism=FailureMechanism.TIER_INSUFFICIENT,
                    ),
                ))
            if t.error_type == "PostconditionFailed":
                results.append(DiagnosticResult(
                    code=DiagnosticCode.POSTCONDITION_FAILED,
                    stage_id=t.stage_id,
                    details="tool postcondition failed",
                    causal_attribution=CausalAttribution(
                        terminal_cause=TerminalCause.POSTCONDITION,
                        causal_status=CausalStatus.TOOL_PROBLEM,
                        mechanism=FailureMechanism.TOOL_VIOLATION,
                    ),
                ))
            if t.tools_declared and not t.tools_called:
                results.append(DiagnosticResult(
                    code=DiagnosticCode.LOW_SKILL_ACTIVATION,
                    stage_id=t.stage_id,
                    details=f"declared={t.tools_declared}, called=[]",
                    causal_attribution=CausalAttribution(
                        terminal_cause=TerminalCause.PROMPT_WEAK,
                        causal_status=CausalStatus.SPEC_PROBLEM,
                        mechanism=FailureMechanism.PROMPT_MISSING_INSTRUCTION,
                    ),
                ))
        return results
