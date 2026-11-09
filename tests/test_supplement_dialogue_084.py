"""Small supplement dialogue stays outside full analysis and real answer writes.

Only microphone cuts, the model transport and speech selection are synthetic.
The endpoint, prompt/validation, evidence owner and SQLite submission are real.
"""

import asyncio
from copy import deepcopy
import json

import pytest

from app.core.errors import ApiError
from app.core.prompt.contracts import SUPPLEMENT_SPEECH, SUPPLEMENT_SPEECH_VERSION
from app.core.prompt.conversation_reception import RECEPTION_SPEECH, RECEPTION_SPEECH_VERSION
from app.core.time import utc_now
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.repositories.sqlite import SQLiteStore
from app.services.conversation_delivery import record_selection, update_delivery
from app.services.continuous_stt import ContinuousSTT
from app.services.interviews import InterviewService
from app.services.spoken_supplement import SpokenSupplementConfirmation
from test_answer_endpoint import _Capture, _endpoint
from test_answer_reuse_078 import _append
from test_continuous_stt import _RawStream, _validated
from test_off_topic_semantics_068 import off_topic_response
from test_preparation_fences_081 import _chain
from test_prepared_decision_binding import _payload


class _Gateway:
    def __init__(self):
        self.requests = []
        self.intent, self.confidence = "question_clarification", .96
        self.reply_text = "你是想确认题目里这个词的含义吗？"
        self.small_error = None
        self.small_started, self.small_release = asyncio.Event(), None
        self.full_started, self.full_release = asyncio.Event(), None

    @property
    def kinds(self):
        return ["small" if r.metadata["prompt_version"].startswith("supplement_reply.")
                else "reception" if r.metadata["prompt_version"].startswith("conversation_reception.")
                else "full" for r in self.requests]

    async def invoke(self, capability, request):
        self.requests.append(request)
        version = request.metadata["prompt_version"]
        if version.startswith("supplement_reply."):
            self.small_started.set()
            if self.small_release is not None:
                await self.small_release.wait()
            if self.small_error is not None:
                raise self.small_error
            data = {"intent": self.intent, "confidence": self.confidence,
                    "evidence_id": request.json_schema["properties"]["evidence_id"]["enum"][-1],
                    "reply_text": self.reply_text}
        elif version.startswith("conversation_reception."):
            data = {"kind": "other", "confidence": .96,
                    "evidence_id": request.json_schema["properties"]["evidence_id"]["enum"][-1],
                    "reply_text": ""}
        else:
            self.full_started.set()
            if self.full_release is not None:
                await self.full_release.wait()
            data = off_topic_response(request.metadata["transcript"], ["幂等键"],
                                      technical=True, relevant=True)["understanding"]
        return ChatJSONResponse(data=deepcopy(data), usage=Usage(), provider=ProviderMeta(
            provider_id="synthetic", model="supplement_084", request_id="synthetic_084", latency_ms=1))


class _CaptureFacade(_Capture):
    def __init__(self, harness):
        super().__init__()
        self.harness, self.current_final = harness, harness.final

    async def prepare_decision(self, final, **kwargs):
        self.prepares.append(final.model_copy(deep=True))
        return await self.harness.chain.prepare_decision(final, **kwargs)

    def assert_preparation_current(self, prepared, final, **kwargs):
        return self.harness.chain.assert_preparation_current(prepared, final, **kwargs)

    async def classify_supplement_reply(self, reply, **kwargs):
        return await self.harness.chain.classify_supplement_reply(reply, **kwargs)

    async def classify_reception(self, reply, **kwargs):
        return await self.harness.chain.classify_reception(reply, **kwargs)

    def confirm_prepared_decision(self, *args, **kwargs):
        return self.harness.chain.confirm_prepared_decision(*args, **kwargs)

    def stage_reception_exchange(self, *args, **kwargs):
        return self.harness.chain.stage_reception_exchange(*args, **kwargs)

    def discard_reception_exchange(self, token):
        self.harness.chain.discard_reception_exchange(token)


