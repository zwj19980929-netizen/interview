"""Recovery through the actual Evidence owner, capture, journal and private PCM."""
import asyncio

import pytest

from app.core.errors import ApiError
from app.domain.interview_agent import ClientSignal
from app.model_gateway.errors import ProviderError
from app.persistence.provider import persistence_for
from app.services.capture_recovery import candidate_capture_recovery
from app.services.interview_evidence import InterviewEvidenceChain

from test_automatic_turn_integration import (
    _automatic_session, _current, _feed, _silence_until, _assert_one_automatic_answer,
    _VOICE, _CONTINUATION, _PREFIX, _SUFFIX,
)
from test_evidence_owner_recovery_integration import ORGANIZATION_ID, TURN_ID, INTERVIEW_ID, _wait_until


def _fail_first_final(monkeypatch, managed):
    raw = managed.chain._stt.stream._stream._stream

    async def temporary_failure():
        raise ProviderError("provider_timeout", "secret upstream body must not be persisted", retryable=True)

    monkeypatch.setattr(raw, "finish", temporary_failure)


def test_real_capture_recovers_snapshot_timeout_and_commits_exact_pcm_once(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX, _PREFIX]) as (store, runtime, channel, managed):
            capture_id = managed._capture_id
            _fail_first_final(monkeypatch, managed)
            await _feed(managed._ingress, _VOICE, 10)
            await _silence_until(managed._ingress, lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            await _wait_until(lambda: all(x["status"] == "completed" for x in store.evidence_commands.values()))
            _assert_one_automatic_answer(store, runtime, _PREFIX, voice_frames=10)
            problems = _current(runtime)["agent_runtime"]["problems"]
            recovery = [p for p in problems if p["code"] == "CAPTURE_RECOVERING"]
            assert recovery and recovery[0]["cause_code"] == "PROVIDER_TIMEOUT"
            assert recovery[0]["stage"] == "snapshot"
            assert recovery[0]["capture_id"] == capture_id
            assert "secret upstream" not in str(problems)
            assert candidate_capture_recovery(_current(runtime)) is None

    asyncio.run(scenario())


def test_exhausted_recognition_keeps_question_and_allows_scoped_new_capture(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX, _SUFFIX]) as (store, runtime, channel, managed):
            old_capture = managed._capture_id
            _fail_first_final(monkeypatch, managed)
            original_recover = InterviewEvidenceChain.recover_capture

            async def unavailable(_chain):
                raise ProviderError("provider_network_error", "sensitive transport body", retryable=True)

            monkeypatch.setattr(InterviewEvidenceChain, "recover_capture", unavailable)
            managed._answer_endpoint.recovery_backoff = 0
            await _feed(managed._ingress, _VOICE, 10)
            await _silence_until(managed._ingress, lambda: not managed.chain.is_open, timeout=4)
            current = _current(runtime)
            assert current["status"] == "in_progress"
            assert current["answers"] == []
            assert current["current_turn_id"] == TURN_ID
            assert candidate_capture_recovery(current) == {
                "status": "retry_required", "turn_id": TURN_ID, "capture_id": old_capture,
                "attempt": 3, "max_attempts": 3,
            }
            stream = next(iter(store.evidence_media_streams.values()))
            assert not stream["complete"] and stream["sealed_byte_count"] >= 6400
            original_revision = stream["capture_revision"]
            # Ordinary open and an old/wrong retry cannot discard any evidence.
            await channel.send(ClientSignal(type="continue_speaking", idempotency_key="wrong_retry",
                                            turn_id=TURN_ID, payload={"capture_id": "capture_wrong"}))
            assert next(iter(store.evidence_media_streams.values()))["capture_revision"] == original_revision
            monkeypatch.setattr(InterviewEvidenceChain, "recover_capture", original_recover)
            original_open = managed.chain.open

            async def failed_retry_open(*args, **kwargs):
                raise ApiError("PROVIDER_NETWORK_ERROR", "sensitive upstream retry", status_code=502)

            monkeypatch.setattr(managed.chain, "open", failed_retry_open)
            await channel.send(ClientSignal(type="continue_speaking", idempotency_key="retry_failed",
                                            causation_id="retry_failed", turn_id=TURN_ID,
                                            payload={"capture_id": old_capture}))
            assert not managed.chain.is_open and managed._capture_id == old_capture
            assert candidate_capture_recovery(_current(runtime))["status"] == "retry_required"
            assert _current(runtime)["agent_runtime"]["problems"][-1]["stage"] == "retry"
            assert "sensitive upstream" not in str(_current(runtime)["agent_runtime"])
            monkeypatch.setattr(managed.chain, "open", original_open)
            await channel.send(ClientSignal(type="continue_speaking", idempotency_key="retry_current",
                                            causation_id="retry_current", turn_id=TURN_ID,
                                            payload={"capture_id": old_capture}))
            assert managed.chain.is_open and managed._capture_id != old_capture, repr([
                {k: v for k, v in c.items() if k in {"command_type", "status", "outcome", "last_error_code"}}
                for c in store.evidence_commands.values()])
            assert candidate_capture_recovery(_current(runtime)) is None
            restarted = next(iter(store.evidence_media_streams.values()))
            assert restarted["capture_revision"] == original_revision + 1
            assert len(restarted["abandoned_captures"]) == 1
            assert restarted["abandoned_captures"][0]["sealed_byte_count"] >= 6400
            await channel.send(ClientSignal(type="continue_speaking", idempotency_key="late_duplicate_retry",
                                            turn_id=TURN_ID, payload={"capture_id": old_capture}))
            assert next(iter(store.evidence_media_streams.values()))["capture_revision"] == original_revision + 1
            await _feed(managed._ingress, _CONTINUATION, 10)
            await _silence_until(managed._ingress, lambda: len(_current(runtime)["answers"]) == 1, timeout=4)
            await _wait_until(lambda: all(x["status"] == "completed" for x in store.evidence_commands.values()))
            _assert_one_automatic_answer(store, runtime, _SUFFIX, voice_frames=0, continuation_frames=10)

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["paused", "answered", "turn_changed"])
def test_stale_recovery_notice_cannot_resume_a_changed_session(tmp_path, monkeypatch, change):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX]) as (store, runtime, channel, managed):
            with persistence_for(store).transaction(ORGANIZATION_ID) as tx:
                session = tx.interview_sessions.get(INTERVIEW_ID)
                if change == "paused":
                    session["status"] = "paused"
                elif change == "answered":
                    session["answers"] = [{"id": "existing_answer", "turn_id": TURN_ID}]
                else:
                    session["current_turn_id"] = "another_turn"
                tx.interview_sessions.update(session, expected_version=session["version"])
            await managed._capture_recovery_notice("capture_recovered", managed._capture_id, TURN_ID, channel)
            assert "capture_recovery" not in _current(runtime)["agent_runtime"]
            assert candidate_capture_recovery(_current(runtime)) is None

    asyncio.run(scenario())


