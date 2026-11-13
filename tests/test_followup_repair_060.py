"""A shape-only proposal repair cannot re-understand or advance an answer."""
import asyncio
from copy import deepcopy
import time

import pytest

from app.core.prompt.contracts import prompt_contract
from app.core.prompt.validation import validate_structured_response
from app.services import conversation_understanding as module
from test_formal_followup_056 import completed_answer
from test_prepared_turn_decision import Gateway, _input, _service


def original(mode, *, question="何时保存？如何恢复？", **patch):
    data = completed_answer()
    data["followup"].update(question_text=question, **patch)
    if mode == "composite":
        return data
    data = data["followup"]
    data["evidence_quote"] = _input()[0].text if data.pop("evidence_id") == "E1" else "我讲完了。"
    data["target_capability_points"] = ["确认序号"]
    data.pop("target_point_ids")
    return data


class ValidatingGateway(Gateway):
    async def invoke(self, capability, request):
        response = await super().invoke(capability, request)
        validate_structured_response(response.data, request.json_schema)
        return response


async def exercise(mode, gateway):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "我讲完了。"})
    interview["_semantic_first"] = True
    if mode == "composite":
        return await _service(gateway).prepare_decision(utterance, turn, interview)
    service = _service(Gateway(completed_answer(selected=False)))
    understanding, _ = await service.prepare_decision(utterance, turn, interview)
    before = understanding.model_dump()
    service.gateway = gateway
    decision = await service.select_followup(interview, turn, utterance, understanding)
    assert understanding.model_dump() == before
    return understanding, decision


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["composite", "separate"])
@pytest.mark.parametrize("question", ["何时保存？如何恢复？", "请" * 181])
async def test_shape_repair_creates_one_evidence_bound_act_without_reunderstanding(mode, question):
    gateway = ValidatingGateway(original(mode, question=question), {"selected": True, "question_text": "恢复位置是在什么时候记录的？"})
    understanding, decision = await exercise(mode, gateway)
    assert understanding.problem is None and understanding.claims
    assert decision["selected"] and decision["followup_depth"] == 1
    assert decision["parent_turn_id"] == decision["root_turn_id"] == "turn_prepared"
    assert decision["conversation_act"]["prompt_version"] == "controlled_followup_repair.v1"
    assert decision["target_key_points"] == ["确认序号"]
    assert decision["evidence_quotes"] == [_input()[0].text]
    assert len(gateway.requests) == 2
    repair = gateway.requests[1]
    assert repair.purpose == "controlled_followup"
    assert repair.execution_budget.timeout_s <= 3 and repair.execution_budget.max_provider_attempts == 1
    assert set(repair.json_schema["properties"]) == {"selected", "question_text"}
    assert "understanding" not in repair.json_schema["properties"]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["composite", "separate"])
@pytest.mark.parametrize("reply", [
    {"selected": True, "question_text": "何时保存？如何恢复？"},
    {"selected": True, "question_text": "你的年龄是多少？"},
    {"selected": True, "question_text": "recoverable_checkpoint fenced_epoch怎么用？"},
    {"selected": True, "question_text": "何时保存？", "evidence_id": "E2"},
    {"selected": False, "question_text": ""},
])
async def test_failed_or_declined_repair_keeps_verified_answer_without_fallback(mode, reply):
    gateway = ValidatingGateway(original(mode), reply)
    understanding, decision = await exercise(mode, gateway)
    assert understanding.problem is None and understanding.claims
    assert not decision["selected"] and "conversation_act" not in decision
    assert len(gateway.requests) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["composite", "separate"])
@pytest.mark.parametrize("patch", [{"leaks_answer": True}, {"sensitive_attribute_inference": True},
    {"evidence_id": "E2"}])
async def test_unsafe_or_unbound_proposal_cannot_enter_shape_repair(mode, patch):
    gateway = ValidatingGateway(original(mode, **patch))
    understanding, decision = await exercise(mode, gateway)
    assert understanding.problem is None and understanding.claims
    assert not decision["selected"]
    assert len(gateway.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["composite", "separate"])
