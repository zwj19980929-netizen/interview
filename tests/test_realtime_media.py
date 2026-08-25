from pathlib import Path
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.provider import reset_store_for_tests
from app.repositories.provider import get_store
from app.persistence.provider import persistence_for


def _create_started_interview(api: TestClient) -> dict:
    position = api.post(
        "/api/v1/job-positions",
        json={"code": "realtime", "name": "实时系统工程师"},
    ).json()
    knowledge_base = api.post(
        "/api/v1/job-positions/%s/knowledge-bases" % position["id"],
        json={"name": "实时系统题库"},
    ).json()
    question = api.post(
        "/api/v1/knowledge-bases/%s/questions" % knowledge_base["id"],
        json={
            "knowledge_base_id": knowledge_base["id"],
            "title": "实时系统设计",
            "question_text": "请说明实时语音面试中的断线恢复策略。",
            "standard_answer": "保存会话状态、音频序号和最终转写，重连后从最近轮次恢复。",
            "key_points": ["保存会话状态", "音频序号", "从最近轮次恢复"],
            "difficulty": "senior",
            "skills": ["realtime", "system_design"],
            "rubric": {"semantic_correctness": 1.0},
        },
    )
    assert question.status_code == 200, question.text

    role = api.post(
        "/api/v1/job-positions/%s/role-requirements" % position["id"],
        json={
            "title": "实时系统工程师",
            "description": "负责 realtime system design 和线上恢复。",
            "must_have_skills": ["realtime", "system_design"],
            "seniority": "senior",
            "interview_duration_minutes": 20,
        },
    )
    assert role.status_code == 200, role.text
    candidate = api.post(
        "/api/v1/candidate-profiles",
        json={"name": "实时候选人", "email": "realtime@example.com", "phone": "13800138003"},
    ).json()

    plan = api.post(
        "/api/v1/interview-plans/generate",
        json={
            "role_requirement_id": role.json()["id"],
            "job_position_id": position["id"],
            "candidate_profile_id": candidate["id"],
            "knowledge_base_ids": [knowledge_base["id"]],
            "question_count": 1,
        },
    )
    assert plan.status_code == 200, plan.text

    approved = api.patch(
        "/api/v1/interview-plans/%s" % plan.json()["id"],
        json={"expected_version": plan.json()["version"], "status": "approved"},
    )
    assert approved.status_code == 200, approved.text

    now = datetime.now(timezone.utc).replace(microsecond=0)
    appointment = api.post(
        "/api/v1/interview-appointments",
        json={
            "plan_id": approved.json()["id"],
            "candidate_profile_id": candidate["id"],
            "job_position_id": position["id"],
            "scheduled_start_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "scheduled_end_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "settings": {"record_audio": True, "record_video": False, "avatar_id": "avatar_default_cn"},
        },
    )
    assert appointment.status_code == 200, appointment.text
    invitation = api.post(
        "/api/v1/interview-appointments/%s/invite" % appointment.json()["id"],
        json={"expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")},
    )
    assert invitation.status_code == 200, invitation.text
    token = invitation.json()["invitation_token"]
    notice = api.get("/api/v1/public/interview-invitations/%s" % token).json()["consent"]
    intake = api.post(
        "/api/v1/public/interview-invitations/%s/intake" % token,
        json={
            "name": "实时候选人",
            "email": "realtime@example.com",
            "phone": "13800138003",
            "consent": {"accepted": True, "version": notice["version"], "recording_accepted": True},
        },
    )
    assert intake.status_code == 200, intake.text
    readiness = api.post(
        "/api/v1/public/interview-invitations/%s/readiness" % token,
        json={"browser_supported": True, "microphone_granted": True, "audio_content_type": "audio/webm"},
    )
    assert readiness.status_code == 200, readiness.text
    started = api.post("/api/v1/public/interview-invitations/%s/start" % token)
    assert started.status_code == 200, started.text
    candidate_token = started.json()["candidate_join_url"].split("token=", 1)[1]
    interview = api.get("/api/v1/interviews/%s" % started.json()["interview_id"]).json()
    assert "candidate_session_token" not in interview
    assert "candidate_join_url" not in interview
    interview["candidate_session_token"] = candidate_token
    return interview


