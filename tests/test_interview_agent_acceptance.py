from copy import deepcopy
from datetime import datetime, timedelta, timezone

from app.core.interview_agent_metrics import (
    CANDIDATE_INTERVIEW_AGENT_METRICS,
    INTERNAL_INTERVIEW_AGENT_METRICS,
    INTERVIEW_AGENT_METRICS,
    INTERVIEW_AGENT_STAGE_METRICS,
    InterviewAgentMetrics,
)
from app.core.interview_agent_release import realtime_agent_release_status
from app.operations.interview_agent_acceptance import (
    build_acceptance_report,
    validate_acceptance_report,
)


SECRET = "acceptance-test-secret-that-is-long-enough-123456"


def _passing_dataset() -> dict:
    latency = {
        "local_microphone_feedback_ms": [50] * 100,
        "server_audio_confirmation_ms": [150] * 100,
        "partial_first_token_ms": [400] * 100,
        "stop_to_final_ms": [700] * 100,
        "barge_in_mute_ms": [80] * 100,
        "final_to_first_audio_realtime_ms": [900] * 100,
        "final_to_first_audio_cascade_ms": [1_400] * 100,
        "avatar_viseme_drift_ms": [35] * 100,
        "avatar_fps": [45] * 100,
        "avatar_freeze_ms": [32] * 100,
    }
    stt = [
        {
            "reference": "我使用 Redis API 做幂等键",
            "hypothesis": "我使用 Redis API 做幂等键",
            "technical_entities": ["Redis", "API"],
        }
        for _ in range(100)
    ]
    classifications = [
        {"actual": "repeat", "predicted": "repeat"},
        {"actual": "answer", "predicted": "answer"},
    ] * 50
    return {
        "release_scope": {
            "deployment_id": "prod-us-west-2",
            "release_revision": "git-deadbeef",
        },
        "latency_samples": latency,
        "stt_cases": stt,
        "meta_intent_cases": classifications,
        "capability_coverage_cases": classifications,
        "followup_cases": [{"relevant": True} for _ in range(100)],
        "recovery_cases": [
            {"lost_confirmed_frames": 0, "duplicate_answers": 0}
            for _ in range(20)
        ],
        "video_privacy_cases": [
            {"video_recording_consented": False, "video_upstream_bytes": 0}
            for _ in range(20)
        ],
        "survey_cases": [
            {"heard_me": 5, "relevant_followup": 4, "felt_like_dialogue": 4}
            for _ in range(30)
        ],
        "browser_cases": [
            {"browser": browser, "passed": True}
            for browser in ("chrome", "edge", "safari")
            for _ in range(10)
        ],
    }


def test_signed_acceptance_report_gates_every_hard_metric_without_raw_data() -> None:
    dataset = _passing_dataset()
    report = build_acceptance_report(dataset, SECRET)

    assert report["ready"] is True
    assert validate_acceptance_report(report, SECRET) is True
    assert validate_acceptance_report(
        report,
        SECRET,
        expected_deployment_id="prod-us-west-2",
        expected_release_revision="git-deadbeef",
    ) is True
    assert "reference" not in str(report)
    assert report["dataset_sha256"]
    assert report["metrics"]["stt"]["wer"] == 0
    assert report["metrics"]["video_privacy"]["video_upstream_bytes"] == 0

    tampered = deepcopy(report)
    tampered["metrics"]["stt"]["wer"] = 0.5
    assert validate_acceptance_report(tampered, SECRET) is False
    assert validate_acceptance_report(
        report,
        SECRET,
        expected_deployment_id="another-deployment",
    ) is False

    incomplete = deepcopy(report)
    incomplete["checks"] = incomplete["checks"][:-1]
    # Re-signing an incomplete checklist must not bypass the policy-defined set.
    incomplete = build_acceptance_report(_passing_dataset(), SECRET) | {
        "checks": incomplete["checks"]
    }
    from app.operations.interview_agent_acceptance import _signature

    incomplete["signature"] = _signature(incomplete, SECRET)
    assert validate_acceptance_report(incomplete, SECRET) is False


def test_acceptance_report_fails_closed_for_bad_latency_or_stale_report() -> None:
    dataset = _passing_dataset()
    dataset["latency_samples"]["barge_in_mute_ms"][-10:] = [500] * 10
    failed = build_acceptance_report(dataset, SECRET)
    assert failed["ready"] is False
    assert validate_acceptance_report(failed, SECRET) is False

    valid = build_acceptance_report(_passing_dataset(), SECRET)
    future_now = datetime.now(timezone.utc) + timedelta(days=31)
    assert validate_acceptance_report(valid, SECRET, now=future_now) is False