async def test_model_declining_probe_does_not_trigger_repair(mode):
    data = original(mode)
    proposal = data["followup"] if mode == "composite" else data
    proposal.update(selected=False, question_text="", rationale="")
    if mode == "composite":
        proposal.update(evidence_id="", target_point_ids=[])
    else:
        proposal.update(evidence_quote="", target_capability_points=[])
    gateway = ValidatingGateway(data)
    understanding, decision = await exercise(mode, gateway)
    assert understanding.problem is None and decision["reason"] == "model_declined"
    assert len(gateway.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["composite", "separate"])
async def test_repair_timeout_uses_remaining_total_budget_and_preserves_answer(mode, monkeypatch):
    monkeypatch.setattr(module, "_DECISION_TIMEOUT", .15, raising=False)
    monkeypatch.setattr(module, "_FOLLOWUP_REPAIR_TIMEOUT", .12, raising=False)
    class SlowGateway(ValidatingGateway):
        async def invoke(self, capability, request):
            await asyncio.sleep(.07 if not self.requests else .12)
            return await super().invoke(capability, request)
    gateway = SlowGateway(original(mode), {"selected": True, "question_text": "何时记录位置？"})
    started = time.monotonic()
    understanding, decision = await exercise(mode, gateway)
    assert time.monotonic() - started < .25
    assert understanding.problem is None and understanding.claims
    assert not decision["selected"] and decision["reason"] == "followup_repair_timeout"


@pytest.mark.anyio
async def test_long_answer_probe_timeout_keeps_full_verified_understanding(monkeypatch):
    from app.core.prompt.understanding_references import understanding_references
    from app.model_gateway.schemas import ChatJSONResponse, ProviderMeta, Usage
    from test_prepared_turn_decision import TRANSCRIPT
    monkeypatch.setattr(module, "_DECISION_TIMEOUT", .16)
    utterance, turn, interview = _input()
    text = "合成的审计背景。" * 600 + TRANSCRIPT + "我讲完了。"
    utterance = utterance.model_copy(update={"text": text})
    interview.update(_semantic_first=True, _answer_completion_confirmed=True)
    cancelled = asyncio.Event()

    class LongTimeoutGateway:
        def __init__(self):
            self.requests = []

        async def invoke(self, capability, request):
            self.requests.append(request)
            if request.purpose == "controlled_followup":
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            assert request.metadata["transcript"] == text
            await asyncio.sleep(.06)
            evidence = understanding_references(text, ["幂等键", "确认序号"])["evidence"]
            claim_id = next(key for key, quote in evidence.items() if quote == TRANSCRIPT)
            finish_id = next(key for key, quote in evidence.items() if quote == "我讲完了。")
            data = completed_answer(selected=False)["understanding"]
            data["claims"][0]["evidence_id"] = claim_id
            data["evidence_ids"] = [claim_id]
            data["completion_basis"]["evidence_ids"] = [finish_id]
            validate_structured_response(data, request.json_schema)
            return ChatJSONResponse(data=data, usage=Usage(), provider=ProviderMeta(
                provider_id="synthetic", model="timeout-060", request_id="synthetic", latency_ms=0))

    gateway = LongTimeoutGateway()
    started = time.monotonic()
    understanding, decision = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert time.monotonic() - started < .3
    assert understanding.problem is None and understanding.evidence_quotes == [TRANSCRIPT]
    assert understanding.turn_intent == "finish_answer" and understanding.claims
    assert not decision["selected"] and decision["reason"] == "followup_deadline_exhausted"
    assert cancelled.is_set()
    assert len(gateway.requests) == 2
    assert gateway.requests[1].execution_budget.timeout_s < .1


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["composite", "separate"])
async def test_new_input_cancellation_revokes_pending_repair(mode):
    entered, cancelled = asyncio.Event(), asyncio.Event()
    class BlockedGateway(ValidatingGateway):
        async def invoke(self, capability, request):
            if self.requests:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return await super().invoke(capability, request)
    gateway = BlockedGateway(original(mode))
    task = asyncio.create_task(exercise(mode, gateway))
    await asyncio.wait_for(entered.wait(), .5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


def test_repair_contract_has_no_authority_to_change_evidence_or_understanding():
    contract = prompt_contract("controlled_followup_repair", {"max_probe_chars": 180})
    assert contract.version == "controlled_followup_repair.v1"
    assert set(contract.response_schema["properties"]) == {"selected", "question_text"}
    assert contract.response_schema["properties"]["question_text"]["maxLength"] == 180
    assert contract.response_schema["additionalProperties"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("boundary", ["time", "root_budget", "candidate_declined"])
async def test_repair_cannot_reopen_closed_scope(boundary):
    from datetime import datetime, timedelta, timezone
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "我讲完了。"})
    interview["_semantic_first"] = True
    data = original("composite")
    if boundary == "candidate_declined":
        data["understanding"].update(turn_intent="finish_topic", followup_allowed=False)
    class BoundaryGateway(ValidatingGateway):
        async def invoke(self, capability, request):
            response = await super().invoke(capability, request)
            if boundary == "time":
                interview["scheduled_end_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            elif boundary == "root_budget":
                interview["turns"].append({"id": "existing_child", "root_turn_id": turn["id"], "is_followup": True})
            return response
    gateway = BoundaryGateway(data)
    understanding, decision = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.claims
    assert not decision["selected"] and len(gateway.requests) == 1


@pytest.mark.anyio
async def test_scope_is_checked_again_after_repair_returns():
    from datetime import datetime, timedelta, timezone
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "我讲完了。"})
    interview["_semantic_first"] = True
    class ExpiresDuringRepair(ValidatingGateway):
        async def invoke(self, capability, request):
            response = await super().invoke(capability, request)
            if len(self.requests) == 2:
                interview["scheduled_end_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            return response
    gateway = ExpiresDuringRepair(original("composite"), {"selected": True, "question_text": "何时记录位置？"})
    understanding, decision = await _service(gateway).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.claims
    assert not decision["selected"] and decision["reason"] == "time_budget_exhausted"
    assert len(gateway.requests) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["provider", "invalid_response"])
