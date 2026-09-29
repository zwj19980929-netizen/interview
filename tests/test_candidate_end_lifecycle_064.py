"""Candidate-requested closure includes speech, a receipt and media release.

The real Evidence owner, audio capture, semantic gateway validation and durable
answer commit run; only synthetic provider output and media transport are used.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.errors import ApiError
from app.core.time import utc_now
from app.domain.interview_agent import ClientSignal
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.media_capture import InterviewMediaCaptureService
from app.services.agent_ticket import InterviewAgentTicketService
from app.services.interview_agent import InterviewAgentRuntime
from app.transport.http.fields.public_interview import PUBLIC_INTERVIEW_FIELDS
from app.transport.http.responses import api_response
from test_automatic_turn_integration import _automatic_session, _current
from test_declined_answer_integration import (
    _TECHNICAL, _add_next_question, _answer_and_confirm_exit,
    _playable_speech, _semantic_provider, _stop_current_playback,
)
from test_evidence_owner_recovery_integration import INTERVIEW_ID, ORGANIZATION_ID, _opened, _session, _wait_until
from test_livekit_tenant_avatar_security import _capture, _provider_event


BODY = _TECHNICAL + "我不想继续面试了，请结束整场面试。"
EXIT_REPLY = "确定结束面试。"


@pytest.mark.parametrize("status", ["in_progress", "completed", "report_generating", "report_ready"])
def test_finished_input_refresh_exposes_only_receipt_and_never_reopens_media(status):
    async def scenario():
        store = InMemoryStore()
        _session(store)
        runtime = InterviewAgentRuntime(store)
        attach = AsyncMock(side_effect=AssertionError("Completed input must not attach an Evidence owner"))
        runtime.evidence_ingress = SimpleNamespace(enabled=True, attach=attach)
        with persistence_for(store).transaction(ORGANIZATION_ID) as tx:
            session = tx.interview_sessions.get(INTERVIEW_ID)
            session.update(status=status, current_turn_id=None, candidate_input_completed_at=utc_now())
            tx.interview_sessions.update(session, expected_version=session["version"])
        token = runtime.interviews._candidate_session_token(session)
        public = runtime.interviews.get_candidate_interview(INTERVIEW_ID, token, ORGANIZATION_ID)
        body = json.loads(api_response(public, fields=PUBLIC_INTERVIEW_FIELDS).body)
        receipt = body["completion"]
        assert set(receipt) == {"interview_id", "status", "submitted_at", "recording_retention_notice", "human_review_required"}
        assert receipt["submitted_at"] == session["candidate_input_completed_at"]
        assert body["status"] == status
        tickets = InterviewAgentTicketService(store)
        with pytest.raises(ApiError) as error:
            tickets.issue_candidate(INTERVIEW_ID, token, organization_id=ORGANIZATION_ID)
        assert error.value.code == "CANDIDATE_INPUT_COMPLETED"
        channel = await runtime.open(_opened("completion_recovery"))
        try:
            assert not attach.called
            snapshot = next(event for event in channel._queue._queue if event.type == "session.snapshot")
            assert snapshot.payload["completion"] == receipt
            await channel._handle(ClientSignal(type="client.ready", idempotency_key="ready_after_completion"))
            assert not attach.called
            assert not any(event.type == "conversation.act.selected" for event in channel._queue._queue)
            with pytest.raises(ApiError) as rejected:
                await channel._handle(ClientSignal(type="media.published", idempotency_key="publish_after_completion"))
            assert rejected.value.code == "CANDIDATE_INPUT_COMPLETED"
        finally:
            await channel.close("test_complete")

    asyncio.run(scenario())


def completed_events(runtime):
    return [event for event in _current(runtime)["agent_events"] if event["type"] == "completed"]


@pytest.mark.parametrize("default_cleanup", [False, True])
@pytest.mark.parametrize("finish_by", ["drained", "timeout"])
def test_candidate_stop_finishes_speech_receipt_and_ingress_before_scoring(tmp_path, monkeypatch, default_cleanup, finish_by):
    _semantic_provider(monkeypatch, technical=True, stop_interview=True)

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [BODY, "", "", EXIT_REPLY, ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            spoken = _playable_speech(runtime)
            ingress = managed._ingress
            stopped_recordings = []
            original_after = channel._after_formal_evidence_finished

            async def stop_recording(*args, **kwargs):
                stopped_recordings.append(args[0])

            async def after(turn_id, signal, **kwargs):
                kwargs["stop_evidence_session"] = default_cleanup
                return await original_after(turn_id, signal, **kwargs)

            monkeypatch.setattr(runtime.media_captures, "stop_for_interview", stop_recording)
            monkeypatch.setattr(channel, "_after_formal_evidence_finished", after)
            await _answer_and_confirm_exit(runtime, channel, managed)
            await _wait_until(lambda: bool(_current(runtime)["agent_runtime"].get("completion_closing_performance_id")))
            assert len(stopped_recordings) == 1
            assert not completed_events(runtime)
            assert not managed._stopped, "Farewell must keep its original expression owner"
            if finish_by == "drained":
                await _stop_current_playback(runtime, channel)
            else:
                performance_id = _current(runtime)["agent_runtime"]["completion_closing_performance_id"]
                await channel._completion_after_timeout(performance_id, 1, None)
            await _wait_until(lambda: managed._stopped and not ingress.connected)
            session = _current(runtime)
            assert session["candidate_input_completion_reason"] == "candidate_requested"
            assert session["candidate_exit_confirmed_at"]
            assert session["status"] == "in_progress", "Pending scoring must not keep candidate media alive"
            assert session["answers"][0]["evaluation_status"] != "completed"
            assert session["answers"][0]["raw_transcript"] == BODY + EXIT_REPLY
            assert len(completed_events(runtime)) == 1
            assert completed_events(runtime)[0]["replayability"] == "replayable"
            assert completed_events(runtime)[0]["payload"]["human_review_required"] is True
            assert channel._evidence_session is None
            assert sum("感谢你的时间" in text for text in spoken) == 1
            await channel._finalize_completion(causation_id=None, reason="duplicate")
            assert len(completed_events(runtime)) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "timeout"])
def test_recording_close_failure_does_not_trap_candidate_in_finished_interview(tmp_path, monkeypatch, failure, caplog):
    _semantic_provider(monkeypatch, technical=True, stop_interview=True)
    monkeypatch.setattr("app.services.interview_agent._COMPLETION_RECORDING_TIMEOUT", .02)

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [BODY, "", "", EXIT_REPLY, ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            _playable_speech(runtime)
            capture = _capture(store, ORGANIZATION_ID, INTERVIEW_ID, capture_id="capture_end",
                               egress_id="egress_end", participant_identity="candidate:end")

            async def stop_recording(*args, **kwargs):
                if failure == "timeout":
                    await asyncio.Event().wait()
                raise RuntimeError("PRIVATE_RECORDING_ERROR")

            monkeypatch.setattr(runtime.media_captures.media_plane, "stop_egress", stop_recording)
            await _answer_and_confirm_exit(runtime, channel, managed)
            await _wait_until(lambda: bool(_current(runtime)["agent_runtime"].get("completion_closing_performance_id")))
            await _stop_current_playback(runtime, channel)
            await _wait_until(lambda: managed._stopped)
            assert len(completed_events(runtime)) == 1
            with persistence_for(store).transaction(ORGANIZATION_ID, read_only=True) as tx:
                saved = tx.interview_media_captures.get(capture["id"])
                finalizations = [work for work in tx.outbox.list() if work["kind"] == "interview.media.finalize"]
            assert len(finalizations) == 1
            assert saved["status"] == ("failed" if failure == "exception" else "stopping")
            if failure == "exception":
                assert saved["failure_code"] == "egress_stop_failed"
            assert _current(runtime)["status"] != "paused"
            assert "PRIVATE_RECORDING_ERROR" not in caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["stop", "webhook"])
@pytest.mark.parametrize("input_ended", [False, True])
def test_recording_failures_pause_only_when_candidate_input_is_open(monkeypatch, source, input_ended):
    async def scenario():
        store = InMemoryStore()
        _session(store)
        with persistence_for(store).transaction(ORGANIZATION_ID) as tx:
            session = tx.interview_sessions.get(INTERVIEW_ID)
            session["candidate_input_completed_at"] = utc_now() if input_ended else None
            tx.interview_sessions.update(session, expected_version=session["version"])
        capture = _capture(store, ORGANIZATION_ID, INTERVIEW_ID, capture_id="capture_end_failure",
                           egress_id="egress_end_failure", participant_identity="candidate:end")
        service = InterviewMediaCaptureService(store)
        if source == "stop":
            async def failed_stop(*args, **kwargs):
                raise RuntimeError("synthetic provider unavailable")
            monkeypatch.setattr(service.media_plane, "stop_egress", failed_stop)
            with pytest.raises(ApiError, match="could not be finalized"):
                await service.stop_for_interview(INTERVIEW_ID, actor_id="synthetic", organization_id=ORGANIZATION_ID)
        else:
            event = _provider_event(capture["room_name"], capture["egress_id"], capture["participant_identity"])
            event["egress_info"]["status"] = "EGRESS_FAILED"
            await service.handle_provider_webhook(event)
        assert service.get_for_interview(INTERVIEW_ID, ORGANIZATION_ID)["status"] == "failed"
        with persistence_for(store).transaction(ORGANIZATION_ID, read_only=True) as tx:
            assert tx.interview_sessions.get(INTERVIEW_ID)["status"] == ("in_progress" if input_ended else "paused")

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "timeout"])
def test_unavailable_farewell_still_emits_receipt_and_releases_evidence(tmp_path, monkeypatch, failure, caplog):
    _semantic_provider(monkeypatch, technical=True, stop_interview=True)
    monkeypatch.setattr("app.services.interview_agent._COMPLETION_PREPARATION_TIMEOUT", .02)

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [BODY, "", "", EXIT_REPLY, ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            _playable_speech(runtime)
            original_audio = runtime._expression_audio

            async def unavailable(*args, **kwargs):
                if not _current(runtime).get("candidate_input_completed_at"):
                    return await original_audio(*args, **kwargs)
                if failure == "timeout":
                    await asyncio.Event().wait()
                raise ApiError("SYNTHETIC_TTS_FAILURE", "PRIVATE_TTS_ERROR", status_code=503)

            monkeypatch.setattr(runtime, "_expression_audio", unavailable)
            await _answer_and_confirm_exit(runtime, channel, managed)
            await _wait_until(lambda: managed._stopped and bool(completed_events(runtime)))
            assert _current(runtime)["status"] != "paused"
            assert _current(runtime)["agent_runtime"]["floor"] == "none"
            assert len(completed_events(runtime)) == 1
            assert "PRIVATE_TTS_ERROR" not in caplog.text

    asyncio.run(scenario())


def test_evidence_cleanup_failure_is_retryable_without_duplicate_receipt(tmp_path, monkeypatch, caplog):
    _semantic_provider(monkeypatch, technical=True, stop_interview=True)

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [BODY, "", "", EXIT_REPLY, ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            _playable_speech(runtime)
            original_stop = managed.stop
            attempts = []

            async def stop(reason):
                if reason != "interview_completed":
                    return await original_stop(reason)
                attempts.append(reason)
                if len(attempts) == 1:
                    raise RuntimeError("PRIVATE_CLEANUP_ERROR")
                return await original_stop(reason)

            monkeypatch.setattr(managed, "stop", stop)
            await _answer_and_confirm_exit(runtime, channel, managed)
            await _wait_until(lambda: bool(_current(runtime)["agent_runtime"].get("completion_closing_performance_id")))
            await _stop_current_playback(runtime, channel)
            await _wait_until(lambda: channel._completion_evidence_cleanup_task.done())
            assert not managed._stopped and len(completed_events(runtime)) == 1
            await channel._finalize_completion(causation_id=None, reason="cleanup_retry")
            await _wait_until(lambda: managed._stopped)
            assert len(attempts) == 2 and len(completed_events(runtime)) == 1
            assert "PRIVATE_CLEANUP_ERROR" not in caplog.text

    asyncio.run(scenario())


def test_late_farewell_after_owner_release_cannot_emit_playback_or_completion(tmp_path, monkeypatch):
    _semantic_provider(monkeypatch, technical=True, stop_interview=True)

    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [BODY, "", "", EXIT_REPLY, ""], spoken_confirmation=True) as (store, runtime, channel, managed):
            _add_next_question(store)
            _playable_speech(runtime)
            original_audio = runtime._expression_audio
            entered, released = asyncio.Event(), asyncio.Event()

            async def delayed(*args, **kwargs):
                if not _current(runtime).get("candidate_input_completed_at"):
                    return await original_audio(*args, **kwargs)
                entered.set()
                await released.wait()
                return {"audio_uri": "mock://late-farewell", "duration_ms": 100}

            monkeypatch.setattr(runtime, "_expression_audio", delayed)
            await _answer_and_confirm_exit(runtime, channel, managed)
            await asyncio.wait_for(entered.wait(), 3)
            started_before_release = [event for event in _current(runtime)["agent_events"]
                                      if event["type"] == "avatar.performance.started"]
            assert len(started_before_release) == 1, "The exit confirmation was actually played"
            managed.supervisor.coordinator.release(managed.ownership)
            released.set()
            await _wait_until(lambda: managed._presentation_task.done())
            assert not completed_events(runtime)
            assert not _current(runtime)["agent_runtime"].get("completion_closing_performance_id")
            assert [event for event in _current(runtime)["agent_events"]
                    if event["type"] == "avatar.performance.started"] == started_before_release

    asyncio.run(scenario())
