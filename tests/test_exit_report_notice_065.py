"""Voluntary withdrawal stays a factual report event, never a hiring decision."""
import csv
from copy import deepcopy
from io import StringIO
import json

import pytest

from app.domain.scoring_quality import CANDIDATE_CONFIRMED_WITHDRAWAL_NOTICE, CANDIDATE_WITHDRAWAL_NOTICE, project_report
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.reports import ReportService
from tests.test_adaptive_interview import end, make_session, scored, select, submit


def _withdrawn_session(schema_version):
    source = scored(submit(select(make_session(), "q1").session).session, 91).session
    source = select(source, "q2").session
    source["plan_snapshot"]["execution_schema_version"] = schema_version
    return end(source, "candidate_requested").session


@pytest.mark.parametrize("schema_version", [2, 3])
def test_exit_report_keeps_evidence_and_unassessed_scope_for_human_review(schema_version):
    session = _withdrawn_session(schema_version)
    before = deepcopy(session)
    report = ReportService(InMemoryStore()).build_report(session, trigger_reason="candidate_requested")

    assert CANDIDATE_WITHDRAWAL_NOTICE in report["scope_notice"]
    assert report["risks"].count(CANDIDATE_WITHDRAWAL_NOTICE) == 1
    assert report["human_decision_required"] is True
    assert report["candidate_input_completion_reason"] == "candidate_requested"
    assert report["question_evaluations"][0]["score"] == 91
    assert report["coverage_status"] == "insufficient_evidence"
    assert report["overall_score"] is None
    assert report["job_fit_level"] == report["recommendation"] == "insufficient_evidence"
    assert report["skipped_questions"][0]["counts_toward_score"] is False
    assert session == before


@pytest.mark.parametrize("schema_version", [2, 3])
def test_repeated_exit_report_projection_preserves_scope_notice_without_duplicate_risks(schema_version):
    session = _withdrawn_session(schema_version)
    report = ReportService(InMemoryStore()).build_report(session, trigger_reason="candidate_requested")
    report["scope_notice"] = "原题未呈现的关键点不参与评分。 " + report["scope_notice"]
    before = deepcopy(report)
    projected = project_report(project_report(report, session), session)

    assert projected["scope_notice"].startswith("原题未呈现的关键点不参与评分。")
    assert projected["scope_notice"].count(CANDIDATE_WITHDRAWAL_NOTICE) == 1
    assert projected["risks"].count(CANDIDATE_WITHDRAWAL_NOTICE) == 1
    assert report == before


@pytest.mark.parametrize("export_format", ["json", "csv"])
def test_exit_report_exports_the_factual_notice_and_retained_scores(export_format):
    session = _withdrawn_session(3)
    store = InMemoryStore()
    service = ReportService(store)
    report = service.build_report(session, trigger_reason="candidate_requested")
    session["report_revisions"] = [report]
    session["current_report_id"] = report["id"]
    with persistence_for(store).transaction("org_default") as tx:
        tx.interview_sessions.add(session)

    content = service.export_report(session["id"], export_format=export_format, actor_id="synthetic_reviewer")["content"]
    value = json.loads(content) if export_format == "json" else next(csv.DictReader(StringIO(content)))
    risks = value["risks"] if export_format == "json" else json.loads(value["risks"])
    evidence = value["question_evaluations"] if export_format == "json" else json.loads(value["question_evaluations"])
    assert risks.count(CANDIDATE_WITHDRAWAL_NOTICE) == 1
    assert evidence[0]["score"] == 91
    assert value["human_decision_required"] in (True, "True")


def test_other_completion_reasons_do_not_become_voluntary_withdrawal():
    session = _withdrawn_session(3)
    session["candidate_input_completion_reason"] = "appointment_window_expired"
    report = ReportService(InMemoryStore()).build_report(session, trigger_reason="appointment_window_expired")
    assert "主动提前退出" not in report.get("scope_notice", "")
    assert all("主动提前退出" not in risk for risk in report["risks"])


@pytest.mark.parametrize("schema_version", [2, 3])
def test_only_recorded_confirmation_can_label_withdrawal_as_confirmed(schema_version):
    session = _withdrawn_session(schema_version)
    session["candidate_exit_confirmed_at"] = "2026-09-23T04:00:00Z"
    report = ReportService(InMemoryStore()).build_report(session, trigger_reason="candidate_requested")
    assert CANDIDATE_CONFIRMED_WITHDRAWAL_NOTICE in report["scope_notice"]
    assert CANDIDATE_CONFIRMED_WITHDRAWAL_NOTICE in report["risks"]
    assert CANDIDATE_WITHDRAWAL_NOTICE not in report["risks"]
    assert report["question_evaluations"][0]["score"] == 91
    assert report["human_decision_required"] is True
