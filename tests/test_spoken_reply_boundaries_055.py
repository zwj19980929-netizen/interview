"""Server-timed reply boundaries survive old ASR edits without inventing consent."""
import asyncio

import pytest

from app.model_gateway.schemas import TranscriptSegment
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _endpoint, _final, _until
from test_conversation_reception import ReceptionCapture
from test_spoken_supplement import ask, setup
from test_stable_preview_preparation import _preview


def timed(*segments):
    final = _final("".join(item[0] for item in segments))
    return final.model_copy(update={"segments": [
        TranscriptSegment(text=text, start_ms=start, end_ms=end, confidence=.96)
        for text, start, end in segments
    ]})


@pytest.mark.parametrize("reply", ["这题已经说完了。", "没有补充了。", "我们进入下一题吧。"])
def test_corrected_old_transcript_keeps_new_timed_finish_reply(reply):
    async def scenario():
        endpoint, clock, _, commits, spoken = setup()
        endpoint.capture.current_final = timed(("原来的回答。" * 150, 0, 1000))
        await ask(endpoint, clock, spoken)
        current = timed(("修正后的回答。" * 150, 0, 600), ("旧结论。", 600, 1000),
                        (reply, 1200, 1500))
        endpoint.capture.current_final = current
        endpoint.speech_started()
        clock.value += 1
        try:
            await _until(lambda: commits or len(spoken) > 1)
            assert endpoint.capture.replies == [reply]
            assert len(commits) == 1 and commits[0].text == current.text
            assert commits[0].final.segments == current.segments
            assert spoken == ["check"]
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("segments", [
    [("改写旧回答。", 0, 900), ("没有补充。", 950, 1200)],  # overlaps old boundary
    [("没有补充。", 1200, 1500)],  # earlier answer omitted
    [("改写旧回答。", 0, 1000), ("没有补充。", 0, 200)],  # stream-relative timestamps
    [("改写旧回答。", 0, 0), ("没有补充。", 0, 0)],  # missing timing
])
def test_incomplete_or_crossing_timeline_cannot_grant_completion(segments):
    async def scenario():
        endpoint, clock, _, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        endpoint.capture.current_final = timed(*segments)
        endpoint.speech_started()
        clock.value += 1
        try:
            await _until(lambda: len(spoken) > 1)
            assert not endpoint.capture.replies and not commits
            assert spoken == ["check", "clarify"]
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_old_corrected_finish_words_before_boundary_are_not_a_new_reply():
    async def scenario():
        endpoint, clock, _, commits, spoken = setup()
        endpoint.capture.current_final = timed(("旧回答。", 0, 1000))
        await ask(endpoint, clock, spoken)
        endpoint.capture.current_final = timed(("旧回答：没有了。", 0, 1000))
        endpoint.speech_started()
        clock.value += 1
        try:
            await _until(lambda: endpoint.capture.snapshots == [True, False])
            assert not endpoint.capture.replies and not commits
            assert spoken == ["check"]
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("reply", [
    "字幕还是没有修正。" + "我说明识别问题。" * 100 + "我刚才说没有了。",
    "我是在引用用户说的话：" + "用户说没有了。" * 100 + "我还没讲完。",
    "没有补充了。" + "再补充一个细节。" * 100 + "等等，我还要补充。",
], ids=["unresolved-dispute", "quoted-finish", "retracted-finish"])
def test_current_reply_is_not_truncated_around_quotes_disputes_or_retractions(reply):
    async def scenario():
        endpoint, clock, _, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        endpoint.capture.intent = "unclear"
        endpoint.capture.current_final = timed(("修正后的旧回答。", 0, 1000), (reply, 1200, 2000))
        endpoint.speech_started()
        clock.value += 1
        try:
            await _until(lambda: len(spoken) > 1)
            assert endpoint.capture.replies == [reply]
            assert not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_new_speech_revokes_slow_classification_after_timed_prefix_repair():
    async def scenario():
        endpoint, clock, _, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        entered = asyncio.Event()
        async def classify(reply):
            entered.set()
            await asyncio.Event().wait()
        endpoint.capture.classify_impl = classify
        endpoint.capture.current_final = timed(("修正后的旧回答。", 0, 1000), ("没有了。", 1200, 1500))
        endpoint.speech_started()
        clock.value += 1
        try:
            await asyncio.wait_for(entered.wait(), 1)
            endpoint.speech_started()
            await _until(lambda: endpoint._inference_task is None)
            assert not endpoint.confirmation.confirmed and not commits
            assert endpoint.capture.is_open
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_reception_uses_new_timed_speech_not_revised_conversation_history():
    async def scenario():
        capture = ReceptionCapture("other")
        endpoint, _, _, commits = _endpoint(capture=capture)
        async def speak(kind, guard):
            guard()
            return True
        confirmation = endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
        endpoint._proposal_revision = endpoint.revision
        prior = timed(("稍等，我还要考虑。", 0, 1000))
        try:
            assert not await confirmation._receive_request(endpoint, prior)
            capture.kind = "wait"
            preview = _preview(timed(("稍等，我还要考虑。", 0, 1000), ("我还要整理。", 1200, 1500)))
            assert await confirmation._receive_request(endpoint, preview, prepare_only=True)
            capture.kind = "other"
            final = timed(("稍等，我再考虑。", 0, 1000), ("这题已经说完了。", 1200, 1500))
            assert not await confirmation._receive_request(endpoint, final)
            assert capture.requests[-1][0] == "这题已经说完了。"
            assert capture.requests[-1][1]["preceding_text"] == prior.text
            assert not commits
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("missing_words", [False, True])
def test_sentence_whitespace_is_supported_but_missing_segment_words_are_not(missing_words):
    async def scenario():
        endpoint, clock, _, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        current = timed(("修正后的旧回答。", 0, 1000), ("没有补充了。", 1200, 1500),
                        ("下一题吧。", 1600, 1900))
        reply = "没有补充了。\n下一题吧。"
        current = current.model_copy(update={"text": "修正后的旧回答。 " +
            ("等等，我还没说完。 " if missing_words else "") + reply})
        endpoint.capture.current_final = current
        endpoint.speech_started()
        clock.value += 1
        try:
            await _until(lambda: commits or len(spoken) > 1)
            if missing_words:
                assert not commits and not endpoint.capture.replies
            else:
                assert endpoint.capture.replies == [reply]
                assert len(commits) == 1 and commits[0].text == current.text
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"type": "transcript.partial", "is_final": False},
    {"type": "transcript.final", "is_final": False},
])
def test_incomplete_snapshot_is_never_a_finish_reply(changes):
    async def scenario():
        endpoint, _, _, commits, spoken = setup()
        endpoint._proposal_revision = endpoint.revision
        final = timed(("没有了。", 0, 1000)).model_copy(update=changes)
        try:
            with pytest.raises(ValueError, match="complete server final"):
                await endpoint.confirmation._reply(endpoint, final)
            assert not endpoint.confirmation.confirmed and not commits
            assert not endpoint.capture.replies and not spoken
        finally:
            await endpoint.close()
    asyncio.run(scenario())


