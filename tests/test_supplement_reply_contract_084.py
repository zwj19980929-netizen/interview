"""One current supplement utterance chooses a safe route without answering it."""
import json
import asyncio
from types import SimpleNamespace

import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.supplement_reply import validate_supplement_reply, SUPPLEMENT_REPLY_INTENTS
from app.core.prompt.validation import StructuredResponseValidationError
from app.core.errors import ApiError
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.repositories.memory import InMemoryStore
from app.services.conversation_understanding import ConversationUnderstandingService


def _response(intent="question_clarification", **changes):
    return {"intent": intent, "confidence": .95, "evidence_id": "E1",
            "reply_text": "这个术语通常指一种任务队列。", **changes}


def test_contract_has_one_route_and_only_fresh_utterance_evidence():
    contract = prompt_contract("supplement_reply", {"reply": "这个术语是什么？",
        "question": "还有补充吗？", "conversation_context": {
            "stage": "awaiting_reply", "current_question": "请说明任务处理方式。",
            "root_question": "请说明系统设计。", "last_delivered_utterance": "还有补充吗？",
            "history": [{"role": "interviewer", "text": "已播出的解释。", "delivery_status": "delivered"},
                        {"role": "interviewer", "text": "未播出的私有说明。", "delivery_status": "selected"}],
            "rubric": "private-rubric-sentinel", "standard_answer": "private-answer-sentinel"}})
    assert contract.version == "supplement_reply.v6"
    context = json.loads(contract.messages[-1].content)
    assert context["evidence"] == {"E1": "这个术语是什么？"}
    assert context["stage"] == "awaiting_reply"
    serialized = contract.messages[-1].content
    assert "private-" not in serialized and "未播出的私有" not in serialized
    assert {"question_clarification", "repeat", "interview_dialogue", "off_topic", "stop_interview",
            "finish_topic", "decline_topic", "finish", "supplement", "unclear"} <= set(SUPPLEMENT_REPLY_INTENTS)


def test_generic_word_meaning_is_distinct_from_company_facts_without_rewriting_speech():
    reply = "这个普通词的意思是什么？"
    contract = prompt_contract("supplement_reply", {"reply": reply})
    system = contract.messages[0].content
    assert "即使该词不在当前题中" in system
    assert "不把词义提问当作询问本公司事实" in system
    assert "不得改写候选人原文或猜成另一个词" in system
    assert json.loads(contract.messages[-1].content)["reply"] == reply


def test_current_reply_target_overrides_historical_check_and_bare_ack_is_not_finish():
    contract = prompt_contract("supplement_reply", {"reply": "嗯，好吧。", "question": "还有补充吗？",
        "conversation_context": {"stage": "awaiting_reply", "reply_target": "other_delivered_speech",
                                 "last_delivered_utterance": "这个术语是指异步任务队列。"}})
    public = json.loads(contract.messages[-1].content)
    assert public["reply_target"] == "other_delivered_speech"
    assert "历史补充确认问题" in contract.messages[0].content
    assert "孤立附和绝不能输出finish" in contract.messages[0].content
    assert "我已经说完了" in contract.messages[0].content


@pytest.mark.parametrize("change", [
    {"intent": "invented"}, {"confidence": True}, {"confidence": 2},
    {"evidence_id": "E2"}, {"private": "extra"}, {"reply_text": ""},
    {"reply_text": "https://example.invalid"}, {"repeat_target": "last_clarification"},
])
def test_new_contract_rejects_invalid_or_unbound_response(change):
    contract = prompt_contract("supplement_reply", {"reply": "这个术语是什么？"})
    with pytest.raises(StructuredResponseValidationError):
        validate_supplement_reply(_response(**change), contract.response_schema, reply="这个术语是什么？")


@pytest.mark.parametrize("intent", ["finish", "finish_topic", "decline_topic", "stop_interview",
                                   "pause", "continue", "supplement", "company_question", "off_topic", "repeat"])
def test_control_and_fixed_routes_cannot_smuggle_generated_speech(intent):
    contract = prompt_contract("supplement_reply", {"reply": "合成新发言。"})
    with pytest.raises(StructuredResponseValidationError):
        validate_supplement_reply(_response(intent), contract.response_schema, reply="合成新发言。")
    result = validate_supplement_reply(_response(intent, reply_text=""), contract.response_schema,
                                       reply="合成新发言。")
    assert result["intent"] == intent and result["reply_text"] == ""


