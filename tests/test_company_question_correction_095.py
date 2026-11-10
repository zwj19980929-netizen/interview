"""Company-question corrections preserve evidence, policy and call limits."""
from copy import deepcopy
import json
import logging

import pytest
from pydantic import TypeAdapter

from app.core.answer_processing_budget import AnswerProcessingBudget, answer_processing_budget
from app.core.prompt.contracts import prompt_contract
from app.core.prompt.semantic_turn import SemanticContractError, validate_semantic_turn
from app.core.prompt.understanding_references import understanding_references, resolve_understanding_references
from app.core.prompt.validation import validate_structured_response
from app.domain.interview_agent import TurnUnderstanding
from app.model_gateway import capabilities as cap
from app.model_gateway.gateway import ModelGateway
from app.model_gateway.schemas import ChatJSONRequest
from app.repositories.memory import InMemoryStore
from test_company_questions import QUESTION, TECH, company_understanding
from test_prepared_turn_decision import Gateway, _data, _input, _service


def test_recovery_clarification_is_independently_versioned_without_changing_the_existing_notice():
    from app.core.prompt.conversation_reception import (
        RECEPTION_RECOVERY_SPEECH_VERSION, RECEPTION_SPEECH, RECEPTION_SPEECH_VERSION,
    )
    assert RECEPTION_RECOVERY_SPEECH_VERSION == "conversation_recovery_speech.v1"
    assert RECEPTION_SPEECH["recovery_clarification"] == (
        "刚才这句话暂时没理解清楚。你可以换个说法，简短说明你想回答或询问的内容，我们继续当前这道题。")
    # Its exact old version/text is used by prepared-context reuse. A genuine
    # clarification must not become that reusable status-only notice.
    assert RECEPTION_SPEECH_VERSION == "conversation_reception_speech.v4"
    assert RECEPTION_SPEECH["unavailable"] == (
        "刚才的回答暂时没处理成功，录音和转写已保留，不用重复整段回答。请留意页面的处理提示。")


def canonical_company():
    raw = company_understanding(QUESTION, ["幂等键", "确认序号"])
    return resolve_understanding_references(raw, understanding_references(QUESTION, ["幂等键", "确认序号"]))


def test_company_correction_requires_explicit_question_field_but_never_fabricates_its_value():
    from app.core.prompt.contracts import structured_output_instruction
    from app.core.prompt.validation import StructuredResponseValidationError
    context = {"transcript": QUESTION, "capability_points": ["幂等键", "确认序号"],
        "semantic_first": True, "interviewer_context": {"company_context": {"available": False}}}
    ordinary = prompt_contract("interview_turn_understanding", context)
    corrected = prompt_contract("interview_turn_understanding", {**context,
        "correction_reason": "company_question_contract_invalid"})
    valid = company_understanding(QUESTION, context["capability_points"])
    omitted = deepcopy(valid)
    omitted.pop("company_question")
    validate_structured_response(omitted, ordinary.response_schema)
    with pytest.raises(StructuredResponseValidationError, match="required"):
        validate_structured_response(omitted, corrected.response_schema)
    validate_structured_response(valid, corrected.response_schema)
    invalid_null = {**valid, "company_question": None}
    validate_structured_response(invalid_null, corrected.response_schema)
    with pytest.raises(SemanticContractError) as rejected:
        validate_semantic_turn(resolve_understanding_references(invalid_null,
            understanding_references(QUESTION, context["capability_points"])), QUESTION)
    assert rejected.value.contract_detail == "missing_question"
    keys = list(corrected.response_schema["properties"])
    assert keys.index("company_question") == keys.index("turn_intent") + 1
    instruction = structured_output_instruction(corrected.response_schema)
    example = json.loads(instruction.split("Example JSON output: ", 1)[1])
    assert "company_question" in example
    assert "公司答案" in corrected.response_schema["properties"]["company_question"]["description"]


