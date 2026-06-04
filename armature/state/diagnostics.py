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


class DiagnosticResult(BaseModel):
    code: DiagnosticCode
    stage_id: str
    details: str = ""


class DiagnosticAnalyzer:
    def __init__(self, traces: list[TraceRecord]) -> None:
        self._traces = traces

    def analyze(self) -> list[DiagnosticResult]:
        results: list[DiagnosticResult] = []
        for t in self._traces:
            if not t.success:
                results.append(DiagnosticResult(
                    code=DiagnosticCode.STAGE_FAILED,
                    stage_id=t.stage_id,
                    details=t.error_type or "",
                ))
            if not t.output_valid:
                results.append(DiagnosticResult(
                    code=DiagnosticCode.OUTPUT_INVALID,
                    stage_id=t.stage_id,
                    details="output failed schema validation",
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
                ))
            if t.escalation_count >= _HIGH_ESCALATION_THRESHOLD:
                results.append(DiagnosticResult(
                    code=DiagnosticCode.HIGH_ESCALATION,
                    stage_id=t.stage_id,
                    details=f"escalation_count={t.escalation_count}",
                ))
            if t.error_type == "PostconditionFailed":
                results.append(DiagnosticResult(
                    code=DiagnosticCode.POSTCONDITION_FAILED,
                    stage_id=t.stage_id,
                    details="tool postcondition failed",
                ))
            if t.tools_declared and not t.tools_called:
                results.append(DiagnosticResult(
                    code=DiagnosticCode.LOW_SKILL_ACTIVATION,
                    stage_id=t.stage_id,
                    details=f"declared={t.tools_declared}, called=[]",
                ))
        return results
