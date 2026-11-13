"""A failed optional invitation cannot discard a successfully transcribed trial."""
import asyncio

import pytest

from app.adapters.livekit_media import LiveKitMediaPlane
from app.core.errors import ApiError
from app.domain.interview_agent import ClientSignal
from app.model_gateway.errors import ProviderError
from app.providers.mock.provider import MockProvider
from app.repositories.memory import InMemoryStore
from app.services import interview_agent
from app.services.interview_agent import InterviewAgentRuntime
from app.services.livekit_evidence_ingress import LiveKitEvidenceIngressSupervisor
from test_approved_speech_output import _Publisher
from test_expression_stale_fence import _mutate
from test_livekit_evidence_supervisor import _configuration, _FakeIngress, _opened, _session as _seed
from test_streaming_expression_lifecycle import _session, _signal, _streaming_harness, _wait_until


_NOTICE = "WARMUP_CONFIRMATION_AUDIO_UNAVAILABLE"


def _make_warmup(session):
    session.update(current_turn_id=None, turns=[], execution_schema_version=3)
    session["settings"].update(record_audio=False, record_video=False)
    session["agent_runtime"].update(calibration_status="awaiting_confirmation", calibration_updated_at="synthetic_trial_1")


async def _select_confirmation(h):
    return await h.channel._select_act(act_type="warmup_confirmation", text="请核对试音字幕。",
        turn_id=None, causation_id="synthetic_trial", evidence_refs=[], gesture="nod")


@pytest.mark.parametrize("control", ["warmup.confirm", "warmup.retry"])
@pytest.mark.parametrize("code", ["provider_timeout", "provider_auth_failed", "provider_billing_required"])
def test_server_audio_final_survives_invitation_failure_and_explicit_control_works(monkeypatch, control, code):
    async def scenario():
        store = InMemoryStore()
        _seed(store)
        runtime = InterviewAgentRuntime(store)
        supervisor = LiveKitEvidenceIngressSupervisor(store, media_plane=LiveKitMediaPlane(_configuration()),
            ingress_factory=_FakeIngress, command_poll_seconds=.001)
        runtime.evidence_ingress = supervisor
        channel = await runtime.open(_opened("connection_original"))
        managed = channel._evidence_session
        _mutate(runtime, channel, _make_warmup)
        runtime._set_calibration(channel.interview_id, "listening", channel.organization_id)
        publisher = _Publisher()
        monkeypatch.setattr(interview_agent, "LiveKitApprovedAudioPublisher", lambda *a, **k: publisher)
        calls, plans, recordings = [], [], []

        async def unavailable(*args, **kwargs):
            calls.append("stream")
            raise ProviderError(code, "Synthetic provider failure.", retryable=code == "provider_timeout")

        async def forbidden_batch(*args, **kwargs):
            raise AssertionError("An unavailable provider must not be called again through batch synthesis")

        async def recording(*args, **kwargs):
            recordings.append(True)
            return {"status": "recording"}

        monkeypatch.setattr(runtime.gateway, "open_tts_stream", unavailable)
        monkeypatch.setattr(runtime, "_expression_audio", forbidden_batch)
        monkeypatch.setattr(runtime.media_captures, "start_for_connection", recording)
        monkeypatch.setattr(channel, "_schedule_planning", lambda cause: plans.append(cause))
        original_open = MockProvider.open_stream
        async def synthetic_stt(provider, request, context):
            request = request.model_copy(update={"metadata": {**request.metadata,
                "development_transcript": "这是合成试音字幕。"}})
            return await original_open(provider, request, context)
        monkeypatch.setattr(MockProvider, "open_stream", synthetic_stt)
        try:
            await channel.send(ClientSignal(type="evidence.stream.open", idempotency_key="trial_open", payload={
                "content_type": "audio/pcm", "sample_rate_hz": 16000, "channels": 1}))
            assert managed._capture_id
            await managed._ingress.push(sequence=1, pcm_s16le=b"\x01\x00" * 320)
            await managed.dispatch(channel, ClientSignal(type="evidence.finish", idempotency_key="trial_finish",
                payload={"capture_id": managed._capture_id}))
            current = runtime.interviews.get_interview(channel.interview_id)
            assert current["status"] == "in_progress"
            assert current["agent_runtime"]["calibration_status"] == "awaiting_confirmation", current["agent_runtime"]
            assert current["agent_runtime"]["floor"] == "candidate"
            assert not current["turns"] and not current["answers"] and not plans and not recordings
            assert calls == ["stream"] and publisher.abort_calls == 1
            assert not current["agent_runtime"].get("expression_replay_act_event_id")
            problem = current["agent_runtime"]["problems"][-1]
            assert problem["code"] == _NOTICE and problem["recoverable"]
            assert problem["cause_code"] == code.upper()
            act = current["agent_runtime"]["session_conversation_acts"][-1]
            assert act["selection_receipts"][-1]["delivery"]["status"] == "failed"
            events = list(channel._queue._queue)
            final = next(event for event in events if event.type == "transcript.final")
            assert final.payload["text"] == "这是合成试音字幕。" and final.payload["ephemeral"]
            notice = next(event for event in events if event.type == "problem" and event.payload["code"] == _NOTICE)
            assert notice.payload["calibration"] and notice.payload["recoverable"]
            assert "cause_code" not in notice.payload and "Synthetic" not in notice.payload["message"]
            assert "字幕已保留" in notice.payload["message"]
            snapshot = [event for event in events if event.type == "session.snapshot"][-1]
            assert snapshot.payload["status"] == "in_progress"
            assert snapshot.payload["calibration_status"] == "awaiting_confirmation"
            assert snapshot.payload["floor"] == "candidate"
            await channel.send(ClientSignal(type=control, idempotency_key="explicit_trial_control"))
            current = runtime.interviews.get_interview(channel.interview_id)
            assert current["status"] == "in_progress" and not current["answers"]
            assert current["agent_runtime"]["calibration_status"] == ("completed" if control == "warmup.confirm" else "retrying")
            assert len(plans) == (1 if control == "warmup.confirm" else 0)
            assert not recordings and calls == ["stream"]
        finally:
            await managed.stop("synthetic_complete")
            await channel.close("synthetic_complete")
    asyncio.run(scenario())


