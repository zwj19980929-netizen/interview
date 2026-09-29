"""Old claims remain retrievable through the current interview's read-only tool."""
from copy import deepcopy
import json

import pytest

from app.core.errors import ApiError
from app.core.prompt.interviewer_supervisor import supervisor_contract
from app.services.interviewer_supervisor.context import conversation_memory, context_fingerprint
from app.services.interviewer_supervisor.tools import InterviewerTools


def history_session():
    turns, answers = [], []
    for index in range(10):
        text = "合成方案用唯一请求号去重。合成方案在事务中写入处理记录。"
        turns.append({
            "id": "turn_%d" % index, "question_id": "question_%d" % index,
            "root_turn_id": "turn_%d" % index, "competency_ids": ["幂等"],
            "question_spoken_text": "你如何处理重复请求？", "is_followup": False,
            "current_understanding": {"revision": 2, "intent": "answer", "turn_intent": "finish_answer",
                "answer_summary": "使用请求号和事务。", "claims": [
                    {"claim": "使用唯一请求号。", "evidence_quote": "合成方案用唯一请求号去重。"},
                    {"claim": "事务记录去重状态。", "evidence_quote": "合成方案在事务中写入处理记录。"},
                ]},
        })
        answers.append({"id": "answer_%d" % index, "turn_id": "turn_%d" % index,
                        "final_transcript": text, "audio_uri": "private-not-for-tool",
                        "score": 88})
    return {"id": "interview_060", "organization_id": "org_060", "turns": turns,
            "answers": answers, "plan_snapshot": {}}


def test_old_second_claim_can_be_read_after_recent_window_expires():
    session = history_session()
    before = deepcopy(session)
    memory = conversation_memory(session)
    assert "turn_0" not in [row["turn_id"] for row in memory["competency_evidence"]["幂等"]["observations"]]
    result = InterviewerTools(session).execute("interview.read_context", "turn_0")
    assert result["status"] == "ok"
    evidence = result["evidence"]
    assert evidence["turn_id"] == "turn_0" and evidence["answer_id"] == "answer_0"
    assert len(evidence["claims"]) == 2
    claim = evidence["claims"][1]
    answer = session["answers"][0]["final_transcript"]
    assert answer[claim["start"]:claim["end"]] == claim["evidence_quote"]
    assert evidence["source"] == "committed_answer"
    assert set(evidence) == {"turn_id", "root_turn_id", "parent_turn_id", "is_followup",
                            "question_text", "answer_id", "answer_summary", "understanding_revision", "source", "claims"}
    assert evidence["understanding_revision"] == 2
    assert session == before


@pytest.mark.parametrize("reference", ["foreign_turn", "interview_060", "", {}, 4])
def test_history_reference_cannot_select_other_interviews_or_arbitrary_objects(reference):
    with pytest.raises(ApiError) as error:
        InterviewerTools(history_session()).execute("interview.read_context", reference)
    assert error.value.code == "INTERVIEW_TOOL_REFERENCE_INVALID"


@pytest.mark.parametrize("state", ["unsubmitted", "duplicate"])
def test_only_one_committed_answer_can_authorize_history_read(state):
    session = history_session()
    if state == "unsubmitted":
        session["answers"] = session["answers"][1:]
    else:
        session["answers"].append({**session["answers"][0], "id": "ambiguous_answer"})
    with pytest.raises(ApiError):
        InterviewerTools(session).execute("interview.read_context", "turn_0")


def test_unbound_claim_is_omitted_and_repeated_quote_has_no_invented_position():
    session = history_session()
    turn, answer = session["turns"][0], session["answers"][0]
    turn["current_understanding"]["claims"].append({"claim": "不能读出", "evidence_quote": "不属于原文"})
    answer["final_transcript"] += "合成方案用唯一请求号去重。"
    evidence = InterviewerTools(session).execute("interview.read_context", "turn_0")["evidence"]
    assert len(evidence["claims"]) == 2
    repeated = evidence["claims"][0]
    assert repeated["occurrences"] == 2
    assert repeated["start"] is None and repeated["end"] is None
    assert "score" not in str(evidence) and "private-not-for-tool" not in str(evidence)


def test_read_context_without_reference_keeps_existing_summary_and_skill_permissions():
    session = history_session()
    assert InterviewerTools(session).execute("interview.read_context", None) == {
        "status": "ok", "context": conversation_memory(session)}
    session["plan_snapshot"]["enterprise_skill_snapshot"] = {"allowed_tools": ["questions.search"]}
    with pytest.raises(ApiError) as error:
        InterviewerTools(session).execute("interview.read_context", "turn_0")
    assert error.value.code == "INTERVIEW_TOOL_FORBIDDEN"


def test_supervisor_contract_describes_targeted_history_without_new_tool_or_unbounded_context():
    contract = supervisor_contract({}, [], tools=["interview.read_context"], question_ids=[])
    assert contract.version == "interviewer_supervisor.v4"
    instructions = contract.messages[0].content
    assert "turn_id" in instructions and "interview.read_context" in instructions
    assert contract.response_schema["properties"]["argument"]["maxLength"] == 128


def test_history_source_correction_invalidates_a_prepared_planning_decision():
    session = history_session()
    original = context_fingerprint(session)
    session["answers"][0]["final_transcript"] = "更正后的合成回答。"
    assert context_fingerprint(session) != original


@pytest.mark.anyio
async def test_supervisor_can_request_old_evidence_in_its_existing_tool_budget():
    from test_interviewer_supervisor import Gateway, decision, source_session, supervisor

    session = source_session()
    session.update({key: value for key, value in history_session().items() if key in {"turns", "answers"}})
    original = deepcopy(session)
    gateway = Gateway(decision(None, action="use_tool", tool_name="interview.read_context",
                               argument="turn_0", reason_code="inspect_context"), decision("q2"))
    result = await supervisor(gateway).propose_next(session)
    observed = json.loads(gateway.calls[1].messages[-1].content)["tool_observations"]
    assert observed[0]["result"]["evidence"]["claims"][1]["evidence_quote"] == "合成方案在事务中写入处理记录。"
    assert result.model_calls == 2 and result.tools_used == ("interview.read_context",)
    assert result.question_id == "q2" and session == original
    assert all(request.execution_budget.max_provider_retries == 0 for request in gateway.calls)
