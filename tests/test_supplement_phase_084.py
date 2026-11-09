"""Private supplement context follows durable delivery, never grants consent."""

from copy import deepcopy
import importlib
import json

import pytest

from app.core.errors import ApiError
from app.core.prompt.contracts import prompt_contract
from app.core.time import utc_now
from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
from app.services.conversation_delivery import record_selection, update_delivery
from app.services.interviewer_supervisor.context import conversation_memory
from test_answer_reuse_078 import _delivered
from test_off_topic_semantics_068 import off_topic_response
from test_preparation_fences_081 import _chain
from test_prepared_decision_binding import _setup
from test_stable_preview_preparation import _preview


def _context(session, turn, **kwargs):
    helper = importlib.import_module("app.services.supplement_context")
    return helper.supplement_context(session, turn, **kwargs)


class _ContextGateway:
    def __init__(self):
        self.requests = []

    async def invoke(self, capability, request):
        self.requests.append(request)
        data = off_topic_response(request.metadata["transcript"], ["幂等键"],
                                  technical=True, relevant=True)["understanding"]
        return ChatJSONResponse(data=deepcopy(data), usage=Usage(), provider=ProviderMeta(
            provider_id="synthetic", model="supplement_phase_084", request_id="synthetic_084", latency_ms=1))


def _request_context(request):
    message = next(m.content for m in request.messages if m.content.startswith("面试官上下文："))
    return json.loads(message.removeprefix("面试官上下文："))


@pytest.mark.anyio
@pytest.mark.parametrize("preview", [False, True])
async def test_delivered_phase_reaches_real_preparation_without_false_unasked_or_confirmation(tmp_path, monkeypatch, preview):
    h = _chain(tmp_path, monkeypatch)
    service = h.stream.interviews
    _delivered(service)
    gateway = service.conversation.gateway = _ContextGateway()
    final = _preview(h.final) if preview else h.final
    prepared = await h.chain.prepare_decision(final, semantic_first=True)
    assert prepared.understanding.problem is None
    context = _request_context(gateway.requests[-1])
    assert context["supplement_context"]["stage"] == "awaiting_reply"
    assert context["supplement_context"]["question_act_id"] == "supplement_078"
    messages = "\n".join(m.content for m in gateway.requests[-1].messages)
    assert "服务端尚未向候选人询问是否补充" not in messages
    assert "已送达不等于已确认" in messages
    assert not prepared.completion_confirmed
    assert not service.get_interview("iv_binding")["answers"]


@pytest.mark.parametrize("status", [None, "selected", "playing", "interrupted", "failed", "delivered"])
def test_phase_requires_actual_current_turn_delivery_not_runtime_status(status):
    service, _ = _setup()
    if status:
        _delivered(service, status=status)
    session = service.get_interview("iv_binding")
    session["agent_runtime"] = {"supplement_confirmation": {"status": "awaiting_reply", "confirmed": True}}
    context = _context(session, session["turns"][0])
    assert context["stage"] == ("awaiting_reply" if status == "delivered" else "not_delivered")
    assert "question" in context if status == "delivered" else "question" not in context
    assert _context(session, session["turns"][0], completion_confirmed=True)["stage"] == "confirmed"
    assert _context(session, session["turns"][0], completion_confirmed=1)["stage"] != "confirmed"


@pytest.mark.parametrize("change", ["turn", "version", "text", "performance", "receipt", "new_selection"])
def test_stale_or_incomplete_delivery_cannot_claim_awaiting_reply(change):
    service, _ = _setup()
    _delivered(service)
    if change == "new_selection":
        _delivered(service, status="selected", act_id="new_supplement_084")
    session = service.get_interview("iv_binding")
    turn = session["turns"][0]
    act = turn["conversation_acts"][0]
    if change == "turn":
        session["current_turn_id"] = "other_turn"
    elif change == "version":
        act["prompt_version"] = "synthetic_old_version"
    elif change == "text":
        act["text"] = "另一个尚未播放的合成问题。"
    elif change == "performance":
        act["selection_receipts"][0]["delivery"].pop("performance_id")
    elif change == "receipt":
        act["selection_receipts"] = []
    assert _context(session, turn)["stage"] == "not_delivered"


def test_internal_transcription_transition_keeps_delivery_facts_for_commit_recheck():
    service, _ = _setup()
    _delivered(service)
    session = service.get_interview("iv_binding")
    turn = session["turns"][0]
    before = _context(session, turn)
    turn["status"] = "transcribing"
    assert _context(session, turn) == before


