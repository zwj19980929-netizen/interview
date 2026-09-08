"""Keep the asked question's context across input races and repeated replies."""
import asyncio
import hashlib
import io
import json
import logging

from test_answer_endpoint import _final, _until
from test_spoken_supplement import setup, ask


def test_control_diagnostics_work_without_root_info_logging_and_never_include_text(monkeypatch):
    from app.core import speech_diagnostics
    logger = logging.Logger("synthetic.speech_control")
    output = io.StringIO()
    logger.addHandler(logging.StreamHandler(output))
    monkeypatch.setattr(speech_diagnostics, "_LOG", logger)
    speech_diagnostics.configure_speech_diagnostics()
    text = "合成的明确回复，不得记录为日志正文"
    speech_diagnostics.record_turn_control("supplement_decided", interview_id="synthetic",
        turn_id="turn", capture_id="capture", intent="finish", confidence=.98, text=text,
        vendor_response="private", credentials="secret")
    data = json.loads(output.getvalue())
    assert data["intent"] == "finish" and data["capture_id"] == "capture"
    assert data["text_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert text not in output.getvalue() and "private" not in output.getvalue() and "secret" not in output.getvalue()


def test_repeated_clear_no_during_slow_preparation_does_not_ask_the_same_question_again():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        release = asyncio.Event()
        async def slow(final):
            await release.wait()
            return endpoint.capture.prepared(final)
        endpoint.capture.prepare_impl = slow
        text = "合成回答结束。"
        replies = ["我这边没有需要补充的了。", "没有的，进入下一题吧。", "我已经说完了，不用再问了。"]
        for index, reply in enumerate(replies):
            text += reply
            endpoint.capture.current_final = _final(text)
            endpoint.speech_started()
            clock.value += 1
            await _until(lambda: len(endpoint.capture.prepares) == index + 1)
            assert spoken == ["check"]
        release.set()
        await _until(lambda: len(commits) == 1)
        assert endpoint.capture.replies == replies
        assert commits[0].text == text
        assert spoken == ["check"]
        await endpoint.close()
    asyncio.run(scenario())


def test_final_covered_partial_cannot_revoke_a_confirmed_answer():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        release = asyncio.Event()
        async def slow(final):
            await release.wait()
            return endpoint.capture.prepared(final)
        endpoint.capture.prepare_impl = slow
        text = "合成回答结束。没有了。"
        endpoint.capture.current_final = _final(text)
        endpoint.observe_transcript(text[:-2])
        clock.value += 1
        await _until(lambda: len(endpoint.capture.prepares) == 1)
        revision = endpoint.revision
        endpoint.observe_transcript(text)
        endpoint.observe_transcript(text[:-1])
        assert endpoint.revision == revision and endpoint.confirmation.confirmed
        release.set()
        await _until(lambda: len(commits) == 1)
        await endpoint.close()
    asyncio.run(scenario())


def test_new_substantive_reply_after_no_still_revokes_completion():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        async def slow(final):
            await asyncio.Event().wait()
        endpoint.capture.prepare_impl = slow
        text = "合成回答结束。没有了。"
        endpoint.capture.current_final = _final(text)
        endpoint.speech_started()
        clock.value += 1
        await _until(lambda: len(endpoint.capture.prepares) == 1)
        addition = "等等，其实还有一点，失败任务应当按幂等键重试。"
        endpoint.capture.current_final = _final(text + addition)
        endpoint.capture.intent = "supplement"
        endpoint.observe_transcript(text + addition)
        clock.value += 1
        await _until(lambda: spoken == ["check", "continue"])
        assert endpoint.capture.replies == ["没有了。", addition]
        assert not commits and not endpoint.confirmation.confirmed
        endpoint.confirmation.floor_returned(endpoint)
        assert endpoint.confirmation.phase == "listening"
        await endpoint.close()
    asyncio.run(scenario())


def test_no_new_words_can_reuse_confirmation_only_after_fresh_complete_snapshot():
    async def scenario():
        endpoint, clock, notices, commits, spoken = setup()
        await ask(endpoint, clock, spoken)
        release = asyncio.Event()
        async def slow(final):
            await release.wait()
            return endpoint.capture.prepared(final)
        endpoint.capture.prepare_impl = slow
        final = _final("合成回答结束。没有了。")
        endpoint.capture.current_final = final
        endpoint.speech_started()
        clock.value += 1
        await _until(lambda: len(endpoint.capture.prepares) == 1)
        endpoint.speech_started()
        endpoint.capture.current_final = None
        clock.value += 1
        await _until(lambda: "transcript_unavailable" in notices)
        assert not commits and len(endpoint.capture.prepares) == 1
        endpoint.capture.current_final = final
        clock.value += 3
        await _until(lambda: endpoint.confirmation.confirmed)
        release.set()
        await _until(lambda: len(commits) == 1)
        assert len(endpoint.capture.prepares) == 1
        assert endpoint.capture.replies == ["没有了。"]
        assert spoken == ["check"]
        await endpoint.close()
    asyncio.run(scenario())
