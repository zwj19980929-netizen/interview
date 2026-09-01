import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, StreamObject

from app.file_storage.provider import reset_private_file_storage_for_tests
from app.main import create_app
from app.persistence.provider import persistence_for
from app.repositories.provider import reset_store_for_tests
from app.workers.outbox import OutboxWorker
from app.repositories.provider import get_store


def client() -> TestClient:
    reset_store_for_tests()
    return TestClient(create_app())


def _pdf(text: str) -> bytes:
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


def test_position_resume_appointment_audio_and_review_loop(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("INTERVIEWER_FILE_QUARANTINE_ROOT", str(tmp_path / "quarantine"))
    reset_private_file_storage_for_tests()
    api = client()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    scheduled_start_at = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    scheduled_end_at = (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    position = api.post(
        "/api/v1/job-positions",
        json={"code": "backend", "name": "后端工程师", "description": "Python 服务开发"},
    )
    assert position.status_code == 200, position.text
    position_id = position.json()["id"]

    knowledge_base = api.post(
        f"/api/v1/job-positions/{position_id}/knowledge-bases",
        json={"name": "Python 后端题库"},
    )
    assert knowledge_base.status_code == 200, knowledge_base.text
    knowledge_base_id = knowledge_base.json()["id"]

    question_ids = []
    for title, text, answer in (
        ("GIL", "请解释 Python GIL。", "GIL 限制同一进程多个线程并行执行 Python 字节码。"),
        ("事务", "请解释数据库事务隔离。", "隔离级别用于平衡并发性能与脏读、不可重复读和幻读。"),
    ):
        question = api.post(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/questions",
            json={
                "knowledge_base_id": knowledge_base_id,
                "title": title,
                "question_text": text,
                "standard_answer": answer,
                "key_points": [answer],
                "rubric": {"semantic_correctness": 0.6, "role_relevance": 0.4},
                "skills": ["python"],
                "difficulty": "mid",
            },
        )
        assert question.status_code == 202, question.text
        question_ids.append(question.json()["id"])
        assert question.json()["validation_status"] == "valid"
        assert question.json()["speech_status"] == "pending"

    asyncio.run(OutboxWorker(get_store()).run_once())

    ready_kb = api.get(f"/api/v1/knowledge-bases/{knowledge_base_id}")
    assert ready_kb.json()["status"] == "ready"
    assert ready_kb.json()["readiness"]["speech_ready_count"] == 2

    structured_search = api.post(
        "/api/v1/questions/search",
        json={
            "job_position_id": position_id,
            "knowledge_base_ids": [knowledge_base_id],
            "query": "Python GIL",
            "filters": {"skills": ["python"], "difficulty": ["mid"]},
            "include_answer": True,
        },
    )
    assert structured_search.status_code == 200, structured_search.text
    assert {item["question_id"] for item in structured_search.json()["items"]} == set(question_ids)

    role = api.post(
        f"/api/v1/job-positions/{position_id}/role-requirements",
        json={
            "title": "后端工程师要求",
            "description": "熟悉 Python 和数据库并能说明项目取舍",
            "must_have_skills": ["python"],
            "interview_duration_minutes": 20,
        },
    )
    assert role.status_code == 200, role.text

    candidate = api.post(
        "/api/v1/candidate-profiles",
        json={"name": "张三", "email": "zhangsan@example.com", "phone": "13800138000"},
    )
    assert candidate.status_code == 200, candidate.text
    candidate_id = candidate.json()["id"]

    resume = api.post(
        f"/api/v1/candidate-profiles/{candidate_id}/resumes",
        files={
            "file": (
                "resume.pdf",
                _pdf("Python order service cache database performance improved 30 percent"),
                "application/pdf",
            )
        },
        headers={"Idempotency-Key": "position-flow-resume"},
    )
    assert resume.status_code == 202, resume.text
    asyncio.run(OutboxWorker(get_store()).run_once())
    resume_id = resume.json()["resume_document_id"]
    resume_detail = api.get(f"/api/v1/candidate-profiles/{candidate_id}/resumes/{resume_id}")
    assert resume_detail.json()["status"] == "ready"

    review = api.post(
        f"/api/v1/candidate-profiles/{candidate_id}/resume-reviews",
        json={
            "resume_document_id": resume_id,
            "job_position_id": position_id,
            "role_requirement_id": role.json()["id"],
        },
    )
    assert review.status_code == 202, review.text
    assert review.json()["review"]["status"] == "queued"
    review_id = review.json()["review"]["id"]
    asyncio.run(OutboxWorker(get_store()).run_once())
    ready_review = api.get(f"/api/v1/resume-reviews/{review_id}")
    assert ready_review.status_code == 200, ready_review.text
    assert ready_review.json()["status"] == "ready_for_review"

    qualified_review = api.patch(
        f"/api/v1/resume-reviews/{review_id}/screening-review",
        json={
            "expected_version": ready_review.json()["version"],
            "decision": "qualified",
            "note": "测试中人工确认简历项目证据符合岗位要求",
        },
    )
    assert qualified_review.status_code == 200, qualified_review.text
    asyncio.run(OutboxWorker(get_store()).run_once())

    experience = api.get(f"/api/v1/resume-reviews/{review_id}/experience-questions")
    assert experience.status_code == 200
    experience_question = experience.json()["items"][0]
    approved_experience = api.patch(
        f"/api/v1/experience-questions/{experience_question['id']}",
        json={"expected_version": experience_question["version"], "status": "approved"},
    )
    assert approved_experience.status_code == 200, approved_experience.text
    assert approved_experience.json()["speech_status"] == "deferred"
    assert approved_experience.json()["speech_asset_id"] is None
    assert not any(
        item.get("kind") == "question.speech.generate"
        and item.get("payload", {}).get("owner_type") == "experience_question"
        for item in get_store().outbox_work_items.values()
    )

    plan = api.post(
        "/api/v1/interview-plans/generate",
        json={
            "role_requirement_id": role.json()["id"],
            "job_position_id": position_id,
            "candidate_profile_id": candidate_id,
            "resume_review_id": review_id,
            "knowledge_base_ids": [knowledge_base_id],
            "question_count": 1,
        },
    )
    assert plan.status_code == 200, plan.text
    assert plan.json()["bank_slots"][0]["candidate_pool_count"] == 2
    assert plan.json()["experience_question_ids"] == [experience_question["id"]]
    selected_slot = deepcopy(plan.json()["bank_slots"][0])
    selected_slot["candidate_pool"] = [{"question_id": question_ids[1]}]
    selected_slot["display_question_id"] = question_ids[1]
    approved_plan = api.patch(
        f"/api/v1/interview-plans/{plan.json()['id']}",
        json={
            "expected_version": plan.json()["version"],
            "status": "approved",
            "bank_slots": [selected_slot],
        },
    )
    assert approved_plan.status_code == 200, approved_plan.text
    assert "items" not in approved_plan.json()
    assert [
        item["question_id"]
        for item in approved_plan.json()["bank_slots"][0]["candidate_pool"]
    ] == [question_ids[1]]
    immutable_edit = api.patch(
        f"/api/v1/interview-plans/{plan.json()['id']}",
        json={
            "expected_version": approved_plan.json()["version"],
            "bank_slots": [selected_slot],
        },
    )
    assert immutable_edit.status_code == 409
    assert immutable_edit.json()["error"]["code"] == "INTERVIEW_PLAN_IMMUTABLE"

    appointment = api.post(
        "/api/v1/interview-appointments",
        json={
            "plan_id": plan.json()["id"],
            "candidate_profile_id": candidate_id,
            "job_position_id": position_id,
            "scheduled_start_at": scheduled_start_at,
            "scheduled_end_at": scheduled_end_at,
        },
    )
    assert appointment.status_code == 200, appointment.text
    assert appointment.json()["speech_preparation"]["status"] == "not_requested"
    assert appointment.json()["speech_preparation"]["total"] == 1
    mismatched_voice = api.patch(
        f"/api/v1/interview-appointments/{appointment.json()['id']}",
        json={
            "expected_version": appointment.json()["version"],
            "settings": {"voice_profile_id": "another_voice"},
        },
    )
    assert mismatched_voice.status_code == 409
    assert mismatched_voice.json()["error"]["code"] == "APPOINTMENT_SPEECH_PROFILE_MISMATCH"
    appointment_patch = api.patch(
        f"/api/v1/interview-appointments/{appointment.json()['id']}",
        json={
            "expected_version": appointment.json()["version"],
            "admission_policy": {"device_readiness_ttl_seconds": 240},
        },
    )
    assert appointment_patch.status_code == 200, appointment_patch.text
    assert appointment_patch.json()["admission_policy"]["device_readiness_ttl_seconds"] == 240
    invalid_notice_version = api.post(
        "/api/v1/interview-appointments",
        json={
            "plan_id": plan.json()["id"],
            "candidate_profile_id": candidate_id,
            "job_position_id": position_id,
            "scheduled_start_at": scheduled_start_at,
            "scheduled_end_at": scheduled_end_at,
            "admission_policy": {"consent_version": "unregistered-version"},
        },
    )
    assert invalid_notice_version.status_code == 400
    assert invalid_notice_version.json()["error"]["code"] == "CONSENT_VERSION_INVALID"
    invited = api.post(
        f"/api/v1/interview-appointments/{appointment.json()['id']}/invite",
        json={"expires_at": scheduled_end_at},
    )
    assert invited.status_code == 200, invited.text
    assert invited.json()["join_url"].startswith("/#invite/")
    assert invited.json()["appointment"]["readiness"]["production_ready"] is False
    token = invited.json()["invitation_token"]
    public_invitation = api.get(f"/api/v1/public/interview-invitations/{token}")
    assert public_invitation.json()["consent"]["privacy_notice"]
    assert public_invitation.json()["consent"]["recording_notice"]
    assert public_invitation.json()["consent"]["notice_hash"]
    assert public_invitation.json()["speech_preparation"]["status"] == "not_requested"

    rejected_consent = api.post(
        f"/api/v1/public/interview-invitations/{token}/intake",
        json={
            "name": "张三",
            "email": "zhangsan@example.com",
            "phone": "13800138000",
            "consent": {"accepted": False, "version": "v1", "recording_accepted": False},
        },
    )
    assert rejected_consent.status_code == 409
    assert rejected_consent.json()["error"]["code"] == "CONSENT_REQUIRED"
    after_rejection = api.get(f"/api/v1/interview-appointments/{appointment.json()['id']}")
    assert after_rejection.json()["status"] == "invited"

    wrong_consent_version = api.post(
        f"/api/v1/public/interview-invitations/{token}/intake",
        json={
            "name": "张三",
            "email": "zhangsan@example.com",
            "phone": "13800138000",
            "consent": {"accepted": True, "version": "outdated", "recording_accepted": True},
        },
    )
    assert wrong_consent_version.status_code == 409
    assert wrong_consent_version.json()["error"]["code"] == "CONSENT_VERSION_INVALID"

    rejected_recording = api.post(
        f"/api/v1/public/interview-invitations/{token}/intake",
        json={
            "name": "张三",
            "email": "zhangsan@example.com",
            "phone": "13800138000",
            "consent": {"accepted": True, "version": "v1", "recording_accepted": False},
        },
    )
    assert rejected_recording.status_code == 409
    assert rejected_recording.json()["error"]["code"] == "RECORDING_CONSENT_REQUIRED"

    wrong = api.post(
        f"/api/v1/public/interview-invitations/{token}/intake",
        json={
            "name": "张三",
            "email": "wrong@example.com",
            "phone": "10000000000",
            "consent": {"accepted": True, "version": "v1", "recording_accepted": True},
        },
    )
    assert wrong.status_code == 403
    registered = api.post(
        f"/api/v1/public/interview-invitations/{token}/intake",
        json={
            "name": "张三",
            "email": "zhangsan@example.com",
            "phone": "13800138000",
            "consent": {"accepted": True, "version": "v1", "recording_accepted": True},
        },
    )
    assert registered.status_code == 200, registered.text
    assert registered.json()["status"] == "registered"
    assert registered.json()["email_reminder"]["status"] == "scheduled"
    assert registered.json()["speech_preparation"]["status"] == "queued"
    stored_appointment = api.get(f"/api/v1/interview-appointments/{appointment.json()['id']}").json()
    reminder_work = next(
        item
        for item in get_store().outbox_work_items.values()
        if item.get("kind") == "appointment.reminder.email"
    )
    assert reminder_work["payload"] == {"appointment_id": appointment.json()["id"]}
    assert reminder_work["id"] == stored_appointment["email_reminder"]["work_item_id"]
    speech_work = next(
        item
        for item in get_store().outbox_work_items.values()
        if item.get("kind") == "question.speech.generate"
        and item.get("payload", {}).get("appointment_id") == appointment.json()["id"]
    )
    assert speech_work["payload"]["speech_profile"] == approved_plan.json()["speech_profile_snapshot"]

    before_speech_ready = api.post(
        f"/api/v1/public/interview-invitations/{token}/readiness",
        json={
            "browser_supported": True,
            "microphone_granted": True,
            "audio_content_type": "audio/webm;codecs=opus",
        },
    )
    assert before_speech_ready.status_code == 200, before_speech_ready.text
    assert before_speech_ready.json()["can_start"] is False

    asyncio.run(OutboxWorker(get_store()).run_once())
    stored_appointment = api.get(f"/api/v1/interview-appointments/{appointment.json()['id']}").json()
    assert stored_appointment["speech_preparation"]["status"] == "ready"
    assert stored_appointment["speech_preparation"]["ready"] == 1
    speech_asset_id = stored_appointment["speech_preparation"]["items"][0]["asset_id"]
    with persistence_for(get_store()).transaction("org_default") as transaction:
        speech_asset = transaction.question_speech_assets.get(speech_asset_id)
        stored_experience = transaction.experience_questions.get(experience_question["id"])
    frozen_profile = approved_plan.json()["speech_profile_snapshot"]
    assert speech_asset["model_configuration_id"] == frozen_profile["model_configuration_id"]
    assert speech_asset["model_configuration_version"] == frozen_profile["model_configuration_version"]
    assert speech_asset["voice_profile_id"] == frozen_profile["voice_profile_id"]
    assert speech_asset["language"] == frozen_profile["language"]
    assert speech_asset["audio_format"] == frozen_profile["audio_format"]
    assert speech_asset["speaking_rate"] == frozen_profile["speaking_rate"]
    assert speech_asset["speech_profile_fingerprint"] == frozen_profile["fingerprint"]
    assert speech_asset["knowledge_base_speech_profile_revisions"] == frozen_profile[
        "knowledge_base_revisions"
    ]
    assert stored_experience["speech_status"] == "deferred"
    assert stored_experience["speech_asset_id"] is None

    device_ready = api.post(
        f"/api/v1/public/interview-invitations/{token}/readiness",
        json={
            "browser_supported": True,
            "microphone_granted": True,
            "audio_content_type": "audio/webm;codecs=opus",
        },
    )
    assert device_ready.status_code == 200, device_ready.text
    assert device_ready.json()["can_start"] is True

    with ThreadPoolExecutor(max_workers=2) as pool:
        started_responses = list(
            pool.map(
                lambda _: api.post(f"/api/v1/public/interview-invitations/{token}/start"),
                range(2),
            )
        )
    assert all(response.status_code == 200 for response in started_responses), [
        response.text for response in started_responses
    ]
    assert len({response.json()["interview_id"] for response in started_responses}) == 1
    started = started_responses[0]
    interview_id = started.json()["interview_id"]
    assert started.json()["status"] == "in_progress"
    session = api.get(f"/api/v1/interviews/{interview_id}").json()
    assert session["candidate"]["privacy_accepted"] is True
    assert session["candidate"]["recording_accepted"] is True
    assert session["candidate"]["consent_notice_hash"]
    assert session["turns"][0]["question_id"] == question_ids[1]
    assert len(session["question_selections"]) == 1
    assert [item["phase"] for item in session["turns"]] == ["position_bank", "resume_experience"]
    resume_turn = next(item for item in session["turns"] if item["phase"] == "resume_experience")
    assert resume_turn["question_snapshot"]["speech_asset_id"] == speech_asset_id
    assert session["plan_snapshot"]["speech_profile_snapshot"] == frozen_profile

    for expected_phase in ("position_bank", "resume_experience"):
        session = api.get(f"/api/v1/interviews/{interview_id}").json()
        turn = next(item for item in session["turns"] if item["id"] == session["current_turn_id"])
        assert turn["phase"] == expected_phase
        answer = api.post(
            f"/api/v1/interviews/{interview_id}/audio-answers",
            json={
                "turn_id": turn["id"],
                "audio_uri": f"/media/{interview_id}/{turn['id']}.webm",
                "development_transcript": turn["question_snapshot"]["standard_answer"],
                "duration_seconds": 10,
            },
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["answer"]["transcript_source"] == "server_batch"
        assert answer.json()["evaluation"]["status"] == "pending"
        asyncio.run(OutboxWorker(get_store()).run_once())
        after_root = api.get(f"/api/v1/interviews/{interview_id}").json()
        if after_root.get("current_turn_id"):
            followup = next(
                item for item in after_root["turns"]
                if item["id"] == after_root["current_turn_id"]
            )
            if followup.get("is_followup"):
                followup_answer = api.post(
                    f"/api/v1/interviews/{interview_id}/audio-answers",
                    json={
                        "turn_id": followup["id"],
                        "audio_uri": f"/media/{interview_id}/{followup['id']}.webm",
                        "development_transcript": followup["question_snapshot"]["standard_answer"],
                        "duration_seconds": 8,
                    },
                )
                assert followup_answer.status_code == 200, followup_answer.text
                asyncio.run(OutboxWorker(get_store()).run_once())

    report = api.get(f"/api/v1/interviews/{interview_id}/report")
    assert report.status_code == 200, report.text
    assert report.json()["job_fit_level"] in {
        "strong_match",
        "match",
        "partial_match",
        "manual_review",
    }
    assert report.json()["human_decision_required"] is True
    csv_export = api.get(f"/api/v1/interviews/{interview_id}/report/export?format=csv")
    assert csv_export.status_code == 200
    assert csv_export.headers["content-type"].startswith("text/csv")
    assert "human_decision_required" in csv_export.text
    json_export = api.get(f"/api/v1/interviews/{interview_id}/report/export?format=json")
    assert json_export.status_code == 200
    assert json_export.json()["human_decision_required"] is True

    enterprise_review = api.get(f"/api/v1/interviews/{interview_id}/review")
    assert enterprise_review.status_code == 200
    answer_id = enterprise_review.json()["turns"][0]["answer"]["id"]
    audio_url = api.post(f"/api/v1/interviews/{interview_id}/answers/{answer_id}/audio-url")
    assert audio_url.status_code == 200
    corrected = api.patch(
        f"/api/v1/interviews/{interview_id}/answers/{answer_id}/transcript",
        json={"final_transcript": "人工复核后的完整回答", "reason": "听录音后修正"},
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["transcript_revision"] == 2
    completed = api.post(
        f"/api/v1/interviews/{interview_id}/review-complete",
        json={"notes": "已完成人工复核"},
    )
    assert completed.status_code == 200
    assert completed.json()["ai_decision_used"] is False

    repeated_start = api.post(f"/api/v1/public/interview-invitations/{token}/start")
    assert repeated_start.status_code == 200
    assert repeated_start.json()["interview_id"] == interview_id

    no_recording_appointment = api.post(
        "/api/v1/interview-appointments",
        json={
            "plan_id": plan.json()["id"],
            "candidate_profile_id": candidate_id,
            "job_position_id": position_id,
            "scheduled_start_at": scheduled_start_at,
            "scheduled_end_at": scheduled_end_at,
            "settings": {"record_audio": False},
        },
    )
    no_recording_invite = api.post(
        f"/api/v1/interview-appointments/{no_recording_appointment.json()['id']}/invite",
        json={"expires_at": scheduled_end_at},
    )
    no_recording_token = no_recording_invite.json()["invitation_token"]
    no_recording_public = api.get(f"/api/v1/public/interview-invitations/{no_recording_token}")
    assert no_recording_public.json()["consent"]["recording_required"] is False
    assert no_recording_public.json()["consent"]["recording_notice"] is None
    no_recording_intake = api.post(
        f"/api/v1/public/interview-invitations/{no_recording_token}/intake",
        json={
            "name": "张三",
            "email": "zhangsan@example.com",
            "phone": "13800138000",
            "consent": {"accepted": True, "version": "v1", "recording_accepted": False},
        },
    )
    assert no_recording_intake.status_code == 200, no_recording_intake.text


def test_cross_position_knowledge_base_is_rejected() -> None:
    api = client()
    first = api.post("/api/v1/job-positions", json={"code": "one", "name": "岗位一"}).json()
    second = api.post("/api/v1/job-positions", json={"code": "two", "name": "岗位二"}).json()
    knowledge_base = api.post(
        f"/api/v1/job-positions/{first['id']}/knowledge-bases", json={"name": "题库一"}
    ).json()
    role = api.post(
        f"/api/v1/job-positions/{second['id']}/role-requirements",
        json={"title": "要求", "description": "Python", "must_have_skills": ["python"]},
    ).json()
    candidate = api.post(
        "/api/v1/candidate-profiles",
        json={"name": "跨岗位候选人", "email": "scope@example.com", "phone": "13800138001"},
    ).json()
    response = api.post(
        "/api/v1/interview-plans/generate",
        json={
            "role_requirement_id": role["id"],
            "job_position_id": second["id"],
            "candidate_profile_id": candidate["id"],
            "knowledge_base_ids": [knowledge_base["id"]],
            "question_count": 1,
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "KNOWLEDGE_BASE_POSITION_MISMATCH"

    search = api.post(
        "/api/v1/questions/search",
        json={
            "job_position_id": second["id"],
            "knowledge_base_ids": [knowledge_base["id"]],
            "query": "Python",
        },
    )
    assert search.status_code == 409
    assert search.json()["error"]["code"] == "KNOWLEDGE_BASE_POSITION_MISMATCH"

    current_second = api.get(f"/api/v1/job-positions/{second['id']}").json()
    assigned = api.post(
        f"/api/v1/job-positions/{second['id']}/knowledge-base-assignments",
        json={
            "knowledge_base_id": knowledge_base["id"],
            "expected_position_version": current_second["version"],
        },
    )
    assert assigned.status_code == 200, assigned.text
    assert knowledge_base["id"] in assigned.json()["position"]["knowledge_base_ids"]
    assert assigned.json()["knowledge_base"]["speech_profile"] == knowledge_base["speech_profile"]
    assert [item["id"] for item in api.get(
        f"/api/v1/job-positions/{second['id']}/knowledge-bases"
    ).json()["items"]] == [knowledge_base["id"]]

    linked_search = api.post(
        "/api/v1/questions/search",
        json={
            "job_position_id": second["id"],
            "knowledge_base_ids": [knowledge_base["id"]],
            "query": "Python",
        },
    )
    assert linked_search.status_code == 200, linked_search.text
    assert linked_search.json()["items"] == []

    linked_plan = api.post(
        "/api/v1/interview-plans/generate",
        json={
            "role_requirement_id": role["id"],
            "job_position_id": second["id"],
            "candidate_profile_id": candidate["id"],
            "knowledge_base_ids": [knowledge_base["id"]],
            "question_count": 1,
        },
    )
    assert linked_plan.status_code == 409
    assert linked_plan.json()["error"]["code"] == "KNOWLEDGE_BASE_NOT_READY"


def test_position_delete_requires_exact_confirmation_and_purges_only_its_candidates() -> None:
    api = client()
    target = api.post(
        "/api/v1/job-positions", json={"code": "delete-me", "name": "待删除岗位"}
    ).json()
    survivor = api.post(
        "/api/v1/job-positions", json={"code": "keep-me", "name": "保留岗位"}
    ).json()
    shared_bank = api.post(
        f"/api/v1/job-positions/{target['id']}/knowledge-bases", json={"name": "共享题库"}
    ).json()
    survivor = api.get(f"/api/v1/job-positions/{survivor['id']}").json()
    assigned = api.post(
        f"/api/v1/job-positions/{survivor['id']}/knowledge-base-assignments",
        json={
            "knowledge_base_id": shared_bank["id"],
            "expected_position_version": survivor["version"],
        },
    )
    assert assigned.status_code == 200, assigned.text
    deleted_candidate = api.post(
        "/api/v1/candidate-profiles",
        json={
            "name": "删除候选人",
            "email": "delete@example.com",
            "phone": "13800138011",
            "job_position_id": target["id"],
        },
    ).json()
    kept_candidate = api.post(
        "/api/v1/candidate-profiles",
        json={
            "name": "保留候选人",
            "email": "keep@example.com",
            "phone": "13800138012",
            "job_position_id": survivor["id"],
        },
    ).json()

    impact = api.get(f"/api/v1/job-positions/{target['id']}/deletion-impact")
    assert impact.status_code == 200, impact.text
    assert impact.json()["candidate_count"] == 1

    wrong = api.request(
        "DELETE",
        f"/api/v1/job-positions/{target['id']}",
        json={"expected_version": impact.json()["position_version"], "confirmation": "写错了"},
    )
    assert wrong.status_code == 422
    assert wrong.json()["error"]["code"] == "JOB_POSITION_DELETE_CONFIRMATION_INVALID"
    assert api.get(f"/api/v1/candidate-profiles/{deleted_candidate['id']}").json()["status"] == "active"

    deleted = api.request(
        "DELETE",
        f"/api/v1/job-positions/{target['id']}",
        headers={"X-Actor-Id": "position_admin"},
        json={
            "expected_version": impact.json()["position_version"],
            "confirmation": target["name"],
        },
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["candidate_count"] == 1
    assert [item["id"] for item in api.get("/api/v1/job-positions").json()["items"]] == [survivor["id"]]
    assert [item["id"] for item in api.get("/api/v1/candidate-profiles").json()["items"]] == [kept_candidate["id"]]
    purged = api.get(f"/api/v1/candidate-profiles/{deleted_candidate['id']}").json()
    assert purged["status"] == "retention_purged"
    assert purged["name"] == "[retention_purged]"
    assert purged["email"] == ""
    assert api.get(f"/api/v1/knowledge-bases/{shared_bank['id']}").status_code == 200
    assert [item["id"] for item in api.get(
        f"/api/v1/job-positions/{survivor['id']}/knowledge-bases"
    ).json()["items"]] == [shared_bank["id"]]