@pytest.mark.parametrize("failure_stage", ["ready", "zero_pcm", "partial_pcm", "publish_started"])
def test_only_zero_pcm_playback_failure_returns_to_confirmation(monkeypatch, failure_stage):
    async def scenario():
        h = _streaming_harness(monkeypatch)
        _mutate(h.runtime, h.channel, _make_warmup)
        h.channel._evidence_session._capture_id = "synthetic_trial_capture"
        try:
            performance = await _select_confirmation(h)
            item = h.outputs[0]
            task = h.channel._speech_output_task
            if failure_stage == "ready":
                item.output._ready_timeout = .01
            else:
                item.raw.chunks = [] if failure_stage == "zero_pcm" else [b"\x01\x00" * 480]
                item.raw.error = ProviderError("provider_timeout", "Synthetic stream failed.", retryable=True)
                if failure_stage == "publish_started":
                    publish = item.publisher.publish
                    async def failed_write(chunk):
                        item.publisher.pcm_delivery_started = True
                        await publish(chunk)
                        raise ApiError("LIVEKIT_AUDIO_OUTPUT_TIMEOUT", "Synthetic uncertain partial write.", status_code=504)
                    item.publisher.publish = failed_write
                await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
            await asyncio.wait_for(task, 2)
            current = _session(h)
            recoverable = failure_stage in {"ready", "zero_pcm"}
            assert current["status"] == ("in_progress" if recoverable else "paused")
            assert current["agent_runtime"]["calibration_status"] == "awaiting_confirmation"
            assert not current["answers"] and not current["turns"]
            assert not current["agent_runtime"].get("active_performance_id")
            assert not current["agent_runtime"].get("expression_replay_act_event_id")
            if recoverable:
                assert current["agent_runtime"]["problems"][-1]["code"] == _NOTICE
                assert item.publisher.interruption_before_abort
                assert not item.publisher.published
                assert current["agent_runtime"]["floor"] == "candidate"
                delivery = current["agent_runtime"]["session_conversation_acts"][-1]["selection_receipts"][-1]["delivery"]
                assert delivery["status"] == "failed" and delivery["performance_id"] == performance.performance_id
            else:
                assert current["interruption"]["reason"] == "agent_expression_unavailable"
                assert current["agent_runtime"]["problems"][-1]["code"] != _NOTICE
            await _signal(h, "avatar.performance.stopped", performance.performance_id, item.output.output_id, reason="drained")
            assert _session(h)["status"] == current["status"] and len(h.outputs) == 1
        finally:
            await h.channel.close("synthetic_complete")
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["pause", "takeover", "confirm", "retry", "new_capture", "owner", "control", "control_generation", "new_act"])
def test_late_preparation_failure_never_changes_superseding_authority(monkeypatch, change):
    async def scenario():
        h = _streaming_harness(monkeypatch)
        _mutate(h.runtime, h.channel, _make_warmup)
        managed = h.channel._evidence_session
        managed._capture_id = "synthetic_trial_capture"
        reached, release = asyncio.Event(), asyncio.Event()

        async def held(*args, **kwargs):
            reached.set()
            await release.wait()
            raise ProviderError("provider_timeout", "Synthetic late timeout.", retryable=True)

        monkeypatch.setattr(h.runtime, "_open_streamed_expression", held)
        task = asyncio.create_task(_select_confirmation(h))
        await asyncio.wait_for(reached.wait(), 1)
        if change == "control_generation":
            h.coordinator.attach_control(interview_id=h.channel.interview_id, organization_id=h.channel.organization_id,
                connection_id="new_control", local_instance_id=h.grant.local_instance_id)
        elif change == "owner":
            assert h.coordinator.release(h.grant)
            h.coordinator.attach_control(interview_id=h.channel.interview_id, organization_id=h.channel.organization_id,
                connection_id="new_control", local_instance_id="new_owner")
        elif change == "control":
            def stale(_channel):
                raise ApiError("EVIDENCE_CONTROL_STALE", "Synthetic replacement.", status_code=409)
            managed.assert_controller = stale
        elif change == "new_capture":
            managed._capture_id = "new_trial_capture"
        else:
            def replace(session):
                state = session["agent_runtime"]
                if change == "pause":
                    session["status"] = "paused"
                elif change == "takeover":
                    state.update(floor="human", takeover={"lease_id": "new_takeover"})
                elif change in {"confirm", "retry"}:
                    state["calibration_status"] = "completed" if change == "confirm" else "retrying"
                else:
                    session["agent_events"].append({"type": "conversation.act.selected", "event_id": "new_act",
                        "payload": {"act_id": "new_act", "approved": True}})
            _mutate(h.runtime, h.channel, replace)
        before = _session(h)
        release.set()
        assert await asyncio.wait_for(task, 1) is None
        after = _session(h)
        assert after["status"] == before["status"]
        assert after["agent_runtime"] == before["agent_runtime"]
        assert after["agent_events"] == before["agent_events"]
    asyncio.run(scenario())