def test_acceptance_report_requires_every_supported_desktop_browser() -> None:
    dataset = _passing_dataset()
    dataset["browser_cases"] = [
        item for item in dataset["browser_cases"] if item["browser"] != "safari"
    ]
    report = build_acceptance_report(dataset, SECRET)

    assert report["ready"] is False
    browser_check = next(
        item for item in report["checks"] if item["name"] == "desktop_browser_matrix"
    )
    assert browser_check["passed"] is False
    assert browser_check["samples"] == 20


def test_process_metrics_accept_only_fixed_label_free_vocabulary() -> None:
    metrics = InterviewAgentMetrics(capacity=3)
    metrics.observe("barge_in_mute_ms", 90)
    metrics.observe("barge_in_mute_ms", 100)
    metrics.observe("barge_in_mute_ms", 110)
    metrics.observe("barge_in_mute_ms", 120)
    snapshot = metrics.snapshot()["barge_in_mute_ms"]
    assert snapshot == {"count": 3, "p50": 110.0, "p95": 120.0, "max": 120.0}

    try:
        metrics.observe("candidate_name", 1)
    except ValueError as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("arbitrary PII-bearing metric names must be rejected")


def test_candidate_and_internal_metric_vocabularies_are_disjoint() -> None:
    assert INTERNAL_INTERVIEW_AGENT_METRICS == INTERVIEW_AGENT_STAGE_METRICS | {
        "evidence_owner_renew_scheduler_lag_ms",
        "evidence_owner_renew_db_latency_ms",
        "evidence_owner_renew_success",
        "turn_decision_cancelled",
        "stt_recognition_opened",
        "stt_recognition_finished",
        "stt_preview_prepared",
        "stt_preview_final_revised",
    }
    assert CANDIDATE_INTERVIEW_AGENT_METRICS.isdisjoint(
        INTERNAL_INTERVIEW_AGENT_METRICS
    )
    assert INTERVIEW_AGENT_METRICS == (
        CANDIDATE_INTERVIEW_AGENT_METRICS
        | INTERNAL_INTERVIEW_AGENT_METRICS
    )


def test_production_release_gate_requires_matching_report_and_explicit_org(
    monkeypatch, tmp_path
) -> None:
    report_path = tmp_path / "acceptance.json"
    report_path.write_text(
        __import__("json").dumps(build_acceptance_report(_passing_dataset(), SECRET)),
        encoding="utf-8",
    )
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.setenv("INTERVIEWER_DEPLOYMENT_ID", "prod-us-west-2")
    monkeypatch.setenv("INTERVIEWER_RELEASE_REVISION", "git-deadbeef")
    monkeypatch.setenv("INTERVIEWER_AGENT_ACCEPTANCE_REPORT", str(report_path))
    monkeypatch.setenv("INTERVIEWER_AGENT_ACCEPTANCE_REPORT_SECRET", SECRET)
    monkeypatch.setenv(
        "INTERVIEWER_REALTIME_AGENT_ENABLED_ORGANIZATIONS", "org_enabled"
    )

    assert realtime_agent_release_status("org_enabled")["ready"] is True
    disabled = realtime_agent_release_status("org_disabled")
    assert disabled["ready"] is False
    assert disabled["organization_enabled"] is False

    monkeypatch.setenv(
        "INTERVIEWER_REALTIME_AGENT_ENABLED_ORGANIZATIONS", "*"
    )
    wildcard = realtime_agent_release_status("org_enabled")
    assert wildcard["ready"] is False
    assert wildcard["organization_enabled"] is False

    monkeypatch.setenv(
        "INTERVIEWER_REALTIME_AGENT_ENABLED_ORGANIZATIONS", "org_enabled"
    )
    monkeypatch.setenv("INTERVIEWER_RELEASE_REVISION", "git-another")
    mismatch = realtime_agent_release_status("org_enabled")
    assert mismatch["ready"] is False
    assert mismatch["acceptance_report_ready"] is False


def test_acceptance_release_scope_rejects_unsafe_identifiers() -> None:
    dataset = _passing_dataset()
    dataset["release_scope"]["release_revision"] = "git-deadbeef\nforged"

    try:
        build_acceptance_report(dataset, SECRET)
    except ValueError as exc:
        assert "deployment-safe" in str(exc)
    else:
        raise AssertionError("control characters must not enter a release scope")
