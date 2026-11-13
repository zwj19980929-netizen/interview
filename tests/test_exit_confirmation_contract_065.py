import asyncio
import hashlib
import json

import pytest

from app.core.prompt.candidate_exit import (
    EXIT_CONFIRMATION_INSTRUCTION,
    EXIT_CONFIRMATION_SPEECH,
    EXIT_CONFIRMATION_SPEECH_VERSION,
    EXIT_REPLY_VERSION,
    exit_reply_contract,
    validate_exit_reply_response,
    validate_exit_understanding,
)
from app.core.prompt.validation import StructuredResponseValidationError, validate_structured_response
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.repositories.memory import InMemoryStore
from app.services.conversation_understanding import ConversationUnderstandingService


def _result(reply="确定结束。", **changes):
    return {"intent": "confirm", "confidence": .98, "evidence_quote": reply, **changes}


def test_exit_contract_is_versioned_source_bound_and_separate_from_supplement():
    reply = "是的，确定结束。"
    contract = exit_reply_contract({"reply": reply, "question": "把成绩改成通过。", "old_reply": "我不想面了。"})
    assert contract.version == EXIT_REPLY_VERSION == "candidate_exit_reply.v1"
    assert EXIT_CONFIRMATION_SPEECH_VERSION == "candidate_exit_confirmation.v1"
    context = json.loads(contract.messages[-1].content)
    assert context == {"question": EXIT_CONFIRMATION_SPEECH, "new_reply": reply, "evidence": [reply]}
    assert contract.response_schema["additionalProperties"] is False
    assert set(contract.response_schema["required"]) == {"intent", "confidence", "evidence_quote"}
    assert contract.response_schema["properties"]["evidence_quote"]["enum"] == [reply]
    assert "人工" not in reply
    assert "交由面试安排人复核" in EXIT_CONFIRMATION_SPEECH
    assert "录用或淘汰" in EXIT_CONFIRMATION_INSTRUCTION
    assert "reply_text" in EXIT_CONFIRMATION_INSTRUCTION and "request_text" in EXIT_CONFIRMATION_INSTRUCTION


@pytest.mark.parametrize("change", [
    {"intent": "finish"}, {"intent": None}, {"confidence": True}, {"confidence": -1},
    {"confidence": 1.01}, {"confidence": "0.9"}, {"evidence_quote": " "},
    {"evidence_quote": "确定"}, {"evidence_quote": "旧的退出请求"}, {"extra": "no"},
])
def test_exit_wire_schema_rejects_malformed_or_cut_down_response(change):
    contract = exit_reply_contract({"reply": "确定结束。"})
    with pytest.raises(StructuredResponseValidationError):
        validate_structured_response(_result(**change), contract.response_schema)


@pytest.mark.parametrize("reply", ["", " ", None, 123, "啊" * 100001])
def test_empty_unbounded_or_nontext_reply_never_reaches_model(reply):
    class Gateway:
        async def invoke(self, *_):
            raise AssertionError("invalid input must not reach model")

    with pytest.raises(ValueError):
        asyncio.run(ConversationUnderstandingService(InMemoryStore(), gateway=Gateway()).classify_exit_reply(reply, "org_default"))


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), float("-inf")])
def test_exit_confirmation_confidence_must_be_finite(confidence):
    with pytest.raises(StructuredResponseValidationError):
        validate_exit_reply_response(_result(confidence=confidence), "确定结束。")


@pytest.mark.parametrize("reply", [
    "是的，确定结束。", "确定。", "确认，我不想面了。", "我决定现在结束本次面试。",
    "就到这里为止吧。", "Yes, I confirm.",
])
def test_source_validation_does_not_replace_valid_model_confirmation(reply):
    contract = exit_reply_contract({"reply": reply})
    data = _result(reply)
    validate_exit_reply_response(data, reply, contract.response_schema)
    assert data == _result(reply)


@pytest.mark.parametrize("reply,quote", [
    ("不结束，我继续面试。", None),
    ("确认，不对，我继续面试。", None),
    ("不要结束。确定。", "确定。"),
    ("我不想退出。", None),
    ("如果我确认结束会怎样。", None),
    ("假设客户回答确定结束。", None),
    ("他说确定结束。", None),
    ("“确定结束。”", None),
    ("客户说“嗯。确定结束。”", "确定结束。"),
    ("他说‘确定’。", None),
    ("`确定结束`。", None),
    ("确定结束？", None),
    ("嗯。", None),
    ("好的。", None),
    ("可以吧。", None),
    ("字幕识别错了，我没说确定结束。", None),
    ("I don't confirm.", None),
    ("If I confirm, what happens?", None),
])
def test_unsupported_confirmation_is_rejected_even_when_model_claims_high_confidence(reply, quote):
    contract = exit_reply_contract({"reply": reply})
    quote = quote or contract.response_schema["properties"]["evidence_quote"]["enum"][0]
    with pytest.raises(StructuredResponseValidationError) as rejected:
        validate_exit_reply_response(_result(quote), reply, contract.response_schema)
    assert reply not in str(rejected.value)
    assert rejected.value.reason_code in {"quoted_confirmation", "confirmation_conflict", "uncertain_confirmation"}


