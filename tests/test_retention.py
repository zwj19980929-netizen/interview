import asyncio
from datetime import datetime, timedelta, timezone
from io import BytesIO

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, StreamObject

from app.file_storage.provider import reset_private_file_storage_for_tests
from app.main import create_app
from app.persistence.provider import persistence_for
from app.repositories.provider import get_store, reset_store_for_tests
from app.services.retention import RetentionService
from app.workers.outbox import OutboxWorker


def _pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = StreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (Sensitive resume content) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_retention_preview_then_explicit_purge_removes_private_resume(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("INTERVIEWER_FILE_QUARANTINE_ROOT", str(tmp_path / "quarantine"))
    reset_private_file_storage_for_tests()
    reset_store_for_tests()
    api = TestClient(create_app())
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    candidate = api.post(
        "/api/v1/candidate-profiles",
        json={
            "name": "Expired Candidate",
            "email": "expired@example.com",
            "phone": "13800138000",
            "retention_expires_at": expired,
        },
    ).json()
    queued = api.post(
        f"/api/v1/candidate-profiles/{candidate['id']}/resumes",
        files={"file": ("resume.pdf", _pdf(), "application/pdf")},
    ).json()
    asyncio.run(OutboxWorker(get_store()).run_once())
    resume_id = queued["resume_document_id"]
    grant = api.post(
        f"/api/v1/candidate-profiles/{candidate['id']}/resumes/{resume_id}/content-url"
    ).json()["url"]
    assert api.get(grant).status_code == 200

    preview = api.post("/api/v1/admin/retention/run", json={"dry_run": True})
    assert preview.status_code == 200
    assert preview.json()["candidate_ids"] == [candidate["id"]]
    assert api.get(grant).status_code == 200

    purged = api.post(
        "/api/v1/admin/retention/run",
        json={"dry_run": False},
        headers={"X-Actor-Id": "privacy_admin"},
    )
    assert purged.status_code == 200
    candidate_after = api.get(f"/api/v1/candidate-profiles/{candidate['id']}").json()
    assert candidate_after["status"] == "retention_purged"
    assert candidate_after["email"] == ""
    assert api.get(grant).status_code == 404
    events = api.get("/api/v1/admin/audit-events").json()["items"]
    assert any(
        item["action"] == "retention.purge.completed" and item["actor_id"] == "privacy_admin"
        for item in events
    )


class _RecordingStorage:
    backend_name = "recording_test"

    def __init__(self, *object_keys: str) -> None:
        self.objects = {key: b"private" for key in object_keys}
        self.delete_calls = []

    def delete(self, object_key: str) -> None:
        self.delete_calls.append(object_key)
        self.objects.pop(object_key, None)


def _add_file_object(transaction, file_id: str, purpose: str, object_key: str, interview_id: str) -> None:
    transaction.file_objects.add(
        {
            "id": file_id,
            "organization_id": "org_default",
            "purpose": purpose,
            "status": "ready",
            "storage_backend": "recording_test",
            "object_key": object_key,
            "content_type": "application/octet-stream",
            "checksum": "sha256:sensitive",
            "byte_count": 7,
            "interview_id": interview_id,
            "turn_id": "turn_retention",
            "created_at": "2026-09-01T00:00:00+00:00",
            "updated_at": "2026-09-01T00:00:00+00:00",
        }
    )


def test_candidate_purge_deletes_all_evidence_revisions_and_livekit_object_once() -> None:
    reset_store_for_tests()
    store = get_store()
    persistence = persistence_for(store)
    object_keys = (
        "private/evidence-abandoned.pcm",
        "private/evidence-current.pcm",
        "private/answer.wav",
        "interview-captures/org_default/interview_retention/capture.mp4",
    )
    storage = _RecordingStorage(*object_keys)
    with persistence.transaction("org_default") as transaction:
        transaction.candidate_profiles.add(
            {
                "id": "candidate_retention",
                "organization_id": "org_default",
                "name": "Sensitive Candidate",
                "email_encrypted": "encrypted",
                "phone_encrypted": "encrypted",
                "email_masked": "s***@example.com",
                "phone_masked": "138****8000",
                "email_lookup_hash": "email-hash",
                "phone_lookup_hash": "phone-hash",
                "metadata": {"sensitive": True},
                "status": "active",
                "created_at": "2026-09-01T00:00:00+00:00",
                "updated_at": "2026-09-01T00:00:00+00:00",
            }
        )
        transaction.interview_sessions.add(
            {
                "id": "interview_retention",
                "organization_id": "org_default",
                "candidate_id": "candidate_retention",
                "candidate": {
                    "name": "Sensitive Candidate",
                    "email": "sensitive@example.com",
                    "phone": "13800138000",
                    "metadata": {"secret": "value"},
                },
                "plan_snapshot": {"candidate_profile_id": "candidate_retention"},
                "answers": [
                    {
                        "id": "answer_retention",
                        "audio_uri": "private-file://file_answer",
                        "raw_transcript": "sensitive raw transcript",
                        "final_transcript": "sensitive final transcript",
                        "transcript_revisions": [{"text": "sensitive"}],
                    }
                ],
                "turns": [
                    {
                        "id": "turn_retention",
                        "utterances": [{"text": "sensitive utterance"}],
                        "current_understanding": {"summary": "sensitive"},
                        "conversation_acts": [{"text": "sensitive echo"}],
                    }
                ],
                "evaluation_revisions": [{"evidence": "sensitive"}],
                "report_revisions": [{"summary": "sensitive"}],
                "current_report_id": "report_sensitive",
                "report_id": "report_sensitive",
                "agent_events": [{"payload": {"transcript": "sensitive"}}],
                "agent_runtime": {
                    "authoritative_media_binding": {
                        "candidate_identity": "candidate:sensitive"
                    },
                    "takeover": {"actor_id": "human_sensitive"},
                    "processed_signal_keys": ["sensitive-key"],
                    "active_performance_id": "performance_sensitive",
                },
                "created_at": "2026-09-01T00:00:00+00:00",
                "updated_at": "2026-09-01T00:00:00+00:00",
            }
        )
        transaction.interview_media_captures.add(
            {
                "id": "capture_retention",
                "organization_id": "org_default",
                "interview_id": "interview_retention",
                "candidate_id": "candidate_retention",
                "status": "completed",
                "participant_identity": "candidate:sensitive",
                "connection_id": "connection_sensitive",
                "requested_scopes": ["audio_recording", "video_recording"],
                "consented_scopes": ["audio_recording", "video_recording"],
                "egress_id": "egress_sensitive",
                "object_key": object_keys[3],
                "private_uri": "private-media-capture://capture_retention",
                "content_hash": "sha256:sensitive",
                "byte_count": 7,
                "provider_result": {"file_results": [{"location": "sensitive"}]},
                "created_at": "2026-09-01T00:00:00+00:00",
                "updated_at": "2026-09-01T00:00:00+00:00",
            }
        )
        transaction.evidence_media_streams.add(
            {
                "id": "stream_retention",
                "organization_id": "org_default",
                "interview_id": "interview_retention",
                "turn_id": "turn_retention",
                "status": "recovered",
                "capture_revision": 2,
                "last_sealed_ordinal": 1,
                "last_sealed_frame_sequence": 2,
                "sealed_byte_count": 14,
                "complete": True,
                "recovered_audio_uri": "private-file://file_answer",
                "recovered_byte_count": 7,
                "recovered_source_pcm_byte_count": 7,
                "abandoned_captures": [{"capture_revision": 1, "sealed_byte_count": 7}],
                "created_at": "2026-09-01T00:00:00+00:00",
                "updated_at": "2026-09-01T00:00:00+00:00",
            }
        )
        for segment_id, revision, file_id in (
            ("segment_abandoned", 1, "file_evidence_abandoned"),
            ("segment_current", 2, "file_evidence_current"),
        ):
            transaction.evidence_media_segments.add(
                {
                    "id": segment_id,
                    "organization_id": "org_default",
                    "stream_id": "stream_retention",
                    "capture_revision": revision,
                    "interview_id": "interview_retention",
                    "turn_id": "turn_retention",
                    "file_id": file_id,
                    "created_at": "2026-09-01T00:00:00+00:00",
                    "updated_at": "2026-09-01T00:00:00+00:00",
                }
            )
        _add_file_object(
            transaction,
            "file_evidence_abandoned",
            "candidate_evidence_segment",
            object_keys[0],
            "interview_retention",
        )
        _add_file_object(
            transaction,
            "file_evidence_current",
            "candidate_evidence_segment",
            object_keys[1],
            "interview_retention",
        )
        _add_file_object(
            transaction,
            "file_answer",
            "candidate_answer_audio",
            object_keys[2],
            "interview_retention",
        )

    service = RetentionService(store, persistence=persistence, storage=storage)
    first = service.purge_candidate(
        "candidate_retention", actor_id="privacy_admin"
    )
    with persistence.transaction("org_default") as transaction:
        version_after_first = transaction.candidate_profiles.get(
            "candidate_retention"
        )["version"]
    second = service.purge_candidate(
        "candidate_retention", actor_id="privacy_admin"
    )

    assert first["changed"] is True
    assert second["changed"] is False
    assert storage.objects == {}
    assert sorted(storage.delete_calls) == sorted(object_keys)
    with persistence.transaction("org_default") as transaction:
        candidate = transaction.candidate_profiles.get("candidate_retention")
        assert candidate["version"] == version_after_first
        assert candidate["status"] == "retention_purged"
        capture = transaction.interview_media_captures.get("capture_retention")
        assert capture["status"] == "retention_purged"
        assert capture["object_key"] is None
        assert capture["private_uri"] is None
        stream = transaction.evidence_media_streams.get("stream_retention")
        assert stream["status"] == "retention_purged"
        assert stream["abandoned_captures"] == []
        assert transaction.evidence_media_segments.list() == []
        for file_id in (
            "file_evidence_abandoned",
            "file_evidence_current",
            "file_answer",
        ):
            file_object = transaction.file_objects.get(file_id)
            assert file_object["status"] == "deleted"
            assert file_object["object_key"] is None
            assert file_object["checksum"] is None
        interview = transaction.interview_sessions.get("interview_retention")
        assert interview["turns"][0]["utterances"] == []
        assert interview["agent_events"] == []
        events = [
            item
            for item in transaction.audit_events.list()
            if item["action"] == "retention.candidate_evidence_purged"
        ]
        assert len(events) == 1
        assert events[0]["metadata"]["evidence_segment_count"] == 2
        assert events[0]["metadata"]["external_object_count"] == 4
        assert all(
            value.startswith("sha256:")
            for value in events[0]["metadata"]["object_key_hashes"]
        )


def test_abandoned_evidence_gc_is_idempotent_and_preserves_current_revision() -> None:
    reset_store_for_tests()
    store = get_store()
    persistence = persistence_for(store)
    storage = _RecordingStorage("private/old.pcm", "private/current.pcm")
    with persistence.transaction("org_default") as transaction:
        transaction.evidence_media_streams.add(
            {
                "id": "stream_gc",
                "organization_id": "org_default",
                "interview_id": "interview_gc",
                "turn_id": "turn_gc",
                "status": "open",
                "capture_revision": 2,
                "created_at": "2026-09-01T00:00:00+00:00",
                "updated_at": "2026-09-01T00:00:00+00:00",
            }
        )
        for segment_id, revision, file_id in (
            ("segment_gc_old", 1, "file_gc_old"),
            ("segment_gc_current", 2, "file_gc_current"),
        ):
            transaction.evidence_media_segments.add(
                {
                    "id": segment_id,
                    "organization_id": "org_default",
                    "stream_id": "stream_gc",
                    "capture_revision": revision,
                    "interview_id": "interview_gc",
                    "turn_id": "turn_gc",
                    "file_id": file_id,
                    "created_at": "2026-09-01T00:00:00+00:00",
                    "updated_at": "2026-09-01T00:00:00+00:00",
                }
            )
        _add_file_object(
            transaction,
            "file_gc_old",
            "candidate_evidence_segment",
            "private/old.pcm",
            "interview_gc",
        )
        _add_file_object(
            transaction,
            "file_gc_current",
            "candidate_evidence_segment",
            "private/current.pcm",
            "interview_gc",
        )

    service = RetentionService(store, persistence=persistence, storage=storage)
    first = service.run_evidence_media_gc(actor_id="privacy_gc")
    second = service.run_evidence_media_gc(actor_id="privacy_gc")

    assert first["segment_count"] == 1
    assert first["file_object_count"] == 1
    assert second == {
        "segment_count": 0,
        "file_object_count": 0,
        "audit_event_id": None,
    }
    assert storage.delete_calls == ["private/old.pcm"]
    assert set(storage.objects) == {"private/current.pcm"}
    with persistence.transaction("org_default") as transaction:
        assert transaction.evidence_media_segments.get("segment_gc_old") is None
        assert transaction.evidence_media_segments.get("segment_gc_current") is not None
        assert transaction.file_objects.get("file_gc_old")["status"] == "deleted"
        assert transaction.file_objects.get("file_gc_current")["status"] == "ready"
        events = [
            item
            for item in transaction.audit_events.list()
            if item["action"] == "retention.evidence_media_gc.completed"
        ]
        assert len(events) == 1
