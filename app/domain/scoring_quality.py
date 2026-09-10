"""Publish valid scores with recognition uncertainty as non-blocking metadata."""
from copy import deepcopy
from math import isfinite

from app.domain.speech_quality import minimum_reported_confidence, transcript_is_verified, transcript_identity

DISPUTE_FLAGS = {"transcription_ambiguity", "low_stt_confidence"}
RECOGNITION_NOTICE = "部分转写存在识别不确定性，已按可理解的回答内容评分；可按需回听或修正，不影响报告生成。"


def valid_score(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and 0 <= value <= 100


def recognition_context(answers):
    return [{"answer_id": item["id"], "confidence": item.get("stt_confidence"),
             "source": item.get("stt_confidence_source", "legacy_unverified"),
             "transcript_verified": transcript_is_verified(item)} for item in answers]


def project_evaluation(evaluation, answers=(), *, capture_verification=False):
    """Restore retained model scores, never invent a score or a human attestation."""
    if evaluation is None:
        return None
    result = deepcopy(evaluation)
    flags = set(result.get("review_flags") or [])
    warnings = set(result.get("quality_warnings") or [])
    if capture_verification:
        result["transcription_verifications"] = {item["id"]: transcript_identity(item)
            for item in answers if transcript_is_verified(item)}
    verifications = result.get("transcription_verifications") or {}
    unverified = [item for item in answers if not (transcript_is_verified(item)
        and verifications.get(item["id"]) == transcript_identity(item))]
    measured = minimum_reported_confidence(*(item.get("stt_confidence") for item in unverified))
    if any(item.get("recognition_disputed") for item in unverified):
        flags.add("transcription_ambiguity")
    if measured is not None and measured < .65:
        flags.add("low_stt_confidence")
        result["confidence"] = min(float(result.get("confidence", 1)), measured)
    for item in unverified:
        source = item.get("stt_confidence_source", "legacy_unverified")
        if source in {"unavailable", "partial", "legacy_unverified"}:
            warnings.add("stt_confidence_" + source)
    if not valid_score(result.get("score")) and valid_score(result.get("provisional_score")):
        result["score"] = result["provisional_score"]
        result["dimension_scores"] = deepcopy(result.get("provisional_dimension_scores", {}))
    available = valid_score(result.get("score"))
    result.update(review_flags=sorted(flags), quality_warnings=sorted(warnings),
                  score_status="available" if available else "unavailable",
                  recognition_warning=bool(flags & DISPUTE_FLAGS))
    if not available:
        result["score"] = None
        result["dimension_scores"] = {}
    result.pop("provisional_score", None)
    result.pop("provisional_dimension_scores", None)
    return result


def evaluation_answers(session, evaluation):
    ids = set(evaluation.get("evidence_answer_ids") or [evaluation.get("answer_id")])
    turns = {item["id"]: item for item in session.get("turns", [])}
    result = []
    for source in session.get("answers", []):
        if source.get("id") not in ids:
            continue
        answer = deepcopy(source)
        understanding = (turns.get(answer.get("turn_id"), {}).get("current_understanding") or {})
        if (answer.get("understanding_id") and understanding.get("understanding_id") == answer["understanding_id"]
            and understanding.get("ambiguities")):
            answer["recognition_disputed"] = True
        result.append(answer)
    return result


def project_report(report, session):
    """One numeric view for current and historical reports, without a write."""
    result = deepcopy(report)
    evaluations = {item["id"]: item for item in session.get("evaluation_revisions", [])}
    warning_ids = []
    unavailable = False
    restored = False
    for item in result.get("question_evaluations", []):
        original = evaluations.get(item.get("evaluation_id"))
        if original is None:
            unavailable = unavailable or not valid_score(item.get("score"))
            continue
        checked = project_evaluation(original, evaluation_answers(session, original))
        item["score_status"] = checked["score_status"]
        item["review_flags"] = checked["review_flags"]
        restored = restored or item.get("score") != checked["score"]
        item["score"] = checked["score"]
        item["recognition_warning"] = checked["recognition_warning"]
        item["quality_warnings"] = checked["quality_warnings"]
        item.pop("provisional_score", None)
        unavailable = unavailable or checked["score"] is None
        if checked["recognition_warning"]:
            warning_ids.append(item["answer_id"])
    result["pending_verification_answer_ids"] = []
    result["recognition_warning_answer_ids"] = warning_ids
    result["recognition_notice"] = RECOGNITION_NOTICE if warning_ids else None
    # 026 may have stored a null total. Rebuild only from actual retained scores
    # and the frozen plan's weights; never drop a missing question or assume 0.
    if not unavailable and (result.get("overall_score") is None or restored):
        plan = {item["question_snapshot_id"]: item for item in session.get("plan_snapshot", {}).get("question_snapshots", [])}
        totals = {}
        weighted = weight_sum = 0.0
        for item in result.get("question_evaluations", []):
            setting = plan.get(item.get("question_snapshot_id"))
            if setting is None:
                unavailable = True
                break
            weight = float(setting.get("weight", 0))
            weighted += item["score"] * weight
            weight_sum += weight
            values = totals.setdefault(setting.get("dimension", "general"), [0.0, 0.0])
            values[0] += item["score"] * weight
            values[1] += weight
        if not unavailable:
            result["overall_score"] = round(weighted / weight_sum) if weight_sum else 0
            result["dimension_scores"] = [{"dimension": name, "score": round(value[0] / value[1]) if value[1] else 0}
                                          for name, value in sorted(totals.items())]
    result["score_status"] = "unavailable" if unavailable else "available"
    if unavailable:
        result.update(overall_score=None, dimension_scores=[])
    result["risks"] = [risk for risk in result.get("risks", [])
                       if risk != "存在待回听核验的转写，核验与重评完成前不形成总分。"]
    if report.get("id") == session.get("current_report_id") and any(
        item.get("evaluation_status") != "completed" for item in session.get("answers", [])
    ):
        result.update(score_status="processing", overall_score=None, dimension_scores=[], strengths=[],
                      job_fit_level="manual_review", recommendation="manual_review")
    return result


def project_interview_scores(session):
    result = deepcopy(session)
    result["evaluation_revisions"] = [project_evaluation(item, evaluation_answers(session, item))
                                      for item in session.get("evaluation_revisions", [])]
    result["report_revisions"] = [project_report(item, session) for item in session.get("report_revisions", [])]
    return result