def test_avatar_and_realtime_audio_flow(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_MEDIA_PATH", str(tmp_path))
    reset_store_for_tests()
    api = TestClient(create_app())
    interview = _create_started_interview(api)
    interview_id = interview["id"]

    detail = api.get("/api/v1/interviews/%s" % interview_id).json()
    turn_id = detail["current_turn_id"]
    avatar = api.post(
        "/api/v1/interviews/%s/avatar/speak" % interview_id,
        json={"turn_id": turn_id, "language": "zh-CN", "voice": "default"},
    )
    assert avatar.status_code == 200, avatar.text
    assert avatar.json()["mode"] == "browser_speech"
    assert avatar.json()["text"] == detail["turns"][0]["question_spoken_text"]
    assert avatar.json()["provider"]["provider_id"] == "mock"

    websocket_url = "/api/v1/interviews/%s/live?role=candidate&token=%s" % (
        interview_id,
        interview["candidate_session_token"],
    )
    with api.websocket_connect(websocket_url) as websocket:
        assert websocket.receive_json()["type"] == "session.state.changed"
        assert websocket.receive_json()["type"] == "question.selected"

        websocket.send_json({"type": "session.ready", "payload": {"source": "test_candidate"}})
        assert websocket.receive_json()["type"] == "session.participant.ready"
        ready_state = websocket.receive_json()
        assert ready_state["type"] == "session.state.changed"
        assert ready_state["payload"]["status"] == "in_progress"

        websocket.send_json(
            {
                "type": "candidate.media.start",
                "turn_id": turn_id,
                "payload": {"mime_type": "audio/webm;codecs=opus", "timeslice_ms": 400},
            }
        )
        assert websocket.receive_json()["type"] == "media.recording.started"
        websocket.send_bytes(b"mock-webm-opus-audio")
        websocket.send_json({"type": "candidate.media.stop", "turn_id": turn_id, "payload": {}})
        recording_stopped = websocket.receive_json()
        assert recording_stopped["type"] == "media.recording.stopped"
        assert recording_stopped["payload"]["byte_count"] == len(b"mock-webm-opus-audio")

        audio_uri = recording_stopped["payload"]["audio_uri"]

    candidate_detail = api.get(
        "/api/v1/public/interviews/%s" % interview_id,
        headers={"X-Candidate-Session-Token": interview["candidate_session_token"]},
    )
    assert candidate_detail.status_code == 200, candidate_detail.text
    assert "candidate_session_token" not in candidate_detail.json()
    assert "question_snapshot" not in candidate_detail.json()["turns"][0]
    assert api.get(
        "/api/v1/public/interviews/%s" % interview_id,
        headers={"X-Candidate-Session-Token": "invalid"},
    ).status_code == 403
    out_of_scope = api.post(
        "/api/v1/public/interviews/%s/audio-answers" % interview_id,
        headers={"X-Candidate-Session-Token": interview["candidate_session_token"]},
        json={"turn_id": turn_id, "audio_uri": "/media/another-session/another-turn/recording.webm"},
    )
    assert out_of_scope.status_code == 403
    assert out_of_scope.json()["error"]["code"] == "CANDIDATE_AUDIO_SCOPE_INVALID"

    submitted = api.post(
        "/api/v1/public/interviews/%s/audio-answers" % interview_id,
        headers={"X-Candidate-Session-Token": interview["candidate_session_token"]},
        json={
            "turn_id": turn_id,
            "audio_uri": audio_uri,
            "content_type": "audio/webm;codecs=opus",
            "language": "zh-CN",
            "duration_seconds": 12,
            "development_transcript": "我会保存会话状态和音频序号，断线重连后从最近轮次恢复。",
            "development_confidence": 0.88,
        },
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "report_ready"

    completed = api.get("/api/v1/interviews/%s" % interview_id)
    assert completed.status_code == 200
    assert completed.json()["status"] == "report_ready"
    assert len(completed.json()["answers"]) == 1
    audio_uri = completed.json()["answers"][0]["audio_uri"]
    assert audio_uri == recording_stopped["payload"]["audio_uri"]

    answer_id = completed.json()["answers"][0]["id"]
    grant = api.post(f"/api/v1/interviews/{interview_id}/answers/{answer_id}/audio-url")
    assert grant.status_code == 200
    assert grant.json()["access_mode"] == "signed_private"
    recording = api.get(grant.json()["url"])
    assert recording.status_code == 200
    assert recording.content == b"mock-webm-opus-audio"
    assert recording.headers["content-type"].startswith("audio/webm")
    audit = api.get("/api/v1/admin/audit-events").json()["items"]
    assert any(item["action"] == "answer.audio.downloaded" and item["resource_id"] == answer_id for item in audit)


def test_candidate_websocket_rejects_invalid_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_MEDIA_PATH", str(tmp_path))
    reset_store_for_tests()
    api = TestClient(create_app())
    interview = _create_started_interview(api)

    with api.websocket_connect(
        "/api/v1/interviews/%s/live?role=candidate&token=invalid" % interview["id"]
    ) as websocket:
        error = websocket.receive_json()
        assert error["type"] == "error"
        assert error["payload"]["code"] == "CANDIDATE_SESSION_TOKEN_INVALID"


def test_server_streaming_stt_websocket_produces_authoritative_final(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_MEDIA_PATH", str(tmp_path))
    reset_store_for_tests()
    api = TestClient(create_app())
    interview = _create_started_interview(api)
    interview_id = interview["id"]
    turn_id = api.get(f"/api/v1/interviews/{interview_id}").json()["current_turn_id"]
    url = f"/api/v1/interviews/{interview_id}/stt-stream?token={interview['candidate_session_token']}"
    transcript = "保存会话状态和音频序号，断线后从最近轮次恢复。"
    with api.websocket_connect(url) as websocket:
        websocket.send_json(
            {
                "type": "stream.open",
                "payload": {
                    "turn_id": turn_id,
                    "content_type": "audio/webm;codecs=opus",
                    "development_transcript": transcript,
                    "development_confidence": 0.91,
                },
            }
        )
        assert websocket.receive_json()["type"] == "stream.ready"
        websocket.send_bytes(b"server-streaming-audio")
        partial = websocket.receive_json()
        assert partial["type"] == "transcript.partial"
        websocket.send_json({"type": "stream.finish", "payload": {"duration_seconds": 8}})
        events = [websocket.receive_json() for _ in range(4)]
        assert [item["type"] for item in events] == [
            "transcript.final",
            "stream.closed",
            "evaluation.completed",
            "interview.completed",
        ]
    completed = api.get(f"/api/v1/interviews/{interview_id}").json()
    assert completed["answers"][0]["final_transcript"] == transcript
    assert completed["answers"][0]["transcript_source"] == "server_streaming"
    assert completed["answers"][0]["stt_provider"]["provider_id"] == "mock"


def test_heartbeat_and_monitor_timeout_share_lifecycle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_MEDIA_PATH", str(tmp_path))
    monkeypatch.setenv("INTERVIEWER_HEARTBEAT_TIMEOUT_SECONDS", "5")
    reset_store_for_tests()
    api = TestClient(create_app())
    interview = _create_started_interview(api)
    with api.websocket_connect(
        f"/api/v1/interviews/{interview['id']}/live?role=candidate&token={interview['candidate_session_token']}"
    ) as websocket:
        websocket.receive_json()
        websocket.receive_json()
        websocket.send_json({"type": "ping"})
        assert websocket.receive_json()["type"] == "pong"
    persistence = persistence_for(get_store())
    with persistence.transaction("org_default") as transaction:
        session = transaction.interview_sessions.get(interview["id"])
        session["last_activity_at"] = "2020-01-01T00:00:00Z"
        transaction.interview_sessions.update(session, expected_version=session["version"])
    monitored = api.post("/api/v1/admin/session-monitor/run")
    assert monitored.status_code == 200
    assert monitored.json()["timed_out_count"] == 1
    assert api.get(f"/api/v1/interviews/{interview['id']}").json()["status"] == "paused"


def test_websocket_ready_is_recorded_on_admitted_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_MEDIA_PATH", str(tmp_path))
    reset_store_for_tests()
    api = TestClient(create_app())
    interview = _create_started_interview(api)
    interview_id = interview["id"]

    websocket_url = "/api/v1/interviews/%s/live?role=candidate&token=%s" % (
        interview_id,
        interview["candidate_session_token"],
    )
    with api.websocket_connect(websocket_url) as websocket:
        initial = websocket.receive_json()
        assert initial["type"] == "session.state.changed"
        assert initial["payload"]["status"] == "in_progress"
        assert websocket.receive_json()["type"] == "question.selected"
        websocket.send_json({"type": "session.ready", "payload": {"source": "candidate_room"}})
        assert websocket.receive_json()["type"] == "session.participant.ready"
        ready_state = websocket.receive_json()
        assert ready_state["type"] == "session.state.changed"
        assert ready_state["payload"]["status"] == "in_progress"

    events = api.get("/api/v1/interviews/%s/events" % interview_id).json()["items"]
    assert [item["type"] for item in events][-1] == "interview.participant_ready"


def test_websocket_interviewer_control_uses_lifecycle_and_rejects_candidate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_MEDIA_PATH", str(tmp_path))
    reset_store_for_tests()
    api = TestClient(create_app())
    interview = _create_started_interview(api)
    interview_id = interview["id"]

    candidate_url = "/api/v1/interviews/%s/live?role=candidate&token=%s" % (
        interview_id,
        interview["candidate_session_token"],
    )
    with api.websocket_connect(candidate_url) as websocket:
        assert websocket.receive_json()["type"] == "session.state.changed"
        assert websocket.receive_json()["type"] == "question.selected"
        websocket.send_json({"type": "interviewer.control.start", "payload": {}})
        forbidden = websocket.receive_json()
        assert forbidden["type"] == "error"
        assert forbidden["payload"]["code"] == "INTERVIEWER_CONTROL_FORBIDDEN"

    with api.websocket_connect("/api/v1/interviews/%s/live?role=interviewer" % interview_id) as websocket:
        assert websocket.receive_json()["type"] == "session.state.changed"
        assert websocket.receive_json()["type"] == "question.selected"
        websocket.send_json({"type": "interviewer.control.pause", "payload": {"reason": "operator pause"}})
        state_changed = websocket.receive_json()
        assert state_changed["type"] == "session.state.changed"
        assert state_changed["payload"]["status"] == "paused"

    detail = api.get("/api/v1/interviews/%s" % interview_id).json()
    assert detail["status"] == "paused"
    assert [item["type"] for item in detail["lifecycle_events"]][-1] == "interview.paused"
