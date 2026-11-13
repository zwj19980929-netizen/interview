"""Synthetic evidence only: parent/child scoring preserves scope and provenance."""
from copy import deepcopy

import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.evaluation_evidence import evaluation_evidence_sources
from app.core.prompt.validation import validate_structured_response
from app.model_gateway.errors import ProviderError
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.providers.mock.provider import evaluate_answer as mock_score
from app.services.evaluation import EvaluationService
from app.services.interviews import InterviewService
from test_followup_review_report_055 import _scored_chain, _persist


def fixture():
    session = _scored_chain()
    session["answers"][0]["final_transcript"] = "我使用幂等键防止重复执行。"
    session["answers"][1]["final_transcript"] = "是的，Outbox 和业务数据在同一事务提交。"
    session["answers"][2]["final_transcript"] = "幂等键还会设置唯一约束。"
    for turn in session["turns"]:
        if turn["id"] == "child_one":
            turn["question_snapshot"]["key_points"] = [turn["question_snapshot"]["key_points"][1]]
            turn["question_spoken_text"] = "Outbox 是否和业务数据一起提交？"
        elif turn["id"] == "child_two":
            turn["question_snapshot"]["key_points"] = [turn["question_snapshot"]["key_points"][0]]
    store = _persist(session)
    merged = InterviewService(store)._merged_authoritative_answer(session, root_answer_id=session["answers"][0]["id"],
                evidence_answer_ids=[a["id"] for a in session["answers"]])
    return session, store, merged


class Gateway:
    def __init__(self, mutate=None):
        self.requests = []
        self.mutate = mutate

    async def invoke(self, capability, request):
        self.requests.append(request)
        data = mock_score(request.metadata)
        if self.mutate:
            self.mutate(data)
        validate_structured_response(data, request.json_schema)
        return ChatJSONResponse(data=data, usage=Usage(), provider=ProviderMeta(
            provider_id="synthetic", model="synthetic", request_id="synthetic", latency_ms=0))


@pytest.mark.anyio
async def test_group_prompt_receives_actual_questions_scopes_and_source_bound_quotes():
    session, store, merged = fixture()
    gateway = Gateway()
    result = await EvaluationService(store, gateway=gateway).evaluate_answer(merged, session["turns"][0]["question_snapshot"])
    request = gateway.requests[0]
    assert request.metadata["prompt_version"] == "answer_evaluation.v8"
    chain = request.metadata["evidence_chain"]
    assert chain == merged["evidence_chain"]
    assert chain[1]["question_text"] == "Outbox 是否和业务数据一起提交？"
    assert chain[1]["target_point_ids"] == ["kp_outbox"]
    assert any("Outbox 是否和业务数据一起提交？" in message.content for message in request.messages)
    source = next(s for s in result["evidence_sources"] if s["key_point_id"] == "kp_outbox")
    assert source == {"key_point_id": "kp_outbox", "quote": session["answers"][1]["final_transcript"],
                      "answer_id": "answer_child_one", "turn_id": "child_one", "transcript_revision": 1}


@pytest.mark.anyio
@pytest.mark.parametrize("tamper", ["missing", "text", "question", "scope", "root", "revision", "extra", "duplicate", "merged_text"])
async def test_forged_or_stale_chain_is_rejected_before_model_call(tamper):
    session, store, merged = fixture()
    child = merged["evidence_chain"][1]
    if tamper == "missing":
        merged.pop("evidence_chain")
    elif tamper == "text":
        child["answer_text"] = "Injected answer"
    elif tamper == "question":
        child["question_text"] = "Injected question"
    elif tamper == "scope":
        child["target_point_ids"] = ["kp_idempotency"]
    elif tamper == "root":
        child["root_turn_id"] = "another_root"
    elif tamper == "revision":
        child["transcript_revision"] = 2
    elif tamper == "extra":
        child["unexpected"] = "ignored?"
    elif tamper == "merged_text":
        merged["final_transcript"] += "未经保存的额外内容"
    else:
        merged["evidence_chain"].append(deepcopy(child))
    gateway = Gateway()
    with pytest.raises(ProviderError) as exc:
        await EvaluationService(store, gateway=gateway).evaluate_answer(merged, session["turns"][0]["question_snapshot"])
    assert exc.value.code == "evaluation_evidence_invalid"
    assert gateway.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize("quote", ["不存在的技术回答", "Outbox 是否和业务数据一起提交？", "【主回答】", "幂等键还会设置唯一约束。"])
