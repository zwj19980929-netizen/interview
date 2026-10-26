"""Synthetic regressions for final-cut budget ownership and bounded recovery."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from app.core.interaction_budget import remaining_interaction_seconds
from app.services.answer_endpoint import InteractionBudgetExpired
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint, _final, _until
from test_stable_preview_preparation import _preview


class Capture(_Capture):
    supports_stable_preview = True

    def __init__(self, *, intent="answer", action="continue_listening"):
        super().__init__()
        self.intent, self.action = intent, action
        self.budgets = []

    async def transcript_preview(self):
        return _preview(self.current_final)

    async def prepare_decision(self, final, **kwargs):
        self.prepares.append(final)
        self.budgets.append(remaining_interaction_seconds())
        return SimpleNamespace(understanding=SimpleNamespace(problem=None, intent=self.intent,
            suggested_action=self.action, turn_intent="answering", completion_basis=None))


@pytest.mark.anyio
@pytest.mark.parametrize("previous_reply", [False, True])
async def test_preview_has_own_budget_and_authoritative_final_gets_full_attempt(previous_reply):
    capture = Capture()
    endpoint, clock, _, _ = _endpoint(capture=capture)
    clock.value += 3.1
    endpoint._proposal_revision = endpoint.revision
    if previous_reply:
        endpoint.cover_transcript(_final("前一个概念已经解释完。"))
        endpoint._interaction_deadline = time.monotonic() - 1
    previous_deadline = endpoint._interaction_deadline
    try:
        endpoint.prepare_stable_preview(await capture.transcript_preview())
        await _until(lambda: capture.prepares)
        assert endpoint._interaction_deadline == previous_deadline
        assert 18 < capture.budgets[0] <= 20
        # An old/successful preview is optional. Rotating provider metadata
        # prevents reuse, so the authoritative request must still get 20s.
        final = capture.current_final.model_copy(update={"confidence": .85})
        endpoint.cover_transcript(final)
        await endpoint._prepare_transcript(final, semantic_first=True)
        assert 19 < capture.budgets[-1] <= 20
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("recovery_fails", [False, True])
async def test_expired_attempt_counts_once_and_recovery_has_one_independent_budget(recovery_fails):
    capture = Capture()
    endpoint, clock, notices, commits = _endpoint(capture=capture)
    calls = []
    async def speak(kind, guard):
        guard()
        calls.append((kind, remaining_interaction_seconds()))
        if recovery_fails:
            raise asyncio.TimeoutError()
        return True
    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint.speech_started()
    endpoint.cover_transcript(capture.current_final)
    # Exhaust the full 40s recovery window, not just its initial 20s stage.
    endpoint._interaction_started_at = time.monotonic() - 41
    endpoint._interaction_deadline = time.monotonic() - 1
    clock.value = 6
    endpoint.start()
    try:
        await _until(lambda: endpoint._prepare_failures == 1 and len(calls) == 1)
        await asyncio.sleep(.02)
        assert endpoint.preparation_blocked and endpoint._prepare_failures == 1
        assert not capture.prepares and not commits
        assert calls[0][0] == "reception_unavailable" and 0 < calls[0][1] <= 12
        assert "understanding_retry_exhausted" in notices
        snapshots = len(capture.snapshots)
        for _ in range(3):
            endpoint.speech_started(source="audio")
            clock.value += 6
            await asyncio.sleep(.11)
        assert len(capture.snapshots) == snapshots and len(calls) == 1
        assert endpoint._prepare_failures == 1 and capture.is_open
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("resume", ["new_final", "explicit_retry"])
async def test_expired_attempt_can_retry_only_with_new_words_or_explicit_action(resume):
    capture = Capture()
    endpoint, _, _, _ = _endpoint(capture=capture)
    endpoint.cover_transcript(capture.current_final)
    # Exhaust the full 40s recovery window, not just its initial 20s stage.
    endpoint._interaction_started_at = time.monotonic() - 41
    endpoint._interaction_deadline = time.monotonic() - 1
    endpoint._proposal_revision = endpoint.revision
    try:
        with pytest.raises(InteractionBudgetExpired):
            await endpoint._prepare_transcript(capture.current_final, semantic_first=True)
        assert endpoint.preparation_blocked and endpoint._prepare_failures == 1
        if resume == "new_final":
            capture.current_final = _final("这是新的一段合成回复。")
            endpoint.observe_transcript(capture.current_final.text)
        else:
            endpoint.continue_speaking()
        endpoint._proposal_revision = endpoint.revision
        endpoint.cover_transcript(capture.current_final)
        await endpoint._prepare_transcript(capture.current_final, semantic_first=True)
        assert len(capture.prepares) == 1 and capture.budgets[-1] > 19
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_blocked_recovery_playback_still_expires_without_retrying_understanding():
    capture = Capture()
    endpoint, clock, _, commits = _endpoint(capture=capture)
    spoken = []
    async def speak(kind, guard):
        guard()
        spoken.append(kind)
        if kind == "playback_timeout":
            # The real media callback revokes this exact performance and
            # returns Floor before its confirmation playback gate reopens.
            endpoint.confirmation.floor_returned(endpoint, reason="confirmation_playback_timeout")
        return True
    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint.speech_started()
    endpoint.cover_transcript(capture.current_final)
    # Exhaust the full 40s recovery window, not just its initial 20s stage.
    endpoint._interaction_started_at = time.monotonic() - 41
    endpoint._interaction_deadline = time.monotonic() - 1
    clock.value = 6
    endpoint.start()
    try:
        await _until(lambda: endpoint.confirmation.speaking and endpoint.preparation_blocked)
        assert spoken == ["reception_unavailable"]
        snapshots = len(capture.snapshots)
        clock.value += 60
        await _until(lambda: endpoint.confirmation.phase == "listening")
        assert spoken == ["reception_unavailable", "playback_timeout"]
        assert endpoint.preparation_blocked and endpoint._prepare_failures == 1
        endpoint.speech_started(source="audio")
        clock.value += 60
        await asyncio.sleep(.21)
        assert not capture.prepares and not commits and capture.is_open
        assert len(capture.snapshots) == snapshots and len(spoken) == 2
        assert endpoint.confirmation.phase == "listening"
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_clear_off_topic_without_ending_redirects_once_and_keeps_listening():
    capture = Capture(intent="off_topic", action="accept")
    endpoint, clock, _, commits = _endpoint(capture=capture)
    spoken = []
    async def speak(kind, guard):
        guard()
        spoken.append(kind)
        return True
    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint.speech_started()
    endpoint._proposal_revision = endpoint.revision
    try:
        await endpoint.confirmation._listen(endpoint)
        assert spoken == ["reception_off_topic"] and endpoint.confirmation.speaking
        endpoint.confirmation.floor_returned(endpoint)
        for n in range(3):
            capture.current_final = _final("合成跑题陈述。" * (n + 1))
            endpoint.observe_transcript(capture.current_final.text)
            clock.value += 6
            await endpoint.confirmation.step(endpoint, 6)
        assert spoken == ["reception_off_topic"] and not commits and capture.is_open
        assert endpoint.confirmation.phase == "listening"
    finally:
        await endpoint.close()
