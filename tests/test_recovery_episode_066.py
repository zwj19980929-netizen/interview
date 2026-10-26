"""Continuous failures do not buy a new retry/apology loop on each utterance."""
import asyncio
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.domain.interview_agent import UnderstandingProblem
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint, _final, _until


def problem(*, retryable):
    return UnderstandingProblem(
        code="UNDERSTANDING_PROVIDER_UNAVAILABLE" if retryable else "UNDERSTANDING_RESULT_REJECTED",
        source="provider" if retryable else "schema_or_content",
        recoverable=True, action="clarify", retryable=retryable,
    )


class RecoveryCapture(_Capture):
    def __init__(self, *, retryable=True):
        super().__init__()
        self.current_final = _final("合成回答。我的介绍完毕。")
        self.problem = problem(retryable=retryable)
        self.recovery_origin = 0

    async def prepare_decision(self, final, **kwargs):
        self.prepares.append(final.model_copy(deep=True))
        return SimpleNamespace(text=final.text, final=final.model_copy(deep=True),
            understanding=SimpleNamespace(problem=self.problem, suggested_action="continue_listening",
                                          completion_basis=None, turn_intent="thinking"))


def setup(capture=None):
    endpoint, clock, notices, commits = _endpoint(capture=capture or RecoveryCapture())
    spoken = []

    async def speak(kind, guard):
        guard()
        spoken.append(kind)
        return True

    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint.speech_started(source="audio")
    endpoint.start()
    return endpoint, clock, notices, commits, spoken


@pytest.mark.anyio
async def test_manual_retry_ack_waits_until_failure_speech_returns_the_floor():
    endpoint, clock, notices, commits = _endpoint(capture=RecoveryCapture())
    endpoint.confirmation = SpokenSupplementConfirmation(speak=None)
    endpoint.confirmation.phase = "speaking"
    endpoint.confirmation._after_speech = "listening"
    endpoint._record_preparation_failure(problem=problem(retryable=False))
    endpoint.continue_speaking()
    await endpoint._publish_preparation_failure()
    assert "understanding_retrying" not in notices
    assert endpoint._manual_retry_notice_pending and endpoint.confirmation.speaking
    endpoint.confirmation.floor_returned(endpoint)
    await endpoint._publish_preparation_failure()
    assert notices.count("understanding_retrying") == 1
    assert not endpoint.confirmation.confirmed and not commits
    await endpoint.close()


async def advance_to_failure(endpoint, clock, count):
    # The one-time recovery allowance is spent before speech I/O. Capture
    # whether this attempt owes playback before advancing the worker, rather
    # than mistaking its later spent flag for an already selected output.
    expects_recovery_speech = count == 1 and endpoint.confirmation._failure_spoken_identity is None
    clock.value += 10
    await _until(lambda: endpoint._prepare_failures == count)
    await _until(lambda: endpoint._inference_task is None)
    # The first failure speaks once. Acknowledge delivery before asking the
    # real endpoint loop to attempt the next request.
    if expects_recovery_speech:
        await _until(lambda: endpoint.confirmation.speaking)
    if endpoint.confirmation.speaking:
        endpoint.confirmation.floor_returned(endpoint)
    await asyncio.sleep(.02)


async def let_endpoint_poll(clock):
    for _ in range(3):
        clock.value += 15
        await asyncio.sleep(.07)


