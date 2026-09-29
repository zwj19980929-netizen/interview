"""A fast social acknowledgement is not the final authority over a turn."""
import asyncio
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint, _final, _until
from test_prepared_turn_decision import Gateway, _input, _service
from test_semantic_turn_policy import semantic_response
from test_stable_preview_preparation import _preview


FINISH = "我用幂等键防止重复执行。稍等，我整理一下。现在想好了，这部分说完了。"
THINKING = "我用幂等键防止重复执行。刚才想结束。不对，我还需要想想。"


class Capture(_Capture):
    supports_stable_preview = True

    def __init__(self, fast="wait", full="finish_topic", text=FINISH):
        super().__init__()
        self.fast, self.full = fast, full
        self.current_final = _final(text)
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.cancelled = 0
        self.receptions = []
        self.failure = False
        self.final_cuts = []

    async def transcript_preview(self):
        return _preview(self.current_final)

    async def transcript_snapshot(self, *, resume=True):
        final = await super().transcript_snapshot(resume=resume)
        self.final_cuts.append(final)
        return final

    async def classify_reception(self, text, **context):
        self.receptions.append(text)
        return {"kind": self.fast, "confidence": .99, "evidence_quote": text}

    async def prepare_decision(self, final, **kwargs):
        self.prepares.append(final)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.failure:
            if isinstance(self.failure, BaseException):
                raise self.failure
            raise ValueError("Synthetic full understanding unavailable")
        utterance, turn, interview = _input()
        interview["_semantic_first"] = True
        utterance = utterance.model_copy(update={"text": final.text})
        data = semantic_response(content="technical", intent=self.full)
        if data["understanding"]["completion_basis"]:
            data["understanding"]["completion_basis"]["evidence_ids"] = ["E3"]
        understanding, followup = await _service(Gateway(data)).prepare_decision(utterance, turn, interview)
        return SimpleNamespace(text=final.text, final=final.model_copy(deep=True),
                               understanding=understanding, followup=followup)


def setup(capture):
    endpoint, clock, notices, commits = _endpoint(capture=capture)
    spoken = []

    async def speak(kind, guard):
        guard()
        endpoint.confirmation.playback_selected(endpoint)
        spoken.append(kind)
        if kind == "pause":
            capture.is_open = False
        return True

    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint.speech_started(source="audio")
    endpoint.start()
    clock.value = 1.1
    return endpoint, clock, notices, commits, spoken


