"""Delivered clarification sources are selected, never regenerated or granted."""
from copy import deepcopy
import json

import pytest

from app.core.prompt.contracts import prompt_contract, structured_output_instruction
from app.core.prompt.semantic_turn import (
    SemanticContractError, content_clarification_instruction,
    semantic_content_correction_instruction, validate_unresolved_completion,
)
from app.core.prompt.understanding_references import understanding_references, resolve_understanding_references
from app.core.prompt.validation import StructuredResponseValidationError, validate_structured_response
from test_semantic_recovery_066 import _clarification_context, unresolved_response, REPLY
from test_prepared_turn_decision import Gateway, _input, _service
from test_answer_declined_understanding import no_followup


def _context(request="我记录确认位置。任务交给蓝桥", reply=REPLY):
    return {"transcript": request + reply, "semantic_first": True,
            "capability_points": ["幂等键", "确认序号"],
            "interviewer_context": {"content_clarification": _clarification_context(request, reply)}}


def _references(context):
    return understanding_references(context["transcript"], context["capability_points"],
        clarification_context=context["interviewer_context"]["content_clarification"])


@pytest.mark.parametrize("before_text", ["我记录确认位置。任务交给蓝桥", "甲" * 239 + "蓝桥", "蓝桥重复出现，仍是蓝桥"])
def test_fixed_source_and_reference_boundary_do_not_rewrite_or_cross_new_reply(before_text):
    context = _context(before_text)
    before = deepcopy(context)
    references = _references(context)
    source = references["clarification_sources"]["C1"]
    assert source == {"focus_quote": "蓝桥", "evidence_quote": "蓝桥"}
    boundary = len(before_text)
    assert all(not (start < boundary < start + len(references["evidence"][key]))
               for key, start in references["offsets"].items())
    assert REPLY in references["evidence"].values()
    assert "C1" not in references["evidence"]
    assert context == before


@pytest.mark.parametrize("mutation", [
    lambda c: c.update(delivered=False), lambda c: c.update(delivered="true"),
    lambda c: c.update(request_text="不属于原文"), lambda c: c.update(reply_text="旧回复"),
    lambda c: c.update(focus_quote="未出现"), lambda c: c.update(question_act_id=""),
])
def test_unbound_context_cannot_create_fixed_source(mutation):
    context = _context()
    mutation(context["interviewer_context"]["content_clarification"])
    assert not _references(context).get("clarification_sources")
    schema = prompt_contract("interview_turn_understanding", context).response_schema
    with pytest.raises(StructuredResponseValidationError):
        validate_structured_response({"resolution": "same_focus_unresolved", "source_ref": "C1"}, schema["properties"]["clarification_target"])


@pytest.mark.parametrize("target", [
    {"source_ref": "C999"}, {"source_ref": "C1", "focus_quote": "不同焦点"},
    {"source_ref": "C1", "evidence_id": "E1"}, {}, {"source_ref": None},
])
def test_fixed_target_wire_is_strict_and_cannot_hide_other_fields(target):
    schema = prompt_contract("interview_turn_understanding", _context()).response_schema
    with pytest.raises(StructuredResponseValidationError):
        validate_structured_response(target, schema["properties"]["clarification_target"])


