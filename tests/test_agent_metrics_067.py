"""Telemetry stays bounded and private while cumulative totals remain additive."""
import ast
import asyncio
import importlib
import json
from pathlib import Path

import pytest

from app.core.interview_agent_metrics import (
    INTERVIEW_AGENT_METRICS, InterviewAgentMetrics,
    measure_interview_agent_stage, observe_interview_agent_metric,
)


def test_all_literal_metric_emitters_use_registered_names():
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "observe_interview_agent_metric" and node.args
                    and isinstance(node.args[0], ast.Constant)):
                assert node.args[0].value in INTERVIEW_AGENT_METRICS, (str(path), node.lineno)


def test_preview_reuse_counter_is_registered_and_survives_legacy_sample_eviction(monkeypatch):
    module = importlib.import_module("app.core.interview_agent_metrics")
    collector = InterviewAgentMetrics(capacity=2)
    monkeypatch.setattr(module, "_METRICS", collector)
    for _ in range(4):
        observe_interview_agent_metric("stt_preview_reused", 1)
    assert collector.snapshot()["stt_preview_reused"]["count"] == 2
    assert collector.aggregation()["counters"]["stt_preview_reused"] == 4


def test_histograms_are_cumulative_and_additive_across_processes():
    left, right = InterviewAgentMetrics(capacity=1), InterviewAgentMetrics(capacity=1)
    for value in (5, 50, 500):
        left.observe("stop_to_final_ms", value)
    right.observe("stop_to_final_ms", 5_000)
    a, b = left.aggregation(), right.aggregation()
    assert a["instance_id"] != b["instance_id"]
    assert left.aggregation()["instance_id"] == a["instance_id"]
    first = a["histograms"]["stop_to_final_ms"]
    second = b["histograms"]["stop_to_final_ms"]
    assert first["count"] + second["count"] == 4
    assert first["sum"] + second["sum"] == 5_555
    assert first["buckets"]["500"] + second["buckets"]["500"] == 3
    assert first["buckets"]["+Inf"] + second["buckets"]["+Inf"] == 4
    assert left.snapshot()["stop_to_final_ms"] == {"count": 1, "p50": 500.0, "p95": 500.0, "max": 500.0}
    first["buckets"]["500"] = 999
    assert left.aggregation()["histograms"]["stop_to_final_ms"]["buckets"]["500"] == 3


def test_stage_outcomes_preserve_exception_and_legacy_histogram(monkeypatch):
    module = importlib.import_module("app.core.interview_agent_metrics")
    collector = InterviewAgentMetrics()
    monkeypatch.setattr(module, "_METRICS", collector)
    clock = iter([0, 0.125, 1, 1.5, 2, 2.25])
    with measure_interview_agent_stage("understanding_prepare_ms", time_source=lambda: next(clock)):
        pass
    with pytest.raises(RuntimeError, match="business failure"):
        with measure_interview_agent_stage("understanding_prepare_ms", time_source=lambda: next(clock)):
            raise RuntimeError("business failure")
    with pytest.raises(asyncio.CancelledError):
        with measure_interview_agent_stage("understanding_prepare_ms", time_source=lambda: next(clock)):
            raise asyncio.CancelledError()
    stages = collector.aggregation()["stages"]["understanding_prepare_ms"]
    assert {name: item["count"] for name, item in stages.items()} == {"success": 1, "failure": 1, "cancelled": 1}
    assert stages["success"]["sum"] == 125
    assert stages["failure"]["sum"] == 500
    assert stages["cancelled"]["sum"] == 250
    assert collector.snapshot()["understanding_prepare_ms"]["count"] == 3


@pytest.mark.parametrize("name,value,outcome", [
    ("candidate_name", 1, "success"),
    ("turn_decision_cancelled", 1, "success"),
    ("understanding_prepare_ms", 1, "private-exception-body"),
    ("understanding_prepare_ms", True, "success"),
    ("understanding_prepare_ms", float("nan"), "success"),
    ("understanding_prepare_ms", -1, "success"),
])
def test_invalid_stage_dimensions_do_not_create_exported_labels(name, value, outcome):
    collector = InterviewAgentMetrics()
    before = collector.aggregation()
    with pytest.raises(ValueError):
        collector.observe_stage(name, value, outcome)
    assert collector.aggregation() == before
    assert "private-exception-body" not in json.dumps(before)


@pytest.mark.anyio
async def test_admin_snapshot_preserves_legacy_shape_and_adds_aggregate_export(monkeypatch):
    module = importlib.import_module("app.api.routers.system")
    collector = InterviewAgentMetrics()
    collector.observe("stt_preview_reused", 1)
    monkeypatch.setattr(module, "interview_agent_metrics", lambda: collector)
    response = await module.interview_agent_metric_snapshot()
    assert response["schema"] == "interview-agent-metrics.v1"
    assert response["metrics"] == collector.snapshot()
    assert response["aggregation"] == collector.aggregation()
    assert response["aggregation"]["schema"] == "interview-agent-metrics.aggregate.v1"
    json.dumps(response, allow_nan=False)