@pytest.mark.parametrize("change, detail", [
    ({"company_question": None}, "missing_question"),
    ({"completion_basis": {"source": "explicit_server_intent", "evidence_quotes": [QUESTION]}}, "completion_basis_present"),
    ({"followup_allowed": True}, "followup_allowed"),
    ({"suggested_action": "next"}, "action_mismatch"),
    ({"confidence": .74}, "low_confidence"),
    ({"ambiguities": ["PRIVATE_AMBIGUITY"]}, "unresolved_ambiguity"),
    ({"intent": "answer"}, "intent_content_mismatch"),
    ({"company_question": {"start": 0, "end": len(QUESTION), "quote": "PRIVATE_QUOTE"}}, "quote_mismatch"),
    ({"company_question": {"start": 0, "end": 0, "quote": ""}}, "invalid_span"),
    ({"turn_intent": "clarify"}, "unexpected_question"),
    ({"turn_intent": "clarify", "company_question": None}, "unexpected_action"),
    ({"turn_intent": "clarify", "company_question": None, "suggested_action": "clarify"}, "unexpected_intent"),
])
def test_rejected_company_contract_keeps_aggregate_error_and_fixed_detail(change, detail, caplog):
    data = canonical_company()
    data.update(deepcopy(change))
    before = deepcopy(data)
    with pytest.raises(SemanticContractError) as caught:
        validate_semantic_turn(data, QUESTION)
    assert caught.value.reason_code == "company_question_contract_invalid"
    assert caught.value.schema_path == "$.company_question"
    assert caught.value.contract_detail == detail
    assert str(caught.value) == "company_question_contract_invalid"
    assert data == before
    assert "PRIVATE_" not in repr(vars(caught.value))
    from app.services.conversation_understanding import _log_validation_rejection
    with caplog.at_level(logging.INFO, logger="interviewer.speech_evidence"):
        _log_validation_rejection("turn_decision_contract_rejected", caught.value,
            stage="understanding_content", attempt=1, interview_id="iv_synthetic_095", turn_id="turn_synthetic_095")
    structured = [json.loads(record.getMessage()) for record in caplog.records
                  if record.name == "interviewer.speech_evidence"]
    assert len(structured) == 1
    assert structured[0]["failure_reason"] == "company_question_contract_invalid"
    assert structured[0]["schema_path"] == "$.company_question"
    assert structured[0]["contract_detail"] == detail
    assert "PRIVATE_" not in caplog.text and QUESTION not in caplog.text


@pytest.mark.parametrize("detail", ["PRIVATE_UNTRUSTED_DETAIL", ["PRIVATE_UNTRUSTED_DETAIL"],
                                  {"detail": "PRIVATE_UNTRUSTED_DETAIL"}])
def test_non_enum_contract_details_cannot_escape_structured_or_plain_logging(detail, caplog):
    from app.services.conversation_understanding import _log_validation_rejection
    error = SemanticContractError("company_question_contract_invalid", "$.company_question")
    error.contract_detail = detail
    with caplog.at_level(logging.INFO, logger="interviewer.speech_evidence"):
        _log_validation_rejection("turn_decision_contract_rejected", error,
            stage="understanding_content", attempt=1, interview_id="iv_synthetic_095", turn_id="turn_synthetic_095")
    records = [json.loads(record.getMessage()) for record in caplog.records
               if record.name == "interviewer.speech_evidence"]
    assert len(records) == 1 and "contract_detail" not in records[0]
    assert records[0]["failure_reason"] == "company_question_contract_invalid"
    assert records[0]["schema_path"] == "$.company_question"
    assert "PRIVATE_UNTRUSTED_DETAIL" not in caplog.text


@pytest.mark.parametrize("purpose,old,new", [
    ("interview_turn_understanding", (120, 121, 124, 125), (130, 131, 132, 133)),
    ("interview_turn_decision", (151, 152, 155, 156), (161, 162, 163, 164)),
])
@pytest.mark.parametrize("confirmed,exit_context,index", [(False, False, 0), (True, False, 1),
                                                           (False, True, 2), (True, True, 3)])
def test_only_company_correction_is_reversioned_and_requires_the_explicit_question_slot(purpose, old, new, confirmed, exit_context, index):
    context = {"transcript": TECH + QUESTION, "capability_points": ["幂等键", "确认序号"],
               "semantic_first": True, "completion_confirmed": confirmed,
               "interviewer_context": {"company_context": {"available": False}}}
    if exit_context:
        context["interviewer_context"]["exit_confirmation"] = {"status": "awaiting_reply"}
    original = prompt_contract(purpose, context)
    corrected = prompt_contract(purpose, {**context, "correction_reason": "company_question_contract_invalid"})
    unrelated = prompt_contract(purpose, {**context, "correction_reason": "wire_schema_invalid"})
    assert original.version == unrelated.version == f"{purpose}.v{old[index]}"
    assert corrected.version == f"{purpose}.v{new[index]}"
    assert original.response_schema == unrelated.response_schema
    before = original.response_schema if purpose == "interview_turn_understanding" else original.response_schema["properties"]["understanding"]
    after = corrected.response_schema if purpose == "interview_turn_understanding" else corrected.response_schema["properties"]["understanding"]
    assert "company_question" not in before["required"]
    assert "company_question" in after["required"]
    compatible = deepcopy(after)
    compatible["required"].remove("company_question")
    compatible["properties"]["company_question"].pop("description")
    assert compatible == before
    messages = "\n".join(item.content for item in corrected.messages)
    for required in ("公司反问字段需要一致", "题干术语", "不能仅凭英文拼写或音近词", "confidence至少0.75",
                     "不要提高confidence", "保留全部可靠claims", "completion_basis=null", "不能据此结束本题"):
        assert required in messages
    assert "公司反问字段需要一致" not in "\n".join(item.content for item in unrelated.messages)


