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
from app.services.resume_review import review_input_hash
from app.services.retention import RetentionService
from app.workers.outbox import OutboxWorker


def _pdf(text: str) -> bytes:
    return _multi_page_pdf([text])


def _multi_page_pdf(page_texts: list[str]) -> bytes:
    writer = PdfWriter()
    for text in page_texts:
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


def _screen_candidate(api: TestClient, name: str) -> tuple[dict, dict]:
    position = api.post(
        "/api/v1/job-positions",
        json={"code": name.lower().replace(" ", "-"), "name": "Python 后端", "description": "Python 服务"},
    ).json()
    role = api.post(
        f"/api/v1/job-positions/{position['id']}/role-requirements",
        json={
            "job_position_id": position["id"],
            "title": "Python 后端要求",
            "description": "必须有 Python 生产经验",
            "must_have_skills": ["python"],
        },
    ).json()
    candidate = api.post(
        "/api/v1/candidate-profiles",
        json={"name": name, "email": f"{name.lower().replace(' ', '.')}@example.com", "phone": "13800138000"},
    ).json()
    queued = api.post(
        f"/api/v1/candidate-profiles/{candidate['id']}/resumes",
        files={"file": ("resume.pdf", _pdf("Java project delivery"), "application/pdf")},
    ).json()
    asyncio.run(OutboxWorker(get_store()).run_once())
    review = api.post(
        f"/api/v1/candidate-profiles/{candidate['id']}/resume-reviews",
        json={
            "resume_document_id": queued["resume_document_id"],
            "job_position_id": position["id"],
            "role_requirement_id": role["id"],
        },
    )
    assert review.status_code == 202, review.text
    queued_review = review.json()["review"]
    assert queued_review["status"] == "queued"
    projected = api.get(f"/api/v1/candidate-profiles/{candidate['id']}").json()
    assert projected["screening"]["effective_outcome"] == "processing"
    asyncio.run(OutboxWorker(get_store()).run_once())
    ready = api.get(f"/api/v1/resume-reviews/{queued_review['id']}")
    assert ready.status_code == 200, ready.text
    assert ready.json()["status"] == "ready_for_review"
    assert ready.json()["screening_recommendation"] == "unqualified"
    assert ready.json()["screening_policy_version"] == "candidate_screening_score.v1"
    return candidate, ready.json()


def test_position_creation_can_atomically_include_initial_requirement() -> None:
    reset_store_for_tests()
    api = TestClient(create_app())
    response = api.post(
        "/api/v1/job-positions",
        json={
            "code": "backend-platform",
            "name": "平台后端工程师",
            "description": "负责服务平台",
            "initial_requirement": {
                "title": "首版岗位要求",
                "description": "负责 Python 服务和 PostgreSQL 数据设计",
                "must_have_skills": ["Python", "PostgreSQL"],
                "nice_to_have_skills": ["Redis"],
                "seniority": "senior",
                "interview_duration_minutes": 60,
            },
        },
    )
    assert response.status_code == 200, response.text
    position = response.json()
    requirement = position["initial_role_requirement"]
    assert requirement["job_position_id"] == position["id"]
    assert requirement["must_have_skills"] == ["python", "postgresql"]
    assert requirement["nice_to_have_skills"] == ["redis"]
    listed = api.get(f"/api/v1/job-positions/{position['id']}/role-requirements").json()["items"]
    assert [item["id"] for item in listed] == [requirement["id"]]

    invalid = api.post(
        "/api/v1/job-positions",
        json={"code": "invalid-role", "name": "无效岗位", "initial_requirement": {"title": "", "description": ""}},
    )
    assert invalid.status_code == 422
    assert all(item["code"] != "invalid-role" for item in api.get("/api/v1/job-positions").json()["items"])


