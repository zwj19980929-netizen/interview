"""A late StopEgress failure cannot revoke independently sealed recording facts."""

import asyncio
import hashlib
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.core.errors import ApiError
from app.persistence.provider import persistence_for
from app.repositories.sqlite import SQLiteStore
from app.services.media_capture import InterviewMediaCaptureService
from test_declined_answer_evaluation import persist
from test_interview_processing_recovery import ORG, capture_fixture


def update_capture(service, capture_id, **changes):
    with service.persistence.transaction(ORG) as transaction:
        capture = transaction.interview_media_captures.get(capture_id)
        capture.update(changes)
        return transaction.interview_media_captures.update(
            capture, expected_version=capture["version"]
        )


def complete_file(tmp_path, capture, info):
    content = b"synthetic-recording-with-audio" * 1024
    (tmp_path / capture["object_key"]).write_bytes(content)
    info.update(status="EGRESS_COMPLETE", file_results=[{
        "filename": capture["object_key"], "size": len(content),
    }])
    return content


def failure_events(service):
    with service.persistence.transaction(ORG) as transaction:
        return [event for event in transaction.audit_events.list()
                if event["action"] == "interview.media_capture.failed"]


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_worker_seals_while_stop_is_pending_then_late_stop_error_is_harmless(
    tmp_path, monkeypatch, backend,
):
    store, session, capture, info, service, storage = capture_fixture(tmp_path, monkeypatch)
    session["candidate_input_completed_at"] = None
    if backend == "sqlite":
        store = SQLiteStore(str(tmp_path / "isolated.sqlite3"))
        persistence = persist(store, session)
        with persistence.transaction(ORG) as transaction:
            transaction.interview_media_captures.add(capture)
        service = InterviewMediaCaptureService(
            SQLiteStore(str(store.path)), persistence=persistence_for(store),
            media_plane=service.media_plane, storage=storage,
        )
    else:
        persist(store, session)
    content = complete_file(tmp_path, capture, info)
    service.interviews.pause_interview = Mock()
    stop_started, release_stop = asyncio.Event(), asyncio.Event()

    async def delayed_stop(egress_id):
        assert egress_id == capture["egress_id"]
        stop_started.set()
        await release_stop.wait()
        raise httpx.HTTPStatusError(
            "already stopped", request=httpx.Request("POST", "https://example.test/stop"),
            response=httpx.Response(404),
        )

    service.media_plane.stop_egress = delayed_stop
    stopping = asyncio.create_task(service.stop_for_interview(
        session["id"], actor_id="synthetic-stop", organization_id=ORG,
    ))
    try:
        await asyncio.wait_for(stop_started.wait(), timeout=2)
        with service.persistence.transaction(ORG) as transaction:
            work = transaction.outbox.list()[0]
        sealed = await service.process_finalization_work(work["id"], ORG)
        assert sealed["status"] == "completed"
        assert sealed["content_hash"] == "sha256:" + hashlib.sha256(content).hexdigest()
        release_stop.set()
        result = await asyncio.wait_for(stopping, timeout=2)
    finally:
        release_stop.set()
        if not stopping.done():
            stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)
    assert result == sealed
    assert service.get_for_interview(session["id"], ORG) == sealed
    assert failure_events(service) == []
    service.interviews.pause_interview.assert_not_called()
    with service.persistence.transaction(ORG) as transaction:
        assert transaction.outbox.get(work["id"])["status"] == "completed"


@pytest.mark.anyio
@pytest.mark.parametrize("terminal", ["cancelled", "retention_purged"])
async def test_late_stop_failure_preserves_cancellation_or_retention(tmp_path, monkeypatch, terminal):
    _, session, capture, _, service, _ = capture_fixture(tmp_path, monkeypatch)
    service.interviews.pause_interview = Mock()

    async def stop_then_fail(_):
        update_capture(service, capture["id"], status=terminal)
        raise httpx.ReadTimeout("synthetic delayed stop timeout")

    service.media_plane.stop_egress = stop_then_fail
    result = await service.stop_for_interview(session["id"], actor_id="synthetic", organization_id=ORG)
    assert result["status"] == terminal
    assert service.get_for_interview(session["id"], ORG)["status"] == terminal
    assert failure_events(service) == []
    service.interviews.pause_interview.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("terminal", ["completed", "cancelled", "retention_purged"])
