"""Independent withdrawal retains real speech behind SQLite and media fences."""
from copy import deepcopy
from dataclasses import replace
import csv
import io

import pytest

from app.core.errors import ApiError
from app.core.prompt.candidate_exit import EXIT_CONFIRMATION_SPEECH, EXIT_CONFIRMATION_SPEECH_VERSION
from app.core.time import utc_now
from app.file_storage.local import LocalPrivateFileAdapter
from app.model_gateway.schemas import TranscriptSegment
from app.repositories.sqlite import SQLiteStore
from app.services.candidate_exit_confirmation import PreparedCandidateExit, text_hash
from app.services.conversation_delivery import record_selection, update_delivery
from app.services.evidence_media import DurableEvidenceMedia
from app.services.interviews import InterviewService
from app.services.retention import RetentionService
from test_preparation_fences_081 import _chain
from test_prepared_decision_binding import _payload


REQUEST = "我会用消息队列异步处理，再用请求标识去重。我想提前结束这场面试。"
REPLY = "确定结束本次面试。"


def _setup(tmp_path, monkeypatch, *, delivered=True):
    harness = _chain(tmp_path, monkeypatch)
    service = harness.stream.interviews
    text = REQUEST + REPLY
    harness.final = harness.final.model_copy(update={"text": text,
        "segments": [TranscriptSegment(text=text, confidence=.96, start_ms=0, end_ms=3000)]})
    context = {"version": EXIT_CONFIRMATION_SPEECH_VERSION, "interview_id": "iv_binding",
        "turn_id": "turn_binding", "capture_id": "capture_086", "question_act_id": "exit_086",
        "question": EXIT_CONFIRMATION_SPEECH, "request_text": REQUEST, "reply_text": REPLY,
        "decision": "confirm", "transcript_sha256": text_hash(text)}
    with service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        session["candidate_id"] = "candidate_086"
        session["plan_snapshot"] = {"question_snapshots": [{
            "question_snapshot_id": "snapshot_binding", "weight": 1, "dimension": "backend"}]}
        turn = session["turns"][0]
        turn["conversation_acts"].append({"act_id": "exit_086", "turn_id": turn["id"],
            "text": EXIT_CONFIRMATION_SPEECH, "prompt_version": EXIT_CONFIRMATION_SPEECH_VERSION})
        event = {"event_id": "exit_selection_086", "turn_id": turn["id"], "session_sequence": 1,
            "occurred_at": utc_now(), "type": "conversation.act.selected",
            "payload": {"act_id": "exit_086", "text": EXIT_CONFIRMATION_SPEECH, "approved": True}}
        session.setdefault("agent_events", []).append(event)
        record_selection(session, event)
        if delivered:
            assert update_delivery(session, event_id=event["event_id"], performance_id="exit_performance_086",
                                   status="delivered", updated_at=utc_now())
        tx.interview_sessions.update(session, expected_version=session["version"])
        tx.candidate_profiles.add({"id": "candidate_086", "organization_id": "org_default"})
    media = DurableEvidenceMedia(harness.store, storage=LocalPrivateFileAdapter(tmp_path / "private_086"))
    writer = media.open_writer(interview_id="iv_binding", turn_id="turn_binding", content_type="audio/pcm",
                              sample_rate_hz=16000, channels=1, fence=harness.owner.commit_fence())
    writer.append(b"\x00\x00" * 2048)
    checkpoint = writer.seal(complete=True).model_dump(mode="json")
    harness.payload = {**_payload(harness.final), "media_evidence": {"mode": "sealed_segments", "complete": True,
        **{key: checkpoint[key] for key in ("stream_id", "capture_revision", "sealed_segment_count",
                                           "last_sealed_frame_sequence", "ownership_epoch")}}}
    harness.context = context
    harness.service = service

    async def no_model(*args, **kwargs):
        raise AssertionError("Confirmed exit must not call a model")

    monkeypatch.setattr(service.conversation.gateway, "invoke", no_model)
    return harness


def _prepare(harness):
    return harness.service.prepare_streaming_exit("iv_binding", "turn_binding", harness.final,
        exit_confirmation=harness.context)


async def _submit(harness, prepared, **kwargs):
    return await harness.service.submit_confirmed_exit("iv_binding", kwargs.pop("payload", harness.payload),
        evidence_fence=kwargs.pop("fence", harness.owner.commit_fence()), prepared_exit=prepared, **kwargs)


