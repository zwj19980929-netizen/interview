"""Real endpoint orchestration keeps listening while one analysis is prepared."""

import asyncio

import pytest

from app.core.errors import ApiError
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _endpoint, _final, _until
from test_semantic_turn_endpoint import SemanticCapture
from test_stable_preview_preparation import _preview


class ParallelCapture(SemanticCapture):
    supports_stable_preview = True
    recovery_required = False

    def __init__(self):
        super().__init__()
        self.preview_value = _preview(self.current_final)
        self.paused = False
        self.timeline = []
        self.reception_entered = asyncio.Event()
        self.reception_gate = None
        self.preparation_cancelled = asyncio.Event()
        self.preparation_done = asyncio.Event()

    async def transcript_preview(self):
        return self.preview_value.model_copy(deep=True)

    async def transcript_snapshot(self, *, resume=True):
        self.timeline.append("snapshot")
        self.paused = not resume
        return await super().transcript_snapshot(resume=resume)

    async def resume_capture(self):
        self.timeline.append("resume")
        self.paused = False
        await super().resume_capture()

    async def classify_reception(self, text, **kwargs):
        self.reception_entered.set()
        if self.reception_gate is not None:
            await self.reception_gate.wait()
        return {"kind": "other", "confidence": .95, "reply_text": ""}

    async def prepare_decision(self, final, **kwargs):
        self.timeline.append("prepare")
        try:
            result = await super().prepare_decision(final, **kwargs)
            self.preparation_done.set()
            return result
        except asyncio.CancelledError:
            self.preparation_cancelled.set()
            raise


def setup(capture=None):
    capture = capture or ParallelCapture()
    endpoint = None

    async def commit(prepared, guard):
        guard()
        assert capture.paused, "A new authoritative cut is mandatory after resumed recognition"
        assert endpoint._identity(prepared.final) == endpoint._identity(capture.current_final)
        capture.timeline.append("commit")

    endpoint, clock, notices, commits = _endpoint(capture=capture, commit_impl=commit)
    spoken = []

    async def speak(kind, guard, **kwargs):
        guard()
        spoken.append(kind)
        return True

    endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    endpoint.speech_started()
    return endpoint, clock, notices, commits, spoken


@pytest.mark.parametrize("new_input", ["audio", "partial"])
def test_slow_understanding_keeps_capture_resumed_and_new_input_revokes_result(new_input):
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        capture = endpoint.capture
        capture.release = asyncio.Event()
        processing = asyncio.create_task(endpoint.confirmation.step(endpoint, quiet=5))
        try:
            await asyncio.wait_for(capture.entered.wait(), 1)
            await _until(lambda: capture.resumes > 0)
            assert not capture.paused
            assert not processing.done() and not commits
            if new_input == "audio":
                endpoint.observe_audio(b"\x00\x40" * 320)
            else:
                endpoint.observe_transcript(capture.current_final.text + "不对，我还要补充。")
            await asyncio.wait_for(capture.preparation_cancelled.wait(), 1)
            capture.release.set()
            with pytest.raises(ApiError) as raised:
                await processing
            assert raised.value.code == "TURN_DECISION_STALE"
            assert not commits and not spoken
            assert capture.is_open and not capture.paused
        finally:
            await endpoint.close()
            await asyncio.gather(processing, return_exceptions=True)
    asyncio.run(scenario())


def test_final_correction_is_covered_and_projected_before_recognition_resumes():
    async def scenario():
        endpoint, _, _, commits, _ = setup()
        capture = endpoint.capture
        endpoint.observe_transcript("这题我部会。")
        endpoint._proposal_revision = endpoint.revision
        initial_revision = endpoint.revision

        async def project(final):
            assert endpoint._covered_server_transcript == final.text
            assert capture.paused
            capture.timeline.append("subtitle")
            # An independently delivered corrected subtitle is already covered;
            # it must not look like a new candidate continuation.
            endpoint.observe_transcript(final.text)
            assert endpoint.revision == initial_revision

        endpoint.on_snapshot = project
        try:
            final = await endpoint.listening_snapshot()
            endpoint.assert_current()
            assert final.text == capture.current_final.text
            assert capture.timeline == ["snapshot", "subtitle", "resume"]
            assert not capture.paused and not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_stable_preview_analysis_starts_with_reception_and_is_reused_at_final():
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        capture = endpoint.capture
        capture.release, capture.reception_gate = asyncio.Event(), asyncio.Event()
        early = asyncio.create_task(endpoint.confirmation.step(endpoint, quiet=1.1))
        try:
            await asyncio.wait_for(capture.entered.wait(), 1)
            await asyncio.wait_for(capture.reception_entered.wait(), 1)
            assert not early.done()
            assert not capture.snapshots and not commits and not spoken
            assert not capture.paused
            capture.reception_gate.set()
            await early
            capture.release.set()
            await asyncio.wait_for(capture.preparation_done.wait(), 1)
            assert not commits, "A ready preview cannot authorize completion"
            await endpoint.confirmation.step(endpoint, quiet=5)
            assert len(capture.prepares) == 1
            assert len(commits) == 1 and not spoken
            assert capture.snapshots == [False, False], "Cut once for understanding and again before commit"
            assert capture.timeline.index("prepare") < capture.timeline.index("snapshot")
            assert capture.timeline.index("resume") < capture.timeline.index("commit")
            assert not endpoint.confirmation.confirmed, "No synthetic spoken confirmation is invented"
        finally:
            await endpoint.close()
            await asyncio.gather(early, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["text", "confidence", "language", "segment", "provider"])
