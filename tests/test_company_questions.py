"""Company facts remain extracted, auditable and outside technical scoring."""
import asyncio
from copy import deepcopy

import pytest

from app.core.errors import ApiError
from app.core.prompt.company_questions import company_reply_contract
from app.core.prompt.validation import validate_structured_response, StructuredResponseValidationError
from app.core.prompt.understanding_references import understanding_references
from app.services.company_questions import prepare_company_reply, scoring_projection
from test_prepared_turn_decision import Gateway, _input, _service, _data

QUESTION = "请问你们公司的主要业务是什么？"
FACT = "公司为制造企业提供离线数据同步平台。"
TECH = "我用幂等键记录确认位置。"


def company_understanding(text, points, *, mixed=False, completed=False):
    refs = understanding_references(text, points)
    eid = next(key for key, value in refs["evidence"].items() if QUESTION in value)
    technical_id = next((key for key, value in refs["evidence"].items() if TECH in value), None)
    data = _data()["understanding"]
    data.update(intent="answer" if mixed else "company_question", answer_content="partial" if mixed else "none",
        turn_intent="finish_topic" if completed else "ask_company", completion_basis=None,
        company_question=None if completed else {"evidence_id": eid, "quote": QUESTION},
        followup_allowed=False, suggested_action="next" if completed else "respond_company",
        answer_summary="使用幂等键。" if mixed else "询问公司业务。",
        claims=[{"claim": "用幂等键记录确认位置", "evidence_id": technical_id}] if mixed else [],
        evidence_ids=[technical_id] if mixed else [eid],
        covered_point_ids=[], missing_point_ids=list(refs["capabilities"]))
    if completed:
        end = next(key for key, value in refs["evidence"].items() if "下一题" in value)
        data["completion_basis"] = {"source": "explicit_server_intent", "evidence_ids": [end]}
        if not mixed:
            data.update(intent="answer_declined", answer_summary="本题不再作答。", evidence_ids=[end])
    return data


@pytest.mark.anyio
@pytest.mark.parametrize("mixed", [False, True])
async def test_company_question_semantics_select_exact_span_and_never_prepare_probe(mixed):
    utterance, turn, session = _input()
    text = (TECH if mixed else "") + QUESTION
    utterance = utterance.model_copy(update={"text": text})
    session.update(_semantic_first=True, _interviewer_context={"skill": {}, "company_context": {"available": True}})
    data = _data()
    data["understanding"] = company_understanding(text, ["幂等键", "确认序号"], mixed=mixed)
    service = _service(Gateway(data))
    understood, followup = await service.prepare_decision(utterance, turn, session)
    assert understood.problem is None
    assert understood.turn_intent == "ask_company" and understood.suggested_action == "respond_company"
    assert text[understood.company_question.start:understood.company_question.end] == QUESTION
    assert understood.company_question.start == (len(TECH) if mixed else 0)
    assert bool(understood.claims) is mixed
    assert not followup["selected"] and not service.can_complete_without_confirmation(understood, text)
    assert understood.prompt_version == "interview_turn_decision.v17"


@pytest.mark.anyio
@pytest.mark.parametrize("change", [
    {"company_question": {"evidence_id": "E999", "quote": QUESTION}},
    {"company_question": {"evidence_id": "E1", "quote": "编造的问题"}},
    {"company_question": None}, {"followup_allowed": True}, {"suggested_action": "next"},
    {"confidence": .7}, {"company_question": {"evidence_id": "E1", "quote": QUESTION, "company": "invented"}},
])
async def test_invalid_question_cannot_authorize_reply_or_answer(change):
    utterance, turn, session = _input()
    utterance = utterance.model_copy(update={"text": QUESTION})
    session.update(_semantic_first=True, _interviewer_context={"company_context": {"available": False}})
    data = _data()
    data["understanding"] = company_understanding(QUESTION, ["幂等键", "确认序号"])
    data["understanding"].update(deepcopy(change))
    understood, followup = await _service(Gateway(data, data)).prepare_decision(utterance, turn, session)
    assert understood.problem is not None and not followup["selected"]


@pytest.mark.anyio
@pytest.mark.parametrize("overbroad", [False, True])
async def test_same_sentence_question_span_must_preserve_the_technical_claim(overbroad):
    utterance, turn, session = _input()
    text = TECH.rstrip("。") + "，" + QUESTION
    utterance = utterance.model_copy(update={"text": text})
    session.update(_semantic_first=True, _interviewer_context={"company_context": {"available": True}})
    data = _data()
    data["understanding"] = company_understanding(TECH + QUESTION, ["幂等键", "确认序号"], mixed=True)
    data["understanding"]["company_question"] = {"evidence_id": "E1", "quote": text if overbroad else QUESTION}
    result, _ = await _service(Gateway(data, data)).prepare_decision(utterance, turn, session)
    if overbroad:
        assert result.problem is not None and result.company_question is None
    else:
        assert result.problem is None and result.company_question.quote == QUESTION
        assert result.claims[0].claim == "用幂等键记录确认位置"


@pytest.mark.anyio
async def test_no_company_material_requires_no_model_and_never_uses_skill_resources():
    gateway = Gateway()
    result = await prepare_company_reply(gateway, organization_id="org_default", company_context=None, question=QUESTION)
    assert result["status"] == "not_found" and not result["citations"] and not gateway.requests
    assert "无法确认" in result["text"]


