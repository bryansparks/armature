"""CLI tests for 'armature dashboard'."""
from __future__ import annotations
import json
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from typer.testing import CliRunner

from armature.cli import app
from armature.report.aggregator import (
    DashboardData, SafetyStats, load_safety_stats
)

runner = CliRunner()


def _make_empty_dashboard(name: str = "test-wf") -> DashboardData:
    return DashboardData(
        workflow_name=name,
        total_runs=0,
        traces=[],
        stage_stats={},
        improvement_cycles=[],
        safety_stats=load_safety_stats([]),
        hqs_trend=[],
        last_run_id=None,
    )


class TestDashboardCommand:
    def test_requires_spec_or_workflow(self):
        result = runner.invoke(app, ["dashboard"])
        assert result.exit_code != 0

    def test_json_format_outputs_valid_json(self, tmp_path):
        spec_file = tmp_path / "wf.yaml"
        spec_file.write_text(
            "name: test-wf\nversion: '1.0'\nstages:\n  - id: s1\n    role:\n"
            "      name: R\n      type: worker\n      description: d\n"
        )
        mock_data = _make_empty_dashboard("test-wf")
        with patch("armature.report.loader.load_dashboard_data", new=AsyncMock(return_value=mock_data)):
            result = runner.invoke(app, ["dashboard", str(spec_file), "--format", "json"])
        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed["workflow_name"] == "test-wf"
        assert "current_hqs" in parsed
        assert "stage_stats" in parsed
        assert "improvement_cycles" in parsed
        assert "safety" in parsed

    def test_workflow_name_flag_accepted(self):
        mock_data = _make_empty_dashboard("my-wf")
        with patch("armature.report.loader.load_dashboard_data", new=AsyncMock(return_value=mock_data)):
            result = runner.invoke(app, ["dashboard", "--workflow", "my-wf", "--format", "json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["workflow_name"] == "my-wf"

    def test_missing_spec_exits_nonzero(self, tmp_path):
        result = runner.invoke(app, ["dashboard", str(tmp_path / "ghost.yaml")])
        assert result.exit_code != 0

    def test_json_output_includes_hqs_trend(self, tmp_path):
        spec_file = tmp_path / "wf.yaml"
        spec_file.write_text(
            "name: trend-wf\nversion: '1.0'\nstages:\n  - id: s1\n    role:\n"
            "      name: R\n      type: worker\n      description: d\n"
        )
        mock_data = DashboardData(
            workflow_name="trend-wf",
            total_runs=3,
            traces=[],
            stage_stats={},
            improvement_cycles=[],
            safety_stats=load_safety_stats([]),
            hqs_trend=[0.70, 0.75, 0.80],
            last_run_id="r3",
        )
        with patch("armature.report.loader.load_dashboard_data", new=AsyncMock(return_value=mock_data)):
            result = runner.invoke(app, ["dashboard", str(spec_file), "--format", "json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["hqs_trend"] == [0.70, 0.75, 0.80]
        assert parsed["current_hqs"] == pytest.approx(0.80)
        assert parsed["hqs_delta"] == pytest.approx(0.05)


class TestRenderTerminalLayout:
    def _make_data(self, n_stages=6):
        from armature.report.aggregator import StageStats
        stats = {}
        for i in range(n_stages):
            stats[f"stage_{i}"] = StageStats(
                stage_id=f"stage_{i}", role_type="worker", run_count=5,
                failure_rate=0.0, avg_latency_ms=1200.0, avg_quorum=None,
                escalation_rate=0.0, is_post_run=False, fan_out_per_run=1,
            )
        return DashboardData(
            workflow_name="test-wf", total_runs=5, traces=[], stage_stats=stats,
            improvement_cycles=[], safety_stats=load_safety_stats([]),
            hqs_trend=[0.80], last_run_id="r1",
        )

    def test_render_terminal_does_not_fill_terminal_height(self):
        """render_terminal should use only as many lines as content needs."""
        from armature.report.layout import render_terminal
        from rich.console import Console
        from io import StringIO
        data = self._make_data(n_stages=6)
        sio = StringIO()
        c = Console(file=sio, width=130, height=40, highlight=False, markup=False)
        render_terminal(data, console=c)
        lines = sio.getvalue().split("\n")
        assert len(lines) < 35, f"Expected <35 lines for 6 stages, got {len(lines)}"

    def test_render_terminal_stage_rows_not_wrapping(self):
        """Each stage should occupy exactly 1 line (status glyph, not multi-word text)."""
        from armature.report.layout import render_terminal
        from rich.console import Console
        from io import StringIO
        data = self._make_data(n_stages=4)
        sio = StringIO()
        c = Console(file=sio, width=130, height=40, highlight=False, markup=False)
        render_terminal(data, console=c)
        out = sio.getvalue()
        assert "h…" not in out  # "healthy" wrapping artifact
