"""Synthetic meaning, late evidence and diagnostic regressions for 057."""
from copy import deepcopy
import json
import logging

import pytest

from app.core.prompt.contracts import prompt_contract
from test_prepared_turn_decision import Gateway, _input, _service
from test_formal_followup_056 import completed_answer


@pytest.mark.anyio
async def test_late_declared_claim_can_support_a_detail_probe():
    utterance, turn, interview = _input()
    sentences = ["先记录请求。", "再记录序号。", "然后检查连接。", "接着恢复订阅。",
                 "最后写入恢复位置。", "我讲完了。"]
    utterance = utterance.model_copy(update={"text": "".join(sentences)})
    interview["_semantic_first"] = True
    data = completed_answer()
    data["understanding"].update(evidence_ids=[f"E{n}" for n in range(1, 7)],
        claims=[{"claim": "写入恢复位置", "evidence_id": "E5"}],
        completion_basis={"source": "explicit_server_intent", "evidence_ids": ["E6"]})
    data["followup"]["evidence_id"] = "E5"
    service = _service(Gateway(data))
    understanding, decision = await service.prepare_decision(utterance, turn, interview)
    assert understanding.problem is None
    assert decision["selected"] and decision["evidence_quotes"] == [sentences[4]]
    proposal = deepcopy(data["followup"])
    proposal.pop("evidence_id")
    proposal.pop("target_point_ids")
    proposal.update(evidence_quote=sentences[4], target_capability_points=["确认序号"])
    service.gateway = Gateway(proposal)
    separate = await service.select_followup(interview, turn, utterance, understanding)
    assert separate["selected"] and separate["evidence_quotes"] == [sentences[4]]


@pytest.mark.anyio
async def test_answer_declined_cannot_be_an_unfinished_or_unclear_turn():
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": "这个词是指什么？"})
    interview["_semantic_first"] = True
    data = completed_answer(selected=False)
    data["understanding"].update(intent="answer_declined", answer_content="none",
        turn_intent="answering", completion_basis=None, followup_allowed=False,
        claims=[], evidence_ids=["E1"], covered_point_ids=[], missing_point_ids=["P1", "P2"])
    understanding, decision = await _service(Gateway(deepcopy(data), deepcopy(data))).prepare_decision(utterance, turn, interview)
    assert understanding.problem is not None
    assert not decision["selected"] and understanding.completion_basis is None


@pytest.mark.anyio
@pytest.mark.parametrize("case,reason", [("declined", "model_declined"), ("rejected", "followup_proposal_rejected"),
                                       ("disabled", "followup_disabled"), ("selected", "claim_detail_verification")])
async def test_followup_diagnostic_has_reason_and_budget_without_candidate_text(caplog, monkeypatch, case, reason):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "我讲完了。"})
    interview["_semantic_first"] = True
    data = completed_answer(selected=case != "declined")
    if case == "rejected":
        data["followup"]["rationale"] = ""
    if case == "disabled":
        turn["allow_followup"] = False
        data = data["understanding"]
    monkeypatch.setattr(logging.getLogger("interviewer.speech_evidence"), "propagate", True)
    with caplog.at_level(logging.INFO, logger="interviewer.speech_evidence"):
        _, decision = await _service(Gateway(data)).prepare_decision(utterance, turn, interview)
    lines = [r.message for r in caplog.records if '"event": "turn_control.followup_decided"' in r.message]
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["reason"] == reason and event["phase"] == "prepared"
    assert "max_total" in event and "target_count" in event
    assert event["prompt_version"].startswith("interview_turn_")
    assert utterance.text not in lines[0] and "恢复位置" not in lines[0]
    assert decision["reason"] == reason


def test_semantic_prompt_separates_unknown_meaning_from_explicit_topic_refusal():
    contract = prompt_contract("interview_turn_decision", {"semantic_first": True,
        "transcript": "这个术语是什么意思？", "capability_points": ["恢复位置"]})
    text = "\n".join(m.content for m in contract.messages)
    assert "不连贯" in text and "疑问" in text and "拒绝" in text
    assert "当selected=false时" in text
    assert "前四项" not in text
    assert "哪怕只说了一句" in text and "内容抽取优先" in text
    fields = contract.response_schema["properties"]["understanding"]["properties"]
    description = fields["answer_content"]["description"]
    assert "未命中能力点也可partial" in description
    assert "含义确定且有原文依据" in description and "none不代表不会或拒答" in description


@pytest.mark.anyio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_brief_partial_answer_then_explicit_topic_closure_preserves_uncovered_claim(confirmed):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": "我会保存请求编号。这个话题先到这里吧。"})
    interview.update(_semantic_first=True, _answer_completion_confirmed=confirmed)
    data = completed_answer(intent="finish_topic", selected=False)
    data["understanding"].update(answer_content="partial", answer_summary="保存请求编号，但没有展开。",
        claims=[{"claim": "保存请求编号", "evidence_id": "E1"}],
        evidence_ids=["E1", "E2"], covered_point_ids=[], missing_point_ids=["P1", "P2"])
    service = _service(Gateway(data))
    understanding, decision = await service.prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and understanding.intent == "answer"
    assert understanding.claims[0].evidence_quote == "我会保存请求编号。"
    assert understanding.answer_content == "partial" and not understanding.covered_capability_points
    assert understanding.turn_intent == "finish_topic" and not understanding.followup_allowed
    assert service.can_complete_without_confirmation(understanding, utterance.text)
    assert not decision["selected"] and decision["reason"] == "candidate_topic_boundary"


