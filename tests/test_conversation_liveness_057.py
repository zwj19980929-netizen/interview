"""One gentle idle check cannot become consent or an endless reminder loop."""
import asyncio

import pytest

from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint, _final, _until
from test_nonclosing_answer_endpoint import _preview


class Capture(_Capture):
    supports_stable_preview = True

    def __init__(self, kind="interview_dialogue"):
        super().__init__()
        self.kind = kind
        self.calls = []
        self.current_final = _final("我可以先说明整体思路吗？")

    async def transcript_preview(self):
        return _preview(self.current_final) if self.current_final else None

    async def classify_reception(self, text, **kwargs):
        self.calls.append(text)
        return {"kind": self.kind, "confidence": .98, "reply_text": "可以，按你的思路说。"}


def setup(capture):
    endpoint, clock, notices, commits = _endpoint(capture=capture)
    spoken = []

    async def speak(kind, guard, **kwargs):
        guard()
        spoken.append(kind)
        return True

    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    return endpoint, clock, notices, commits, spoken


@pytest.mark.parametrize("kind", ["interview_dialogue", "question_clarification", "repeat", "presence", "continue"])
def test_ordinary_response_long_silence_checks_once_without_new_model_call_or_commit(kind):
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup(Capture(kind))
        try:
            endpoint.speech_started()
            endpoint._proposal_revision = endpoint.revision
            assert await endpoint.confirmation._receive_request(endpoint, endpoint.capture.current_final)
            endpoint.start()
            # Time spent speaking does not consume the candidate's pause.
            clock.value = 20
            endpoint.confirmation.floor_returned(endpoint)
            clock.value += 14.9
            await asyncio.sleep(.15)
            assert spoken == ["reception_" + kind]
            clock.value += .1
            await _until(lambda: len(spoken) == 2)
            assert spoken[-1] == "reception_idle_check"
            endpoint.confirmation.floor_returned(endpoint)
            clock.value += 600
            await asyncio.sleep(.25)
            assert len(spoken) == 2 and len(endpoint.capture.calls) == 1
            assert not commits and not endpoint.capture.prepares and endpoint.capture.is_open
            assert not endpoint.confirmation.confirmed
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_explicit_wait_is_respected_then_new_server_speech_rearms_idle_check():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup(Capture("wait"))
        try:
            endpoint.speech_started()
            endpoint._proposal_revision = endpoint.revision
            await endpoint.confirmation._receive_request(endpoint, endpoint.capture.current_final)
            endpoint.confirmation.floor_returned(endpoint)
            endpoint.start()
            clock.value = 600
            await asyncio.sleep(.2)
            assert spoken == ["reception_wait"] and not commits
            endpoint.capture.kind = "interview_dialogue"
            endpoint.capture.current_final = _final(endpoint.capture.current_final.text + "我能先说整体方案吗？")
            endpoint.observe_transcript(endpoint.capture.current_final.text)
            clock.value += 1.1
            await _until(lambda: len(spoken) == 2)
            endpoint.confirmation.floor_returned(endpoint)
            clock.value += 15
            await _until(lambda: len(spoken) == 3)
            assert spoken[-1] == "reception_idle_check" and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_no_initial_speech_gets_one_check_without_snapshot_or_fabricated_reply():
    async def scenario():
        capture = Capture()
        capture.current_final = None
        endpoint, clock, notices, commits, spoken = setup(capture)
        try:
            endpoint.start()
            clock.value = 14.9
            await asyncio.sleep(.15)
            assert not spoken
            clock.value = 15
            await _until(lambda: spoken)
            assert spoken == ["reception_idle_check"]
            endpoint.confirmation.floor_returned(endpoint)
            clock.value += 600
            await asyncio.sleep(.25)
            assert len(spoken) == 1 and not capture.calls and not capture.prepares
            assert not capture.snapshots and not commits and capture.is_open
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_new_voice_cancels_idle_output_before_it_can_play():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup(Capture())
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def held_speak(kind, guard, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        endpoint.confirmation.speak = held_speak
        try:
            endpoint.start()
            clock.value = 15
            await asyncio.wait_for(entered.wait(), 1)
            endpoint.speech_started()
            await asyncio.wait_for(cancelled.wait(), 1)
            assert not commits and not endpoint.confirmation.confirmed
            assert not endpoint._closed and not endpoint._task.done()
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_pending_silence_check_does_not_mask_new_final_words():
    async def scenario():
        capture = Capture("interview_dialogue")
        endpoint, clock, notices, commits, spoken = setup(capture)
        try:
            endpoint.speech_started()
            endpoint._proposal_revision = endpoint.revision
            await endpoint.confirmation._receive_request(endpoint, capture.current_final)
            endpoint.confirmation.floor_returned(endpoint)
            # A provider can revise the final before its subtitle callback.
            capture.current_final = _final(capture.current_final.text + "我再补充一下。")
            endpoint.start()
            clock.value = 15
            await _until(lambda: len(capture.calls) == 2)
            assert capture.calls[-1] == "我再补充一下。"
            assert "reception_idle_check" not in spoken and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_nonpreview_wait_does_not_repeatedly_restart_stt_before_one_idle_check():
    async def scenario():
        capture = Capture()
        capture.supports_stable_preview = False
        endpoint, clock, notices, commits, spoken = setup(capture)
        try:
            endpoint.speech_started()
            endpoint._proposal_revision = endpoint.revision
            await endpoint.confirmation._receive_request(endpoint, capture.current_final)
            endpoint.confirmation.floor_returned(endpoint)
            endpoint.start()
            clock.value = 5
            await _until(lambda: endpoint._blocked_revision == endpoint.revision)
            count = len(capture.snapshots)
            await asyncio.sleep(.2)
            assert len(capture.snapshots) == count
            clock.value = 15
            await _until(lambda: spoken[-1] == "reception_idle_check")
            endpoint.confirmation.floor_returned(endpoint)
            clock.value += 600
            await asyncio.sleep(.15)
            assert len(spoken) == 2 and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_candidate_floor_return_resets_the_initial_silence_clock():
    async def scenario():
        capture = Capture()
        capture.current_final = None
        endpoint, clock, notices, commits, spoken = setup(capture)
        try:
            endpoint.start()
            clock.value = 14
            endpoint.candidate_floor_returned()
            clock.value = 20
            await asyncio.sleep(.15)
            assert not spoken
            clock.value = 29
            await _until(lambda: spoken)
            assert spoken == ["reception_idle_check"] and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())
