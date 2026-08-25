import asyncio
from datetime import datetime, timedelta, timezone
from io import BytesIO

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, StreamObject

from app.file_storage.provider import reset_private_file_storage_for_tests
from app.main import create_app
from app.repositories.provider import get_store, reset_store_for_tests
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