def test_fixed_source_requires_explicit_selection_and_keeps_original_wire_unchanged():
    context = _context()
    references = _references(context)
    _, _, data = unresolved_response(technical=True)
    data["evidence_ids"] = list(references["evidence"])
    data["completion_basis"]["evidence_ids"] = [list(references["evidence"])[-1]]
    data["clarification_target"] = {"resolution": "same_focus_unresolved", "source_ref": "C1"}
    before = deepcopy(data)
    result = resolve_understanding_references(data, references)
    assert result["clarification_target"] == references["clarification_sources"]["C1"]
    assert data == before
    data["clarification_target"] = {"resolution": "same_focus_unresolved", "source_ref": "C2"}
    with pytest.raises(SemanticContractError):
        resolve_understanding_references(data, references)
    data["clarification_target"] = {"resolution": "new_focus_unresolved", "evidence_id": "E1", "focus_quote": "位置"}
    assert resolve_understanding_references(data, references)["clarification_target"]["focus_quote"] == "位置"


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [False, True])
@pytest.mark.parametrize("resolved", [False, True])
async def test_both_contracts_restore_fixed_source_or_accept_real_correction(composite, resolved):
    reply = "蓝桥指我们的任务队列。我已经说完了。" if resolved else REPLY
    context = _context(reply=reply)
    references = _references(context)
    _, _, data = unresolved_response(technical=True)
    data.update(evidence_ids=list(references["evidence"]),
                clarification_target={"resolution": "resolved"} if resolved else {"resolution": "same_focus_unresolved", "source_ref": "C1"})
    data["completion_basis"]["evidence_ids"] = [list(references["evidence"])[-1]]
    if resolved:
        data.update(intent="answer", ambiguities=[], followup_allowed=True)
        data["claims"].append({"claim": "蓝桥指任务队列", "evidence_id": "E3"})
    utterance, turn, interview = _input()
    turn["allow_followup"] = composite
    utterance = utterance.model_copy(update={"text": context["transcript"]})
    interview.update(_semantic_first=True, _interviewer_context=context["interviewer_context"])
    gateway = Gateway({"understanding": data, "followup": no_followup()} if composite else data)
    understanding, followup = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.claims
    assert understanding.intent == ("answer" if resolved else "answer_unresolved")
    assert (understanding.clarification_target is None) is resolved
    assert understanding.completion_basis.evidence_quotes == [REPLY]
    assert len(gateway.requests) == 1 and not followup["selected"]
    assert utterance.text == context["transcript"]


def test_source_correction_names_exact_binding_without_inserting_sensitive_text():
    instruction = semantic_content_correction_instruction("unresolved_clarification_source_invalid")
    assert "C1" in instruction and "原焦点" in instruction and "新回复" in instruction
    assert "蓝桥" not in instruction
    contract = prompt_contract("interview_turn_understanding", _context())
    assert "最小实际疑点" in "".join(message.content for message in contract.messages)
    assert "回答不充分" in "".join(message.content for message in contract.messages)
    assert '"source_ref":{"type":"string","enum":["C1"]}' in structured_output_instruction(contract.response_schema)
    assert '"clarification_target":{"resolution":"resolved"}' in structured_output_instruction(contract.response_schema)


@pytest.mark.parametrize("delivered", [False, True])
def test_current_phase_keeps_known_claims_before_testing_real_ambiguity_and_completion(delivered):
    receipt = _context()["interviewer_context"]["content_clarification"] if delivered else None
    instruction = content_clarification_instruction(receipt)
    assert instruction.index("累计完整原文逐项提取") < instruction.index("知识缺失和语言歧义")
    assert instruction.index("知识缺失和语言歧义") < instruction.index("当前新回复是否消除了旧疑点")
    assert "没给出具体产品或作用细节不构成歧义" in instruction
    assert "只属于回答不充分" in instruction and "最小疑点仅古帕" in instruction
    if delivered:
        assert "旧疑点已解决，不能再选C1" in instruction
        assert "旧C1不适用" in instruction and "不能只概括当前解释或结束语" in instruction
    else:
        assert "不要因为当前还没有内容澄清记录就先制造疑点" in instruction