async def test_stop_does_not_contact_provider_for_existing_terminal_capture(tmp_path, monkeypatch, terminal):
    _, session, capture, _, service, _ = capture_fixture(tmp_path, monkeypatch)
    update_capture(service, capture["id"], status=terminal)
    service.media_plane.stop_egress = AsyncMock(side_effect=AssertionError("terminal capture must not stop again"))
    result = await service.stop_for_interview(session["id"], actor_id="synthetic", organization_id=ORG)
    assert result["status"] == terminal
    service.media_plane.stop_egress.assert_not_called()
    assert failure_events(service) == []


@pytest.mark.anyio
async def test_stop_rechecks_terminal_state_after_its_initial_read(tmp_path, monkeypatch):
    _, session, capture, info, service, _ = capture_fixture(tmp_path, monkeypatch)
    stale = service.get_for_interview(session["id"], ORG)
    complete_file(tmp_path, capture, info)
    sealed = await service.finalize_provider_result(capture["id"], info, actor_id="synthetic", organization_id=ORG)
    monkeypatch.setattr(service, "get_for_interview", lambda *args: stale)
    service.media_plane.stop_egress = AsyncMock(side_effect=AssertionError("sealed during initial read"))
    result = await service.stop_for_interview(session["id"], actor_id="synthetic", organization_id=ORG)
    assert result == sealed
    service.media_plane.stop_egress.assert_not_called()
    assert failure_events(service) == []


@pytest.mark.anyio
async def test_actual_stop_failure_remains_visible_and_pauses_active_interview(tmp_path, monkeypatch):
    store, session, _, _, service, _ = capture_fixture(tmp_path, monkeypatch)
    session["candidate_input_completed_at"] = None
    persist(store, session)
    service.interviews.pause_interview = Mock()
    service.media_plane.stop_egress = AsyncMock(side_effect=httpx.ReadTimeout("synthetic timeout"))
    with pytest.raises(ApiError) as caught:
        await service.stop_for_interview(session["id"], actor_id="synthetic", organization_id=ORG)
    assert caught.value.code == "MEDIA_CAPTURE_STOP_FAILED"
    assert service.get_for_interview(session["id"], ORG)["failure_code"] == "egress_stop_failed"
    assert len(failure_events(service)) == 1
    service.interviews.pause_interview.assert_called_once_with(
        session["id"], reason="required_media_capture_failed", organization_id=ORG,
    )


@pytest.mark.anyio
async def test_reverified_recording_clears_stale_failure_diagnostics(tmp_path, monkeypatch):
    _, _, capture, info, service, _ = capture_fixture(tmp_path, monkeypatch)
    content = complete_file(tmp_path, capture, info)
    update_capture(service, capture["id"], status="failed", failure_code="egress_stop_failed",
                   failure_type="HTTPStatusError", finalization_error={"code": "recording_capture_failed"})
    result = await service.finalize_provider_result(capture["id"], info, actor_id="synthetic", organization_id=ORG)
    assert result["status"] == "completed"
    assert result["byte_count"] == len(content)
    assert result["content_hash"] == "sha256:" + hashlib.sha256(content).hexdigest()
    assert result["storage_protection"] == "local_private_development"
    assert result.get("failure_code") is None
    assert result.get("failure_type") is None
    assert result["finalization_error"] is None


@pytest.mark.anyio
async def test_size_mismatch_does_not_clear_failure_as_if_recovered(tmp_path, monkeypatch):
    _, _, capture, info, service, _ = capture_fixture(tmp_path, monkeypatch)
    complete_file(tmp_path, capture, info)
    info["file_results"][0]["size"] += 1
    update_capture(service, capture["id"], status="failed", failure_code="egress_stop_failed",
                   failure_type="HTTPStatusError")
    result = await service.finalize_provider_result(capture["id"], info, actor_id="synthetic", organization_id=ORG)
    assert result["status"] == "hash_pending"
    assert result["content_hash"] is None
    assert result["failure_code"] == "egress_stop_failed"


@pytest.mark.anyio
async def test_protection_failure_during_reverification_is_not_ignored(tmp_path, monkeypatch):
    _, session, capture, info, service, _ = capture_fixture(tmp_path, monkeypatch)
    complete_file(tmp_path, capture, info)
    update_capture(service, capture["id"], status="failed", failure_code="egress_stop_failed")
    monkeypatch.setenv("INTERVIEWER_RUNTIME_ENV", "production")
    with pytest.raises(ApiError) as caught:
        await service.finalize_provider_result(capture["id"], info, actor_id="synthetic", organization_id=ORG)
    assert caught.value.code == "MEDIA_CAPTURE_ENCRYPTION_UNVERIFIED"
    result = service.get_for_interview(session["id"], ORG)
    assert result["status"] == "failed"
    assert result["failure_code"] == "recording_object_encryption_unverified"