@pytest.mark.parametrize("boundary,reason", [
    ("time", "time_budget_exhausted"),
    ("root_budget", "root_budget_exhausted"),
    ("topic", "candidate_topic_boundary"),
])
async def test_failed_standalone_probe_cannot_fallback_after_scope_closes(boundary, reason, failure):
    from datetime import datetime, timedelta, timezone
    from app.model_gateway.errors import ProviderError
    from test_prepared_turn_decision import _data
    utterance, turn, interview = _input()
    interview["turns"].extend([{"id": "root_two"}, {"id": "root_three"}])
    interview["followup_policy"] = {"max_per_root": 1}
    service = _service(Gateway(_data()))
    understanding, _ = await service.prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.missing_capability_points
    claims_before = deepcopy(understanding.claims)

    class ScopeClosingGateway(Gateway):
        async def invoke(self, capability, request):
            if boundary == "time":
                interview["scheduled_end_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            elif boundary == "root_budget":
                interview["turns"].append({"id": "existing_child", "root_turn_id": turn["id"], "is_followup": True})
            else:
                understanding.followup_allowed = False
            return await super().invoke(capability, request)

    gateway = ScopeClosingGateway({"selected": "invalid"}, error=(
        ProviderError("synthetic_unavailable", "synthetic", retryable=True) if failure == "provider" else None))
    service.gateway = gateway
    decision = await service.select_followup(interview, turn, utterance, understanding)
    assert decision["selected"] is False and decision["reason"] == reason
    assert "conversation_act" not in decision
    assert understanding.problem is None and understanding.claims == claims_before
    assert len(gateway.requests) == 1


@pytest.mark.anyio
async def test_invalid_understanding_cannot_enter_optional_repair():
    data = original("composite")
    data["understanding"]["claims"] = []
    gateway = ValidatingGateway(data, data)
    understanding, decision = await exercise("composite", gateway)
    assert understanding.problem is not None and not decision["selected"]
    assert len(gateway.requests) == 2
    assert all(request.purpose == "interview_turn_understanding" for request in gateway.requests)


@pytest.mark.anyio
async def test_understanding_correction_and_repair_share_three_call_limit():
    invalid, corrected = original("composite"), original("composite")
    invalid["understanding"]["missing_point_ids"] = ["P1"]
    gateway = ValidatingGateway(invalid, corrected, {"selected": True, "question_text": "何时记录位置？"})
    understanding, decision = await exercise("composite", gateway)
    assert understanding.problem is None and understanding.claims and decision["selected"]
    assert len(gateway.requests) == 3
    assert [request.purpose for request in gateway.requests] == [
        "interview_turn_understanding", "interview_turn_understanding", "controlled_followup"]


@pytest.mark.anyio
async def test_repaired_probe_commits_parent_and_delivers_unique_child(tmp_path, monkeypatch):
    from app.core.prompt.contracts import SUPPLEMENT_SPEECH
    from app.core.prompt.understanding_references import understanding_references
    from app.providers.mock.provider import MockProvider
    from test_automatic_turn_integration import (
        _automatic_session, _current, _assert_one_automatic_answer, _feed, _CONTINUATION,
    )
    from test_declined_answer_integration import (
        _semantic_provider, _add_next_question, _playable_speech, _answer_without_confirmation, _TECHNICAL,
        _stop_current_playback,
    )
    from test_evidence_owner_recovery_integration import TURN_ID, _wait_until
    from test_spoken_supplement_integration import _audio_aligned_transcripts
    calls = _semantic_provider(monkeypatch, technical=True, probe=True)
    invoke, repairs = MockProvider.invoke, []
    async def repair_provider(provider, capability, request, context):
        response = await invoke(provider, capability, request, context)
        version = request.metadata.get("prompt_version", "")
        data = deepcopy(response.data)
        if version.startswith("interview_turn_decision."):
            refs = understanding_references(request.metadata["transcript"], request.metadata["capability_points"])
            data["understanding"].update(turn_intent="finish_answer", answer_content="technical", followup_allowed=True,
                covered_point_ids=list(refs["capabilities"]), missing_point_ids=[],
                completion_basis={"source": "explicit_server_intent", "evidence_ids": list(refs["evidence"])[-1:]})
            data["followup"]["question_text"] = "何时记录恢复位置？如何避免丢失？"
        elif version == "controlled_followup_repair.v1":
            repairs.append(version)
            data = {"selected": True, "question_text": "你是在什么时机记录恢复位置的？"}
        return response.model_copy(update={"data": data})
    monkeypatch.setattr(MockProvider, "invoke", repair_provider)
    body = _TECHNICAL + "我讲完了。"
    reply = "没有补充了。"
    _audio_aligned_transcripts(monkeypatch, [body, reply])
    async with _automatic_session(tmp_path, monkeypatch, [""], spoken_confirmation=True) as (store, runtime, channel, managed):
        _add_next_question(store)
        spoken = _playable_speech(runtime)
        endpoint, clock = await _answer_without_confirmation(managed)
        await _wait_until(lambda: endpoint.confirmation.speaking)
        assert spoken == [SUPPLEMENT_SPEECH["check"]] and not _current(runtime)["answers"]
        assert not any(turn.get("is_followup") for turn in _current(runtime)["turns"])
        await _stop_current_playback(runtime, channel)
        await _wait_until(lambda: endpoint.confirmation._supplement_question_delivered)
        assert not _current(runtime)["answers"]
        await _feed(managed._ingress, _CONTINUATION, 5)
        clock.value += 1
        await _wait_until(lambda: len(_current(runtime)["answers"]) == 1, timeout=5)
        await _wait_until(lambda: any(event["type"] == "conversation.act.selected" and
            event["payload"]["act_type"] == "followup" for event in _current(runtime)["agent_events"]), timeout=5)
        _assert_one_automatic_answer(store, runtime, body + reply, voice_frames=5, continuation_frames=5)
        current = _current(runtime)
        children = [turn for turn in current["turns"] if turn.get("is_followup")]
        assert len(children) == 1 and repairs == ["controlled_followup_repair.v1"] * 2
        child = children[0]
        assert child["parent_turn_id"] == child["root_turn_id"] == TURN_ID
        assert child["followup_depth"] == 1 and current["current_turn_id"] == child["id"]
        assert child["conversation_acts"][0]["prompt_version"] == "controlled_followup_repair.v1"
        assert [call[1] for call in calls if call[0] == "understanding"] == [body, body + reply]
        assert [call for call in calls if call[0] == "supplement"] == [("supplement", reply, "finish")]
        assert spoken.count(SUPPLEMENT_SPEECH["check"]) == 1
