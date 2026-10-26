"""Synthetic approved-child recovery: no parent resubmission or implicit replay."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.core.time import utc_now
from app.domain.interview_agent import ApprovedConversationAct, ClientSignal
from app.model_gateway.errors import ProviderError
from app.services.livekit_evidence_ingress import ManagedLiveKitEvidenceSession
from app.services.interview_agent import _AGENT_CHANNEL_HUB
from test_expression_stale_fence import _mutate
from test_streaming_expression_lifecycle import (
    _client_ready, _replacement_channel, _session, _signal, _streaming_harness, _wait_until,
)

_TEXT = "你刚才提到重试，请说明哪些错误可以重试，以及如何设置退避。"


def _followup(monkeypatch):
    h = _streaming_harness(monkeypatch)
    frozen = ApprovedConversationAct(act_id="frozen_child_act", act_type="followup", text=_TEXT,
        turn_id="turn_1", root_turn_id="parent_turn", followup_depth=1,
        evidence_quotes=["使用重试机制"], target_capability_points=["重试与退避"],
        approved_by="controlled_decision_gate", created_at=utc_now()).model_dump(mode="json")
    def seed(session):
        session["turns"][0].update(is_followup=True, parent_turn_id="parent_turn", root_turn_id="parent_turn",
            followup_depth=1, question_spoken_text=_TEXT, conversation_acts=[frozen])
        session["turns"].insert(0, {"id": "parent_turn", "status": "completed", "order": 0,
                                   "question_spoken_text": "请说明 API 客户端。", "is_followup": False})
        session["answers"] = [{"id": "parent_answer", "turn_id": "parent_turn", "transcript": "使用重试机制"}]
    _mutate(h.runtime, h.channel, seed)
    _AGENT_CHANNEL_HUB.register(h.channel)
    h.open_stream = h.runtime._open_streamed_expression
    return h


async def _select(h):
    return await h.channel._select_act(act_type="followup", text=_TEXT, turn_id="turn_1",
        causation_id="synthetic_child", evidence_refs=["使用重试机制"], gesture="look_at_candidate")


def _marker(h):
    return (_session(h).get("agent_runtime") or {}).get("question_audio_recovery")


async def _fail_open(h, code="provider_timeout"):
    async def unavailable(*args, **kwargs):
        error = ProviderError(code, "Synthetic provider error, never exposed.", retryable=True)
        error.speech_preparation_closed = True
        raise error
    h.runtime._open_streamed_expression = unavailable
    assert await _select(h) is None
    h.runtime._open_streamed_expression = h.open_stream
    assert _marker(h)["status"] == "retry_required"
    return deepcopy(_marker(h))


async def _retry(h, marker, *, key="explicit_retry", token=None):
    await h.channel.send(ClientSignal(type="request_repeat", idempotency_key=key, turn_id="turn_1",
        payload={"question_audio_recovery_event_id": token or marker["selection_event_id"],
                 "text": "Client text must never replace the frozen question."}))


@pytest.mark.parametrize("code", ["provider_timeout", "provider_auth_failed", "provider_billing_required"])
def test_zero_audio_failure_retries_exact_approved_child_only_after_explicit_command(monkeypatch, code):
    async def scenario():
        h = _followup(monkeypatch)
        original = _session(h)
        try:
            marker = await _fail_open(h, code)
            failed = _session(h)
            assert failed["status"] == "in_progress" and failed["agent_runtime"]["floor"] == "none"
            assert failed["answers"] == original["answers"] and failed["current_turn_id"] == "turn_1"
            assert len(failed["turns"]) == 2
            assert failed["turns"][1]["conversation_acts"][0]["selection_receipts"][-1]["delivery"]["status"] == "failed"
            snapshot = h.runtime._snapshot(failed, h.channel.principal)
            assert snapshot["question_audio_recovery"] == {key: marker[key] for key in ("status", "turn_id", "selection_event_id")}
            problem = next(event for event in reversed(h.emitted) if event["type"] == "problem")
            assert problem["payload"]["action"] == "retry_question_audio"
            assert "Synthetic" not in problem["payload"]["message"]
            await _retry(h, marker, key="old_token", token="obsolete_selection")
            assert not h.outputs and _marker(h) == marker
            await _retry(h, marker)
            assert len(h.outputs) == 1
            active = _marker(h)
            assert active["status"] == "retrying" and active["act_id"] == marker["act_id"]
            assert active["selection_event_id"] != marker["selection_event_id"]
            assert _session(h)["agent_runtime"]["floor"] == "agent"
            # Duplicate command and obsolete selection with a new key cannot start another output.
            await _retry(h, marker)
            await _retry(h, marker, key="obsolete_retry")
            assert len(h.outputs) == 1
            item = h.outputs[0]
            task = h.channel._speech_output_task
            await _signal(h, "avatar.performance.stopped", item.output.performance_id, item.output.output_id, "drained")
            assert _marker(h)["status"] == "retrying"
            await _signal(h, "avatar.performance.ready", item.output.performance_id, item.output.output_id)
            await _wait_until(lambda: item.output.producer_finished)
            assert _session(h)["agent_runtime"]["floor"] == "agent" and _marker(h)
            await h.channel.send(ClientSignal(type="avatar.performance.stopped", idempotency_key="wrong_turn_drain",
                turn_id="parent_turn", payload={"performance_id": item.output.performance_id,
                    "output_id": item.output.output_id, "reason": "drained"}))
            assert _marker(h)["status"] == "retrying" and not item.output.playback_drained
            await _signal(h, "avatar.performance.stopped", item.output.performance_id, item.output.output_id, "drained")
            await asyncio.wait_for(task, 1)
            current = _session(h)
            assert current["agent_runtime"]["floor"] == "candidate" and not _marker(h)
            assert current["answers"] == original["answers"] and len(current["turns"]) == 2
            assert len(current["turns"][1]["conversation_acts"]) == 1
            assert current["turns"][1]["conversation_acts"][0]["text"] == _TEXT
            assert current["turns"][1]["conversation_acts"][0]["selection_receipts"][-1]["delivery"]["status"] == "delivered"
            snapshots = [event for event in h.channel._queue._queue if event.type == "session.snapshot"]
            assert snapshots[-1].payload["question_audio_recovery"] is None
            released = next(event for event in reversed(h.emitted) if event["type"] == "floor.changed"
                            and event["payload"].get("owner") == "candidate")
            assert snapshots[-1].session_sequence < released["session_sequence"]
        finally:
            await h.channel.close("synthetic_complete")
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["ready", "zero_pcm", "partial_pcm", "publish_started"])
def test_only_proven_unplayed_closed_output_becomes_retryable(monkeypatch, stage):
    async def scenario():
        h = _followup(monkeypatch)
        try:
            performance = await _select(h)
            item, task = h.outputs[0], h.channel._speech_output_task
            if stage == "ready":
                item.output._ready_timeout = .01
            else:
                item.raw.chunks = [] if stage == "zero_pcm" else [b"\1\0" * 480]
                item.raw.error = ProviderError("provider_timeout", "Synthetic interrupted PCM.", retryable=True)
                if stage == "publish_started":
                    async def uncertain_write(_chunk):
                        item.publisher.pcm_delivery_started = True
                        raise ApiError("LIVEKIT_AUDIO_OUTPUT_TIMEOUT", "Synthetic uncertain write.", status_code=504)
                    item.publisher.publish = uncertain_write
                await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
            await asyncio.wait_for(task, 1)
            current = _session(h)
            recoverable = stage in {"ready", "zero_pcm"}
            assert current["status"] == ("in_progress" if recoverable else "paused")
            assert bool(_marker(h)) is recoverable
            assert item.output.closed and item.publisher.abort_calls >= 1
            assert current["agent_runtime"]["floor"] == "none"
            assert len(current["answers"]) == 1 and len(current["turns"]) == 2
            if recoverable:
                assert item.publisher.interruption_before_abort
            assert h.runtime._recover_streamed_expression(h.channel.interview_id, h.channel.organization_id) is None
        finally:
            await h.channel.close("synthetic_complete")
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["retry_required", "retrying"])
@pytest.mark.parametrize("kind", ["evidence.stream.open", "speech.started", "continue_speaking"])
def test_candidate_signals_cannot_open_capture_or_grant_floor_before_recovery_drains(monkeypatch, status, kind):
    async def scenario():
        h = _followup(monkeypatch)
        try:
            marker = await _fail_open(h)
            if status == "retrying":
                await _retry(h, marker)
            before = _session(h)
            calls = []
            async def forbidden(*args, **kwargs):
                calls.append(True)
                raise AssertionError("Unplayed question cannot authorize capture")
            h.channel._evidence_session.dispatch = forbidden
            await h.channel.send(ClientSignal(type=kind, idempotency_key="premature_input", turn_id="turn_1"))
            current = _session(h)
            assert not calls and current["answers"] == before["answers"]
            assert current["agent_runtime"]["floor"] == before["agent_runtime"]["floor"]
            assert _marker(h)["status"] == status
            rejected = [event for event in h.channel._queue._queue if event.type == "problem"][-1]
            assert rejected.payload["code"] == "QUESTION_AUDIO_RETRY_REQUIRED"
            assert rejected.payload["recoverable"] is True
            assert rejected.payload["action"] == "retry_question_audio"
            assert set(rejected.payload) == {"code", "message", "recoverable", "action"}
        finally:
            await h.channel.close("synthetic_complete")
    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["_apply_open", "_apply_speech_started"])
def test_owner_executor_rechecks_gate_for_previously_queued_commands(monkeypatch, method):
    async def scenario():
        h = _followup(monkeypatch)
        try:
            await _fail_open(h)
            managed = SimpleNamespace(interview_id=h.channel.interview_id, organization_id=h.channel.organization_id,
                                      ingress_connected=True)
            with pytest.raises(ApiError) as error:
                await getattr(ManagedLiveKitEvidenceSession, method)(managed, h.channel,
                    ClientSignal(type="evidence.stream.open" if method == "_apply_open" else "speech.started",
                                 idempotency_key="queued_before_failure", turn_id="turn_1"))
            assert error.value.code == "QUESTION_AUDIO_RETRY_REQUIRED"
            assert _session(h)["agent_runtime"]["floor"] == "none"
        finally:
            await h.channel.close("synthetic_complete")
    asyncio.run(scenario())


@pytest.mark.parametrize("replacement", ["pause", "turn", "selection", "owner", "control", "takeover"])
def test_old_failure_cannot_create_recovery_under_replacement_authority(monkeypatch, replacement):
    async def scenario():
        h = _followup(monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            error = ProviderError("provider_timeout", "Synthetic old failure.", retryable=True)
            error.speech_preparation_closed = True
            raise error
        h.runtime._open_streamed_expression = held
        task = asyncio.create_task(_select(h))
        await entered.wait()
        try:
            if replacement in {"owner", "control"}:
                if replacement == "owner":
                    assert h.coordinator.release(h.grant)
                h.coordinator.attach_control(interview_id=h.channel.interview_id, organization_id=h.channel.organization_id,
                    connection_id="replacement", local_instance_id="new_owner" if replacement == "owner" else h.grant.local_instance_id)
            else:
                def change(session):
                    if replacement == "pause":
                        session["status"] = "paused"
                    elif replacement == "turn":
                        session["current_turn_id"] = "different_turn"
                    elif replacement == "takeover":
                        session["agent_runtime"]["takeover"] = {"status": "active"}
                        session["agent_runtime"]["floor"] = "human"
                    else:
                        newer = deepcopy(session["agent_events"][-1])
                        newer["event_id"] = "newer_selection"
                        newer["session_sequence"] += 1
                        session["agent_events"].append(newer)
                _mutate(h.runtime, h.channel, change)
            before = _session(h)
            release.set()
            assert await asyncio.wait_for(task, 1) is None
            after = _session(h)
            assert after == before and not _marker(h)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await h.channel.close("synthetic_complete")
    asyncio.run(scenario())


def test_failed_question_survives_reconnect_without_automatic_synthesis(monkeypatch):
    async def scenario():
        h = _followup(monkeypatch)
        replacement = None
        try:
            marker = await _fail_open(h)
            await h.channel.close("synthetic_disconnect")
            replacement = await _replacement_channel(h)
            await _client_ready(replacement)
            assert _marker(replacement) == marker and not h.outputs
            assert _session(replacement)["status"] == "in_progress"
            snapshots = [event for event in replacement.channel._queue._queue if event.type == "session.snapshot"]
            assert snapshots[-1].payload["question_audio_recovery"]["selection_event_id"] == marker["selection_event_id"]
            await _retry(replacement, marker)
            assert len(h.outputs) == 1 and _marker(replacement)["status"] == "retrying"
        finally:
            await h.channel.close("synthetic_complete")
            if replacement:
                await replacement.channel.close("synthetic_complete")
    asyncio.run(scenario())


@pytest.mark.parametrize("disconnect", ["close", "crash", "preparation_crash", "manual_pause"])
def test_uncertain_retry_is_paused_and_human_resume_can_repeat_original_question(monkeypatch, disconnect):
    async def scenario():
        h = _followup(monkeypatch)
        replacement = None
        try:
            marker = await _fail_open(h)
            if disconnect != "preparation_crash":
                await _retry(h, marker)
            else:
                _mutate(h.runtime, h.channel, lambda s: s["agent_runtime"]["question_audio_recovery"].update(status="retrying"))
            if disconnect == "close":
                await h.channel.close("synthetic_disconnect")
            elif disconnect == "manual_pause":
                h.runtime.interviews.pause_interview(h.channel.interview_id)
                await h.channel._cancel_speech_output()
            else:
                # A crashed process has lost its objects but retained durable active IDs.
                await h.channel._cancel_speech_output()
                assert h.runtime._recover_streamed_expression(h.channel.interview_id, h.channel.organization_id) is None
            current = _session(h)
            assert current["status"] == "paused" and not _marker(h)
            assert not current["agent_runtime"].get("expression_replay_act_event_id")
            assert current["current_turn_id"] == "turn_1" and len(current["answers"]) == 1
            h.runtime.interviews.resume_interview(h.channel.interview_id)
            replacement = await _replacement_channel(h)
            await _client_ready(replacement)
            assert _session(replacement)["status"] == "in_progress" and not _marker(replacement)
            before = len(h.outputs)
            await replacement.channel.send(ClientSignal(type="request_repeat", idempotency_key="human_resumed_repeat", turn_id="turn_1"))
            assert len(h.outputs) == before + 1
            assert _session(replacement)["current_turn_id"] == "turn_1" and len(_session(replacement)["answers"]) == 1
        finally:
            await h.channel.close("synthetic_complete")
            if replacement:
                await replacement.channel.close("synthetic_complete")
    asyncio.run(scenario())


@pytest.mark.parametrize("active", [False, True])
def test_closed_parent_endpoint_is_not_an_active_answer_capture(monkeypatch, active):
    async def scenario():
        h = _followup(monkeypatch)
        h.channel._evidence_session._answer_endpoint = SimpleNamespace(
            capture=SimpleNamespace(is_open=active), revision=1, confirmation=None)
        async def failed(*args, **kwargs):
            error = ProviderError("provider_auth_failed", "Synthetic failure.", retryable=False)
            error.speech_preparation_closed = True
            raise error
        h.runtime._open_streamed_expression = failed
        try:
            assert await _select(h) is None
            assert _session(h)["status"] == ("paused" if active else "in_progress")
            assert bool(_marker(h)) is not active
        finally:
            await h.channel.close("synthetic_complete")
    asyncio.run(scenario())


@pytest.mark.parametrize("proof", [None, False])
def test_unproven_transport_cleanup_never_grants_question_retry(monkeypatch, proof):
    async def scenario():
        h = _followup(monkeypatch)
        async def failed(*args, **kwargs):
            error = ProviderError("provider_timeout", "Synthetic failure.", retryable=True)
            if proof is not None:
                error.speech_preparation_closed = proof
            raise error
        h.runtime._open_streamed_expression = failed
        try:
            assert await _select(h) is None
            assert _session(h)["status"] == "paused" and not _marker(h)
        finally:
            await h.channel.close("synthetic_complete")
    asyncio.run(scenario())


def test_closed_flag_without_completed_cleanup_is_not_a_recovery_proof(monkeypatch):
    async def scenario():
        h = _followup(monkeypatch)
        try:
            performance = await _select(h)
            item, task = h.outputs[0], h.channel._speech_output_task
            item.raw.chunks = []
            item.raw.error = ProviderError("provider_timeout", "Synthetic failure.", retryable=True)
            async def failed_cleanup():
                raise RuntimeError("Synthetic cleanup failure")
            item.publisher.abort = failed_cleanup
            await _signal(h, "avatar.performance.ready", performance.performance_id, item.output.output_id)
            await asyncio.wait_for(task, 1)
            assert item.output.closed and not item.output.cleanup_complete
            assert _session(h)["status"] == "paused" and not _marker(h)
        finally:
            await h.channel.close("synthetic_complete")
    asyncio.run(scenario())


@pytest.mark.parametrize("cleanup_failure", [None, "stream", "publisher"])
def test_preparation_exception_carries_proof_only_after_transport_cleanup(monkeypatch, cleanup_failure):
    from test_followup_expression_failure_071 import _transport_harness, _open
    async def scenario():
        runtime, channel, publisher, _, _ = _transport_harness(monkeypatch)
        error = ApiError("SYNTHETIC_MEDIA_FAILURE", "Synthetic transport failure.", status_code=503)
        cleaned = []
        async def abort_stream():
            cleaned.append("stream")
            if cleanup_failure == "stream":
                raise RuntimeError("Synthetic cleanup failure")
        async def provider(*args, **kwargs):
            return SimpleNamespace(abort=abort_stream)
        async def publish():
            raise error
        async def abort_publisher():
            cleaned.append("publisher")
            if cleanup_failure == "publisher":
                raise RuntimeError("Synthetic cleanup failure")
        runtime.gateway.open_tts_stream = provider
        publisher.open, publisher.abort = publish, abort_publisher
        with pytest.raises(ApiError) as raised:
            await _open(runtime, channel)
        assert raised.value is error and cleaned == ["stream", "publisher"]
        assert getattr(error, "speech_preparation_closed", False) is (cleanup_failure is None)
    asyncio.run(scenario())


def test_failed_retry_requires_a_new_explicit_selection_token(monkeypatch):
    async def scenario():
        h = _followup(monkeypatch)
        try:
            first = await _fail_open(h)
            async def failed(*args, **kwargs):
                error = ProviderError("provider_timeout", "Synthetic retry failure.", retryable=True)
                error.speech_preparation_closed = True
                raise error
            h.runtime._open_streamed_expression = failed
            await _retry(h, first)
            second = _marker(h)
            assert second["status"] == "retry_required" and second["act_id"] == first["act_id"]
            assert second["selection_event_id"] != first["selection_event_id"]
            h.runtime._open_streamed_expression = h.open_stream
            await _retry(h, first, key="old_retry_token")
            assert not h.outputs and _marker(h) == second
            await _retry(h, second, key="new_explicit_retry")
            assert len(h.outputs) == 1 and _marker(h)["status"] == "retrying"
            assert len(_session(h)["answers"]) == 1
        finally:
            await h.channel.close("synthetic_complete")
    asyncio.run(scenario())


def test_cancelled_retry_preparation_does_not_forge_zero_audio_failure(monkeypatch):
    async def scenario():
        h = _followup(monkeypatch)
        entered = asyncio.Event()
        try:
            marker = await _fail_open(h)
            async def held(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
            h.runtime._open_streamed_expression = held
            task = asyncio.create_task(_retry(h, marker))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert _marker(h)["status"] == "retrying"
            assert h.runtime._recover_streamed_expression(h.channel.interview_id, h.channel.organization_id) is None
            assert _session(h)["status"] == "paused" and not _marker(h)
            assert len(_session(h)["answers"]) == 1
        finally:
            await h.channel.close("synthetic_complete")
    asyncio.run(scenario())


@pytest.mark.parametrize("replacement", ["controller", "owner"])
def test_old_channel_close_cleans_local_output_without_mutating_replacement_state(monkeypatch, replacement):
    from app.services.evidence_coordination import assert_current_evidence_control
    async def scenario():
        h = _followup(monkeypatch)
        marker = await _fail_open(h)
        await _retry(h, marker)
        item, task = h.outputs[0], h.channel._speech_output_task
        def assert_controller(_channel):
            with h.runtime.persistence.transaction(h.channel.organization_id, read_only=True) as transaction:
                assert_current_evidence_control(transaction, h.grant)
        h.channel._evidence_session.assert_controller = assert_controller
        if replacement == "owner":
            assert h.coordinator.release(h.grant)
        h.coordinator.attach_control(interview_id=h.channel.interview_id, organization_id=h.channel.organization_id,
            connection_id="replacement_control", local_instance_id="new_owner" if replacement == "owner" else h.grant.local_instance_id)
        before = _session(h)
        await h.channel.close("superseded_control_closed")
        after = _session(h)
        assert h.channel._closed and item.output.cleanup_complete and task.done()
        assert after["status"] == before["status"] == "in_progress"
        assert after["answers"] == before["answers"] and after["turns"] == before["turns"]
        assert after["agent_events"] == before["agent_events"]
        for key in ("floor", "question_audio_recovery", "active_performance_id", "active_output_id",
                    "active_expression_act_event_id", "expression_replay_act_event_id"):
            assert after["agent_runtime"].get(key) == before["agent_runtime"].get(key)
    asyncio.run(scenario())