def test_corrected_final_text_or_provenance_cannot_reuse_preview_analysis(change):
    async def scenario():
        endpoint, _, _, commits, _ = setup()
        capture = endpoint.capture
        try:
            await endpoint.confirmation.step(endpoint, quiet=1.1)
            await asyncio.wait_for(capture.preparation_done.wait(), 1)
            revised = capture.current_final.model_copy(deep=True)
            if change == "text":
                revised = _final("这道题我暂时不会。下一题吧。")
            elif change == "confidence":
                revised.confidence = .88
            elif change == "language":
                revised.language = "zh"
            elif change == "segment":
                revised.segments[0].end_ms += 1
            elif change == "provider":
                revised.provider.request_id = "different-recognition-request"
            capture.current_final = revised
            await endpoint.confirmation.step(endpoint, quiet=5)
            assert len(capture.prepares) == 2
            assert endpoint._identity(capture.prepares[0]) != endpoint._identity(capture.prepares[1])
            assert len(commits) == 1
            assert endpoint._identity(commits[0].final) == endpoint._identity(revised)
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("interrupt", ["audio", "partial", "close"])
def test_new_input_or_close_cancels_speculative_work_before_five_second_confirmation(interrupt):
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        capture = endpoint.capture
        capture.release = asyncio.Event()
        try:
            await endpoint.confirmation.step(endpoint, quiet=1.1)
            await asyncio.wait_for(capture.entered.wait(), 1)
            assert not capture.snapshots
            if interrupt == "close":
                await endpoint.close()
            elif interrupt == "partial":
                endpoint.observe_transcript(capture.current_final.text + "不过我可以尝试一下。")
            else:
                endpoint.observe_audio(b"\x00\x40" * 320)
            await asyncio.wait_for(capture.preparation_cancelled.wait(), 1)
            capture.release.set()
            await asyncio.sleep(0)
            assert not endpoint.preparation.matching(capture.current_final)
            assert not commits and not spoken
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_final_revision_at_commit_is_reprepared_while_listening_then_cut_again():
    async def scenario():
        endpoint, _, _, commits, _ = setup()
        capture = endpoint.capture
        initial = capture.current_final.model_copy(deep=True)
        revised = _final("这个方向我暂时不会。下一题吧。")
        capture.finals.extend([initial, revised, revised])
        try:
            await endpoint.confirmation.step(endpoint, quiet=1.1)
            await asyncio.wait_for(capture.preparation_done.wait(), 1)
            await endpoint.confirmation.step(endpoint, quiet=5)
            assert capture.snapshots == [False, False, False]
            assert len(capture.prepares) == 2
            assert len(commits) == 1 and commits[0].text == revised.text
            revision_prepare = capture.timeline.index("prepare", capture.timeline.index("prepare") + 1)
            assert capture.timeline[revision_prepare - 1] == "resume"
            assert capture.timeline[revision_prepare + 1:] == ["snapshot", "commit"]
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_speech_during_final_subtitle_projection_revokes_before_any_response():
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        capture = endpoint.capture
        entered, release = asyncio.Event(), asyncio.Event()

        async def project(final):
            entered.set()
            await release.wait()

        endpoint.on_snapshot = project
        processing = asyncio.create_task(endpoint.confirmation.step(endpoint, quiet=5))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            endpoint.observe_transcript(capture.current_final.text + "等一下，刚才没说完整。")
            release.set()
            with pytest.raises(ApiError) as raised:
                await processing
            assert raised.value.code == "TURN_DECISION_STALE"
            assert not commits and not spoken
            assert capture.is_open and not capture.paused
        finally:
            await endpoint.close()
            await asyncio.gather(processing, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("corrected_intent", ["thinking", "answering"])
def test_corrected_commit_final_cannot_inherit_previews_permission_to_finish(corrected_intent):
    async def scenario():
        from types import SimpleNamespace
        from test_prepared_turn_decision import Gateway, _input, _service
        from test_semantic_turn_policy import semantic_response

        revised = _final("不对，我还没说完。让我再想一下。" if corrected_intent == "thinking"
                         else "我先确认任务状态。然后结合幂等键继续分析。")

        class CorrectedCapture(ParallelCapture):
            async def prepare_decision(self, final, *, semantic_first=False, completion_confirmed=False):
                if final.text != revised.text:
                    return await super().prepare_decision(final, semantic_first=semantic_first,
                                                          completion_confirmed=completion_confirmed)
                self.timeline.append("prepare")
                self.prepares.append(final.model_copy(deep=True))
                self.flags.append((semantic_first, completion_confirmed))
                assert semantic_first and not completion_confirmed
                assert not self.paused, "Revised understanding must run with recognition resumed"
                utterance, turn, interview = _input()
                utterance = utterance.model_copy(update={"text": final.text})
                interview["_semantic_first"] = True
                data = semantic_response(content="none" if corrected_intent == "thinking" else "technical",
                                         intent=corrected_intent, technical=corrected_intent == "answering")
                understanding, followup = await _service(Gateway(data)).prepare_decision(utterance, turn, interview)
                assert understanding.problem is None and understanding.completion_basis is None
                return SimpleNamespace(text=final.text, final=final.model_copy(deep=True),
                                       understanding=understanding, followup=followup)

        capture = CorrectedCapture()
        endpoint, _, _, commits, spoken = setup(capture)
        capture.finals.extend([capture.current_final.model_copy(deep=True), revised])
        try:
            await endpoint.confirmation.step(endpoint, quiet=1.1)
            await asyncio.wait_for(capture.preparation_done.wait(), 1)
            await endpoint.confirmation.step(endpoint, quiet=5)
            assert len(capture.prepares) == 2
            assert capture.snapshots == [False, False]
            assert capture.flags == [(True, False), (True, False)]
            assert not commits and not spoken
            assert not endpoint.confirmation.confirmed
            assert capture.is_open and not capture.paused
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_new_speech_while_resuming_revised_final_does_not_start_obsolete_reasoning():
    async def scenario():
        endpoint, _, _, commits, _ = setup()
        capture = endpoint.capture
        revised = _final("这个方向我暂时不会。下一题吧。")
        capture.finals.extend([capture.current_final.model_copy(deep=True), revised])
        original_resume = capture.resume_capture

        async def resume_and_receive_speech():
            await original_resume()
            if capture.resumes == 2:
                # ASR replay may emit a new partial before resume returns.
                endpoint.observe_transcript(revised.text + "不对，我想起来了。")

        capture.resume_capture = resume_and_receive_speech
        try:
            await endpoint.confirmation.step(endpoint, quiet=1.1)
            await asyncio.wait_for(capture.preparation_done.wait(), 1)
            await endpoint.confirmation.step(endpoint, quiet=5)
            assert len(capture.prepares) == 1, "Already revoked reasoning must not be sent to the model"
            assert not commits and capture.is_open and not capture.paused
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("finish_button", [False, True])
def test_final_only_continuation_revokes_prior_completion_confirmation(finish_button):
    async def scenario():
        from types import SimpleNamespace
        from test_prepared_turn_decision import Gateway, _input, _service
        from test_semantic_turn_policy import semantic_response

        continued = _final("这题我不会。下一题吧。另外一个情况我还在考虑。")

        class FinalOnlyCapture(ParallelCapture):
            async def prepare_decision(self, final, *, semantic_first=False, completion_confirmed=False):
                if final.text != continued.text:
                    return await super().prepare_decision(final, semantic_first=semantic_first,
                                                          completion_confirmed=completion_confirmed)
                self.flags.append((semantic_first, completion_confirmed))
                self.prepares.append(final.model_copy(deep=True))
                assert semantic_first and not completion_confirmed
                assert not self.paused
                utterance, turn, interview = _input()
                utterance = utterance.model_copy(update={"text": final.text})
                interview["_semantic_first"] = True
                understanding, followup = await _service(Gateway(
                    semantic_response(content="none", intent="thinking", technical=False)
                )).prepare_decision(utterance, turn, interview)
                assert understanding.problem is None and understanding.completion_basis is None
                return SimpleNamespace(text=final.text, final=final.model_copy(deep=True),
                                       understanding=understanding, followup=followup)

        capture = FinalOnlyCapture()
        endpoint, _, _, commits, spoken = setup(capture)
        original = capture.current_final.model_copy(deep=True)
        capture.finals.append(continued)
        endpoint.confirmation.confirmed = True
        endpoint.confirmation._confirmed_final = original
        endpoint.confirmation.phase = "confirmed"
        endpoint.confirmation._semantic_wait_identity = endpoint._identity(original)
        if finish_button:
            endpoint.request_finish()
            assert endpoint._explicit_revision == endpoint.revision
        endpoint._proposal_revision = endpoint.revision
        try:
            await endpoint._propose(final_snapshot=original, recognition_resumed=True)
            assert capture.flags == [(True, True), (True, False)]
            assert len(capture.prepares) == 2
            assert not endpoint.confirmation.confirmed
            assert endpoint.confirmation._confirmed_final is None
            assert endpoint.confirmation.phase == "listening"
            assert endpoint.confirmation._semantic_wait_identity is None
            assert endpoint._explicit_revision is None
            assert not commits and not spoken
            assert capture.is_open and not capture.paused
            snapshots = list(capture.snapshots)
            # No VAD or partial arrived: even at the same input revision, the
            # next scheduler tick must not reuse the previous finish button.
            await endpoint.confirmation.step(endpoint, quiet=.1)
            assert len(capture.prepares) == 2 and capture.snapshots == snapshots
            assert not endpoint.confirmation.confirmed
            assert endpoint.confirmation.phase == "listening"
            assert not commits and not spoken
            assert capture.is_open and not capture.paused
        finally:
            await endpoint.close()
    asyncio.run(scenario())
