import asyncio
import importlib
from types import SimpleNamespace

import pytest

from app.core.interview_agent_metrics import (
    INTERVIEW_AGENT_STAGE_METRICS,
    InterviewAgentMetrics,
    measure_interview_agent_stage,
    observe_interview_agent_metric,
)
from app.services.interview_agent import InterviewAgentRuntime


@pytest.fixture
def metrics(monkeypatch):
    collector = InterviewAgentMetrics()
    module = importlib.import_module("app.core.interview_agent_metrics")
    monkeypatch.setattr(module, "_METRICS", collector)
    return collector


def test_stage_vocabulary_is_fixed_and_contains_no_dynamic_labels():
    assert INTERVIEW_AGENT_STAGE_METRICS == {
        "stt_snapshot_ms",
        "stt_final_ms",
        "understanding_prepare_ms",
        "decision_commit_ms",
        "turn_detector_ms",
        "tts_synthesis_ms",
        "tts_asset_import_ms",
        "tts_stream_open_ms",
        "tts_first_pcm_ms",
        "tts_playback_ready_ms",
        "tts_remote_drain_ms",
    }


def test_stage_timer_records_elapsed_duration_on_success_and_failure(metrics):
    clock = iter([10.0, 10.125, 20.0, 20.25])
    with measure_interview_agent_stage("stt_final_ms", time_source=lambda: next(clock)):
        pass
    with pytest.raises(RuntimeError, match="original failure"):
        with measure_interview_agent_stage("stt_final_ms", time_source=lambda: next(clock)):
            raise RuntimeError("original failure")
    assert metrics.snapshot()["stt_final_ms"] == {
        "count": 2, "p50": 125.0, "p95": 250.0, "max": 250.0,
    }


@pytest.mark.anyio
async def test_stage_timer_preserves_task_cancellation_and_records_attempt(metrics):
    entered = asyncio.Event()
    clock = iter([0.0, 0.5])

    async def work():
        with measure_interview_agent_stage(
            "understanding_prepare_ms", time_source=lambda: next(clock)
        ):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert metrics.snapshot()["understanding_prepare_ms"]["max"] == 500.0


def test_counter_and_unknown_names_cannot_be_used_as_stage_durations(metrics):
    for name in ("candidate_name", "turn_decision_cancelled", "stop_to_final_ms"):
        with measure_interview_agent_stage(name):
            pass
    observe_interview_agent_metric("candidate_name", 1)
    observe_interview_agent_metric("turn_decision_cancelled", 1)
    snapshot = metrics.snapshot()
    assert "candidate_name" not in snapshot
    assert snapshot["stop_to_final_ms"]["count"] == 0
    assert snapshot["turn_decision_cancelled"]["count"] == 1


def test_telemetry_or_clock_failure_cannot_change_business_outcome(monkeypatch):
    module = importlib.import_module("app.core.interview_agent_metrics")

    class BrokenMetrics:
        def observe(self, *args):
            raise RuntimeError("metrics offline")

    monkeypatch.setattr(module, "_METRICS", BrokenMetrics())
    observe_interview_agent_metric("turn_decision_cancelled", 1)
    with measure_interview_agent_stage("decision_commit_ms"):
        pass
    with pytest.raises(ValueError, match="business error"):
        with measure_interview_agent_stage("decision_commit_ms"):
            raise ValueError("business error")

    def broken_clock():
        raise RuntimeError("clock unavailable")

    with measure_interview_agent_stage("decision_commit_ms", time_source=broken_clock):
        pass


@pytest.mark.anyio
async def test_dynamic_expression_separately_measures_synthesis_and_private_import(metrics):
    async def synthesize(capability, request):
        assert request.purpose == "interview_agent_expression"
        assert request.metadata["approved"] is True
        assert request.text == "请举一个具体例子。"
        return SimpleNamespace(
            audio_uri="https://unused.example.test/generated.wav",
            content_type="audio/wav",
            duration_ms=1500,
            provider=SimpleNamespace(provider_id="test_provider"),
            visemes=[],
        )

    async def import_tts(**kwargs):
        assert kwargs["interview_id"] == "iv_stage_test"
        assert kwargs["turn_id"] == "turn_stage_test"
        return {"audio_uri": "agent-expression://file_test", "duration_ms": 1500}

    runtime = SimpleNamespace(
        interviews=SimpleNamespace(get_interview=lambda *args: {"settings": {}}),
        _current_turn=lambda session: None,
        gateway=SimpleNamespace(invoke=synthesize),
        expression_audio=SimpleNamespace(import_tts=import_tts),
    )
    result = await InterviewAgentRuntime._expression_audio(
        runtime, "iv_stage_test", "turn_stage_test", "请举一个具体例子。",
        "actor_test", "org_default",
    )
    assert result == {
        "audio_uri": "agent-expression://file_test", "duration_ms": 1500,
        "visemes": [], "delivery": "cascade",
    }
    assert metrics.snapshot()["tts_synthesis_ms"]["count"] == 1
    assert metrics.snapshot()["tts_asset_import_ms"]["count"] == 1


@pytest.mark.anyio
async def test_failed_tts_records_attempt_without_starting_private_import(metrics):
    async def synthesize(*args):
        raise RuntimeError("provider unavailable")

    runtime = SimpleNamespace(
        interviews=SimpleNamespace(get_interview=lambda *args: {"settings": {}}),
        _current_turn=lambda session: None,
        gateway=SimpleNamespace(invoke=synthesize),
    )
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await InterviewAgentRuntime._expression_audio(
            runtime, "iv_stage_test", "turn_stage_test", "请举一个具体例子。",
            "actor_test", "org_default",
        )
    assert metrics.snapshot()["tts_synthesis_ms"]["count"] == 1
    assert metrics.snapshot()["tts_asset_import_ms"]["count"] == 0
