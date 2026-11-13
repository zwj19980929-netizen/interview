"""Recording failures must be actionable without weakening finalization checks."""
import hashlib
import json

import httpx
import pytest

from app.core.errors import ApiError
from app.adapters.livekit_media import LiveKitConfiguration, LiveKitMediaPlane
from app.persistence.errors import ConcurrencyConflict
from app.persistence.provider import persistence_for
from app.repositories.sqlite import SQLiteStore
from app.services.media_capture import InterviewMediaCaptureService
from test_declined_answer_evaluation import persist
from test_interview_processing_recovery import capture_fixture, ORG


def queued(fixture):
    _, session, _, _, service, _ = fixture
    return service.request_finalization(session["id"], actor_id="synthetic", organization_id=ORG)


@pytest.mark.anyio
@pytest.mark.parametrize("error,code,category,retryable", [
    (ApiError("LIVEKIT_RECORDING_NOT_READY", "SECRET /absolute/path"), "recording_configuration_invalid", "configuration", False),
    (httpx.HTTPStatusError("SECRET", request=httpx.Request("POST", "https://secret.example"), response=httpx.Response(401)), "recording_provider_unauthorized", "configuration", False),
    (httpx.HTTPStatusError("SECRET", request=httpx.Request("POST", "https://secret.example"), response=httpx.Response(503)), "recording_provider_unavailable", "provider", True),
    (httpx.ReadTimeout("SECRET /absolute/path"), "recording_provider_unavailable", "provider", True),
    (ValueError("SECRET /absolute/path"), "recording_finalization_failed", "unknown", False),
])
async def test_failures_are_classified_and_safe(tmp_path, monkeypatch, error, code, category, retryable):
    fixture = capture_fixture(tmp_path, monkeypatch)
    _, session, capture, _, service, _ = fixture
    work = queued(fixture)
    service.media_plane.list_egress.side_effect = error
    with pytest.raises(ApiError) as caught:
        await service.process_finalization_work(work["id"], ORG)
    assert caught.value.code == code
    current = service.get_for_interview(session["id"], ORG)
    assert current["status"] == "stopping"  # Correct configuration can recover the same capture.
    assert current["finalization_error"]["category"] == category
    assert current["finalization_error"]["retryable"] is retryable
    with service.persistence.transaction(ORG) as tx:
        failed = tx.outbox.get(work["id"])
    assert failed["status"] == ("failed" if retryable else "dead_letter")
    assert failed["last_error_code"] == code
    assert failed["error_retryable"] is retryable
    assert "SECRET" not in json.dumps([current, failed])
    assert "/absolute/path" not in json.dumps([current, failed])


@pytest.mark.anyio
async def test_provider_still_ending_remains_retryable(tmp_path, monkeypatch):
    fixture = capture_fixture(tmp_path, monkeypatch)
    _, session, _, _, service, _ = fixture
    work = queued(fixture)
    with pytest.raises(ApiError) as caught:
        await service.process_finalization_work(work["id"], ORG)
    assert caught.value.code == "recording_finalization_pending"
    assert service.get_for_interview(session["id"], ORG)["finalization_error"]["retryable"]


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_configuration_recovery_hashes_real_file_once_and_preserves_answers(tmp_path, monkeypatch, backend):
    fixture = capture_fixture(tmp_path, monkeypatch)
    store, session, capture, info, service, storage = fixture
    if backend == "sqlite":
        store = SQLiteStore(str(tmp_path / "isolated.sqlite3"))
        persistence = persist(store, session)
        with persistence.transaction(ORG) as tx:
            tx.interview_media_captures.add(capture)
        # Reopen persistence to exercise the durable JSON boundary.
        store = SQLiteStore(str(store.path))
        service = InterviewMediaCaptureService(store, persistence=persistence_for(store),
                                               media_plane=service.media_plane, storage=storage)
        fixture = store, session, capture, info, service, storage
    work = queued(fixture)
    service.media_plane.list_egress.side_effect = ApiError("LIVEKIT_RECORDING_NOT_READY", "bad config")
    with pytest.raises(ApiError):
        await service.process_finalization_work(work["id"], ORG)
    service.media_plane.list_egress.side_effect = None
    info["status"] = "EGRESS_COMPLETE"
    content = b"synthetic-private-recording" * 10000
    (tmp_path / capture["object_key"]).write_bytes(content)
    info["file_results"] = [{"filename": capture["object_key"], "size": len(content)}]
    assert queued(fixture)["id"] == work["id"]
    assert queued(fixture)["id"] == work["id"]
    completed = await service.process_finalization_work(work["id"], ORG)
    assert completed["content_hash"] == "sha256:" + hashlib.sha256(content).hexdigest()
    assert completed["byte_count"] == len(content)
    assert completed["storage_protection"] == "local_private_development"
    assert completed["finalization_error"] is None
    assert service.interviews.get_interview(session["id"], ORG)["answers"] == session["answers"]
    with pytest.raises(ConcurrencyConflict):
        await service.process_finalization_work(work["id"], ORG)
    assert service.request_finalization(session["id"], actor_id="synthetic", organization_id=ORG) is None
    with service.persistence.transaction(ORG) as tx:
        assert len(tx.outbox.list()) == 1


