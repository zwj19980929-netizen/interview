"""Exhausted understanding invites one new utterance, never a fabricated answer."""
import asyncio
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _endpoint, _final, _until
from test_recovery_episode_066 import RecoveryCapture


def setup():
    endpoint, clock, notices, commits = _endpoint(capture=RecoveryCapture())
    spoken = []

    async def speak(kind, guard):
        guard()
        spoken.append(kind)
        return True

    confirmation = endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint.cover_transcript(endpoint.capture.current_final)
    endpoint._proposal_revision = endpoint.revision
    for _ in range(2):
        endpoint._record_preparation_failure(problem=SimpleNamespace(
            source="schema_or_content", reason_code="invalid_result", retryable=True))
    return endpoint, confirmation, clock, notices, commits, spoken


@pytest.mark.anyio
async def test_exhausted_answer_speaks_once_and_only_completed_playback_changes_recovery_status():
    endpoint, confirmation, clock, notices, commits, spoken = setup()
    capture, deadline, failures = endpoint.capture, endpoint._interaction_deadline, endpoint._prepare_failures
    try:
        await confirmation._recover_interaction(endpoint, capture.current_final)
        assert spoken == ["reception_recovery_clarification"]
        assert "understanding_clarification" not in notices
        assert endpoint.preparation_blocked and not commits and not capture.prepares
        await confirmation.complete_playback(endpoint, reason="agent_finished", delivery_guard=endpoint.assert_current)
        assert notices.count("understanding_clarification") == 1
        assert confirmation.phase == "listening" and not confirmation.confirmed
        assert endpoint.capture is capture and capture.is_open
        assert endpoint._interaction_deadline == deadline and endpoint._prepare_failures == failures
        for _ in range(3):
            endpoint.speech_started(source="audio")
            clock.value += 20
            await confirmation.control_step(endpoint, quiet=20)
        assert spoken == ["reception_recovery_clarification"] and not capture.prepares and not commits
        assert endpoint.retry_answer_processing()
        assert not endpoint.preparation_blocked and confirmation.phase == "listening"
        for _ in range(2):
            endpoint._record_preparation_failure()
        endpoint._proposal_revision = endpoint.revision
        await confirmation._recover_interaction(endpoint, capture.current_final)
        assert spoken == ["reception_recovery_clarification"]
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["failed_selection", "tts_error", "barge_in", "stale_delivery", "new_input", "unplayed_cleanup"])
async def test_incomplete_or_stale_clarification_does_not_clear_failure_or_loop(outcome):
    endpoint, confirmation, _, notices, commits, spoken = setup()
    original = confirmation.speak

    async def speak(kind, guard):
        checkpoint = confirmation.begin_unplayed_speech()
        await original(kind, guard)
        if outcome == "tts_error":
            raise RuntimeError("synthetic speech failure")
        if outcome == "unplayed_cleanup":
            confirmation.restore_unplayed_speech(checkpoint)
            return False
        return outcome != "failed_selection"

    confirmation.speak = speak
    try:
        await confirmation._recover_interaction(endpoint, endpoint.capture.current_final)
        if confirmation.speaking:
            if outcome == "new_input":
                endpoint.speech_started(source="audio")

            def delivery_guard():
                if outcome == "stale_delivery":
                    raise ApiError("EVIDENCE_OWNER_FENCED", "Synthetic old owner", status_code=409)
                endpoint.assert_current()

            await confirmation.complete_playback(endpoint,
                reason="interrupted" if outcome == "barge_in" else "agent_finished", delivery_guard=delivery_guard)
        assert "understanding_clarification" not in notices
        assert endpoint.preparation_blocked and not commits
        endpoint._proposal_revision = endpoint.revision
        await confirmation._recover_interaction(endpoint, endpoint.capture.current_final)
        assert spoken == ["reception_recovery_clarification"]
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["confirmed", "exit", "exit_context", "content_clarification", "explicit_finish", "pause", "non_retryable"])
async def test_recovery_clarification_does_not_replace_another_authoritative_exchange(state):
    endpoint, confirmation, _, _, commits, spoken = setup()
    if state == "confirmed":
        confirmation.confirmed = True
    elif state == "exit":
        confirmation._exit_waiting = True
    elif state == "exit_context":
        confirmation._exit_context = {"delivered": True}
    elif state == "content_clarification":
        confirmation._content_question_focus = "synthetic focus"
    elif state == "explicit_finish":
        endpoint._explicit_revision = endpoint.revision
    elif state == "pause":
        confirmation.phase = "pause_requested"
    else:
        endpoint._recovery_retryable = False
    try:
        await confirmation._recover_interaction(endpoint, endpoint.capture.current_final)
        assert "reception_recovery_clarification" not in spoken and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_background_failure_after_notice_uses_endpoint_loop_for_one_clarification():
    endpoint, confirmation, clock, notices, commits, spoken = setup()
    # This is the retained task finishing after its old waiter returned. The
    # endpoint loop must handle its failure without a second semantic caller.
    confirmation._failure_spoken_identity = True
    endpoint.speech_started(source="audio")
    endpoint.start()
    try:
        clock.value += 10
        await _until(lambda: spoken)
        assert spoken == ["reception_recovery_clarification"]
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        await _until(lambda: "understanding_clarification" in notices)
        assert not commits and not endpoint.capture.prepares
        clock.value += 30
        await asyncio.sleep(.1)
        assert spoken == ["reception_recovery_clarification"]
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_new_authoritative_words_after_clarification_get_one_normal_bounded_analysis():
    endpoint, confirmation, clock, notices, commits, spoken = setup()
    try:
        await confirmation._recover_interaction(endpoint, endpoint.capture.current_final)
        await confirmation.complete_playback(endpoint, reason="agent_finished")
        capture = endpoint.capture
        fresh = _final(capture.current_final.text + "我想问的是题目中的术语，请解释一下。")
        capture.current_final = fresh
        endpoint.observe_transcript(fresh.text)
        endpoint.start()
        clock.value += 10
        await _until(lambda: len(capture.prepares) == 1)
        await _until(lambda: endpoint._prepare_failures == 3)
        clock.value += 30
        await asyncio.sleep(.1)
        assert capture.prepares[0].text == fresh.text
        assert len(capture.prepares) == 1 and not commits and not confirmation.confirmed
        assert endpoint.capture is capture and capture.is_open
        assert spoken == ["reception_recovery_clarification"]
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("next_action", ["retry", "fresh_speech"])
async def test_real_output_completion_persists_clarifying_and_continues_same_capture(tmp_path, monkeypatch, next_action):
    from app.core.prompt.conversation_reception import RECEPTION_SPEECH, RECEPTION_RECOVERY_SPEECH_VERSION
    from app.domain.interview_agent import ClientSignal
    from test_automatic_turn_integration import _automatic_session, _current, _feed, _VOICE, _PREFIX
    from test_evidence_owner_recovery_integration import TURN_ID
    from test_spoken_supplement_integration import _synthetic_tts, _finish_playback

    async with _automatic_session(tmp_path, monkeypatch, [_PREFIX, "补充说明，我会检查任务重试次数。"],
                                  spoken_confirmation=True) as (store, runtime, channel, managed):
        spoken = _synthetic_tts(runtime)
        endpoint, capture_id, chain = managed._answer_endpoint, managed._capture_id, managed.chain
        await _feed(managed._ingress, _VOICE, 3)
        endpoint._proposal_revision = endpoint.revision
        final = await endpoint.listening_snapshot()
        assert final is not None
        for _ in range(2):
            endpoint._record_preparation_failure(problem=SimpleNamespace(
                source="schema_or_content", reason_code="invalid_result", retryable=True))
        await endpoint._understanding_failed(record_failure=False)
        await endpoint.confirmation._recover_interaction(endpoint, final)
        assert _current(runtime)["agent_runtime"]["answer_preparation"]["status"] == "retry_required"
        await _finish_playback(channel, runtime, managed)
        await _until(lambda: _current(runtime)["agent_runtime"]["answer_preparation"]["status"] == "clarifying")
        current = _current(runtime)
        assert spoken == [RECEPTION_SPEECH["recovery_clarification"]]
        act = current["turns"][0]["conversation_acts"][-1]
        assert act["prompt_version"] == RECEPTION_RECOVERY_SPEECH_VERSION
        assert not current["answers"] and managed.chain is chain and managed._capture_id == capture_id
        assert chain.is_open and managed.evidence_open
        assert not [item for item in store.evidence_commands.values() if item["command_type"] == "evidence.seal"]
        await endpoint._notify("answer_listening")
        assert _current(runtime)["agent_runtime"]["answer_preparation"]["status"] == "clarifying"
        if next_action == "fresh_speech":
            entered = asyncio.Event()

            async def prepare_new_words(final, **kwargs):
                assert "补充说明" in final.text
                entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(chain, "prepare_decision", prepare_new_words)
            await _feed(managed._ingress, _VOICE, 3)
            endpoint._proposal_revision = endpoint.revision
            fresh = await endpoint.listening_snapshot()
            pending = asyncio.create_task(endpoint.confirmation._semantic_turn(endpoint, fresh))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                assert _current(runtime)["agent_runtime"]["answer_preparation"]["status"] == "preparing"
                assert managed.chain is chain and managed._capture_id == capture_id and not _current(runtime)["answers"]
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            return
        await channel.send(ClientSignal(type="retry_answer_processing", turn_id=TURN_ID,
            idempotency_key="retry_clarifying_095", payload={"capture_id": capture_id}))
        assert not endpoint.preparation_blocked
        assert managed.chain is chain and managed._capture_id == capture_id
        assert spoken == [RECEPTION_SPEECH["recovery_clarification"]]