@pytest.mark.anyio
async def test_one_bounded_call_restores_fresh_evidence_and_validates_before_return():
    calls = []
    class Gateway:
        async def invoke(self, capability, request):
            calls.append(request)
            return ChatJSONResponse(data=_response(), usage=Usage(), provider=ProviderMeta(
                provider_id="synthetic", model="synthetic", request_id="synthetic", latency_ms=0))
    result = await ConversationUnderstandingService(InMemoryStore(), gateway=Gateway()).classify_supplement_reply(
        "这个术语是什么？", "org_default", conversation_context={"stage": "awaiting_reply"})
    assert len(calls) == 1
    assert calls[0].execution_budget.timeout_s == 8
    assert calls[0].execution_budget.max_provider_retries == 0
    assert calls[0].execution_budget.max_provider_attempts == 1
    assert calls[0].max_output_tokens == 350
    assert result == {"intent": "question_clarification", "confidence": .95,
        "evidence_quote": "这个术语是什么？", "reply_text": "这个术语通常指一种任务队列。", "repeat_target": None}


@pytest.mark.anyio
@pytest.mark.parametrize("change", [None, "final_confidence", "boundary_provider", "recognizer_final",
                                    "capture", "recognizer", "generation", "turn", "context", "owner"])
async def test_complete_reply_and_context_are_fenced_across_the_small_call(tmp_path, monkeypatch, change):
    from test_preparation_fences_081 import _chain
    from test_answer_reuse_078 import _append, _delivered
    harness = _chain(tmp_path, monkeypatch)
    service = harness.stream.interviews
    _delivered(service)
    final = _append(harness.final, "这个术语是什么？")
    harness.stream.stream = SimpleNamespace(_final=final.model_copy(deep=True))
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    class Gateway:
        async def invoke(self, capability, request):
            requests.append(request)
            entered.set()
            await release.wait()
            return ChatJSONResponse(data=_response(), usage=Usage(), provider=ProviderMeta(
                provider_id="synthetic", model="synthetic", request_id="synthetic", latency_ms=0))
    service.conversation.gateway = Gateway()
    task = asyncio.create_task(harness.chain.classify_supplement_reply("这个术语是什么？",
        final=final, boundary_final=harness.final))
    await asyncio.wait_for(entered.wait(), 1)
    if change == "final_confidence": final.confidence = .8
    elif change == "boundary_provider": harness.final.provider.model = "another-model"
    elif change == "recognizer_final": harness.stream.stream._final.confidence = .7
    elif change == "capture": harness.chain._stt = None
    elif change == "recognizer": harness.stream.stream = SimpleNamespace(_final=final)
    elif change == "generation": harness.stream._capture_generation += 1
    elif change == "turn": harness.stream.turn_id = "changed_turn"
    elif change == "owner": harness.coordinator.release(harness.owner)
    elif change == "context":
        with service.persistence.transaction("org_default") as tx:
            session = tx.interview_sessions.get("iv_binding")
            session["turns"][0]["question_spoken_text"] = "一条新的正式问题。"
            tx.interview_sessions.update(session, expected_version=session["version"])
    release.set()
    if change:
        with pytest.raises(ApiError):
            await asyncio.wait_for(task, 1)
    else:
        assert (await asyncio.wait_for(task, 1))["intent"] == "question_clarification"
        context = json.loads(requests[0].messages[-1].content)
        assert context["stage"] == "awaiting_reply"
        assert context["current_question"] == "请解释如何防止重复请求。"
        assert context["evidence"] == {"E1": "这个术语是什么？"}
        assert "使用请求标识记录处理状态" not in requests[0].messages[-1].content
    assert len(requests) == 1
    assert service.get_interview("iv_binding")["answers"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["source", "selected", "interrupted", "failed"])
async def test_stale_source_or_undelivered_check_is_rejected_before_model_call(tmp_path, monkeypatch, change):
    from test_preparation_fences_081 import _chain
    from test_answer_reuse_078 import _append, _delivered
    harness = _chain(tmp_path, monkeypatch)
    service = harness.stream.interviews
    _delivered(service, status="delivered" if change == "source" else change)
    final = _append(harness.final, "我已经说完了。")
    observed = final.model_copy(deep=True)
    if change == "source":
        observed.confidence = .4
    harness.stream.stream = SimpleNamespace(_final=observed)
    calls = []
    class Gateway:
        async def invoke(self, capability, request):
            calls.append(request)
            return ChatJSONResponse(data=_response("finish", reply_text=""), usage=Usage(), provider=ProviderMeta(
                provider_id="synthetic", model="synthetic", request_id="synthetic", latency_ms=0))
    service.conversation.gateway = Gateway()
    with pytest.raises(ApiError) as rejected:
        await harness.chain.classify_supplement_reply("我已经说完了。", final=final, boundary_final=harness.final)
    assert rejected.value.code == "TURN_DECISION_STALE"
    assert not calls
    assert service.get_interview("iv_binding")["answers"] == []