@pytest.mark.parametrize("purpose", ["interview_turn_understanding", "interview_turn_decision"])
@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("semantic", [False, True])
def test_semantic_contract_has_one_initial_system_without_losing_policy_or_evidence(purpose, confirmed, semantic):
    from app.core.prompt.contracts import _interview_turn_understanding, _interview_turn_decision
    context = _context()
    context.update(semantic_first=semantic, completion_confirmed=confirmed,
                   correction_reason="unresolved_clarification_source_invalid")
    context["interviewer_context"].update(skill={}, company_context={"available": False},
                                        exit_confirmation={"status": "awaiting_reply"})
    before = deepcopy(context)
    raw = (_interview_turn_understanding if purpose == "interview_turn_understanding" else _interview_turn_decision)(context)
    contract = prompt_contract(purpose, context)
    assert context == before and contract.version == raw.version
    assert contract.response_schema == raw.response_schema
    if semantic:
        assert [message.role for message in contract.messages].count("system") == 1
        assert contract.messages[0].role == "system"
        assert contract.messages[0].content == "\n\n".join(message.content for message in raw.messages if message.role == "system")
        assert contract.messages[1:] == [message for message in raw.messages if message.role != "system"]
        assert "本轮内容澄清阶段" in contract.messages[0].content
        if purpose == "interview_turn_decision":
            assert "本次一次性返回 understanding 与 followup" in contract.messages[0].content
    else:
        assert contract.messages == raw.messages


@pytest.mark.parametrize("target", [{"resolution": "resolved"}, {"resolution": "same_focus_unresolved", "source_ref": "C1"}, {"resolution": "new_focus_unresolved", "evidence_id": "E1", "focus_quote": "位置"}])
def test_all_target_shapes_are_validated_without_cross_branch_fields(target):
    schema = prompt_contract("interview_turn_understanding", _context()).response_schema
    validate_structured_response(target, schema["properties"]["clarification_target"])


@pytest.mark.parametrize("location", ["claim", "evidence", "completion"])
def test_clarification_source_cannot_authorize_claims_or_completion(location):
    schema = prompt_contract("interview_turn_understanding", _context()).response_schema
    if location == "claim":
        value, field = "C1", schema["properties"]["claims"]["items"]["properties"]["evidence_id"]
    elif location == "evidence":
        value, field = ["C1"], schema["properties"]["evidence_ids"]
    else:
        value = {"source": "explicit_server_intent", "evidence_ids": ["C1"]}
        field = schema["properties"]["completion_basis"]
    with pytest.raises(StructuredResponseValidationError):
        validate_structured_response(value, field)


def test_anyof_has_standard_multiple_match_semantics_and_keeps_outer_constraints():
    schema = {"anyOf": [{"type": "integer"}, {"type": "number"}], "minimum": 2}
    validate_structured_response(2, schema)  # Matching both branches is allowed by anyOf.
    for value in [None, "2", 1]:
        with pytest.raises(StructuredResponseValidationError):
            validate_structured_response(value, schema)
    with pytest.raises(StructuredResponseValidationError):
        validate_structured_response({}, {"anyOf": []})


def test_nested_anyof_does_not_hide_unknown_fields_or_optional_object_validation():
    target = prompt_contract("interview_turn_understanding", _context()).response_schema["properties"]["clarification_target"]
    schema = {"type": "object", "required": ["target"], "additionalProperties": False,
              "properties": {"target": target}}
    validate_structured_response({"target": {"resolution": "resolved"}}, schema)
    for value in [{"target": {"source_ref": "C1", "secret": "extra"}},
                  {"target": {"resolution": "same_focus_unresolved", "source_ref": "C1"}, "unknown": True}, {}]:
        with pytest.raises(StructuredResponseValidationError):
            validate_structured_response(value, schema)
    legacy = {"type": ["object", "null"], "required": ["focus_quote"], "additionalProperties": False,
              "properties": {"focus_quote": {"type": "string", "minLength": 1}}}
    validate_structured_response(None, legacy)
    validate_structured_response({"focus_quote": "旧焦点"}, legacy)
    with pytest.raises(StructuredResponseValidationError):
        validate_structured_response({"focus_quote": " "}, legacy)


