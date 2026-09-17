"""Runtime reports preserve playback causes and do not rewrite prior pauses."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.core.auth import Principal
from app.core.errors import ApiError
from app.domain.candidate_runtime import CANDIDATE_RUNTIME_PROBLEM_REASONS
from app.domain.interview_agent import ClientSignal
from app.domain.interview_lifecycle import InterviewSessionLifecycle, LifecycleCommand, LifecycleCommandType
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.schemas.api import CandidateRuntimeProblemReport
from app.services.interview_agent import AgentChannel, InterviewAgentRuntime
from app.services.livekit_evidence_ingress import ManagedLiveKitEvidenceSession
from app.services.speech_output_interruption import revoke_active_performance
from app.services.interviews import InterviewService


def prepared_service(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_CANDIDATE_TOKEN_SECRET", "c" * 48)
    store = InMemoryStore()
    item = {"id": "interview_playback_failure_053", "organization_id": "org_default", "candidate_id": "candidate_synthetic",
            "candidate": {"id": "candidate_synthetic", "name": "Synthetic"}, "status": "in_progress",
            "turns": [], "answers": [], "settings": {"record_audio": False, "record_video": False},
            "created_at": "2026-09-17T00:00:00Z", "updated_at": "2026-09-17T00:00:00Z"}
    with persistence_for(store).transaction("org_default") as tx:
        session = tx.interview_sessions.add(item)
    service = InterviewService(store)
    token = service.candidate_join_url(session).split("token=", 1)[1]
    return service, store, session["id"], token


@pytest.mark.parametrize("code", list(CANDIDATE_RUNTIME_PROBLEM_REASONS))
@pytest.mark.parametrize("fallback_first", [True, False])
def test_report_and_authenticated_fallback_share_atomic_pause_and_safe_diagnosis(monkeypatch, code, fallback_first):
    service, store, interview_id, token = prepared_service(monkeypatch)
    CandidateRuntimeProblemReport(code=code)
    if fallback_first:
        service.pause_for_candidate_runtime_problem(interview_id, code)
    first = service.report_candidate_runtime_problem(interview_id, token, code)
    service.pause_for_candidate_runtime_problem(interview_id, code)
    final = service.get_interview(interview_id)
    assert first["status"] == "paused"
    assert final["interruption"]["kind"] == "runtime"
    assert final["interruption"]["reason"] == CANDIDATE_RUNTIME_PROBLEM_REASONS[code]
    assert len(final["agent_runtime"]["problems"]) == 1
    assert len([e for e in final["lifecycle_events"] if e["type"] == "interview.paused"]) == 1
    snapshot = InterviewAgentRuntime(store)._snapshot(final, Principal(actor_id="candidate:synthetic", organization_id="org_default",
                                                                      roles=frozenset({"candidate"}), authenticated=True))
    assert snapshot["runtime_problem_code"] == code
    assert "problems" not in snapshot
    assert "pause_epoch" not in snapshot


def test_a_prior_manual_pause_keeps_its_cause_but_does_not_hide_audio_failure(monkeypatch):
    service, _, interview_id, token = prepared_service(monkeypatch)
    manual = service.pause_interview(interview_id, "candidate_requested_pause")
    service.report_candidate_runtime_problem(interview_id, token, "AUDIO_STREAM_INVALID")
    final = service.get_interview(interview_id)
    assert final["interruption"] == manual["interruption"]
    assert final["agent_runtime"]["problems"][-1]["code"] == "AUDIO_STREAM_INVALID"


def test_same_second_resume_and_new_pause_does_not_deduplicate_new_failure():
    lifecycle = InterviewSessionLifecycle()
    source = {"id": "synthetic", "organization_id": "org_default", "status": "in_progress",
              "current_turn_id": "turn_synthetic", "turns": [{"id": "turn_synthetic", "status": "asking"}], "answers": []}
    command = LifecycleCommand(LifecycleCommandType.PAUSE, {"runtime_problem_code": "AUDIO_TRACK_LOST"})
    first = lifecycle.execute(source, command, now="2026-09-17T00:00:00Z").session
    resumed = lifecycle.execute(first, LifecycleCommand(LifecycleCommandType.RESUME), now="2026-09-17T00:00:00Z").session
    second = lifecycle.execute(resumed, command, now="2026-09-17T00:00:00Z").session
    assert len(second["agent_runtime"]["problems"]) == 2
    assert len({p["pause_epoch"] for p in second["agent_runtime"]["problems"]}) == 2


def test_raw_error_payloads_and_wrong_token_cannot_write_diagnostics(monkeypatch):
    service, _, interview_id, _ = prepared_service(monkeypatch)
    for payload in [{"code": "secret arbitrary message"}, {"code": "AUDIO_TRACK_LOST", "message": "secret"}]:
        with pytest.raises(ValidationError):
            CandidateRuntimeProblemReport(**payload)
    with pytest.raises(ApiError) as error:
        service.report_candidate_runtime_problem(interview_id, "wrong-token", "AUDIO_TRACK_LOST")
    assert error.value.code == "CANDIDATE_SESSION_TOKEN_INVALID"
    assert service.get_interview(interview_id)["status"] == "in_progress"


@pytest.mark.parametrize("reason", ["AUDIO_TRACK_LOST", "manual"])
def test_authenticated_channel_fallback_preserves_allowlisted_cause(monkeypatch, reason):
    service, _, interview_id, _ = prepared_service(monkeypatch)
    channel = SimpleNamespace(
        principal=Principal(actor_id="candidate:synthetic", organization_id="org_default", roles=frozenset({"candidate"}), authenticated=True),
        interview_id=interview_id, organization_id="org_default", _evidence_session=None,
        _cancel_planning=lambda: None, _cancel_speech_output=AsyncMock(), _set_floor=AsyncMock(), _emit_snapshot=AsyncMock(),
        runtime=SimpleNamespace(interviews=service, _require_candidate=lambda principal: None,
                                _clear_active_performance=lambda *args: None),
    )
    asyncio.run(AgentChannel._handle(channel, ClientSignal(type="pause", idempotency_key="synthetic_failure", payload={"reason": reason})))
    final = service.get_interview(interview_id)
    assert final["interruption"]["reason"] == ("candidate_audio_track_lost" if reason == "AUDIO_TRACK_LOST" else "candidate_requested_pause")


@pytest.mark.parametrize("managed", [True, False])
def test_barge_in_revokes_and_notifies_before_blocked_media_cleanup(monkeypatch, managed):
    service, store, interview_id, _ = prepared_service(monkeypatch)
    runtime = InterviewAgentRuntime(store)
    with persistence_for(store).transaction("org_default") as tx:
        session = tx.interview_sessions.get(interview_id)
        session["agent_runtime"] = {"floor": "agent", "calibration_status": "awaiting_confirmation", "active_performance_id": "performance_old"}
        tx.interview_sessions.update(session, expected_version=session["version"])

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        events = []

        async def emit(kind, payload, **kwargs):
            assert service.get_interview(interview_id)["agent_runtime"]["active_performance_id"] is None
            events.append((kind, payload))

        async def cleanup(**kwargs):
            assert kwargs == {"expected_performance_id": "performance_old"}
            entered.set()
            await release.wait()

        source = SimpleNamespace(runtime=runtime, interview_id=interview_id, organization_id="org_default",
                                 principal=Principal(actor_id="candidate:synthetic", organization_id="org_default", roles=frozenset({"candidate"}), authenticated=True),
                                 _evidence_session=None, _endpoint_task=None, _require_active_interview=lambda: None,
                                 _emit=emit, _cancel_speech_output=cleanup, _set_floor=AsyncMock())
        signal = ClientSignal(type="speech.started", idempotency_key="barge_synthetic")
        evidence = SimpleNamespace(interview_id=interview_id, organization_id="org_default", chain=SimpleNamespace(is_open=False),
                                   _matches_capture=lambda signal: False, _presentation_task=None)
        task = asyncio.create_task(ManagedLiveKitEvidenceSession._apply_speech_started(evidence, source, signal)
                                   if managed else AgentChannel._candidate_speech_started(source, signal))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert events == [("avatar.performance.interrupted", {"performance_id": "performance_old", "reason": "barge_in", "deadline_ms": 200})]
            assert not task.done()
        finally:
            release.set()
            await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())


def test_a_late_old_interruption_cannot_revoke_or_cancel_a_new_output(monkeypatch):
    _, store, interview_id, _ = prepared_service(monkeypatch)
    runtime = InterviewAgentRuntime(store)
    runtime._set_active_performance(interview_id, "performance_new", "org_default")
    output = SimpleNamespace(performance_id="performance_new", abort=AsyncMock())
    source = SimpleNamespace(runtime=runtime, interview_id=interview_id, organization_id="org_default",
                             _emit=AsyncMock(), _speech_output=output, _speech_output_task=None)

    async def scenario():
        assert not await revoke_active_performance(source, "performance_old", reason="barge_in")
        await AgentChannel._cancel_speech_output(source, expected_performance_id="performance_old")
        source._emit.assert_not_called()
        output.abort.assert_not_called()
        assert source._speech_output is output

    asyncio.run(scenario())
