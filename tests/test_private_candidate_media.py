from datetime import datetime, timezone

import pytest

from app.adapters.private_media import (
    PrivateMediaStorage,
    media_recording_storage,
    read_managed_audio,
)
from app.file_storage.local import LocalPrivateFileAdapter
from app.file_storage.signing import FileAccessSigner
from app.domain.appointment_admission import AppointmentAdmission
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore
from app.services.review import EnterpriseReviewService


def test_candidate_recording_uses_private_storage_and_signed_review_access(tmp_path) -> None:
    store = InMemoryStore()
    persistence = persistence_for(store)
    storage = LocalPrivateFileAdapter(
        tmp_path / "private",
        signer=FileAccessSigner("private-media-test-secret-at-least-32-chars"),
    )
    recording = PrivateMediaStorage(persistence, storage=storage).start_recording(
        "interview_1", "turn_1", "audio/webm;codecs=opus"
    )
    recording.append(b"candidate-")
    recording.append(b"audio")
    result = recording.finish()

    assert result.audio_uri.startswith("private-file://")
    assert read_managed_audio(persistence, "org_default", result.audio_uri, storage) == b"candidate-audio"
    file_id = result.audio_uri.removeprefix("private-file://")
    with persistence.transaction("org_default") as transaction:
        file_object = transaction.file_objects.get(file_id)
        transaction.interview_sessions.add(
            {
                "id": "interview_1",
                "organization_id": "org_default",
                "status": "report_ready",
                "candidate": {"name": "Candidate"},
                "turns": [],
                "answers": [{"id": "answer_1", "turn_id": "turn_1", "audio_uri": result.audio_uri}],
                "created_at": "2026-08-26T00:00:00Z",
                "updated_at": "2026-08-26T00:00:00Z",
            }
        )
    assert file_object["purpose"] == "candidate_answer_audio"
    assert file_object["interview_id"] == "interview_1"
    assert file_object["turn_id"] == "turn_1"

    review = EnterpriseReviewService(store, persistence=persistence, storage=storage)
    grant = review.audio_url("interview_1", "answer_1")
    token = grant["url"].rsplit("/", 1)[1]
    opened = review.open_audio_grant(token)
    assert opened == {"content": b"candidate-audio", "content_type": "audio/webm;codecs=opus"}


def test_browser_pcm_recording_is_persisted_as_valid_pcm16_wav(tmp_path) -> None:
    store = InMemoryStore()
    persistence = persistence_for(store)
    storage = LocalPrivateFileAdapter(
        tmp_path / "private-pcm",
        signer=FileAccessSigner("private-media-pcm-test-secret-at-least-32"),
    )
    recording = PrivateMediaStorage(persistence, storage=storage).start_recording(
        "interview_pcm",
        "turn_pcm",
        "audio/pcm",
        sample_rate_hz=16000,
        channels=1,
    )
    pcm_samples = b"\x00\x00\xff\x7f\x00\x80"
    recording.append(pcm_samples)

    result = recording.finish()
    content = read_managed_audio(persistence, "org_default", result.audio_uri, storage)

    assert result.mime_type == "audio/wav"
    assert result.byte_count == 44 + len(pcm_samples)
    assert content[:4] == b"RIFF"
    assert content[8:12] == b"WAVE"
    assert int.from_bytes(content[24:28], "little") == 16000
    assert int.from_bytes(content[22:24], "little") == 1
    assert int.from_bytes(content[40:44], "little") == len(pcm_samples)
    assert content[44:] == pcm_samples


def test_production_recording_refuses_local_backend(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.setenv("INTERVIEWER_MEDIA_RECORDING_BACKEND", "local")

    with pytest.raises(RuntimeError, match="private media storage"):
        media_recording_storage(persistence_for(InMemoryStore()))


def test_production_readiness_requires_private_object_storage(monkeypatch) -> None:
    persistence = persistence_for(InMemoryStore())
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    monkeypatch.setenv("INTERVIEWER_MEDIA_RECORDING_BACKEND", "private")

    with persistence.transaction("org_default") as transaction:
        monkeypatch.setenv("INTERVIEWER_FILE_STORAGE_BACKEND", "local")
        local = AppointmentAdmission().plan_readiness(
            transaction, {"bank_slots": []}, now=datetime.now(timezone.utc)
        )
        monkeypatch.setenv("INTERVIEWER_FILE_STORAGE_BACKEND", "aliyun_oss")
        private = AppointmentAdmission().plan_readiness(
            transaction, {"bank_slots": []}, now=datetime.now(timezone.utc)
        )

    local_check = next(item for item in local["checks"] if item["name"] == "candidate_audio_storage")
    private_check = next(item for item in private["checks"] if item["name"] == "candidate_audio_storage")
    assert local_check["ready"] is False
    assert private_check["ready"] is True