@pytest.mark.anyio
async def test_confirmed_exit_retains_mixed_speech_without_an_answer_or_model_and_is_idempotent(tmp_path, monkeypatch):
    harness = _setup(tmp_path, monkeypatch)
    prepared = _prepare(harness)
    assert isinstance(prepared, PreparedCandidateExit)
    assert not hasattr(prepared, "understanding")
    assert not hasattr(prepared, "followup")
    before = harness.service.get_interview("iv_binding")
    harness.service.assert_prepared_streaming_exit(prepared, harness.final)
    assert harness.service.get_interview("iv_binding") == before
    result = await _submit(harness, prepared)
    assert result["exit_confirmed"] and result["accepted"] is False and result["answer"] is None
    assert result["evaluation"] is None
    assert result["next_turn_id"] is None
    assert any(item["type"] == "interview.candidate_input_completed" for item in result["events"])
    reloaded = InterviewService(SQLiteStore(str(harness.store.path)))
    session = reloaded.get_interview("iv_binding")
    turn = session["turns"][0]
    assert session["answers"] == [] and session["evaluation_revisions"] == []
    assert session["candidate_input_completion_reason"] == "candidate_requested"
    assert turn["status"] == "skipped" and turn.get("current_understanding") is None
    assert turn["exit_evidence"]["final_transcript"] == REQUEST + REPLY
    assert turn["exit_evidence"]["audio_uri"] == harness.payload["audio_uri"]
    assert turn["exit_evidence"]["media_evidence"] == harness.payload["media_evidence"]
    assert turn["exit_evidence"]["assessment_status"] == "not_evaluated"
    assert len(turn["utterances"]) == 1 and turn["utterances"][0]["authoritative"] is True
    assert turn["utterances"][0]["text"] == REQUEST + REPLY
    repeated = await _submit(harness, prepared)
    assert repeated["exit_confirmed"] and repeated["events"] == []
    assert harness.service.get_interview("iv_binding") == session
    with harness.service.persistence.transaction("org_default", read_only=True) as tx:
        work = tx.outbox.list()
    assert not any(item["kind"] == "answer.evaluate" for item in work)
    assert len([item for item in work if item["kind"] == "interview.report.generate"]) == 1


@pytest.mark.parametrize("change", ["confidence", "language", "segments", "provider", "text", "not_final"])
@pytest.mark.anyio
async def test_exit_preflight_rejects_any_changed_final_identity(tmp_path, monkeypatch, change):
    harness = _setup(tmp_path, monkeypatch)
    prepared = _prepare(harness)
    updates = {"confidence": .4} if change == "confidence" else {"language": "en-US"} if change == "language" \
        else {"segments": []} if change == "segments" else {"provider": harness.final.provider.model_copy(update={"model": "other"})} \
        if change == "provider" else {"text": harness.final.text + "我继续。"} if change == "text" else {"is_final": False}
    before = harness.service.get_interview("iv_binding")
    with pytest.raises(ApiError):
        harness.service.assert_prepared_streaming_exit(prepared, harness.final.model_copy(update=updates))
    assert harness.service.get_interview("iv_binding") == before