def _select(harness, kind, guard, kwargs):
    service = harness.stream.interviews
    act_id = "speech_084_%s" % len(harness.spoken)
    if kind in {"check", "clarify", "continue"}:
        act_type, version, text = "supplement_" + kind, SUPPLEMENT_SPEECH_VERSION, SUPPLEMENT_SPEECH[kind]
    elif kind in {"exit_check", "exit_clarify"}:
        from app.core.prompt.candidate_exit import EXIT_CONFIRMATION_SPEECH, EXIT_CONFIRMATION_SPEECH_VERSION
        act_type, version, text = "exit_confirmation", EXIT_CONFIRMATION_SPEECH_VERSION, EXIT_CONFIRMATION_SPEECH
    else:
        route = kind.removeprefix("reception_")
        act_type = "question_clarification" if route == "question_clarification" else "conversation_acknowledgement"
        version = kwargs.get("reply_prompt_version") or RECEPTION_SPEECH_VERSION
        text = kwargs.get("reply_text") or RECEPTION_SPEECH.get(route, "请解释如何防止重复请求。")
    with service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        turn = session["turns"][0]
        act = {"act_id": act_id, "turn_id": turn["id"], "act_type": act_type,
               "prompt_version": version, "text": text}
        turn["conversation_acts"].append(act)
        event = {"event_id": "event_" + act_id, "turn_id": turn["id"],
                 "session_sequence": len(session.get("agent_events", [])) + 1,
                 "occurred_at": utc_now(), "type": "conversation.act.selected",
                 "payload": {"act_id": act_id, "text": text, "approved": True}}
        session.setdefault("agent_events", []).append(event)
        record_selection(session, event)
        session.setdefault("agent_runtime", {}).update(
            active_performance_id="performance_" + act_id, active_expression_act_event_id=event["event_id"])
        tx.interview_sessions.update(session, expected_version=session["version"])
    if kind.startswith("reception_"):
        harness.chain.record_reception_exchange(performance_id="performance_" + act_id, guard=guard)
    harness.selected.append(act_id)
    return {"act_id": act_id}


async def _deliver(harness, reason="agent_finished"):
    with harness.stream.interviews.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        act_id = harness.selected[-1]
        update_delivery(session, event_id="event_" + act_id, performance_id="performance_" + act_id,
                        status="delivered" if reason == "agent_finished" else "interrupted", updated_at=utc_now())
        tx.interview_sessions.update(session, expected_version=session["version"])
    await harness.confirmation.complete_playback(harness.endpoint, reason=reason)


async def _setup(tmp_path, monkeypatch):
    harness = _chain(tmp_path, monkeypatch)
    service = harness.stream.interviews
    harness.gateway = service.conversation.gateway = _Gateway()
    harness.capture = _CaptureFacade(harness)
    harness.spoken, harness.selected, harness.committed = [], [], []

    async def commit(prepared, guard):
        guard()
        harness.chain.assert_preparation_current(prepared, harness.capture.current_final,
                                                semantic_first=True, completion_confirmed=True)
        service.assert_prepared_streaming_decision(prepared, harness.capture.current_final, "org_default")
        result = await service.submit_streaming_answer("iv_binding", _payload(harness.capture.current_final),
            evidence_fence=harness.owner.commit_fence(), prepared_decision=prepared)
        assert result["accepted"]
        harness.committed.append(result)

    harness.endpoint, harness.clock, harness.notices, _ = _endpoint(capture=harness.capture, commit_impl=commit)

    async def speak(kind, guard, **kwargs):
        guard()
        harness.spoken.append(kind)
        return _select(harness, kind, guard, kwargs)

    harness.confirmation = harness.endpoint.confirmation = SpokenSupplementConfirmation(speak=speak)
    harness.endpoint.speech_started()
    await harness.confirmation.step(harness.endpoint, quiet=5)
    assert harness.spoken == ["check"]
    assert harness.confirmation._prepared_answer is not None
    await _deliver(harness)
    assert harness.confirmation._supplement_question_delivered
    harness.gateway.requests.clear()
    harness.gateway.small_started.clear()
    harness.gateway.full_started.clear()
    return harness


async def _reply(harness, text):
    harness.capture.current_final = _append(harness.capture.current_final, text)
    if isinstance(harness.stream.stream, ContinuousSTT):
        harness.stream.stream._final = harness.capture.current_final.model_copy(deep=True)
    harness.endpoint.observe_transcript(harness.capture.current_final.text)
    harness.clock.value += 5
    await harness.confirmation.step(harness.endpoint, quiet=5)