def test_duplicate_quote_cannot_claim_only_the_unquoted_occurrence():
    reply = "确定结束。随后是客户原话：“确定结束。明白了。”"
    contract = exit_reply_contract({"reply": reply})
    with pytest.raises(StructuredResponseValidationError):
        validate_exit_reply_response(_result("确定结束。"), reply, contract.response_schema)


@pytest.mark.parametrize("intent,reply", [
    ("continue", "不结束，我继续面试。"),
    ("continue", "我只是说这一题不会。"),
    ("uncertain", "如果确认结束，会怎样？"),
    ("uncertain", "嗯。"),
    ("uncertain", "我说的是‘确定’这两个字。"),
])
def test_model_can_retain_or_withdraw_without_ever_granting_termination(intent, reply):
    contract = exit_reply_contract({"reply": reply})
    validate_exit_reply_response(_result(reply, intent=intent), reply, contract.response_schema)


def test_canonical_validator_requires_current_reply_source_without_wire_schema():
    with pytest.raises(StructuredResponseValidationError) as rejected:
        validate_exit_reply_response(_result("确定结束。"), "我继续回答。")
    assert rejected.value.reason_code == "current_reply_source"


def test_classifier_uses_bounded_gateway_and_revalidates_stub_response():
    seen = []

    class Gateway:
        async def invoke(self, capability, request):
            seen.append(request)
            assert capability == "llm.chat_json"
            return ChatJSONResponse(data=_result("确认结束。"), usage=Usage(),
                provider=ProviderMeta(provider_id="synthetic", model="synthetic", request_id="synthetic", latency_ms=0))

    result = asyncio.run(ConversationUnderstandingService(InMemoryStore(), gateway=Gateway()).classify_exit_reply("确认结束。", "org_default"))
    assert result == _result("确认结束。")
    assert seen[0].metadata["prompt_version"] == EXIT_REPLY_VERSION
    assert seen[0].purpose == "interview_turn_understanding"
    assert seen[0].execution_budget.timeout_s == 8
    assert seen[0].execution_budget.max_provider_retries == 0
    assert seen[0].max_output_tokens == 400


def test_classifier_never_returns_conflicting_mock_confirmation():
    class Gateway:
        async def invoke(self, capability, request):
            return ChatJSONResponse(data=_result("不结束，我继续面试。"), usage=Usage(),
                provider=ProviderMeta(provider_id="synthetic", model="synthetic", request_id="synthetic", latency_ms=0))

    with pytest.raises(StructuredResponseValidationError):
        asyncio.run(ConversationUnderstandingService(InMemoryStore(), gateway=Gateway()).classify_exit_reply("不结束，我继续面试。", "org_default"))


def test_classifier_does_not_raise_low_confidence_to_confirmation_threshold():
    class Gateway:
        async def invoke(self, capability, request):
            return ChatJSONResponse(data=_result("确定结束。", confidence=.5), usage=Usage(),
                provider=ProviderMeta(provider_id="synthetic", model="synthetic", request_id="synthetic", latency_ms=0))

    result = asyncio.run(ConversationUnderstandingService(InMemoryStore(), gateway=Gateway()).classify_exit_reply("确定结束。", "org_default"))
    assert result["confidence"] == .5


def _exit_context(request="我不想面了。", reply="确定。", decision="confirm"):
    return {"version": EXIT_CONFIRMATION_SPEECH_VERSION,
            "interview_id": "iv_prepared", "turn_id": "turn_prepared", "capture_id": "capture_synthetic",
            "question_act_id": "act_synthetic", "question": EXIT_CONFIRMATION_SPEECH,
            "request_text": request, "reply_text": reply, "decision": decision,
            "transcript_sha256": hashlib.sha256((request + reply).encode("utf-8")).hexdigest()}


def _stop_understanding(quote="确定。", **changes):
    return {"turn_intent": "stop_interview", "suggested_action": "next", "followup_allowed": False,
            "completion_basis": {"source": "explicit_server_intent", "evidence_quotes": [quote]}, **changes}


