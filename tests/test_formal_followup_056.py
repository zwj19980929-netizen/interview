"""Finishing an answer preserves bounded, evidence-bound interviewer probes."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.core.prompt.contracts import prompt_contract, SUPPLEMENT_SPEECH, SUPPLEMENT_SPEECH_VERSION
from app.domain.adaptive_interview import source_topic_closed
from app.model_gateway.errors import ProviderError
from test_prepared_turn_decision import Gateway, _data, _input, _service


def completed_answer(*, intent="finish_answer", selected=True):
    data = _data()
    data["understanding"].update(answer_content="technical", turn_intent=intent,
        covered_point_ids=["P1", "P2"], missing_point_ids=[], followup_allowed=intent == "finish_answer",
        suggested_action="next", completion_basis={"source": "explicit_server_intent", "evidence_ids": ["E2"]})
    data["followup"].update(selected=selected,
        question_text="你刚才说会记录恢复位置，这个位置是在什么时机更新的？" if selected else "",
        evidence_id="E1" if selected else "", target_point_ids=["P2"] if selected else [],
        rationale="已经说明使用恢复位置，但尚未说明位置更新和处理完成的先后关系。" if selected else "")
    return data


@pytest.mark.anyio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_answer_completion_keeps_covered_claim_detail_probe_available(confirmed):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "我讲完了。"})
    interview.update(_semantic_first=True, _answer_completion_confirmed=confirmed)
    gateway = Gateway(completed_answer())
    service = _service(gateway)
    understanding, followup = await service.prepare_decision(utterance, turn, interview)
    assert understanding.problem is None
    assert understanding.turn_intent == "finish_answer" and understanding.followup_allowed
    assert service.can_complete_without_confirmation(understanding, utterance.text)
    assert not source_topic_closed({"current_understanding": understanding.model_dump()})
    assert understanding.missing_capability_points == []
    assert followup["selected"] and followup["target_key_points"] == ["确认序号"]
    assert followup["reason"] == "claim_detail_verification"
    assert followup["parent_turn_id"] == followup["root_turn_id"] == turn["id"]
    assert followup["conversation_act"]["act_type"] == "followup"
    assert len(gateway.requests) == 1


@pytest.mark.anyio
async def test_model_can_skip_probe_even_after_answer_completion():
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "我讲完了。"})
    interview.update(_semantic_first=True, _answer_completion_confirmed=True)
    understanding, followup = await _service(Gateway(completed_answer(selected=False))).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.turn_intent == "finish_answer"
    assert not followup["selected"] and followup["reason"] == "model_declined"


@pytest.mark.anyio
async def test_explicit_topic_boundary_still_blocks_covered_detail_probe():
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "这部分不用再问，下一题吧。"})
    interview.update(_semantic_first=True, _answer_completion_confirmed=True)
    understanding, followup = await _service(Gateway(completed_answer(intent="finish_topic"))).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and source_topic_closed({"current_understanding": understanding.model_dump()})
    assert not followup["selected"] and followup["reason"] == "candidate_topic_boundary"


@pytest.mark.anyio
async def test_previously_probed_covered_point_remains_ineligible():
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "我讲完了。"})
    interview.update(_semantic_first=True, _answer_completion_confirmed=True)
    interview["turns"].extend([{"id": "another_root", "is_followup": False},
        {"id": "prior_child", "is_followup": True, "root_turn_id": turn["id"], "target_key_points": ["确认序号"]}])
    understanding, followup = await _service(Gateway(completed_answer())).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None
    assert not followup["selected"] and followup["reason"] == "followup_proposal_rejected"


@pytest.mark.anyio
async def test_optional_detail_probe_failure_cannot_force_generic_followup():
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "我讲完了。"})
    interview.update(_semantic_first=True)
    service = _service(Gateway(completed_answer(selected=False)))
    understanding, _ = await service.prepare_decision(utterance, turn, interview)
    service.gateway = Gateway(error=ProviderError("provider_unavailable", "synthetic", retryable=True))
    followup = await service.select_followup(interview, turn, utterance, understanding)
    assert not followup["selected"] and followup["reason"] == "optional_detail_probe_unavailable"


@pytest.mark.anyio
async def test_provider_fallback_can_only_probe_still_missing_points():
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "我讲完了。"})
    interview["_semantic_first"] = True
    data = completed_answer(selected=False)
    data["understanding"].update(covered_point_ids=["P2"], missing_point_ids=["P1"])
    service = _service(Gateway(data))
    understanding, _ = await service.prepare_decision(utterance, turn, interview)
    service.gateway = Gateway(error=ProviderError("provider_unavailable", "synthetic", retryable=True))
    followup = await service.select_followup(interview, turn, utterance, understanding)
    assert followup["selected"] and followup["target_key_points"] == ["幂等键"]
    assert followup["evidence_quotes"] == [utterance.text.removesuffix("我讲完了。")]
    assert "确认序号" not in followup["conversation_act"]["target_capability_points"]


@pytest.mark.anyio
@pytest.mark.parametrize("budget,reason", [
    ("disabled", "followup_disabled"), ("depth", "depth_budget_exhausted"),
    ("root", "root_budget_exhausted"), ("session", "session_budget_exhausted"),
    ("time", "time_budget_exhausted"),
])
async def test_finishing_answer_does_not_override_followup_budgets(budget, reason):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "我讲完了。"})
    interview["_semantic_first"] = True
    if budget == "disabled":
        turn["allow_followup"] = False
    elif budget == "depth":
        turn["followup_depth"] = 2
    elif budget == "root":
        interview["turns"].extend([{"id": "other_root", "is_followup": False},
            {"id": "third_root", "is_followup": False},
            {"id": "prior_one", "is_followup": True, "root_turn_id": turn["id"]},
            {"id": "prior_two", "is_followup": True, "root_turn_id": turn["id"]}])
    elif budget == "session":
        interview["turns"].append({"id": "prior_one", "is_followup": True, "root_turn_id": turn["id"]})
    else:
        interview["scheduled_end_at"] = datetime.now(timezone.utc).isoformat()
    gateway = Gateway(completed_answer()["understanding"])
    service = _service(gateway)
    understanding, followup = await service.prepare_decision(utterance, turn, interview)
    assert understanding.problem is None
    assert service.can_complete_without_confirmation(understanding, utterance.text)
    assert not followup["selected"] and followup["reason"] == reason
    assert len(gateway.requests) == 1
    assert "understanding" not in gateway.requests[0].json_schema["properties"]


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", [
    lambda data: data["understanding"].update(completion_basis=None),
    lambda data: data["understanding"].update(confidence=.74),
    lambda data: data["understanding"].update(answer_content="none", claims=[], covered_point_ids=[], missing_point_ids=["P1", "P2"]),
])
async def test_invalid_finish_answer_neither_commits_nor_creates_a_followup(mutation):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "我讲完了。"})
    interview["_semantic_first"] = True
    data = completed_answer()
    mutation(data)
    gateway = Gateway(deepcopy(data), deepcopy(data))
    service = _service(gateway)
    understanding, followup = await service.prepare_decision(utterance, turn, interview)
    assert understanding.problem is not None
    assert not service.can_complete_without_confirmation(understanding, utterance.text)
    assert not followup["selected"] and "conversation_act" not in followup
    assert len(gateway.requests) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("allow_followup", [True, False])
async def test_derived_followup_permission_still_requires_source_evidence_and_real_budget(allow_followup):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "我讲完了。"})
    interview["_semantic_first"] = True
    turn["allow_followup"] = allow_followup
    data = completed_answer()
    data["understanding"]["followup_allowed"] = False
    gateway = Gateway(data if allow_followup else data["understanding"])
    service = _service(gateway)
    understanding, followup = await service.prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.followup_allowed
    assert understanding.claims and understanding.completion_basis.evidence_quotes == ["我讲完了。"]
    assert service.can_complete_without_confirmation(understanding, utterance.text)
    assert followup["selected"] is allow_followup and len(gateway.requests) == 1
    if not allow_followup:
        assert followup["reason"] == "followup_disabled" and "conversation_act" not in followup


@pytest.mark.anyio
@pytest.mark.parametrize("rejection", ["empty_rationale", "blank_rationale", "non_claim_evidence"])
@pytest.mark.parametrize("mode", ["composite", "separate"])
async def test_covered_detail_probe_requires_explicit_reason_and_exact_claim_source(rejection, mode):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "我讲完了。"})
    interview["_semantic_first"] = True
    data = completed_answer()
    # The completion sentence is declared evidence, but is not a technical claim.
    data["understanding"]["evidence_ids"] = ["E1", "E2"]
    if rejection == "empty_rationale":
        data["followup"]["rationale"] = ""
    elif rejection == "blank_rationale":
        data["followup"]["rationale"] = "   "
    else:
        data["followup"]["evidence_id"] = "E2"
    gateway = Gateway(data)
    service = _service(gateway)
    understanding, followup = await service.prepare_decision(utterance, turn, interview)
    assert understanding.problem is None
    assert service.can_complete_without_confirmation(understanding, utterance.text)
    assert not followup["selected"] and followup["reason"] == "followup_proposal_rejected"
    assert len(gateway.requests) == 1
    if mode == "separate":
        proposal = deepcopy(data["followup"])
        proposal["evidence_quote"] = "我讲完了。" if proposal.pop("evidence_id") == "E2" else utterance.text.removesuffix("我讲完了。")
        proposal["target_capability_points"] = ["确认序号"]
        proposal.pop("target_point_ids")
        service.gateway = Gateway(proposal)
        followup = await service.select_followup(interview, turn, utterance, understanding)
        assert not followup["selected"] and followup["reason"] == "optional_detail_probe_unavailable"
        assert len(service.gateway.requests) == 1


@pytest.mark.anyio
async def test_missing_point_probe_keeps_existing_evidence_and_reason_contract():
    utterance, turn, interview = _input()
    data = _data()
    data["understanding"]["claims"] = [{"claim": "记录恢复位置", "evidence_id": "E1"}]
    data["followup"]["rationale"] = ""
    understanding, followup = await _service(Gateway(data)).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and followup["selected"]
    assert followup["target_key_points"] == ["幂等键"]


def test_confirmation_prompt_distinguishes_completion_scope_and_has_no_next_question_promise():
    contract = prompt_contract("interview_turn_decision", {"transcript": "我记录恢复位置。我没有补充了。",
        "capability_points": ["恢复位置"], "semantic_first": True, "completion_confirmed": True})
    schema = contract.response_schema["properties"]["understanding"]
    assert "finish_answer" in schema["properties"]["turn_intent"]["enum"]
    text = "\n".join(m.content for m in contract.messages)
    assert "已覆盖" in text and "finish_answer" in text and "finish_topic" in text
    assert "下一题" not in SUPPLEMENT_SPEECH["check"]
    assert SUPPLEMENT_SPEECH_VERSION == "supplement_confirmation.v2"


@pytest.mark.anyio
async def test_spoken_finish_answer_commits_parent_and_delivers_formal_child_once(tmp_path, monkeypatch):
    from app.core.prompt.understanding_references import understanding_references
    from app.providers.mock.provider import MockProvider
    from test_automatic_turn_integration import _automatic_session, _current, _assert_one_automatic_answer
    from test_declined_answer_integration import (
        _semantic_provider, _add_next_question, _playable_speech, _answer_without_confirmation, _TECHNICAL,
    )
    from test_evidence_owner_recovery_integration import TURN_ID, _wait_until

    calls = _semantic_provider(monkeypatch, technical=True, probe=True)
    original = MockProvider.invoke

    async def finish_answer(provider, capability, request, context):
        response = await original(provider, capability, request, context)
        if request.metadata.get("prompt_version", "").startswith("interview_turn_decision."):
            data = deepcopy(response.data)
            refs = understanding_references(request.metadata["transcript"], request.metadata["capability_points"])
            data["understanding"].update(turn_intent="finish_answer", answer_content="technical",
                followup_allowed=True, covered_point_ids=list(refs["capabilities"]), missing_point_ids=[],
                completion_basis={"source": "explicit_server_intent", "evidence_ids": list(refs["evidence"])[-1:]})
            response = response.model_copy(update={"data": data})
        return response

    monkeypatch.setattr(MockProvider, "invoke", finish_answer)
    body = _TECHNICAL + "我讲完了。"
    async with _automatic_session(tmp_path, monkeypatch, [body, ""], spoken_confirmation=True) as (store, runtime, channel, managed):
        _add_next_question(store)
        spoken = _playable_speech(runtime)
        await _answer_without_confirmation(managed)
        await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
        await _wait_until(lambda: any(event["type"] == "conversation.act.selected"
            and event["payload"]["act_type"] == "followup" for event in _current(runtime)["agent_events"]), timeout=5)
        _assert_one_automatic_answer(store, runtime, body, voice_frames=5, continuation_frames=0)
        current = _current(runtime)
        children = [turn for turn in current["turns"] if turn.get("is_followup")]
        assert len(children) == 1
        child = children[0]
        assert child["parent_turn_id"] == child["root_turn_id"] == TURN_ID
        assert child["followup_depth"] == 1 and current["current_turn_id"] == child["id"]
        assert current["answers"][0]["turn_id"] == TURN_ID
        assert current["turns"][0]["current_understanding"]["turn_intent"] == "finish_answer"
        assert child["conversation_acts"][0]["prompt_version"] == current["turns"][0]["current_understanding"]["prompt_version"] == "interview_turn_decision.v95"
        assert SUPPLEMENT_SPEECH["check"] not in spoken
        assert len([call for call in calls if call[0] == "understanding"]) == 1
        assert not [call for call in calls if call[0] == "supplement"]