@pytest.mark.anyio
@pytest.mark.parametrize("intent,reply,speech,generated", [
    ("question_clarification", "题目里的这个术语是什么意思？", "reception_question_clarification", True),
    ("interview_dialogue", "我们现在还在讨论上一段吗？", "reception_interview_dialogue", True),
    ("presence", "你还在听吗？", "reception_presence", True),
    ("audio_problem", "我听不清你的声音。", "reception_audio_problem", True),
    ("transcript_correction", "字幕不对，我刚才说的是另一种工具。", "reception_transcript_correction", True),
    ("wait", "让我想一想。", "reception_wait", True),
    ("repeat", "请把题目再说一遍。", "reception_repeat", False),
    ("off_topic", "我今天在路上看见一只猫。", "reception_off_topic", False),
    ("unclear", "那一个，刚才那个。", "clarify", True),
    ("continue", "我还没说完。", "continue", False),
    ("supplement", "我再补充一下唯一约束。", "continue", False),
])
async def test_new_noncompletion_reply_uses_one_small_decision_and_no_full_analysis(
        tmp_path, monkeypatch, intent, reply, speech, generated):
    harness = await _setup(tmp_path, monkeypatch)
    try:
        harness.gateway.intent = intent
        harness.gateway.reply_text = ("好的，我还在听。" if intent in {"presence", "wait", "interview_dialogue"}
                                      else "你是想确认题目里这个词的含义吗？") if generated else ""
        await _reply(harness, reply)
        assert harness.gateway.kinds == ["small"]
        assert harness.gateway.requests[0].metadata["prompt_version"] == "supplement_reply.v6"
        assert harness.spoken == ["check", speech]
        assert not harness.committed and not harness.confirmation.confirmed
        assert harness.capture.is_open and len(harness.capture.prepares) == 1
        assert harness.stream.interviews.get_interview("iv_binding")["answers"] == []
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
async def test_dialogue_then_new_finish_preserves_all_evidence_and_commits_once(tmp_path, monkeypatch):
    harness = await _setup(tmp_path, monkeypatch)
    try:
        await _reply(harness, "题目里的这个术语是什么意思？")
        assert harness.gateway.kinds == ["small"] and not harness.committed
        await _deliver(harness)
        assert harness.stream.interviews.get_interview("iv_binding")["turns"][0]["reception_exchanges"]
        harness.gateway.intent, harness.gateway.reply_text = "finish", ""
        await _reply(harness, "我已经说完了。")
        assert harness.gateway.kinds == ["small", "small", "full"]
        assert len(harness.committed) == 1 and not harness.capture.is_open
        reopened = InterviewService(SQLiteStore(str(tmp_path / "preparation-fences.sqlite3"))).get_interview("iv_binding")
        assert len(reopened["answers"]) == 1
        assert reopened["answers"][0]["final_transcript"] == harness.capture.current_final.text
        assert reopened["answers"][0]["transcript_segments"] == _payload(harness.capture.current_final)["segments"]
        assert len(reopened["turns"][0]["utterances"]) == 1
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("low_source", [False, True])
async def test_uncertain_model_or_source_never_requests_full_analysis(tmp_path, monkeypatch, low_source):
    harness = await _setup(tmp_path, monkeypatch)
    try:
        if low_source:
            harness.capture.current_final = harness.capture.current_final.model_copy(update={"confidence": .5})
        else:
            harness.gateway.confidence = .6
        await _reply(harness, "那个词是指什么？")
        assert harness.gateway.kinds == ([] if low_source else ["small"])
        assert harness.spoken == ["check", "clarify"]
        assert not harness.committed and not harness.confirmation.confirmed
        assert len(harness.capture.prepares) == 1
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
async def test_exit_route_only_asks_new_exit_question_and_never_finishes_answer(tmp_path, monkeypatch):
    harness = await _setup(tmp_path, monkeypatch)
    try:
        harness.gateway.intent, harness.gateway.reply_text = "stop_interview", ""
        await _reply(harness, "我要结束今天整场面试。")
        assert harness.gateway.kinds == ["small"]
        assert harness.spoken == ["check", "exit_check"]
        assert harness.confirmation._exit_waiting
        assert not harness.confirmation._exit_question_delivered
        assert not harness.confirmation.confirmed and not harness.committed
        await _deliver(harness)
        assert harness.confirmation._exit_question_delivered
        await harness.confirmation.step(harness.endpoint, quiet=5)
        assert harness.gateway.kinds == ["small"] and not harness.committed
        assert harness.stream.interviews.get_interview("iv_binding")["status"] == "in_progress"
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
async def test_disabled_answer_review_cannot_classify_or_accept_fresh_finish(tmp_path, monkeypatch):
    harness = await _setup(tmp_path, monkeypatch)
    try:
        harness.gateway.intent, harness.gateway.reply_text = "finish", ""
        harness.capture.current_final = _append(harness.capture.current_final, "我已经说完了。")
        harness.endpoint.observe_transcript(harness.capture.current_final.text)
        harness.endpoint._proposal_revision = harness.endpoint.revision
        await harness.confirmation._listen(harness.endpoint, allow_answer_review=False)
        assert "small" not in harness.gateway.kinds and "full" not in harness.gateway.kinds
        assert not harness.committed and not harness.confirmation.confirmed
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
async def test_short_acknowledgment_is_bound_to_latest_delivered_explanation(tmp_path, monkeypatch):
    harness = await _setup(tmp_path, monkeypatch)
    try:
        await _reply(harness, "你能解释一下题目的范围吗？")
        await _deliver(harness)
        harness.gateway.intent, harness.gateway.reply_text = "interview_dialogue", "好的，我们按刚才澄清的范围继续交流。"
        await _reply(harness, "对。")
        assert harness.gateway.kinds == ["small", "small"]
        context = json.loads(harness.gateway.requests[-1].messages[-1].content)
        assert context["reply"] == "对。" and list(context["evidence"].values()) == ["对。"]
        assert context["stage"] == "awaiting_reply"
        assert context["reply_target"] == "other_delivered_speech"
        assert context["last_delivered_utterance"] == "你是想确认题目里这个词的含义吗？"
        assert context["last_delivered_utterance"] != context["question"]
        assert context["current_question"] == "请解释如何防止重复请求。"
        assert not harness.confirmation.confirmed and not harness.committed
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["capture", "owner", "question", "new_words", "confidence", "provider", "generation"])
async def test_result_after_source_or_confirmation_context_changes_has_no_side_effect(
        tmp_path, monkeypatch, change):
    harness = await _setup(tmp_path, monkeypatch)
    async def unexpected_reopen():
        raise AssertionError("This test never rotates or opens external speech transport")
    harness.stream.stream = ContinuousSTT(_validated(_RawStream("stream_binding")),
        reopen=unexpected_reopen, record=lambda _: None, bytes_per_second=32000)
    harness.gateway.small_release = asyncio.Event()
    task = asyncio.create_task(_reply(harness, "题目里的这个术语是什么意思？"))
    try:
        await asyncio.wait_for(harness.gateway.small_started.wait(), 1)
        if change == "capture":
            harness.chain._stt = None
        elif change == "owner":
            assert harness.coordinator.release(harness.owner)
        elif change == "question":
            with harness.stream.interviews.persistence.transaction("org_default") as tx:
                session = tx.interview_sessions.get("iv_binding")
                session["turns"][0]["question_spoken_text"] = "请解释另外一种设计。"
                tx.interview_sessions.update(session, expected_version=session["version"])
        elif change == "new_words":
            harness.capture.current_final = _append(harness.capture.current_final, "等一下，先别回答。")
            harness.endpoint.observe_transcript(harness.capture.current_final.text)
        elif change == "confidence":
            harness.capture.current_final = harness.capture.current_final.model_copy(update={"confidence": .4})
            harness.stream.stream._final = harness.capture.current_final.model_copy(deep=True)
        elif change == "generation":
            harness.stream._capture_generation += 1
        else:
            final = harness.capture.current_final
            harness.capture.current_final = final.model_copy(update={
                "provider": final.provider.model_copy(update={"request_id": "replacement_source"})})
            harness.stream.stream._final = harness.capture.current_final.model_copy(deep=True)
        harness.gateway.small_release.set()
        result = await asyncio.gather(task, return_exceptions=True)
        if isinstance(result[0], BaseException):
            assert isinstance(result[0], ApiError)
            assert result[0].code in {"TURN_DECISION_STALE", "EVIDENCE_OWNER_FENCED", "EVIDENCE_CONTROL_STALE"}
        assert harness.spoken == ["check"]
        assert harness.gateway.kinds == ["small"]
        assert not harness.committed and not harness.confirmation.confirmed
        assert harness.stream.interviews.get_interview("iv_binding")["answers"] == []
    finally:
        harness.gateway.small_release.set()
        await asyncio.gather(task, return_exceptions=True)
        await harness.endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", [None, "new_words", "metadata"])