@pytest.mark.anyio
async def test_reply_extracts_only_exact_allowed_source_quotes():
    gateway = Gateway({"status": "supported", "citations": [{"reference_id": "company:context", "quote": FACT}]})
    result = await prepare_company_reply(gateway, organization_id="org_default", company_context=FACT, question=QUESTION)
    assert result["status"] == "supported" and FACT in result["text"]
    request = gateway.requests[0]
    assert request.metadata == {"prompt_version": "company_question_reply.v1"}
    assert request.execution_budget.timeout_s == 8 and request.execution_budget.max_provider_retries == 0
    assert "standard_answer" not in str(request.messages) and "Skill" not in request.messages[-1].content


@pytest.mark.anyio
@pytest.mark.parametrize("data", [
    {"status": "supported", "citations": []},
    {"status": "not_found", "citations": [{"reference_id": "company:context", "quote": FACT}]},
    {"status": "supported", "citations": [{"reference_id": "skill:resource", "quote": FACT}]},
    {"status": "supported", "citations": [{"reference_id": "company:context", "quote": "公司保证不会加班。"}]},
    {"status": "supported", "citations": [{"reference_id": "company:context", "quote": FACT}], "answer": "自由生成"},
    {"status": "supported", "citations": [{"reference_id": "company:context", "quote": " "}]},
])
async def test_invalid_company_claims_use_fixed_unavailable_response(data):
    result = await prepare_company_reply(Gateway(data), organization_id="org_default", company_context=FACT, question=QUESTION)
    assert result["status"] == "unavailable" and result["citations"] == []
    assert FACT not in result["text"] and "加班" not in result["text"]


@pytest.mark.anyio
async def test_company_timeout_falls_back_but_cancellation_propagates():
    result = await prepare_company_reply(Gateway(error=asyncio.TimeoutError()), organization_id="org_default", company_context=FACT, question=QUESTION)
    assert result["status"] == "unavailable"
    with pytest.raises(asyncio.CancelledError):
        await prepare_company_reply(Gateway(error=asyncio.CancelledError()), organization_id="org_default", company_context=FACT, question=QUESTION)


def exchange(text, quote=QUESTION):
    start = text.index(quote)
    return {"transcript_prefix": text, "question_span": {"start": start, "end": start + len(quote), "quote": quote},
            "media_checkpoint": {"stream_id": "stream", "capture_revision": 1}}


def test_scoring_excludes_only_question_ranges_without_losing_same_sentence_technical_evidence():
    prefix = TECH.rstrip("。") + "，" + QUESTION
    turn = {"company_question_exchanges": [exchange(prefix)]}
    final = prefix + "之后我会核验恢复位置。下一题吧。"
    projected, excluded = scoring_projection(final, turn)
    assert projected == TECH.rstrip("。") + "，之后我会核验恢复位置。下一题吧。"
    assert len(excluded) == 1 and final == prefix + "之后我会核验恢复位置。下一题吧。"
    with pytest.raises(ApiError, match="转写发生变化"):
        scoring_projection("前缀被识别器更改。" + final, turn)
    assert scoring_projection(final, {}) == (final, [])


@pytest.mark.anyio
async def test_company_reply_mock_dispatch_and_audit_preserve_version():
    from app.model_gateway.gateway import ModelGateway
    from app.repositories.memory import InMemoryStore
    store = InMemoryStore()
    result = await prepare_company_reply(ModelGateway(store), organization_id="org_default", company_context=FACT, question=QUESTION)
    assert result["status"] == "not_found"
    assert store.model_invocations[-1]["prompt_version"] == "company_question_reply.v1"


def test_company_reply_schema_has_no_open_answer_and_enforces_array_bounds():
    schema = company_reply_contract(QUESTION, FACT).response_schema
    assert set(schema["properties"]) == {"status", "citations"}
    with pytest.raises(StructuredResponseValidationError):
        validate_structured_response({"status": "supported", "citations": [
            {"reference_id": "company:context", "quote": str(n)} for n in range(4)]}, schema)


@pytest.mark.anyio
async def test_missed_technical_claim_during_question_does_not_replace_original_scoring_evidence():
    utterance, turn, session = _input()
    text = TECH + QUESTION
    utterance = utterance.model_copy(update={"text": text})
    session.update(_semantic_first=True, _interviewer_context={"company_context": {"available": False}})
    data = _data()
    # A model can miss a technical claim while still identifying the exact
    # company question. Its summary never replaces the authoritative text.
    data["understanding"] = company_understanding(text, ["幂等键", "确认序号"], mixed=False)
    understanding, _ = await _service(Gateway(data)).prepare_decision(utterance, turn, session)
    assert understanding.problem is None and not understanding.claims
    turn["company_question_exchanges"] = [exchange(text)]
    assert scoring_projection(text, turn)[0] == TECH


@pytest.mark.anyio
async def test_duplicate_already_delivered_reply_returns_to_listening_without_a_new_performance():
    from app.services.spoken_supplement import SpokenSupplementConfirmation
    from test_answer_endpoint import _endpoint, _final
    endpoint, _, _, commits = _endpoint()
    calls = []
    async def already_delivered(prepared, final, guard):
        guard()
        calls.append(True)
        return True
    confirmation = SpokenSupplementConfirmation(speak=None, respond_company=already_delivered)
    endpoint.confirmation = confirmation
    endpoint._proposal_revision = endpoint.revision
    await confirmation.company_answer(endpoint, object(), _final(QUESTION))
    assert confirmation.phase == "listening" and endpoint.capture.is_open and not commits
    await confirmation.company_answer(endpoint, object(), _final(QUESTION))
    assert calls == [True]
    await endpoint.close()
