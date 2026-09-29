"""Warm-up controls may arrive while their confirmation audio is still playing."""
import asyncio

import pytest

from app.core.errors import ApiError
from app.domain.interview_agent import ClientSignal
from test_expression_stale_fence import _mutate
from test_streaming_expression_lifecycle import _session, _signal, _streaming_harness


async def _playing_confirmation(monkeypatch, phase):
    h = _streaming_harness(monkeypatch, gate_eof=phase == "playing")

    def warmup(session):
        session.update(current_turn_id=None, turns=[], execution_schema_version=3)
        session["agent_runtime"]["calibration_status"] = "awaiting_confirmation"

    _mutate(h.runtime, h.channel, warmup)
    events = []
    emit = h.channel._emit

    async def observe(kind, payload, **kwargs):
        events.append((kind, dict(payload)))
        return await emit(kind, payload, **kwargs)

    monkeypatch.setattr(h.channel, "_emit", observe)
    performance = await h.channel._select_act(
        act_type="warmup_confirmation", text="请确认试音字幕。", turn_id=None,
        causation_id="synthetic_warmup", evidence_refs=[], gesture="nod",
    )
    item = h.outputs[0]
    if phase != "before_ready":
        await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
        await asyncio.wait_for(item.raw.eof_reached.wait(), 1)
    h.events = events
    h.performance = performance
    return h


@pytest.mark.parametrize("phase", ["before_ready", "playing", "awaiting_drain"])
def test_confirm_revokes_warmup_before_recording_or_first_question_planning(monkeypatch, phase):
    async def scenario():
        h = await _playing_confirmation(monkeypatch, phase)
        item = h.outputs[0]
        task = h.channel._speech_output_task
        order = []
        abort = item.publisher.abort

        async def observed_abort():
            await abort()
            assert item.publisher.interruption_before_abort, "unpublish must follow the exact cancellation"
            order.append("abort")

        async def start_capture(*args, **kwargs):
            assert task.done() and item.publisher.abort_calls
            assert _session(h)["agent_runtime"]["calibration_status"] == "awaiting_confirmation"
            order.append("recording")
            return {"status": "recording"}

        def schedule(causation_id):
            assert h.channel._speech_output is None
            assert task.done() and item.publisher.abort_calls
            assert _session(h)["agent_runtime"]["active_performance_id"] is None
            order.append("plan")

        monkeypatch.setattr(item.publisher, "abort", observed_abort)
        monkeypatch.setattr(h.runtime.media_captures, "start_for_connection", start_capture)
        monkeypatch.setattr(h.channel, "_schedule_planning", schedule)
        _mutate(h.runtime, h.channel, lambda session: session["settings"].update(record_audio=True))
        try:
            signal = ClientSignal(type="warmup.confirm", idempotency_key="confirm_once")
            await h.channel.send(signal)
            await h.channel.send(signal)
            assert order[-2:] == ["recording", "plan"], h.events
            assert order.count("plan") == 1
            assert _session(h)["agent_runtime"]["calibration_status"] == "completed"
            assert _session(h)["status"] == "in_progress"
            assert _session(h)["answers"] == []
            # Late browser receipts for the old trial cannot return the floor
            # to the candidate while the first real question is being planned.
            await _signal(h, "avatar.performance.ready", h.performance.performance_id, item.output.output_id)
            await _signal(h, "avatar.performance.stopped", h.performance.performance_id,
                          item.output.output_id, reason="drained")
            assert order.count("plan") == 1
            assert _session(h)["agent_runtime"]["calibration_status"] == "completed"
        finally:
            monkeypatch.setattr(item.publisher, "abort", abort)
            await h.channel.close("synthetic_complete")

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["paused", "stale_control"])
def test_invalid_confirm_cannot_revoke_current_output_or_start_recording(monkeypatch, invalid):
    async def scenario():
        h = await _playing_confirmation(monkeypatch, "before_ready")
        if invalid == "paused":
            _mutate(h.runtime, h.channel, lambda session: session.update(status="paused"))
        else:
            def stale(_channel):
                raise ApiError("EVIDENCE_CONTROL_STALE", "Old controller", status_code=409)
            h.channel._evidence_session.assert_controller = stale
        original = _session(h)["agent_runtime"]["calibration_status"]
        try:
            with pytest.raises(ApiError):
                await h.channel._confirm_warmup(ClientSignal(type="warmup.confirm", idempotency_key="invalid"))
            assert _session(h)["agent_runtime"]["calibration_status"] == original
            assert not any(kind == "avatar.performance.interrupted" for kind, _ in h.events)
        finally:
            await h.channel.close("synthetic_complete")

    asyncio.run(scenario())