def test_identical_pdf_versions_create_distinct_resume_review_work_items(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("INTERVIEWER_FILE_QUARANTINE_ROOT", str(tmp_path / "quarantine"))
    reset_private_file_storage_for_tests()
    reset_store_for_tests()
    api = TestClient(create_app())
    position = api.post(
        "/api/v1/job-positions",
        json={"code": "duplicate-resume", "name": "重复简历岗位", "description": "Python 服务"},
    ).json()
    role = api.post(
        f"/api/v1/job-positions/{position['id']}/role-requirements",
        json={"title": "Python 要求", "description": "Python 生产经验", "must_have_skills": ["python"]},
    ).json()
    candidate = api.post(
        "/api/v1/candidate-profiles",
        json={"name": "Duplicate Resume", "email": "duplicate@example.com", "phone": "13800138009"},
    ).json()
    resume_bytes = _pdf("Python production service")

    first = api.post(
        f"/api/v1/candidate-profiles/{candidate['id']}/resumes",
        data={"job_position_id": position["id"], "role_requirement_id": role["id"]},
        files={"file": ("same.pdf", resume_bytes, "application/pdf")},
        headers={"Idempotency-Key": "duplicate-resume-upload-1"},
    )
    assert first.status_code == 202, first.text
    asyncio.run(OutboxWorker(get_store()).run_once())
    second = api.post(
        f"/api/v1/candidate-profiles/{candidate['id']}/resumes",
        data={"job_position_id": position["id"], "role_requirement_id": role["id"]},
        files={"file": ("same-again.pdf", resume_bytes, "application/pdf")},
        headers={"Idempotency-Key": "duplicate-resume-upload-2"},
    )
    assert second.status_code == 202, second.text
    asyncio.run(OutboxWorker(get_store()).run_once())

    with persistence_for(get_store()).transaction("org_default") as transaction:
        reviews = [
            item for item in transaction.resume_reviews.list() if item["candidate_profile_id"] == candidate["id"]
        ]
        review_work = [item for item in transaction.outbox.list() if item.get("kind") == "resume.review"]
    assert len(reviews) == 2
    assert len(review_work) == 2
    assert {item["aggregate_id"] for item in review_work} == {item["id"] for item in reviews}
    assert len({item["idempotency_key"] for item in review_work}) == 2
    for item in review_work:
        review = next(value for value in reviews if value["id"] == item["aggregate_id"])
        assert item["idempotency_key"].startswith(f"resume.review:{review['resume_document_id']}:")


def test_requeue_command_repairs_a_queued_review_with_no_work_item(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("INTERVIEWER_FILE_QUARANTINE_ROOT", str(tmp_path / "quarantine"))
    reset_private_file_storage_for_tests()
    reset_store_for_tests()
    api = TestClient(create_app())
    position = api.post(
        "/api/v1/job-positions",
        json={"code": "orphan-review", "name": "孤儿审阅岗位", "description": "Python 服务"},
    ).json()
    role = api.post(
        f"/api/v1/job-positions/{position['id']}/role-requirements",
        json={"title": "Python 要求", "description": "Python 生产经验", "must_have_skills": ["python"]},
    ).json()
    candidate = api.post(
        "/api/v1/candidate-profiles",
        json={"name": "Orphan Review", "email": "orphan@example.com", "phone": "13800138008"},
    ).json()
    queued_resume = api.post(
        f"/api/v1/candidate-profiles/{candidate['id']}/resumes",
        files={"file": ("orphan.pdf", _pdf("Python production service"), "application/pdf")},
    ).json()
    asyncio.run(OutboxWorker(get_store()).run_once())
    resume = api.get(
        f"/api/v1/candidate-profiles/{candidate['id']}/resumes/{queued_resume['resume_document_id']}"
    ).json()
    input_hash = review_input_hash(resume, position, role)
    with persistence_for(get_store()).transaction("org_default") as transaction:
        orphan = transaction.resume_reviews.add(
            {
                "id": "resume_review_orphan",
                "organization_id": "org_default",
                "candidate_profile_id": candidate["id"],
                "resume_document_id": resume["id"],
                "job_position_id": position["id"],
                "role_requirement_id": role["id"],
                "input_hash": input_hash,
                "status": "queued",
                "processing_stage": "queued",
                "processing_progress": {"completed_chunks": 0, "total_chunks": None},
                "created_at": "2026-08-28T12:00:00Z",
                "updated_at": "2026-08-28T12:00:00Z",
            }
        )

    repaired = api.post(
        f"/api/v1/candidate-profiles/{candidate['id']}/resume-reviews",
        json={
            "resume_document_id": resume["id"],
            "job_position_id": position["id"],
            "role_requirement_id": role["id"],
        },
    )
    assert repaired.status_code == 202, repaired.text
    assert repaired.json()["review"]["id"] == orphan["id"]
    assert repaired.json()["job"]["status"] == "pending"
    assert repaired.json()["job"]["aggregate_id"] == orphan["id"]

    asyncio.run(OutboxWorker(get_store()).run_once())
    assert api.get(f"/api/v1/resume-reviews/{orphan['id']}").json()["status"] == "ready_for_review"


def test_candidate_screening_explains_outcome_supports_review_resume_and_crud(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("INTERVIEWER_FILE_QUARANTINE_ROOT", str(tmp_path / "quarantine"))
    reset_private_file_storage_for_tests()
    reset_store_for_tests()
    api = TestClient(create_app())

    candidate, review = _screen_candidate(api, "Screened Candidate")
    listed = api.get("/api/v1/candidate-profiles").json()["items"]
    projected = next(item for item in listed if item["id"] == candidate["id"])
    screening = projected["screening"]
    assert screening["effective_outcome"] == "unqualified"
    assert screening["score_policy_version"] == "candidate_screening_score.v1"
    assert screening["unmet_requirements"][0]["requirement"] == "python"
    assert projected["retention_reason"] == "screening_unqualified"
    deadline = datetime.fromisoformat(projected["retention_expires_at"])
    assert timedelta(days=6, hours=23) < deadline - datetime.now(timezone.utc) <= timedelta(days=7, minutes=1)

    resumes = api.get(f"/api/v1/candidate-profiles/{candidate['id']}/resumes").json()["items"]
    grant = api.post(
        f"/api/v1/candidate-profiles/{candidate['id']}/resumes/{resumes[0]['id']}/content-url"
    ).json()["url"]
    assert api.get(grant).status_code == 200

    reviewed = api.patch(
        f"/api/v1/resume-reviews/{review['id']}/screening-review",
        json={"expected_version": review["version"], "decision": "qualified", "note": "项目证明可迁移"},
        headers={"X-Actor-Id": "reviewer_1"},
    )
    assert reviewed.status_code == 200, reviewed.text
    after_review = api.get(f"/api/v1/candidate-profiles/{candidate['id']}").json()
    assert after_review["screening"]["ai_recommendation"] == "unqualified"
    assert after_review["screening"]["effective_outcome"] == "qualified"
    assert after_review["retention_expires_at"] is None

    patched = api.patch(
        f"/api/v1/candidate-profiles/{candidate['id']}",
        json={"expected_version": after_review["version"], "name": "Updated Candidate"},
    ).json()
    deleted = api.delete(
        f"/api/v1/candidate-profiles/{candidate['id']}?expected_version={patched['version']}"
    )
    assert deleted.status_code == 200
    assert all(item["id"] != candidate["id"] for item in api.get("/api/v1/candidate-profiles").json()["items"])


def test_unqualified_screening_is_automatically_purged_after_seven_days(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("INTERVIEWER_FILE_QUARANTINE_ROOT", str(tmp_path / "quarantine"))
    reset_private_file_storage_for_tests()
    reset_store_for_tests()
    api = TestClient(create_app())
    candidate, _ = _screen_candidate(api, "Auto Purge")

    result = RetentionService(get_store()).run_screening_retention(
        now=(datetime.now(timezone.utc) + timedelta(days=8)).isoformat()
    )
    assert result["candidate_ids"] == [candidate["id"]]
    assert api.get(f"/api/v1/candidate-profiles/{candidate['id']}").json()["status"] == "retention_purged"


def test_retention_worker_reconciles_legacy_score_before_starting_seven_day_deadline(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("INTERVIEWER_FILE_QUARANTINE_ROOT", str(tmp_path / "quarantine"))
    reset_private_file_storage_for_tests()
    reset_store_for_tests()
    api = TestClient(create_app())
    candidate, review = _screen_candidate(api, "Legacy Score Policy")
    with persistence_for(get_store()).transaction("org_default") as transaction:
        stored_review = transaction.resume_reviews.get(review["id"])
        stored_review["screening_score"] = 50
        stored_review["screening_recommendation"] = "manual_review"
        stored_review.pop("screening_policy_version", None)
        transaction.resume_reviews.update(stored_review, expected_version=stored_review["version"])
        stored_candidate = transaction.candidate_profiles.get(candidate["id"])
        stored_candidate["retention_reason"] = None
        stored_candidate["retention_expires_at"] = None
        transaction.candidate_profiles.update(stored_candidate, expected_version=stored_candidate["version"])

    first_run = RetentionService(get_store()).run_screening_retention(now="2026-08-28T12:00:00+00:00")
    assert first_run["candidate_ids"] == []
    assert first_run["reconciled_candidate_ids"] == [candidate["id"]]
    projected = api.get(f"/api/v1/candidate-profiles/{candidate['id']}").json()
    assert projected["screening"]["ai_recommendation"] == "unqualified"
    assert projected["retention_reason"] == "screening_unqualified"
    assert projected["retention_expires_at"] == "2026-09-04T12:00:00+00:00"

    second_run = RetentionService(get_store()).run_screening_retention(now="2026-09-05T12:00:00+00:00")
    assert second_run["candidate_ids"] == [candidate["id"]]


def test_failed_resume_review_can_be_retried_from_the_domain_endpoint(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("INTERVIEWER_FILE_QUARANTINE_ROOT", str(tmp_path / "quarantine"))
    reset_private_file_storage_for_tests()
    reset_store_for_tests()
    api = TestClient(create_app())
    position = api.post(
        "/api/v1/job-positions",
        json={"code": "retry-screening", "name": "重试岗位", "description": "Python 服务"},
    ).json()
    role = api.post(
        f"/api/v1/job-positions/{position['id']}/role-requirements",
        json={"title": "重试要求", "description": "Python 生产经验", "must_have_skills": ["python"]},
    ).json()
    candidate = api.post(
        "/api/v1/candidate-profiles",
        json={"name": "Retry Candidate", "email": "retry@example.com", "phone": "13800138003"},
    ).json()
    queued_resume = api.post(
        f"/api/v1/candidate-profiles/{candidate['id']}/resumes",
        files={"file": ("retry.pdf", _pdf("Python production service"), "application/pdf")},
    ).json()
    asyncio.run(OutboxWorker(get_store()).run_once())
    queued_review = api.post(
        f"/api/v1/candidate-profiles/{candidate['id']}/resume-reviews",
        json={
            "resume_document_id": queued_resume["resume_document_id"],
            "job_position_id": position["id"],
            "role_requirement_id": role["id"],
        },
    ).json()
    review = queued_review["review"]
    work_id = queued_review["job"]["id"]
    with persistence_for(get_store()).transaction("org_default") as transaction:
        running = transaction.outbox.start(work_id)
        failed_work = transaction.outbox.fail(
            work_id,
            "Provider timed out.",
            lease_token=running["lease_token"],
            error_code="provider_timeout",
            retryable=True,
        )
        current = transaction.resume_reviews.get(review["id"])
        current.update(
            {
                "status": "failed",
                "processing_stage": "failed",
                "error": {"code": "provider_timeout", "message": "Provider timed out.", "retryable": True},
            }
        )
        failed_review = transaction.resume_reviews.update(current, expected_version=current["version"])
    assert failed_work["status"] == "failed"

    retried = api.post(
        f"/api/v1/resume-reviews/{review['id']}/retry",
        json={"expected_version": failed_review["version"], "reason": "模型超时后人工重试"},
        headers={"X-Actor-Id": "interviewer_1"},
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["review"]["status"] == "queued"
    assert retried.json()["review"]["error"] is None
    assert retried.json()["job"]["status"] == "pending"
    assert retried.json()["job"]["attempt_count"] == 0
    assert retried.json()["job"]["replay_count"] == 1
    stale = api.post(
        f"/api/v1/resume-reviews/{review['id']}/retry",
        json={"expected_version": failed_review["version"], "reason": "重复点击"},
    )
    assert stale.status_code == 409
    with persistence_for(get_store()).transaction("org_default") as transaction:
        events = transaction.audit_events.list()
    assert any(
        item["action"] == "resume.review.retried"
        and item["resource_id"] == review["id"]
        and item["actor_id"] == "interviewer_1"
        for item in events
    )


def test_long_resume_is_queued_and_late_page_evidence_reaches_final_screening(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("INTERVIEWER_FILE_QUARANTINE_ROOT", str(tmp_path / "quarantine"))
    monkeypatch.setenv("INTERVIEWER_RESUME_SINGLE_PASS_TOKENS", "5")
    monkeypatch.setenv("INTERVIEWER_RESUME_CHUNK_TOKENS", "12")
    monkeypatch.setenv("INTERVIEWER_RESUME_REDUCE_TOKENS", "500")
    reset_private_file_storage_for_tests()
    reset_store_for_tests()
    api = TestClient(create_app())
    position = api.post(
        "/api/v1/job-positions",
        json={"code": "long-resume", "name": "长简历岗位", "description": "后端服务"},
    ).json()
    role = api.post(
        f"/api/v1/job-positions/{position['id']}/role-requirements",
        json={"title": "Python 要求", "description": "必须有 Python 经验", "must_have_skills": ["python"]},
    ).json()
    candidate = api.post(
        "/api/v1/candidate-profiles",
        json={
            "name": "Long Resume",
            "email": "long.resume@example.com",
            "phone": "13800138002",
            "job_position_id": position["id"],
        },
    ).json()
    queued = api.post(
        f"/api/v1/candidate-profiles/{candidate['id']}/resumes",
        data={"job_position_id": position["id"], "role_requirement_id": role["id"]},
        files={
            "file": (
                "long.pdf",
                _multi_page_pdf(
                    [
                        "Java service ownership and delivery",
                        "Database reliability and monitoring",
                        "Python production platform improved latency",
                    ]
                ),
                "application/pdf",
            )
        },
    )
    assert queued.status_code == 202, queued.text
    assert api.get(f"/api/v1/candidate-profiles/{candidate['id']}").json()["screening"]["effective_outcome"] == "processing"

    asyncio.run(OutboxWorker(get_store()).run_once())
    after_ingestion = api.get(f"/api/v1/candidate-profiles/{candidate['id']}").json()
    assert after_ingestion["screening"]["effective_outcome"] == "processing"
    assert after_ingestion["screening"]["processing_stage"] == "queued"

    asyncio.run(OutboxWorker(get_store()).run_once())
    completed = api.get(f"/api/v1/candidate-profiles/{candidate['id']}").json()
    assert completed["screening"]["effective_outcome"] == "qualified"
    assert completed["screening"]["score_policy_version"] == "candidate_screening_score.v1"
    review = api.get(f"/api/v1/resume-reviews/{completed['screening']['review_id']}").json()
    assert review["screening_recommendation"] == "qualified"
    assert review["screening_policy_version"] == "candidate_screening_score.v1"
    assert review["processing_strategy"] == "map_reduce"
    assert review["processing_progress"]["completed_chunks"] == review["processing_progress"]["total_chunks"]
    assert review["processing_progress"]["total_chunks"] >= 2
    python_match = next(item for item in review["matched_requirements"] if item["requirement"] == "python")
    assert 3 in python_match["source_pages"]
    assert any(chunk["page_end"] == 3 for chunk in review["evidence_chunks"])
