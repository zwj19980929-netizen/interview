"""Release evidence cannot turn omitted measurements into passing results."""
from copy import deepcopy
import json

import pytest

from app.operations.interview_agent_acceptance import (
    POLICY_VERSION, REQUIRED_META_INTENTS, build_acceptance_report,
    validate_acceptance_report, _signature,
)
from test_interview_agent_acceptance import SECRET, _passing_dataset


def _checks(report):
    return {check["name"]: check for check in report["checks"]}


def test_one_easy_intent_cannot_pass_the_full_intent_gate():
    dataset = _passing_dataset()
    dataset["meta_intent_cases"] = [{"actual": "answering", "predicted": "answering"}] * 100
    report = build_acceptance_report(dataset, SECRET)
    assert not report["ready"]
    assert not _checks(report)["meta_intent_class_coverage"]["passed"]
    assert report["metrics"]["meta_intent_by_class"]["stop_interview"]["count"] == 0
    assert report["metrics"]["meta_intent_macro_f1"]["value"] < 0.95


def test_overall_f1_cannot_hide_poor_recall_of_one_intent():
    dataset = _passing_dataset()
    cases = dataset["meta_intent_cases"]
    mistakes = [case for case in cases if case["actual"] == "stop_interview"][:2]
    for case in mistakes:
        case["predicted"] = "answering"
    report = build_acceptance_report(dataset, SECRET)
    assert _checks(report)["meta_intent_macro_f1"]["passed"]
    assert _checks(report)["meta_intent_class_coverage"]["passed"]
    assert not _checks(report)["meta_intent_minimum_recall"]["passed"]
    assert report["metrics"]["meta_intent_by_class"]["stop_interview"]["recall"] == 0.8
    assert not report["ready"]


@pytest.mark.parametrize("mode", ["missing", "short", "duplicate_response", "duplicate_answer", "unauthorized_exit", "failed"])
def test_multi_turn_scenarios_require_coverage_and_actual_safe_results(mode):
    dataset = _passing_dataset()
    if mode == "missing":
        dataset["multiturn_cases"] = [case for case in dataset["multiturn_cases"] if case["scenario"] != "exit_cancel"]
    else:
        case = next(case for case in dataset["multiturn_cases"] if case["scenario"] == "long_conversation")
        if mode == "short":
            case["turn_count"] = 19
        elif mode == "failed":
            case["passed"] = False
        else:
            key = {"duplicate_response": "duplicate_responses", "duplicate_answer": "duplicate_answers",
                   "unauthorized_exit": "unauthorized_endings"}[mode]
            case[key] = 1
    report = build_acceptance_report(dataset, SECRET)
    assert not _checks(report)["multiturn_scenario_matrix"]["passed"]
    assert not report["ready"]


@pytest.mark.parametrize("section,record", [
    ("recovery_cases", {}),
    ("recovery_cases", {"interruption_seconds": 30, "lost_confirmed_frames": None, "duplicate_answers": 0}),
    ("recovery_cases", {"interruption_seconds": 30, "lost_confirmed_frames": False, "duplicate_answers": 0}),
    ("recovery_cases", {"interruption_seconds": 30, "lost_confirmed_frames": "0", "duplicate_answers": 0}),
    ("recovery_cases", {"interruption_seconds": 30, "lost_confirmed_frames": -1, "duplicate_answers": 1}),
    ("recovery_cases", {"interruption_seconds": 30, "lost_confirmed_frames": 0.5, "duplicate_answers": 0}),
    ("video_privacy_cases", {}),
    ("video_privacy_cases", {"video_recording_consented": "false", "video_upstream_bytes": 0}),
    ("followup_cases", {"relevant": True}),
    ("survey_cases", {"heard_me": 50, "relevant_followup": 5, "felt_like_dialogue": 5}),
    ("browser_cases", {"browser": "chrome", "passed": "true"}),
    ("meta_intent_cases", {"actual": "unknown", "predicted": "unknown"}),
    ("stt_cases", {"reference": "technical evidence", "technical_entities": []}),
    ("stt_cases", {"reference": " ", "hypothesis": "", "technical_entities": []}),
    ("stt_cases", {"reference": "technical evidence", "hypothesis": "", "technical_entities": [" "]}),
    ("multiturn_cases", {"scenario": "made_up", "turn_count": 20, "passed": True,
                         "duplicate_responses": 0, "duplicate_answers": 0, "unauthorized_endings": 0}),
])
def test_missing_or_coerced_measurements_are_rejected_before_signing(section, record):
    dataset = _passing_dataset()
    dataset[section][0] = record
    with pytest.raises(ValueError, match="missing, unknown or invalid measurements"):
        build_acceptance_report(dataset, SECRET)


