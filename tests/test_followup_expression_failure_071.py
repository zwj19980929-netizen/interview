"""Synthetic transport failures after a committed answer; no external services."""

import asyncio
from copy import deepcopy
from time import monotonic
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.core.interaction_budget import interaction_deadline
from app.core.time import utc_now
from app.model_gateway.errors import ProviderError
from app.services import interview_agent
from test_approved_speech_output import _Publisher
from test_expression_stale_fence import _harness, _mutate


def _transport_harness(monkeypatch):
    runtime, channel, coordinator, grant = _harness()
    _mutate(runtime, channel, lambda session: session["turns"][0].update(is_followup=True))
    publisher = _Publisher()
    monkeypatch.setattr(interview_agent, "LiveKitApprovedAudioPublisher", lambda *args, **kwargs: publisher)
    return runtime, channel, publisher, coordinator, grant


async def _open(runtime, channel):
    return await runtime._open_streamed_expression(
        channel.interview_id, "turn_1", "Synthetic follow-up", channel.organization_id,
        performance_id="performance_synthetic", assert_current=lambda: None,
    )


@pytest.mark.anyio
async def test_publisher_failure_does_not_wait_for_or_get_replaced_by_pending_tts(monkeypatch):
    runtime, channel, publisher, _, _ = _transport_harness(monkeypatch)
    started, cancelled = asyncio.Event(), asyncio.Event()
    failure = ApiError("SYNTHETIC_MEDIA_FAILURE", "Synthetic media failure.", status_code=503)

    async def provider(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def publish():
        await started.wait()
        raise failure

    monkeypatch.setattr(runtime.gateway, "open_tts_stream", provider)
    publisher.open = publish
    with interaction_deadline(monotonic() + .5):
        with pytest.raises(ApiError) as raised:
            await asyncio.wait_for(_open(runtime, channel), .3)
    assert raised.value is failure
    assert cancelled.is_set() and publisher.abort_calls == 1


@pytest.mark.anyio
async def test_simultaneous_unsupported_tts_and_publisher_failure_keep_batch_fallback(monkeypatch):
    runtime, channel, publisher, _, _ = _transport_harness(monkeypatch)

    async def provider(*args, **kwargs):
        raise ProviderError("provider_streaming_not_supported", "Synthetic unsupported transport.", retryable=False)

    async def publish():
        raise ApiError("SYNTHETIC_MEDIA_FAILURE", "Synthetic media failure.", status_code=503)

    monkeypatch.setattr(runtime.gateway, "open_tts_stream", provider)
    publisher.open = publish
    assert await _open(runtime, channel) is None
    assert publisher.abort_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("failure_side", ["tts", "publisher", "unsupported", "raw_tts_timeout"])
async def test_outer_deadline_race_preserves_already_completed_transport_failure(monkeypatch, caplog, failure_side):
    runtime, channel, publisher, _, _ = _transport_harness(monkeypatch)
    error = (ProviderError("provider_timeout", "Synthetic TTS open timed out.", retryable=True)
             if failure_side == "tts" else
             ProviderError("provider_streaming_not_supported", "Synthetic unsupported transport.", retryable=False)
             if failure_side == "unsupported" else
             asyncio.TimeoutError() if failure_side == "raw_tts_timeout" else
             ApiError("SYNTHETIC_MEDIA_FAILURE", "Synthetic media failure.", status_code=503))
    cleaned = []

    async def abort_stream():
        cleaned.append("stream")

    async def provider(*args, **kwargs):
        if failure_side != "publisher":
            raise error
        return SimpleNamespace(abort=abort_stream)

    async def publish():
        if failure_side in {"publisher", "unsupported"}:
            raise (error if failure_side == "publisher" else
                   ApiError("SYNTHETIC_MEDIA_FAILURE", "Synthetic media failure.", status_code=503))
        return publisher.publication

    original_wait_for = asyncio.wait_for

    async def outer_timer_wins(work, *, timeout):
        if getattr(getattr(work, "cr_code", None), "co_name", None) == "prepare_transports":
            # Both children finish, but the outer timer wins before their
            # result reaches its waiter. No provider/network timing is used.
            await asyncio.sleep(0)
            work.close()
            raise asyncio.TimeoutError()
        return await original_wait_for(work, timeout=timeout)

    monkeypatch.setattr(runtime.gateway, "open_tts_stream", provider)
    monkeypatch.setattr(asyncio, "wait_for", outer_timer_wins)
    publisher.open = publish
    if failure_side == "unsupported":
        assert await _open(runtime, channel) is None
    else:
        with pytest.raises(ApiError if failure_side == "raw_tts_timeout" else type(error)) as raised:
            await _open(runtime, channel)
        if failure_side == "raw_tts_timeout":
            assert raised.value.code == "AGENT_AUDIO_FIRST_PCM_TIMEOUT"
            assert raised.value.details["stage"] == "tts_stream_open"
        else:
            assert raised.value is error
            expected_stage = "tts_stream_open" if failure_side == "tts" else "media_publication_open"
            assert "stage=%s" % expected_stage in caplog.text and "budget_ms=" in caplog.text
    assert publisher.abort_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("pending", ["tts", "publisher", "both"])
async def test_open_timeout_is_structured_and_reports_only_safe_stage_metadata(monkeypatch, pending):
    runtime, channel, publisher, _, _ = _transport_harness(monkeypatch)
    cleaned = []

    async def abort_stream():
        cleaned.append("stream")

    async def provider(*args, **kwargs):
        if pending in {"tts", "both"}:
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append("tts_cancelled")
        return SimpleNamespace(abort=abort_stream)

    original_open = publisher.open

    async def publish():
        if pending in {"publisher", "both"}:
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append("publisher_cancelled")
        return await original_open()

    monkeypatch.setattr(runtime.gateway, "open_tts_stream", provider)
    publisher.open = publish
    with interaction_deadline(monotonic() + .05):
        with pytest.raises(ApiError) as raised:
            await _open(runtime, channel)
    error = raised.value
    assert error.code == "AGENT_AUDIO_FIRST_PCM_TIMEOUT" and error.message.strip()
    assert error.status_code == 504
    assert error.details["stage"] == {
        "tts": "tts_stream_open", "publisher": "media_publication_open", "both": "speech_transports_open",
    }[pending]
    assert 0 < error.details["budget_ms"] <= 50
    assert set(error.details) == {"stage", "budget_ms"}
    await channel._problem(error, turn_id="turn_1", causation_id="synthetic-timeout")
    recorded = runtime.interviews.get_interview(channel.interview_id)["agent_runtime"]["problems"][-1]
    assert "stage=%s" % error.details["stage"] in recorded["message"]
    assert "budget_ms=%s" % error.details["budget_ms"] in recorded["message"]
    assert publisher.abort_calls == 1
    assert "tts_cancelled" in cleaned if pending in {"tts", "both"} else "stream" in cleaned


@pytest.mark.anyio
@pytest.mark.parametrize("cleanup_failure", ["stream", "publisher"])
async def test_cleanup_error_cannot_replace_transport_root_cause(monkeypatch, cleanup_failure):
    runtime, channel, publisher, _, _ = _transport_harness(monkeypatch)
    failure = ApiError("SYNTHETIC_MEDIA_FAILURE", "Synthetic media failure.", status_code=503)
    cleaned = []

    async def abort_stream():
        cleaned.append("stream")
        if cleanup_failure == "stream":
            raise RuntimeError("Synthetic cleanup failure")

    async def provider(*args, **kwargs):
        return SimpleNamespace(abort=abort_stream)

    async def publish():
        raise failure

    async def abort_publisher():
        cleaned.append("publisher")
        if cleanup_failure == "publisher":
            raise RuntimeError("Synthetic cleanup failure")

    monkeypatch.setattr(runtime.gateway, "open_tts_stream", provider)
    publisher.open, publisher.abort = publish, abort_publisher
    with pytest.raises(ApiError) as raised:
        await _open(runtime, channel)
    assert raised.value is failure and cleaned == ["stream", "publisher"]


@pytest.mark.anyio
async def test_cancellation_during_stream_cleanup_stays_cancellation_and_closes_publisher(monkeypatch):
    runtime, channel, publisher, _, _ = _transport_harness(monkeypatch)
    cleaning = asyncio.Event()

    async def abort_stream():
        cleaning.set()
        await asyncio.Event().wait()

    async def provider(*args, **kwargs):
        return SimpleNamespace(abort=abort_stream)

    async def publish():
        raise ApiError("SYNTHETIC_MEDIA_FAILURE", "Synthetic media failure.", status_code=503)

    monkeypatch.setattr(runtime.gateway, "open_tts_stream", provider)
    publisher.open = publish
    task = asyncio.create_task(_open(runtime, channel))
    await asyncio.wait_for(cleaning.wait(), .3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert publisher.abort_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("superseded", [None, "turn", "owner"])
async def test_failed_frozen_followup_emits_paused_snapshot_without_touching_new_owner(monkeypatch, superseded):
    await _frozen_followup_scenario(monkeypatch, superseded)


async def _frozen_followup_scenario(monkeypatch, superseded=None):
    runtime, channel, _, coordinator, grant = _transport_harness(monkeypatch)
    frozen = {
        "act_id": "conversation_act_synthetic", "act_type": "followup", "text": "Synthetic follow-up",
        "turn_id": "turn_1", "root_turn_id": "turn_1", "evidence_quotes": ["Synthetic answer"],
        "target_capability_points": ["Testing"], "followup_depth": 1, "evaluative": False,
        "approved_by": "controlled_followup.v1", "prompt_version": "controlled_followup.v1", "created_at": utc_now(),
    }
    _mutate(runtime, channel, lambda session: session["turns"][0].update(conversation_acts=[frozen]))
    def add_saved_answer(session):
        session["turns"].insert(0, {"id": "turn_parent", "status": "answered", "order": 1,
            "question_spoken_text": "Synthetic primary question", "is_followup": False})
        session["answers"] = [{"id": "answer_saved", "turn_id": "turn_parent", "final_transcript": "Synthetic saved answer"}]
    _mutate(runtime, channel, add_saved_answer)
    before = deepcopy(runtime.interviews.get_interview(channel.interview_id))
    managed = channel._evidence_session
    managed._capture_id = None
    managed._answer_endpoint = SimpleNamespace(capture=SimpleNamespace(is_open=False), revision=1, confirmation=None)
    monkeypatch.setattr(interview_agent, "ManagedLiveKitEvidenceSession", SimpleNamespace)

    async def timeout(*args, **kwargs):
        raise ApiError("AGENT_AUDIO_FIRST_PCM_TIMEOUT", "Synthetic preparation timeout.", status_code=504)

    monkeypatch.setattr(runtime, "_open_streamed_expression", timeout)
    problem = channel._problem

    async def problem_then_supersede(*args, **kwargs):
        await problem(*args, **kwargs)
        if superseded == "turn":
            def change(session):
                session["current_turn_id"] = "turn_2"
                session["turns"].append({"id": "turn_2", "status": "asking"})
                session["agent_runtime"]["active_performance_id"] = "performance_replacement"
            _mutate(runtime, channel, change)
        elif superseded == "owner":
            assert coordinator.release(grant)
            coordinator.attach_control(interview_id=channel.interview_id, organization_id=channel.organization_id,
                connection_id="connection_replacement", local_instance_id="instance_replacement")

    monkeypatch.setattr(channel, "_problem", problem_then_supersede)
    interview_agent._AGENT_CHANNEL_HUB.register(channel)
    try:
        assert await channel._select_act(act_type="followup", text=frozen["text"], turn_id="turn_1",
            causation_id="synthetic", evidence_refs=[], gesture="nod") is None
    finally:
        interview_agent._AGENT_CHANNEL_HUB.unregister(channel)
    assert not channel._terminating
    projected = []
    while not channel._queue.empty():
        projected.append(channel._queue.get_nowait().model_dump(mode="json"))
    session = runtime.interviews.get_interview(channel.interview_id)
    snapshots = [event for event in projected if event["type"] == "session.snapshot"]
    assert not any(event["type"] == "avatar.performance.started" for event in session["agent_events"])
    if superseded:
        assert session["status"] == "in_progress" and not snapshots
        if superseded == "turn":
            assert session["agent_runtime"]["active_performance_id"] == "performance_replacement"
    else:
        assert session["status"] == "paused" and session["agent_runtime"]["floor_reason"] == "expression_failure"
        assert len(snapshots) == 1 and projected[-1] == snapshots[0]
        assert session["answers"] == before["answers"] and session["current_turn_id"] == before["current_turn_id"]
        assert [(turn["id"], turn["question_spoken_text"]) for turn in session["turns"]] == [
            (turn["id"], turn["question_spoken_text"]) for turn in before["turns"]]
        snapshot = projected[-1]
        assert snapshot["type"] == "session.snapshot" and snapshot["causation_id"] == "synthetic"
        assert snapshot["payload"]["status"] == "paused" and snapshot["payload"]["floor"] == "none"
        assert snapshot["payload"]["current_turn_id"] == "turn_1"
        assert snapshot["payload"]["completed_answers"] == 1
        problem = next(event for event in projected if event["type"] == "problem")
        assert problem["causation_id"] == snapshot["causation_id"] and problem["session_sequence"] < snapshot["session_sequence"]
    return projected
