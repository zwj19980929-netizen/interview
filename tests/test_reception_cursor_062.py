"""An interrupted response does not turn an old final into new candidate speech."""
from types import SimpleNamespace

import pytest

from app.services.livekit_evidence_ingress import ManagedLiveKitEvidenceSession
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _endpoint, _final
from test_conversation_reception import ReceptionCapture


def setup():
    capture = ReceptionCapture("question_clarification")
    capture.thinking = True
    capture.current_final = _final("这里说的幂等是什么意思？")
    endpoint, clock, notices, commits = _endpoint(capture=capture)
    spoken = []

    async def speak(kind, guard, **kwargs):
        guard()
        endpoint.confirmation.playback_selected(endpoint)
        spoken.append(kind)
        return True

    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint.speech_started()
    endpoint._proposal_revision = endpoint.revision
    managed = SimpleNamespace(_answer_endpoint=endpoint, _supplement_preroll=[], chain=capture, _stopped=False)
    return endpoint, clock, spoken, commits, managed


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["agent_finished", "barge_in", "candidate_continues"])
async def test_handled_final_is_not_reanalyzed_after_floor_return_without_new_words(reason):
    endpoint, clock, spoken, commits, managed = setup()
    try:
        await endpoint.confirmation._listen(endpoint)
        assert spoken == ["reception_question_clarification"]
        if reason != "agent_finished":
            endpoint.speech_started()  # A scoped signal is not a new transcript.
        await ManagedLiveKitEvidenceSession.confirmation_floor_returned(managed, reason)
        clock.value += 6
        await endpoint.confirmation.step(endpoint, 6)
        assert spoken == ["reception_question_clarification"]
        assert not endpoint.capture.prepares and not commits
        assert len(endpoint.capture.requests) == 1
        assert endpoint.capture.is_open
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_new_authoritative_repeat_after_interruption_still_gets_a_response():
    endpoint, clock, spoken, commits, managed = setup()
    try:
        await endpoint.confirmation._listen(endpoint)
        endpoint.speech_started()
        await ManagedLiveKitEvidenceSession.confirmation_floor_returned(managed, "barge_in")
        from app.model_gateway.schemas import TranscriptSegment
        first = endpoint.capture.current_final
        fresh = "这里说的幂等是什么意思？"
        endpoint.capture.current_final = first.model_copy(update={"text": first.text + fresh,
            "segments": [*first.segments, TranscriptSegment(text=fresh, start_ms=2000, end_ms=3000, confidence=.9)]})
        endpoint.observe_transcript(endpoint.capture.current_final.text)
        clock.value += 6
        await endpoint.confirmation.step(endpoint, 6)
        assert spoken == ["reception_question_clarification"] * 2
        assert endpoint.capture.requests[-1][0] == fresh
        assert not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_acoustic_activity_without_new_words_does_not_repeat_an_answer_clarification():
    endpoint, clock, spoken, commits, managed = setup()
    try:
        final = endpoint.capture.current_final
        await endpoint.confirmation.clarify_answer(endpoint, SimpleNamespace(clarification_target=None), final)
        await ManagedLiveKitEvidenceSession.confirmation_floor_returned(managed, "agent_finished")
        for _ in range(6):
            endpoint.speech_started(source="audio")
            clock.value += 6
            await endpoint.confirmation.step(endpoint, 6)
        assert spoken == ["answer_clarify"]
        assert not endpoint.capture.requests and not endpoint.capture.prepares and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_new_capture_does_not_inherit_the_previous_captures_handled_cursor():
    endpoint, _, spoken, _, _ = setup()
    try:
        await endpoint.confirmation._listen(endpoint)
    finally:
        await endpoint.close()
    replacement, _, new_spoken, _, _ = setup()
    try:
        await replacement.confirmation._listen(replacement)
        assert spoken == new_spoken == ["reception_question_clarification"]
    finally:
        await replacement.close()


@pytest.mark.anyio
async def test_provider_recovery_preserves_handled_prefix_and_bounded_diagnostics():
    from app.model_gateway.errors import ProviderError
    endpoint, clock, spoken, commits, managed = setup()
    trace = []
    endpoint.trace = lambda event, **details: trace.append((event, details))
    async def recover():
        old = endpoint.capture.current_final
        endpoint.capture.current_final = old.model_copy(update={"provider": old.provider.model_copy(
            update={"request_id": "recovered_request"})})
    endpoint.capture.recover_capture = recover
    try:
        await endpoint.confirmation._listen(endpoint)
        await ManagedLiveKitEvidenceSession.confirmation_floor_returned(managed, "agent_finished")
        await endpoint._recover_capture(ProviderError("provider_timeout", "Synthetic timeout", retryable=True), "send")
        endpoint.speech_started(source="audio")
        clock.value += 6
        for _ in range(4):
            await endpoint.confirmation.step(endpoint, 6)
        assert spoken == ["reception_question_clarification"] and not commits
        assert not endpoint.capture.prepares
        suppressed = [details for event, details in trace if event == "handled_final_suppressed"]
        assert suppressed == [{"revision": endpoint.revision, "phase": "listening", "source": "stable_preview"}]
    finally:
        await endpoint.close()


def test_handled_cursor_diagnostics_have_no_transcript_content(monkeypatch):
    import json
    from app.core import speech_diagnostics
    emitted = []
    monkeypatch.setattr(speech_diagnostics._LOG, "info", emitted.append)
    speech_diagnostics.record_turn_control("handled_final_suppressed", interview_id="synthetic", turn_id="turn",
        revision=4, source="stable_preview", phase="listening", private_transcript="MUST_NOT_LOG")
    assert json.loads(emitted[0])["event"] == "turn_control.handled_final_suppressed"
    assert "MUST_NOT_LOG" not in emitted[0]