@pytest.mark.anyio
async def test_binding_mismatch_never_hashes_or_retries_automatically(tmp_path, monkeypatch):
    fixture = capture_fixture(tmp_path, monkeypatch)
    _, session, _, info, service, _ = fixture
    work = queued(fixture)
    info["room_name"] = "foreign-room"
    info["status"] = "EGRESS_COMPLETE"
    monkeypatch.setattr(service, "_object_digest", lambda key: pytest.fail("must not read foreign object"))
    with pytest.raises(ApiError) as caught:
        await service.process_finalization_work(work["id"], ORG)
    assert caught.value.code == "recording_binding_invalid"
    assert service.get_for_interview(session["id"], ORG)["finalization_error"]["category"] == "integrity"


@pytest.mark.anyio
async def test_bad_configuration_cannot_be_manually_requeued_until_fixed(tmp_path, monkeypatch):
    fixture = capture_fixture(tmp_path, monkeypatch)
    _, _, _, _, service, _ = fixture
    work = queued(fixture)
    service.media_plane.list_egress.side_effect = ApiError("LIVEKIT_RECORDING_NOT_READY", "bad config")
    with pytest.raises(ApiError):
        await service.process_finalization_work(work["id"], ORG)
    monkeypatch.setenv("INTERVIEWER_LOCAL_MEDIA", "false")
    service.media_plane = LiveKitMediaPlane(LiveKitConfiguration.from_environment())
    with pytest.raises(ApiError) as caught:
        queued(fixture)
    assert caught.value.code == "LIVEKIT_RECORDING_NOT_READY"
    with service.persistence.transaction(ORG) as tx:
        still_failed = tx.outbox.get(work["id"])
    assert still_failed["status"] == "dead_letter"
    assert still_failed.get("replay_count", 0) == 0


@pytest.mark.anyio
async def test_real_file_without_required_protection_never_becomes_playable(tmp_path, monkeypatch):
    fixture = capture_fixture(tmp_path, monkeypatch)
    _, session, capture, info, service, _ = fixture
    work = queued(fixture)
    (tmp_path / capture["object_key"]).write_bytes(b"synthetic-video")
    info["status"] = "EGRESS_COMPLETE"
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    with pytest.raises(ApiError) as caught:
        await service.process_finalization_work(work["id"], ORG)
    assert caught.value.code == "recording_protection_unverified"
    current = service.get_for_interview(session["id"], ORG)
    assert current["status"] == "failed"
    assert current.get("private_uri") is None
    assert current["finalization_error"]["retryable"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("provider_status", ["EGRESS_FAILED", "EGRESS_ABORTED"])
async def test_provider_terminal_failure_does_not_loop_or_claim_completed(tmp_path, monkeypatch, provider_status):
    fixture = capture_fixture(tmp_path, monkeypatch)
    _, session, _, info, service, _ = fixture
    work = queued(fixture)
    info["status"] = provider_status
    with pytest.raises(ApiError) as caught:
        await service.process_finalization_work(work["id"], ORG)
    assert caught.value.code == "recording_capture_failed"
    assert service.get_for_interview(session["id"], ORG)["status"] == "failed"
    with service.persistence.transaction(ORG) as tx:
        assert tx.outbox.get(work["id"])["status"] == "dead_letter"