def test_new_clarification_revocation_rejects_old_occurrence_of_identical_completion():
    from app.core.prompt.semantic_turn import normalize_ambiguous_completion
    from test_semantic_recovery_066 import _canonical
    before, reply = "我记录位置。我说完了。蓝桥", "蓝桥是复核队列，再到红石。我说完了。"
    transcript = before + reply
    data = _canonical()
    data.update(ambiguities=["红石含义不明。"],
                clarification_target={"focus_quote": "红石", "evidence_quote": "蓝桥是复核队列，再到红石。"})
    receipt = _clarification_context(before, reply)
    with pytest.raises(SemanticContractError) as caught:
        normalize_ambiguous_completion(data, transcript, content_clarification=receipt,
            new_clarification_scope=True, completion_ranges=[(6, 11)])
    assert caught.value.reason_code == "semantic_current_speech_conflict"


@pytest.mark.parametrize("mutation,path", [
    (lambda c, d: c.update(request_text="不同旧请求"), "$.content_clarification.request_text"),
    (lambda c, d: c.update(reply_text="不同新回复"), "$.content_clarification.reply_text"),
    (lambda c, d: d["clarification_target"].update(focus_quote="不同焦点"), "$.clarification_target.focus_quote"),
    (lambda c, d: d["clarification_target"].update(evidence_quote=REPLY), "$.clarification_target.evidence_quote"),
])
def test_source_rejections_explain_safe_subfield_without_returning_text(mutation, path):
    context = _context()
    data = {"intent": "answer_unresolved", "turn_intent": "finish_answer",
            "clarification_target": {"focus_quote": "蓝桥", "evidence_quote": "蓝桥"}}
    receipt = context["interviewer_context"]["content_clarification"]
    mutation(receipt, data)
    with pytest.raises(SemanticContractError) as caught:
        validate_unresolved_completion(data, context["transcript"], receipt)
    assert caught.value.reason_code == "unresolved_clarification_source_invalid"
    assert caught.value.schema_path == path
    assert "蓝桥" not in str(caught.value) and REPLY not in str(caught.value)


def test_a_new_ambiguity_cannot_use_the_old_delivered_focus_to_finish():
    context = _context(reply="我改用红石。我已经说完了。")
    references = _references(context)
    _, _, wire = unresolved_response(technical=True)
    wire.update(evidence_ids=list(references["evidence"]),
                clarification_target={"resolution": "new_focus_unresolved", "evidence_id": "E3", "focus_quote": "红石"})
    wire["completion_basis"]["evidence_ids"] = ["E4"]
    canonical = resolve_understanding_references(wire, references)
    assert canonical["clarification_target"]["focus_quote"] == "红石"
    with pytest.raises(SemanticContractError) as caught:
        validate_unresolved_completion(canonical, context["transcript"],
                                       context["interviewer_context"]["content_clarification"])
    assert caught.value.schema_path == "$.clarification_target.focus_quote"


@pytest.mark.parametrize("target", [
    {"resolution": "resolved"},
    {"resolution": "new_focus_unresolved", "evidence_id": "E2", "focus_quote": "蓝桥"},
])
def test_joint_resolution_cannot_clear_actual_doubts_or_label_old_focus_as_new(target):
    context = _context(reply="蓝桥指复核队列，之后进入红石。我说完了。")
    references = _references(context)
    _, _, wire = unresolved_response(technical=True)
    wire.update(evidence_ids=list(references["evidence"]), ambiguities=["红石的处理含义不明确。"],
                clarification_target=target)
    wire["completion_basis"]["evidence_ids"] = ["E4"]
    before = deepcopy(wire)
    with pytest.raises(SemanticContractError) as caught:
        resolve_understanding_references(wire, references)
    assert caught.value.reason_code == "unresolved_clarification_source_invalid"
    assert caught.value.schema_path == "$.clarification_target.resolution"
    assert wire == before