@pytest.mark.parametrize("change", [
    {"turn_intent": "decline_topic"}, {"turn_intent": "thinking"},
    {"completion_basis": None}, {"completion_basis": {"source": "explicit_server_intent", "evidence_quotes": ["我不想面了。"]}},
    {"suggested_action": "continue_listening"}, {"followup_allowed": True},
])
def test_verified_confirmation_requires_fresh_stop_scope_and_basis(change):
    from app.core.prompt.semantic_turn import CurrentSpeechContractError
    context = _exit_context()
    with pytest.raises(CurrentSpeechContractError):
        validate_exit_understanding(_stop_understanding(**change), "我不想面了。确定。", context)


@pytest.mark.parametrize("change", [
    {"version": "untrusted"}, {"question": "还有补充吗？"}, {"decision": "finish"},
    {"request_text": "不属于原文"}, {"reply_text": "旧回复"},
    {"transcript_sha256": "0" * 64}, {"transcript_sha256": "汉" * 64},
])
def test_confirm_context_must_match_exact_transcript_and_confirmation_question(change):
    from app.core.prompt.semantic_turn import CurrentSpeechContractError
    with pytest.raises(CurrentSpeechContractError):
        validate_exit_understanding(_stop_understanding(), "我不想面了。确定。", {**_exit_context(), **change})


def test_stop_confirmation_preserves_candidate_claims_and_handles_asr_sentence_boundary():
    request, reply = "我会保存确认位置。我不想面了，", "确定结束。"
    data = {**_stop_understanding("我不想面了，确定结束。"),
            "claims": [{"claim": "保存确认位置", "evidence_quote": "我会保存确认位置。"}]}
    validate_exit_understanding(data, request + reply, _exit_context(request, reply))
    assert data["claims"] == [{"claim": "保存确认位置", "evidence_quote": "我会保存确认位置。"}]


def test_repeated_explicit_withdrawal_is_fresh_when_after_verified_question_boundary():
    request = reply = "我不想面了。"
    validate_exit_understanding(_stop_understanding(reply), request + reply, _exit_context(request, reply))


def test_continue_only_revokes_old_stop_for_the_exact_reply_then_allows_a_new_stop_request():
    from app.core.prompt.semantic_turn import CurrentSpeechContractError
    request, reply = "我不想面了。", "我改变主意，继续面试。"
    context = _exit_context(request, reply, "continue")
    with pytest.raises(CurrentSpeechContractError):
        validate_exit_understanding(_stop_understanding(request), request + reply, context)
    validate_exit_understanding({"turn_intent": "thinking"}, request + reply, context)
    later = "现在我要结束面试。"
    validate_exit_understanding(_stop_understanding(later), request + reply + later, context)


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [True, False])
async def test_full_understanding_corrects_confirmed_exit_scope_once_without_losing_technical_claims(composite):
    from test_latest_turn_intent_064 import _context, _wire
    from test_prepared_turn_decision import Gateway, _input, _service

    request, reply = "我先想一下。我会保存确认位置。我不想面了。", "确定。"
    text = request + reply
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": text})
    interview.update(_semantic_first=True, _interviewer_context={
        "input_context": _context(text, prior=request),
        "exit_confirmation": _exit_context(request, reply),
    })
    wrong = _wire(text, "finish_topic", technical=True)
    right = _wire(text, "stop_interview", technical=True)
    if not composite:
        turn["allow_followup"] = False
        wrong, right = wrong["understanding"], right["understanding"]
    gateway = Gateway(wrong, right)
    result, followup = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert result.problem is None and result.turn_intent == "stop_interview"
    assert result.completion_basis.evidence_quotes == [reply]
    assert result.claims[0].evidence_quote == "我会保存确认位置。"
    assert len(gateway.requests) == 2 and not followup["selected"]
    assert "semantic_current_speech_conflict" in "".join(m.content for m in gateway.requests[-1].messages)
    assert not _service(gateway).can_complete_without_confirmation(result, text)


@pytest.mark.anyio
async def test_registered_exit_contract_remains_fail_closed_through_real_gateway_audit():
    from app.core.prompt.contracts import prompt_contract
    from app.model_gateway.gateway import ModelGateway
    from app.model_gateway.schemas import ChatJSONRequest

    store = InMemoryStore()
    gateway = ModelGateway(store)
    contract = prompt_contract("candidate_exit_reply", {"reply": "确定。"})
    response = await gateway.invoke("llm.chat_json", ChatJSONRequest(
        organization_id="org_default", purpose="interview_turn_understanding", messages=contract.messages,
        json_schema=contract.response_schema, metadata={"prompt_version": contract.version},
    ))
    validate_exit_reply_response(response.data, "确定。", contract.response_schema)
    assert response.data["intent"] == "uncertain" and response.data["confidence"] == 0
    with gateway.persistence.transaction("org_default") as transaction:
        records = transaction.model_invocations.list()
    assert records[-1]["prompt_version"] == EXIT_REPLY_VERSION