def test_send_buffer_exhaustion_closes_only_capture_not_livekit_or_interview(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX]) as (store, runtime, channel, managed):
            continuous = managed.chain._stt.stream
            # Shrink the same finite intake boundary instead of allocating
            # minutes of synthetic PCM; the rejected frame must never commit.
            continuous._rotating = True
            continuous._bytes_per_second = 11  # 660 bytes recovery capacity
            await managed._ingress.push(_VOICE)
            await managed._ingress.push(_CONTINUATION)
            await _wait_until(lambda: not managed.chain.is_open)
            current = _current(runtime)
            assert current["status"] == "in_progress" and not current["answers"]
            assert candidate_capture_recovery(current)["status"] == "retry_required"
            problem = current["agent_runtime"]["problems"][-1]
            assert problem["cause_code"] == "PROVIDER_AUDIO_RECOVERY_BUFFER_EXCEEDED"
            assert managed._ingress.connected
            stream = next(iter(store.evidence_media_streams.values()))
            assert stream["sealed_byte_count"] == len(_VOICE) and not stream["complete"]
            await managed._ingress.push(_CONTINUATION)
            assert next(iter(store.evidence_media_streams.values()))["sealed_byte_count"] == len(_VOICE)

    asyncio.run(scenario())


def test_failed_durable_recovery_notice_revokes_input_and_records_safe_failure(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX]) as (store, runtime, channel, managed):
            def failed_checkpoint(_chain):
                raise ApiError("EVIDENCE_MEDIA_CHECKSUM_MISMATCH", "private candidate details", status_code=409)

            monkeypatch.setattr(InterviewEvidenceChain, "checkpoint_incomplete", failed_checkpoint)
            continuous = managed.chain._stt.stream
            continuous._recovery_error = ProviderError("provider_audio_stream_too_large", "synthetic", retryable=False)
            await _wait_until(lambda: _current(runtime)["status"] == "paused")
            assert not managed.chain.is_open
            before = continuous._received_bytes
            await managed._ingress.push(_VOICE)
            assert continuous._received_bytes == before
            current = _current(runtime)
            assert not current["answers"]
            assert current["agent_runtime"]["problems"][-1]["cause_code"] == "EVIDENCE_MEDIA_CHECKSUM_MISMATCH"
            assert "private candidate" not in str(current["agent_runtime"]["problems"])

    asyncio.run(scenario())


def test_candidate_pause_during_recovery_cancels_worker_and_does_not_submit_prefix(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX]) as (store, runtime, channel, managed):
            entered, cancelled = asyncio.Event(), asyncio.Event()

            async def slow_recovery(_chain):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            monkeypatch.setattr(InterviewEvidenceChain, "recover_capture", slow_recovery)
            continuous = managed.chain._stt.stream
            continuous._recovery_error = ProviderError("provider_timeout", "synthetic", retryable=True)
            await asyncio.wait_for(entered.wait(), timeout=1)
            await channel.send(ClientSignal(type="pause", idempotency_key="pause_while_recovering"))
            assert cancelled.is_set() and not managed.chain.is_open
            assert candidate_capture_recovery(_current(runtime)) is None
            assert _current(runtime)["status"] == "paused" and not _current(runtime)["answers"]
            received = continuous._received_bytes
            await managed._ingress.push(_VOICE)
            assert continuous._received_bytes == received
            assert not [c for c in store.evidence_commands.values() if c["command_type"] == "evidence.seal"]

    asyncio.run(scenario())


def test_recovery_ui_projection_failure_does_not_pause_healthy_capture(tmp_path, monkeypatch):
    async def scenario():
        async with _automatic_session(tmp_path, monkeypatch, [_PREFIX, _PREFIX]) as (store, runtime, channel, managed):
            original_emit = channel._emit

            async def offline_projection(kind, payload, **kwargs):
                if kind == "floor.changed" and payload.get("reason") in {"answer_recovering", "answer_listening"}:
                    raise RuntimeError("synthetic disconnected control")
                return await original_emit(kind, payload, **kwargs)

            monkeypatch.setattr(channel, "_emit", offline_projection)
            _fail_first_final(monkeypatch, managed)
            await _feed(managed._ingress, _VOICE, 10)
            await _silence_until(managed._ingress, lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
            await _wait_until(lambda: all(c["status"] == "completed" for c in store.evidence_commands.values()))
            _assert_one_automatic_answer(store, runtime, _PREFIX, voice_frames=10)

    asyncio.run(scenario())