@pytest.mark.anyio
@pytest.mark.parametrize("disabled", [False, True])
async def test_separate_followup_diagnostic_uses_actual_producer_version(caplog, monkeypatch, disabled):
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": utterance.text + "我讲完了。"})
    interview["_semantic_first"] = True
    service = _service(Gateway(completed_answer(selected=False)))
    understanding, _ = await service.prepare_decision(utterance, turn, interview)
    proposal = completed_answer(selected=False)["followup"]
    proposal.pop("evidence_id")
    proposal.pop("target_point_ids")
    proposal.update(evidence_quote="", target_capability_points=[])
    service.gateway = Gateway(proposal)
    turn["allow_followup"] = not disabled
    monkeypatch.setattr(logging.getLogger("interviewer.speech_evidence"), "propagate", True)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="interviewer.speech_evidence"):
        await service.select_followup(interview, turn, utterance, understanding)
    events = [json.loads(r.message) for r in caplog.records if '"event": "turn_control.followup_decided"' in r.message]
    assert len(events) == 1
    assert events[0]["prompt_version"] == (None if disabled else "controlled_followup.v6")
    assert events[0]["reason"] == ("followup_disabled" if disabled else "model_declined")
    assert len(service.gateway.requests) == (0 if disabled else 1)


def test_followup_diagnostic_drops_arbitrary_reason_and_prompt_text(caplog, monkeypatch):
    _, turn, interview = _input()
    service = _service(Gateway())
    monkeypatch.setattr(logging.getLogger("interviewer.speech_evidence"), "propagate", True)
    secret = "synthetic candidate private statement"
    with caplog.at_level(logging.INFO, logger="interviewer.speech_evidence"):
        service._record_followup_decision({"selected": False, "reason": secret, "rationale": secret,
            "policy": {}, "target_key_points": [secret]}, interview, turn, phase="prepared", prompt_version=secret)
    event = json.loads(caplog.records[-1].message)
    assert event["reason"] == "unclassified" and event["prompt_version"] is None
    assert event["target_count"] == 1 and secret not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("term", ["message", "image", "storage", "manager", "package"])
async def test_english_technical_words_are_not_sensitive_age_attributes(term):
    from test_prepared_turn_decision import _data
    utterance, turn, interview = _input()
    utterance = utterance.model_copy(update={"text": f"I store the {term} identifier."})
    turn["question_snapshot"]["key_points"] = [{"text": term + " validation"}, {"text": "确认序号"}]
    data = _data()
    data["followup"]["question_text"] = f"How do you validate the {term} identifier?"
    understanding, decision = await _service(Gateway(data)).prepare_decision(utterance, turn, interview)
    assert understanding.problem is None and decision["selected"]
    assert decision["evidence_quotes"] == [utterance.text]
    assert decision["target_key_points"] == [term + " validation"]


@pytest.mark.parametrize("question", ["What is your age?", "What is your gender?", "你的年龄是多少？",
                                      "请问你的age是多少？", "可以说说你的religion吗？"])
def test_actual_sensitive_attribute_question_remains_rejected(question):
    from app.services.conversation_understanding import FollowupProposalError
    from test_prepared_turn_decision import _data
    utterance, turn, _ = _input()
    data = _data()["followup"]
    data.update(question_text=question, evidence_quote=utterance.text, target_capability_points=["幂等键"])
    with pytest.raises(FollowupProposalError, match="safety_gate_rejected"):
        _service(Gateway())._gate_followup(data, transcript=utterance.text, allowed_evidence=[utterance.text],
            allowed_targets=["幂等键"], difficulty="mid", root=turn, max_chars=180)


def test_followup_contract_requests_one_focus_and_keeps_the_shape_gate():
    from app.services.conversation_understanding import FollowupProposalError
    from test_prepared_turn_decision import _data
    for purpose in ("interview_turn_decision", "controlled_followup"):
        contract = prompt_contract(purpose, {"semantic_first": True, "transcript": "我记录位置。",
            "capability_points": ["恢复位置"], "max_probe_chars": 180})
        schema = contract.response_schema["properties"].get("followup", contract.response_schema)
        assert "至多一个" in schema["properties"]["question_text"]["description"]
        assert schema["properties"]["question_text"]["maxLength"] == 600
    utterance, turn, _ = _input()
    data = _data()["followup"]
    data.update(question_text="什么时候更新位置？如何确认成功？", evidence_quote=utterance.text,
                target_capability_points=["幂等键"])
    with pytest.raises(FollowupProposalError, match="question_shape_invalid"):
        _service(Gateway())._gate_followup(data, transcript=utterance.text, allowed_evidence=[utterance.text],
            allowed_targets=["幂等键"], difficulty="mid", root=turn, max_chars=180)