@pytest.mark.parametrize("semantic,optional", [(False, False), (False, True), (True, False)])
@pytest.mark.parametrize("purpose", ["interview_turn_understanding", "interview_turn_decision"])
def test_company_guidance_does_not_change_unrelated_contract_branches(semantic, optional, purpose):
    context = {"transcript": TECH, "capability_points": ["幂等键"], "semantic_first": semantic,
               "interviewer_context": {"company_context": {"available": False}} if optional else {}}
    original = prompt_contract(purpose, context)
    corrected = prompt_contract(purpose, {**context, "correction_reason": "company_question_contract_invalid"})
    assert corrected.version == original.version
    assert corrected.response_schema == original.response_schema
    assert "公司反问字段需要一致" not in "\n".join(item.content for item in corrected.messages)


@pytest.mark.anyio
@pytest.mark.parametrize("purpose,old,new", [
    ("interview_turn_understanding", 120, 130), ("interview_turn_understanding", 121, 131),
    ("interview_turn_understanding", 124, 132), ("interview_turn_understanding", 125, 133),
    ("interview_turn_decision", 151, 161), ("interview_turn_decision", 152, 162),
    ("interview_turn_decision", 155, 163), ("interview_turn_decision", 156, 164),
])
async def test_new_versions_pass_real_gateway_audit_and_keep_historical_mock_wire_shapes(purpose, old, new):
    context = {"transcript": TECH, "capability_points": ["幂等键", "确认序号"], "semantic_first": True,
               "completion_confirmed": new in {131, 133, 162, 164},
               "interviewer_context": {"company_context": {"available": False}}}
    if new in {132, 133, 163, 164}:
        context["interviewer_context"]["exit_confirmation"] = {"status": "awaiting_reply"}
    contract = prompt_contract(purpose, {**context, "correction_reason": "company_question_contract_invalid"})
    versions = TypeAdapter(TurnUnderstanding.model_fields["prompt_version"].annotation)
    store = InMemoryStore()
    gateway = ModelGateway(store)
    results = []
    for number in (old, new - 4, new):
        version = f"{purpose}.v{number}"
        assert versions.validate_python(version) == version
        schema = deepcopy(contract.response_schema)
        if number != new:
            previous = schema if purpose == "interview_turn_understanding" else schema["properties"]["understanding"]
            previous["required"].remove("company_question")
            previous["properties"]["company_question"].pop("description")
        response = await gateway.invoke(cap.LLM_CHAT_JSON, ChatJSONRequest(
            organization_id="org_default", purpose="interview_turn_understanding",
            messages=contract.messages, json_schema=schema,
            metadata={"prompt_version": version, "transcript": TECH,
                      "capability_points": context["capability_points"]},
        ))
        validate_structured_response(response.data, schema)
        results.append(response.data)
        assert store.model_invocations[-1]["prompt_version"] == version
        assert store.model_invocations[-1]["status"] == "success"
    assert results[0] == results[1]
    latest = deepcopy(results[2])
    latest_understanding = latest if purpose == "interview_turn_understanding" else latest["understanding"]
    assert latest_understanding.pop("company_question") is None
    assert latest == results[0]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["json_schema", "json_object", "prompt"])
async def test_required_company_source_survives_each_provider_request_encoding(mode):
    import httpx
    from test_openai_compatible_provider import make_provider
    context = {"transcript": QUESTION, "capability_points": ["幂等键", "确认序号"],
        "semantic_first": True, "interviewer_context": {"company_context": {"available": False}},
        "correction_reason": "company_question_contract_invalid"}
    contract = prompt_contract("interview_turn_decision", context)
    valid = _data()
    valid["understanding"] = company_understanding(QUESTION, context["capability_points"])
    valid["followup"].update(selected=False, evidence_id="", target_point_ids=[], question_text="", rationale="")

    def handler(request):
        payload = json.loads(request.content)
        if mode == "json_schema":
            schema = payload["response_format"]["json_schema"]["schema"]
        else:
            instruction = payload["messages"][0]["content"]
            schema = json.loads(instruction.split("Schema: ", 1)[1].split(". Example JSON output: ", 1)[0])
            example = json.loads(instruction.split("Example JSON output: ", 1)[1])
            assert "company_question" in example["understanding"]
        assert schema == contract.response_schema
        assert "company_question" in schema["properties"]["understanding"]["required"]
        return httpx.Response(200, json={"id": "synthetic_095", "choices": [
            {"message": {"content": json.dumps(valid)}, "finish_reason": "stop"}]})

    response = await make_provider(handler).chat_json(ChatJSONRequest(
        purpose="interview_turn_understanding", messages=contract.messages, json_schema=contract.response_schema,
        metadata={"prompt_version": contract.version}),
        config={"base_url": "https://synthetic.example/v1", "structured_output_mode": mode},
        credentials={"api_key": "synthetic-test"}, model="synthetic", timeout_s=5)
    validate_structured_response(response.data, contract.response_schema)