@pytest.mark.parametrize("value", [None, "10", True, -1, float("nan"), float("inf")])
def test_invalid_latency_sample_cannot_be_discarded_to_improve_percentiles(value):
    dataset = _passing_dataset()
    dataset["latency_samples"]["stop_to_final_ms"].append(value)
    with pytest.raises(ValueError):
        build_acceptance_report(dataset, SECRET)


def test_missing_metric_and_extra_private_fields_fail_without_echoing_input():
    for mutate in (
        lambda data: data["latency_samples"].pop("stop_to_final_ms"),
        lambda data: data["stt_cases"][0].update({"private-candidate-name": "secret-candidate-value"}),
        lambda data: data.pop("multiturn_cases"),
    ):
        dataset = _passing_dataset()
        mutate(dataset)
        with pytest.raises(ValueError) as error:
            build_acceptance_report(dataset, SECRET)
        assert "private-candidate-name" not in str(error.value)
        assert "secret-candidate-value" not in str(error.value)


def test_recovery_requires_a_measured_thirty_second_interruption():
    dataset = _passing_dataset()
    dataset["recovery_cases"][0]["interruption_seconds"] = 29
    report = build_acceptance_report(dataset, SECRET)
    assert not report["ready"]
    assert not _checks(report)["thirty_second_recovery_integrity"]["passed"]


def test_insufficient_but_well_formed_measurements_produce_finite_failed_report():
    dataset = _passing_dataset()
    dataset["stt_cases"] = []
    dataset["meta_intent_cases"] = []
    dataset["recovery_cases"] = []
    dataset["multiturn_cases"] = []
    report = build_acceptance_report(dataset, SECRET)
    assert not report["ready"]
    assert report["metrics"]["stt"]["wer"] is None
    assert report["metrics"]["recovery"]["minimum_interruption_seconds"] is None
    json.dumps(report, allow_nan=False)


def test_v3_report_has_complete_safe_class_results_and_does_not_mutate_source():
    dataset = _passing_dataset()
    before = deepcopy(dataset)
    report = build_acceptance_report(dataset, SECRET)
    assert dataset == before
    assert report["policy_version"] == POLICY_VERSION == "realtime-interview-agent.acceptance.v3"
    assert validate_acceptance_report(report, SECRET)
    assert set(report["metrics"]["meta_intent_by_class"]) == REQUIRED_META_INTENTS
    assert all(value["recall"] == 1 for value in report["metrics"]["meta_intent_by_class"].values())
    assert "Redis API" not in json.dumps(report, ensure_ascii=False)


def test_even_authentic_old_v2_report_requires_new_acceptance_evidence():
    report = build_acceptance_report(_passing_dataset(), SECRET)
    report["policy_version"] = "realtime-interview-agent.acceptance.v2"
    report["signature"] = _signature(report, SECRET)
    assert not validate_acceptance_report(report, SECRET)


@pytest.mark.parametrize("report", [None, [], {"signature": None}, {"signature": "invalid", "metrics": float("nan")}])
def test_malformed_report_fails_closed_without_raising(report):
    assert not validate_acceptance_report(report, SECRET)
