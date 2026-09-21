import csv
from copy import deepcopy
from io import StringIO
import json

import pytest

from app.core.errors import ApiError
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.reports import ReportService
from app.services.review import EnterpriseReviewService
from test_interview_evidence_group import _base_session, _turn, _answer, _evaluation


def _scored_chain():
    session = _base_session()
    session["candidate"] = {"id": "candidate_synthetic"}
    session["status"] = "in_progress"
    root, other = session["turns"]
    root.update(status="completed", question_spoken_text="实际提出的父问题？")
    other.update(order=4)
    first = _turn("child_one", 2, status="completed", is_followup=True,
                  parent_turn_id=root["id"], root_turn_id=root["id"], depth=1)
    second = _turn("child_two", 3, status="completed", is_followup=True,
                   parent_turn_id=first["id"], root_turn_id=root["id"], depth=2)
    first["question_spoken_text"] = "实际提出的第一条追问？"
    second["question_spoken_text"] = "实际提出的第二条追问？"
    # Deliberately store out of order; relationships, not array position, bind evidence.
    session["turns"] = [root, other, second, first]
    session["answers"] = [_answer("answer_" + t["id"], t["id"], "独立回答" + t["id"], status="completed")
                          for t in (root, first, second)]
    ids = [a["id"] for a in session["answers"]]
    for turn, answer in zip((root, first, second), session["answers"]):
        evaluation = _evaluation("eval_" + turn["id"], answer["id"], turn["question_snapshot_id"],
                                 80 if turn is root else 10)
        evaluation.update(revision=1, evidence_answer_ids=ids if turn is root else [answer["id"]])
        if turn is root:
            evaluation["evidence_sources"] = [{"key_point_id": "kp_outbox", "quote": "独立回答child_one",
                "answer_id": "answer_child_one", "turn_id": "child_one", "transcript_revision": 1}]
        answer.update(current_evaluation_id=evaluation["id"], current_evidence_answer_ids=evaluation["evidence_answer_ids"])
        session["evaluation_revisions"].append(evaluation)
    return session


def _persist(session):
    store = InMemoryStore()
    with persistence_for(store).transaction(session["organization_id"]) as tx:
        tx.interview_sessions.add(session)
    return store


def test_review_exposes_actual_spoken_question_and_parent_child_identity():
    session = _scored_chain()
    store = _persist(session)
    service = EnterpriseReviewService(store)
    turns = service.get_review(session["id"])["turns"]
    assert [t["turn_id"] for t in turns] == ["turn_root", "child_one", "child_two", "turn_second"]
    first, second = turns[1:3]
    assert first["question_spoken_text"] == first["question_text"] == "实际提出的第一条追问？"
    assert first["question"]["question_text"] != first["question_text"]
    assert (first["parent_turn_id"], first["root_turn_id"], first["followup_depth"]) == ("turn_root", "turn_root", 1)
    assert (second["parent_turn_id"], second["root_turn_id"], second["followup_depth"]) == ("child_one", "turn_root", 2)
    assert first["answer"]["id"] != second["answer"]["id"]
    assert first["playback"] is not None
    first["answer"]["final_transcript"] = "changed"
    assert service.get_review(session["id"])["turns"][1]["answer"]["final_transcript"] != "changed"
    with pytest.raises(ApiError):
        service.get_review(session["id"], "other_org")


def test_report_freezes_child_question_answer_scope_without_counting_child_scores():
    session = _scored_chain()
    service = ReportService(_persist(session))
    report = service.build_report(session, trigger_reason="synthetic")
    assert report["overall_score"] == 80
    assert len(report["question_evaluations"]) == 1
    root = report["question_evaluations"][0]
    assert root["question_text"] == "实际提出的父问题？"
    assert root["answer"]["id"] == "answer_turn_root"
    assert root["evidence_sources"][0]["answer_id"] == "answer_child_one"
    assert [item["turn_id"] for item in root["followups"]] == ["child_one", "child_two"]
    assert all(item["scope"]["weight"] == 0 and not item["scope"]["counts_toward_score"]
               and item["included_in_root_evidence"] for item in root["followups"])
    assert root["followups"][1]["parent_turn_id"] == "child_one"
    assert root["followups"][0]["scope"]["target_key_points"] == ["Outbox"]
    assert "audio_uri" not in root["followups"][0]["answer"]
    original = deepcopy(root["followups"][0])
    session["answers"][1]["final_transcript"] = "later correction"
    assert root["followups"][0] == original


def test_report_keeps_unanswered_followup_distinct_from_root_evidence():
    session = _scored_chain()
    session["answers"] = session["answers"][:2]
    session["turns"][2]["status"] = "skipped"
    session["evaluation_revisions"][0]["evidence_answer_ids"] = [a["id"] for a in session["answers"]]
    root = ReportService(_persist(session)).build_report(session, trigger_reason="synthetic")["question_evaluations"][0]
    assert root["followups"][1]["answer"] is None
    assert root["followups"][1]["included_in_root_evidence"] is False


@pytest.mark.parametrize("format", ["json", "csv"])
def test_report_export_preserves_parent_child_details(format):
    session = _scored_chain()
    store = _persist(session)
    service = ReportService(store)
    report = service.build_report(session, trigger_reason="synthetic")
    with persistence_for(store).transaction("org_default") as tx:
        item = tx.interview_sessions.get(session["id"])
        item["report_revisions"] = [report]
        item["current_report_id"] = report["id"]
        tx.interview_sessions.update(item, expected_version=item["version"])
    content = service.export_report(session["id"], export_format=format, actor_id="reviewer")["content"]
    rows = (json.loads(content)["question_evaluations"] if format == "json"
            else json.loads(next(csv.DictReader(StringIO(content)))["question_evaluations"]))
    assert rows[0]["followups"][1]["answer"]["id"] == "answer_child_two"