@pytest.mark.anyio
@pytest.mark.parametrize("mixed", [False, True])
async def test_real_understanding_correction_keeps_original_evidence_and_two_call_budget(mixed):
    utterance, turn, session = _input()
    text = (TECH if mixed else "") + QUESTION
    utterance = utterance.model_copy(update={"text": text})
    session.update(_semantic_first=True, _interviewer_context={"company_context": {"available": False}})
    valid = _data()
    valid["understanding"] = company_understanding(text, ["幂等键", "确认序号"], mixed=mixed)
    invalid = deepcopy(valid)
    invalid["understanding"]["confidence"] = .74
    gateway = Gateway(invalid, valid)
    budget = AnswerProcessingBudget(limit=2)
    service = _service(gateway)
    with answer_processing_budget(budget):
        understanding, followup = await service.prepare_decision(utterance, turn, session)
    assert understanding.problem is None and not followup["selected"]
    assert [request.metadata["prompt_version"] for request in gateway.requests] == [
        "interview_turn_decision.v151", "interview_turn_decision.v161"]
    assert budget.used == 2
    assert all(request.execution_budget.max_provider_attempts == 1 for request in gateway.requests)
    assert understanding.company_question.quote == QUESTION
    assert bool(understanding.claims) is mixed
    assert understanding.completion_basis is None
    assert not service.can_complete_without_confirmation(understanding, text)
    assert utterance.text == text


@pytest.mark.anyio
async def test_invalid_company_correction_still_fails_closed_at_original_limit():
    utterance, turn, session = _input()
    utterance = utterance.model_copy(update={"text": QUESTION})
    session.update(_semantic_first=True, _interviewer_context={"company_context": {"available": False}})
    invalid = _data()
    invalid["understanding"] = company_understanding(QUESTION, ["幂等键", "确认序号"])
    invalid["understanding"]["confidence"] = .74
    gateway = Gateway(invalid, invalid)
    budget = AnswerProcessingBudget(limit=2)
    with answer_processing_budget(budget):
        understanding, followup = await _service(gateway).prepare_decision(utterance, turn, session)
    assert understanding.problem.reason_code == "company_question_contract_invalid"
    assert understanding.company_question is None and not understanding.claims
    assert understanding.completion_basis is None and not followup["selected"]
    assert len(gateway.requests) == budget.used == 2


@pytest.mark.anyio
async def test_term_ambiguity_correction_preserves_original_words_without_creating_a_company_question():
    utterance, turn, session = _input()
    text = "题目里的Salary指什么？"
    utterance = utterance.model_copy(update={"text": text})
    turn["question_spoken_text"] = "如何使用Celery处理异步任务？"
    session.update(_semantic_first=True, _interviewer_context={"company_context": {"available": False}})
    invalid = _data()
    invalid["understanding"] = company_understanding(QUESTION, ["幂等键", "确认序号"])
    invalid["understanding"].update(company_question=None)
    corrected = deepcopy(invalid)
    corrected["understanding"].update(intent="clarification_request", answer_content="none",
        turn_intent="clarify", suggested_action="clarify", confidence=.6, company_question=None,
        answer_summary="候选人询问题目中的术语，需确认所指。", evidence_ids=["E1"],
        ambiguities=["该英文词的所指需要确认。"],
        clarification_target={"evidence_id": "E1", "focus_quote": "Salary"})
    corrected["followup"].update(selected=False, evidence_id="", target_point_ids=[], question_text="", rationale="")
    gateway = Gateway(invalid, corrected)
    understanding, followup = await _service(gateway).prepare_decision(utterance, turn, session)
    assert understanding.problem is None and not followup["selected"]
    assert understanding.company_question is None and understanding.completion_basis is None
    assert understanding.clarification_target.focus_quote == "Salary"
    assert understanding.evidence_quotes == [text] and utterance.text == text
    assert not understanding.claims
    validate_structured_response(corrected, gateway.requests[-1].json_schema)
