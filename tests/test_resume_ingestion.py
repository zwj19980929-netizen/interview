import asyncio
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, StreamObject

from app.core.errors import ApiError
from app.file_storage.aliyun_oss import AliyunOssFileAdapter
from app.file_storage.local import LocalPrivateFileAdapter
from app.file_storage.signing import FileAccessSigner
from app.main import create_app
from app.persistence.provider import persistence_for
from app.repositories.provider import get_store, reset_store_for_tests
from app.services.resume_ingestion import ResumeIngestionService, SafePdfDownloader
from app.workers.outbox import OutboxWorker


def _pdf(text: str = "Python backend resume") -> bytes:
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
    stream.set_data(("BT /F1 12 Tf 72 720 Td (%s) Tj ET" % text).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _candidate(api: TestClient) -> str:
    response = api.post(
        "/api/v1/candidate-profiles",
        json={"name": "Candidate", "email": "candidate@example.com", "phone": "13800138000"},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_pdf_upload_worker_private_access_and_idempotency(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("INTERVIEWER_FILE_QUARANTINE_ROOT", str(tmp_path / "quarantine"))
    monkeypatch.setenv("INTERVIEWER_FILE_SIGNING_SECRET", "test-signing-secret-at-least-32-characters")
    from app.file_storage.provider import reset_private_file_storage_for_tests

    reset_private_file_storage_for_tests()
    reset_store_for_tests()
    api = TestClient(create_app())
    candidate_id = _candidate(api)
    content = _pdf()
    upload = api.post(
        f"/api/v1/candidate-profiles/{candidate_id}/resumes",
        files={"file": ("candidate.pdf", content, "application/pdf")},
        data={"display_name": "candidate.pdf"},
        headers={"Idempotency-Key": "resume-upload-1"},
    )
    assert upload.status_code == 202, upload.text
    duplicate = api.post(
        f"/api/v1/candidate-profiles/{candidate_id}/resumes",
        files={"file": ("candidate.pdf", content, "application/pdf")},
        headers={"Idempotency-Key": "resume-upload-1"},
    )
    assert duplicate.json()["ingestion_job_id"] == upload.json()["ingestion_job_id"]

    results = asyncio.run(OutboxWorker(get_store()).run_once())
    assert any(item["kind"] == "resume.ingest" and item["status"] == "completed" for item in results)
    job = api.get(f"/api/v1/file-ingestion-jobs/{upload.json()['ingestion_job_id']}")
    assert job.status_code == 200
    assert job.json()["resume_document"]["status"] == "ready"

    resume_id = upload.json()["resume_document_id"]
    detail = api.get(f"/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}")
    assert detail.json()["page_count"] == 1
    assert detail.json()["file"]["scan_status"] == "clean"
    assert "parsed_text" not in detail.json()
    grant = api.post(
        f"/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}/content-url",
        headers={"X-Actor-Id": "reviewer_1"},
    )
    download = api.get(grant.json()["url"])
    assert download.status_code == 200
    assert download.content == content
    assert download.headers["cache-control"] == "private, no-store"
    assert download.headers["accept-ranges"] == "bytes"
    assert download.headers["content-length"] == str(len(content))

    full_head = api.head(grant.json()["url"])
    assert full_head.status_code == 200
    assert full_head.content == b""
    assert full_head.headers["content-length"] == str(len(content))
    assert full_head.headers["accept-ranges"] == "bytes"

    partial = api.get(grant.json()["url"], headers={"Range": "bytes=2-7"})
    assert partial.status_code == 206
    assert partial.content == content[2:8]
    assert partial.headers["content-range"] == "bytes 2-7/%d" % len(content)
    assert partial.headers["content-length"] == "6"
    assert partial.headers["accept-ranges"] == "bytes"

    case_insensitive = api.get(
        grant.json()["url"], headers={"Range": "BYTES=2-7"}
    )
    assert case_insensitive.status_code == 206
    assert case_insensitive.content == content[2:8]

    open_ended = api.get(grant.json()["url"], headers={"Range": "bytes=7-"})
    assert open_ended.status_code == 206
    assert open_ended.content == content[7:]
    assert open_ended.headers["content-range"] == "bytes 7-%d/%d" % (
        len(content) - 1,
        len(content),
    )

    suffix = api.get(grant.json()["url"], headers={"Range": "bytes=-5"})
    assert suffix.status_code == 206
    assert suffix.content == content[-5:]
    assert suffix.headers["content-range"] == "bytes %d-%d/%d" % (
        len(content) - 5,
        len(content) - 1,
        len(content),
    )

    head = api.head(grant.json()["url"], headers={"Range": "bytes=2-7"})
    assert head.status_code == 206
    assert head.content == b""
    assert head.headers["content-range"] == "bytes 2-7/%d" % len(content)
    assert head.headers["content-length"] == "6"

    for invalid_range in (
        "bytes=999999999-",
        "bytes=0-1,3-4",
        "bytes=" + "9" * 5000 + "-",
    ):
        invalid = api.get(grant.json()["url"], headers={"Range": invalid_range})
        assert invalid.status_code == 416
        assert invalid.content == b""
        assert invalid.headers["content-range"] == "bytes */%d" % len(content)
        assert invalid.headers["accept-ranges"] == "bytes"
        assert invalid.headers["content-length"] == "0"
    with persistence_for(get_store()).transaction("org_default") as transaction:
        events = transaction.audit_events.list()
        raw_resume = transaction.resume_documents.get(resume_id)
        parsed_file = transaction.file_objects.get(raw_resume["parsed_text_file_object_id"])
    assert any(item["action"] == "resume.file.access_granted" for item in events)
    assert "parsed_text" not in raw_resume
    assert parsed_file["purpose"] == "resume_parsed_text"
    assert parsed_file["status"] == "ready"


def test_pdf_upload_rejects_type_and_worker_fails_malware(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("INTERVIEWER_FILE_QUARANTINE_ROOT", str(tmp_path / "quarantine"))
    from app.file_storage.provider import reset_private_file_storage_for_tests

    reset_private_file_storage_for_tests()
    reset_store_for_tests()
    api = TestClient(create_app())
    candidate_id = _candidate(api)
    invalid = api.post(
        f"/api/v1/candidate-profiles/{candidate_id}/resumes",
        files={"file": ("resume.pdf", b"not-a-pdf", "application/pdf")},
    )
    assert invalid.status_code == 415
    assert invalid.json()["error"]["code"] == "PDF_SIGNATURE_INVALID"

    infected = _pdf() + b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE"
    queued = api.post(
        f"/api/v1/candidate-profiles/{candidate_id}/resumes",
        files={"file": ("resume.pdf", infected, "application/pdf")},
    )
    assert queued.status_code == 202
    results = asyncio.run(OutboxWorker(get_store()).run_once())
    assert any(item["kind"] == "resume.ingest" and item["status"] == "failed" for item in results)
    detail = api.get(
        f"/api/v1/candidate-profiles/{candidate_id}/resumes/{queued.json()['resume_document_id']}"
    )
    assert detail.json()["status"] == "failed"
    assert detail.json()["processing_error"]["code"] == "FILE_MALWARE_DETECTED"


def test_resume_document_crud_cancels_pending_ingestion_and_cleans_quarantine(tmp_path, monkeypatch) -> None:
    private_root = tmp_path / "private"
    quarantine_root = tmp_path / "quarantine"
    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(private_root))
    monkeypatch.setenv("INTERVIEWER_FILE_QUARANTINE_ROOT", str(quarantine_root))
    from app.file_storage.provider import reset_private_file_storage_for_tests

    reset_private_file_storage_for_tests()
    reset_store_for_tests()
    api = TestClient(create_app())
    candidate_id = _candidate(api)
    queued = api.post(
        f"/api/v1/candidate-profiles/{candidate_id}/resumes",
        files={"file": ("candidate-old.pdf", _pdf(), "application/pdf")},
        data={"display_name": "candidate-old.pdf"},
        headers={"Idempotency-Key": "resume-crud-1"},
    )
    assert queued.status_code == 202, queued.text
    resume_id = queued.json()["resume_document_id"]
    work_id = queued.json()["ingestion_job_id"]
    quarantine_path = quarantine_root / f"{work_id}.upload"
    assert quarantine_path.exists()

    detail = api.get(f"/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}")
    assert detail.status_code == 200
    assert detail.json()["file_name"] == "candidate-old.pdf"
    renamed = api.patch(
        f"/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}",
        json={"expected_version": detail.json()["version"], "display_name": "candidate-current.pdf"},
        headers={"X-Actor-Id": "recruiter_1"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["file_name"] == "candidate-current.pdf"

    stale_delete = api.delete(
        f"/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}",
        params={"expected_version": detail.json()["version"]},
    )
    assert stale_delete.status_code == 409
    assert stale_delete.json()["error"]["code"] == "RESUME_VERSION_CONFLICT"

    deleted = api.delete(
        f"/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}",
        params={"expected_version": renamed.json()["version"]},
        headers={"X-Actor-Id": "recruiter_1"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "deleted"
    assert not quarantine_path.exists()
    assert api.get(f"/api/v1/candidate-profiles/{candidate_id}/resumes").json()["items"] == []
    assert api.get(f"/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}").status_code == 404
    with persistence_for(get_store()).transaction("org_default") as transaction:
        raw_resume = transaction.resume_documents.get(resume_id)
        file_object = transaction.file_objects.get(raw_resume["file_object_id"])
        work = transaction.outbox.get(work_id)
        actions = [item["action"] for item in transaction.audit_events.list()]
    assert raw_resume["status"] == "deleted"
    assert raw_resume["file_hash"] is None
    assert file_object["status"] == "deleted"
    assert work["status"] == "cancelled"
    assert "resume.display_name.updated" in actions
    assert "resume.delete.completed" in actions


def test_resume_delete_rejects_an_interview_plan_reference(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("INTERVIEWER_FILE_QUARANTINE_ROOT", str(tmp_path / "quarantine"))
    from app.file_storage.provider import reset_private_file_storage_for_tests

    reset_private_file_storage_for_tests()
    reset_store_for_tests()
    api = TestClient(create_app())
    candidate_id = _candidate(api)
    queued = api.post(
        f"/api/v1/candidate-profiles/{candidate_id}/resumes",
        files={"file": ("referenced.pdf", _pdf(), "application/pdf")},
        headers={"Idempotency-Key": "resume-reference-1"},
    )
    resume_id = queued.json()["resume_document_id"]
    with persistence_for(get_store()).transaction("org_default") as transaction:
        review = transaction.resume_reviews.add(
            {
                "id": "review_delete_guard",
                "organization_id": "org_default",
                "candidate_profile_id": candidate_id,
                "resume_document_id": resume_id,
                "job_position_id": "position_delete_guard",
                "status": "ready_for_review",
                "created_at": "2026-08-28T00:00:00Z",
            }
        )
        transaction.interview_plans.add(
            {
                "id": "plan_delete_guard",
                "organization_id": "org_default",
                "resume_review_id": review["id"],
                "experience_question_ids": [],
                "status": "draft",
                "created_at": "2026-08-28T00:00:00Z",
            }
        )
        resume = transaction.resume_documents.get(resume_id)
    rejected = api.delete(
        f"/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}",
        params={"expected_version": resume["version"]},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "RESUME_DOCUMENT_IN_USE"
    assert rejected.json()["error"]["details"]["plan_ids"] == ["plan_delete_guard"]
    with persistence_for(get_store()).transaction("org_default") as transaction:
        assert transaction.resume_documents.get(resume_id)["status"] == "processing"
        assert transaction.outbox.get(queued.json()["ingestion_job_id"])["status"] == "pending"


def test_safe_pdf_downloader_rejects_loopback() -> None:
    with pytest.raises(ApiError) as captured:
        asyncio.run(SafePdfDownloader().download("http://127.0.0.1/resume.pdf"))
    assert captured.value.code == "PDF_URL_ADDRESS_FORBIDDEN"


def test_local_private_file_storage_contract(tmp_path) -> None:
    adapter = LocalPrivateFileAdapter(
        tmp_path,
        signer=FileAccessSigner("contract-signing-secret-at-least-32-characters"),
    )
    content = _pdf()
    stored = adapter.store(
        organization_id="org_a",
        object_id="file_1",
        content=content,
        content_type="application/pdf",
        checksum="sha256:test",
    )
    assert adapter.open(stored.object_key) == content
    grant = adapter.issue_read_access(stored.object_key, expires_seconds=60)
    assert adapter.signer.verify(grant)["object_key"] == stored.object_key
    adapter.delete(stored.object_key)
    with pytest.raises(FileNotFoundError):
        adapter.open(stored.object_key)


def test_url_import_uses_the_same_verified_private_pipeline(tmp_path) -> None:
    class FixedDownloader:
        async def download(self, source_url: str):
            assert source_url == "https://public.example/resume.pdf"
            return _pdf("URL imported Python resume"), source_url + "?temporary=redacted"

    reset_store_for_tests()
    api = TestClient(create_app())
    candidate_id = _candidate(api)
    service = ResumeIngestionService(
        get_store(),
        storage=LocalPrivateFileAdapter(
            tmp_path / "private",
            signer=FileAccessSigner("url-contract-signing-secret-at-least-32-chars"),
        ),
        downloader=FixedDownloader(),
        quarantine_root=tmp_path / "quarantine",
    )
    queued = service.queue_url(
        candidate_id,
        source_url="https://public.example/resume.pdf",
        file_name="resume.pdf",
        idempotency_key="url-import-1",
    )
    completed = asyncio.run(service.process(queued["job"]["id"]))
    assert completed["status"] == "ready"
    assert completed["page_count"] == 1
    with persistence_for(get_store()).transaction("org_default") as transaction:
        file_object = transaction.file_objects.get(completed["file_object_id"])
    assert file_object["scan_status"] == "clean"
    assert file_object["source_reference"] == "https://public.example"
    assert file_object["source_url_hash"].startswith("sha256:")
    assert "temporary=redacted" not in str(get_store().outbox_work_items)


def test_aliyun_oss_private_storage_contract(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_OSS_SSE", "AES256")
    class Download:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def read(self) -> bytes:
            return self.content

    class Bucket:
        def __init__(self) -> None:
            self.objects = {}
            self.last_headers = {}

        def put_object(self, key, content, headers):
            self.objects[key] = content
            self.last_headers = headers

        def get_object(self, key):
            return Download(self.objects[key])

        def delete_object(self, key):
            self.objects.pop(key, None)

        def sign_url(self, method, key, expires, slash_safe):
            assert method == "GET" and slash_safe is True and expires <= 900
            return "https://oss.example/%s?signature=test" % key

        def get_bucket_info(self):
            return {"name": "private-resumes"}

        def get_bucket_encryption(self):
            return type("Encryption", (), {"sse_algorithm": "AES256"})()

        def get_object_meta(self, key):
            assert key in self.objects
            return type(
                "ObjectMeta",
                (),
                {"headers": {"x-oss-server-side-encryption": "AES256"}},
            )()

    bucket = Bucket()
    adapter = AliyunOssFileAdapter(bucket=bucket, bucket_name="private-resumes")
    adapter.healthcheck()
    content = _pdf()
    stored = adapter.store(
        organization_id="org_a",
        object_id="file_oss_1",
        content=content,
        content_type="application/pdf",
        checksum="sha256:abc",
    )
    assert adapter.open(stored.object_key) == content
    assert bucket.last_headers["x-oss-server-side-encryption"] == "AES256"
    assert bucket.last_headers["x-oss-meta-sha256"] == "abc"
    assert adapter.verify_encryption(stored.object_key) == "aliyun_oss_aes256"
    assert adapter.issue_read_access(stored.object_key, expires_seconds=3600).startswith("https://oss.example/")
    adapter.delete(stored.object_key)
    assert stored.object_key not in bucket.objects