def test_released_takeover_history_does_not_block_unplayed_trial_recovery(monkeypatch):
    async def scenario():
        h = _streaming_harness(monkeypatch)
        _mutate(h.runtime, h.channel, _make_warmup)
        _mutate(h.runtime, h.channel, lambda s: s["agent_runtime"].update(
            takeover={"status": "released", "expires_at": "2999-01-01T00:00:00Z"}))
        async def failed(*args, **kwargs):
            failure = ProviderError("provider_timeout", "Synthetic timeout.", retryable=True)
            # This fixture creates no transport, matching the real open seam's
            # affirmative cleanup receipt for a zero-audio failure.
            failure.speech_preparation_closed = True
            raise failure
        monkeypatch.setattr(h.runtime, "_open_streamed_expression", failed)
        assert await _select_confirmation(h) is None
        assert _session(h)["status"] == "in_progress"
        assert _session(h)["agent_runtime"]["problems"][-1]["code"] == _NOTICE
    asyncio.run(scenario())


def test_session_delivery_receipts_are_exact_and_do_not_become_turn_evidence():
    from copy import deepcopy
    from app.services.conversation_delivery import record_selection, update_delivery, mark_unplayed_selection, selected_events
    act = {"act_id": "session_act", "turn_id": None, "text": "请确认字幕。"}
    formal = {"id": "formal_turn", "conversation_acts": [{"act_id": "formal_act", "turn_id": "formal_turn", "text": "正式提问。"}]}
    session = {"turns": [formal], "agent_runtime": {"session_conversation_acts": [act]}}
    event = {"type": "conversation.act.selected", "event_id": "session_selection", "turn_id": None,
        "session_sequence": 1, "occurred_at": "2026-10-18T00:00:00Z",
        "payload": {"act_id": act["act_id"], "text": act["text"], "approved": True}}
    record_selection(session, {**event, "turn_id": "formal_turn"})
    assert "selection_receipts" not in act
    record_selection(session, event)
    record_selection(session, event)
    assert len(act["selection_receipts"]) == 1
    before = deepcopy(formal)
    assert not mark_unplayed_selection(session, event_id="other_selection", status="failed", updated_at="now")
    assert update_delivery(session, event_id=event["event_id"], performance_id="trial_output", status="selected", updated_at="now")
    assert not update_delivery(session, event_id=event["event_id"], performance_id="wrong_output", status="delivered", updated_at="now")
    assert update_delivery(session, event_id=event["event_id"], performance_id="trial_output", status="failed", updated_at="now")
    assert not update_delivery(session, event_id=event["event_id"], performance_id="trial_output", status="delivered", updated_at="now")
    assert selected_events(session, formal) == [] and formal == before


@pytest.mark.parametrize("code", ["provider_bad_request", "provider_auth_failed", "provider_rate_limited", "provider_billing_required"])
def test_rejected_provider_request_is_not_retried_through_batch(monkeypatch, code):
    from test_followup_expression_failure_071 import _open, _transport_harness
    async def scenario():
        runtime, channel, publisher, _, _ = _transport_harness(monkeypatch)
        failure = ProviderError(code, "Synthetic rejection.", retryable=False)
        async def rejected(*args, **kwargs):
            raise failure
        monkeypatch.setattr(runtime.gateway, "open_tts_stream", rejected)
        with pytest.raises(ProviderError) as error:
            await _open(runtime, channel)
        assert error.value is failure and publisher.abort_calls == 1
    asyncio.run(scenario())