@pytest.mark.anyio
@pytest.mark.parametrize("invalidation", ["new_input", "controller"])
async def test_queued_clarification_notice_rechecks_verified_delivery_before_persisting(tmp_path, monkeypatch, invalidation):
    from app.services.evidence_coordination import EvidenceOwnershipCoordinator
    from test_automatic_turn_integration import _automatic_session, _current, _feed, _VOICE, _PREFIX
    from test_spoken_supplement_integration import _synthetic_tts, _finish_playback

    async with _automatic_session(tmp_path, monkeypatch, [_PREFIX], spoken_confirmation=True) as (store, runtime, channel, managed):
        _synthetic_tts(runtime)
        endpoint, capture_id, chain = managed._answer_endpoint, managed._capture_id, managed.chain
        await _feed(managed._ingress, _VOICE, 3)
        endpoint._proposal_revision = endpoint.revision
        final = await endpoint.listening_snapshot()
        for _ in range(2):
            endpoint._record_preparation_failure(problem=SimpleNamespace(
                source="schema_or_content", reason_code="invalid_result", retryable=True))
        await endpoint._understanding_failed(record_failure=False)
        await endpoint.confirmation._recover_interaction(endpoint, final)
        notify, entered, release = endpoint.notify, asyncio.Event(), asyncio.Event()

        async def delayed_notice(reason):
            if reason == "understanding_clarification":
                entered.set()
                await release.wait()
            await notify(reason)

        endpoint.notify = delayed_notice
        completion = asyncio.create_task(_finish_playback(channel, runtime, managed))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if invalidation == "new_input":
                endpoint.speech_started(source="audio")
            else:
                grant = managed.ownership
                successor = EvidenceOwnershipCoordinator(store, lease_seconds=30).attach_control(
                    interview_id=managed.interview_id, organization_id=managed.organization_id,
                    connection_id="successor_095", local_instance_id=grant.owner_instance_id)
                assert successor.control_generation > grant.control_generation
            release.set()
            await completion
            await _until(lambda: managed._reply_audio_handoff is None)
            current = _current(runtime)
            assert current["agent_runtime"]["answer_preparation"]["status"] == "retry_required"
            assert not current["answers"] and endpoint.preparation_blocked
            assert managed.chain is chain and managed._capture_id == capture_id and chain.is_open
        finally:
            release.set()
            await asyncio.gather(completion, return_exceptions=True)