def test_confirmation_still_blocks_formal_questions_when_recording_fails(monkeypatch):
    async def scenario():
        h = await _playing_confirmation(monkeypatch, "playing")
        plans = []

        async def failed_capture(*args, **kwargs):
            assert h.channel._speech_output is None
            return {"status": "failed"}

        monkeypatch.setattr(h.runtime.media_captures, "start_for_connection", failed_capture)
        monkeypatch.setattr(h.channel, "_schedule_planning", lambda cause: plans.append(cause))
        _mutate(h.runtime, h.channel, lambda session: session["settings"].update(record_audio=True))
        try:
            with pytest.raises(ApiError) as error:
                await h.channel._confirm_warmup(ClientSignal(type="warmup.confirm", idempotency_key="failed_capture"))
            assert error.value.code == "MEDIA_CAPTURE_NOT_RECORDING"
            assert not plans
            session = _session(h)
            assert session["status"] == "paused"
            assert session["agent_runtime"]["calibration_status"] == "awaiting_confirmation"
            assert not session["turns"] and not session["answers"]
        finally:
            await h.channel.close("synthetic_complete")

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["interrupt", "recording"])
@pytest.mark.parametrize("change", ["pause", "controller", "retry", "turn", "closed", "detached"])
def test_delayed_confirmation_cannot_complete_after_its_authority_changes(monkeypatch, boundary, change):
    async def scenario():
        h = await _playing_confirmation(monkeypatch, "playing")
        calls = []
        original_evidence = h.channel._evidence_session

        def replace():
            if change == "pause":
                _mutate(h.runtime, h.channel, lambda s: s.update(status="paused"))
            elif change == "retry":
                _mutate(h.runtime, h.channel, lambda s: s["agent_runtime"].update(calibration_status="retrying"))
            elif change == "turn":
                _mutate(h.runtime, h.channel, lambda s: s.update(current_turn_id="newer_turn"))
            elif change == "closed":
                h.channel._terminating = True
            elif change == "detached":
                h.channel._evidence_session = None
            else:
                def stale(_channel):
                    raise ApiError("EVIDENCE_CONTROL_STALE", "Replaced", status_code=409)
                h.channel._evidence_session.assert_controller = stale

        emit = h.channel._emit
        async def delayed_emit(kind, payload, **kwargs):
            event = await emit(kind, payload, **kwargs)
            if kind == "avatar.performance.interrupted" and boundary == "interrupt":
                replace()
            return event

        async def capture(*args, **kwargs):
            calls.append("recording")
            if boundary == "recording":
                replace()
            return {"status": "recording"}

        monkeypatch.setattr(h.channel, "_emit", delayed_emit)
        monkeypatch.setattr(h.runtime.media_captures, "start_for_connection", capture)
        monkeypatch.setattr(h.channel, "_schedule_planning", lambda cause: calls.append("plan"))
        _mutate(h.runtime, h.channel, lambda s: s["settings"].update(record_audio=True))
        try:
            with pytest.raises(ApiError):
                await h.channel._confirm_warmup(ClientSignal(type="warmup.confirm", idempotency_key="delay"))
            assert calls == (["recording"] if boundary == "recording" else [])
            assert _session(h)["agent_runtime"]["calibration_status"] != "completed"
            assert not _session(h)["turns"] and not _session(h)["answers"]
        finally:
            h.channel._terminating = False
            h.channel._evidence_session = original_evidence
            await h.channel.close("synthetic_complete")

    asyncio.run(scenario())


def test_playing_trial_to_real_planner_selects_and_speaks_exactly_one_first_question(monkeypatch):
    from test_interviewer_supervisor import Gateway, decision, source_session, supervisor

    async def scenario():
        h = await _playing_confirmation(monkeypatch, "playing")
        source = source_session()
        excluded = {"id", "version", "agent_runtime", "agent_events", "candidate", "candidate_id"}
        _mutate(h.runtime, h.channel, lambda s: s.update({k: v for k, v in source.items() if k not in excluded}))
        h.runtime.interviews.supervisor = supervisor(Gateway(decision()), h.runtime.store)
        try:
            signal = ClientSignal(type="warmup.confirm", idempotency_key="start_formal")
            await h.channel.send(signal)
            task = h.channel._planning_task
            assert task is not None
            await asyncio.wait_for(task, 2)
            await h.channel.send(signal)
            current = _session(h)
            assert current["status"] == "in_progress"
            assert len(current["turns"]) == 1 and current["answers"] == []
            assert current["turns"][0]["question_spoken_text"] == "说明python的边界。"
            assert len(h.outputs) == 2
            assert h.outputs[0].publisher.interruption_before_abort
            assert h.channel._speech_output is h.outputs[1].output
            assert not current["agent_runtime"].get("planning_problem")
            assert not current["agent_runtime"].get("problems")
        finally:
            await h.channel.close("synthetic_complete")

    asyncio.run(scenario())