@pytest.mark.parametrize("state", ["selected", "playing", "interrupted", "failed", "unknown", "new_selection"])
@pytest.mark.anyio
async def test_exit_cannot_use_a_question_without_current_durable_delivery(tmp_path, monkeypatch, state):
    harness = _setup(tmp_path, monkeypatch, delivered=False)
    with harness.service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        receipt = session["turns"][0]["conversation_acts"][0]["selection_receipts"][0]
        if state == "new_selection":
            receipt["delivery"] = {"status": "delivered", "performance_id": "old"}
            event = {**session["agent_events"][0], "event_id": "newer_selection", "session_sequence": 2}
            session["agent_events"].append(event)
            record_selection(session, event)
        else:
            receipt["delivery"] = {"status": state, "performance_id": "performance"}
        tx.interview_sessions.update(session, expected_version=session["version"])
    with pytest.raises(ApiError) as error:
        _prepare(harness)
    assert error.value.code == "EXIT_CONFIRMATION_REQUIRED"


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["owner", "no_fence", "media_incomplete", "media_changed", "context", "takeover", "confirmation", "other_turn", "other_org"])
async def test_exit_transaction_rejects_invalid_authority_atomically(tmp_path, monkeypatch, failure):
    harness = _setup(tmp_path, monkeypatch)
    prepared = _prepare(harness)
    payload, kwargs = deepcopy(harness.payload), {}
    if failure == "owner":
        assert harness.coordinator.release(harness.owner)
    elif failure == "no_fence":
        kwargs["fence"] = None
    elif failure == "media_incomplete":
        payload["media_evidence"]["complete"] = False
    elif failure == "media_changed":
        payload["media_evidence"]["capture_revision"] += 1
    elif failure in {"context", "takeover"}:
        with harness.service.persistence.transaction("org_default") as tx:
            session = tx.interview_sessions.get("iv_binding")
            if failure == "context":
                session["turns"][0]["question_spoken_text"] = "另一道题"
            else:
                session["agent_runtime"] = {"floor": "human", "takeover": {"status": "active"}}
            tx.interview_sessions.update(session, expected_version=session["version"])
    elif failure == "confirmation":
        prepared = replace(prepared, exit_confirmation={**prepared.exit_confirmation, "decision": "continue"})
    elif failure == "other_turn":
        payload["turn_id"] = "other_turn"
    else:
        kwargs["organization_id"] = "other_org"
    before = harness.service.get_interview("iv_binding")
    with pytest.raises(ApiError):
        await _submit(harness, prepared, payload=payload, **kwargs)
    assert harness.service.get_interview("iv_binding") == before


@pytest.mark.anyio
async def test_exit_preserves_pending_prior_score_and_report_labels_unassessed_material(tmp_path, monkeypatch):
    harness = _setup(tmp_path, monkeypatch)
    with harness.service.persistence.transaction("org_default") as tx:
        session = tx.interview_sessions.get("iv_binding")
        previous = {**deepcopy(session["turns"][0]), "id": "earlier", "order": 0,
                    "root_turn_id": "earlier", "status": "evaluating", "conversation_acts": []}
        session["turns"].insert(0, previous)
        session["answers"] = [{"id": "earlier_answer", "turn_id": "earlier", "evaluation_status": "pending",
                               "final_transcript": "先前的原始回答"}]
        tx.interview_sessions.update(session, expected_version=session["version"])
    prepared = _prepare(harness)
    await _submit(harness, prepared)
    session = harness.service.get_interview("iv_binding")
    assert session["status"] == "in_progress" and session["candidate_input_completed_at"]
    assert session["turns"][0]["status"] == "evaluating"
    assert session["answers"] == [{"id": "earlier_answer", "turn_id": "earlier", "evaluation_status": "pending",
                                    "final_transcript": "先前的原始回答"}]
    report = harness.service.reports.build_report(session, trigger_reason="synthetic_086")
    material = report["unassessed_retained_evidence"]
    assert len(material) == 1 and material[0]["final_transcript"] == REQUEST + REPLY
    assert material[0]["assessment_status"] == "not_evaluated" and not material[0]["counts_toward_score"]
    assert report["overall_score"] is None and report["manual_review_required"] is True
    assert "不将其视作未作答或计零分" in report["scope_notice"]
    monkeypatch.setattr(harness.service.reports, "get_report", lambda *args: report)
    exported = harness.service.reports.export_report("iv_binding", export_format="csv", actor_id="synthetic")
    row = next(csv.DictReader(io.StringIO(exported["content"])))
    assert REQUEST in row["unassessed_retained_evidence"]


@pytest.mark.anyio
async def test_retention_includes_exit_only_recording_and_removes_retained_transcript(tmp_path, monkeypatch):
    harness = _setup(tmp_path, monkeypatch)
    prepared = _prepare(harness)
    payload = {**harness.payload, "audio_uri": "/media/synthetic_exit_086.wav"}
    await _submit(harness, prepared, payload=payload)
    service = RetentionService(harness.store, storage=LocalPrivateFileAdapter(tmp_path / "retention_private"))
    with service.persistence.transaction("org_default", read_only=True) as tx:
        scope = service._candidate_scope(tx, "candidate_086")
    assert "/media/synthetic_exit_086.wav" in scope["local_audio_uris"]
    session = harness.service.get_interview("iv_binding")
    purged = service._sanitize_interview(session, utc_now())
    assert "exit_evidence" not in purged["turns"][0]
    assert purged["turns"][0]["utterances"] == []
    assert REQUEST not in str(purged)
    assert session["turns"][0]["exit_evidence"]["final_transcript"] == REQUEST + REPLY