@pytest.mark.anyio
async def test_full_finish_corrects_fast_wait_using_the_already_running_preparation():
    capture = Capture()
    endpoint, clock, _, commits, spoken = setup(capture)
    try:
        await _until(lambda: spoken == ["reception_wait"])
        assert capture.prepares and not capture.cancelled, "Fast reception must not cancel full understanding"
        capture.release.set()
        await asyncio.sleep(.12)
        assert not commits, "Do not advance during the acknowledged wait's playback"
        endpoint.confirmation.floor_returned(endpoint)
        clock.value += 1.1
        await _until(lambda: len(commits) == 1)
        assert commits[0].text == FINISH and capture.final_cuts[-1].type == "transcript.final"
        assert capture.final_cuts[-1].text == FINISH
        assert capture.snapshots == [False, False], "Initial final plus a fresh commit cut remain mandatory"
        assert len(capture.prepares) == 1 and spoken == ["reception_wait"]
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_late_final_correction_cannot_be_committed_using_the_fast_waits_old_full_result():
    capture = Capture()
    endpoint, clock, _, commits, spoken = setup(capture)
    try:
        await _until(lambda: spoken)
        capture.release.set()
        await _until(lambda: endpoint.preparation._task.done())
        # A final-only correction may arrive without a local VAD/ASR event.
        # The fresh commit cut still prevents the prepared old finish.
        capture.current_final = _final(THINKING)
        capture.full = "thinking"
        endpoint.confirmation.floor_returned(endpoint)
        clock.value += 1.1
        await _until(lambda: len(capture.prepares) >= 2)
        await asyncio.sleep(.15)
        assert not commits and capture.final_cuts[-1].text == THINKING
        assert capture.is_open and not endpoint.confirmation.confirmed
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_long_conversation_runs_one_reconciliation_per_new_phrase_without_replaying_wait():
    capture = Capture(full="thinking", text=THINKING + "我先整理思路。" * 120)
    capture.release.set()
    endpoint, clock, _, commits, spoken = setup(capture)
    try:
        for index in range(5):
            await _until(lambda: len(spoken) == index + 1)
            endpoint.confirmation.floor_returned(endpoint)
            clock.value += 1.1
            await _until(lambda: endpoint.confirmation._reception_review is None)
            await asyncio.sleep(.12)
            calls = len(capture.prepares)
            clock.value += 600
            await asyncio.sleep(.12)
            assert calls == index + 1 == len(capture.prepares)
            assert len(spoken) == index + 1 and not commits
            assert not endpoint.confirmation._idle_check_allowed
            if index < 4:
                capture.current_final = _final(capture.current_final.text + "我还需要再想一个步骤。")
                endpoint.observe_transcript(capture.current_final.text)
                clock.value += 1.1
        assert spoken == ["reception_wait"] * 5
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("fast", ["wait", "continue", "other"])
async def test_full_thinking_never_inherits_completion_or_repeated_nudges(fast):
    capture = Capture(fast=fast, full="thinking", text=THINKING)
    endpoint, clock, _, commits, spoken = setup(capture)
    try:
        if fast != "other":
            await _until(lambda: spoken)
            assert not capture.cancelled
            endpoint.confirmation.floor_returned(endpoint)
        capture.release.set()
        clock.value = 6
        await _until(lambda: endpoint.confirmation._semantic_wait_identity is not None)
        await asyncio.sleep(.15)
        if endpoint.confirmation.speaking:
            endpoint.confirmation.floor_returned(endpoint)
        count = len(spoken)
        clock.value += 600
        await asyncio.sleep(.2)
        assert not commits and not endpoint.confirmation.confirmed
        assert not endpoint.confirmation._idle_check_allowed
        assert len(spoken) == count and "check" not in spoken and "reception_idle_check" not in spoken
        assert len(capture.prepares) == 1
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("interruption", ["audio", "transcript", "close", "capture"])
async def test_new_input_or_capture_revocation_discards_the_earlier_full_finish(interruption):
    capture = Capture()
    endpoint, clock, _, commits, spoken = setup(capture)
    try:
        await _until(lambda: spoken)
        endpoint.confirmation.floor_returned(endpoint)
        if interruption == "close":
            await endpoint.close()
        elif interruption == "capture":
            capture.is_open = False
        elif interruption == "transcript":
            capture.current_final = _final(THINKING)
            capture.full, capture.fast = "thinking", "other"
            endpoint.observe_transcript(THINKING)
        else:
            endpoint.speech_started(source="audio")
        capture.release.set()
        clock.value += 6
        await asyncio.sleep(.25)
        assert not commits and not endpoint.confirmation.confirmed
        assert len(spoken) <= 2
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_full_understanding_unavailable_does_not_turn_fast_wait_into_submission_or_retry_loop():
    capture = Capture()
    endpoint, clock, _, commits, spoken = setup(capture)
    capture.failure = True
    try:
        await _until(lambda: spoken)
        endpoint.confirmation.floor_returned(endpoint)
        capture.release.set()
        clock.value += 1.1
        await asyncio.sleep(.25)
        if endpoint.confirmation.speaking:
            endpoint.confirmation.floor_returned(endpoint)
        calls, outputs = len(capture.prepares), len(spoken)
        clock.value += 600
        await asyncio.sleep(.25)
        assert not commits and not endpoint.confirmation.confirmed and capture.is_open
        assert len(capture.prepares) == calls <= 2 and len(spoken) == outputs
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_uncertain_full_result_replaces_fast_wait_lock_with_one_completion_check():
    capture = Capture(full="answering")
    endpoint, clock, _, commits, spoken = setup(capture)
    try:
        await _until(lambda: spoken)
        endpoint.confirmation.floor_returned(endpoint)
        capture.release.set()
        clock.value += 1.1
        await _until(lambda: spoken == ["reception_wait", "check"])
        endpoint.confirmation.floor_returned(endpoint)
        assert endpoint.confirmation.phase == "awaiting_reply"
        assert endpoint.confirmation._semantic_wait_identity is None
        clock.value += 600
        await _until(lambda: len(spoken) == 3)
        endpoint.confirmation.floor_returned(endpoint)
        clock.value += 600
        await asyncio.sleep(.15)
        assert spoken == ["reception_wait", "check", "clarify"]
        assert len(capture.prepares) == 1 and not commits and not endpoint.confirmation.confirmed
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_full_review_deadline_cancels_work_and_never_creates_a_polling_inference_loop():
    capture = Capture()
    endpoint, clock, notices, commits, spoken = setup(capture)
    endpoint.preparation._timeout = .05
    infer = endpoint._infer

    async def bounded_infer(work, *, timeout, **kwargs):
        return await infer(work, timeout=min(timeout, .05), **kwargs)

    endpoint._infer = bounded_infer
    try:
        await _until(lambda: spoken)
        endpoint.confirmation.floor_returned(endpoint)
        clock.value += 1.1
        await _until(lambda: "understanding_unavailable" in notices)
        await asyncio.sleep(.15)
        count = len(capture.prepares)
        clock.value += 600
        await asyncio.sleep(.2)
        assert count == len(capture.prepares) == capture.cancelled == 2
        assert endpoint.confirmation._reception_review is None
        assert not endpoint.preparation._tasks and endpoint._inference_task is None
        assert spoken == ["reception_wait"] and not commits and capture.is_open
    finally:
        await endpoint.close()


@pytest.mark.anyio
async def test_durable_owner_rejection_during_reconciliation_cannot_use_the_old_preparation():
    capture = Capture()
    capture.failure = ApiError("EVIDENCE_OWNER_FENCED", "Synthetic owner revoked.", status_code=409)
    endpoint, clock, _, commits, spoken = setup(capture)
    try:
        await _until(lambda: spoken)
        endpoint.confirmation.floor_returned(endpoint)
        capture.release.set()
        clock.value += 1.1
        await _until(lambda: endpoint._closed)
        assert not commits and not endpoint.confirmation.confirmed
        assert spoken == ["reception_wait"]
    finally:
        await endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["pause", "repeat", "question_clarification"])
async def test_dedicated_reception_actions_are_not_overridden_by_speculative_completion(kind):
    capture = Capture(fast=kind)
    endpoint, clock, _, commits, spoken = setup(capture)
    try:
        await _until(lambda: spoken)
        endpoint.confirmation.floor_returned(endpoint)
        capture.release.set()
        clock.value += 6
        await asyncio.sleep(.2)
        assert not commits
        assert spoken[0] == "reception_" + kind
    finally:
        await endpoint.close()