async def test_recovery_rejoins_same_final_but_classifies_new_words_or_metadata(tmp_path, monkeypatch, change):
    harness = await _setup(tmp_path, monkeypatch)
    try:
        final = harness.capture.current_final = _append(harness.capture.current_final, "我想换一种方式说明。")
        harness.endpoint.observe_transcript(final.text)
        harness.endpoint._proposal_revision = harness.endpoint.revision
        harness.endpoint._begin_interaction(final.text)
        harness.gateway.full_release = asyncio.Event()
        await harness.confirmation._recover_interaction(harness.endpoint, final)
        await asyncio.wait_for(harness.gateway.full_started.wait(), 1)
        assert harness.spoken == ["check", "reception_unavailable"]
        assert harness.gateway.kinds == ["full"] and not harness.committed
        await _deliver(harness)
        if change == "new_words":
            harness.capture.current_final = _append(final, "你还在线吗？")
        elif change == "metadata":
            harness.capture.current_final = final.model_copy(update={"confidence": .81})
            assert harness.endpoint._identity(harness.capture.current_final) != harness.endpoint._identity(final)
        harness.gateway.full_release.set()
        await harness.confirmation.step(harness.endpoint, quiet=0)
        assert harness.gateway.kinds == (["full"] if change is None else ["full", "small"])
        assert not harness.committed and not harness.confirmation.confirmed
        assert harness.stream.interviews.get_interview("iv_binding")["answers"] == []
    finally:
        if harness.gateway.full_release is not None:
            harness.gateway.full_release.set()
        await harness.endpoint.close()


