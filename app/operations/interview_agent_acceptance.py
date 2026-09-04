"""Offline, signed production acceptance report for the real-time agent.

The input may contain de-identified transcripts and annotations. The emitted
report contains only aggregate metrics, sample counts and a dataset hash.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


POLICY_VERSION = "realtime-interview-agent.acceptance.v2"
MAX_REPORT_AGE_DAYS = 30
SUPPORTED_DESKTOP_BROWSERS = frozenset({"chrome", "edge", "safari"})
MINIMUM_BROWSER_CASES_PER_BROWSER = 10
LATENCY_LIMITS = {
    "local_microphone_feedback_ms": 100,
    "server_audio_confirmation_ms": 300,
    "partial_first_token_ms": 800,
    "stop_to_final_ms": 1_200,
    "barge_in_mute_ms": 200,
    "final_to_first_audio_realtime_ms": 1_500,
    "final_to_first_audio_cascade_ms": 2_500,
    "avatar_viseme_drift_ms": 80,
}
MINIMUM_SAMPLES = {
    "latency": 100,
    "stt": 100,
    "meta_intent": 100,
    "capability_coverage": 100,
    "followup": 100,
    "recovery": 20,
    "video_privacy": 20,
    "survey": 30,
}
REQUIRED_CHECK_NAMES = frozenset(
    {
        *LATENCY_LIMITS,
        "avatar_fps",
        "avatar_freeze_ms",
        "mandarin_technical_wer",
        "english_technical_entity_recall",
        "meta_intent_macro_f1",
        "capability_coverage_macro_f1",
        "irrelevant_followup_rate",
        "followup_policy_violations",
        "thirty_second_recovery_integrity",
        "unconsented_video_upstream_bytes",
        "survey_heard_me",
        "survey_relevant_followup",
        "survey_felt_like_dialogue",
        "desktop_browser_matrix",
    }
)


def build_acceptance_report(dataset: Dict[str, Any], secret: str) -> Dict[str, Any]:
    if len(secret.encode("utf-8")) < 32:
        raise ValueError("acceptance report secret must contain at least 32 bytes")
    release_scope = _release_scope(dataset.get("release_scope"))
    canonical_dataset = _canonical(dataset)
    checks: List[Dict[str, Any]] = []
    metrics: Dict[str, Any] = {}
    latency = dataset.get("latency_samples") or {}
    for name, limit in LATENCY_LIMITS.items():
        values = _numbers(latency.get(name) or [])
        p95 = _percentile(values, 0.95)
        metrics[name] = {"count": len(values), "p95": p95}
        checks.append(
            _check(
                name,
                len(values) >= MINIMUM_SAMPLES["latency"]
                and p95 is not None
                and p95 < limit,
                len(values),
                {"p95_lt": limit},
            )
        )

    avatar_fps = _numbers(latency.get("avatar_fps") or [])
    fps_p05 = _percentile(avatar_fps, 0.05)
    metrics["avatar_fps"] = {"count": len(avatar_fps), "p05": fps_p05}
    checks.append(
        _check(
            "avatar_fps",
            len(avatar_fps) >= MINIMUM_SAMPLES["latency"]
            and fps_p05 is not None
            and fps_p05 >= 30,
            len(avatar_fps),
            {"p05_gte": 30},
        )
    )
    freezes = _numbers(latency.get("avatar_freeze_ms") or [])
    freeze_max = max(freezes) if freezes else None
    metrics["avatar_freeze_ms"] = {"count": len(freezes), "max": freeze_max}
    checks.append(
        _check(
            "avatar_freeze_ms",
            len(freezes) >= MINIMUM_SAMPLES["latency"]
            and freeze_max is not None
            and freeze_max <= 500,
            len(freezes),
            {"max_lte": 500},
        )
    )

    stt_cases = list(dataset.get("stt_cases") or [])
    total_edits = 0
    total_tokens = 0
    entity_hits = 0
    entity_total = 0
    for case in stt_cases:
        reference = _tokens(str(case.get("reference") or ""))
        hypothesis = _tokens(str(case.get("hypothesis") or ""))
        total_edits += _edit_distance(reference, hypothesis)
        total_tokens += len(reference)
        normalized_hypothesis = str(case.get("hypothesis") or "").lower()
        for entity in case.get("technical_entities") or []:
            entity_total += 1
            entity_hits += int(str(entity).lower() in normalized_hypothesis)
    wer = total_edits / total_tokens if total_tokens else math.inf
    entity_recall = entity_hits / entity_total if entity_total else 0.0
    metrics["stt"] = {
        "count": len(stt_cases),
        "wer": round(wer, 6),
        "technical_entity_recall": round(entity_recall, 6),
        "technical_entity_count": entity_total,
    }
    checks.extend(
        [
            _check(
                "mandarin_technical_wer",
                len(stt_cases) >= MINIMUM_SAMPLES["stt"] and wer < 0.12,
                len(stt_cases),
                {"lt": 0.12},
            ),
            _check(
                "english_technical_entity_recall",
                len(stt_cases) >= MINIMUM_SAMPLES["stt"]
                and entity_total > 0
                and entity_recall >= 0.95,
                entity_total,
                {"gte": 0.95},
            ),
        ]
    )

    for key, threshold in (
        ("meta_intent_cases", 0.95),
        ("capability_coverage_cases", 0.85),
    ):
        cases = list(dataset.get(key) or [])
        score = _macro_f1(cases)
        metric_name = key.removesuffix("_cases") + "_macro_f1"
        minimum_key = "meta_intent" if key.startswith("meta") else "capability_coverage"
        metrics[metric_name] = {"count": len(cases), "value": score}
        checks.append(
            _check(
                metric_name,
                len(cases) >= MINIMUM_SAMPLES[minimum_key]
                and score >= threshold,
                len(cases),
                {"gte": threshold},
            )
        )

    followups = list(dataset.get("followup_cases") or [])
    irrelevant = sum(not bool(item.get("relevant")) for item in followups)
    violations = sum(
        bool(item.get("safety_violation"))
        or bool(item.get("answer_leak"))
        or bool(item.get("sensitive_attribute"))
        or bool(item.get("over_budget"))
        for item in followups
    )
    irrelevant_rate = irrelevant / len(followups) if followups else 1.0
    metrics["followup"] = {
        "count": len(followups),
        "irrelevant_rate": round(irrelevant_rate, 6),
        "violations": violations,
    }
    checks.extend(
        [
            _check(
                "irrelevant_followup_rate",
                len(followups) >= MINIMUM_SAMPLES["followup"]
                and irrelevant_rate < 0.05,
                len(followups),
                {"lt": 0.05},
            ),
            _check(
                "followup_policy_violations",
                len(followups) >= MINIMUM_SAMPLES["followup"] and violations == 0,
                len(followups),
                {"eq": 0},
            ),
        ]
    )

    recoveries = list(dataset.get("recovery_cases") or [])
    lost_frames = sum(int(item.get("lost_confirmed_frames") or 0) for item in recoveries)
    duplicate_answers = sum(int(item.get("duplicate_answers") or 0) for item in recoveries)
    metrics["recovery"] = {
        "count": len(recoveries),
        "lost_confirmed_frames": lost_frames,
        "duplicate_answers": duplicate_answers,
    }
    checks.append(
        _check(
            "thirty_second_recovery_integrity",
            len(recoveries) >= MINIMUM_SAMPLES["recovery"]
            and lost_frames == 0
            and duplicate_answers == 0,
            len(recoveries),
            {"lost_frames_eq": 0, "duplicate_answers_eq": 0},
        )
    )

    privacy = [
        item
        for item in (dataset.get("video_privacy_cases") or [])
        if not bool(item.get("video_recording_consented"))
    ]
    video_bytes = sum(int(item.get("video_upstream_bytes") or 0) for item in privacy)
    metrics["video_privacy"] = {
        "count": len(privacy),
        "video_upstream_bytes": video_bytes,
    }
    checks.append(
        _check(
            "unconsented_video_upstream_bytes",
            len(privacy) >= MINIMUM_SAMPLES["video_privacy"] and video_bytes == 0,
            len(privacy),
            {"eq": 0},
        )
    )

    surveys = list(dataset.get("survey_cases") or [])
    for field in ("heard_me", "relevant_followup", "felt_like_dialogue"):
        values = _numbers(item.get(field) for item in surveys)
        average = sum(values) / len(values) if values else 0.0
        metrics["survey_%s" % field] = {
            "count": len(values),
            "average": round(average, 6),
        }
        checks.append(
            _check(
                "survey_%s" % field,
                len(values) >= MINIMUM_SAMPLES["survey"] and average >= 4.0,
                len(values),
                {"average_gte": 4.0},
            )
        )

    browser_cases = list(dataset.get("browser_cases") or [])
    browser_counts = {
        browser: sum(
            str(item.get("browser") or "").strip().lower() == browser
            for item in browser_cases
            if isinstance(item, dict)
        )
        for browser in sorted(SUPPORTED_DESKTOP_BROWSERS)
    }
    browser_matrix_passed = bool(browser_cases) and all(
        isinstance(item, dict)
        and str(item.get("browser") or "").strip().lower()
        in SUPPORTED_DESKTOP_BROWSERS
        and item.get("passed") is True
        for item in browser_cases
    ) and all(
        count >= MINIMUM_BROWSER_CASES_PER_BROWSER
        for count in browser_counts.values()
    )
    metrics["desktop_browser_matrix"] = {
        "count": len(browser_cases),
        "by_browser": browser_counts,
    }
    checks.append(
        _check(
            "desktop_browser_matrix",
            browser_matrix_passed,
            len(browser_cases),
            {
                "required_browsers": sorted(SUPPORTED_DESKTOP_BROWSERS),
                "minimum_cases_per_browser": MINIMUM_BROWSER_CASES_PER_BROWSER,
                "all_cases_passed": True,
            },
        )
    )

    report: Dict[str, Any] = {
        "policy_version": POLICY_VERSION,
        "release_scope": release_scope,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dataset_sha256": hashlib.sha256(canonical_dataset).hexdigest(),
        "ready": all(item["passed"] for item in checks),
        "metrics": metrics,
        "checks": checks,
    }
    report["signature"] = _signature(report, secret)
    return report


def validate_acceptance_report(
    report: Dict[str, Any],
    secret: str,
    *,
    now: datetime | None = None,
    expected_deployment_id: str | None = None,
    expected_release_revision: str | None = None,
) -> bool:
    if len(secret.encode("utf-8")) < 32:
        return False
    supplied = str(report.get("signature") or "")
    if not supplied or not hmac.compare_digest(supplied, _signature(report, secret)):
        return False
    if report.get("policy_version") != POLICY_VERSION or report.get("ready") is not True:
        return False
    try:
        release_scope = _release_scope(report.get("release_scope"))
    except ValueError:
        return False
    if (
        expected_deployment_id is not None
        and release_scope["deployment_id"] != expected_deployment_id
    ):
        return False
    if (
        expected_release_revision is not None
        and release_scope["release_revision"] != expected_release_revision
    ):
        return False
    checks = report.get("checks")
    if not isinstance(checks, list) or not checks:
        return False
    names = [str(item.get("name") or "") for item in checks if isinstance(item, dict)]
    if len(names) != len(checks) or len(names) != len(set(names)):
        return False
    if frozenset(names) != REQUIRED_CHECK_NAMES:
        return False
    if not all(item.get("passed") is True for item in checks):
        return False
    try:
        generated = datetime.fromisoformat(str(report["generated_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError):
        return False
    current = now or datetime.now(timezone.utc)
    return generated.tzinfo is not None and current - timedelta(days=MAX_REPORT_AGE_DAYS) <= generated <= current + timedelta(minutes=5)


def load_and_validate_acceptance_report(
    path: str,
    secret: str,
    *,
    expected_deployment_id: str | None = None,
    expected_release_revision: str | None = None,
) -> bool:
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(report, dict) and validate_acceptance_report(
        report,
        secret,
        expected_deployment_id=expected_deployment_id,
        expected_release_revision=expected_release_revision,
    )


def _release_scope(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "deployment_id",
        "release_revision",
    }:
        raise ValueError(
            "release_scope must contain only deployment_id and release_revision"
        )
    result = {
        key: str(value.get(key) or "").strip()
        for key in ("deployment_id", "release_revision")
    }
    if any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}", item)
        for item in result.values()
    ):
        raise ValueError(
            "release scope values must use 1 to 160 deployment-safe characters"
        )
    return result


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _signature(report: Dict[str, Any], secret: str) -> str:
    unsigned = {key: value for key, value in report.items() if key != "signature"}
    return "hmac-sha256:%s" % hmac.new(
        secret.encode("utf-8"), _canonical(unsigned), hashlib.sha256
    ).hexdigest()


def _check(name: str, passed: bool, samples: int, threshold: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": name, "passed": bool(passed), "samples": samples, "threshold": threshold}


def _numbers(values: Iterable[Any]) -> List[float]:
    result = []
    for value in values:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric) and numeric >= 0:
            result.append(numeric)
    return result


def _percentile(values: List[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * quantile) - 1)], 6)


def _tokens(text: str) -> List[str]:
    return re.findall(r"[\u3400-\u9fff]|[a-z0-9_+#.-]+", text.lower())


def _edit_distance(left: List[str], right: List[str]) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_token in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_token in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + int(left_token != right_token),
                )
            )
        previous = current
    return previous[-1]


def _macro_f1(cases: List[Dict[str, Any]]) -> float:
    labels = sorted(
        {str(item.get("actual")) for item in cases}
        | {str(item.get("predicted")) for item in cases}
    )
    if not cases or not labels:
        return 0.0
    scores = []
    for label in labels:
        true_positive = sum(
            str(item.get("actual")) == label and str(item.get("predicted")) == label
            for item in cases
        )
        false_positive = sum(
            str(item.get("actual")) != label and str(item.get("predicted")) == label
            for item in cases
        )
        false_negative = sum(
            str(item.get("actual")) == label and str(item.get("predicted")) != label
            for item in cases
        )
        denominator = 2 * true_positive + false_positive + false_negative
        scores.append((2 * true_positive / denominator) if denominator else 0.0)
    return round(sum(scores) / len(scores), 6)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a signed real-time interview acceptance report")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    secret_source = parser.add_mutually_exclusive_group()
    secret_source.add_argument(
        "--secret-env",
        default="INTERVIEWER_AGENT_ACCEPTANCE_REPORT_SECRET",
        help="environment variable containing the signing secret",
    )
    secret_source.add_argument(
        "--secret-file",
        help="path to a private file containing the signing secret",
    )
    arguments = parser.parse_args()
    if arguments.secret_file:
        secret = Path(arguments.secret_file).read_text(encoding="utf-8").strip()
    else:
        secret = os.getenv(arguments.secret_env, "")
    if not secret:
        parser.error("acceptance signing secret is missing")
    dataset = json.loads(Path(arguments.input).read_text(encoding="utf-8"))
    if not isinstance(dataset, dict):
        parser.error("acceptance input must be a JSON object")
    report = build_acceptance_report(dataset, secret)
    output = Path(arguments.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".%s." % output.name,
        dir=str(output.parent),
        text=True,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    main()