@pytest.mark.anyio
async def test_retryable_episode_stops_at_two_and_fresh_finished_words_get_only_one_request():
    endpoint, clock, notices, commits, spoken = setup()
    try:
        for count in range(1, 3):
            await advance_to_failure(endpoint, clock, count)
        assert len(endpoint.capture.prepares) == 2
        assert "understanding_retry_exhausted" in notices
        assert spoken == ["reception_unavailable"]

        endpoint.capture.current_final = endpoint.capture.current_final.model_copy(update={
            "sequence": 25, "stream_id": "new-provider-stream", "confidence": .8,
        })
        endpoint.speech_started(source="audio")
        await let_endpoint_poll(clock)
        assert len(endpoint.capture.prepares) == 2

        text = endpoint.capture.current_final.text + "我说完了。"
        endpoint.capture.current_final = _final(text)
        endpoint.observe_transcript(text)
        await advance_to_failure(endpoint, clock, 3)
        await let_endpoint_poll(clock)
        assert len(endpoint.capture.prepares) == 3
        assert endpoint.preparation_blocked and endpoint._prepare_failures == 3
        assert spoken == ["reception_unavailable"]
        assert not commits and not endpoint.confirmation.confirmed and endpoint.capture.is_open
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_nonretryable_schema_problem_blocks_same_words_but_accepts_one_new_request():
    endpoint, clock, notices, commits, spoken = setup(RecoveryCapture(retryable=False))
    try:
        await advance_to_failure(endpoint, clock, 1)
        assert "understanding_result_rejected" in notices
        assert endpoint.preparation_blocked
        for _ in range(3):
            endpoint.speech_started(source="audio")
            clock.value += 15
            await asyncio.sleep(.07)
        assert len(endpoint.capture.prepares) == 1

        text = endpoint.capture.current_final.text + "请重新解释一下这道题。"
        endpoint.capture.current_final = _final(text)
        endpoint.observe_transcript(text)
        await advance_to_failure(endpoint, clock, 2)
        await let_endpoint_poll(clock)
        assert len(endpoint.capture.prepares) == 2
        assert spoken == ["reception_unavailable"] and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_explicit_continue_renews_budget_without_completion_or_another_apology():
    endpoint, clock, notices, commits, spoken = setup()
    try:
        for count in range(1, 3):
            await advance_to_failure(endpoint, clock, count)
        endpoint.continue_speaking()
        assert not endpoint.confirmation.confirmed and endpoint._explicit_revision is None
        assert endpoint.confirmation.exit_context(endpoint.capture.current_final) is None
        assert endpoint._prepare_failures == 0 and not endpoint.preparation_blocked
        for count in range(1, 3):
            await advance_to_failure(endpoint, clock, count)
        await let_endpoint_poll(clock)
        assert len(endpoint.capture.prepares) == 4
        assert not commits and spoken == ["reception_unavailable"]
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_successful_understanding_clears_failure_episode_and_allows_next_episode_notice():
    endpoint, clock, notices, commits, spoken = setup(RecoveryCapture(retryable=False))
    try:
        await advance_to_failure(endpoint, clock, 1)
        endpoint.capture.problem = None
        text = endpoint.capture.current_final.text + "我再想一会儿。"
        endpoint.capture.current_final = _final(text)
        endpoint.observe_transcript(text)
        clock.value += 10
        await _until(lambda: "understanding_recovered" in notices)
        await _until(lambda: spoken[-1] == "reception_wait")
        endpoint.confirmation.floor_returned(endpoint)
        assert endpoint._prepare_failures == 0 and not endpoint.preparation_blocked
        assert not endpoint._preparation_terminal and not endpoint._understanding_recovery_active
        assert endpoint.confirmation._failure_spoken_identity is None
        assert not endpoint.confirmation.confirmed and not commits

        endpoint.capture.problem = problem(retryable=False)
        text += "我继续补充新的内容。"
        endpoint.capture.current_final = _final(text)
        endpoint.observe_transcript(text)
        await advance_to_failure(endpoint, clock, 1)
        assert spoken == ["reception_unavailable", "reception_wait", "reception_unavailable"]
        await let_endpoint_poll(clock)
        assert len(endpoint.capture.prepares) == 3 and not commits
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("resume_source", ["origin", "continue", "finish"])
async def test_missing_final_same_origin_budget_survives_vad_and_partial_revisions(resume_source):
    capture = RecoveryCapture()
    capture.current_final = None
    endpoint, clock, notices, commits, spoken = setup(capture)
    try:
        for count in range(1, 4):
            clock.value += 10
            await _until(lambda: endpoint._missing_final_attempts == count)
        await _until(lambda: "transcript_retry_required" in notices)
        attempts = len(capture.snapshots)
        for index in range(5):
            endpoint.speech_started(source="audio")
            endpoint.observe_transcript("重新识别的临时片段%d" % index)
            clock.value += 20
            await asyncio.sleep(.06)
        assert len(capture.snapshots) == attempts == 3
        assert endpoint.awaiting_transcript and endpoint._missing_final_attempts == 3
        assert notices.count("transcript_retry_required") == 1
        assert not spoken and not commits

        if resume_source == "origin":
            capture.recovery_origin = 640
            endpoint.speech_started(source="audio")
        elif resume_source == "continue":
            endpoint.continue_speaking()
            assert endpoint._explicit_revision is None and not endpoint.confirmation.confirmed
        else:
            endpoint.request_finish()
        clock.value += 10
        await _until(lambda: len(capture.snapshots) > attempts)
        assert not commits and capture.is_open
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_missing_final_is_counted_even_when_input_invalidates_the_snapshot_waiter():
    capture = RecoveryCapture()
    endpoint, clock, notices, commits = _endpoint(capture=capture)

    async def stale_snapshot(*, resume=True):
        endpoint.speech_started(source="transcript")
        return None

    capture.transcript_snapshot = stale_snapshot
    try:
        for _ in range(3):
            endpoint._proposal_revision = endpoint.revision
            with pytest.raises(ApiError) as error:
                await endpoint.listening_snapshot()
            assert error.value.code == "TURN_DECISION_STALE"
        assert endpoint._missing_final_attempts == 3 and endpoint.awaiting_transcript
        assert capture.resumes == 3 and not commits
    finally:
        await endpoint.close()