def _bury_supplement(service):
    with service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        turn = session["turns"][0]
        for index in range(5):
            act = {"act_id": "later_084_%s" % index, "turn_id": turn["id"],
                   "act_type": "conversation_acknowledgement", "text": "合成交流。"}
            turn["conversation_acts"].append(act)
            event = {"event_id": "event_later_084_%s" % index, "turn_id": turn["id"],
                     "session_sequence": index + 10, "occurred_at": utc_now(),
                     "type": "conversation.act.selected",
                     "payload": {"act_id": act["act_id"], "text": act["text"], "approved": True}}
            session.setdefault("agent_events", []).append(event)
            record_selection(session, event)
        tx.interview_sessions.update(session, expected_version=session["version"])


@pytest.mark.anyio
async def test_delivery_outside_recent_dialogue_invalidates_prepared_context(tmp_path, monkeypatch):
    h = _chain(tmp_path, monkeypatch)
    service = h.stream.interviews
    _delivered(service, status="selected")
    _bury_supplement(service)
    prepared = await h.chain.prepare_decision(h.final, semantic_first=True)
    before = service.get_interview("iv_binding")
    with service.persistence.transaction("org_default") as tx:
        current = tx.interview_sessions.get("iv_binding")
        assert update_delivery(current, event_id="event_supplement_078", performance_id="performance_078",
                               status="delivered", updated_at=utc_now())
        tx.interview_sessions.update(current, expected_version=current["version"])
    after = service.get_interview("iv_binding")
    assert conversation_memory(before) == conversation_memory(after)
    with pytest.raises(ApiError) as error:
        h.chain.assert_preparation_current(prepared, h.final, semantic_first=True)
    assert error.value.code == "TURN_DECISION_STALE"
    assert _context(after, after["turns"][0])["stage"] == "awaiting_reply"
    assert not after["answers"]


@pytest.mark.parametrize("purpose", ["interview_turn_understanding", "interview_turn_decision"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_phase_instruction_keeps_confirmation_authority_and_completion_evidence(purpose, confirmed):
    contract = prompt_contract(purpose, {"semantic_first": True, "completion_confirmed": confirmed,
        "transcript": "我使用幂等键。", "capability_points": ["幂等键"],
        "interviewer_context": {"supplement_context": {"version": "supplement_context.v1",
                                                       "stage": "awaiting_reply"}}})
    text = "\n".join(m.content for m in contract.messages)
    assert "已送达不等于已确认" in text
    assert "服务端尚未向候选人询问是否补充" not in text
    assert ("真实结束确认" in text) is confirmed
    schema = contract.response_schema["properties"].get("understanding", contract.response_schema)
    assert "completion_basis" in schema["required"]


@pytest.mark.anyio
@pytest.mark.parametrize("purpose", ["interview_turn_understanding", "interview_turn_decision"])
@pytest.mark.parametrize("optional,exit_context,confirmed", [
    (optional, exit_context, confirmed)
    for optional in [False, True] for exit_context in [False, True] for confirmed in [False, True]
])
async def test_all_phase_contract_versions_keep_mock_schema_and_invocation_audit(purpose, optional, exit_context, confirmed):
    from app.core.prompt.validation import validate_structured_response
    from app.model_gateway.gateway import ModelGateway
    from app.model_gateway.schemas import ChatJSONRequest
    from app.providers.mock.provider import MockProvider
    from app.repositories.memory import InMemoryStore
    from test_dashscope_provider import context as provider_context

    dialogue = {"supplement_context": {"version": "supplement_context.v1", "stage": "not_delivered"}}
    if optional:
        dialogue.update(skill={}, company_context={"available": False})
    if exit_context:
        dialogue["exit_confirmation"] = {"decision": "confirm"}
    source = {"transcript": "我使用幂等键。", "capability_points": ["幂等键"],
              "semantic_first": True, "completion_confirmed": confirmed, "interviewer_context": dialogue}
    contract = prompt_contract(purpose, source)
    number = (118 if purpose == "interview_turn_understanding" else 149) + 4 * exit_context + 2 * optional + confirmed
    assert contract.version == "%s.v%s" % (purpose, number)
    request = ChatJSONRequest(purpose="interview_turn_understanding", messages=contract.messages,
        json_schema=contract.response_schema, metadata={"prompt_version": contract.version,
            "transcript": source["transcript"], "capability_points": source["capability_points"]})
    response = await MockProvider().invoke("llm.chat_json", request, provider_context("llm.chat_json", "mock-json"))
    validate_structured_response(response.data, contract.response_schema)
    gateway = ModelGateway(InMemoryStore())
    gateway._log_invocation(invocation_id="synthetic_phase_084", organization_id="org_default",
        capability="llm.chat_json", purpose=request.purpose, route={"id": "synthetic_route"},
        provider_connection_id="synthetic_provider", model_configuration_id="synthetic_model",
        provider_id="mock", model="mock-json", status="success", latency_ms=1,
        attempt=1, fallback_index=0, request_hash="synthetic", response=response, prompt_version=contract.version)
    with gateway.persistence.transaction("org_default") as tx:
        assert tx.model_invocations.list()[-1]["prompt_version"] == contract.version