@pytest.mark.anyio
async def test_source_updated_during_resume_is_rejected_before_small_model_call(tmp_path, monkeypatch):
    harness = await _setup(tmp_path, monkeypatch)
    async def unexpected_reopen():
        raise AssertionError("This test never opens external speech transport")
    harness.stream.stream = ContinuousSTT(_validated(_RawStream("stream_binding")),
        reopen=unexpected_reopen, record=lambda _: None, bytes_per_second=32000)
    resume = harness.capture.resume_capture

    async def changed_source_on_resume():
        await resume()
        # The completed cut was copied before recognition resumed. A later
        # provider correction can retain its text while revising its quality.
        harness.stream.stream._final = harness.capture.current_final.model_copy(update={"confidence": .4})

    monkeypatch.setattr(harness.capture, "resume_capture", changed_source_on_resume)
    try:
        result = await asyncio.gather(_reply(harness, "题目里的这个术语是什么意思？"), return_exceptions=True)
        if isinstance(result[0], BaseException):
            assert isinstance(result[0], ApiError) and result[0].code == "TURN_DECISION_STALE"
        assert harness.gateway.kinds == []
        assert harness.spoken == ["check"]
        assert not harness.committed and not harness.confirmation.confirmed
    finally:
        await harness.endpoint.close()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["invalid_contract", "provider", "timeout"])
async def test_small_failure_stays_bounded_and_cannot_fall_into_full_analysis(tmp_path, monkeypatch, failure):
    harness = await _setup(tmp_path, monkeypatch)
    try:
        if failure == "invalid_contract":
            harness.gateway.intent = "invented_intent"
        elif failure == "provider":
            harness.gateway.small_error = ProviderError("provider_unavailable", "Synthetic model unavailable", retryable=True)
        else:
            harness.gateway.small_error = asyncio.TimeoutError()
        await _reply(harness, "题目里的这个术语是什么意思？")
        assert harness.gateway.kinds == ["small"]
        for _ in range(3):
            harness.clock.value += 5
            await harness.confirmation.step(harness.endpoint, quiet=5)
        assert harness.gateway.kinds == ["small", "small"]
        assert harness.spoken == ["check"]
        assert harness.endpoint.preparation_blocked
        assert not harness.committed and not harness.confirmation.confirmed
        assert len(harness.capture.prepares) == 1
        assert all(request.execution_budget.timeout_s == 8 for request in harness.gateway.requests)
        assert all(request.execution_budget.max_provider_attempts == 1 for request in harness.gateway.requests)
        assert all(request.execution_budget.max_provider_retries == 0 for request in harness.gateway.requests)
    finally:
        await harness.endpoint.close()