@pytest.mark.anyio
@pytest.mark.parametrize("composite", [False, True])
@pytest.mark.parametrize("repaired", [False, True])
async def test_conflicting_old_source_is_rejected_and_only_a_corrected_new_target_can_continue(composite, repaired):
    context = _context(reply="蓝桥指复核队列，之后进入红石。我说完了。")
    references = _references(context)
    _, _, bad = unresolved_response(technical=True)
    bad.update(evidence_ids=list(references["evidence"]), ambiguities=["红石的处理含义不明确。"],
               clarification_target={"resolution": "new_focus_unresolved", "evidence_id": "E2", "focus_quote": "蓝桥"})
    bad["completion_basis"]["evidence_ids"] = ["E4"]
    bad["claims"].append({"claim": "蓝桥指复核队列", "evidence_id": "E3"})
    fixed = deepcopy(bad)
    fixed.update(intent="answer", turn_intent="clarify", confidence=.6, suggested_action="clarify",
                 completion_basis=None, clarification_target={"resolution": "new_focus_unresolved", "evidence_id": "E3", "focus_quote": "红石"})
    responses = [bad, fixed if repaired else bad]
    gateway = Gateway(*({"understanding": data, "followup": no_followup()} if composite else data for data in responses))
    utterance, turn, interview = _input()
    turn["allow_followup"] = composite
    utterance = utterance.model_copy(update={"text": context["transcript"]})
    interview.update(_semantic_first=True, _interviewer_context=context["interviewer_context"])
    understanding, followup = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert len(gateway.requests) == 2 and not followup["selected"]
    assert "unresolved_clarification_source_invalid" in "".join(message.content for message in gateway.requests[1].messages)
    assert all(request.execution_budget.max_provider_attempts == 1 and request.execution_budget.timeout_s == 18
               for request in gateway.requests)
    if repaired:
        assert understanding.problem is None and len(understanding.claims) == 2
        assert understanding.clarification_target.focus_quote == "红石"
        assert understanding.suggested_action == "clarify" and understanding.completion_basis is None
    else:
        assert understanding.problem is not None
        assert understanding.problem.reason_code == "unresolved_clarification_source_invalid"
        assert understanding.completion_basis is None


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
@pytest.mark.parametrize("valid", [True, False])
async def test_provider_transports_fixed_source_shape_and_gateway_keeps_strict_validation(mode, valid):
    import httpx
    from app.model_gateway import capabilities as cap
    from app.model_gateway.errors import ProviderError
    from app.model_gateway.gateway import ModelGateway
    from app.model_gateway.schemas import ChatJSONRequest
    from app.repositories.memory import InMemoryStore
    from test_dashscope_provider import make_provider, context as provider_context

    prompt_context = _context()
    contract = prompt_contract("interview_turn_understanding", prompt_context)
    references = _references(prompt_context)
    _, _, wire = unresolved_response(technical=True)
    wire.update(evidence_ids=list(references["evidence"]), clarification_target={"resolution": "same_focus_unresolved", "source_ref": "C1"})
    wire["completion_basis"]["evidence_ids"] = ["E3"]
    if not valid:
        wire["clarification_target"]["focus_quote"] = "擅改焦点"
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(wire)}}], "usage": {}})

    request = ChatJSONRequest(purpose="interview_turn_understanding", messages=contract.messages,
                              json_schema=contract.response_schema)
    response = await make_provider(handler).invoke(cap.LLM_CHAT_JSON, request,
        provider_context(cap.LLM_CHAT_JSON, "qwen3.7-plus", config={
            "base_url": "https://provider.invalid/v1", "structured_output_mode": mode}))
    if mode == "json_schema":
        target = seen["response_format"]["json_schema"]["schema"]["properties"]["clarification_target"]
        assert target["anyOf"][1]["properties"]["source_ref"]["enum"] == ["C1"]
    else:
        assert '"same_focus_unresolved"' in "".join(message["content"] for message in seen["messages"])
    gateway = ModelGateway(InMemoryStore())
    if valid:
        gateway._validate_response(cap.LLM_CHAT_JSON, request, response)
    else:
        with pytest.raises(ProviderError) as rejected:
            gateway._validate_response(cap.LLM_CHAT_JSON, request, response)
        assert rejected.value.code == "provider_schema_invalid"