async def test_covered_quote_must_be_in_answer_with_matching_scope(quote):
    session, store, merged = fixture()
    def corrupt(data):
        data["covered_key_points"] = [{"key_point_id": "kp_outbox", "evidence": quote}]
        data["evidence"] = [quote]
    gateway = Gateway(corrupt)
    with pytest.raises(ProviderError) as exc:
        await EvaluationService(store, gateway=gateway).evaluate_answer(merged, session["turns"][0]["question_snapshot"])
    assert exc.value.code == "evaluation_evidence_invalid"
    assert quote not in str(exc.value)


def test_group_contract_restricts_covered_and_missing_to_frozen_root_scope():
    session, _, merged = fixture()
    contract = prompt_contract("answer_evaluation", {"question_text": "Q", "standard_answer": "A",
        "answer_text": merged["final_transcript"], "key_points": session["turns"][0]["question_snapshot"]["key_points"],
        "evidence_chain": merged["evidence_chain"]})
    assert contract.version == "answer_evaluation.v8"
    for field in ("covered_key_points", "missing_key_points"):
        assert contract.response_schema["properties"][field]["items"]["properties"]["key_point_id"]["enum"] == ["kp_idempotency", "kp_outbox"]


def test_short_duplicate_quotes_preserve_all_possible_sources():
    _, _, merged = fixture()
    chain = merged["evidence_chain"]
    chain[0]["answer_text"] = chain[1]["answer_text"] = "是的。"
    data = {"covered_key_points": [{"key_point_id": "kp_outbox", "evidence": "是的。"}], "evidence": ["是的。"]}
    sources = evaluation_evidence_sources(data, chain, ["kp_outbox"])
    assert [s["answer_id"] for s in sources] == ["answer_turn_root", "answer_child_one"]


@pytest.mark.anyio
@pytest.mark.parametrize("declined_root", [False, True])
async def test_declined_answer_is_absent_from_structured_prompt_and_source_quotes(declined_root):
    from test_declined_answer_evaluation import session_for, persist, DECLINED, question
    from app.repositories.memory import InMemoryStore
    session = session_for(["answer_declined", "answer"] if declined_root else ["answer", "answer_declined"], followups=True)
    store = InMemoryStore()
    persist(store, session)
    merged = InterviewService(store)._merged_authoritative_answer(session, root_answer_id="answer_0", evidence_answer_ids=["answer_0", "answer_1"])
    gateway = Gateway()
    result = await EvaluationService(store, gateway=gateway).evaluate_answer(merged, question(0))
    assert all(DECLINED not in message.content for message in gateway.requests[0].messages)
    assert len(gateway.requests[0].metadata["evidence_chain"]) == 1
    assert {s["answer_id"] for s in result["evidence_sources"]} == {"answer_1" if declined_root else "answer_0"}


@pytest.mark.anyio
async def test_company_question_exclusions_also_apply_to_structured_chain():
    from test_company_questions import exchange, QUESTION
    session, _, _ = fixture()
    child = session["answers"][1]
    original = child["final_transcript"] + QUESTION
    child["final_transcript"] = original
    next(t for t in session["turns"] if t["id"] == child["turn_id"])["company_question_exchanges"] = [exchange(original)]
    store = _persist(session)
    merged = InterviewService(store)._merged_authoritative_answer(session, root_answer_id=session["answers"][0]["id"],
                evidence_answer_ids=[a["id"] for a in session["answers"]])
    gateway = Gateway()
    result = await EvaluationService(store, gateway=gateway).evaluate_answer(merged, session["turns"][0]["question_snapshot"])
    assert all(QUESTION not in message.content for message in gateway.requests[0].messages)
    assert QUESTION not in str(gateway.requests[0].metadata["evidence_chain"])
    assert all(QUESTION not in source["quote"] for source in result["evidence_sources"])
    assert child["final_transcript"] == original


@pytest.mark.anyio
async def test_unbacked_human_correction_cannot_enter_group_scoring():
    session, _, _ = fixture()
    session["answers"][1]["transcript_source"] = "human_correction"
    store = _persist(session)
    merged = InterviewService(store)._merged_authoritative_answer(session, root_answer_id=session["answers"][0]["id"],
                evidence_answer_ids=[a["id"] for a in session["answers"]])
    gateway = Gateway()
    with pytest.raises(ProviderError) as exc:
        await EvaluationService(store, gateway=gateway).evaluate_answer(merged, session["turns"][0]["question_snapshot"])
    assert exc.value.code == "evaluation_evidence_invalid" and not gateway.requests