def test_repaired_reply_with_low_segment_confidence_still_needs_clarification():
    async def scenario():
        endpoint, clock, _, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        current = timed(("修正后的旧回答。", 0, 1000), ("没有了。", 1200, 1500))
        # model_copy mirrors adapters producing detached objects, and must not
        # let a high aggregate field conceal a low-confidence reply segment.
        current.segments[-1].confidence = .2
        endpoint.capture.current_final = current
        endpoint.speech_started()
        clock.value += 1
        try:
            await _until(lambda: len(spoken) > 1)
            assert not endpoint.capture.replies and not commits
            assert spoken == ["check", "clarify"]
        finally:
            await endpoint.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("aggregate_confidence", [.9, .2])
def test_old_low_segment_does_not_replace_fresh_reply_quality_or_aggregate_gate(aggregate_confidence):
    async def scenario():
        endpoint, clock, _, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        current = timed(("修正后的旧回答。", 0, 1000), ("没有补充了。", 1200, 1500))
        # Preserve the fields separately to exercise a provider returning a
        # high final aggregate with an old individually uncertain segment.
        current.segments[0].confidence = .2
        current.confidence = aggregate_confidence
        endpoint.capture.current_final = current
        endpoint.speech_started()
        clock.value += 1
        try:
            await _until(lambda: commits or len(spoken) > 1)
            if aggregate_confidence < .65:
                assert not commits and not endpoint.capture.replies
                assert spoken == ["check", "clarify"]
            else:
                assert endpoint.capture.replies == ["没有补充了。"]
                assert len(commits) == 1 and commits[0].text == current.text
                assert commits[0].final.segments == current.segments
        finally:
            await endpoint.close()
    asyncio.run(scenario())
